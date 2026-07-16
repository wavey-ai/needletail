//! Strict verification and authorization for Needletail media-capability v1.
//!
//! The only high-level admission API, [`MediaCapabilityVerifier::authorize`],
//! requires a caller-owned [`ReplayAdmissionGuard`]. This crate contains public
//! verification keys only. Capability signing and all private-key custody stay
//! in the issuer boundary.

use std::collections::btree_map::Entry;
use std::collections::BTreeMap;
use std::fmt;

use base64::engine::general_purpose::URL_SAFE_NO_PAD;
use base64::Engine as _;
use ed25519_dalek::{Signature, VerifyingKey};
pub use media_object::{
    AudienceId, CapabilityId, ContributorId, EdgeId, EndpointId, MediaCapabilityClaimsV1,
    MediaClass, Operation, ParticipantId, SessionId, SourceId, TakeId, TenantId,
};
use media_object::{
    MediaCapabilityValidationContextV1, MediaControlError, MediaControlErrorCode,
    MEDIA_CONTROL_MAX_CLOCK_SKEW_SECONDS, MEDIA_CONTROL_MAX_JSON_BYTES,
};
use serde::{de, Deserialize, Deserializer, Serialize};

/// Required protected `alg` value for every v1 capability.
pub const PROTECTED_ALGORITHM: &str = "EdDSA";
/// Required protected `typ` value for every v1 capability.
pub const PROTECTED_TOKEN_TYPE: &str = "needletail-media-capability+jwt";
/// Maximum encoded size of a protected header segment.
pub const MAX_PROTECTED_HEADER_SEGMENT_BYTES: usize = 1_368;
/// Maximum encoded size of the bounded media-control payload segment.
pub const MAX_CLAIMS_SEGMENT_BYTES: usize = MEDIA_CONTROL_MAX_JSON_BYTES.div_ceil(3) * 4;
/// Maximum total byte length of one compact capability.
pub const MAX_COMPACT_JWS_BYTES: usize = MAX_PROTECTED_HEADER_SEGMENT_BYTES
    + MAX_CLAIMS_SEGMENT_BYTES
    + ED25519_SIGNATURE_SEGMENT_BYTES
    + 2;

const MAX_KEY_ID_BYTES: usize = 128;
const MAX_PROTECTED_HEADER_JSON_BYTES: usize = 1_024;
const ED25519_SIGNATURE_BYTES: usize = 64;
const ED25519_SIGNATURE_SEGMENT_BYTES: usize = 86;
const MAX_EXPECTED_AUTHORITY_BYTES: usize = 512;
const MAX_EXACT_UNIX_SECONDS: i64 = 9_007_199_254_740_991;
const REDACTED: &str = "[REDACTED]";

/// Stable verifier failure categories suitable for metrics and conformance tests.
#[derive(Clone, Copy, Debug, Deserialize, Eq, Hash, Ord, PartialEq, PartialOrd, Serialize)]
#[serde(rename_all = "snake_case")]
#[non_exhaustive]
pub enum CapabilityVerifierErrorCode {
    MalformedCompactJws,
    SegmentTooLarge,
    NonCanonicalBase64Url,
    InvalidProtectedHeader,
    InvalidConfiguration,
    InvalidKeyId,
    InvalidVerificationKey,
    DuplicateKey,
    UnknownKey,
    KeyNotAccepted,
    InvalidSignature,
    ClaimsRejected,
    NonCanonicalClaims,
    AuthorizationRejected,
    ReplayAdmissionRejected,
}

/// Stable rejection reasons supplied by an atomic replay/admission boundary.
#[derive(Clone, Copy, Debug, Deserialize, Eq, Hash, Ord, PartialEq, PartialOrd, Serialize)]
#[serde(rename_all = "snake_case")]
#[non_exhaustive]
pub enum ReplayAdmissionRejection {
    Replay,
    Capacity,
    Policy,
    Unavailable,
}

/// A bounded, value-free verifier failure.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct CapabilityVerifierError {
    code: CapabilityVerifierErrorCode,
    field: &'static str,
    reason: &'static str,
    claims_code: Option<MediaControlErrorCode>,
    replay_rejection: Option<ReplayAdmissionRejection>,
}

