"""Errors raised by the reminders domain. The API maps them to problem details."""


class RemindersError(Exception):
    """Base class; the message is safe to show to the caller."""


class NotFoundError(RemindersError):
    pass


class InvalidReminderError(RemindersError):
    pass


class ReminderClosedError(RemindersError):
    """The reminder was dismissed, or is not in a state that allows this."""
