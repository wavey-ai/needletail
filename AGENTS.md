# Needletail agent notes

Needletail is the product-level composition repository. It owns multi-service
topology, lifecycle orchestration, qualification and soak tooling, deployed
composition, cross-service observability, and the `mission-control/` product UI.

Service implementations, protocol logic, reusable caches/FEC, and
service-specific images remain in sibling repositories such as `av-contrib`,
`av-mesh`, `playlists`, and `raptor-fec`.

The Rust supervisor serves local development and qualification. Needletail
production deployment uses the durable Needletail controller, host node agents,
and `systemd`-supervised native services. Never commit TLS keys, provider
credentials, generated artifacts, or production host secrets.

Default sibling paths may be overridden with `WORKSPACE_ROOT`, `AV_CONTRIB_ROOT`,
`AV_MESH_ROOT`, `CONTRIB_ROOT`, and `MESH_ROOT` as documented by each command.
