# Native deployment and control

Component repositories produce native service binaries.`systemd` supervises
them on explicitly provisioned hosts managed by Needletail.

The target production control path is:

1. Provider adapters create hosts, private networking, DNS, and storage.
2. Cloud-init installs a short-lived bootstrap identity and the Needletail node
   agent.
3. The agent establishes mTLS to the Needletail controller and exchanges a
   certificate-bound node identity for short-lived workload credentials.
4. The controller publishes a versioned desired-state generation: approved
   artifact hashes, service roles, relay parents, stream placement, limits,
   drain state, and rollout policy.
5. The agent reconciles native binaries and `systemd` units idempotently, then
   reports observed state, command IDs, health, and failure reasons.
6. Durable leases and fencing prevent a replaced or partitioned node from
   continuing to publish or control traffic.

The first controller store may be PostgreSQL behind a storage trait. It holds
desired state, observed generations, leases, idempotency keys, and an append-only
audit log. Realtime media flows directly between ingress, relays, and edges.

The existing provider bootstrap script under `scripts/` remains a lab migration
tool invoked explicitly by an operator. The durable controller becomes the
authoritative source for production reconciliation.
