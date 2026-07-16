use std::fmt;

use base64::engine::general_purpose::URL_SAFE_NO_PAD;
use base64::Engine as _;
use hmac::{Hmac, Mac as _};
use media_object::{
    MediaAuthorizationFactV1, MediaAuthorizationRequestV1, SessionId, MEDIA_CONTROL_MAX_JSON_BYTES,
};
use sha2::{Digest, Sha256};
use zeroize::Zeroize;

use crate::entropy::EntropySource;
use crate::error::{
    ControllerError, ControllerErrorCode, ControllerErrorDetail, ControllerStage,
    IdentityBoundaryErrorCode, Result,
};

const REQUEST_SIGNATURE_VERSION: &str = "NEEDLETAIL-HMAC-SHA256-V1";
const RESPONSE_SIGNATURE_VERSION: &str = "NEEDLETAIL-HMAC-SHA256-RESPONSE-V1";
const MAX_RESPONSE_SKEW_SECONDS: i64 = 30;
const NONCE_BYTES: usize = 24;
const REDACTED: &str = "[REDACTED]";

type HmacSha256 = Hmac<Sha256>;

/// Value-free transport adapter failure.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct IdentityTransportError {
    reason: &'static str,
}

impl IdentityTransportError {
    /// Construct a bounded transport failure without a URL, body, or credential.
    #[must_use]
    pub const fn new(reason: &'static str) -> Self {
        Self { reason }
    }

    /// Return the bounded reason.
    #[must_use]
    pub const fn reason(&self) -> &'static str {
        self.reason
    }
}

impl fmt::Display for IdentityTransportError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(self.reason)
    }
}

impl std::error::Error for IdentityTransportError {}

/// Exact signed P01 request handed only to an internal transport adapter.
pub struct SignedIdentityRequest {
    path: String,
    body: Vec<u8>,
    service_id: String,
    audience: String,
    timestamp: i64,
    nonce: String,
    signature: String,
}

impl SignedIdentityRequest {
    #[must_use]
    pub fn path(&self) -> &str {
        &self.path
    }

    #[must_use]
    pub fn body(&self) -> &[u8] {
        &self.body
    }

    #[must_use]
    pub fn service_id(&self) -> &str {
        &self.service_id
    }

    #[must_use]
    pub fn audience(&self) -> &str {
        &self.audience
    }

    #[must_use]
    pub const fn timestamp(&self) -> i64 {
        self.timestamp
    }

    #[must_use]
    pub fn nonce(&self) -> &str {
        &self.nonce
    }

    #[must_use]
    pub fn signature(&self) -> &str {
        &self.signature
    }
}

impl fmt::Debug for SignedIdentityRequest {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter
            .debug_struct("SignedIdentityRequest")
            .field("path", &REDACTED)
            .field("body_bytes", &self.body.len())
            .field("service_id", &REDACTED)
            .field("audience", &REDACTED)
            .field("timestamp", &self.timestamp)
            .field("nonce", &REDACTED)
            .field("signature", &REDACTED)
            .finish()
    }
}

/// Signed P01 response returned by an internal transport adapter.
pub struct SignedIdentityResponse {
    status: u16,
    body: Vec<u8>,
    audience: String,
    timestamp: i64,
    request_nonce: String,
    signature: String,
}

impl SignedIdentityResponse {
    /// Construct a transport response. Authentication occurs in the client boundary.
    #[must_use]
    pub fn new(
        status: u16,
        body: Vec<u8>,
        audience: impl Into<String>,
        timestamp: i64,
        request_nonce: impl Into<String>,
        signature: impl Into<String>,
    ) -> Self {
        Self {
            status,
            body,
            audience: audience.into(),
            timestamp,
            request_nonce: request_nonce.into(),
            signature: signature.into(),
        }
    }

    #[must_use]
    pub const fn status(&self) -> u16 {
        self.status
    }

    #[must_use]
    pub fn body(&self) -> &[u8] {
        &self.body
    }
}

impl fmt::Debug for SignedIdentityResponse {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter
            .debug_struct("SignedIdentityResponse")
            .field("status", &self.status)
            .field("body_bytes", &self.body.len())
            .field("audience", &REDACTED)
            .field("timestamp", &self.timestamp)
            .field("request_nonce", &REDACTED)
            .field("signature", &REDACTED)
            .finish()
    }
}

/// Internal HTTP/service adapter. Implementations must not expose this as a
/// public browser endpoint.
pub trait IdentityAuthorizationTransport: Send + Sync {
    /// Send one fully signed request and return the exact response bytes/headers.
    ///
    /// # Errors
    ///
    /// Returns a value-free transport failure; HTTP policy responses are
    /// returned as [`SignedIdentityResponse`] and authenticated before use.
    fn send(
        &self,
        request: &SignedIdentityRequest,
    ) -> std::result::Result<SignedIdentityResponse, IdentityTransportError>;
}

