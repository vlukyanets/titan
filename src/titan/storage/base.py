"""Declarative base shared by every domain model."""

import enum

from sqlalchemy import Enum, MetaData
from sqlalchemy.orm import DeclarativeBase

# Deterministic constraint names, so Alembic can drop and rename them on every engine.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def str_enum(cls: type[enum.StrEnum], name: str) -> Enum:
    """A column type for a StrEnum, stored as a string with a CHECK constraint.

    Native Postgres enums need ALTER TYPE to grow, which is awkward to roll out
    across a Spock mesh (ADR 0006).
    """
    return Enum(
        cls,
        name=name,
        native_enum=False,
        create_constraint=True,
        length=16,
        values_callable=lambda members: [m.value for m in members],
    )
