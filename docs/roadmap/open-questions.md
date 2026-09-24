# Open questions

| # | Question | Blocks | Where it gets answered |
|---|---|---|---|
| 1 | Which replicated database engine? | M1 cluster work, M3 | [ADR 0006](../adr/0006-replicated-database-with-vectors.md), M0 spike |
| 2 | How are health and finance data protected at rest and towards Claude? | Sharing sensitive data | [ADR 0007](../adr/0007-sensitive-data-protection.md) |
| 3 | Which embedding model? Must retrieve across English, Russian and Ukrainian ([Languages](../spec/product.md#languages)); also dimension and CPU speed | Notes and memory | M0 |
| 4 | Conflict rules per entity when two nodes edit the same row (last writer wins, field merge, or keep both)? | M3 | Follows from ADR 0006 |
| 5 | When is the `titan-web` repository created, and with which stack? | M3 Web UI | Separate ADR in titan-web |
| 6 | How does a client choose and fail over between nodes (fixed list, DNS on the tailnet, Tailscale Serve)? | M3 | Architecture update |
| 7 | Time zone handling for family members in different zones | Planner | Calendar spec update |