impl CapabilityVerifierError {
    const fn new(
        code: CapabilityVerifierErrorCode,
        field: &'static str,
        reason: &'static str,
    ) -> Self {
        Self {
            code,
            field,
            reason,
            claims_code: None,
            replay_rejection: None,
        }
    }

    fn claims(
        code: CapabilityVerifierErrorCode,
        error: &MediaControlError,
    ) -> CapabilityVerifierError {
        Self {
            code,
            field: error.field(),
            reason: error.reason(),
            claims_code: Some(error.code()),
            replay_rejection: None,
        }
    }

    const fn replay(rejection: ReplayAdmissionRejection) -> Self {
        Self {
            code: CapabilityVerifierErrorCode::ReplayAdmissionRejected,
            field: "replay_admission_guard",
            reason: "the caller-owned replay/admission guard rejected the capability",
            claims_code: None,
            replay_rejection: Some(rejection),
        }
    }

    /// Return the stable top-level verifier error category.
    #[must_use]
    pub const fn code(&self) -> CapabilityVerifierErrorCode {
        self.code
    }

    /// Return the contract field or verification stage that failed.
    #[must_use]
    pub const fn field(&self) -> &'static str {
        self.field
    }

    /// Return a bounded diagnostic that never includes token material.
    #[must_use]
    pub const fn reason(&self) -> &'static str {
        self.reason
    }

    /// Return the underlying media-control classification when claims failed.
    #[must_use]
    pub const fn claims_code(&self) -> Option<MediaControlErrorCode> {
        self.claims_code
    }

    /// Return the caller guard's stable rejection reason, when applicable.
    #[must_use]
    pub const fn replay_rejection(&self) -> Option<ReplayAdmissionRejection> {
        self.replay_rejection
    }
}

impl fmt::Display for CapabilityVerifierError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(formatter, "{}: {}", self.field, self.reason)
    }
}

impl std::error::Error for CapabilityVerifierError {}

/// Result alias for verifier configuration, keyring, and authorization calls.
pub type Result<T> = std::result::Result<T, CapabilityVerifierError>;

/// A bounded opaque identifier selecting a public verification key.
#[derive(Clone, Eq, Hash, Ord, PartialEq, PartialOrd, Serialize)]
#[serde(transparent)]
pub struct KeyId(String);

impl KeyId {
    /// Validate and construct a key ID.
    ///
    /// # Errors
    ///
    /// Returns an error for an empty, oversized, or non-token value.
    pub fn new(value: impl Into<String>) -> Result<Self> {
        let value = value.into();
        validate_key_id(&value)?;
        Ok(Self(value))
    }

    /// Return the exact key ID for explicit key-management operations.
    #[must_use]
    pub fn as_str(&self) -> &str {
        &self.0
    }
}

impl fmt::Debug for KeyId {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.debug_tuple("KeyId").field(&REDACTED).finish()
    }
}

impl<'de> Deserialize<'de> for KeyId {
    fn deserialize<D>(deserializer: D) -> std::result::Result<Self, D::Error>
    where
        D: Deserializer<'de>,
    {
        let value = String::deserialize(deserializer)?;
        Self::new(value).map_err(de::Error::custom)
    }
}

/// Verification-key lifecycle state used during overlap rotation.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum VerificationKeyState {
    /// The current online verification key.
    Active,
    /// A previous key accepted only until a fixed Unix-second deadline.
    Retiring { accept_until: i64 },
}

struct VerificationKeyEntry {
    key: VerifyingKey,
    state: VerificationKeyState,
}

/// Public-only verification keys indexed by protected-header `kid`.
#[derive(Default)]
pub struct VerificationKeyring {
    entries: BTreeMap<KeyId, VerificationKeyEntry>,
}

impl VerificationKeyring {
    /// Construct an empty keyring.
    #[must_use]
    pub const fn new() -> Self {
        Self {
            entries: BTreeMap::new(),
        }
    }

