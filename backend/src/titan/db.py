"""The data module: the only code that runs SQL."""

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine


def make_engine(url: str) -> AsyncEngine:
    return create_async_engine(url)


async def ping(engine: AsyncEngine) -> bool:
    """Whether the database answers a query."""
    try:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
    except SQLAlchemyError, OSError:
        return False
    return True
