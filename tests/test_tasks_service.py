"""Projects, tasks, sharing rules and recurring tasks."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from titan.domains.accounts.models import Role
from titan.domains.accounts.service import AccountsService
from titan.domains.tasks.errors import (
    AlreadyDoneError,
    ForbiddenError,
    InvalidRecurrenceError,
    InvalidTaskError,
    NotFoundError,
)
from titan.domains.tasks.models import ProjectStatus, TaskStatus
from titan.domains.tasks.recurrence import next_occurrence, normalize
from titan.domains.tasks.service import TaskQuery, TasksService

MONDAY_9 = datetime(2026, 9, 21, 9, 0, tzinfo=UTC)


def test_rules_are_normalized_and_bounded() -> None:
    assert normalize(" rrule:freq=weekly;byday=mo ") == "FREQ=WEEKLY;BYDAY=MO"
    for bad in (
        "",
        "FREQ=HOURLY",
        "FREQ=DAILY;COUNT=3",
        "FREQ=FORTNIGHTLY",
        "DTSTART:20260101T000000Z\nRRULE:FREQ=DAILY",
        "FREQ=DAILY;UNTIL=20270101",
        "FREQ=DAILY;" + "BYHOUR=9;" * 30,
    ):
        with pytest.raises(InvalidRecurrenceError):
            normalize(bad)


def test_the_next_occurrence_skips_missed_ones() -> None:
    weekly = "FREQ=WEEKLY;BYDAY=MO"
    # Completed early: the next Monday after the due date.
    assert next_occurrence(weekly, MONDAY_9, MONDAY_9 - timedelta(days=2)) == MONDAY_9 + timedelta(
        days=7
    )
    # Completed three weeks late: the next Monday after today, not a backlog.
    late = MONDAY_9 + timedelta(days=22)
    assert next_occurrence(weekly, MONDAY_9, late) == MONDAY_9 + timedelta(days=28)
    # 31st of every month skips the short ones.
    aug_31 = datetime(2026, 8, 31, 9, tzinfo=UTC)
    assert next_occurrence("FREQ=MONTHLY;BYMONTHDAY=31", aug_31, aug_31) == datetime(
        2026, 10, 31, 9, tzinfo=UTC
    )
    assert next_occurrence("FREQ=DAILY;UNTIL=20260901T000000Z", aug_31, aug_31) is None
    # A rule that never matches ends instead of searching forever.
    assert next_occurrence("FREQ=DAILY;BYMONTH=2;BYMONTHDAY=30", aug_31, aug_31) is None


@pytest.fixture
async def sessions(db_url: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(db_url)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def user(
    sessions: async_sessionmaker[AsyncSession], username: str, role: Role = Role.MEMBER
) -> uuid.UUID:
    async with sessions() as session:
        created = await AccountsService(session).create_user(
            username, "a-long-test-password", role=role
        )
        return created.id


@pytest.mark.db
async def test_tasks_are_created_listed_and_filtered(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    anna = await user(sessions, "anna", Role.OWNER)
    boris = await user(sessions, "boris")
    async with sessions() as session:
        tasks = TasksService(session)
        passport = await tasks.create_task(
            anna,
            "  Renew   the passport ",
            due_at=datetime(2026, 10, 15, 10, tzinfo=UTC),
            estimate_minutes=90,
            tags=["Docs", "docs", "errands"],
            priority=1,
        )
        assert passport.title == "Renew the passport"
        assert passport.tags == ["docs", "errands"]
        assert passport.status is TaskStatus.TODO
        milk = await tasks.create_task(anna, "Buy milk", notes="2 litres, 100% organic")
        await tasks.create_task(boris, "Boris's own task")

        assert [t.id for t in await tasks.tasks(anna)] == [milk.id, passport.id]
        assert [t.id for t in await tasks.tasks(anna, TaskQuery(tag="DOCS"))] == [passport.id]
        assert [t.id for t in await tasks.tasks(anna, TaskQuery(text="100%"))] == [milk.id]
        assert await tasks.tasks(anna, TaskQuery(text="_")) == []
        due_soon = TaskQuery(due_before=datetime(2026, 11, 1, tzinfo=UTC))
        assert [t.id for t in await tasks.tasks(anna, due_soon)] == [passport.id]
        assert [t.id for t in await tasks.tasks(anna, before=milk.id)] == [passport.id]
        assert [t.id for t in await tasks.tasks(anna, limit=1)] == [milk.id]
        with pytest.raises(NotFoundError):
            await tasks.get_task(boris, passport.id)

        for bad in (
            {"title": " "},
            {"title": "x", "priority": 5},
            {"title": "x", "estimate_minutes": 0},
            {"title": "x", "due_at": datetime(2026, 10, 1)},
            {"title": "x", "recurrence": "FREQ=DAILY"},
            {"title": "x", "tags": ["t" * 33]},
        ):
            with pytest.raises(InvalidTaskError):
                await tasks.create_task(anna, **bad)


@pytest.mark.db
async def test_updates_change_only_what_they_name(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    anna = await user(sessions, "anna", Role.OWNER)
    async with sessions() as session:
        tasks = TasksService(session)
        task = await tasks.create_task(
            anna, "Call the plumber", notes="Leak under the sink", estimate_minutes=15
        )
        updated = await tasks.update_task(
            anna, task.id, {"status": "doing", "estimate_minutes": None, "tags": ["home"]}
        )
        assert (updated.status, updated.estimate_minutes, updated.tags) == (
            TaskStatus.DOING,
            None,
            ["home"],
        )
        assert updated.notes == "Leak under the sink"
        with pytest.raises(InvalidTaskError, match="complete a task"):
            await tasks.update_task(anna, task.id, {"status": "done"})
        with pytest.raises(InvalidTaskError, match="no field"):
            await tasks.update_task(anna, task.id, {"owner_id": uuid.uuid4()})
        with pytest.raises(InvalidTaskError, match="needs due_at"):
            await tasks.update_task(anna, task.id, {"recurrence": "FREQ=DAILY"})

        done = await tasks.complete_task(anna, task.id)
        assert done.task.status is TaskStatus.DONE
        assert done.task.completed_at is not None
        assert done.next is None
        with pytest.raises(AlreadyDoneError):
            await tasks.complete_task(anna, task.id)
        reopened = await tasks.update_task(anna, task.id, {"status": "todo"})
        assert (reopened.status, reopened.completed_at) == (TaskStatus.TODO, None)

        await tasks.delete_task(anna, task.id)
        with pytest.raises(NotFoundError):
            await tasks.get_task(anna, task.id)


@pytest.mark.db
async def test_completing_a_recurring_task_rolls_it_over_once(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    anna = await user(sessions, "anna", Role.OWNER)
    async with sessions() as session:
        tasks = TasksService(session)
        bins = await tasks.create_task(
            anna,
            "Take out the bins",
            due_at=MONDAY_9,
            recurrence="freq=weekly;byday=mo",
            tags=["home"],
            priority=2,
        )
        done = await tasks.complete_task(anna, bins.id, now=MONDAY_9 + timedelta(hours=3))
        assert done.next is not None
        assert done.next.due_at == MONDAY_9 + timedelta(days=7)
        assert (done.next.title, done.next.tags, done.next.priority) == (
            "Take out the bins",
            ["home"],
            2,
        )
        assert done.next.recurrence == "FREQ=WEEKLY;BYDAY=MO"
        assert done.next.status is TaskStatus.TODO
        # The rule moved on, so reopening and completing again makes no second copy.
        assert done.task.recurrence is None
        await tasks.update_task(anna, bins.id, {"status": "todo"})
        again = await tasks.complete_task(anna, bins.id)
        assert again.next is None
        assert len(await tasks.tasks(anna, TaskQuery(statuses=[TaskStatus.TODO]))) == 1


@pytest.mark.db
async def test_shared_projects_follow_the_access_rules(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    anna = await user(sessions, "anna", Role.OWNER)
    boris = await user(sessions, "boris")
    carla = await user(sessions, "carla")
    async with sessions() as session:
        tasks = TasksService(session)
        home = await tasks.create_project(anna, "Home", shared_with=[boris, anna, boris])
        assert home.shared_with == [boris]
        private = await tasks.create_project(anna, "Surprise party")
        assert [p.project.id for p in await tasks.projects(boris)] == [home.project.id]
        assert await tasks.projects(carla) == []

        hers = await tasks.create_task(anna, "Fix the shelf", project_id=home.project.id)
        his = await tasks.create_task(boris, "Paint the fence", project_id=home.project.id)
        extra = await tasks.create_task(boris, "Buy brushes", project_id=home.project.id)
        seen_by_boris = await tasks.tasks(boris, TaskQuery(project_id=home.project.id))
        assert {t.id for t in seen_by_boris} == {hers.id, his.id, extra.id}
        assert await tasks.tasks(carla) == []
        with pytest.raises(NotFoundError):
            await tasks.create_task(boris, "Peek", project_id=private.project.id)
        with pytest.raises(NotFoundError):
            await tasks.get_project(carla, home.project.id)

        # Members edit and complete each other's tasks, but only owners delete.
        assert (await tasks.update_task(boris, hers.id, {"priority": 2})).priority == 2
        await tasks.complete_task(boris, hers.id)
        with pytest.raises(ForbiddenError):
            await tasks.delete_task(boris, hers.id)
        with pytest.raises(ForbiddenError):
            await tasks.update_project(boris, home.project.id, {"title": "Mine now"})
        with pytest.raises(ForbiddenError):
            await tasks.delete_project(boris, home.project.id)
        with pytest.raises(InvalidTaskError, match="does not exist"):
            await tasks.update_project(anna, home.project.id, {"shared_with": [uuid.uuid4()]})

        # Unsharing hides Anna's task from Boris, but not his own.
        await tasks.update_project(anna, home.project.id, {"shared_with": []})
        assert [t.id for t in await tasks.tasks(boris)] == [extra.id, his.id]

        archived = await tasks.update_project(anna, home.project.id, {"status": "archived"})
        assert archived.project.status is ProjectStatus.ARCHIVED
        assert await tasks.projects(anna, status=ProjectStatus.ACTIVE) == [
            await tasks.get_project(anna, private.project.id)
        ]
        with pytest.raises(InvalidTaskError, match="archived"):
            await tasks.create_task(anna, "Late addition", project_id=home.project.id)

        # The project owner may delete a member's task; deleting the project
        # sends the rest to their owners' inboxes.
        await tasks.update_project(anna, home.project.id, {"shared_with": [boris]})
        await tasks.delete_task(anna, extra.id)
        await tasks.delete_project(anna, home.project.id)
        moved = await tasks.get_task(boris, his.id)
        assert moved.project_id is None
        assert [t.id for t in await tasks.tasks(boris, TaskQuery(inbox=True))] == [his.id]
        assert [t.id for t in await tasks.tasks(anna, TaskQuery(inbox=True))] == [hers.id]
