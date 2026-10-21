"""The HTTP API of the node."""

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urlsplit

import httpx2
from cryptography.fernet import InvalidToken
from fastapi import Depends, FastAPI, Header, HTTPException, Response
from fastapi.responses import StreamingResponse
from pydantic import AfterValidator, BaseModel, StringConstraints

from titan import agent, db, keys


class SignIn(BaseModel):
    name: str
    code: str


NotEmpty = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


def _http_url(url: str) -> str:
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("the URL must start with http:// or https:// and a host")
    return url


class ModelIn(BaseModel):
    # Kept as typed: a URL type would change it, adding a slash.
    url: Annotated[NotEmpty, AfterValidator(_http_url)]
    # None or empty for a model that needs no key, such as a local one.
    key: str | None = None


class ModelOut(BaseModel):
    name: str
    url: str
    has_key: bool
    current: bool


class Settings(BaseModel):
    models: list[ModelOut]


class CurrentModel(BaseModel):
    name: NotEmpty


class ChatIn(BaseModel):
    message: NotEmpty


# Seconds the model may stay silent between two chunks of a reply.
MODEL_SILENCE = 300.0
# Seconds to connect to the model, send to it or wait for a free
# connection: httpx2's default.
MODEL_CONNECT = 5.0


def create_app(
    database_url: str | None = None,
    key_file: str | None = None,
    model_transport: httpx2.AsyncBaseTransport | None = None,
) -> FastAPI:
    engine = db.make_engine(database_url or os.environ["TITAN_DATABASE_URL"])
    node_key = keys.load_or_create(Path(key_file or os.environ["TITAN_KEY_FILE"]))
    # The read timeout is the longest silence between two chunks, never a
    # limit on the whole reply; a model may think for minutes before it
    # writes. Redirects are not followed, so the key goes to no other host.
    models = httpx2.AsyncClient(
        transport=model_transport,
        timeout=httpx2.Timeout(MODEL_CONNECT, read=MODEL_SILENCE),
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        yield
        await models.aclose()
        await engine.dispose()

    # No /docs or /openapi.json: they would be routes without a token.
    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    async def current_user(authorization: Annotated[str, Header()] = "") -> int:
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() == "bearer" and token:
            async with db.transaction(engine):
                user_id = await db.user_of_token(token)
            if user_id is not None:
                return user_id
        raise HTTPException(401, headers={"WWW-Authenticate": "Bearer"})

    @app.get("/health")
    async def health(response: Response) -> dict[str, str]:
        if await db.ping(engine):
            return {"status": "ok"}
        response.status_code = 503
        return {"status": "database unavailable"}

    @app.post("/sign-in")
    async def sign_in(body: SignIn) -> dict[str, str]:
        async with db.transaction(engine):
            token = await db.sign_in(body.name, body.code)
        if token is None:
            raise HTTPException(401)
        return {"token": token}

    # Every route below takes current_user; a test checks that none is open.
    User = Annotated[int, Depends(current_user)]

    @app.get("/me")
    async def me(user_id: User) -> dict[str, str]:
        async with db.transaction(engine):
            return {"name": await db.user_name(user_id)}

    @app.get("/settings")
    async def settings(user_id: User) -> Settings:
        async with db.transaction(engine):
            models = await db.models_of(user_id)
        return Settings(
            models=[
                ModelOut(
                    name=m.name,
                    url=m.url,
                    has_key=m.encrypted_key is not None,
                    current=m.current,
                )
                for m in models
            ]
        )

    # A model name may hold a slash, as in "openai/gpt-4o".
    @app.put("/settings/models/{name:path}", status_code=204)
    async def save_model(user_id: User, name: NotEmpty, body: ModelIn) -> None:
        encrypted_key = node_key.encrypt(body.key.encode()) if body.key else None
        model = db.Model(name=name, url=body.url, encrypted_key=encrypted_key)
        async with db.transaction(engine):
            await db.save_model(user_id, model)

    @app.delete("/settings/models/{name:path}", status_code=204)
    async def remove_model(user_id: User, name: NotEmpty) -> None:
        async with db.transaction(engine):
            if not await db.remove_model(user_id, name):
                raise HTTPException(404, "no model of that name")

    @app.put("/settings/current-model", status_code=204)
    async def use_model(user_id: User, body: CurrentModel) -> None:
        async with db.transaction(engine):
            if not await db.use_model(user_id, body.name):
                raise HTTPException(404, "no model of that name")

    @app.post("/chat")
    async def chat(user_id: User, body: ChatIn) -> StreamingResponse:
        async with db.transaction(engine):
            model = await db.current_model(user_id)
            history = await db.messages_of(user_id)
        if model is None:
            raise HTTPException(409, "no current model")
        question = db.Message("user", body.message)
        headers = {}
        if model.encrypted_key is not None:
            try:
                key = node_key.decrypt(model.encrypted_key).decode()
            except InvalidToken:
                # The node key changed, as when its volume was lost.
                raise HTTPException(
                    500,
                    f'the key of model "{model.name}" cannot be read;'
                    " add the model again",
                ) from None
            headers["Authorization"] = f"Bearer {key}"
        messages = agent.conversation(history, question)

        async def ask(
            messages: list[agent.Message], tools: list[dict[str, Any]] | None
        ) -> httpx2.Response:
            body: dict[str, Any] = {"model": model.name, "messages": messages}
            if tools:
                body["tools"] = tools
            request = models.build_request(
                "POST",
                model.url.rstrip("/") + "/chat/completions",
                headers=headers,
                json={**body, "stream": True},
            )
            try:
                response = await models.send(request, stream=True)
            except httpx2.TransportError:
                raise HTTPException(502, "the model is unreachable") from None
            if response.status_code != 200:
                await response.aclose()
                raise HTTPException(502, f"the model answered {response.status_code}")
            return response

        # Sent before the reply starts, so a refusal is still an HTTP status.
        response = await ask(messages, [agent.RUN_PLAN])
        return StreamingResponse(
            agent.converse(engine, user_id, question, messages, ask, response),
            media_type="text/plain; charset=utf-8",
        )

    return app
