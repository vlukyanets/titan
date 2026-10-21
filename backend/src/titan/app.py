"""The HTTP API of the node."""

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, Response
from pydantic import BaseModel

from titan import db


class SignIn(BaseModel):
    name: str
    code: str


def create_app(database_url: str | None = None) -> FastAPI:
    engine = db.make_engine(database_url or os.environ["TITAN_DATABASE_URL"])

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

    return app
