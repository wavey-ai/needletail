use std::fmt;

use base64::engine::general_purpose::URL_SAFE_NO_PAD;
use base64::Engine as _;
use media_object::{
    AudienceId, CapabilityId, ContributorId, DescriptorId, EndpointId, LiveMonitorTransport,
    MediaAuthorizationFactV1, MediaAuthorizationRequestV1, MediaAuthorizationRequestV1Params,
    MediaCapabilityClaimsV1, MediaCapabilityClaimsV1Params, MediaClass, MediaEndpointDescriptorV1,
    Operation, SessionId, SessionWorkflowMode, SourceId, SubjectId, TakeId, TenantId,
};
use serde::Serialize;

use crate::entropy::EntropySource;
use crate::error::{
    ControllerError, ControllerErrorCode, ControllerErrorDetail, ControllerStage,
    ExchangeRejection, Result,
};
use crate::exchange::{
    token_hash, CompactCapability, ExchangeConsumeRequest, ExchangeLease, ExchangeStore,
    ExchangeStoreConsume, ExchangeStoreInsert, ExchangeToken,
};
use crate::identity::IdentityAuthorizationClient;
use crate::issuer::{Ed25519CapabilityIssuer, JwksView};
use crate::policy::{ClientLifetimeClass, LifetimePolicy};
use crate::route::{
    AdmissionClientProfile, Codec, DeadlineClass, RouteCandidate, RouteSelectionRequest,
    RouteSelector,
};
use crate::telemetry::{CapabilityTelemetryEvent, TelemetrySink};

const ID_ENTROPY_BYTES: usize = 18;
const MAX_EXCHANGE_INSERT_ATTEMPTS: usize = 4;
const REDACTED: &str = "[REDACTED]";
const TALKBACK_SAMPLE_RATE_HZ: u32 = 48_000;
const TALKBACK_FRAME_DURATION_US: u32 = 5_000;
const TALKBACK_FRAME_SAMPLES: u32 = 240;

#[derive(Clone, Copy)]
struct ExchangeDeadlines {
    consume_expires_at: i64,
    capability_expires_at: i64,
}

/// Authenticated broker profile. Public browsers are represented explicitly so
/// they can be denied before identity or route work.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum BrokerCaller {
    SessionsBroker,
    NativeBroker,
    PublicBrowser,
}

/// Authorization response profile and hard lifetime class.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum AuthorizationMode {
    BrowserPlayback,
    BrowserTalkback,
    NativeMedia,
}

impl AuthorizationMode {
    const fn lifetime_class(self) -> ClientLifetimeClass {
        match self {
            Self::BrowserPlayback | Self::BrowserTalkback => ClientLifetimeClass::BrowserPlayback,
            Self::NativeMedia => ClientLifetimeClass::NativeMedia,
        }
    }
}

/// One independently controlled rollout gate.
#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
pub enum FeatureState {
    #[default]
    Disabled,
    Enabled,
}

impl FeatureState {
    const fn is_enabled(self) -> bool {
        matches!(self, Self::Enabled)
    }
}

/// Fail-closed global issuance kill switch.
#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
pub enum KillSwitch {
    #[default]
    Engaged,
    Open,
}

/// Independent rollout gates and a fail-closed global kill switch.
#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
pub struct FeatureGates {
    pub global_kill_switch: KillSwitch,
    pub media_authorization_view_v1: FeatureState,
    pub media_capability_issue_v1: FeatureState,
    pub browser_playback_exchange_v1: FeatureState,
}

impl FeatureGates {
    /// Construct fully enabled local/test gates. Production rollout must supply
    /// explicit account/session policy outside this crate.
    #[must_use]
    pub const fn enabled() -> Self {
        Self {
            global_kill_switch: KillSwitch::Open,
            media_authorization_view_v1: FeatureState::Enabled,
            media_capability_issue_v1: FeatureState::Enabled,
            browser_playback_exchange_v1: FeatureState::Enabled,
        }
    }
}

/// Static issuer/audience/bootstrap policy and rollout gates.
pub struct ControllerConfig {
    issuer: String,
    publish_audience: String,
    subscribe_audience: String,
    take_audience: String,
    acknowledge_audience: String,
    browser_bootstrap_url: String,
    feature_gates: FeatureGates,
    lifetime_policy: LifetimePolicy,
}

