"""Recording what an agent session used (docs/spec/domains/usage.md)."""

from __future__ import annotations

import uuid

from claude_agent_sdk import ResultMessage
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from titan.agent.node import TokenUsage
from titan.domains.usage.service import UsageService


async def record_usage(
    sessions: async_sessionmaker[AsyncSession],
    result: ResultMessage | None,
    *,
    user_id: uuid.UUID,
    source: str,
    model: str,
    reference: uuid.UUID | None = None,
) -> None:
    """Record a session's usage from its result message, failed sessions included.

    A session that ended without a result message reported nothing to record.
    """
    if result is None or (result.usage is None and result.total_cost_usd is None):
        return
    usage = TokenUsage.of(result.usage)
    async with sessions() as session:
        await UsageService(session).record(
            user_id,
            source,
            model,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cache_read_tokens=usage.cache_read_tokens,
            cache_creation_tokens=usage.cache_creation_tokens,
            cost_usd=result.total_cost_usd,
            reference=reference,
        )
