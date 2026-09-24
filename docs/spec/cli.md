# CLI: `titan`

Status: **Draft v1**. Part of the [product spec](product.md#surfaces-v1).

One command, two kinds of subcommands:

- **Node administration** (`migrate`, `users`, `usage`, `budget`,
  `notifications`, `claude`, `openapi`) runs on a node and talks to its database
  directly. It needs the node's `TITAN_*` settings and no login.
- **Client** subcommands (`login`, `logout`, `whoami`, `chat`, `approvals`)
  run on any machine in the tailnet and talk to a node's HTTP API with a device
  token, like the Android app. They never touch a database.

## Logging in

`titan login SERVER USERNAME [--device-name NAME] [--password-stdin]` pairs this
machine as a `cli` device (`POST /api/v1/devices/pair`). `SERVER` is the node's
base URL, for example `http://titan-home:8000`. The password is asked for,
or read from standard input, and never stored. The device name defaults to
the host name.

The server, username, device id and token are saved in
`$XDG_CONFIG_HOME/titan/client.json` (`~/.config/titan/client.json` by
default), created with mode `0600` in a directory with mode `0700`
([ADR 0011](../adr/0011-cli-client-over-http.md)). `TITAN_CLIENT_CONFIG` points
at another file. Logging in again replaces the saved login and revokes the
device it held, when the server still accepts its token.

`titan logout` revokes the saved device on the server and deletes the file. When
the server cannot be reached, the file is kept and the command fails, so the
token is not forgotten while it still works; a token the server already
refuses is simply forgotten. `titan logout --local` deletes the
file only. `titan whoami` shows the server and the account.

## Chat

`titan chat MESSAGE` starts a new thread; `titan chat --continue MESSAGE`
continues the last thread used from this machine, and `titan chat --thread ID
MESSAGE` a given one. `MESSAGE` `-` reads the message from standard input.

- The reply streams to standard output as it is written, then ends with a
  newline.
- Tool activity and approval requests go to standard error, so the reply can be
  piped. An approval request shows its id and what the agent asked for, and how
  to answer it.
- A failed reply prints its error to standard error and exits with `1`.
- The thread id is printed to standard error when a new thread starts, and
  saved as the last thread.

## Approvals

`titan approvals list` shows pending requests: id, domain, tool and summary.
`titan approvals approve ID` and `titan approvals reject ID` answer one; approving
prints what the approved call returned.

## Errors

Any API error prints the problem's `title` and `detail` to standard error and
exits with `1`. A `401` adds that the device may have been revoked and that
`titan login` pairs it again. A client command without a saved login fails with
a hint to run `titan login`.

## Later

Domain commands (`titan tasks`, `titan notes`, …) follow the same pattern and
are added per domain.

## Acceptance criteria (v1)

- After `titan login`, `titan chat hello` streams a reply from the node, and
  `titan chat --continue` sees the previous exchange.
- The saved file is readable by its owner only, and holds no password.
- An approval requested during `titan chat` can be answered with
  `titan approvals approve ID`.
- `titan logout` makes the token fail on the next request.
