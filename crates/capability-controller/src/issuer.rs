use std::collections::BTreeSet;
use std::fmt;

use base64::engine::general_purpose::URL_SAFE_NO_PAD;
use base64::Engine as _;
use ed25519_dalek::{Signer, SigningKey, VerifyingKey};
use media_capability::{KeyId, VerificationKeyring, PROTECTED_ALGORITHM, PROTECTED_TOKEN_TYPE};
use media_object::MediaCapabilityClaimsV1;
use serde::ser::SerializeStruct;
use serde::{Serialize, Serializer};

use crate::error::{ControllerError, ControllerErrorCode, ControllerStage, Result};

const MAX_PUBLISHED_KEYS: usize = 8;
const MINIMUM_ROTATION_OVERLAP_SECONDS: i64 = 95;
const REDACTED: &str = "[REDACTED]";

/// One old public key retained through the capability lifetime plus clock skew.
pub struct RetiringPublicKey {
    key_id: KeyId,
    public_key: VerifyingKey,
    accept_until: i64,
}

impl RetiringPublicKey {
    /// Validate an overlapping old public key.
    ///
    /// # Errors
    ///
    /// Returns an error unless the overlap is at least 90 seconds plus five
    /// seconds of permitted verifier skew.
    pub fn new(
        key_id: impl Into<String>,
        public_key: [u8; 32],
        rotation_started_at: i64,
        accept_until: i64,
    ) -> Result<Self> {
        if accept_until.saturating_sub(rotation_started_at) < MINIMUM_ROTATION_OVERLAP_SECONDS {
            return Err(ControllerError::new(
                ControllerErrorCode::InvalidControllerState,
                ControllerStage::CapabilityIssuance,
                "retiring key overlap is shorter than capability lifetime plus clock skew",
            ));
        }
        let key_id = KeyId::new(key_id).map_err(|_error| {
            ControllerError::new(
                ControllerErrorCode::InvalidControllerState,
                ControllerStage::CapabilityIssuance,
                "retiring key ID is invalid",
            )
        })?;
        let public_key = VerifyingKey::from_bytes(&public_key).map_err(|_error| {
            ControllerError::new(
                ControllerErrorCode::InvalidControllerState,
                ControllerStage::CapabilityIssuance,
                "retiring Ed25519 public key is invalid",
            )
        })?;
        Ok(Self {
            key_id,
            public_key,
            accept_until,
        })
    }

    /// Return the public key overlap deadline.
    #[must_use]
    pub const fn accept_until(&self) -> i64 {
        self.accept_until
    }
}

impl fmt::Debug for RetiringPublicKey {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter
            .debug_struct("RetiringPublicKey")
            .field("key_id", &REDACTED)
            .field("public_key", &REDACTED)
            .field("accept_until", &self.accept_until)
            .finish()
    }
}

/// Standard public Ed25519 JWK. Serialization is an intentional public boundary.
#[derive(Clone, Eq, PartialEq)]
pub struct JwkPublicKey {
    key_type: &'static str,
    curve: &'static str,
    key_use: &'static str,
    algorithm: &'static str,
    key_id: String,
    public_x: String,
}

impl JwkPublicKey {
    fn new(key_id: &KeyId, public_key: &VerifyingKey) -> Self {
        Self {
            key_type: "OKP",
            curve: "Ed25519",
            key_use: "sig",
            algorithm: PROTECTED_ALGORITHM,
            key_id: key_id.as_str().to_owned(),
            public_x: URL_SAFE_NO_PAD.encode(public_key.to_bytes()),
        }
    }

    /// Return the explicit public key ID for JWKS/verification configuration.
    #[must_use]
    pub fn key_id(&self) -> &str {
        &self.key_id
    }

    /// Return the unpadded base64url Ed25519 public key coordinate.
    #[must_use]
    pub fn public_x(&self) -> &str {
        &self.public_x
    }
}

impl Serialize for JwkPublicKey {
    fn serialize<S>(&self, serializer: S) -> std::result::Result<S::Ok, S::Error>
    where
        S: Serializer,
    {
        let mut state = serializer.serialize_struct("JwkPublicKey", 6)?;
        state.serialize_field("kty", self.key_type)?;
        state.serialize_field("crv", self.curve)?;
        state.serialize_field("use", self.key_use)?;
        state.serialize_field("alg", self.algorithm)?;
        state.serialize_field("kid", &self.key_id)?;
        state.serialize_field("x", &self.public_x)?;
        state.end()
    }
}

impl fmt::Debug for JwkPublicKey {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter
            .debug_struct("JwkPublicKey")
            .field("kty", &self.key_type)
            .field("crv", &self.curve)
            .field("use", &self.key_use)
            .field("alg", &self.algorithm)
            .field("key_id", &REDACTED)
            .field("public_x", &REDACTED)
            .finish()
    }
}

/// Deterministically ordered public-only JWKS rotation view.
#[derive(Clone, Eq, PartialEq, Serialize)]
pub struct JwksView {
    keys: Vec<JwkPublicKey>,
}

impl JwksView {
    /// Return the currently published public keys.
    #[must_use]
    pub fn keys(&self) -> &[JwkPublicKey] {
        &self.keys
    }
}

