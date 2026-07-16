SHELL := /bin/sh

CARGO ?= cargo
HOST ?= local.bitneedle.com
STREAM_ID ?= 1
PART_MS ?= 50
RUST_LOG ?= info
STACK_ARGS ?=

.DEFAULT_GOAL := help

.PHONY: help local local-debug local-fast build build-release check test fmt \
	mission-control-build mission-control-serve mission-control-check mission-control-test \
	realtime-benchmark realtime-qualification realtime-soak gcp-intercontinental-qualification two-region-smoke \
	observability-check observability-up observability-down product-boundary-check

help:
	@printf '%s\n' 'Needletail product orchestration'
	@printf '%s\n' ''
	@printf '%s\n' '  make local                  Build and run the local contributor + two-edge constellation'
	@printf '%s\n' '  make local-fast             Run existing release binaries and Mission Control assets'
	@printf '%s\n' '  make mission-control-build Build Needletail Mission Control assets'
	@printf '%s\n' '  make mission-control-serve Serve Mission Control with Trunk'
	@printf '%s\n' '  make mission-control-check Check product UI models and WASM'
	@printf '%s\n' '  make realtime-benchmark     Benchmark an already-running constellation'
	@printf '%s\n' '  make realtime-qualification Run local baseline + controlled-loss qualification'
	@printf '%s\n' '  make gcp-intercontinental-qualification Qualify the deployed four-region relay DAG'
	@printf '%s\n' '  make realtime-soak          Run an explicitly targeted deployed canary soak'
	@printf '%s\n' '  make two-region-smoke       Run the development two-region propagation smoke'
	@printf '%s\n' '  make observability-check    Validate product Prometheus/Alertmanager/Grafana assets'
	@printf '%s\n' '  make observability-up       Start local product observability on loopback'
	@printf '%s\n' '  make observability-down     Stop local product observability'
	@printf '%s\n' '  make product-boundary-check Check that product integrations stay outside Needletail'
	@printf '%s\n' '  make check                  Check the standalone Needletail Rust tools'
	@printf '%s\n' ''
	@printf '%s\n' 'Common overrides: STREAM_ID=1 PART_MS=50 RUST_LOG=info HOST=local.bitneedle.com'

local:
	AV_LL_HLS_PART_MS=$(PART_MS) RUST_LOG=$(RUST_LOG) \
	$(CARGO) run --locked --release --bin needletail -- \
		--host $(HOST) --stream-id $(STREAM_ID) --part-ms $(PART_MS) $(STACK_ARGS)

local-debug:
	AV_LL_HLS_PART_MS=$(PART_MS) RUST_LOG=$(RUST_LOG) \
	$(CARGO) run --locked --bin needletail -- \
		--host $(HOST) --stream-id $(STREAM_ID) --part-ms $(PART_MS) $(STACK_ARGS)

local-fast:
	AV_LL_HLS_PART_MS=$(PART_MS) RUST_LOG=$(RUST_LOG) \
	$(CARGO) run --locked --release --bin needletail -- \
		--host $(HOST) --stream-id $(STREAM_ID) --part-ms $(PART_MS) \
		--no-build --no-mission-control-build $(STACK_ARGS)

mission-control-build:
	$(MAKE) -C mission-control build

mission-control-serve:
	$(MAKE) -C mission-control serve

mission-control-check:
	$(MAKE) -C mission-control check

mission-control-test:
	$(MAKE) -C mission-control test

realtime-benchmark:
	./scripts/realtime-benchmark.sh

realtime-qualification:
	./scripts/realtime-qualification.sh

gcp-intercontinental-qualification:
	./scripts/gcp-intercontinental-qualification.sh

realtime-soak:
	./scripts/realtime-soak.sh

two-region-smoke:
	./scripts/two-region-smoke.sh

observability-check:
	./scripts/validate-observability.sh

observability-up:
	docker compose -f observability/compose.yml up -d

observability-down:
	docker compose -f observability/compose.yml down

product-boundary-check:
	./scripts/validate-product-boundary.sh

build:
	$(CARGO) build --locked

build-release:
	$(CARGO) build --locked --release

check: product-boundary-check
	$(CARGO) check --locked --all-targets
	$(MAKE) -C mission-control check

test:
	$(CARGO) test --locked
	$(MAKE) -C mission-control test

fmt:
	$(CARGO) fmt
	$(CARGO) fmt --manifest-path mission-control/Cargo.toml
