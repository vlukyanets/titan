# 0011. The CLI client talks HTTP and keeps its token in a private file

- Status: Proposed
- Date: 2026-09-24

## Context

The product spec lists a `titan` CLI for power use from any machine in the
tailnet. The node administration commands already exist and use the database
directly, which only works on a node. A client needs the same API the Android
app uses ([accounts](../spec/accounts.md)): pair once, keep a device token,
send it with every call. The spec asks clients to keep the token in secure
storage.

[ADR 0004](0004-openapi-from-fastapi.md) says clients generate their API code
from the committed `openapi.json`. That is written for clients in other
repositories.

## Options

Token storage:

1. **OS keyring** (`keyring` package): encrypted at rest on desktops. Headless
   Linux machines, which is where a CLI often runs, usually have no keyring
   backend, so it needs a fallback anyway, and adds a dependency with
   platform-specific backends.
2. **A file readable only by the user** (`0600` in a `0700` directory), as
   `gh`, `kubectl` and `tailscale` do. Protected by file permissions and, on
   TITAN's machines, by full-disk encryption
   ([ADR 0007](0007-sensitive-data-protection.md)). Anyone who can read the
   user's files can use the token until it is revoked.

API code:

1. **Generate a client** from `openapi.json` (for example
   `openapi-python-client`): the same path as other repositories, but generated
   code and a generator in the build, for a client that ships in the same
   package as the server.
2. **Parse responses with the API's own Pydantic models**: the client lives in
   the same package and release as the server, so the models cannot drift from
   the schema, which is generated from them.

## Decision

The CLI client talks to a node only over HTTP, with a device token paired as
platform `cli`. It saves the token in `$XDG_CONFIG_HOME/titan/client.json` with
mode `0600` in a `0700` directory, and never stores the password. It parses
responses with the Pydantic models in `titan.api`, which ADR 0004's generated
schema is made from; generating a client stays the rule for other repositories.

## Consequences

- One command works everywhere: administration on nodes, client commands from
  any machine with the package installed.
- A stolen file is a working token until `titan logout` or a revoke from another
  device. Tokens are per device, so revoking one does not affect others.
- The client package includes the server code. Splitting it out later means
  generating a client as ADR 0004 describes.
- A keyring can be added later as an option without changing the file format's
  other fields.
