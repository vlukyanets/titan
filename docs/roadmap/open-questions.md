# Open questions

| # | Question | Blocks | Where it gets answered |
|---|---|---|---|
| 2 | How are health and finance data protected at rest and towards Claude? | Sharing sensitive data, VPS node | [ADR 0007](../adr/0007-sensitive-data-protection.md) |
| 3 | Which embedding model (language coverage, dimension, CPU speed)? | Notes and memory | M0 |
| 4 | Which entities need more than write affinity and last commit wins per row (append-only rows, Spock `delta_apply` columns, or keeping both versions of a note)? | M2 domain models | Domain specs, rules in [ADR 0006](../adr/0006-replicated-database-with-vectors.md) |
| 5 | When is the `titan-web` repository created, and with which stack? | M3 Web UI | Separate ADR in titan-web |
| 6 | How does a client choose and fail over between nodes (fixed list, DNS on the tailnet, Tailscale Serve)? | M3 | Architecture update |
| 7 | Which languages does the assistant speak to users (English only, or several)? | Prompts, embeddings | Product spec update |
| 8 | Time zone handling for family members in different zones | Planner | Calendar spec update |
