"""Scheduled workflows: the daily plan and replanning missed time blocks.

`titan-worker` starts them from its sweeps. Every run of an agent workflow goes
through `Workflows.run`, the common runner, which skips the run while the
user's budget is exceeded and tells them so (docs/spec/domains/usage.md).

Runs are once per user and key: each ends in one notification whose id is
derived from the run, so a run that already ended is never started again on
this node, and copies from two nodes become one notification (ADR 0006).
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable, Mapping, MutableMapping
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import TypedDict
from zoneinfo import ZoneInfo

from claude_agent_sdk import ResultMessage, query
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.runtime import Runtime
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from titan.agent.node import QueryFn, Tier, agent_options, agent_run, stream_agent
from titan.agent.policy import policy_hooks
from titan.agent.runtime import prepare_claude
from titan.agent.tools import ToolScope, execute, mcp_servers
from titan.agent.usage import record_usage
from titan.domains.accounts.models import User
from titan.domains.autonomy.models import ActionClass, Decision
from titan.domains.autonomy.service import ApprovalsService, PolicyService
from titan.domains.calendar.models import Event, EventKind, PlanningPrefs
from titan.domains.calendar.service import zone
from titan.domains.calendar.tools import REPLAN_BLOCK
from titan.domains.notifications.models import Notification, NotificationKind
from titan.domains.notifications.service import NotificationsService
from titan.domains.tasks.models import Task, TaskStatus
from titan.domains.trackers.service import TrackersService
from titan.domains.usage.budget import BudgetService
from titan.notify import Pusher
from titan.settings import Settings

log = logging.getLogger(__name__)

NAMESPACE = uuid.UUID("3b0c8f5e-7a41-4c8e-9d36-5f2e1a7c4b90")
DAILY_PLAN = "daily_plan"
# Blocks that ended this long ago or less are replanned.
REPLAN_WINDOW = timedelta(hours=1)
_OPEN = (TaskStatus.TODO, TaskStatus.DOING)

DAILY_PLAN_TOOLS = frozenset(
    {
        "mcp__tasks__list_tasks",
        "mcp__tasks__get_task",
        "mcp__tasks__list_projects",
        "mcp__calendar__list_events",
        "mcp__calendar__free_busy",
        "mcp__calendar__plan_day",
        "mcp__reminders__list_reminders",
        "mcp__memory__recall",
    }
)

DAILY_PLAN_PROMPT = """\
You are TITAN, planning one household member's working day. Nobody is reading \
along; your final reply becomes a short push notification to them.

1. Look at their open tasks (deadlines, priorities, estimates) and the day's \
calendar. Recall what you remember about how they like to work if it helps.
2. Call plan_day once for the day. Give task_ids in the order you want them \
placed when deadlines or priorities call for it; otherwise let it choose. \
TITAN places the blocks itself and keeps working hours and buffers.
3. Reply with the plan in at most 600 characters: what is on today, in order, \
and anything urgent that did not fit. No greeting. Write in the language their \
tasks are written in.