/// Strict identity fact source required by the capability controller.
pub trait IdentityAuthorizationClient: Send + Sync {
    /// Re-evaluate one canonical P00 request for the exact session.
    ///
    /// # Errors
    ///
    /// Returns a stable redacted error if transport, authentication, policy,
    /// or strict fact parsing fails.
    fn authorize(
        &self,
        session_id: &SessionId,
        request: &MediaAuthorizationRequestV1,
        now: i64,
    ) -> Result<MediaAuthorizationFactV1>;
}

/// P01 HMAC request/response codec over an injected internal transport.
///
/// TLS/mTLS establishment remains an adapter/P24 responsibility; this type
/// enforces P01 message authentication, audience, nonce, timestamp, byte bound,
/// and strict P00 fact parsing before returning a value.
pub struct P01HmacIdentityClient<T, E> {
    transport: T,
    entropy: E,
    service_id: String,
    audience: String,
    hmac_key: [u8; 32],
}

impl<T, E> P01HmacIdentityClient<T, E> {
    /// Construct a signed client from externally supplied secret material.
    ///
    /// # Errors
    ///
    /// Returns an error for invalid service/audience identifiers.
    pub fn new(
        transport: T,
        entropy: E,
        service_id: impl Into<String>,
        audience: impl Into<String>,
        hmac_key: [u8; 32],
    ) -> Result<Self> {
        let service_id = service_id.into();
        let audience = audience.into();
        if !valid_authority_token(&service_id) || !valid_authority_token(&audience) {
            return Err(identity_error(
                ControllerErrorCode::InvalidControllerState,
                IdentityBoundaryErrorCode::InvalidConfiguration,
                "identity service authentication identifiers are invalid",
            ));
        }
        Ok(Self {
            transport,
            entropy,
            service_id,
            audience,
            hmac_key,
        })
    }
}

impl<T, E> IdentityAuthorizationClient for P01HmacIdentityClient<T, E>
where
    T: IdentityAuthorizationTransport,
    E: EntropySource,
{
    fn authorize(
        &self,
        session_id: &SessionId,
        request: &MediaAuthorizationRequestV1,
        now: i64,
    ) -> Result<MediaAuthorizationFactV1> {
        let body = request.to_canonical_json_vec().map_err(|error| {
            ControllerError::new(
                ControllerErrorCode::InvalidRequest,
                ControllerStage::RequestValidation,
                "identity authorization request is not canonical P00 media-control v1",
            )
            .with_detail(ControllerErrorDetail::MediaControl(error.code()))
        })?;
        if body.len() > MEDIA_CONTROL_MAX_JSON_BYTES {
            return Err(identity_error(
                ControllerErrorCode::InvalidRequest,
                IdentityBoundaryErrorCode::InvalidConfiguration,
                "identity authorization request exceeds the fixed byte bound",
            ));
        }
        let mut nonce_bytes = [0_u8; NONCE_BYTES];
        self.entropy
            .fill_bytes(&mut nonce_bytes)
            .map_err(|_error| {
                identity_error(
                    ControllerErrorCode::EntropyUnavailable,
                    IdentityBoundaryErrorCode::EntropyUnavailable,
                    "identity request nonce entropy is unavailable",
                )
            })?;
        let nonce = URL_SAFE_NO_PAD.encode(nonce_bytes);
        let path = format!(
            "/internal/v1/sessions/{}/media-authorization",
            session_id.as_str()
        );
        let signature = sign_request(
            &self.hmac_key,
            &self.service_id,
            &self.audience,
            now,
            &nonce,
            &path,
            &body,
        );
        let request = SignedIdentityRequest {
            path,
            body,
            service_id: self.service_id.clone(),
            audience: self.audience.clone(),
            timestamp: now,
            nonce,
            signature,
        };
        let response = self.transport.send(&request).map_err(|_error| {
            identity_error(
                ControllerErrorCode::IdentityUnavailable,
                IdentityBoundaryErrorCode::TransportUnavailable,
                "identity authorization transport is unavailable",
            )
        })?;
        self.authenticate_response(&request, &response, now)?;
        if response.status != 200 {
            return Err(identity_error(
                ControllerErrorCode::IdentityRejected,
                IdentityBoundaryErrorCode::RemoteRejected,
                "identity policy rejected the media authorization request",
            ));
        }
        MediaAuthorizationFactV1::from_json_slice(&response.body).map_err(|error| {
            ControllerError::new(
                ControllerErrorCode::IdentityResponseInvalid,
                ControllerStage::IdentityAuthorization,
                "authenticated identity response is not a strict P00 authorization fact",
            )
            .with_detail(ControllerErrorDetail::MediaControl(error.code()))
        })
    }
}

