"""The PreToolUse hook: the one place where the autonomy policy is enforced (ADR 0005).

`auto` and `auto-undo` allow the call. `deny` refuses it. `confirm` stores an
approval request with the exact input, notifies the user and refuses the call
with a reason that tells the model the user has been asked (ADR 0010). Unknown
tools and any error inside the hook refuse the call.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import timedelta
from typing import Any

from claude_agent_sdk import HookContext, HookInput, HookJSONOutput, HookMatcher

from titan.agent.tools import REGISTRY, ToolScope
from titan.domains.autonomy.models import Approval, Decision
from titan.domains.autonomy.service import ApprovalsService, PolicyService
from titan.domains.notifications.models import NotificationKind
from titan.domains.notifications.service import NotificationsService

log = logging.getLogger(__name__)


def _answer(decision: str, reason: str) -> HookJSONOutput:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": decision,  # type: ignore[typeddict-item]
            "permissionDecisionReason": reason,
        }
    }


def allow() -> HookJSONOutput:
    return _answer("allow", "allowed by the user's policy")


def deny(reason: str) -> HookJSONOutput:
    return _answer("deny", reason)


def policy_hook(
    scope: ToolScope,
    *,
    approval_ttl: timedelta,
    on_approval: Callable[[Approval], None] = lambda _: None,
) -> Callable[[HookInput, str | None, HookContext], Any]:
    async def hook(
        input_data: HookInput, tool_use_id: str | None, context: HookContext
    ) -> HookJSONOutput:
        if input_data.get("hook_event_name") != "PreToolUse":
            return {}
        name = str(input_data.get("tool_name", ""))
        spec = REGISTRY.get(name)
        if spec is None:
            return deny(f"{name} is not a TITAN tool.")
        raw_input = input_data.get("tool_input")
        tool_input: dict[str, Any] = dict(raw_input) if isinstance(raw_input, dict) else {}
        try:
            async with scope.sessions() as session:
                decision = await PolicyService(session).decide(
                    scope.user_id, spec.domain, spec.action_class
                )
                if decision in (Decision.AUTO, Decision.AUTO_UNDO):
                    return allow()
                if decision is Decision.DENY:
                    return deny(
                        f"The user's policy does not allow {spec.action_class.value} actions "
                        f"in {spec.domain}. Do not try again; tell the user."
                    )
                approval = await ApprovalsService(session, ttl=approval_ttl).request(
                    scope.user_id, spec, tool_input, thread_id=scope.thread_id
                )
                await NotificationsService(
                    session, pusher=scope.pusher, push_origins=scope.push_origins
                ).notify(
                    scope.user_id,
                    NotificationKind.APPROVAL,
                    "Approval needed",
                    approval.summary,
                    {"approval_id": str(approval.id)},
                )
        except Exception as exc:
            log.error("policy hook failed for %s: %s", name, type(exc).__name__)
            return deny("TITAN could not check its policy for this action; it was not run.")
        on_approval(approval)
        return deny(
            f"This action needs the user's approval, and they have been asked: "
            f"“{approval.summary}”. It runs by itself once they approve. Tell the user, "
            f"and do not call the tool again for it."
        )

    return hook


def policy_hooks(
    scope: ToolScope,
    *,
    approval_ttl: timedelta,
    on_approval: Callable[[Approval], None] = lambda _: None,
) -> dict[Any, list[HookMatcher]]:
    """Hooks for `agent_options`: every tool call goes through the policy."""
    hook = policy_hook(scope, approval_ttl=approval_ttl, on_approval=on_approval)
    return {"PreToolUse": [HookMatcher(matcher=None, hooks=[hook])]}
