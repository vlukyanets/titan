"""Errors raised by the accounts domain. The API maps them to problem details."""


class AccountsError(Exception):
    """Base class; the message is safe to show to the caller."""


class InvalidCredentialsError(AccountsError):
    """Wrong username or password, or the account is locked or disabled.

    Deliberately one error for all of these, so callers cannot probe usernames.
    """

    def __init__(self) -> None:
        super().__init__("invalid username or password, or too many failed attempts")


class InvalidUsernameError(AccountsError):
    pass


class InvalidPasswordError(AccountsError):
    pass


class UsernameTakenError(AccountsError):
    def __init__(self, username: str) -> None:
        super().__init__(f"username {username!r} is taken")


class NotFoundError(AccountsError):
    pass


class ForbiddenError(AccountsError):
    pass
