SHELL := /bin/sh

CARGO ?= cargo
TRUNK ?= trunk
TRUNK_ENV ?= env -u NO_COLOR TRUNK_COLOR=never
ADDRESS ?= 127.0.0.1
PORT ?= 5188
DIST ?= dist

.DEFAULT_GOAL := help

.PHONY: help serve build check test clean

help:
	@printf '%s\n' 'Needletail Operations'
	@printf '%s\n' ''
	@printf '%s\n' '  make serve   Serve Needletail Operations with Trunk'
	@printf '%s\n' '  make build   Build release WASM assets'
	@printf '%s\n' '  make check   Check native models and the WASM app'
	@printf '%s\n' '  make test    Test bounded snapshot parsing and summaries'
	@printf '%s\n' '  make clean   Remove generated assets'

serve:
	$(TRUNK_ENV) $(TRUNK) serve --address $(ADDRESS) --port $(PORT)

build:
	CARGO=$(CARGO) TRUNK=$(TRUNK) ./scripts/build.sh $(abspath $(DIST))

check:
	$(CARGO) check --locked
	$(CARGO) check --locked --target wasm32-unknown-unknown

test:
	$(CARGO) test --locked

clean:
	rm -rf $(DIST)