impl ControllerConfig {
    /// Construct validated controller policy.
    ///
    /// # Errors
    ///
    /// Returns an error for unbounded/non-ASCII authorities or a bootstrap URL
    /// that is not a fixed credential-free HTTPS endpoint.
    #[allow(clippy::too_many_arguments)]
    pub fn new(
        issuer: impl Into<String>,
        publish_audience: impl Into<String>,
        subscribe_audience: impl Into<String>,
        take_audience: impl Into<String>,
        acknowledge_audience: impl Into<String>,
        browser_bootstrap_url: impl Into<String>,
        feature_gates: FeatureGates,
        lifetime_policy: LifetimePolicy,
    ) -> Result<Self> {
        let config = Self {
            issuer: issuer.into(),
            publish_audience: publish_audience.into(),
            subscribe_audience: subscribe_audience.into(),
            take_audience: take_audience.into(),
            acknowledge_audience: acknowledge_audience.into(),
            browser_bootstrap_url: browser_bootstrap_url.into(),
            feature_gates,
            lifetime_policy,
        };
        if [
            config.issuer.as_str(),
            config.publish_audience.as_str(),
            config.subscribe_audience.as_str(),
            config.take_audience.as_str(),
            config.acknowledge_audience.as_str(),
        ]
        .iter()
        .any(|value| !valid_visible_authority(value))
            || !valid_bootstrap_url(&config.browser_bootstrap_url)
        {
            return Err(ControllerError::new(
                ControllerErrorCode::InvalidControllerState,
                ControllerStage::FeatureGate,
                "controller authority or bootstrap configuration is invalid",
            ));
        }
        Ok(config)
    }

    fn audience(&self, operation: Operation) -> &str {
        match operation {
            Operation::Publish => &self.publish_audience,
            Operation::Subscribe => &self.subscribe_audience,
            Operation::UploadTake | Operation::ReadTake => &self.take_audience,
            Operation::AcknowledgePlayout => &self.acknowledge_audience,
        }
    }
}

impl fmt::Debug for ControllerConfig {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter
            .debug_struct("ControllerConfig")
            .field("issuer", &REDACTED)
            .field("publish_audience", &REDACTED)
            .field("subscribe_audience", &REDACTED)
            .field("take_audience", &REDACTED)
            .field("acknowledge_audience", &REDACTED)
            .field("browser_bootstrap_url", &REDACTED)
            .field("feature_gates", &self.feature_gates)
            .field("lifetime_policy", &self.lifetime_policy)
            .finish()
    }
}

/// Internal broker request. The browser advertises client support but cannot
/// choose tenant, topology generation, route, binding, edge, or contributor.
#[derive(Clone)]
pub struct BrokerAuthorizationRequest {
    pub mode: AuthorizationMode,
    pub subject: SubjectId,
    pub endpoint_id: EndpointId,
    pub operation: Operation,
    pub media_class: MediaClass,
    pub source_ids: Vec<SourceId>,
    pub audience_ids: Vec<AudienceId>,
    pub take_id: Option<TakeId>,
    pub client_key_thumbprint: Option<String>,
    pub requested_channels: u16,
    pub requested_sample_rate_hz: Option<u32>,
    pub requested_frame_duration_us: Option<u32>,
    pub requested_frame_samples: Option<u32>,
    pub requested_bitrate: u64,
    pub requested_datagram_bytes: u32,
    pub client: AdmissionClientProfile,
}

impl fmt::Debug for BrokerAuthorizationRequest {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter
            .debug_struct("BrokerAuthorizationRequest")
            .field("mode", &self.mode)
            .field("subject", &REDACTED)
            .field("endpoint_id", &REDACTED)
            .field("operation", &self.operation)
            .field("media_class", &self.media_class)
            .field("source_count", &self.source_ids.len())
            .field("audience_count", &self.audience_ids.len())
            .field("has_take", &self.take_id.is_some())
            .field("client_key_thumbprint", &REDACTED)
            .field("requested_channels", &self.requested_channels)
            .field("requested_sample_rate_hz", &self.requested_sample_rate_hz)
            .field(
                "requested_frame_duration_us",
                &self.requested_frame_duration_us,
            )
            .field("requested_frame_samples", &self.requested_frame_samples)
            .field("requested_bitrate", &self.requested_bitrate)
            .field("requested_datagram_bytes", &self.requested_datagram_bytes)
            .field("client", &self.client)
            .finish()
    }
}

/// Trusted current desired state supplied by Needletail control, not the client.
pub struct DesiredMediaState<'a> {
    pub tenant_id: &'a TenantId,
    pub session_id: &'a SessionId,
    pub topology_generation: u64,
    pub class_authorization_epoch: Option<u64>,
    pub contributor_id: Option<&'a ContributorId>,
    pub require_independent_repair: bool,
    pub route_candidates: &'a [RouteCandidate],
}

impl fmt::Debug for DesiredMediaState<'_> {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter
            .debug_struct("DesiredMediaState")
            .field("tenant_id", &REDACTED)
            .field("session_id", &REDACTED)
            .field("topology_generation", &self.topology_generation)
            .field("class_authorization_epoch", &self.class_authorization_epoch)
            .field("contributor_id", &REDACTED)
            .field(
                "require_independent_repair",
                &self.require_independent_repair,
            )
            .field("route_candidate_count", &self.route_candidates.len())
            .finish()
    }
}

/// Public bootstrap object containing only the one-use exchange value.
#[derive(Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct BrowserExchangeGrant {
    token: ExchangeToken,
    expires_at: i64,
    bootstrap_url: String,
}

impl BrowserExchangeGrant {
    #[must_use]
    pub const fn token(&self) -> &ExchangeToken {
        &self.token
    }

    #[must_use]
    pub const fn expires_at(&self) -> i64 {
        self.expires_at
    }