If there is nothing to plan, say so in one sentence.
"""


def run_id(workflow: str, user_id: uuid.UUID, key: str) -> uuid.UUID:
    """The id of one run, which is also the id of the notification it ends with."""
    return uuid.uuid5(NAMESPACE, f"{workflow}:{user_id}:{key}")


# -------------------------------------------------------------- daily plan


class DailyPlanState(TypedDict):
    user_id: str
    day: str
    run_id: str
    summary: str


@dataclass(frozen=True)
class WorkflowContext:
    """Per-run dependencies. Passed as LangGraph context, never checkpointed."""

    settings: Settings
    sessions: async_sessionmaker[AsyncSession]
    query_fn: QueryFn = query
    environ: Mapping[str, str] | None = None
    pusher: Pusher | None = None


async def _plan(state: DailyPlanState, runtime: Runtime[WorkflowContext]) -> dict[str, str]:
    context = runtime.context
    settings = context.settings
    user_id = uuid.UUID(state["user_id"])
    async with context.sessions() as session:
        exposure = await TrackersService(session).exposure(user_id)
    scope = ToolScope(
        context.sessions,
        user_id,
        None,
        context.pusher,
        settings.push_allowed_origins,
        # By default health and finance show only sums here (ADR 0007).
        aggregates_only=exposure.aggregates_only("workflows"),
    )
    options = agent_options(
        settings,
        tier=Tier.FAST,
        system_prompt=DAILY_PLAN_PROMPT,
        mcp_servers=mcp_servers(scope, only=DAILY_PLAN_TOOLS),
        hooks=policy_hooks(scope, approval_ttl=timedelta(hours=settings.approval_ttl_hours)),
        max_turns=8,
    )
    async with context.sessions() as session:
        tz = await _zone(session, user_id)
    now = datetime.now(tz)
    prompt = f"Plan {state['day']}.\nCurrent time: {now.isoformat(timespec='minutes')} ({tz})"
    result: ResultMessage | None = None
    async for message in stream_agent(
        prompt, options, settings, query_fn=context.query_fn, environ=context.environ
    ):
        if isinstance(message, ResultMessage):
            result = message
    await record_usage(
        context.sessions,
        result,
        user_id=user_id,
        source=DAILY_PLAN,
        model=options.model or "",
        reference=uuid.UUID(state["run_id"]),
        pusher=context.pusher,
    )
    return {"summary": agent_run(result, options).text}


async def _notify(state: DailyPlanState, runtime: Runtime[WorkflowContext]) -> dict[str, str]:
    context = runtime.context
    async with context.sessions() as session:
        await NotificationsService(
            session, pusher=context.pusher, push_origins=context.settings.push_allowed_origins
        ).notify(
            uuid.UUID(state["user_id"]),
            NotificationKind.PLAN,
            "Your day",
            state["summary"],
            {"day": state["day"]},
            notification_id=uuid.UUID(state["run_id"]),
        )
    return {}


def build_daily_plan() -> CompiledStateGraph[
    DailyPlanState, WorkflowContext, DailyPlanState, DailyPlanState
]:
    graph = StateGraph(DailyPlanState, context_schema=WorkflowContext)
    graph.add_node("plan", _plan)
    graph.add_node("notify", _notify)
    graph.add_edge(START, "plan")
    graph.add_edge("plan", "notify")
    graph.add_edge("notify", END)
    # No checkpointer: a run that dies is started again on the next tick.
    return graph.compile(name=DAILY_PLAN)


async def _zone(session: AsyncSession, user_id: uuid.UUID) -> ZoneInfo:
    prefs = await session.get(PlanningPrefs, user_id)
    return zone(prefs.time_zone if prefs is not None else "UTC")


@dataclass(frozen=True)
class Due:
    user_id: uuid.UUID
    day: date

    @property
    def run_id(self) -> uuid.UUID:
        return run_id(DAILY_PLAN, self.user_id, self.day.isoformat())


async def _ended(session: AsyncSession, notification_id: uuid.UUID) -> bool:
    return await session.get(Notification, notification_id) is not None


# ------------------------------------------------------------------ runner


class Workflows:
    def __init__(
        self,
        settings: Settings,
        sessions: async_sessionmaker[AsyncSession],
        *,
        pusher: Pusher | None = None,
        query_fn: QueryFn = query,
        environ: MutableMapping[str, str] | None = None,
    ) -> None:
        self.settings = settings
        self.sessions = sessions
        self.pusher = pusher
        self.query_fn = query_fn
        self.environ = environ
        self.daily_plan_graph = build_daily_plan()
        self._ready: bool | None = None

    def _notifications(self, session: AsyncSession) -> NotificationsService:
        return NotificationsService(
            session, pusher=self.pusher, push_origins=self.settings.push_allowed_origins
        )

    async def ready(self) -> bool:
        """The credential check, once per process; agent workflows wait for it."""
        if self._ready is None:
            self._ready = await prepare_claude(
                self.settings,
                query_fn=self.query_fn,
                environ=self.environ,
                what="scheduled agent workflows",
            )
        return self._ready

    async def run(
        self,
        workflow: str,
        user_id: uuid.UUID,
        notification_id: uuid.UUID,
        start: Callable[[], Awaitable[None]],
    ) -> bool:
        """The common runner: start a run unless it ended or the budget is exceeded.

        Answers whether it started. A skipped run ends in a `budget` notification
        under the run's id; a failed one in a notification that says so.
        """
        async with self.sessions() as session:
            if await _ended(session, notification_id):
                return False
            status = await BudgetService(session).status(user_id)
            if status.exceeded:
                await self._notifications(session).notify(
                    user_id,
                    NotificationKind.BUDGET,
                    "Skipped: over the monthly budget",
                    f"The {workflow.replace('_', ' ')} did not run because this month's "
                    "Claude budget is used up. It runs again once the limit is raised "
                    "or the month ends.",
                    {"workflow": workflow},
                    notification_id=notification_id,
                )
                return False
        try:
            await start()
        except Exception as exc:
            # Logged by type only: messages can carry user data.
            log.error("%s for a user failed: %s", workflow, type(exc).__name__)
            async with self.sessions() as session:
                if not await _ended(session, notification_id):
                    await self._notifications(session).notify(
                        user_id,
                        NotificationKind.PLAN,
                        "Your day",
                        "TITAN could not plan today; it tries again tomorrow.",
                        {"workflow": workflow, "failed": True},
                        notification_id=notification_id,
                    )
        return True

    async def due_daily_plans(self, now: datetime) -> list[Due]:
        """Users whose plan time has come today, inside working hours, not yet run."""
        async with self.sessions() as session:
            rows = (
                await session.execute(
                    select(User.id, PlanningPrefs)
                    .outerjoin(PlanningPrefs, PlanningPrefs.user_id == User.id)
                    .where(User.disabled_at.is_(None))
                )
            ).all()
            due: list[Due] = []
            for user_id, prefs in rows:
                tz = zone(prefs.time_zone) if prefs else ZoneInfo("UTC")
                at = prefs.daily_plan_at if prefs else time(7)
                end = prefs.work_end if prefs else time(17)
                days = list(prefs.work_days) if prefs else [1, 2, 3, 4, 5]
                local = now.astimezone(tz)
                if at is None or local.isoweekday() not in days:
                    continue
                if not at <= local.time() < end:
                    continue
                item = Due(user_id, local.date())
                if await _ended(session, item.run_id):
                    continue
                # Nothing to plan, nothing to spend: checked again on later ticks.
                has_tasks = await session.scalar(
                    select(Task.id).where(Task.owner_id == user_id, Task.status.in_(_OPEN)).limit(1)
                )
                if has_tasks is not None:
                    due.append(item)
            return due

    async def daily_plan(self, item: Due) -> bool:
        async def start() -> None:
            state: DailyPlanState = {
                "user_id": str(item.user_id),
                "day": item.day.isoformat(),
                "run_id": str(item.run_id),
                "summary": "",
            }
            await self.daily_plan_graph.ainvoke(
                state,
                context=WorkflowContext(
                    self.settings, self.sessions, self.query_fn, self.environ, self.pusher
                ),
            )

        return await self.run(DAILY_PLAN, item.user_id, item.run_id, start)

    # ------------------------------------------------------------ replanning

    async def replan_missed(self, now: datetime) -> int:
        """Move or propose moving time blocks that ended with their task still open.

        No model is involved: the block goes to the next free slot, through the
        same tool and policy as the agent's `replan_block`. `auto` and
        `auto-undo` move it and tell the user; `confirm` asks them; `deny`
        leaves it.
        """
        async with self.sessions() as session:
            blocks = (
                await session.scalars(
                    select(Event)
                    .join(Task, Task.id == Event.task_id)
                    .where(
                        Event.kind == EventKind.TIME_BLOCK,
                        Event.recurrence.is_(None),
                        Event.ends_at <= now,
                        Event.ends_at > now - REPLAN_WINDOW,
                        Task.status.in_(_OPEN),
                    )
                )
            ).all()
        handled = 0
        for block in blocks:
            marker = run_id("replan", block.owner_id, f"{block.id}:{block.ends_at.isoformat()}")
            try:
                handled += await self._replan(block, marker)
            except Exception as exc:
                log.error("replanning a block failed: %s", type(exc).__name__)
        return handled

    async def _replan(self, block: Event, marker: uuid.UUID) -> bool:
        tool_input = {"event_id": str(block.id)}
        async with self.sessions() as session:
            if await _ended(session, marker):
                return False
            decision = await PolicyService(session).decide(
                block.owner_id, REPLAN_BLOCK.domain, ActionClass.WRITE_INTERNAL
            )
            if decision is Decision.DENY:
                return False
            if decision is Decision.CONFIRM:
                approval = await ApprovalsService(
                    session, ttl=timedelta(hours=self.settings.approval_ttl_hours)
                ).request(
                    block.owner_id,
                    REPLAN_BLOCK,
                    tool_input,
                    summary=f"Move the missed block “{block.title}” to the next free slot",
                )
                await self._notifications(session).notify(
                    block.owner_id,
                    NotificationKind.APPROVAL,
                    "Approval needed",
                    approval.summary,
                    {"approval_id": str(approval.id)},
                    notification_id=marker,
                )
                return True
        scope = ToolScope(
            self.sessions, block.owner_id, None, self.pusher, self.settings.push_allowed_origins
        )
        execution = await execute(REPLAN_BLOCK, scope, tool_input, decision=decision)
        async with self.sessions() as session:
            await self._notifications(session).notify(
                block.owner_id,
                NotificationKind.PLAN,
                "Missed time block",
                execution.result.text,
                {"event_id": str(block.id)},
                notification_id=marker,
            )
        return True
