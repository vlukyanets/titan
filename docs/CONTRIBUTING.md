# Contributing

These rules for branches, commits and pull requests apply to humans and AI
agents alike, in every TITAN repository
([titan](https://github.com/vlukyanets/titan),
[titan-android](https://github.com/vlukyanets/titan-android),
[titan-web](https://github.com/vlukyanets/titan-web)). Branches,
commits and pull requests are read by people, so write them for people.

## Branches

Branch names say what stage of the project the work belongs to and what it
changes, so the list of branches reads like a status board.

```text
<kind>/<milestone>-<topic>
```

- **kind** is one of:

  | Kind | Use for |
  |---|---|
  | `feature` | New behaviour |
  | `fix` | Bug fixes |
  | `spec` | Specs, ADRs, architecture docs, roadmap |
  | `research` | Spikes and experiments that may never be merged |
  | `refactor` | Restructuring without behaviour change |
  | `chore` | Build, CI, dependencies, tooling |

- **milestone** is the roadmap milestone the work belongs to: `m0`–`m4` in
  titan, `a1`–`a4` in titan-android, `w1`–`w4` in titan-web. Leave it out
  only for work outside any milestone (`fix/crash-on-empty-plan`).
- **topic** is two to five words in kebab-case that name the feature or change.
- Lowercase letters, digits and hyphens only, one `/`, at most 50 characters.
- No personal names, dates, ticket numbers or tool-generated names such as
  `claude/bold-ritchie-f571jn`.
- A change that spans several repositories uses the **same branch name** in
  each, with the backend milestone (`feature/m1-device-pairing` in all of them).
- Branch from `master`. Delete the branch once it has been merged.

| Good | Bad | Why it is bad |
|---|---|---|
| `spec/m0-foundation-docs` | `docs` | No kind or topic |
| `feature/m1-device-pairing` | `feature/DevicePairing` | Uppercase, no milestone |
| `research/m0-db-replication` | `alice/test` | Personal name, says nothing |
| `fix/a2-chat-stream-reconnect` | `fix/issue-42` | Ticket number instead of a topic |

## Commit messages

A commit message has two parts: a title line and one explanatory paragraph.

```text
Added per-domain autonomy policy to the product spec

Each domain now declares how the agent may act for reads, internal writes,
external side effects and destructive operations, so finance can require
confirmation while tasks stay fully automatic. The defaults match what we
agreed on for v1 and every user can override them per domain.
```

### Title line

- Starts with a **past-tense verb** that says what happened: `Added`, `Fixed`,
  `Removed`, `Changed`, `Updated`, `Refactored`, `Renamed`, `Moved`, `Improved`.
- At most **72 characters**, no trailing period.
- No type prefixes such as `feat:`, `fix:` or `chore:`. The verb already says it.
- An optional scope is allowed when the change is clearly about one area:
  `Fixed sync: handled clock skew between nodes`.
- No issue numbers in the title.

### Body

- Exactly **one paragraph**, separated from the title by a blank line.
- Explain what changed and why in plain language that someone unfamiliar with
  the code can follow. The diff shows *how*; the message explains *why*.
- Wrap lines at about 72 characters.
- Nothing follows the paragraph: no `Co-Authored-By:`, session links or other
  trailers, and no AI attribution of any kind.

### Examples

| Good | Bad | Why it is bad |
|---|---|---|
| `Added reminder delivery through ntfy` | `feat: ntfy` | Type prefix, says nothing |
| `Fixed duplicate tasks after offline edits` | `Fix bug.` | Not past tense, vague, trailing period |
| `Removed unused calendar sync stub` | `WIP` | Not a description |
| `Changed default model for log parsing to Haiku` | `Changed model (#42)` | Issue number in the title, vague |

## Pull requests

- **Title** follows the same rules as a commit title.
- **Description** is written for a human reviewer and may have several
  paragraphs. Use the template in
  [`.github/pull_request_template.md`](../.github/pull_request_template.md):
  what changed, why, how to verify it, and what could go wrong.
- Issue references (`Closes #12`, `Refs #7`) go at the **end** of the
  description, never in the title.
- No AI attribution: no "Generated with …" footers, bot signatures or session
  links. The description explains the change and nothing else.
- Keep a pull request focused on one change. Unrelated fixes get their own PR.
- If the change affects behaviour described in `docs/`, update those documents
  in the same pull request.

## Merging

Pull requests are merged with **rebase merge**. Every commit lands on `master`
as-is, so every commit must:

- follow the message rules above;
- leave the repository in a working state (builds and tests pass);
- be meaningful on its own: squash "fix typo" and "address review" commits into
  the commit they fix before the branch is merged.

## Documentation layout

- `docs/` holds **permanent** documentation: specifications, architecture,
  architecture decision records (ADRs). It describes how the system is and why.
- `docs/roadmap/` holds **volatile** planning material: milestones, plans, task
  lists, open questions. It is expected to change often and to be pruned when
  work is done.
