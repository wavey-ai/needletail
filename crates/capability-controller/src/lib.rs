//! Bounded Needletail P02 media-capability issuance and exchange control.
//!
//! This crate is deliberately transport- and deployment-neutral. It defines
//! strict traits for the authenticated P01 identity boundary and atomic
//! exchange storage, plus an honest process-local store for tests. Durable
//! distributed storage, mTLS, secret-manager loading, and deployed edge
//! invalidation are not claimed here.

mod controller;
mod entropy;
mod error;
mod exchange;
mod identity;
mod issuer;
mod policy;
mod route;
mod telemetry;

pub use controller::{
    AdmissionMetadata, AuthorizationMode, BrokerAuthorizationRequest, BrokerCaller,
    BrowserExchangeGrant, BrowserPlaybackAuthorization, CapabilityController, ControllerConfig,
    DesiredMediaState, FeatureGates, FeatureState, IssuedAuthorization, KillSwitch,
    NativeMediaAuthorization,
};
pub use entropy::{EntropyError, EntropySource, SystemEntropy};
pub use error::{
    ControllerError, ControllerErrorCode, ControllerErrorDetail, ControllerStage,
    ExchangeRejection, IdentityBoundaryErrorCode, Result, RouteRefusalReason,
};
pub use exchange::{
    CompactCapability, ExchangeConsumeRequest, ExchangeLease, ExchangeStore, ExchangeStoreConsume,
    ExchangeStoreInsert, ExchangeToken, InMemoryExchangeStore,
};
pub use identity::{
    IdentityAuthorizationClient, IdentityAuthorizationTransport, IdentityTransportError,
    P01HmacIdentityClient, SignedIdentityRequest, SignedIdentityResponse,
};
pub use issuer::{Ed25519CapabilityIssuer, JwkPublicKey, JwksView, RetiringPublicKey};
pub use policy::{ClientLifetimeClass, LifetimePolicy};
pub use route::{
    AdmissionClientProfile, Codec, DeadlineClass, RouteAdmission, RouteCandidate,
    RouteSelectionRequest, RouteSelector, WarmRepairPath,
};
pub use telemetry::{CapabilityTelemetryEvent, NoopTelemetry, TelemetryOutcome, TelemetrySink};
