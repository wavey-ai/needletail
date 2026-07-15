use std::collections::BTreeMap;
use std::fmt;
use std::sync::Mutex;

use base64::engine::general_purpose::URL_SAFE_NO_PAD;
use base64::Engine as _;
use media_object::{EdgeId, EndpointId, MediaClass, MediaEndpointDescriptorV1, Operation};
use serde::{Serialize, Serializer};
use sha2::{Digest, Sha256};
use subtle::ConstantTimeEq as _;
use zeroize::Zeroize;

use crate::error::ExchangeRejection;

const EXCHANGE_TOKEN_BYTES: usize = 32;
const EXCHANGE_TOKEN_CHARACTERS: usize = 43;
const DEFAULT_MAX_ENTRIES: usize = 10_000;
const MAX_CONFIGURED_ENTRIES: usize = 1_000_000;
const REDACTED: &str = "[REDACTED]";

/// Reusable compact capability kept behind an authenticated internal boundary.
#[derive(Clone, Eq, PartialEq)]
pub struct CompactCapability(String);

impl CompactCapability {
    pub(crate) fn new(value: String) -> Self {
        Self(value)
    }

    /// Deliberately expose the JWS only to an authenticated native/edge adapter.
    #[must_use]
    pub fn expose(&self) -> &str {
        &self.0
    }
}

impl Serialize for CompactCapability {
    fn serialize<S>(&self, serializer: S) -> std::result::Result<S::Ok, S::Error>
    where
        S: Serializer,
    {
        serializer.serialize_str(&self.0)
    }
}

impl fmt::Debug for CompactCapability {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter
            .debug_tuple("CompactCapability")
            .field(&REDACTED)
            .finish()
    }
}

impl Drop for CompactCapability {
    fn drop(&mut self) {
        self.0.zeroize();
    }
}

/// Browser-visible, short-lived, one-use exchange value.
#[derive(Clone, Eq, PartialEq)]
pub struct ExchangeToken(String);

impl ExchangeToken {
    pub(crate) fn from_bytes(bytes: [u8; EXCHANGE_TOKEN_BYTES]) -> Self {
        Self(URL_SAFE_NO_PAD.encode(bytes))
    }

    /// Return the token for the one authenticated POST body/header use.
    #[must_use]
    pub fn expose(&self) -> &str {
        &self.0
    }
}

impl Serialize for ExchangeToken {
    fn serialize<S>(&self, serializer: S) -> std::result::Result<S::Ok, S::Error>
    where
        S: Serializer,
    {
        serializer.serialize_str(&self.0)
    }
}

impl fmt::Debug for ExchangeToken {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter
            .debug_tuple("ExchangeToken")
            .field(&REDACTED)
            .finish()
    }
}

impl Drop for ExchangeToken {
    fn drop(&mut self) {
        self.0.zeroize();
    }
}

/// Public edge consume input. The token is never included in `Debug`.
pub struct ExchangeConsumeRequest<'a> {
    pub token: &'a str,
    pub edge_id: &'a EdgeId,
    pub endpoint_id: &'a EndpointId,
    pub endpoint_proof_thumbprint: &'a str,
    pub now: i64,
}

impl fmt::Debug for ExchangeConsumeRequest<'_> {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter
            .debug_struct("ExchangeConsumeRequest")
            .field("token", &REDACTED)
            .field("edge_id", &REDACTED)
            .field("endpoint_id", &REDACTED)
            .field("endpoint_proof_thumbprint", &REDACTED)
            .field("now", &self.now)
            .finish()
    }
}

/// Internal record handed to an exchange storage adapter.
pub struct ExchangeStoreInsert {
    token_hash: [u8; 32],
    expected_edge_id: EdgeId,
    expected_endpoint_id: EndpointId,
    expected_endpoint_proof: String,
    consume_expires_at: i64,
    capability_expires_at: i64,
    capability: CompactCapability,
    descriptor: MediaEndpointDescriptorV1,
    operation: Operation,
    media_class: MediaClass,
}

impl ExchangeStoreInsert {
    #[allow(clippy::too_many_arguments)]
    pub(crate) fn new(
        token_hash: [u8; 32],
        expected_edge_id: EdgeId,
        expected_endpoint_id: EndpointId,
        expected_endpoint_proof: String,
        consume_expires_at: i64,
        capability_expires_at: i64,
        capability: CompactCapability,
        descriptor: MediaEndpointDescriptorV1,
        operation: Operation,
        media_class: MediaClass,
    ) -> Self {
        Self {
            token_hash,
            expected_edge_id,
            expected_endpoint_id,
            expected_endpoint_proof,
            consume_expires_at,
            capability_expires_at,
            capability,
            descriptor,
            operation,
            media_class,
        }
    }

    /// Return the irreversible SHA-256 lookup key, never the token.
    #[must_use]
    pub const fn token_hash(&self) -> &[u8; 32] {
        &self.token_hash
    }

    #[must_use]
    pub const fn expected_edge_id(&self) -> &EdgeId {
        &self.expected_edge_id
    }

