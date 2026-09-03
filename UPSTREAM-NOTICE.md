# Upstream and attribution notice

This SCEX-specific project adapts operational ideas and selected implementations from:

- `i0czf/minecraft-server-ops-kit`
- <https://github.com/i0czf/minecraft-server-ops-kit>
- deployment snapshot recorded as reviewed: `19de94de25efe149d9364ed6572cd634e8138112`

The private deployment notice recorded the reviewed snapshot as PolyForm Noncommercial
1.0.0. The upstream `main` README observed during this public export (2026-09-03) says
that its formal license will be added before the first public release. The SCEX project
owner states that the upstream author has permitted this noncommercial derivative.
Because the public upstream metadata is currently inconsistent, this repository takes
the conservative route and distributes the combined work under the PolyForm
Noncommercial License 1.0.0. This notice does not grant commercial rights to upstream
code or third-party components.

The integration was substantially rewritten as small Python modules around OneBot v11,
RCON, BlueMap, SCEX event formats, local state and safety gates. Secrets, live
configuration, server logs, player data, maps, worlds and QQ login state are not part of
this repository.

The conversational role-card structure and lightweight social simulation also draw on
public design ideas from:

- `Derpyu520/qq-bridge`
- <https://github.com/Derpyu520/qq-bridge>

This project does not include that repository's DeepSeek Harness agent runtime or copy
its packaged dependencies. See `AI-GENERATED.md` for the development provenance.

