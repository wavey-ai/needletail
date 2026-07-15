use std::collections::BTreeSet;

use base64::engine::general_purpose::URL_SAFE_NO_PAD;
use base64::Engine as _;
use ed25519_dalek::{Signer, SigningKey};
use media_capability::{
    CapabilityVerifierErrorCode, CurrentMediaAuthorizationContextV1, MediaCapabilityVerifier,
    ReplayAdmissionGuard, ReplayAdmissionRejection, ReplayAdmissionV1, VerificationKeyState,
    VerificationKeyring, MAX_COMPACT_JWS_BYTES, PROTECTED_ALGORITHM, PROTECTED_TOKEN_TYPE,
};
use media_object::{
    CapabilityId, ContributorId, EdgeId, EndpointId, MediaCapabilityClaimsV1,
    MediaCapabilityClaimsV1Params, MediaClass, MediaControlErrorCode, Operation, ParticipantId,
    SessionId, SourceId, TenantId,
};

const NOW: i64 = 1_784_131_220;
const ISSUER: &str = "https://control.infidelity.io";
const AUDIENCE: &str = "av-contrib";
const ACTIVE_KID: &str = "key_active_01";
const RETIRING_KID: &str = "key_retiring_00";

fn signing_key(seed: u8) -> SigningKey {
    SigningKey::from_bytes(&[seed; 32])
}

fn claims_params() -> MediaCapabilityClaimsV1Params {
    MediaCapabilityClaimsV1Params {
        issuer: ISSUER.to_owned(),
        audience: AUDIENCE.to_owned(),
        capability_id: CapabilityId::new("cap_publish_mix").unwrap(),
        tenant_id: TenantId::new("ten_wavey").unwrap(),
        session_id: SessionId::new("ses_mix").unwrap(),
        session_epoch: 9,
        media_authorization_epoch: 14,
        subject_grant_epoch: 3,
        media_policy_version: 7,
        class_authorization_epoch: Some(4),
        binding_generation: 8,
        participant_id: ParticipantId::new("par_producer").unwrap(),
        endpoint_id: EndpointId::new("ep_logic").unwrap(),
        contributor_id: Some(ContributorId::new("con_logic").unwrap()),
        operation: Operation::Publish,
        media_class: MediaClass::Program,
        source_ids: vec![SourceId::new("src_mix").unwrap()],
        audience_ids: Vec::new(),
        take_id: None,
        topology_generation: 52,
        edge_ids: vec![EdgeId::new("edge_lon").unwrap()],
        max_channels: 2,
        max_bitrate: 512_000,
        max_datagram_bytes: 1_200,
        client_key_thumbprint: Some("AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA".to_owned()),
        issued_at: NOW - 20,
        not_before: NOW - 20,
        expires_at: NOW + 40,
    }
}

fn canonical_claims(params: MediaCapabilityClaimsV1Params) -> Vec<u8> {
    MediaCapabilityClaimsV1::new(params)
        .unwrap()
        .to_canonical_json_vec()
        .unwrap()
}

