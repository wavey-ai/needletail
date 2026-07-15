use media_object::{
    MEDIA_CONTROL_MAX_CAPABILITY_LIFETIME_SECONDS, MEDIA_CONTROL_MAX_CLOCK_SKEW_SECONDS,
};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};

use crate::error::{ControllerError, ControllerErrorCode, ControllerStage, Result};

/// Lifetime profile selected by the authenticated broker, never by a public edge.
#[derive(Clone, Copy, Debug, Deserialize, Eq, Hash, Ord, PartialEq, PartialOrd, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum ClientLifetimeClass {
    BrowserPlayback,
    NativeMedia,
}

/// Frozen P02 capability, exchange, skew, freshness, and renewal policy.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
#[allow(clippy::struct_field_names)]
pub struct LifetimePolicy {
    browser_capability_seconds: i64,
    native_capability_seconds: i64,
    browser_exchange_seconds: i64,
    browser_renewal_seconds: i64,
    native_renewal_seconds: i64,
    renewal_jitter_seconds: i64,
    clock_skew_seconds: i64,
    identity_fact_max_age_seconds: i64,
}

impl Default for LifetimePolicy {
    fn default() -> Self {
        Self {
            browser_capability_seconds: 90,
            native_capability_seconds: 60,
            browser_exchange_seconds: 15,
            browser_renewal_seconds: 45,
            native_renewal_seconds: 40,
            renewal_jitter_seconds: 5,
            clock_skew_seconds: MEDIA_CONTROL_MAX_CLOCK_SKEW_SECONDS,
            identity_fact_max_age_seconds: 30,
        }
    }
}

impl LifetimePolicy {
    /// Construct and validate a custom policy without weakening P00/P02 ceilings.
    ///
    /// # Errors
    ///
    /// Returns an error when a lifetime, skew, freshness, or renewal value is unsafe.
    #[allow(clippy::too_many_arguments)]
    pub fn new(
        browser_capability_seconds: i64,
        native_capability_seconds: i64,
        browser_exchange_seconds: i64,
        browser_renewal_seconds: i64,
        native_renewal_seconds: i64,
        renewal_jitter_seconds: i64,
        clock_skew_seconds: i64,
        identity_fact_max_age_seconds: i64,
    ) -> Result<Self> {
        let policy = Self {
            browser_capability_seconds,
            native_capability_seconds,
            browser_exchange_seconds,
            browser_renewal_seconds,
            native_renewal_seconds,
            renewal_jitter_seconds,
            clock_skew_seconds,
            identity_fact_max_age_seconds,
        };
        policy.validate()?;
        Ok(policy)
    }

    fn validate(&self) -> Result<()> {
        let valid = (1..=MEDIA_CONTROL_MAX_CAPABILITY_LIFETIME_SECONDS)
            .contains(&self.browser_capability_seconds)
            && (1..=60).contains(&self.native_capability_seconds)
            && (1..=15).contains(&self.browser_exchange_seconds)
            && (1..self.browser_capability_seconds).contains(&self.browser_renewal_seconds)
            && (1..self.native_capability_seconds).contains(&self.native_renewal_seconds)
            && (0..=15).contains(&self.renewal_jitter_seconds)
            && self.renewal_jitter_seconds < self.browser_renewal_seconds
            && self.renewal_jitter_seconds < self.native_renewal_seconds
            && (0..=MEDIA_CONTROL_MAX_CLOCK_SKEW_SECONDS).contains(&self.clock_skew_seconds)
            && (1..=60).contains(&self.identity_fact_max_age_seconds);
        if !valid {
            return Err(ControllerError::new(
                ControllerErrorCode::InvalidControllerState,
                ControllerStage::FeatureGate,
                "lifetime policy violates the frozen P02 safety ceilings",
            ));
        }
        Ok(())
    }

