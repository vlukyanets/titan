"""Errors raised by the tasks domain. The API maps them to problem details."""


class TasksError(Exception):
    """Base class; the message is safe to show to the caller."""


class NotFoundError(TasksError):
    """Missing, or not visible to the caller: the two are not told apart."""


class ForbiddenError(TasksError):
    """Visible to the caller, but only its owner may do this."""


class InvalidTaskError(TasksError):
    pass


class InvalidRecurrenceError(InvalidTaskError):
    pass


class AlreadyDoneError(TasksError):
    pass