    #[must_use]
    pub fn bootstrap_url(&self) -> &str {
        &self.bootstrap_url
    }
}

impl fmt::Debug for BrowserExchangeGrant {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter
            .debug_struct("BrowserExchangeGrant")
            .field("token", &self.token)
            .field("expires_at", &self.expires_at)
            .field("bootstrap_url", &REDACTED)
            .finish()
    }
}

/// Non-secret admission explanation returned to the browser broker.
#[derive(Clone, Debug, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct AdmissionMetadata {
    workflow_mode: SessionWorkflowMode,
    live_transport: Codec,
    receiver_allowance_ms: u16,
    reason: &'static str,
    renew_at: i64,
}

impl AdmissionMetadata {
    #[must_use]
    pub const fn workflow_mode(&self) -> SessionWorkflowMode {
        self.workflow_mode
    }

    #[must_use]
    pub const fn live_transport(&self) -> Codec {
        self.live_transport
    }

    #[must_use]
    pub const fn receiver_allowance_ms(&self) -> u16 {
        self.receiver_allowance_ms
    }

    #[must_use]
    pub const fn reason(&self) -> &'static str {
        self.reason
    }

    #[must_use]
    pub const fn renew_at(&self) -> i64 {
        self.renew_at
    }
}

/// Public browser broker response. It intentionally has no reusable JWS or descriptor.
#[derive(Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct BrowserExchangeAuthorization {
    authorization_id: String,
    issued_at: i64,
    authorization_expires_at: i64,
    session_epoch: u64,
    media_authorization_epoch: u64,
    subject_grant_epoch: u64,
    media_policy_version: u64,
    binding_generation: u64,
    topology_generation: u64,
    exchange: BrowserExchangeGrant,
    admission: AdmissionMetadata,
}

impl BrowserExchangeAuthorization {
    #[must_use]
    pub const fn exchange(&self) -> &BrowserExchangeGrant {
        &self.exchange
    }

    #[must_use]
    pub const fn admission(&self) -> &AdmissionMetadata {
        &self.admission
    }

    #[must_use]
    pub const fn authorization_expires_at(&self) -> i64 {
        self.authorization_expires_at
    }

    #[must_use]
    pub const fn session_epoch(&self) -> u64 {
        self.session_epoch
    }

    #[must_use]
    pub const fn binding_generation(&self) -> u64 {
        self.binding_generation
    }

    #[must_use]
    pub const fn topology_generation(&self) -> u64 {
        self.topology_generation
    }
}

impl fmt::Debug for BrowserExchangeAuthorization {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter
            .debug_struct("BrowserExchangeAuthorization")
            .field("authorization_id", &REDACTED)
            .field("issued_at", &self.issued_at)
            .field("authorization_expires_at", &self.authorization_expires_at)
            .field("session_epoch", &self.session_epoch)
            .field("media_authorization_epoch", &self.media_authorization_epoch)
            .field("subject_grant_epoch", &self.subject_grant_epoch)
            .field("media_policy_version", &self.media_policy_version)
            .field("binding_generation", &self.binding_generation)
            .field("topology_generation", &self.topology_generation)
            .field("exchange", &self.exchange)
            .field("admission", &self.admission)
            .finish()
    }
}

/// Backward-compatible name for the original browser playback exchange shape.
pub type BrowserPlaybackAuthorization = BrowserExchangeAuthorization;

/// Authenticated native broker response carrying the short-lived JWS.
#[derive(Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct NativeMediaAuthorization {
    authorization_id: String,
    issued_at: i64,
    expires_at: i64,
    renew_at: i64,
    capability: CompactCapability,
    endpoints: Vec<MediaEndpointDescriptorV1>,
}

impl NativeMediaAuthorization {
    #[must_use]
    pub const fn capability(&self) -> &CompactCapability {
        &self.capability
    }

    #[must_use]
    pub fn endpoints(&self) -> &[MediaEndpointDescriptorV1] {
        &self.endpoints
    }

    #[must_use]
    pub const fn expires_at(&self) -> i64 {
        self.expires_at
    }

    #[must_use]
    pub const fn renew_at(&self) -> i64 {
        self.renew_at
    }
}

impl fmt::Debug for NativeMediaAuthorization {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter
            .debug_struct("NativeMediaAuthorization")
            .field("authorization_id", &REDACTED)
            .field("issued_at", &self.issued_at)
            .field("expires_at", &self.expires_at)
            .field("renew_at", &self.renew_at)
            .field("capability", &self.capability)
            .field("endpoints", &self.endpoints)
            .finish()
    }
}

/// Closed success variants for browser and authenticated native brokers.
#[derive(Clone, Debug, Serialize)]
#[serde(tag = "kind", content = "authorization", rename_all = "snake_case")]
pub enum IssuedAuthorization {
    BrowserPlayback(BrowserExchangeAuthorization),
    BrowserTalkback(BrowserExchangeAuthorization),
    NativeMedia(NativeMediaAuthorization),
}

impl IssuedAuthorization {
    const fn expires_at(&self) -> i64 {
        match self {
            Self::BrowserPlayback(value) | Self::BrowserTalkback(value) => {
                value.authorization_expires_at
            }
            Self::NativeMedia(value) => value.expires_at,
        }
    }
}

