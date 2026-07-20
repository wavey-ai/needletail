# capability-controller

`capability-controller` is the bounded P02 control-plane core for Needletail.
It re-evaluates the P01 identity authorization view. It admits a current P00
route deterministically. It signs the frozen compact Ed25519 capability contract
and creates proof-bound one-use browser exchanges.

The crate deliberately does not expose an HTTP listener or a public browser
identity endpoint. Network transport, mTLS, durable distributed exchange
storage, secret-manager/TPM key loading, and deployed edge invalidation remain
P24 integration work. The supplied identity transport and exchange storage are
traits. `InMemoryExchangeStore` is process-local test infrastructure and is not
production durability.

Private signing and HMAC keys are constructor inputs only. They are redacted by
`Debug`, are never serialized, and have no file/environment loader in this
crate. Browser responses contain a 15-second one-use exchange value, never the
reusable signed edge capability or endpoint descriptor.

Development gates from the Needletail repository root:

```sh
cargo fmt --all -- --check
cargo check --locked -p capability-controller --all-targets
cargo clippy --locked -p capability-controller --all-targets -- -D warnings
cargo test --locked -p capability-controller
RUSTDOCFLAGS="-D warnings" cargo doc --locked -p capability-controller --no-deps
```
