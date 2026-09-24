# Plan: M3 CLI client

Implements the first part of the "`titan` CLI covering chat, domains and admin"
item of [M3](../milestones.md), per the [CLI spec](../../spec/cli.md) and
[ADR 0011](../../adr/0011-cli-client-over-http.md). It also gives the M1 exit
criterion a client: pair, chat with streaming, approve a confirm-class action.

Branch `feature/m3-cli-client`, stacked on `feature/m4-budget-caps`.

Tasks:

- [x] Spec, ADR 0011 and this plan.
- [ ] Saved login (`client.json`, `0600`), API client with problem errors and
      an SSE reader.
- [ ] `login`, `logout`, `whoami`.
- [ ] `chat` with streaming, `--continue` and `--thread`.
- [ ] `approvals list|approve|reject`.
- [ ] Domain commands, one domain at a time (later branches).