/// P02 controller composed from explicit identity, storage, entropy, and telemetry boundaries.
pub struct CapabilityController<I, S, E, T> {
    identity: I,
    route_selector: RouteSelector,
    issuer: Ed25519CapabilityIssuer,
    exchange_store: S,
    entropy: E,
    telemetry: T,
    config: ControllerConfig,
}

impl<I, S, E, T> CapabilityController<I, S, E, T>
where
    I: IdentityAuthorizationClient,
    S: ExchangeStore,
    E: EntropySource,
    T: TelemetrySink,
{
    /// Construct the pure controller. All production adapters remain injected.
    #[must_use]
    pub fn new(
        identity: I,
        route_selector: RouteSelector,
        issuer: Ed25519CapabilityIssuer,
        exchange_store: S,
        entropy: E,
        telemetry: T,
        config: ControllerConfig,
    ) -> Self {
        Self {
            identity,
            route_selector,
            issuer,
            exchange_store,
            entropy,
            telemetry,
            config,
        }
    }

    /// Fetch current identity state, select a route, and issue one authorization.
    ///
    /// # Errors
    ///
    /// Fails closed on caller/gate, identity, route, signing, entropy, or store errors.
    pub fn issue(
        &self,
        caller: BrokerCaller,
        request: &BrokerAuthorizationRequest,
        desired: &DesiredMediaState<'_>,
        now: i64,
    ) -> Result<IssuedAuthorization> {
        self.issue_or_renew(caller, request, desired, now, false)
    }

    /// Renew by repeating the full current identity and topology evaluation.
    ///
    /// There is intentionally no path that copies generations from an old token.
    ///
    /// # Errors
    ///
    /// Returns the same fail-closed categories as [`Self::issue`].
    pub fn renew(
        &self,
        caller: BrokerCaller,
        request: &BrokerAuthorizationRequest,
        desired: &DesiredMediaState<'_>,
        now: i64,
    ) -> Result<IssuedAuthorization> {
        self.issue_or_renew(caller, request, desired, now, true)
    }

    fn issue_or_renew(
        &self,
        caller: BrokerCaller,
        request: &BrokerAuthorizationRequest,
        desired: &DesiredMediaState<'_>,
        now: i64,
        renewal: bool,
    ) -> Result<IssuedAuthorization> {
        let result = self.issue_inner(caller, request, desired, now);
        let class = request.mode.lifetime_class();
        match &result {
            Ok(issued) => self.telemetry.record(CapabilityTelemetryEvent::succeeded(
                match issued {
                    IssuedAuthorization::BrowserPlayback(_)
                    | IssuedAuthorization::BrowserTalkback(_) => ControllerStage::BrowserExchange,
                    IssuedAuthorization::NativeMedia(_) => ControllerStage::CapabilityIssuance,
                },
                class,
                renewal,
                Some(request.operation),
                Some(request.media_class),
                desired.route_candidates.len(),
                request.source_ids.len(),
                request.audience_ids.len(),
                Some(issued.expires_at().saturating_sub(now)),
            )),
            Err(error) => self.telemetry.record(CapabilityTelemetryEvent::rejected(
                error,
                class,
                renewal,
                Some(request.operation),
                Some(request.media_class),
                desired.route_candidates.len(),
                request.source_ids.len(),
                request.audience_ids.len(),
            )),
        }
        result
    }

    #[allow(clippy::too_many_lines)]
    fn issue_inner(
        &self,
        caller: BrokerCaller,
        request: &BrokerAuthorizationRequest,
        desired: &DesiredMediaState<'_>,
        now: i64,
    ) -> Result<IssuedAuthorization> {
        self.check_gates(request.mode)?;
        validate_caller(caller, request.mode)?;
        validate_client_request(request)?;
        validate_desired_state(request, desired)?;

        let identity_request =
            MediaAuthorizationRequestV1::new(MediaAuthorizationRequestV1Params {
                subject: request.subject.clone(),
                endpoint_id: request.endpoint_id.clone(),
                requested_operation: request.operation,
                requested_media_class: request.media_class,
                requested_source_ids: request.source_ids.clone(),
                requested_audience_ids: request.audience_ids.clone(),
                take_id: request.take_id.clone(),
            })
            .map_err(|error| invalid_request(&error))?;
        let fact = self
            .identity
            .authorize(desired.session_id, &identity_request, now)?;
        validate_identity_fact(
            &fact,
            &identity_request,
            desired.session_id,
            now,
            self.config.lifetime_policy,
        )?;

        let class = request.mode.lifetime_class();
        let expires_at = self.config.lifetime_policy.capability_expires_at(
            class,
            now,
            fact.access_expires_at(),
        )?;
        let descriptor_id =
            DescriptorId::new(self.random_id("dsc")?).map_err(|error| invalid_request(&error))?;
        let admission = self.route_selector.select(
            &RouteSelectionRequest {
                tenant_id: desired.tenant_id,
                session_id: desired.session_id,
                session_epoch: fact.session_epoch(),
                endpoint_id: identity_request.endpoint_id(),
                descriptor_id: &descriptor_id,
                topology_generation: desired.topology_generation,
                operation: request.operation,
                media_class: request.media_class,
                source_ids: identity_request.requested_source_ids(),
                audience_ids: identity_request.requested_audience_ids(),
                deadline_class: deadline_class(request.operation, request.media_class),
                require_independent_repair: desired.require_independent_repair,
                requested_channels: request.requested_channels,
                requested_bitrate: request.requested_bitrate,
                requested_datagram_bytes: request.requested_datagram_bytes,
                client: &request.client,
                now,
                expires_at,
            },
            desired.route_candidates,
        )?;

        let capability_id_value = self.random_id("cap")?;
        let capability_id = CapabilityId::new(capability_id_value.clone())
            .map_err(|error| invalid_request(&error))?;
        let authorization_id = self.random_id("auth")?;
        let contributor_id = contributor_for_operation(request.operation, desired.contributor_id)?;
        let claims = MediaCapabilityClaimsV1::new(MediaCapabilityClaimsV1Params {
            issuer: self.config.issuer.clone(),
            audience: self.config.audience(request.operation).to_owned(),
            capability_id,
            tenant_id: desired.tenant_id.clone(),
            session_id: desired.session_id.clone(),
            session_epoch: fact.session_epoch(),
            media_authorization_epoch: fact.media_authorization_epoch(),
            subject_grant_epoch: fact.subject_grant_epoch(),
            media_policy_version: fact.media_policy_version(),
            class_authorization_epoch: desired.class_authorization_epoch,
            binding_generation: admission.binding_generation,
            participant_id: fact.participant_id().clone(),
            endpoint_id: fact.endpoint_id().clone(),
            contributor_id,
            operation: request.operation,
            media_class: request.media_class,
            source_ids: identity_request.requested_source_ids().to_vec(),
            audience_ids: identity_request.requested_audience_ids().to_vec(),
            take_id: identity_request.take_id().cloned(),
            topology_generation: admission.topology_generation,
            edge_ids: admission.edge_ids.clone(),
            max_channels: admission.max_channels,
            max_bitrate: admission.max_bitrate,
            max_datagram_bytes: admission.max_datagram_bytes,
            client_key_thumbprint: request.client_key_thumbprint.clone(),
            issued_at: now,
            not_before: self.config.lifetime_policy.not_before(now),
            expires_at,
        })
        .map_err(|error| {
            ControllerError::new(
                ControllerErrorCode::InvalidControllerState,
                ControllerStage::CapabilityIssuance,
                "qualified state could not form canonical P00 capability claims",
            )
            .with_detail(ControllerErrorDetail::MediaControl(error.code()))
        })?;
        let compact = CompactCapability::new(self.issuer.sign(&claims)?);
        let renew_at = self.config.lifetime_policy.renewal_at(
            class,
            now,
            expires_at,
            capability_id_value.as_bytes(),
        );

        match request.mode {
            AuthorizationMode::NativeMedia => {
                Ok(IssuedAuthorization::NativeMedia(NativeMediaAuthorization {
                    authorization_id,
                    issued_at: now,
                    expires_at,
                    renew_at,
                    capability: compact,
                    endpoints: vec![admission.descriptor],
                }))
            }
            AuthorizationMode::BrowserPlayback | AuthorizationMode::BrowserTalkback => {
                let proof = request.client_key_thumbprint.as_ref().ok_or_else(|| {
                    ControllerError::new(
                        ControllerErrorCode::InvalidRequest,
                        ControllerStage::RequestValidation,
                        "browser exchange requires an endpoint key thumbprint",
                    )
                })?;
                let exchange_expires_at = self
                    .config
                    .lifetime_policy
                    .exchange_expires_at(now, expires_at)?;
                let token = self.insert_browser_exchange(
                    proof,
                    &fact,
                    request,
                    &admission.descriptor,
                    &compact,
                    ExchangeDeadlines {
                        consume_expires_at: exchange_expires_at,
                        capability_expires_at: expires_at,
                    },
                )?;
                let authorization = BrowserExchangeAuthorization {
                    authorization_id,
                    issued_at: now,
                    authorization_expires_at: expires_at,
                    session_epoch: fact.session_epoch(),
                    media_authorization_epoch: fact.media_authorization_epoch(),
                    subject_grant_epoch: fact.subject_grant_epoch(),
                    media_policy_version: fact.media_policy_version(),
                    binding_generation: admission.binding_generation,
                    topology_generation: admission.topology_generation,
                    exchange: BrowserExchangeGrant {
                        token,
                        expires_at: exchange_expires_at,
                        bootstrap_url: self.config.browser_bootstrap_url.clone(),
                    },
                    admission: AdmissionMetadata {
                        workflow_mode: fact.workflow_mode(),
                        live_transport: admission.codec,
                        receiver_allowance_ms: request.client.receiver_allowance_ms,
                        reason: "admitted",
                        renew_at,
                    },
                };
                Ok(match request.mode {
                    AuthorizationMode::BrowserPlayback => {
                        IssuedAuthorization::BrowserPlayback(authorization)
                    }
                    AuthorizationMode::BrowserTalkback => {
                        IssuedAuthorization::BrowserTalkback(authorization)
                    }
                    AuthorizationMode::NativeMedia => unreachable!("native mode handled above"),
                })
            }
        }
    }

    fn insert_browser_exchange(
        &self,
        proof: &str,
        fact: &MediaAuthorizationFactV1,
        request: &BrokerAuthorizationRequest,
        descriptor: &MediaEndpointDescriptorV1,
        capability: &CompactCapability,
        deadlines: ExchangeDeadlines,
    ) -> Result<ExchangeToken> {
        for _attempt in 0..MAX_EXCHANGE_INSERT_ATTEMPTS {
            let mut bytes = [0_u8; 32];
            self.entropy.fill_bytes(&mut bytes).map_err(|_error| {
                ControllerError::new(
                    ControllerErrorCode::EntropyUnavailable,
                    ControllerStage::BrowserExchange,
                    "browser exchange entropy is unavailable",
                )
            })?;
            let token = ExchangeToken::from_bytes(bytes);
            let hash = token_hash(token.expose()).expect("issued exchange bytes are canonical");
            let insert = ExchangeStoreInsert::new(
                hash,
                descriptor.edge_id().clone(),
                fact.endpoint_id().clone(),
                proof.to_owned(),
                deadlines.consume_expires_at,
                deadlines.capability_expires_at,
                capability.clone(),
                descriptor.clone(),
                request.operation,
                request.media_class,
            );
            match self.exchange_store.insert(insert) {
                Ok(()) => return Ok(token),
                Err(ExchangeRejection::DuplicateToken) => {}
                Err(reason) => return Err(exchange_error(reason)),
            }
        }
        Err(exchange_error(ExchangeRejection::DuplicateToken))
    }

    /// Atomically consume one proof/edge/endpoint-bound browser exchange.
    ///
    /// # Errors
    ///
    /// Exactly one parallel request can succeed. Wrong proof, edge, endpoint,
    /// expiry, replay, malformed values, and store failure all fail closed.
    pub fn consume_exchange(&self, request: &ExchangeConsumeRequest<'_>) -> Result<ExchangeLease> {
        let class = ClientLifetimeClass::BrowserPlayback;
        if let Err(error) = self.check_gates(AuthorizationMode::BrowserPlayback) {
            self.telemetry.record(CapabilityTelemetryEvent::rejected(
                &error, class, false, None, None, 0, 0, 0,
            ));
            return Err(error);
        }
        let hash = match token_hash(request.token) {
            Ok(hash) => hash,
            Err(reason) => {
                let error = exchange_error(reason);
                self.telemetry.record(CapabilityTelemetryEvent::rejected(
                    &error, class, false, None, None, 0, 0, 0,
                ));
                return Err(error);
            }
        };
        let result = self
            .exchange_store
            .consume(ExchangeStoreConsume {
                token_hash: hash,
                edge_id: request.edge_id,
                endpoint_id: request.endpoint_id,
                endpoint_proof_thumbprint: request.endpoint_proof_thumbprint,
                now: request.now,
            })
            .map_err(exchange_error);
        match &result {
            Ok(lease) => self.telemetry.record(CapabilityTelemetryEvent::succeeded(
                ControllerStage::BrowserExchange,
                class,
                false,
                Some(lease.operation()),
                Some(lease.media_class()),
                0,
                0,
                0,
                Some(lease.expires_at().saturating_sub(request.now)),
            )),
            Err(error) => self.telemetry.record(CapabilityTelemetryEvent::rejected(
                error, class, false, None, None, 0, 0, 0,
            )),
        }
        result
    }

    /// Return the public-only active/overlap key view even when issuance is disabled.
    #[must_use]
    pub fn jwks(&self, now: i64) -> JwksView {
        self.issuer.jwks(now)
    }

    fn check_gates(&self, mode: AuthorizationMode) -> Result<()> {
        let gates = self.config.feature_gates;
        if gates.global_kill_switch == KillSwitch::Engaged
            || !gates.media_authorization_view_v1.is_enabled()
            || !gates.media_capability_issue_v1.is_enabled()
            || (matches!(
                mode,
                AuthorizationMode::BrowserPlayback | AuthorizationMode::BrowserTalkback
            ) && !gates.browser_playback_exchange_v1.is_enabled())
        {
            return Err(ControllerError::new(
                ControllerErrorCode::FeatureDisabled,
                ControllerStage::FeatureGate,
                "media capability issuance is disabled by current server policy",
            ));
        }
        Ok(())
    }

    fn random_id(&self, prefix: &str) -> Result<String> {
        let mut bytes = [0_u8; ID_ENTROPY_BYTES];
        self.entropy.fill_bytes(&mut bytes).map_err(|_error| {
            ControllerError::new(
                ControllerErrorCode::EntropyUnavailable,
                ControllerStage::CapabilityIssuance,
                "capability identifier entropy is unavailable",
            )
        })?;
        Ok(format!("{prefix}_{}", URL_SAFE_NO_PAD.encode(bytes)))
    }
}

