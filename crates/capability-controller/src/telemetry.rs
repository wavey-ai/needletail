use media_object::{MediaClass, Operation};
use serde::{Deserialize, Serialize};

use crate::error::{ControllerError, ControllerErrorCode, ControllerErrorDetail, ControllerStage};
use crate::policy::ClientLifetimeClass;

/// Stable success/failure result recorded without opaque values.
#[derive(Clone, Copy, Debug, Deserialize, Eq, Hash, Ord, PartialEq, PartialOrd, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum TelemetryOutcome {
    Succeeded,
    Rejected,
}

/// Redacted P02 event. IDs, scopes, proofs, tokens, keys, origins, and paths are absent.
#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize)]
pub struct CapabilityTelemetryEvent {
    pub stage: ControllerStage,
    pub outcome: TelemetryOutcome,
    pub client_class: ClientLifetimeClass,
    pub renewal: bool,
    pub operation: Option<Operation>,
    pub media_class: Option<MediaClass>,
    pub route_candidate_count: usize,
    pub source_scope_count: usize,
    pub audience_scope_count: usize,
    pub capability_lifetime_seconds: Option<i64>,
    pub error_code: Option<ControllerErrorCode>,
    pub error_detail: Option<ControllerErrorDetail>,
}

impl CapabilityTelemetryEvent {
    #[allow(clippy::too_many_arguments)]
    pub(crate) const fn succeeded(
        stage: ControllerStage,
        client_class: ClientLifetimeClass,
        renewal: bool,
        operation: Option<Operation>,
        media_class: Option<MediaClass>,
        route_candidate_count: usize,
        source_scope_count: usize,
        audience_scope_count: usize,
        capability_lifetime_seconds: Option<i64>,
    ) -> Self {
        Self {
            stage,
            outcome: TelemetryOutcome::Succeeded,
            client_class,
            renewal,
            operation,
            media_class,
            route_candidate_count,
            source_scope_count,
            audience_scope_count,
            capability_lifetime_seconds,
            error_code: None,
            error_detail: None,
        }
    }

    #[allow(clippy::too_many_arguments)]
    pub(crate) const fn rejected(
        error: &ControllerError,
        client_class: ClientLifetimeClass,
        renewal: bool,
        operation: Option<Operation>,
        media_class: Option<MediaClass>,
        route_candidate_count: usize,
        source_scope_count: usize,
        audience_scope_count: usize,
    ) -> Self {
        Self {
            stage: error.stage(),
            outcome: TelemetryOutcome::Rejected,
            client_class,
            renewal,
            operation,
            media_class,
            route_candidate_count,
            source_scope_count,
            audience_scope_count,
            capability_lifetime_seconds: None,
            error_code: Some(error.code()),
            error_detail: error.detail(),
        }
    }
}

/// Best-effort telemetry sink. Recording failure must never grant authorization.
pub trait TelemetrySink: Send + Sync {
    fn record(&self, event: CapabilityTelemetryEvent);
}

/// Explicit sink for deployments/tests that do not install telemetry yet.
#[derive(Clone, Copy, Debug, Default)]
pub struct NoopTelemetry;

impl TelemetrySink for NoopTelemetry {
    fn record(&self, _event: CapabilityTelemetryEvent) {}
}
