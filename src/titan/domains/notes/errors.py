"""Errors raised by the notes domain. The API maps them to problem details."""


class NotesError(Exception):
    """Base class; the message is safe to show to the caller."""


class NotFoundError(NotesError):
    """Missing, or not visible to the caller: the two are not told apart."""


class ForbiddenError(NotesError):
    """Visible to the caller, but only its owner may do this."""


class InvalidNoteError(NotesError):
    pass


class InvalidMemoryError(NotesError):
    pass
