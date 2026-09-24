"""Errors raised by the notifications domain. The API maps them to problem details."""


class NotificationsError(Exception):
    """Base class; the message is safe to show to the caller."""


class NotFoundError(NotificationsError):
    pass


class InvalidPushEndpointError(NotificationsError):
    pass
