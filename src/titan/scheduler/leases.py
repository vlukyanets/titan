"""Taking and renewing sweep leases (ADR 0006).

The lease is advisory: replication is asynchronous, so during a partition two
nodes can both believe they hold it. The preferred node and the grace period
keep that to real partitions, and sweeps make their effects idempotent.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from titan.scheduler.models import SchedulerLease


@dataclass(frozen=True)
class LeasePolicy:
    node: str
    preferred_node: str
    ttl: timedelta
    grace: timedelta

    @property
    def preferred(self) -> bool:
        return self.node == self.preferred_node


async def hold(session: AsyncSession, name: str, policy: LeasePolicy, now: datetime) -> bool:
    """Take or renew the lease for this node if the rules allow; commits.

    True while this node holds the lease until `now + ttl`.
    """
    # A new sweep's row starts expired, so whoever may take it does.
    await session.execute(
        insert(SchedulerLease)
        .values(name=name, holder=policy.node, expires_at=now - policy.ttl - policy.grace)
        .on_conflict_do_nothing(index_elements=["name"])
    )
    lease = await session.scalar(
        select(SchedulerLease).where(SchedulerLease.name == name).with_for_update()
    )
    assert lease is not None
    if policy.preferred:
        lease.preferred_seen_at = now
    preferred_active = (
        not policy.preferred
        and lease.preferred_seen_at is not None
        and now - lease.preferred_seen_at < policy.ttl
    )
    held = lease.holder == policy.node and lease.expires_at > now
    if held and preferred_active:
        # Hand it back; the preferred node takes it once it has expired.
        lease.expires_at = now
        held = False
    elif held:
        lease.expires_at = now + policy.ttl
    elif not preferred_active:
        free_at = lease.expires_at if policy.preferred else lease.expires_at + policy.grace
        if now >= free_at:
            lease.holder = policy.node
            lease.expires_at = now + policy.ttl
            held = True
    await session.commit()
    return held