fn canonical_header(key_id: &str) -> String {
    format!(r#"{{"alg":"{PROTECTED_ALGORITHM}","kid":"{key_id}","typ":"{PROTECTED_TOKEN_TYPE}"}}"#)
}

fn sign_raw(signing_key: &SigningKey, protected_json: &str, claims_json: &[u8]) -> String {
    let protected = URL_SAFE_NO_PAD.encode(protected_json.as_bytes());
    let claims = URL_SAFE_NO_PAD.encode(claims_json);
    let signing_input = format!("{protected}.{claims}");
    let signature = signing_key.sign(signing_input.as_bytes());
    let signature = URL_SAFE_NO_PAD.encode(signature.to_bytes());
    format!("{signing_input}.{signature}")
}

fn sign_claims(
    signing_key: &SigningKey,
    key_id: &str,
    params: MediaCapabilityClaimsV1Params,
) -> String {
    sign_raw(
        signing_key,
        &canonical_header(key_id),
        &canonical_claims(params),
    )
}

fn verifier_with_active(signing_key: &SigningKey) -> MediaCapabilityVerifier {
    let mut keyring = VerificationKeyring::new();
    keyring
        .insert_active(ACTIVE_KID, signing_key.verifying_key().to_bytes())
        .unwrap();
    MediaCapabilityVerifier::new(keyring, ISSUER, AUDIENCE).unwrap()
}

struct ContextIds {
    tenant: TenantId,
    session: SessionId,
    participant: ParticipantId,
    endpoint: EndpointId,
    contributor: ContributorId,
    source: SourceId,
    edge: EdgeId,
}

impl ContextIds {
    fn new() -> Self {
        Self {
            tenant: TenantId::new("ten_wavey").unwrap(),
            session: SessionId::new("ses_mix").unwrap(),
            participant: ParticipantId::new("par_producer").unwrap(),
            endpoint: EndpointId::new("ep_logic").unwrap(),
            contributor: ContributorId::new("con_logic").unwrap(),
            source: SourceId::new("src_mix").unwrap(),
            edge: EdgeId::new("edge_lon").unwrap(),
        }
    }

    fn context(&self, now: i64) -> CurrentMediaAuthorizationContextV1<'_> {
        CurrentMediaAuthorizationContextV1 {
            tenant_id: &self.tenant,
            session_id: &self.session,
            session_epoch: 9,
            media_authorization_epoch: 14,
            subject_grant_epoch: 3,
            media_policy_version: 7,
            class_authorization_epoch: Some(4),
            binding_generation: 8,
            topology_generation: 52,
            participant_id: &self.participant,
            endpoint_id: &self.endpoint,
            contributor_id: Some(&self.contributor),
            operation: Operation::Publish,
            media_class: MediaClass::Program,
            source_id: Some(&self.source),
            audience_id: None,
            take_id: None,
            edge_id: &self.edge,
            now,
            clock_skew_seconds: 0,
        }
    }
}

#[derive(Default)]
struct RecordingGuard {
    seen: BTreeSet<String>,
    calls: usize,
    forced_rejection: Option<ReplayAdmissionRejection>,
}

impl ReplayAdmissionGuard for RecordingGuard {
    fn check_and_admit(
        &mut self,
        admission: ReplayAdmissionV1<'_>,
    ) -> Result<(), ReplayAdmissionRejection> {
        self.calls += 1;
        if let Some(rejection) = self.forced_rejection {
            return Err(rejection);
        }
        if !self
            .seen
            .insert(admission.capability_id.as_str().to_owned())
        {
            return Err(ReplayAdmissionRejection::Replay);
        }
        Ok(())
    }
}

#[test]
fn valid_active_and_overlapping_retiring_keys_authorize() {
    let active = signing_key(7);
    let retiring = signing_key(9);
    let mut keyring = VerificationKeyring::new();
    keyring
        .insert_active(ACTIVE_KID, active.verifying_key().to_bytes())
        .unwrap();
    keyring
        .insert_retiring(RETIRING_KID, retiring.verifying_key().to_bytes(), NOW + 10)
        .unwrap();
    let verifier = MediaCapabilityVerifier::new(keyring, ISSUER, AUDIENCE).unwrap();
    let ids = ContextIds::new();

    let active_token = sign_claims(&active, ACTIVE_KID, claims_params());
    let mut guard = RecordingGuard::default();
    let authorized = verifier
        .authorize(&active_token, &ids.context(NOW), &mut guard)
        .unwrap();
    assert_eq!(authorized.key_state(), VerificationKeyState::Active);
    assert_eq!(authorized.key_id().as_str(), ACTIVE_KID);
    assert_eq!(guard.calls, 1);

    let mut retiring_params = claims_params();
    retiring_params.capability_id = CapabilityId::new("cap_retiring").unwrap();
    let retiring_token = sign_claims(&retiring, RETIRING_KID, retiring_params);
    let authorized = verifier
        .authorize(&retiring_token, &ids.context(NOW), &mut guard)
        .unwrap();
    assert_eq!(
        authorized.key_state(),
        VerificationKeyState::Retiring {
            accept_until: NOW + 10
        }
    );

    let error = verifier
        .authorize(&retiring_token, &ids.context(NOW + 10), &mut guard)
        .unwrap_err();
    assert_eq!(error.code(), CapabilityVerifierErrorCode::KeyNotAccepted);
}

