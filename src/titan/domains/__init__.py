"""Domain logic: tasks, calendar, notes, memory, trackers, reminders.

Never imports from titan.api or titan.agent (enforced by import-linter). Importing
this package registers every domain model on the shared metadata, which Alembic
autogenerate relies on.
"""

from titan.domains import accounts, autonomy, chat, notifications

__all__ = ["accounts", "autonomy", "chat", "notifications"]