    /// Insert one active Ed25519 public key.
    ///
    /// # Errors
    ///
    /// Returns an error for an invalid/duplicate key ID or invalid public key.
    pub fn insert_active(&mut self, key_id: impl Into<String>, public_key: [u8; 32]) -> Result<()> {
        self.insert(key_id, public_key, VerificationKeyState::Active)
    }

    /// Insert a retiring public key accepted during an explicit overlap window.
    ///
    /// # Errors
    ///
    /// Returns an error for an invalid deadline, key ID, duplicate, or key.
    pub fn insert_retiring(
        &mut self,
        key_id: impl Into<String>,
        public_key: [u8; 32],
        accept_until: i64,
    ) -> Result<()> {
        if !(1..=MAX_EXACT_UNIX_SECONDS).contains(&accept_until) {
            return Err(CapabilityVerifierError::new(
                CapabilityVerifierErrorCode::InvalidConfiguration,
                "accept_until",
                "retiring-key deadline must be a positive exact Unix second",
            ));
        }
        self.insert(
            key_id,
            public_key,
            VerificationKeyState::Retiring { accept_until },
        )
    }

    /// Return the number of configured public verification keys.
    #[must_use]
    pub fn len(&self) -> usize {
        self.entries.len()
    }

    /// Return whether the keyring contains no verification keys.
    #[must_use]
    pub fn is_empty(&self) -> bool {
        self.entries.is_empty()
    }

    fn insert(
        &mut self,
        key_id: impl Into<String>,
        public_key: [u8; 32],
        state: VerificationKeyState,
    ) -> Result<()> {
        let key_id = KeyId::new(key_id)?;
        let key = VerifyingKey::from_bytes(&public_key).map_err(|_error| {
            CapabilityVerifierError::new(
                CapabilityVerifierErrorCode::InvalidVerificationKey,
                "public_key",
                "bytes are not a valid Ed25519 public key",
            )
        })?;
        match self.entries.entry(key_id) {
            Entry::Vacant(entry) => {
                entry.insert(VerificationKeyEntry { key, state });
                Ok(())
            }
            Entry::Occupied(_) => Err(CapabilityVerifierError::new(
                CapabilityVerifierErrorCode::DuplicateKey,
                "kid",
                "verification key ID is already configured",
            )),
        }
    }

    fn accepted(&self, key_id: &KeyId, now: i64) -> Result<&VerificationKeyEntry> {
        let entry = self.entries.get(key_id).ok_or_else(|| {
            CapabilityVerifierError::new(
                CapabilityVerifierErrorCode::UnknownKey,
                "kid",
                "protected key ID is not in the configured verification keyring",
            )
        })?;
        if let VerificationKeyState::Retiring { accept_until } = entry.state {
            if now >= accept_until {
                return Err(CapabilityVerifierError::new(
                    CapabilityVerifierErrorCode::KeyNotAccepted,
                    "kid",
                    "retiring verification key is outside its overlap window",
                ));
            }
        }
        Ok(entry)
    }
}

impl fmt::Debug for VerificationKeyring {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        let active = self
            .entries
            .values()
            .filter(|entry| entry.state == VerificationKeyState::Active)
            .count();
        let retiring = self.entries.len() - active;
        formatter
            .debug_struct("VerificationKeyring")
            .field("active_keys", &active)
            .field("retiring_keys", &retiring)
            .finish()
    }
}

/// Exact current state against which a verified capability is authorized.
#[derive(Clone, Copy)]
pub struct CurrentMediaAuthorizationContextV1<'a> {
    pub tenant_id: &'a TenantId,
    pub session_id: &'a SessionId,
    pub session_epoch: u64,
    pub media_authorization_epoch: u64,
    pub subject_grant_epoch: u64,
    pub media_policy_version: u64,
    pub class_authorization_epoch: Option<u64>,
    pub binding_generation: u64,
    pub topology_generation: u64,
    pub participant_id: &'a ParticipantId,
    pub endpoint_id: &'a EndpointId,
    pub contributor_id: Option<&'a ContributorId>,
    pub operation: Operation,
    pub media_class: MediaClass,
    pub source_id: Option<&'a SourceId>,
    pub audience_id: Option<&'a AudienceId>,
    pub take_id: Option<&'a TakeId>,
    pub edge_id: &'a EdgeId,
    pub now: i64,
    pub clock_skew_seconds: i64,
}

