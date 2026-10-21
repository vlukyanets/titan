# AGENTS.md

TITAN is a personal AI assistant that runs on the owner's own machines. A
user chats with it from a CLI; it keeps their tasks and the facts it
learns about them. Each user brings their own OpenAI-compatible model
provider and key, and each user's data is kept apart from the others.

The project is a prototype: get the smallest version working end to end,
then harden it. Working code beats documents.

## Layout

- `backend/`: the app, in Python, managed with `uv`.
- `cli/`: the `titan` CLI in Go.
- `docs/architecture.md`: how the parts fit together, the invariants and
  the decisions. Read it before changing the structure or a security
  boundary.

## Commands

- `make check`: lint, type check and tests of everything. Run it before
  saying a change works.
- One Python test: `cd backend && uv run pytest tests/test_x.py::test_y`.
- One Go test: `cd cli && go test ./... -run TestY`.

## How to work

- Work comes from a GitHub issue (`gh issue view N`). Build to its
  acceptance criteria, one test per criterion. When it is done, move what
  stays true into `docs/`.
- Use library defaults. Add a setting, constant or timeout only when
  something breaks without it.
- Test the main path and the invariants in `docs/architecture.md`. A
  test for a bug fails before the fix and passes after it.
- Done means `make check` passed when you ran it. If a check failed or
  could not run, say so and show its output.
- Keep changes small: one topic per branch.
- Read and change files with the file tools (Read, Edit, Write), not
  shell commands: no `cat`, `sed -i`, `echo >` or heredocs. Delete only
  with `git rm`, after asking.

## Ask the owner first

Bring these as 2-3 options with a recommendation, and wait for a yes:

- a new dependency;
- a change to the HTTP API, the database schema or the security model;
- anything hard to undo, such as deleting data, branches or files that are
  not part of the current change.

A question from the owner is not approval.

## Never

- Put secrets, tokens or personal data in files, logs, commits or chat.
  Never read, copy or ask for the owner's model credentials.
- Run a test that calls a real model; only the owner does.
- Commit, push or open a pull request unless the owner says so. Never
  merge, approve or close a pull request, touch tags, publish, release or
  deploy.
- Expose an interface without authentication, even on a private network.
  Never assume how a node is reached (VPN, Tailscale, a proxy).
- Add AI attribution anywhere: no `Co-Authored-By`, session links or
  "Generated with" lines in commits or pull requests.
- Disable, skip or weaken a check to make it pass.
- Change `user.name` or `user.email`, pass `--author`, run a bare
  `git stash` or `pkill`.

## Git

- Name a branch after its topic, in a few lowercase words joined by
  hyphens, such as `sign-in` or `chat`: no prefix like `claude/` or
  `worktree-`, no random words. Its worktree folder has the same name.
- Work in a worktree of your own, never in the main checkout:
  `git worktree add .claude/worktrees/<branch> -b <branch> origin/master`.
  Not `claude --worktree`, which names the branch itself. A branch that
  builds on another one starts from that branch instead.
- Run every command inside the worktree. The main checkout stays on
  `master`; do not switch, rebase or reset it.
- Rebase a stack of branches from the worktree of its top branch, with
  `--update-refs`; git skips a branch checked out in another worktree.
- Remove a worktree only when its branch is merged or the owner drops it,
  and only when `git status` there prints nothing.
- Commit title: imperative, at most 72 characters, no prefix, no period.
  The body says why.
- Push only with `--force-with-lease --force-if-includes`.
- A pull request says `Closes #N`.

## Writing

- English, plain ASCII: no long dashes, curly quotes, arrows or emoji.
- Markdown is formatted by hand; tables are unpadded, with the separator
  `| --- | --- |`.

## This file

Add a rule here only after an agent made that mistake. Keep the file under
100 lines.
