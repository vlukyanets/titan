"""RFC 9457 problem details for every error response."""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

from titan.domains.accounts import errors as accounts_errors

PROBLEM_JSON = "application/problem+json"


def problem(status: int, title: str, detail: str | None = None, **extra: object) -> JSONResponse:
    body: dict[str, object] = {"type": "about:blank", "title": title, "status": status}
    if detail:
        body["detail"] = detail
    body.update(extra)
    return JSONResponse(body, status_code=status, media_type=PROBLEM_JSON)


def install(app: FastAPI) -> None:
    @app.exception_handler(HTTPException)
    async def _http(_: Request, exc: HTTPException) -> JSONResponse:
        response = problem(exc.status_code, _title(exc.status_code), str(exc.detail))
        if exc.headers:
            response.headers.update(exc.headers)
        return response

    @app.exception_handler(accounts_errors.AccountsError)
    async def _accounts(_: Request, exc: accounts_errors.AccountsError) -> JSONResponse:
        status = _ACCOUNTS_STATUS.get(type(exc), 400)
        headers = {"WWW-Authenticate": "Bearer"} if status == 401 else None
        response = problem(status, _title(status), str(exc))
        if headers:
            response.headers.update(headers)
        return response

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, exc: RequestValidationError) -> JSONResponse:
        # Drop the submitted values: they can contain passwords or tokens.
        errors = [
            {k: v for k, v in error.items() if k not in ("input", "ctx", "url")}
            for error in exc.errors()
        ]
        return problem(422, "Unprocessable Content", errors=errors)


_ACCOUNTS_STATUS: dict[type[Exception], int] = {
    accounts_errors.InvalidCredentialsError: 401,
    accounts_errors.ForbiddenError: 403,
    accounts_errors.NotFoundError: 404,
    accounts_errors.UsernameTakenError: 409,
    accounts_errors.InvalidUsernameError: 422,
    accounts_errors.InvalidPasswordError: 422,
}


def _title(status: int) -> str:
    from http import HTTPStatus

    try:
        return HTTPStatus(status).phrase
    except ValueError:
        return "Error"
