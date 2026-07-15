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

- contributor carrier readiness and recoverable current health for the primary
  source lane and warm secondary repair lane;
- canonical object output, RaptorQ source/repair symbols, per-parent object
  outcomes, current lane impairment, surviving-lane deliveries, encoding
  errors, carrier send errors,
  deadline budget/headroom, and the declared wall-clock
  error estimate;
- native RelaySession total, encoder-lock wait, RaptorQ encode, source-first
  scheduling, primary-source send, warm-source send, and repair-send
  distributions, plus contributor deadline hits, misses, and symbol expiry;
- edge primary/secondary parents and an explicit split between authenticated
  sessions and controlled-qualification sessions;
- automatic warm-secondary state, primary-source and secondary-repair age,
  promotion/demotion transitions, control-command outcomes, and lease health;
- primary-silence detection, promotion-to-first-source, and cache-completion
  interruption measurements for every failover;
- active, buffered, retained-complete, decoded, repaired, and expired object
  state;
- conflict, authentication, deadline, rejected, and duplicate datagram
  outcomes;
- LL-HLS traffic, errors, freshness, and response-handler p50/p95/p99.

`controlled_qualification` is an explicit trust-boundary state. Its alert is an
informational qualification marker. Authenticated carrier sessions are the
promotion evidence for an Internet-facing deployment.

The qualification profile carries the bounded failover lease over its
controlled-link UDP channel. Internet-facing relay sessions carry the same
generation-fenced command over the authenticated reliable control channel,
alongside subscriptions, initialization objects, and bounded backfill. RaptorQ
source and repair symbols remain on the deadline-bound media carrier.

The failover alert set covers an unavailable warm path, command errors or
rejections, expired promotion leases, activation above 250 ms, and a media
completion gap above 250 ms. Mission Control uses the runtime transition time
for the event timeline and falls back to the snapshot time only when an older
runtime omits that field.

## Current metric boundary

The service histograms support contributor forwarding, native RelaySession
RaptorQ and carrier stages, canonical publication-to-verified-cache p50/p95/p99
at every relay, LL-HLS response-handler quantiles, and automatic warm-secondary
failover timings. Publication latency carries the maximum source-clock error
bound alongside the distribution; a measured host clock-offset series belongs
in the next exporter slice. Controller observations also expose selected-route
RTT, the fastest direct RTT baseline, path stretch, jitter, loss, queue delay,
and observation age.

The next instrumentation slice should add measured distributions for:

- capture-to-contributor latency when the contribution protocol supplies a
  source timestamp;
- RelaySession RaptorQ decode, first-symbol arrival, and receiver object
  completion distributions;
- per-parent RTT, jitter, loss, and continuous route-quality history;
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
