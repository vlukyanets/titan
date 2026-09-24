"""Errors raised by the calendar domain. The API maps them to problem details."""


class CalendarError(Exception):
    """Base class; the message is safe to show to the caller."""


class NotFoundError(CalendarError):
    """Missing, or not visible to the caller: the two are not told apart."""


class ForbiddenError(CalendarError):
    """Visible to the caller as an attendee, but only its owner may do this."""


class InvalidEventError(CalendarError):
    pass