impl CurrentMediaAuthorizationContextV1<'_> {
    fn claims_context<'a>(
        &'a self,
        expected_issuer: &'a str,
        expected_audience: &'a str,
    ) -> MediaCapabilityValidationContextV1<'a> {
        MediaCapabilityValidationContextV1 {
            expected_issuer,
            expected_audience,
            tenant_id: self.tenant_id,
            session_id: self.session_id,
            session_epoch: self.session_epoch,
            media_authorization_epoch: self.media_authorization_epoch,
            subject_grant_epoch: self.subject_grant_epoch,
            media_policy_version: self.media_policy_version,
            class_authorization_epoch: self.class_authorization_epoch,
            binding_generation: self.binding_generation,
            topology_generation: self.topology_generation,
            participant_id: self.participant_id,
            endpoint_id: self.endpoint_id,
            contributor_id: self.contributor_id,
            operation: self.operation,
            media_class: self.media_class,
            source_id: self.source_id,
            audience_id: self.audience_id,
            take_id: self.take_id,
            edge_id: Some(self.edge_id),
            now: self.now,
            clock_skew_seconds: self.clock_skew_seconds,
        }
    }
}

/// Minimal capability facts presented to an atomic replay/admission guard.
#[derive(Clone, Copy)]
pub struct ReplayAdmissionV1<'a> {
    pub capability_id: &'a CapabilityId,
    pub session_id: &'a SessionId,
    pub endpoint_id: &'a EndpointId,
    pub edge_id: &'a EdgeId,
    pub operation: Operation,
    pub expires_at: i64,
}

/// Caller-owned atomic replay and resource-admission boundary.
pub trait ReplayAdmissionGuard {
    /// Atomically reject a replay/capacity/policy conflict or record admission.
    ///
    /// # Errors
    ///
    /// Returns a stable reason when the capability must not be admitted.
    fn check_and_admit(
        &mut self,
        admission: ReplayAdmissionV1<'_>,
    ) -> std::result::Result<(), ReplayAdmissionRejection>;
}

/// Successfully verified, context-authorized, and guard-admitted capability.
pub struct AuthorizedMediaCapability {
    claims: MediaCapabilityClaimsV1,
    key_id: KeyId,
    key_state: VerificationKeyState,
}

impl AuthorizedMediaCapability {
    /// Return the validated immutable claims.
    #[must_use]
    pub const fn claims(&self) -> &MediaCapabilityClaimsV1 {
        &self.claims
    }

    /// Return the selected key ID for explicit audit correlation.
    #[must_use]
    pub const fn key_id(&self) -> &KeyId {
        &self.key_id
    }

    /// Return whether the signature used an active or overlapping retiring key.
    #[must_use]
    pub const fn key_state(&self) -> VerificationKeyState {
        self.key_state
    }

    /// Consume the authorization and return its validated claims.
    #[must_use]
    pub fn into_claims(self) -> MediaCapabilityClaimsV1 {
        self.claims
    }
}

impl fmt::Debug for AuthorizedMediaCapability {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter
            .debug_struct("AuthorizedMediaCapability")
            .field("claims", &self.claims)
            .field("key_id", &self.key_id)
            .field("key_state", &self.key_state)
            .finish()
    }
}

/// Strict public-key verifier with pinned issuer and audience.
pub struct MediaCapabilityVerifier {
    keyring: VerificationKeyring,
    expected_issuer: String,
    expected_audience: String,
}

