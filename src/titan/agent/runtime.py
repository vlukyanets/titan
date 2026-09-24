"""Running chat turns in the process that serves the API.

Until `titan-worker` exists, the API process runs turns itself. Each turn runs
in a task of its own, so a client that disconnects does not stop it; the reply
is stored either way. Events reach the client through a queue that the task
keeps filling until the client goes away.
"""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from collections.abc import AsyncIterator, MutableMapping
from dataclasses import dataclass

from claude_agent_sdk import ClaudeSDKError, query
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from titan.agent import auth
from titan.agent.auth import ClaudeAuthError
from titan.agent.chat import (
    ChatContext,
    ChatTurnState,
    TextDelta,
    ToolActivity,
    TurnEvent,
    build_graph,
    thread_key,
)
from titan.agent.node import AgentRunError, QueryFn, self_check
from titan.domains.chat.models import ChatMessage
from titan.domains.chat.service import ChatService
from titan.settings import Settings

log = logging.getLogger(__name__)

# LangSmith tracing would send conversations to a third party. Nothing in TITAN
# enables it, and these variables are removed so nothing can.
_TRACING_PREFIXES = ("LANGSMITH_", "LANGCHAIN_")


def disable_tracing(environ: MutableMapping[str, str]) -> None:
    for key in [key for key in environ if key.startswith(_TRACING_PREFIXES)]:
        del environ[key]


def conninfo(database_url: str) -> str:
    """A libpq connection string from TITAN's SQLAlchemy URL."""
    url = make_url(database_url).set(drivername="postgresql")
    return url.render_as_string(hide_password=False)


def failure_reason(exc: BaseException, timeout: float) -> str:
    """What went wrong, safe to store and show: never message content."""
    if isinstance(exc, AgentRunError | ClaudeAuthError):
        return str(exc)
    if isinstance(exc, TimeoutError):
        return f"the reply took longer than {timeout:g} seconds"
    if isinstance(exc, asyncio.CancelledError):
        return "the server stopped during the reply"
    if isinstance(exc, ClaudeSDKError):
        return f"Claude Code failed ({type(exc).__name__})"
    return "internal error"


@dataclass(frozen=True)
class TurnEnded:
    """The last event of a turn: the stored assistant message, complete or failed.

    None when the message is gone, for example because its thread was deleted.
    """

    message: ChatMessage | None


class TurnStream:
    """The events of one running turn, for one listener."""

    def __init__(self) -> None:
        self._queue: asyncio.Queue[TurnEvent | TurnEnded] = asyncio.Queue()
        self._attached = True

    def put(self, event: TurnEvent | TurnEnded) -> None:
        if self._attached:
            self._queue.put_nowait(event)

    def detach(self) -> None:
        """The listener left; the turn goes on without queueing its events."""
        self._attached = False
        while not self._queue.empty():
            self._queue.get_nowait()

    async def __aiter__(self) -> AsyncIterator[TurnEvent | TurnEnded]:
        try:
            while True:
                event = await self._queue.get()
                yield event
                if isinstance(event, TurnEnded):
                    return
        finally:
            self.detach()