impl<T, E> P01HmacIdentityClient<T, E> {
    fn authenticate_response(
        &self,
        request: &SignedIdentityRequest,
        response: &SignedIdentityResponse,
        now: i64,
    ) -> Result<()> {
        if response.body.len() > MEDIA_CONTROL_MAX_JSON_BYTES {
            return Err(identity_error(
                ControllerErrorCode::IdentityResponseInvalid,
                IdentityBoundaryErrorCode::ResponseTooLarge,
                "identity response exceeds the fixed byte bound",
            ));
        }
        if response.audience != self.audience || response.request_nonce != request.nonce {
            return Err(identity_error(
                ControllerErrorCode::IdentityResponseInvalid,
                IdentityBoundaryErrorCode::ReplayedOrMismatchedResponse,
                "identity response audience or request nonce does not match",
            ));
        }
        if response.timestamp < 0
            || response.timestamp.abs_diff(now) > MAX_RESPONSE_SKEW_SECONDS as u64
        {
            return Err(identity_error(
                ControllerErrorCode::IdentityResponseInvalid,
                IdentityBoundaryErrorCode::StaleResponse,
                "identity response timestamp is outside the accepted window",
            ));
        }
        let signature = decode_canonical_signature(&response.signature)?;
        let canonical = canonical_response(
            &response.audience,
            response.timestamp,
            &response.request_nonce,
            response.status,
            &request.path,
            &response.body,
        );
        let mut mac = HmacSha256::new_from_slice(&self.hmac_key)
            .expect("a 32-byte key is valid for HMAC-SHA256");
        mac.update(canonical.as_bytes());
        mac.verify_slice(&signature).map_err(|_error| {
            identity_error(
                ControllerErrorCode::IdentityResponseInvalid,
                IdentityBoundaryErrorCode::ResponseAuthenticationFailed,
                "identity response HMAC authentication failed",
            )
        })
    }
}

impl<T, E> Drop for P01HmacIdentityClient<T, E> {
    fn drop(&mut self) {
        self.hmac_key.zeroize();
    }
}

impl<T, E> fmt::Debug for P01HmacIdentityClient<T, E> {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter
            .debug_struct("P01HmacIdentityClient")
            .field("transport", &REDACTED)
            .field("entropy", &REDACTED)
            .field("service_id", &REDACTED)
            .field("audience", &REDACTED)
            .field("hmac_key", &REDACTED)
            .finish()
    }
}

fn sign_request(
    key: &[u8; 32],
    service_id: &str,
    audience: &str,
    timestamp: i64,
    nonce: &str,
    path: &str,
    body: &[u8],
) -> String {
    let canonical = format!(
        "{REQUEST_SIGNATURE_VERSION}\n{service_id}\n{audience}\n{timestamp}\n{nonce}\nPOST\n{path}\n{}",
        sha256_base64url(body)
    );
    let mut mac = HmacSha256::new_from_slice(key).expect("a 32-byte key is valid for HMAC-SHA256");
    mac.update(canonical.as_bytes());
    URL_SAFE_NO_PAD.encode(mac.finalize().into_bytes())
}

fn canonical_response(
    audience: &str,
    timestamp: i64,
    nonce: &str,
    status: u16,
    path: &str,
    body: &[u8],
) -> String {
    format!(
        "{RESPONSE_SIGNATURE_VERSION}\n{audience}\n{timestamp}\n{nonce}\n{status}\n{path}\n{}",
        sha256_base64url(body)
    )
}

fn sha256_base64url(bytes: &[u8]) -> String {
    URL_SAFE_NO_PAD.encode(Sha256::digest(bytes))
}

fn decode_canonical_signature(value: &str) -> Result<Vec<u8>> {
    let decoded = URL_SAFE_NO_PAD.decode(value).map_err(|_error| {
        identity_error(
            ControllerErrorCode::IdentityResponseInvalid,
            IdentityBoundaryErrorCode::ResponseAuthenticationFailed,
            "identity response signature is not canonical base64url",
        )
    })?;
    if decoded.len() != 32 || URL_SAFE_NO_PAD.encode(&decoded) != value {
        return Err(identity_error(
            ControllerErrorCode::IdentityResponseInvalid,
            IdentityBoundaryErrorCode::ResponseAuthenticationFailed,
            "identity response signature is not canonical HMAC-SHA256",
        ));
    }
    Ok(decoded)
}

fn valid_authority_token(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 128
        && value.is_ascii()
        && value
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'_' | b':' | b'-'))
}

fn identity_error(
    code: ControllerErrorCode,
    detail: IdentityBoundaryErrorCode,
    reason: &'static str,
) -> ControllerError {
    ControllerError::new(code, ControllerStage::IdentityAuthorization, reason)
        .with_detail(ControllerErrorDetail::Identity(detail))
}