impl MediaCapabilityVerifier {
    /// Construct a verifier from public keys and pinned authority values.
    ///
    /// # Errors
    ///
    /// Returns an error for an empty keyring or invalid issuer/audience.
    pub fn new(
        keyring: VerificationKeyring,
        expected_issuer: impl Into<String>,
        expected_audience: impl Into<String>,
    ) -> Result<Self> {
        if keyring.is_empty() {
            return Err(CapabilityVerifierError::new(
                CapabilityVerifierErrorCode::InvalidConfiguration,
                "keyring",
                "at least one public verification key is required",
            ));
        }
        let expected_issuer = expected_issuer.into();
        let expected_audience = expected_audience.into();
        validate_expected_authority("expected_issuer", &expected_issuer)?;
        validate_expected_authority("expected_audience", &expected_audience)?;
        Ok(Self {
            keyring,
            expected_issuer,
            expected_audience,
        })
    }

    /// Verify, authorize, and atomically admit one compact capability.
    ///
    /// Signature verification happens before the claims payload is decoded or
    /// parsed. No successful value is returned until the caller's replay and
    /// resource-admission guard has accepted the exact capability.
    ///
    /// # Errors
    ///
    /// Returns a stable error for compact encoding, header, key, signature,
    /// claims, current-context, or replay/admission failure.
    pub fn authorize<G: ReplayAdmissionGuard + ?Sized>(
        &self,
        compact_jws: &str,
        context: &CurrentMediaAuthorizationContextV1<'_>,
        guard: &mut G,
    ) -> Result<AuthorizedMediaCapability> {
        validate_context_time(context)?;
        let verified = self.verify_signature_and_claims(compact_jws, context.now)?;
        let claims_context = context.claims_context(&self.expected_issuer, &self.expected_audience);
        verified
            .claims
            .authorize(&claims_context)
            .map_err(|error| {
                CapabilityVerifierError::claims(
                    CapabilityVerifierErrorCode::AuthorizationRejected,
                    &error,
                )
            })?;

        guard
            .check_and_admit(ReplayAdmissionV1 {
                capability_id: verified.claims.capability_id(),
                session_id: verified.claims.session_id(),
                endpoint_id: verified.claims.endpoint_id(),
                edge_id: context.edge_id,
                operation: verified.claims.operation(),
                expires_at: verified.claims.expires_at(),
            })
            .map_err(CapabilityVerifierError::replay)?;

        Ok(AuthorizedMediaCapability {
            claims: verified.claims,
            key_id: verified.key_id,
            key_state: verified.key_state,
        })
    }

    fn verify_signature_and_claims(&self, compact_jws: &str, now: i64) -> Result<VerifiedClaims> {
        let parts = CompactParts::parse(compact_jws)?;
        validate_segment_syntax(
            "protected_header",
            parts.protected_header,
            MAX_PROTECTED_HEADER_SEGMENT_BYTES,
        )?;
        validate_segment_syntax("claims", parts.claims, MAX_CLAIMS_SEGMENT_BYTES)?;
        if parts.signature.len() != ED25519_SIGNATURE_SEGMENT_BYTES {
            return Err(CapabilityVerifierError::new(
                CapabilityVerifierErrorCode::NonCanonicalBase64Url,
                "signature",
                "Ed25519 signature segment must be exactly 86 unpadded characters",
            ));
        }

        let protected_bytes = decode_canonical_segment(
            "protected_header",
            parts.protected_header,
            MAX_PROTECTED_HEADER_JSON_BYTES,
        )?;
        let protected_header = parse_protected_header(&protected_bytes)?;
        let entry = self.keyring.accepted(&protected_header.kid, now)?;
        let signature_bytes =
            decode_canonical_segment("signature", parts.signature, ED25519_SIGNATURE_BYTES)?;
        let signature = Signature::from_slice(&signature_bytes).map_err(|_error| {
            CapabilityVerifierError::new(
                CapabilityVerifierErrorCode::InvalidSignature,
                "signature",
                "signature is not a 64-byte Ed25519 value",
            )
        })?;

        let signing_input = parts.signing_input();
        entry
            .key
            .verify_strict(&signing_input, &signature)
            .map_err(|_error| {
                CapabilityVerifierError::new(
                    CapabilityVerifierErrorCode::InvalidSignature,
                    "signature",
                    "Ed25519 signature verification failed",
                )
            })?;

        // Signature authentication intentionally precedes payload decoding and
        // all claims parsing.
        let claims_bytes =
            decode_canonical_segment("claims", parts.claims, MEDIA_CONTROL_MAX_JSON_BYTES)?;
        let claims = MediaCapabilityClaimsV1::from_json_slice(&claims_bytes).map_err(|error| {
            CapabilityVerifierError::claims(CapabilityVerifierErrorCode::ClaimsRejected, &error)
        })?;
        let canonical_claims = claims.to_canonical_json_vec().map_err(|error| {
            CapabilityVerifierError::claims(CapabilityVerifierErrorCode::ClaimsRejected, &error)
        })?;
        if canonical_claims != claims_bytes {
            return Err(CapabilityVerifierError::new(
                CapabilityVerifierErrorCode::NonCanonicalClaims,
                "claims",
                "signed claims are not the canonical media-control v1 JSON bytes",
            ));
        }

        Ok(VerifiedClaims {
            claims,
            key_id: protected_header.kid,
            key_state: entry.state,
        })
    }
}