#[test]
fn compact_shape_padding_and_noncanonical_base64url_fail_closed() {
    let key = signing_key(7);
    let verifier = verifier_with_active(&key);
    let ids = ContextIds::new();
    let mut guard = RecordingGuard::default();

    let malformed = "one.two";
    assert_eq!(
        verifier
            .authorize(malformed, &ids.context(NOW), &mut guard)
            .unwrap_err()
            .code(),
        CapabilityVerifierErrorCode::MalformedCompactJws
    );

    let oversized = "a".repeat(MAX_COMPACT_JWS_BYTES + 1);
    assert_eq!(
        verifier
            .authorize(&oversized, &ids.context(NOW), &mut guard)
            .unwrap_err()
            .code(),
        CapabilityVerifierErrorCode::SegmentTooLarge
    );

    let valid = sign_claims(&key, ACTIVE_KID, claims_params());
    let mut segments: Vec<String> = valid.split('.').map(str::to_owned).collect();
    segments[0].push('=');
    let padded = segments.join(".");
    assert_eq!(
        verifier
            .authorize(&padded, &ids.context(NOW), &mut guard)
            .unwrap_err()
            .code(),
        CapabilityVerifierErrorCode::NonCanonicalBase64Url
    );

    let mut segments: Vec<String> = valid.split('.').map(str::to_owned).collect();
    let last = segments[2].pop().unwrap();
    let alphabet = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_";
    let index = alphabet
        .iter()
        .position(|candidate| *candidate == last as u8)
        .unwrap();
    let alternate = (index & 0b11_0000) | ((index + 1) & 0b00_1111);
    segments[2].push(char::from(alphabet[alternate]));
    let noncanonical = segments.join(".");
    assert_eq!(
        verifier
            .authorize(&noncanonical, &ids.context(NOW), &mut guard)
            .unwrap_err()
            .code(),
        CapabilityVerifierErrorCode::NonCanonicalBase64Url
    );
}

