"""Errors raised by the trackers domain. The API maps them to problem details."""


class TrackersError(Exception):
    """Base class; the message is safe to show to the caller."""


class NotFoundError(TrackersError):
    """Missing, or someone else's: the two are not told apart."""


class InvalidTrackerError(TrackersError):
    pass


class InvalidEntryError(TrackersError):
    pass


class DuplicateNameError(TrackersError):
    pass


class ArchivedError(TrackersError):
    pass