impl fmt::Debug for MediaCapabilityVerifier {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter
            .debug_struct("MediaCapabilityVerifier")
            .field("keyring", &self.keyring)
            .field("expected_issuer", &REDACTED)
            .field("expected_audience", &REDACTED)
            .finish()
    }
}

struct VerifiedClaims {
    claims: MediaCapabilityClaimsV1,
    key_id: KeyId,
    key_state: VerificationKeyState,
}

#[derive(Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct ProtectedHeader {
    alg: String,
    kid: KeyId,
    typ: String,
}

fn parse_protected_header(input: &[u8]) -> Result<ProtectedHeader> {
    let header: ProtectedHeader = serde_json::from_slice(input).map_err(|_error| {
        CapabilityVerifierError::new(
            CapabilityVerifierErrorCode::InvalidProtectedHeader,
            "protected_header",
            "protected header is not the closed v1 JSON object",
        )
    })?;
    if header.alg != PROTECTED_ALGORITHM {
        return Err(CapabilityVerifierError::new(
            CapabilityVerifierErrorCode::InvalidProtectedHeader,
            "alg",
            "protected algorithm must be exactly EdDSA",
        ));
    }
    if header.typ != PROTECTED_TOKEN_TYPE {
        return Err(CapabilityVerifierError::new(
            CapabilityVerifierErrorCode::InvalidProtectedHeader,
            "typ",
            "protected token type is not media-capability v1",
        ));
    }
    let canonical = serde_json::to_vec(&header).map_err(|_error| {
        CapabilityVerifierError::new(
            CapabilityVerifierErrorCode::InvalidProtectedHeader,
            "protected_header",
            "protected header could not be canonically serialized",
        )
    })?;
    if canonical != input {
        return Err(CapabilityVerifierError::new(
            CapabilityVerifierErrorCode::InvalidProtectedHeader,
            "protected_header",
            "protected header must use compact declaration-order JSON",
        ));
    }
    Ok(header)
}

struct CompactParts<'a> {
    protected_header: &'a str,
    claims: &'a str,
    signature: &'a str,
}

impl<'a> CompactParts<'a> {
    fn parse(compact: &'a str) -> Result<Self> {
        if compact.len() > MAX_COMPACT_JWS_BYTES {
            return Err(CapabilityVerifierError::new(
                CapabilityVerifierErrorCode::SegmentTooLarge,
                "compact_jws",
                "compact capability exceeds its fixed total bound",
            ));
        }
        let mut segments = compact.split('.');
        let protected_header = segments.next().unwrap_or_default();
        let claims = segments.next().unwrap_or_default();
        let signature = segments.next().unwrap_or_default();
        if protected_header.is_empty()
            || claims.is_empty()
            || signature.is_empty()
            || segments.next().is_some()
        {
            return Err(CapabilityVerifierError::new(
                CapabilityVerifierErrorCode::MalformedCompactJws,
                "compact_jws",
                "capability must contain exactly three nonempty compact JWS segments",
            ));
        }
        Ok(Self {
            protected_header,
            claims,
            signature,
        })
    }

