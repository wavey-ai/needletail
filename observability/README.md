# Needletail realtime observability

This product-level bundle persists the `av-contrib` and `av-mesh` metrics that Mission
Control shows in-process. It provisions:

- Prometheus with 15 days of local TSDB retention;
- p50, p95, and p99 recording rules for contributor forwarding and LL-HLS
  response handling;
- RelaySession recording rules for canonical objects, RaptorQ source/repair
  symbols, recovery, expiry, duplicates, and bounded drop reasons;
- Alertmanager with a local UI receiver;
- the **Needletail Realtime Qualification** Grafana dashboard.

Start the contributor-plus-mesh stack first, then run:

```sh
docker compose -f observability/compose.yml up -d
```

The local URLs are:

- Grafana: <http://127.0.0.1:3000/d/needletail-realtime>
- Prometheus: <http://127.0.0.1:9090>
- Alertmanager: <http://127.0.0.1:9093>

Grafana provisioning removes retired file-backed dashboards, so an existing
local volume converges on the Needletail qualification view after restart.

Named Docker volumes preserve Prometheus, Grafana, and Alertmanager state
across container restarts. Stop the services without deleting data with:

```sh
docker compose -f observability/compose.yml down
```

The local scrape file uses `host.docker.internal`, the default local ports, and
`insecure_skip_verify` for development certificates. A deployed configuration
must replace those targets and trust the deployment CA rather than disabling
verification. Load `prometheus/av-realtime.rules.yml` into the production
Prometheus rule path and import `grafana/dashboards/av-realtime.json` into the
production Grafana instance.

The included Alertmanager receiver intentionally keeps notifications in the
local UI. Production routes the `critical` and `warning` labels to the team's
paging/chat receivers. Every alert includes a short diagnosis path.

All latency thresholds carry `slo: provisional` and reproduce the current
local qualification gates. A deployed-region soak stating hardware, bitrate,
geography, concurrency, and viewer load establishes the global SLOs.

## Relay fabric qualification view

The dashboard follows one canonical media object from contributor emission to
edge publication:

- contributor carrier readiness for the primary source lane and warm secondary
  repair lane;
- canonical object output, RaptorQ source/repair symbols, encoding errors,
  carrier send errors, deadline budget/headroom, and the declared wall-clock
  error estimate;
- edge primary/secondary parents and an explicit split between authenticated
  sessions and controlled-qualification sessions;
- active, buffered, retained-complete, decoded, repaired, and expired object
  state;
- conflict, authentication, deadline, rejected, and duplicate datagram
  outcomes;
- LL-HLS traffic, errors, freshness, and response-handler p50/p95/p99.

`controlled_qualification` is an explicit trust-boundary state. Its alert is an
informational qualification marker. Authenticated carrier sessions are the
promotion evidence for an Internet-facing deployment.

## Current metric boundary

The service histograms currently support contributor forwarding and forwarding
stage quantiles plus LL-HLS response-handler quantiles. The wall-clock panel
reports the configured maximum error estimate carried by canonical timestamps;
a measured host clock-offset series belongs in the next exporter slice.

The next instrumentation slice should add measured distributions for:

- capture-to-contributor and canonical publication-to-edge completion latency;
- RelaySession scheduler wait, RaptorQ encode/decode, first-symbol arrival,
  object completion, and deadline-hit rate;
- parent RTT, jitter, loss, path stretch, and make-before-break failover time;
- contiguous-publication high-water, buffered gap count, and gap residence;
- reliable object fetch/backfill and carrier pacing/congestion state.

Stable, low-cardinality service exporters are the entry gate for adding those
measurements to the dashboard.

Validate the JSON, YAML, rule uniqueness, and Compose model with:

```sh
make observability-check
```

If `promtool` and `amtool` are installed, the validator also runs their native
rule and Alertmanager checks.
