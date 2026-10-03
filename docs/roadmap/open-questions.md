# Open questions

| # | Question | Blocks | Where it gets answered |
|---|---|---|---|
| 1 | ~~Which replicated database engine?~~ Answered by [ADR 0006](../adr/0006-replicated-database-with-vectors.md): PostgreSQL with pgEdge Spock and pgvector | M1 cluster work, M3 | [ADR 0006](../adr/0006-replicated-database-with-vectors.md), M0 spike |
| 2 | ~~How are health and finance data protected at rest and towards Claude?~~ Answered by [ADR 0007](../adr/0007-sensitive-data-protection.md): an encrypted Docker volume on every node and per-domain limits on what reaches Claude | Sharing sensitive data | [ADR 0007](../adr/0007-sensitive-data-protection.md) |
| 3 | ~~Which embedding model?~~ Answered by [ADR 0014](../adr/0014-embeddings-from-a-local-server.md): a local OpenAI-compatible server with the model as a setting; the default, `snowflake-arctic-embed-m-v2.0`, was picked by a benchmark | Notes and memory | [ADR 0014](../adr/0014-embeddings-from-a-local-server.md) |
| 4 | Which entities need more than write affinity and last commit wins per row (append-only rows, Spock `delta_apply` columns, or keeping both versions of a note)? | M2 domain models | Domain specs, rules in [ADR 0006](../adr/0006-replicated-database-with-vectors.md) |
| 5 | ~~When is the `titan-web` repository created, and with which stack?~~ Answered: created in M3 with React and Vite ([titan-web ADR 0004](https://github.com/vlukyanets/titan-web/blob/master/docs/adr/0004-frontend-framework.md)) | M3 Web UI | Separate ADR in titan-web |
| 6 | ~~How does a client choose and fail over between nodes (fixed list, DNS on the tailnet, Tailscale Serve)?~~ Answered by [ADR 0013](../adr/0013-one-cluster-address.md): one cluster address through Tailscale Services, with the node addresses as a fallback | M3 | Architecture update |
| 7 | ~~Which languages does the assistant speak to users (English only, or several)?~~ Answered in the [product spec](../spec/product.md#languages): English, Russian and Ukrainian from the start, without being limited to them | Prompts, embeddings | Product spec update |
| 8 | ~~Time zone handling for family members in different zones~~ Answered in the [calendar spec](../spec/domains/calendar.md#time-zones): a zone per user and per event. Tasks and reminders repeat in their owner's zone | Planner | Calendar spec update |