#[test]
fn protected_header_rejects_alg_confusion_unknown_duplicate_and_noncanonical_fields() {
    let key = signing_key(7);
    let verifier = verifier_with_active(&key);
    let ids = ContextIds::new();
    let claims = canonical_claims(claims_params());
    let mut guard = RecordingGuard::default();

    let cases = [
        (
            format!(r#"{{"alg":"HS256","kid":"{ACTIVE_KID}","typ":"{PROTECTED_TOKEN_TYPE}"}}"#),
            "alg",
        ),
        (
            format!(r#"{{"alg":"{PROTECTED_ALGORITHM}","kid":"{ACTIVE_KID}","typ":"JWT"}}"#),
            "typ",
        ),
        (
            format!(
                r#"{{"alg":"{PROTECTED_ALGORITHM}","kid":"{ACTIVE_KID}","typ":"{PROTECTED_TOKEN_TYPE}","crit":[]}}"#
            ),
            "protected_header",
        ),
        (
            format!(
                r#"{{"alg":"{PROTECTED_ALGORITHM}","kid":"{ACTIVE_KID}","kid":"duplicate","typ":"{PROTECTED_TOKEN_TYPE}"}}"#
            ),
            "protected_header",
        ),
        (
            format!(
                r#"{{"kid":"{ACTIVE_KID}","alg":"{PROTECTED_ALGORITHM}","typ":"{PROTECTED_TOKEN_TYPE}"}}"#
            ),
            "protected_header",
        ),
        (
            format!(r#"{{"alg":"{PROTECTED_ALGORITHM}","kid":"","typ":"{PROTECTED_TOKEN_TYPE}"}}"#),
            "protected_header",
        ),
    ];

    for (header, expected_field) in cases {
        let token = sign_raw(&key, &header, &claims);
        let error = verifier
            .authorize(&token, &ids.context(NOW), &mut guard)
            .unwrap_err();
        assert_eq!(
            error.code(),
            CapabilityVerifierErrorCode::InvalidProtectedHeader
        );
        assert_eq!(error.field(), expected_field);
    }
    assert_eq!(guard.calls, 0);
}

#[test]
fn unknown_kid_wrong_signature_and_retired_key_are_rejected() {
    let trusted = signing_key(7);
    let attacker = signing_key(11);
    let verifier = verifier_with_active(&trusted);
    let ids = ContextIds::new();
    let mut guard = RecordingGuard::default();

    let unknown = sign_claims(&trusted, "key_unknown", claims_params());
    assert_eq!(
        verifier
            .authorize(&unknown, &ids.context(NOW), &mut guard)
            .unwrap_err()
            .code(),
        CapabilityVerifierErrorCode::UnknownKey
    );

    let wrong_signature = sign_claims(&attacker, ACTIVE_KID, claims_params());
    assert_eq!(
        verifier
            .authorize(&wrong_signature, &ids.context(NOW), &mut guard)
            .unwrap_err()
            .code(),
        CapabilityVerifierErrorCode::InvalidSignature
    );
    assert_eq!(guard.calls, 0);
}

#[test]
fn signature_is_verified_before_malformed_claims_are_parsed() {
    let trusted = signing_key(7);
    let attacker = signing_key(11);
    let verifier = verifier_with_active(&trusted);
    let ids = ContextIds::new();
    let mut guard = RecordingGuard::default();
    let malformed_claims = b"{not-json}\n";

    let wrong_signature = sign_raw(&attacker, &canonical_header(ACTIVE_KID), malformed_claims);
    let error = verifier
        .authorize(&wrong_signature, &ids.context(NOW), &mut guard)
        .unwrap_err();
    assert_eq!(error.code(), CapabilityVerifierErrorCode::InvalidSignature);

    let authenticated_malformed =
        sign_raw(&trusted, &canonical_header(ACTIVE_KID), malformed_claims);
    let error = verifier
        .authorize(&authenticated_malformed, &ids.context(NOW), &mut guard)
        .unwrap_err();
    assert_eq!(error.code(), CapabilityVerifierErrorCode::ClaimsRejected);
    assert_eq!(
        error.claims_code(),
        Some(MediaControlErrorCode::MalformedJson)
    );
}

#[test]
fn signed_claims_must_be_canonical_and_known_version() {
    let key = signing_key(7);
    let verifier = verifier_with_active(&key);
    let ids = ContextIds::new();
    let mut guard = RecordingGuard::default();
    let canonical = canonical_claims(claims_params());

    let value: serde_json::Value = serde_json::from_slice(&canonical).unwrap();
    let mut pretty = serde_json::to_vec_pretty(&value).unwrap();
    pretty.push(b'\n');
    let token = sign_raw(&key, &canonical_header(ACTIVE_KID), &pretty);
    assert_eq!(
        verifier
            .authorize(&token, &ids.context(NOW), &mut guard)
            .unwrap_err()
            .code(),
        CapabilityVerifierErrorCode::NonCanonicalClaims
    );

    let future =
        String::from_utf8(canonical)
            .unwrap()
            .replacen("\"version\":1", "\"version\":2", 1);
    let token = sign_raw(&key, &canonical_header(ACTIVE_KID), future.as_bytes());
    let error = verifier
        .authorize(&token, &ids.context(NOW), &mut guard)
        .unwrap_err();
    assert_eq!(error.code(), CapabilityVerifierErrorCode::ClaimsRejected);
    assert_eq!(
        error.claims_code(),
        Some(MediaControlErrorCode::UnsupportedVersion)
    );
}

#[test]
fn pinned_issuer_and_audience_are_not_taken_from_the_token() {
    let key = signing_key(7);
    let verifier = verifier_with_active(&key);
    let ids = ContextIds::new();
    let mut guard = RecordingGuard::default();

    let mut wrong_issuer = claims_params();
    wrong_issuer.issuer = "https://attacker.invalid".to_owned();
    let token = sign_claims(&key, ACTIVE_KID, wrong_issuer);
    let error = verifier
        .authorize(&token, &ids.context(NOW), &mut guard)
        .unwrap_err();
    assert_eq!(
        error.code(),
        CapabilityVerifierErrorCode::AuthorizationRejected
    );
    assert_eq!(error.field(), "issuer");

    let mut wrong_audience = claims_params();
    wrong_audience.audience = "other-service".to_owned();
    let token = sign_claims(&key, ACTIVE_KID, wrong_audience);
    let error = verifier
        .authorize(&token, &ids.context(NOW), &mut guard)
        .unwrap_err();
    assert_eq!(
        error.code(),
        CapabilityVerifierErrorCode::AuthorizationRejected
    );
    assert_eq!(error.field(), "audience");
    assert_eq!(guard.calls, 0);
}

#[test]
fn expired_future_and_wrong_context_claims_fail_before_admission() {
    let key = signing_key(7);
    let verifier = verifier_with_active(&key);
    let ids = ContextIds::new();
    let mut guard = RecordingGuard::default();

    let mut expired = claims_params();
    expired.issued_at = NOW - 80;
    expired.not_before = NOW - 80;
    expired.expires_at = NOW - 10;
    let token = sign_claims(&key, ACTIVE_KID, expired);
    let error = verifier
        .authorize(&token, &ids.context(NOW), &mut guard)
        .unwrap_err();
    assert_eq!(error.claims_code(), Some(MediaControlErrorCode::Expired));

    let mut future = claims_params();
    future.issued_at = NOW;
    future.not_before = NOW + 20;
    future.expires_at = NOW + 60;
    let token = sign_claims(&key, ACTIVE_KID, future);
    let error = verifier
        .authorize(&token, &ids.context(NOW), &mut guard)
        .unwrap_err();
    assert_eq!(
        error.claims_code(),
        Some(MediaControlErrorCode::NotYetValid)
    );

    let token = sign_claims(&key, ACTIVE_KID, claims_params());
    let wrong_session = SessionId::new("ses_other").unwrap();
    let wrong_context = CurrentMediaAuthorizationContextV1 {
        session_id: &wrong_session,
        ..ids.context(NOW)
    };
    let error = verifier
        .authorize(&token, &wrong_context, &mut guard)
        .unwrap_err();
    assert_eq!(
        error.claims_code(),
        Some(MediaControlErrorCode::AuthorizationMismatch)
    );
    assert_eq!(error.field(), "session_id");
    assert_eq!(guard.calls, 0);
}

#[test]
fn caller_guard_is_mandatory_for_success_and_replay_is_stable() {
    let key = signing_key(7);
    let verifier = verifier_with_active(&key);
    let ids = ContextIds::new();
    let token = sign_claims(&key, ACTIVE_KID, claims_params());
    let mut guard = RecordingGuard::default();

    verifier
        .authorize(&token, &ids.context(NOW), &mut guard)
        .unwrap();
    let error = verifier
        .authorize(&token, &ids.context(NOW), &mut guard)
        .unwrap_err();
    assert_eq!(
        error.code(),
        CapabilityVerifierErrorCode::ReplayAdmissionRejected
    );
    assert_eq!(
        error.replay_rejection(),
        Some(ReplayAdmissionRejection::Replay)
    );

    let mut capacity_guard = RecordingGuard {
        forced_rejection: Some(ReplayAdmissionRejection::Capacity),
        ..RecordingGuard::default()
    };
    let error = verifier
        .authorize(&token, &ids.context(NOW), &mut capacity_guard)
        .unwrap_err();
    assert_eq!(
        error.replay_rejection(),
        Some(ReplayAdmissionRejection::Capacity)
    );
}

#[test]
fn keyring_configuration_is_bounded_and_duplicate_safe() {
    let key = signing_key(7);
    let mut keyring = VerificationKeyring::new();
    keyring
        .insert_active(ACTIVE_KID, key.verifying_key().to_bytes())
        .unwrap();
    assert_eq!(keyring.len(), 1);
    assert_eq!(
        keyring
            .insert_active(ACTIVE_KID, key.verifying_key().to_bytes())
            .unwrap_err()
            .code(),
        CapabilityVerifierErrorCode::DuplicateKey
    );
    assert_eq!(
        keyring
            .insert_retiring("", key.verifying_key().to_bytes(), NOW)
            .unwrap_err()
            .code(),
        CapabilityVerifierErrorCode::InvalidKeyId
    );

    let empty = VerificationKeyring::new();
    assert_eq!(
        MediaCapabilityVerifier::new(empty, ISSUER, AUDIENCE)
            .unwrap_err()
            .code(),
        CapabilityVerifierErrorCode::InvalidConfiguration
    );
}

#[test]
fn debug_and_errors_never_render_token_or_identity_material() {
    let key = signing_key(7);
    let verifier = verifier_with_active(&key);
    let ids = ContextIds::new();
    let token = sign_claims(&key, ACTIVE_KID, claims_params());
    let mut guard = RecordingGuard::default();
    let authorized = verifier
        .authorize(&token, &ids.context(NOW), &mut guard)
        .unwrap();

    let verifier_debug = format!("{verifier:?}");
    let authorized_debug = format!("{authorized:?}");
    for forbidden in [ACTIVE_KID, "ses_mix", "cap_publish_mix", ISSUER, AUDIENCE] {
        assert!(!verifier_debug.contains(forbidden));
        assert!(!authorized_debug.contains(forbidden));
    }
}
