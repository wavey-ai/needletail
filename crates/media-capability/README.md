# media-capability

`media-capability` is the reusable public-key verifier for Infidelity
media-capability v1. It consumes the frozen `MediaCapabilityClaimsV1` contract
from `media-object`; it does not define, extend, or reinterpret claim fields.

The crate has no signing API and stores no private key. Issuance and private-key
custody remain in the Needletail issuer boundary.

## Strict compact JWS profile

A capability has exactly three nonempty compact-JWS segments. The protected
header is closed and canonically serialized as:

```json
{"alg":"EdDSA","kid":"opaque-key-id","typ":"infidelity-media-capability+jwt"}
```

The verifier enforces:

- exact `alg`, `typ`, and bounded opaque `kid` values;
- no duplicate or unknown protected-header fields;
- unpadded canonical base64url with no alternate trailing-bit representation;
- fixed header, claims, and Ed25519 signature bounds;
- Ed25519 `verify_strict` against a public-key keyring;
- signature verification before claims decoding or parsing;
- canonical compact media-control JSON bytes after authentication;
- pinned verifier-owned issuer and audience;
- the exact caller-provided current authorization context;
- an atomic caller-provided replay and resource-admission decision.

Header parsing necessarily precedes signature verification because `kid`
selects the public key. Claims parsing never does.

## Key rotation

`VerificationKeyring` contains only Ed25519 public keys. Each unique `kid` is
either:

- `Active`; or
- `Retiring { accept_until }`, accepted only while the verifier's trusted
  current time is earlier than that deadline.

An operator loads the new active key and the old retiring key together for at
least the maximum capability lifetime plus accepted clock skew. Duplicate IDs,
unknown IDs, invalid public keys, and expired overlap keys fail closed. A fully
removed key is unknown.

## Replay and admission

The only high-level success API is:

```text
MediaCapabilityVerifier::authorize(token, current_context, replay_admission_guard)
```

There is intentionally no implicit allow guard. A caller implements
`ReplayAdmissionGuard::check_and_admit` as one atomic operation appropriate to
its boundary. It can key replay state by capability, endpoint, edge, or an
equivalent connection lease and reject replay, capacity, policy, or control-
plane availability failures. The verifier returns no authorized capability
until that guard succeeds.

The guard receives only the capability ID, session ID, endpoint ID, serving
edge, operation, and expiry. It does not receive the compact token or signature.

## Safe diagnostics

Errors expose bounded machine-readable codes, static field names, and optional
underlying media-control or guard classifications. They never contain token,
signature, claim, identity, or key bytes. `Debug` for key IDs, the keyring,
verifier, claims, and authorized result is redacted.

Callers must likewise exclude compact JWS values from URLs, logs, metrics,
traces, crash reports, and analytics.

## Development

From the Needletail repository root:

```sh
cargo fmt --all -- --check
cargo check --locked -p media-capability --all-targets
cargo clippy --locked -p media-capability --all-targets -- -D warnings
cargo test --locked -p media-capability
RUSTDOCFLAGS="-D warnings" cargo doc --locked -p media-capability --no-deps
```
