"""A fake OpenAI-compatible model for the tests; no test calls a real one."""

import json
from collections.abc import Callable

import httpx2
from fastapi.testclient import TestClient

KEY = "sk-test-key-1234567890"
URL = "https://models.example/v1"

# One tool call: the tool's name and its arguments.
ToolCall = tuple[str, object]


class FakeModel:
    """Answers as an OpenAI-compatible model does and keeps what it got."""

    def __init__(self) -> None:
        self.requests: list[httpx2.Request] = []
        self.chunks = ["Hel", "lo"]
        self.done = True
        # Answered instead of a reply; an exception is raised.
        self.response: httpx2.Response | Exception | None = None
        # What to answer, one entry per request, before the default reply:
        # a text, or tool calls.
        self.script: list[str | list[ToolCall]] = []
        # Called with each request before it is answered.
        self.on_request: Callable[[httpx2.Request], None] | None = None

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        if self.on_request is not None:
            self.on_request(request)
        if isinstance(self.response, Exception):
            raise self.response
        if self.response is not None:
            return self.response
        answer = self.script.pop(0) if self.script else self.chunks
        events: list[object] = [{"choices": [{"delta": {"role": "assistant"}}]}]
        if isinstance(answer, str):
            events.append({"choices": [{"delta": {"content": answer}}]})
        elif answer and isinstance(answer[0], tuple):
            for index, (name, args) in enumerate(answer):
                # The arguments come in two pieces, as a real stream splits them.
                arguments = json.dumps(args)
                half = len(arguments) // 2
                events += [
                    _piece(index, f"call-{index}", name, arguments[:half]),
                    _piece(index, None, None, arguments[half:]),
                ]
        else:
            events += [{"choices": [{"delta": {"content": c}}]} for c in answer]
        body = "".join(f"data: {json.dumps(e)}\n\n" for e in events)
        if self.done:
            body += "data: [DONE]\n\n"
        return httpx2.Response(
            200, headers={"Content-Type": "text/event-stream"}, text=body
        )

    def body(self, request: int = -1) -> dict[str, object]:
        result: dict[str, object] = json.loads(self.requests[request].content)
        return result

    def messages(self, request: int = -1) -> list[dict[str, object]]:
        """The messages the model got in a request, without the system prompt."""
        messages = self.body(request)["messages"]
        assert isinstance(messages, list)
        return [m for m in messages if m["role"] != "system"]

    def results(self, request: int = -1) -> list[object]:
        """The tool results the model got in a request, read out of their
        data blocks."""
        return [
            read_data(m["content"])
            for m in self.messages(request)
            if m["role"] == "tool"
        ]


def read_data(content: object) -> object:
    assert isinstance(content, str)
    assert content.startswith("<data>\n") and content.endswith("\n</data>")
    return json.loads(content.removeprefix("<data>\n").removesuffix("\n</data>"))


def _piece(
    index: int, id: str | None, name: str | None, arguments: str
) -> dict[str, object]:
    """A piece of a tool call in a streamed reply."""
    call: dict[str, object] = {"index": index, "function": {"arguments": arguments}}
    if id is not None:
        call.update(
            id=id, type="function", function={"name": name, "arguments": arguments}
        )
    return {"choices": [{"delta": {"tool_calls": [call]}}]}


def plan(*steps: dict[str, object]) -> list[ToolCall]:
    """A model answer that sends a plan of these steps."""
    return [("run_plan", {"steps": list(steps)})]


def chat(client: TestClient, user: dict[str, str], message: str) -> list[str]:
    """Sends the message and returns the reply as the chunks it came in."""
    with client.stream(
        "POST", "/chat", json={"message": message}, headers=user
    ) as response:
        assert response.status_code == 200
        assert response.headers["content-type"] == "text/plain; charset=utf-8"
        return list(response.iter_text())
