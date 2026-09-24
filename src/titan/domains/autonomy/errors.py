"""Errors raised by the autonomy domain. The API maps them to problem details."""


class AutonomyError(Exception):
    """Base class; the message is safe to show to the caller."""


class NotFoundError(AutonomyError):
    pass


class ForbiddenError(AutonomyError):
    pass


class InvalidRuleError(AutonomyError):
    pass


class ApprovalClosedError(AutonomyError):
    """The request was already decided or has expired."""


class NotUndoableError(AutonomyError):
    pass


class UndoConflictError(AutonomyError):
    """The entity changed after the logged call, so undoing it would lose that change."""