    #[must_use]
    pub const fn expected_endpoint_id(&self) -> &EndpointId {
        &self.expected_endpoint_id
    }

    #[must_use]
    pub fn expected_endpoint_proof(&self) -> &str {
        &self.expected_endpoint_proof
    }

    #[must_use]
    pub const fn consume_expires_at(&self) -> i64 {
        self.consume_expires_at
    }

    /// Return the independently signed capability/lease expiry.
    #[must_use]
    pub const fn capability_expires_at(&self) -> i64 {
        self.capability_expires_at
    }

    #[must_use]
    pub const fn capability(&self) -> &CompactCapability {
        &self.capability
    }

    #[must_use]
    pub const fn descriptor(&self) -> &MediaEndpointDescriptorV1 {
        &self.descriptor
    }

    #[must_use]
    pub const fn operation(&self) -> Operation {
        self.operation
    }

    #[must_use]
    pub const fn media_class(&self) -> MediaClass {
        self.media_class
    }
}

impl fmt::Debug for ExchangeStoreInsert {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter
            .debug_struct("ExchangeStoreInsert")
            .field("token_hash", &REDACTED)
            .field("expected_edge_id", &REDACTED)
            .field("expected_endpoint_id", &REDACTED)
            .field("expected_endpoint_proof", &REDACTED)
            .field("consume_expires_at", &self.consume_expires_at)
            .field("capability_expires_at", &self.capability_expires_at)
            .field("capability", &self.capability)
            .field("descriptor", &self.descriptor)
            .field("operation", &self.operation)
            .field("media_class", &self.media_class)
            .finish()
    }
}

/// Hash-only consume operation handed to the atomic storage boundary.
pub struct ExchangeStoreConsume<'a> {
    pub token_hash: [u8; 32],
    pub edge_id: &'a EdgeId,
    pub endpoint_id: &'a EndpointId,
    pub endpoint_proof_thumbprint: &'a str,
    pub now: i64,
}

impl fmt::Debug for ExchangeStoreConsume<'_> {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter
            .debug_struct("ExchangeStoreConsume")
            .field("token_hash", &REDACTED)
            .field("edge_id", &REDACTED)
            .field("endpoint_id", &REDACTED)
            .field("endpoint_proof_thumbprint", &REDACTED)
            .field("now", &self.now)
            .finish()
    }
}

/// Internal lease released exactly once to the selected edge.
#[derive(Clone, Serialize)]
pub struct ExchangeLease {
    capability: CompactCapability,
    descriptor: MediaEndpointDescriptorV1,
    expires_at: i64,
    operation: Operation,
    media_class: MediaClass,
}

impl ExchangeLease {
    #[must_use]
    pub const fn capability(&self) -> &CompactCapability {
        &self.capability
    }

    #[must_use]
    pub const fn descriptor(&self) -> &MediaEndpointDescriptorV1 {
        &self.descriptor
    }

    #[must_use]
    pub const fn expires_at(&self) -> i64 {
        self.expires_at
    }

    #[must_use]
    pub const fn operation(&self) -> Operation {
        self.operation
    }

    #[must_use]
    pub const fn media_class(&self) -> MediaClass {
        self.media_class
    }
}

impl fmt::Debug for ExchangeLease {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter
            .debug_struct("ExchangeLease")
            .field("capability", &self.capability)
            .field("descriptor", &self.descriptor)
            .field("expires_at", &self.expires_at)
            .field("operation", &self.operation)
            .field("media_class", &self.media_class)
            .finish()
    }
}

/// Atomic exchange persistence boundary.
pub trait ExchangeStore: Send + Sync {
    /// Insert a hash-keyed unconsumed exchange.
    ///
    /// # Errors
    ///
    /// Returns a stable duplicate/capacity/unavailable reason.
    fn insert(&self, insert: ExchangeStoreInsert) -> std::result::Result<(), ExchangeRejection>;

    /// Atomically compare every binding and mark one exchange consumed.
    ///
    /// # Errors
    ///
    /// Returns a stable reason without revealing whether a supplied token maps
    /// to another endpoint/session to public callers.
    fn consume(
        &self,
        consume: ExchangeStoreConsume<'_>,
    ) -> std::result::Result<ExchangeLease, ExchangeRejection>;

    /// Remove expired/consumed records according to the adapter's retention policy.
    ///
    /// # Errors
    ///
    /// Returns storage-unavailable without exposing records.
    fn purge_expired(&self, now: i64) -> std::result::Result<usize, ExchangeRejection>;
}

struct StoredExchange {
    expected_edge_id: EdgeId,
    expected_endpoint_id: EndpointId,
    expected_endpoint_proof: String,
    consume_expires_at: i64,
    consumed_at: Option<i64>,
    lease: ExchangeLease,
}

/// Process-local mutex-backed implementation for deterministic tests only.
///
/// It is atomic inside one process, but it is neither durable nor distributed
/// and must not be represented as a production P24 store.
pub struct InMemoryExchangeStore {
    max_entries: usize,
    records: Mutex<BTreeMap<[u8; 32], StoredExchange>>,
}

