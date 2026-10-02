"""The tasks domain's agent tools: audit, undo, and the shared-item rule."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from tests.fake_claude import FakeClaude
from tests.test_agent_tools import World
from tests.test_agent_tools import world as world  # the fixture
from titan.agent.tools import REGISTRY, execute, run_approved
from titan.domains.autonomy.errors import UndoConflictError
from titan.domains.autonomy.models import ActionClass, Approval, AuditEntry, Decision
from titan.domains.autonomy.service import ApprovalsService, AuditService
from titan.domains.calendar.models import PlanningPrefs
from titan.domains.tasks.models import Task, TaskStatus
from titan.domains.tasks.service import TasksService

pytestmark = pytest.mark.db

CREATE = "mcp__tasks__create_task"
UPDATE = "mcp__tasks__update_task"
COMPLETE = "mcp__tasks__complete_task"
DELETE = "mcp__tasks__delete_task"
LIST = "mcp__tasks__list_tasks"
CREATE_PROJECT = "mcp__tasks__create_project"
UPDATE_PROJECT = "mcp__tasks__update_project"


async def undo(world: World, entry_id: uuid.UUID) -> None:
    async with world.sessions() as session:
        await AuditService(session).undo(
            world.anna, entry_id, REGISTRY, world.scope().context(session)
        )


async def task(world: World, task_id: uuid.UUID) -> Task | None:
    async with world.sessions() as session:
        return await session.get(Task, task_id)


async def shared_task(world: World) -> Task:
    """A task of Anna's in a project she shares with Boris."""
    async with world.sessions() as session:
        service = TasksService(session)
        project = await service.create_project(
            world.anna.user_id, "Kitchen", shared_with=[world.boris.user_id]
        )
        return await service.create_task(
            world.anna.user_id, "Buy tiles", project_id=project.project.id
        )


async def test_create_runs_in_the_users_zone_and_undo_removes_it(world: World) -> None:
    async with world.sessions() as session:
        session.add(PlanningPrefs(user_id=world.anna.user_id, time_zone="Europe/Kyiv"))
        await session.commit()
    fake = FakeClaude(
        [
            (CREATE, {"title": "Renew the passport", "due_at": "2026-11-02T09:00"}),
            (LIST, {}),
        ]
    )
    await world.turn(fake)
    created, listed = fake.outcomes
    assert (created.decision, created.is_error) == ("allow", False)
    # 09:00 in Kyiv is 07:00 UTC in November, and is shown back as 09:00.
    assert "due 2026-11-02 09:00" in (listed.result or "")
    (entry,) = await world.rows(AuditEntry)
    assert (entry.tool, entry.decision, entry.undoable) == (CREATE, Decision.AUTO_UNDO, True)
    assert entry.after is not None
    assert entry.after["due_at"] == "2026-11-02T07:00:00+00:00"
    assert entry.entity_id is not None
    await undo(world, entry.id)
    assert await task(world, uuid.UUID(entry.entity_id)) is None


async def test_update_undo_restores_unless_changed_since(world: World) -> None:
    async with world.sessions() as session:
        created = await TasksService(session).create_task(world.anna.user_id, "Call mum")
    fake = FakeClaude([(UPDATE, {"task_id": str(created.id), "title": "Call dad"})])
    await world.turn(fake)
    (entry,) = await world.rows(AuditEntry)
    assert entry.summary == "Change the task “Call mum”: title Call dad"
    await undo(world, entry.id)
    restored = await task(world, created.id)
    assert restored is not None
    assert restored.title == "Call mum"

    fake = FakeClaude([(UPDATE, {"task_id": str(created.id), "priority": 1})])
    await world.turn(fake)
    second = (await world.rows(AuditEntry, AuditEntry.undone_at.is_(None)))[0]
    async with world.sessions() as session:
        await TasksService(session).update_task(world.anna.user_id, created.id, {"priority": 2})
    with pytest.raises(UndoConflictError):
        await undo(world, second.id)