impl<I, S, E, T> fmt::Debug for CapabilityController<I, S, E, T> {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter
            .debug_struct("CapabilityController")
            .field("identity", &REDACTED)
            .field("route_selector", &self.route_selector)
            .field("issuer", &self.issuer)
            .field("exchange_store", &REDACTED)
            .field("entropy", &REDACTED)
            .field("telemetry", &REDACTED)
            .field("config", &self.config)
            .finish()
    }
}

fn validate_caller(caller: BrokerCaller, mode: AuthorizationMode) -> Result<()> {
    let allowed = matches!(
        (caller, mode),
        (
            BrokerCaller::SessionsBroker,
            AuthorizationMode::BrowserPlayback | AuthorizationMode::BrowserTalkback
        ) | (BrokerCaller::NativeBroker, AuthorizationMode::NativeMedia)
    );
    if !allowed {
        return Err(ControllerError::new(
            ControllerErrorCode::CallerNotAllowed,
            ControllerStage::CallerAuthentication,
            "caller is not the authenticated broker for this authorization profile",
        ));
    }
    Ok(())
}

fn validate_client_request(request: &BrokerAuthorizationRequest) -> Result<()> {
    if request.client.supported_transports.is_empty()
        || request.client.supported_transports.len() > 8
        || request.client.supported_codecs.is_empty()
        || request.client.supported_codecs.len() > 8
        || request.client.max_channels == 0
        || request.client.receiver_allowance_ms < 5
        || request.client.receiver_allowance_ms > 250
    {
        return Err(ControllerError::new(
            ControllerErrorCode::InvalidRequest,
            ControllerStage::RequestValidation,
            "client admission profile is empty or outside fixed bounds",
        ));
    }
    match request.mode {
        AuthorizationMode::BrowserPlayback => {
            if request.operation != Operation::Subscribe {
                return Err(ControllerError::new(
                    ControllerErrorCode::CallerNotAllowed,
                    ControllerStage::CallerAuthentication,
                    "browser playback exchange cannot issue publish or take authority",
                ));
            }
            if request.media_class == MediaClass::Talkback {
                return Err(ControllerError::new(
                    ControllerErrorCode::InvalidRequest,
                    ControllerStage::RequestValidation,
                    "talkback must use the browser talkback authorization profile",
                ));
            }
        }
        AuthorizationMode::BrowserTalkback => {
            if request.media_class != MediaClass::Talkback {
                return Err(ControllerError::new(
                    ControllerErrorCode::InvalidRequest,
                    ControllerStage::RequestValidation,
                    "browser talkback exchange only issues talkback authority",
                ));
            }
        }
        AuthorizationMode::NativeMedia => {}
    }
    if matches!(
        request.mode,
        AuthorizationMode::BrowserPlayback | AuthorizationMode::BrowserTalkback
    ) && request.client_key_thumbprint.is_none()
    {
        return Err(ControllerError::new(
            ControllerErrorCode::InvalidRequest,
            ControllerStage::RequestValidation,
            "browser exchange requires endpoint proof binding",
        ));
    }
    if request.media_class == MediaClass::Talkback {
        validate_talkback_request(request)?;
    } else if request.requested_sample_rate_hz.is_some()
        || request.requested_frame_duration_us.is_some()
        || request.requested_frame_samples.is_some()
    {
        return Err(ControllerError::new(
            ControllerErrorCode::InvalidRequest,
            ControllerStage::RequestValidation,
            "frame format parameters are only accepted for talkback",
        ));
    }
    Ok(())
}