    /// Return the accepted verifier clock skew, never more than five seconds.
    #[must_use]
    pub const fn clock_skew_seconds(&self) -> i64 {
        self.clock_skew_seconds
    }

    /// Return the maximum accepted age of a freshly fetched identity fact.
    #[must_use]
    pub const fn identity_fact_max_age_seconds(&self) -> i64 {
        self.identity_fact_max_age_seconds
    }

    /// Return the class-specific hard capability lifetime ceiling.
    #[must_use]
    pub const fn capability_lifetime_seconds(&self, class: ClientLifetimeClass) -> i64 {
        match class {
            ClientLifetimeClass::BrowserPlayback => self.browser_capability_seconds,
            ClientLifetimeClass::NativeMedia => self.native_capability_seconds,
        }
    }

    /// Calculate expiry, capped by a non-null identity access ceiling.
    ///
    /// # Errors
    ///
    /// Returns an authorization-expired error if no positive lifetime remains.
    pub fn capability_expires_at(
        &self,
        class: ClientLifetimeClass,
        now: i64,
        access_expires_at: Option<i64>,
    ) -> Result<i64> {
        self.validate()?;
        let policy_expiry = now
            .checked_add(self.capability_lifetime_seconds(class))
            .ok_or_else(|| {
                ControllerError::new(
                    ControllerErrorCode::InvalidControllerState,
                    ControllerStage::CapabilityIssuance,
                    "capability expiry overflowed the exact timestamp domain",
                )
            })?;
        let expires_at =
            access_expires_at.map_or(policy_expiry, |ceiling| ceiling.min(policy_expiry));
        if expires_at <= now {
            return Err(ControllerError::new(
                ControllerErrorCode::AuthorizationExpired,
                ControllerStage::IdentityAuthorization,
                "identity access has no remaining capability lifetime",
            ));
        }
        Ok(expires_at)
    }

    /// Return a browser exchange expiry capped by the underlying capability.
    ///
    /// # Errors
    ///
    /// Returns an error if the capability already has no positive lifetime.
    pub fn exchange_expires_at(&self, now: i64, capability_expires_at: i64) -> Result<i64> {
        let policy_expiry = now
            .checked_add(self.browser_exchange_seconds)
            .ok_or_else(|| {
                ControllerError::new(
                    ControllerErrorCode::InvalidControllerState,
                    ControllerStage::BrowserExchange,
                    "exchange expiry overflowed the exact timestamp domain",
                )
            })?;
        let expires_at = policy_expiry.min(capability_expires_at);
        if expires_at <= now {
            return Err(ControllerError::new(
                ControllerErrorCode::AuthorizationExpired,
                ControllerStage::BrowserExchange,
                "underlying capability expires before an exchange can be used",
            ));
        }
        Ok(expires_at)
    }

    /// Return a deterministic jittered renewal second bounded before expiry.
    #[must_use]
    pub fn renewal_at(
        &self,
        class: ClientLifetimeClass,
        issued_at: i64,
        expires_at: i64,
        stable_seed: &[u8],
    ) -> i64 {
        let nominal = match class {
            ClientLifetimeClass::BrowserPlayback => self.browser_renewal_seconds,
            ClientLifetimeClass::NativeMedia => self.native_renewal_seconds,
        };
        let digest = Sha256::digest(stable_seed);
        let span = self.renewal_jitter_seconds * 2 + 1;
        let sample = u16::from_be_bytes([digest[0], digest[1]]);
        let jitter = i64::from(sample) % span - self.renewal_jitter_seconds;
        let desired = issued_at.saturating_add(nominal).saturating_add(jitter);
        let last_safe_second = expires_at.saturating_sub(1);
        desired.clamp(issued_at, last_safe_second.max(issued_at))
    }

    /// Return the delayed-activation lower bound used to absorb permitted skew.
    #[must_use]
    pub const fn not_before(&self, issued_at: i64) -> i64 {
        issued_at.saturating_sub(self.clock_skew_seconds)
    }
}