async def test_completing_a_recurring_task_and_undoing_it(world: World) -> None:
    due = datetime.now(UTC) + timedelta(days=1)
    async with world.sessions() as session:
        created = await TasksService(session).create_task(
            world.anna.user_id, "Water plants", due_at=due, recurrence="FREQ=WEEKLY"
        )
    fake = FakeClaude([(COMPLETE, {"task_id": str(created.id)})])
    await world.turn(fake)
    (outcome,) = fake.outcomes
    assert "The next one is due" in (outcome.result or "")
    (entry,) = await world.rows(AuditEntry)
    assert entry.after is not None
    next_id = uuid.UUID(entry.after["next_task_id"])
    assert await task(world, next_id) is not None

    await undo(world, entry.id)
    assert await task(world, next_id) is None
    reopened = await task(world, created.id)
    assert reopened is not None
    assert (reopened.status, reopened.recurrence, reopened.completed_at) == (
        TaskStatus.TODO,
        "FREQ=WEEKLY",
        None,
    )


async def test_writes_to_shared_items_ask_first(world: World) -> None:
    tiles = await shared_task(world)
    fake = FakeClaude([(COMPLETE, {"task_id": str(tiles.id)})])
    await world.turn(fake)
    (outcome,) = fake.outcomes
    assert outcome.decision == "deny"
    (approval,) = await world.rows(Approval)
    assert approval.action_class is ActionClass.EXTERNAL
    assert approval.summary == "Complete the task “Buy tiles”"
    current = await task(world, tiles.id)
    assert current is not None
    assert current.status is TaskStatus.TODO

    async with world.sessions() as session:
        claimed = await ApprovalsService(session).claim(world.anna, approval.id)
    await run_approved(claimed, world.scope())
    (entry,) = await world.rows(AuditEntry)
    assert (entry.action_class, entry.decision) == (ActionClass.EXTERNAL, Decision.CONFIRM)


async def test_a_call_is_refused_if_its_item_became_shared(world: World) -> None:
    # The hook saw a private task; by the time the call runs it is shared.
    tiles = await shared_task(world)
    execution = await execute(
        REGISTRY[UPDATE], world.scope(), {"task_id": str(tiles.id), "priority": 1}
    )
    assert execution.result.is_error
    assert "approval" in execution.result.text
    current = await task(world, tiles.id)
    assert current is not None
    assert current.priority == 4


async def test_sharing_a_project_asks_and_deleting_needs_approval(world: World) -> None:
    async with world.sessions() as session:
        created = await TasksService(session).create_task(world.anna.user_id, "Old")
    fake = FakeClaude(
        [
            (CREATE_PROJECT, {"title": "Garden", "shared_with": ["boris"]}),
            (CREATE_PROJECT, {"title": "Reading"}),
            (DELETE, {"task_id": str(created.id)}),
        ]
    )
    await world.turn(fake)
    shared, private, deleted = fake.outcomes
    assert shared.decision == "deny"
    assert private.decision == "allow"
    assert deleted.decision == "deny"
    approvals = await world.rows(Approval)
    assert {(a.action_class, a.summary) for a in approvals} == {
        (ActionClass.EXTERNAL, "Create the project “Garden”, shared with boris"),
        (ActionClass.DESTRUCTIVE, "Delete the task “Old”"),
    }


async def test_project_update_undo_restores_sharing(world: World) -> None:
    async with world.sessions() as session:
        project = await TasksService(session).create_project(world.anna.user_id, "Reading")
    project_id = str(project.project.id)
    fake = FakeClaude([(UPDATE_PROJECT, {"project_id": project_id, "status": "archived"})])
    await world.turn(fake)
    (entry,) = await world.rows(AuditEntry)
    assert entry.summary == "Change the project “Reading”: status archived"
    await undo(world, entry.id)
    async with world.sessions() as session:
        restored = await TasksService(session).get_project(world.anna.user_id, project.project.id)
    assert restored.project.status.value == "active"


async def test_other_users_items_stay_invisible(world: World) -> None:
    async with world.sessions() as session:
        private = await TasksService(session).create_task(world.boris.user_id, "Boris's secret")
    fake = FakeClaude([(UPDATE, {"task_id": str(private.id), "title": "Mine now"}), (LIST, {})])
    await world.turn(fake)
    updated, listed = fake.outcomes
    assert updated.is_error
    assert "not found" in (updated.result or "")
    assert "secret" not in (listed.result or "")