fn validate_talkback_request(request: &BrokerAuthorizationRequest) -> Result<()> {
    if !matches!(request.operation, Operation::Publish | Operation::Subscribe) {
        return Err(ControllerError::new(
            ControllerErrorCode::InvalidRequest,
            ControllerStage::RequestValidation,
            "talkback capability issuance supports publish and subscribe only",
        ));
    }
    if !request.source_ids.is_empty() || request.take_id.is_some() {
        return Err(ControllerError::new(
            ControllerErrorCode::InvalidRequest,
            ControllerStage::RequestValidation,
            "talkback capability scope must be audience-only",
        ));
    }
    if request.audience_ids.is_empty()
        || (request.operation == Operation::Publish && request.audience_ids.len() != 1)
    {
        return Err(ControllerError::new(
            ControllerErrorCode::InvalidRequest,
            ControllerStage::RequestValidation,
            "talkback publish requires exactly one audience and subscribe requires explicit audiences",
        ));
    }
    if request.client_key_thumbprint.is_none() {
        return Err(ControllerError::new(
            ControllerErrorCode::InvalidRequest,
            ControllerStage::RequestValidation,
            "talkback requires endpoint proof binding",
        ));
    }
    if request.requested_channels != 1
        || request.requested_sample_rate_hz != Some(TALKBACK_SAMPLE_RATE_HZ)
        || request.requested_frame_duration_us != Some(TALKBACK_FRAME_DURATION_US)
        || request.requested_frame_samples != Some(TALKBACK_FRAME_SAMPLES)
        || request.client.requested_live_transport != LiveMonitorTransport::Opus
        || !request.client.supported_codecs.contains(&Codec::Opus)
    {
        return Err(ControllerError::new(
            ControllerErrorCode::InvalidRequest,
            ControllerStage::RequestValidation,
            "talkback v1 is fixed to mono 48 kHz Opus with 5 ms frames",
        ));
    }
    Ok(())
}

