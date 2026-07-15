use std::fmt;

use media_object::MediaControlErrorCode;
use serde::{Deserialize, Serialize};

/// Stable controller stage suitable for bounded telemetry and API errors.
#[derive(Clone, Copy, Debug, Deserialize, Eq, Hash, Ord, PartialEq, PartialOrd, Serialize)]
#[serde(rename_all = "snake_case")]
#[non_exhaustive]
pub enum ControllerStage {
    FeatureGate,
    CallerAuthentication,
    RequestValidation,
    IdentityAuthorization,
    RouteAdmission,
    CapabilityIssuance,
    BrowserExchange,
}

/// Stable top-level P02 failure categories. No variant carries private values.
#[derive(Clone, Copy, Debug, Deserialize, Eq, Hash, Ord, PartialEq, PartialOrd, Serialize)]
#[serde(rename_all = "snake_case")]
#[non_exhaustive]
pub enum ControllerErrorCode {
    FeatureDisabled,
    CallerNotAllowed,
    InvalidRequest,
    IdentityUnavailable,
    IdentityRejected,
    IdentityResponseInvalid,
    AuthorizationMismatch,
    AuthorizationExpired,
    RouteRefused,
    InvalidControllerState,
    SigningFailed,
    EntropyUnavailable,
    ExchangeUnavailable,
    ExchangeRejected,
}

/// Stable signed P01 client-boundary failures.
#[derive(Clone, Copy, Debug, Deserialize, Eq, Hash, Ord, PartialEq, PartialOrd, Serialize)]
#[serde(rename_all = "snake_case")]
#[non_exhaustive]
pub enum IdentityBoundaryErrorCode {
    InvalidConfiguration,
    EntropyUnavailable,
    TransportUnavailable,
    ResponseTooLarge,
    ResponseAuthenticationFailed,
    StaleResponse,
    ReplayedOrMismatchedResponse,
    RemoteRejected,
    MalformedAuthorizationFact,
}

/// Stable route refusal reasons. Candidate identifiers are intentionally absent.
#[derive(Clone, Copy, Debug, Deserialize, Eq, Hash, Ord, PartialEq, PartialOrd, Serialize)]
#[serde(rename_all = "snake_case")]
#[non_exhaustive]
pub enum RouteRefusalReason {
    NoCandidates,
    DesiredStateInactive,
    GenerationMismatch,
    LeaseUnhealthy,
    ProbeStale,
    DeadlineUnsupported,
    MediaClassUnsupported,
    ScopeUnavailable,
    TransportUnsupported,
    CodecUnsupported,
    CapacityExceeded,
    SessionCapacityExceeded,
    CostPolicyRefused,
    IndependentRepairUnavailable,
    InvalidDescriptor,
    NoQualifiedRoute,
}

/// Stable one-use exchange failure reasons.
#[derive(Clone, Copy, Debug, Deserialize, Eq, Hash, Ord, PartialEq, PartialOrd, Serialize)]
#[serde(rename_all = "snake_case")]
#[non_exhaustive]
pub enum ExchangeRejection {
    MalformedToken,
    DuplicateToken,
    NotFound,
    Expired,
    AlreadyConsumed,
    WrongEdge,
    WrongEndpoint,
    WrongEndpointProof,
    StorageUnavailable,
}

/// Optional bounded detail attached to a controller error.
#[derive(Clone, Copy, Debug, Deserialize, Eq, Hash, Ord, PartialEq, PartialOrd, Serialize)]
#[serde(tag = "kind", content = "code", rename_all = "snake_case")]
#[non_exhaustive]
pub enum ControllerErrorDetail {
    MediaControl(MediaControlErrorCode),
    Identity(IdentityBoundaryErrorCode),
    Route(RouteRefusalReason),
    Exchange(ExchangeRejection),
}

/// A value-free failure safe for routine logs, metrics, and API error bodies.
#[derive(Clone, Eq, PartialEq)]
pub struct ControllerError {
    code: ControllerErrorCode,
    stage: ControllerStage,
    reason: &'static str,
    detail: Option<ControllerErrorDetail>,
}

impl ControllerError {
    pub(crate) const fn new(
        code: ControllerErrorCode,
        stage: ControllerStage,
        reason: &'static str,
    ) -> Self {
        Self {
            code,
            stage,
            reason,
            detail: None,
        }
    }

    pub(crate) const fn with_detail(mut self, detail: ControllerErrorDetail) -> Self {
        self.detail = Some(detail);
        self
    }

    /// Return the stable top-level category.
    #[must_use]
    pub const fn code(&self) -> ControllerErrorCode {
        self.code
    }

    /// Return the stable controller stage.
    #[must_use]
    pub const fn stage(&self) -> ControllerStage {
        self.stage
    }

    /// Return a bounded reason containing no request, identity, route, or token values.
    #[must_use]
    pub const fn reason(&self) -> &'static str {
        self.reason
    }

    /// Return an optional value-free subsystem classification.
    #[must_use]
    pub const fn detail(&self) -> Option<ControllerErrorDetail> {
        self.detail
    }
}

impl fmt::Debug for ControllerError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter
            .debug_struct("ControllerError")
            .field("code", &self.code)
            .field("stage", &self.stage)
            .field("reason", &self.reason)
            .field("detail", &self.detail)
            .finish()
    }
}

impl fmt::Display for ControllerError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(formatter, "{:?}: {}", self.code, self.reason)
    }
}

impl std::error::Error for ControllerError {}

/// Result alias for P02 controller operations.
pub type Result<T> = std::result::Result<T, ControllerError>;
