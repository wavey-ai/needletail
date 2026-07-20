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

## GCP performance lab

Use local hosts for correctness, build, UI, and browser checks. Run load,
capacity, profiling, and soak tests on explicitly scoped GCP hosts. Keep the
source, contributor, relays, edge, and load reader in one region and on private
subnets unless the test measures geographic distance.

Before a latency test, configure all lab hosts to use the GCP metadata time
server and verify the clock gate:

```sh
GCP_PROJECT=<project-id> deploy/gcp-lab/configure-clock.sh
```

Run the matched live-tail profile with an explicit label:

```sh
GCP_PROJECT=<project-id> \
  GCP_PROFILE_RUN_LABEL=<run-label> \
  scripts/gcp-live-tail-profile.sh
```

The profile retains private-path geometry, binary hashes, clock state, service
state, exact media counters, latency, CPU, journals, invalid attempts, and
cleanup evidence under `target/gcp-qualification/`. Add a sanitized JSON record
and dated narrative under `docs/real-world-tests/` before making a performance
claim. Do not commit provider credentials, host secrets, TLS private keys, or
raw generated artifacts.
