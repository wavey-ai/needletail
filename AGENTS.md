# Needletail agent notes

Needletail is the product-level composition repository. It owns multi-service
topology, lifecycle orchestration, qualification and soak tooling, deployed
composition, cross-service observability, and the `mission-control/` product UI.

Service implementations, protocol logic, reusable caches/FEC, and
service-specific images remain in sibling repositories such as `av-contrib`,
`av-mesh`, `playlists`, and `raptor-fec`.

Contributor-product adapters, identity/client configuration, app session policy,
DAW/plugin semantics, and app-specific end-to-end tests live in the owning
contributor application repositories. Needletail accepts those products through
generic ingest, capability, session, relay, and observability contracts.
Run `make product-boundary-check` before committing boundary-sensitive changes.

The Rust supervisor serves local development and qualification. Needletail
production deployment uses the durable Needletail controller, host node agents,
and `systemd`-supervised native services. Never commit TLS keys, provider
credentials, generated artifacts, or production host secrets.

Default sibling paths may be overridden with `WORKSPACE_ROOT`, `AV_CONTRIB_ROOT`,
`AV_MESH_ROOT`, `CONTRIB_ROOT`, and `MESH_ROOT` as documented by each command.

## Internal dashboard style

Needletail Operations is an internal operations surface. Build it like a mature
cloud console: dense, calm, predictable, and optimised for repeated diagnosis.

- Use `Needletail Operations` or `Needletail Ops` in visible product copy. Do
  not call the interface "Mission Control" or a "cockpit".
- Lead with the current state and the next useful action. Use plain labels such
  as `Overview`, `Network map`, `Throughput`, `Route health`, and `Refresh now`.
- Do not add slogans, value propositions, scene-setting paragraphs, or copy that
  tells operators the dashboard is "decision-first", "powerful", or "realtime".
- Use separate pages for major workflows and persistent navigation between
  them. Keep overview pages concise and put full tables, route details, metrics,
  and event history on their owning pages.
- Prefer compact toolbars, segmented filters, status summaries, tables, charts,
  and detail panes. Avoid oversized hero sections, decorative cards, nested
  cards, rounded pills for ordinary labels, and landing-page composition.
- Use colour only to encode state: green for healthy, amber for degraded or
  pending, red for failed or urgent, and blue for selected or informational.
  Keep the base palette neutral and maintain readable contrast.
- Keep card radii at 8px or less. Use borders, spacing, and type hierarchy before
  shadows or decorative effects.
- Make data freshness explicit. Show timestamps or ages close to the affected
  data and distinguish unavailable, stale, and healthy states.
- Keep live updates bounded and cheap. Prefer coarse snapshots and client-side
  deltas, pause routine polling while the page is hidden, cap retained chart
  history, and never require TRACE-level logging for dashboard data.
- Use direct, user-facing language. Infrastructure details belong in runbooks or
  developer documentation unless an operator needs them to make the immediate
  decision.
