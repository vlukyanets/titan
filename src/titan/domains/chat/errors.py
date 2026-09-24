"""Errors raised by the chat domain. The API maps them to problem details."""


class ChatError(Exception):
    """Base class; the message is safe to show to the caller."""


class NotFoundError(ChatError):
    pass


class InvalidMessageError(ChatError):
    pass


class TurnInProgressError(ChatError):
    pass
