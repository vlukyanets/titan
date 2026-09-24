"""The Agent SDK node wrapper, against a fake `query`."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    Message,
    ResultMessage,
    SystemMessage,
    TextBlock,
)

from titan.agent.auth import ClaudeAuthError
from titan.agent.node import (
    AgentRunError,
    Tier,
    TokenUsage,
    agent_options,
    run_agent,
    self_check,
    stream_agent,
)
from titan.settings import Settings

URL = "postgresql+psycopg://user:pw@db/titan"
API_KEY = "sk-ant-api-fake-key-for-tests"


def init(source: str = "ANTHROPIC_API_KEY") -> SystemMessage:
    return SystemMessage(subtype="init", data={"apiKeySource": source, "session_id": "s1"})


def result(**overrides: object) -> ResultMessage:
    fields: dict[str, object] = {
        "subtype": "success",
        "duration_ms": 10,
        "duration_api_ms": 8,
        "is_error": False,
        "num_turns": 2,
        "session_id": "s1",
        "result": "Two tasks are due today.",
        "total_cost_usd": 0.0123,
        "usage": {
            "input_tokens": 120,
            "output_tokens": 30,
            "cache_read_input_tokens": 900,
            "cache_creation_input_tokens": 50,
        },
    }
    fields.update(overrides)
    return ResultMessage(**fields)  # type: ignore[arg-type]


REPLY = AssistantMessage(content=[TextBlock(text="Two tasks are due today.")], model="m")


class FakeQuery:
    def __init__(self, *messages: Message) -> None:
        self.messages = messages
        self.calls: list[tuple[str, ClaudeAgentOptions]] = []
        self.yielded = 0
        self.closed = False

    def __call__(self, *, prompt: str, options: ClaudeAgentOptions) -> AsyncIterator[Message]:
        self.calls.append((prompt, options))
        return self._run()

    async def _run(self) -> AsyncIterator[Message]:
        try:
            for message in self.messages:
                self.yielded += 1
                yield message
        finally:
            self.closed = True


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        database_url=URL,
        claude_config_dir=tmp_path / "claude",
        claude_model_fast="fast-model",
        claude_model_strong="strong-model",
    )


@pytest.fixture
def environ(settings: Settings) -> dict[str, str]:
    """A process environment as auth.install leaves it in api-key mode."""
    return {
        "PATH": "/usr/bin",
        "ANTHROPIC_API_KEY": API_KEY,
        "CLAUDE_CONFIG_DIR": str(settings.claude_config_dir),
    }


def test_options_lock_the_session_down(settings: Settings) -> None:
    options = agent_options(
        settings,
        tier=Tier.STRONG,
        system_prompt="You plan the day.",
        allowed_tools=["mcp__tasks__list"],
        max_turns=4,
    )
    assert options.model == "strong-model"
    assert options.system_prompt == "You plan the day."
    assert options.tools == []
    assert options.allowed_tools == ["mcp__tasks__list"]
    assert options.setting_sources == []
    assert options.strict_mcp_config is True
    assert options.env == {"CLAUDE_CONFIG_DIR": str(settings.claude_config_dir)}
    assert Path(str(options.cwd)).is_dir()
    assert list(Path(str(options.cwd)).iterdir()) == []
    assert options.max_turns == 4
    fast = agent_options(settings, tier=Tier.FAST, system_prompt="Classify.")
    assert fast.model == "fast-model"


async def test_run_returns_text_and_usage(settings: Settings, environ: dict[str, str]) -> None:
    fake = FakeQuery(init(), REPLY, result())
    options = agent_options(settings, tier=Tier.STRONG, system_prompt="You plan the day.")
    run = await run_agent("What is due?", options, settings, query_fn=fake, environ=environ)
    assert run.text == "Two tasks are due today."
    assert run.model == "strong-model"
    assert run.session_id == "s1"
    assert run.num_turns == 2
    assert run.cost_usd == pytest.approx(0.0123)
    assert run.usage == TokenUsage(
        input_tokens=120, output_tokens=30, cache_read_tokens=900, cache_creation_tokens=50
    )
    assert fake.calls == [("What is due?", options)]
    assert fake.closed


async def test_wrong_credential_source_stops_before_any_reply(
    settings: Settings, environ: dict[str, str]
) -> None:
    fake = FakeQuery(init("apiKeyHelper"), REPLY, result())
    options = agent_options(settings, tier=Tier.FAST, system_prompt="x")
    seen: list[Message] = []

    async def consume() -> None:
        async for message in stream_agent("hi", options, settings, query_fn=fake, environ=environ):
            seen.append(message)

    with pytest.raises(ClaudeAuthError, match="apiKeyHelper"):
        await consume()
    assert seen == []
    assert fake.yielded == 1
    assert fake.closed


async def test_messages_before_init_are_refused(
    settings: Settings, environ: dict[str, str]
) -> None:
    fake = FakeQuery(REPLY, init(), result())
    options = agent_options(settings, tier=Tier.FAST, system_prompt="x")
    with pytest.raises(ClaudeAuthError, match="before its system/init"):
        await run_agent("hi", options, settings, query_fn=fake, environ=environ)


async def test_a_polluted_environment_never_starts_a_session(
    settings: Settings, environ: dict[str, str]
) -> None:
    fake = FakeQuery(init(), result())
    options = agent_options(settings, tier=Tier.FAST, system_prompt="x")
    polluted = environ | {"CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-fake"}
    with pytest.raises(ClaudeAuthError, match="CLAUDE_CODE_OAUTH_TOKEN"):
        await run_agent("hi", options, settings, query_fn=fake, environ=polluted)
    assert fake.calls == []


async def test_failed_sessions_raise_without_content(
    settings: Settings, environ: dict[str, str]
) -> None:
    fake = FakeQuery(
        init(), result(is_error=True, result="secret reply text", api_error_status=529)
    )
    options = agent_options(settings, tier=Tier.FAST, system_prompt="x")
    with pytest.raises(AgentRunError) as info:
        await run_agent("hi", options, settings, query_fn=fake, environ=environ)
    assert "HTTP 529" in str(info.value)
    assert "secret" not in str(info.value)


async def test_a_session_without_result_is_an_error(
    settings: Settings, environ: dict[str, str]
) -> None:
    options = agent_options(settings, tier=Tier.FAST, system_prompt="x")
    with pytest.raises(AgentRunError, match="without a result"):
        await run_agent("hi", options, settings, query_fn=FakeQuery(init()), environ=environ)


async def test_self_check_stops_at_init(settings: Settings, environ: dict[str, str]) -> None:
    fake = FakeQuery(init(), REPLY, result())
    assert await self_check(settings, query_fn=fake, environ=environ) == "ANTHROPIC_API_KEY"
    assert fake.yielded == 1
    assert fake.closed
    options = fake.calls[0][1]
    assert options.model == "fast-model"
    assert options.max_turns == 1


async def test_self_check_rejects_an_oauth_session_using_an_api_key(tmp_path: Path) -> None:
    settings = Settings(database_url=URL, claude_auth_mode="oauth", claude_config_dir=tmp_path)
    environ = {"CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-fake", "CLAUDE_CONFIG_DIR": str(tmp_path)}
    with pytest.raises(ClaudeAuthError, match="expected 'none'"):
        await self_check(settings, query_fn=FakeQuery(init("ANTHROPIC_API_KEY")), environ=environ)