fn validate_desired_state(
    request: &BrokerAuthorizationRequest,
    desired: &DesiredMediaState<'_>,
) -> Result<()> {
    if request.media_class == MediaClass::Talkback && desired.class_authorization_epoch.is_none() {
        return Err(ControllerError::new(
            ControllerErrorCode::InvalidControllerState,
            ControllerStage::RequestValidation,
            "talkback desired state lacks the current class authorization epoch",
        ));
    }
    Ok(())
}

fn validate_identity_fact(
    fact: &MediaAuthorizationFactV1,
    request: &MediaAuthorizationRequestV1,
    session_id: &SessionId,
    now: i64,
    policy: LifetimePolicy,
) -> Result<()> {
    let exact = fact.session_id() == session_id
        && fact.endpoint_id() == request.endpoint_id()
        && fact.requested_operation() == request.requested_operation()
        && fact.requested_media_class() == request.requested_media_class()
        && fact.take_id() == request.take_id()
        && fact
            .allowed_operations()
            .contains(&request.requested_operation())
        && fact
            .allowed_media_classes()
            .contains(&request.requested_media_class())
        && request
            .requested_source_ids()
            .iter()
            .all(|source| fact.allowed_source_ids().contains(source))
        && request
            .requested_audience_ids()
            .iter()
            .all(|audience| fact.allowed_audience_ids().contains(audience));
    if !exact {
        return Err(ControllerError::new(
            ControllerErrorCode::AuthorizationMismatch,
            ControllerStage::IdentityAuthorization,
            "identity authorization fact disagrees with the exact request context",
        ));
    }
    if fact.evaluated_at() > now.saturating_add(policy.clock_skew_seconds())
        || fact.evaluated_at() < now.saturating_sub(policy.identity_fact_max_age_seconds())
    {
        return Err(ControllerError::new(
            ControllerErrorCode::IdentityResponseInvalid,
            ControllerStage::IdentityAuthorization,
            "identity authorization fact is outside its freshness window",
        ));
    }
    if fact.access_expires_at().is_some_and(|expiry| expiry <= now) {
        return Err(ControllerError::new(
            ControllerErrorCode::AuthorizationExpired,
            ControllerStage::IdentityAuthorization,
            "identity access expired before capability issuance",
        ));
    }
    Ok(())
}

