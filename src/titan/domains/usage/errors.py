"""Errors raised by the usage domain. The API maps them to problem details."""


class UsageError(Exception):
    """Base class; the message is safe to show to the caller."""


class InvalidMonthError(UsageError):
    pass


class ForbiddenError(UsageError):
    pass
