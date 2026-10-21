"""The HTTP API of the node."""

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, Header, HTTPException, Response
from pydantic import AfterValidator, BaseModel, StringConstraints

from titan import db, keys


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


def create_app(database_url: str | None = None, key_file: str | None = None) -> FastAPI:
    engine = db.make_engine(database_url or os.environ["TITAN_DATABASE_URL"])
    node_key = keys.load_or_create(Path(key_file or os.environ["TITAN_KEY_FILE"]))

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        yield
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

    return app