fn contributor_for_operation(
    operation: Operation,
    contributor_id: Option<&ContributorId>,
) -> Result<Option<ContributorId>> {
    if matches!(operation, Operation::Publish | Operation::UploadTake) {
        return contributor_id.cloned().map(Some).ok_or_else(|| {
            ControllerError::new(
                ControllerErrorCode::InvalidControllerState,
                ControllerStage::CapabilityIssuance,
                "publish or take-upload desired state lacks a contributor binding",
            )
        });
    }
    Ok(None)
}

const fn deadline_class(operation: Operation, media_class: MediaClass) -> DeadlineClass {
    if matches!(operation, Operation::UploadTake | Operation::ReadTake) {
        DeadlineClass::BulkTransfer
    } else if matches!(media_class, MediaClass::Talkback) {
        DeadlineClass::Interactive
    } else {
        DeadlineClass::LiveMonitor
    }
}

fn invalid_request(error: &media_object::MediaControlError) -> ControllerError {
    ControllerError::new(
        ControllerErrorCode::InvalidRequest,
        ControllerStage::RequestValidation,
        "request violates the frozen P00 media-control contract",
    )
    .with_detail(ControllerErrorDetail::MediaControl(error.code()))
}

fn exchange_error(reason: ExchangeRejection) -> ControllerError {
    let code = if reason == ExchangeRejection::StorageUnavailable {
        ControllerErrorCode::ExchangeUnavailable
    } else {
        ControllerErrorCode::ExchangeRejected
    };
    ControllerError::new(
        code,
        ControllerStage::BrowserExchange,
        "browser exchange was unavailable or did not match its one-use binding",
    )
    .with_detail(ControllerErrorDetail::Exchange(reason))
}

fn valid_visible_authority(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 512
        && value.is_ascii()
        && value.bytes().all(|byte| byte.is_ascii_graphic())
}

fn valid_bootstrap_url(value: &str) -> bool {
    let Some(rest) = value.strip_prefix("https://") else {
        return false;
    };
    let Some((authority, path)) = rest.split_once('/') else {
        return false;
    };
    !authority.is_empty()
        && path.starts_with("v1/")
        && value.len() <= 512
        && value.is_ascii()
        && !value
            .bytes()
            .any(|byte| byte.is_ascii_whitespace() || matches!(byte, b'?' | b'#' | b'@'))
}