class ChatRuntime:
    def __init__(
        self,
        settings: Settings,
        sessions: async_sessionmaker[AsyncSession],
        *,
        query_fn: QueryFn = query,
        environ: MutableMapping[str, str] | None = None,
        checkpointer: BaseCheckpointSaver[str] | None = None,
    ) -> None:
        self.settings = settings
        self.sessions = sessions
        self.query_fn = query_fn
        # The agent process's environment, cleaned by `ready()`. Tests pass a dict.
        self.environ = os.environ if environ is None else environ
        self._checkpointer = checkpointer
        self._pool: AsyncConnectionPool | None = None
        self._ready: bool | None = None
        self._lock = asyncio.Lock()
        self._tasks: set[asyncio.Task[None]] = set()

    async def ready(self) -> bool:
        """Clean the environment and run the credential self-check, once per process.

        A failed check leaves chat unavailable until the process restarts with a
        working credential; the rest of the API keeps running.
        """
        async with self._lock:
            if self._ready is None:
                self._ready = await self._check()
            return self._ready

    async def _check(self) -> bool:
        try:
            mode = auth.install(self.settings, self.environ)
            disable_tracing(self.environ)
            source = await self_check(self.settings, query_fn=self.query_fn, environ=self.environ)
        except (ClaudeAuthError, ClaudeSDKError, OSError) as exc:
            log.error("chat is unavailable: %s", exc)
            return False
        log.info("Claude credential check passed: %s mode, source %s", mode, source)
        return True

    def start(self, user_id: uuid.UUID, assistant_message_id: uuid.UUID) -> TurnStream:
        """Run the turn in the background and return its event stream."""
        stream = TurnStream()
        task = asyncio.create_task(
            self._run(user_id, assistant_message_id, stream),
            name=f"chat-turn-{assistant_message_id}",
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return stream

    async def wait(self) -> None:
        """Wait for every running turn to end (tests, graceful shutdown)."""
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)

    async def aclose(self) -> None:
        """Stop running turns, marking them failed, and close the database pool."""
        for task in list(self._tasks):
            task.cancel()
        await self.wait()
        if self._pool is not None:
            await self._pool.close()

    async def _checkpointer_ready(self) -> BaseCheckpointSaver[str]:
        if self._checkpointer is None:
            pool = AsyncConnectionPool(
                conninfo(self.settings.database_url.get_secret_value()),
                min_size=1,
                max_size=4,
                open=False,
                # What AsyncPostgresSaver requires of its connections.
                kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},
            )
            await pool.open()
            self._pool = pool
            self._checkpointer = AsyncPostgresSaver(pool)  # type: ignore[arg-type]
        return self._checkpointer

    async def _run(
        self, user_id: uuid.UUID, assistant_message_id: uuid.UUID, stream: TurnStream
    ) -> None:
        timeout = self.settings.chat_turn_timeout_seconds
        thread = thread_key(assistant_message_id)
        checkpointer: BaseCheckpointSaver[str] | None = None
        try:
            async with asyncio.timeout(timeout):
                checkpointer = await self._checkpointer_ready()
                graph = build_graph(checkpointer)
                context = ChatContext(
                    self.settings, self.sessions, query_fn=self.query_fn, environ=self.environ
                )
                state: ChatTurnState = {
                    "user_id": str(user_id),
                    "assistant_message_id": str(assistant_message_id),
                }
                async for event in graph.astream(
                    state,
                    {"configurable": {"thread_id": thread}},
                    context=context,
                    stream_mode="custom",
                    # Checkpoints are written before the next step starts, as a
                    # resume after an approval will need.
                    durability="sync",
                ):
                    if isinstance(event, TextDelta | ToolActivity):
                        stream.put(event)
        except (Exception, asyncio.CancelledError) as exc:
            # Only the exception type is logged: messages of database errors can
            # carry statement parameters, which here would be message content.
            log.error("chat turn %s failed: %s", assistant_message_id, type(exc).__name__)
            async with self.sessions() as session:
                await ChatService(session).fail_turn(
                    assistant_message_id, failure_reason(exc, timeout)
                )
            if isinstance(exc, asyncio.CancelledError):
                raise
        finally:
            await self._finish(assistant_message_id, thread, checkpointer, stream)

    async def _finish(
        self,
        assistant_message_id: uuid.UUID,
        thread: str,
        checkpointer: BaseCheckpointSaver[str] | None,
        stream: TurnStream,
    ) -> None:
        if checkpointer is not None:
            try:
                await checkpointer.adelete_thread(thread)
            except Exception as exc:
                log.warning("could not delete checkpoints of %s: %s", thread, type(exc).__name__)
        message: ChatMessage | None = None
        try:
            async with self.sessions() as session:
                message = await ChatService(session).get_message(assistant_message_id)
        except Exception as exc:
            log.error("could not load chat turn %s: %s", assistant_message_id, type(exc).__name__)
        finally:
            # The listener always gets an end, or it would wait forever.
            stream.put(TurnEnded(message))