impl fmt::Debug for JwksView {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter
            .debug_struct("JwksView")
            .field("public_key_count", &self.keys.len())
            .finish()
    }
}

/// Online Ed25519 issuer with one active private key and public-only overlap keys.
pub struct Ed25519CapabilityIssuer {
    active_key_id: KeyId,
    signing_key: SigningKey,
    retiring_keys: Vec<RetiringPublicKey>,
}

impl Ed25519CapabilityIssuer {
    /// Construct an issuer from externally loaded key material.
    ///
    /// This crate deliberately has no environment/file key loader.
    ///
    /// # Errors
    ///
    /// Returns an error for invalid or duplicate key IDs or too many overlap keys.
    pub fn new(
        active_key_id: impl Into<String>,
        signing_key: SigningKey,
        mut retiring_keys: Vec<RetiringPublicKey>,
    ) -> Result<Self> {
        let active_key_id = KeyId::new(active_key_id).map_err(|_error| {
            ControllerError::new(
                ControllerErrorCode::InvalidControllerState,
                ControllerStage::CapabilityIssuance,
                "active signing key ID is invalid",
            )
        })?;
        if retiring_keys.len().saturating_add(1) > MAX_PUBLISHED_KEYS {
            return Err(ControllerError::new(
                ControllerErrorCode::InvalidControllerState,
                ControllerStage::CapabilityIssuance,
                "public verification key set exceeds its fixed bound",
            ));
        }
        retiring_keys.sort_by(|left, right| left.key_id.cmp(&right.key_id));
        let mut seen = BTreeSet::new();
        seen.insert(active_key_id.as_str());
        for key in &retiring_keys {
            if !seen.insert(key.key_id.as_str()) {
                return Err(ControllerError::new(
                    ControllerErrorCode::InvalidControllerState,
                    ControllerStage::CapabilityIssuance,
                    "public verification key IDs must be unique",
                ));
            }
        }
        Ok(Self {
            active_key_id,
            signing_key,
            retiring_keys,
        })
    }

    /// Sign canonical P00 claims using the exact protected header accepted by
    /// `media-capability`.
    ///
    /// # Errors
    ///
    /// Returns a stable error if canonical claims/header serialization fails.
    pub fn sign(&self, claims: &MediaCapabilityClaimsV1) -> Result<String> {
        #[derive(Serialize)]
        struct ProtectedHeader<'a> {
            alg: &'static str,
            kid: &'a KeyId,
            typ: &'static str,
        }

        let header = serde_json::to_vec(&ProtectedHeader {
            alg: PROTECTED_ALGORITHM,
            kid: &self.active_key_id,
            typ: PROTECTED_TOKEN_TYPE,
        })
        .map_err(|_error| signing_error())?;
        let claims = claims
            .to_canonical_json_vec()
            .map_err(|_error| signing_error())?;
        let protected_segment = URL_SAFE_NO_PAD.encode(header);
        let claims_segment = URL_SAFE_NO_PAD.encode(claims);
        let signing_input = format!("{protected_segment}.{claims_segment}");
        let signature = self.signing_key.sign(signing_input.as_bytes());
        Ok(format!(
            "{signing_input}.{}",
            URL_SAFE_NO_PAD.encode(signature.to_bytes())
        ))
    }

    /// Publish the active public key and still-valid overlap keys.
    #[must_use]
    pub fn jwks(&self, now: i64) -> JwksView {
        let mut keys = vec![JwkPublicKey::new(
            &self.active_key_id,
            &self.signing_key.verifying_key(),
        )];
        keys.extend(
            self.retiring_keys
                .iter()
                .filter(|key| now < key.accept_until)
                .map(|key| JwkPublicKey::new(&key.key_id, &key.public_key)),
        );
        keys.sort_by(|left, right| left.key_id.cmp(&right.key_id));
        JwksView { keys }
    }

    /// Build the public verifier keyring matching the current rotation view.
    ///
    /// # Errors
    ///
    /// Returns a stable error if public-key configuration cannot be represented.
    pub fn verification_keyring(&self, now: i64) -> Result<VerificationKeyring> {
        let mut keyring = VerificationKeyring::new();
        keyring
            .insert_active(
                self.active_key_id.as_str(),
                self.signing_key.verifying_key().to_bytes(),
            )
            .map_err(|_error| signing_error())?;
        for key in &self.retiring_keys {
            if now < key.accept_until {
                keyring
                    .insert_retiring(
                        key.key_id.as_str(),
                        key.public_key.to_bytes(),
                        key.accept_until,
                    )
                    .map_err(|_error| signing_error())?;
            }
        }
        Ok(keyring)
    }
}

impl fmt::Debug for Ed25519CapabilityIssuer {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter
            .debug_struct("Ed25519CapabilityIssuer")
            .field("active_key_id", &REDACTED)
            .field("signing_key", &REDACTED)
            .field("retiring_key_count", &self.retiring_keys.len())
            .finish()
    }
}

fn signing_error() -> ControllerError {
    ControllerError::new(
        ControllerErrorCode::SigningFailed,
        ControllerStage::CapabilityIssuance,
        "capability could not be signed with the configured Ed25519 issuer",
    )
}
