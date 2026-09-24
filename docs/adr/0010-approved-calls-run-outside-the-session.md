# 0010. Approved tool calls run outside the model session

- Status: Accepted
- Date: 2026-09-24
- Amends: [ADR 0002](0002-langgraph-with-agent-sdk-nodes.md) (the approval
  interrupt and the `PostToolUse` audit hook)

## Context

[ADR 0005](0005-per-domain-autonomy-policy.md) makes some tool calls wait for
the user's approval, and the answer may take up to 24 hours. ADR 0002 planned
to turn an approval request into a LangGraph `interrupt` and to resume the
graph, and with it the agent's reasoning, on the user's answer. It also planned
a `PostToolUse` hook to write the audit log.

Writing the policy hook showed three problems with that plan:

- A tool call happens inside a Claude Code session. Holding the session open
  for hours is not possible, and continuing it later means resuming its
  transcript. [ADR 0009](0009-chat-history-in-titan-tables.md) keeps
  transcripts local and never resumes them, so another node could not continue
  the call. Claude Code's own `defer` decision has the same need: the run stops
  and has to be resumed from its session.
- If the model continued after the approval, it could run the call with
  different input than the user approved.
- A `PostToolUse` hook sees what the tool returned to the model. Before and
  after state for undo would have to go through the model's context, where it
  costs tokens and does not belong.

## Options

1. **Interrupt and resume the session** with `SessionStore`-backed
   transcripts. Keeps the model in the loop after approval, but reverses ADR
   0009 and lets the model change the call after the user said yes.
2. **Refuse the call, record the request, run it without the model on
   approval.** The request stores the exact tool input. On approval TITAN runs
   the tool itself and posts the result to the thread.

## Decision

Option 2.

- On `confirm` the `PreToolUse` hook stores an approval request with the exact
  input and a summary, notifies the user, and denies the call with a reason
  that tells the model the user has been asked.
- On approval the API runs the stored call through the same tool registry and
  writes the result into the thread as an assistant message. No model is
  involved, and no LangGraph interrupt is used.
- The audit log is written by TITAN's tool wrapper, which runs in process
  around every domain tool and sees the before and after state directly. The
  policy is still enforced only in the `PreToolUse` hook.

## Consequences

- What runs is exactly what the user approved, on whichever node receives the
  answer.
- The agent cannot build on the result of an approved call within the same
  turn. The user continues in the chat, and the next turn sees the result
  message.
- LangGraph checkpoints stay short-lived: a turn's checkpoints are deleted when
  it ends. Interrupts remain available for workflows that pause inside a graph,
  such as a daily plan that waits for the user to pick between options.
- Every tool needs a summary that a person can approve without seeing the raw
  input.