impl Default for InMemoryExchangeStore {
    fn default() -> Self {
        Self::new(DEFAULT_MAX_ENTRIES).expect("the default exchange capacity is valid")
    }
}

impl InMemoryExchangeStore {
    /// Construct a bounded process-local test store.
    ///
    /// # Errors
    ///
    /// Returns invalid configuration for zero or excessive capacity.
    pub fn new(max_entries: usize) -> std::result::Result<Self, ExchangeRejection> {
        if max_entries == 0 || max_entries > MAX_CONFIGURED_ENTRIES {
            return Err(ExchangeRejection::StorageUnavailable);
        }
        Ok(Self {
            max_entries,
            records: Mutex::new(BTreeMap::new()),
        })
    }

    /// Return record count for deterministic tests and local diagnostics.
    ///
    /// # Errors
    ///
    /// Returns storage-unavailable if the process-local mutex is poisoned.
    pub fn len(&self) -> std::result::Result<usize, ExchangeRejection> {
        self.records
            .lock()
            .map(|records| records.len())
            .map_err(|_error| ExchangeRejection::StorageUnavailable)
    }

    /// Return whether the local store contains no records.
    ///
    /// # Errors
    ///
    /// Returns storage-unavailable if the process-local mutex is poisoned.
    pub fn is_empty(&self) -> std::result::Result<bool, ExchangeRejection> {
        self.len().map(|length| length == 0)
    }
}

impl ExchangeStore for InMemoryExchangeStore {
    fn insert(&self, insert: ExchangeStoreInsert) -> std::result::Result<(), ExchangeRejection> {
        let mut records = self
            .records
            .lock()
            .map_err(|_error| ExchangeRejection::StorageUnavailable)?;
        if records.contains_key(&insert.token_hash) {
            return Err(ExchangeRejection::DuplicateToken);
        }
        if records.len() >= self.max_entries {
            return Err(ExchangeRejection::StorageUnavailable);
        }
        records.insert(
            insert.token_hash,
            StoredExchange {
                expected_edge_id: insert.expected_edge_id,
                expected_endpoint_id: insert.expected_endpoint_id,
                expected_endpoint_proof: insert.expected_endpoint_proof,
                consume_expires_at: insert.consume_expires_at,
                consumed_at: None,
                lease: ExchangeLease {
                    capability: insert.capability,
                    descriptor: insert.descriptor,
                    expires_at: insert.capability_expires_at,
                    operation: insert.operation,
                    media_class: insert.media_class,
                },
            },
        );
        Ok(())
    }

    fn consume(
        &self,
        consume: ExchangeStoreConsume<'_>,
    ) -> std::result::Result<ExchangeLease, ExchangeRejection> {
        let mut records = self
            .records
            .lock()
            .map_err(|_error| ExchangeRejection::StorageUnavailable)?;
        let record = records
            .get_mut(&consume.token_hash)
            .ok_or(ExchangeRejection::NotFound)?;
        if consume.now >= record.consume_expires_at {
            return Err(ExchangeRejection::Expired);
        }
        if record.consumed_at.is_some() {
            return Err(ExchangeRejection::AlreadyConsumed);
        }
        if consume.edge_id != &record.expected_edge_id {
            return Err(ExchangeRejection::WrongEdge);
        }
        if consume.endpoint_id != &record.expected_endpoint_id {
            return Err(ExchangeRejection::WrongEndpoint);
        }
        if !constant_time_equal(
            consume.endpoint_proof_thumbprint.as_bytes(),
            record.expected_endpoint_proof.as_bytes(),
        ) {
            return Err(ExchangeRejection::WrongEndpointProof);
        }
        record.consumed_at = Some(consume.now);
        Ok(record.lease.clone())
    }

    fn purge_expired(&self, now: i64) -> std::result::Result<usize, ExchangeRejection> {
        let mut records = self
            .records
            .lock()
            .map_err(|_error| ExchangeRejection::StorageUnavailable)?;
        let before = records.len();
        records.retain(|_, record| record.consume_expires_at > now);
        Ok(before - records.len())
    }
}

impl fmt::Debug for InMemoryExchangeStore {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        let count = self.records.lock().map_or(0, |records| records.len());
        formatter
            .debug_struct("InMemoryExchangeStore")
            .field("max_entries", &self.max_entries)
            .field("record_count", &count)
            .field("durable", &false)
            .finish()
    }
}

pub(crate) fn token_hash(token: &str) -> std::result::Result<[u8; 32], ExchangeRejection> {
    if token.len() != EXCHANGE_TOKEN_CHARACTERS {
        return Err(ExchangeRejection::MalformedToken);
    }
    let decoded = URL_SAFE_NO_PAD
        .decode(token)
        .map_err(|_error| ExchangeRejection::MalformedToken)?;
    if decoded.len() != EXCHANGE_TOKEN_BYTES || URL_SAFE_NO_PAD.encode(&decoded) != token {
        return Err(ExchangeRejection::MalformedToken);
    }
    Ok(Sha256::digest(token.as_bytes()).into())
}

fn constant_time_equal(left: &[u8], right: &[u8]) -> bool {
    left.len() == right.len() && bool::from(left.ct_eq(right))
}