    fn signing_input(&self) -> Vec<u8> {
        let mut input = Vec::with_capacity(self.protected_header.len() + self.claims.len() + 1);
        input.extend_from_slice(self.protected_header.as_bytes());
        input.push(b'.');
        input.extend_from_slice(self.claims.as_bytes());
        input
    }
}

fn validate_segment_syntax(field: &'static str, segment: &str, maximum: usize) -> Result<()> {
    if segment.len() > maximum {
        return Err(CapabilityVerifierError::new(
            CapabilityVerifierErrorCode::SegmentTooLarge,
            field,
            "compact JWS segment exceeds its fixed encoded bound",
        ));
    }
    if segment.len() % 4 == 1
        || !segment
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'_' | b'-'))
    {
        return Err(CapabilityVerifierError::new(
            CapabilityVerifierErrorCode::NonCanonicalBase64Url,
            field,
            "segment must use strict unpadded base64url",
        ));
    }
    Ok(())
}

fn decode_canonical_segment(
    field: &'static str,
    segment: &str,
    maximum_decoded: usize,
) -> Result<Vec<u8>> {
    let decoded = URL_SAFE_NO_PAD.decode(segment).map_err(|_error| {
        CapabilityVerifierError::new(
            CapabilityVerifierErrorCode::NonCanonicalBase64Url,
            field,
            "segment is not canonical unpadded base64url",
        )
    })?;
    if decoded.len() > maximum_decoded {
        return Err(CapabilityVerifierError::new(
            CapabilityVerifierErrorCode::SegmentTooLarge,
            field,
            "decoded compact JWS segment exceeds its fixed bound",
        ));
    }
    if URL_SAFE_NO_PAD.encode(&decoded) != segment {
        return Err(CapabilityVerifierError::new(
            CapabilityVerifierErrorCode::NonCanonicalBase64Url,
            field,
            "segment has a non-canonical base64url representation",
        ));
    }
    Ok(decoded)
}

fn validate_key_id(value: &str) -> Result<()> {
    if value.is_empty() || value.len() > MAX_KEY_ID_BYTES {
        return Err(CapabilityVerifierError::new(
            CapabilityVerifierErrorCode::InvalidKeyId,
            "kid",
            "key ID must contain between one and 128 bytes",
        ));
    }
    let mut bytes = value.bytes();
    let first = bytes
        .next()
        .is_some_and(|byte| byte.is_ascii_alphanumeric());
    let rest =
        bytes.all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'_' | b':' | b'-'));
    if !first || !rest {
        return Err(CapabilityVerifierError::new(
            CapabilityVerifierErrorCode::InvalidKeyId,
            "kid",
            "key ID must use the v1 opaque ASCII token alphabet",
        ));
    }
    Ok(())
}

fn validate_expected_authority(field: &'static str, value: &str) -> Result<()> {
    if value.is_empty()
        || value.len() > MAX_EXPECTED_AUTHORITY_BYTES
        || !value.is_ascii()
        || value.bytes().any(|byte| !byte.is_ascii_graphic())
    {
        return Err(CapabilityVerifierError::new(
            CapabilityVerifierErrorCode::InvalidConfiguration,
            field,
            "expected authority must be bounded visible ASCII",
        ));
    }
    Ok(())
}

fn validate_context_time(context: &CurrentMediaAuthorizationContextV1<'_>) -> Result<()> {
    if !(0..=MAX_EXACT_UNIX_SECONDS).contains(&context.now) {
        return Err(CapabilityVerifierError::new(
            CapabilityVerifierErrorCode::InvalidConfiguration,
            "now",
            "current time must be a non-negative exact Unix second",
        ));
    }
    if context.clock_skew_seconds < 0
        || context.clock_skew_seconds > MEDIA_CONTROL_MAX_CLOCK_SKEW_SECONDS
    {
        return Err(CapabilityVerifierError::new(
            CapabilityVerifierErrorCode::InvalidConfiguration,
            "clock_skew_seconds",
            "clock skew must be between zero and five seconds",
        ));
    }
    Ok(())
}
