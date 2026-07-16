mod support;

use std::collections::BTreeSet;
use std::sync::{Arc, Barrier};
use std::thread;

use base64::engine::general_purpose::URL_SAFE_NO_PAD;
use base64::Engine as _;
use capability_controller::{
    AuthorizationMode, BrokerCaller, Codec, ControllerErrorCode, ControllerErrorDetail,
    DesiredMediaState, Ed25519CapabilityIssuer, ExchangeConsumeRequest, ExchangeRejection,
    FeatureGates, IdentityAuthorizationClient, IdentityAuthorizationTransport,
    IdentityBoundaryErrorCode, IdentityTransportError, IssuedAuthorization, P01HmacIdentityClient,
    RetiringPublicKey, RouteRefusalReason, SignedIdentityRequest, SignedIdentityResponse,
};
use ed25519_dalek::SigningKey;
use hmac::{Hmac, Mac as _};
use media_capability::{
    CapabilityVerifierErrorCode, CurrentMediaAuthorizationContextV1, MediaCapabilityVerifier,
    ReplayAdmissionGuard, ReplayAdmissionRejection, ReplayAdmissionV1, VerificationKeyState,
    VerificationKeyring,
};
use media_object::{
    AudienceId, EdgeId, LiveMonitorTransport, MediaAuthorizationRequestV1, MediaCapabilityClaimsV1,
    MediaClass, MediaEndpointTransport, Operation, SessionId, TakeId,
};
use sha2::{Digest, Sha256};

use support::{
    browser_request, candidate, desired, fact, harness, harness_with_gates, native_request,
    talkback_candidate, talkback_fact, talkback_request, CounterEntropy, TestIds, NOW, THUMBPRINT,
};

type HmacSha256 = Hmac<Sha256>;
const HMAC_KEY: [u8; 32] = [19; 32];

struct FixtureIdentityTransport {
    tamper_response_signature: bool,
}

impl IdentityAuthorizationTransport for FixtureIdentityTransport {
    fn send(
        &self,
        request: &SignedIdentityRequest,
    ) -> Result<SignedIdentityResponse, IdentityTransportError> {
        assert_eq!(
            request.body(),
            include_bytes!("fixtures/p01-media-authorization-request.json")
        );
        let canonical_request = format!(
            "NEEDLETAIL-HMAC-SHA256-V1\n{}\n{}\n{}\n{}\nPOST\n{}\n{}",
            request.service_id(),
            request.audience(),
            request.timestamp(),
            request.nonce(),
            request.path(),
            URL_SAFE_NO_PAD.encode(Sha256::digest(request.body()))
        );
        let mut request_mac = HmacSha256::new_from_slice(&HMAC_KEY).unwrap();
        request_mac.update(canonical_request.as_bytes());
        request_mac
            .verify_slice(&URL_SAFE_NO_PAD.decode(request.signature()).unwrap())
            .unwrap();

        let body = include_bytes!("fixtures/p01-media-authorization-fact.json").to_vec();
        let canonical_response = format!(
            "NEEDLETAIL-HMAC-SHA256-RESPONSE-V1\n{}\n{}\n{}\n200\n{}\n{}",
            request.audience(),
            NOW,
            request.nonce(),
            request.path(),
            URL_SAFE_NO_PAD.encode(Sha256::digest(&body))
        );
        let mut response_mac = HmacSha256::new_from_slice(&HMAC_KEY).unwrap();
        response_mac.update(canonical_response.as_bytes());
        let mut signature = URL_SAFE_NO_PAD.encode(response_mac.finalize().into_bytes());
        if self.tamper_response_signature {
            let replacement = if signature.starts_with('A') { "B" } else { "A" };
            signature.replace_range(..1, replacement);
        }
        Ok(SignedIdentityResponse::new(
            200,
            body,
            request.audience(),
            NOW,
            request.nonce(),
            signature,
        ))
    }
}

#[derive(Default)]
struct ReplayGuard {
    seen: BTreeSet<String>,
}

impl ReplayAdmissionGuard for ReplayGuard {
    fn check_and_admit(
        &mut self,
        admission: ReplayAdmissionV1<'_>,
    ) -> Result<(), ReplayAdmissionRejection> {
        if self
            .seen
            .insert(admission.capability_id.as_str().to_owned())
        {
            Ok(())
        } else {
            Err(ReplayAdmissionRejection::Replay)
        }
    }
}

fn talkback_desired<'a>(
    ids: &'a TestIds,
    routes: &'a [capability_controller::RouteCandidate],
    class_authorization_epoch: Option<u64>,
) -> DesiredMediaState<'a> {
    DesiredMediaState {
        tenant_id: &ids.tenant,
        session_id: &ids.session,
        topology_generation: 52,
        class_authorization_epoch,
        contributor_id: Some(&ids.contributor),
        require_independent_repair: false,
        route_candidates: routes,
    }
}

#[test]
fn signed_identity_boundary_consumes_exact_p01_fixtures_and_rejects_tampering() {
    let request = MediaAuthorizationRequestV1::from_json_slice(include_bytes!(
        "fixtures/p01-media-authorization-request.json"
    ))
    .unwrap();
    let session = SessionId::new("ses_mix").unwrap();
    let client = P01HmacIdentityClient::new(
        FixtureIdentityTransport {
            tamper_response_signature: false,
        },
        CounterEntropy::default(),
        "needletail",
        "needletail-media-control-production",
        HMAC_KEY,
    )
    .unwrap();
    let fact = client.authorize(&session, &request, NOW).unwrap();
    assert_eq!(fact.session_id(), &session);
    assert_eq!(fact.subject_grant_epoch(), 3);

    let tampered = P01HmacIdentityClient::new(
        FixtureIdentityTransport {
            tamper_response_signature: true,
        },
        CounterEntropy::default(),
        "needletail",
        "needletail-media-control-production",
        HMAC_KEY,
    )
    .unwrap();
    let error = tampered.authorize(&session, &request, NOW).unwrap_err();
    assert_eq!(error.code(), ControllerErrorCode::IdentityResponseInvalid);
    assert_eq!(
        error.detail(),
        Some(ControllerErrorDetail::Identity(
            IdentityBoundaryErrorCode::ResponseAuthenticationFailed
        ))
    );
    let rendered = format!("{error:?}");
    assert!(!rendered.contains("sub_zeroth_01"));
    assert!(!rendered.contains("ses_mix"));
}

#[test]
fn native_issuer_matches_the_existing_strict_verifier_and_context_fences() {
    let harness = harness(Operation::Publish);
    let request = native_request(&harness.ids);
    let routes = vec![candidate(
        &harness.ids,
        MediaEndpointTransport::NativeDatagram,
        harness.ids.primary_edge.clone(),
        18,
    )];
    let issued = harness
        .controller
        .issue(
            BrokerCaller::NativeBroker,
            &request,
            &desired(&harness.ids, &routes, true),
            NOW,
        )
        .unwrap();
    let IssuedAuthorization::NativeMedia(native) = issued else {
        panic!("expected native authorization");
    };
    assert_eq!(native.expires_at(), NOW + 60);
    assert!((NOW + 35..=NOW + 45).contains(&native.renew_at()));
    assert_eq!(native.endpoints()[0].edge_id(), &harness.ids.primary_edge);

    let mut keyring = VerificationKeyring::new();
    keyring
        .insert_active("key_active_01", harness.verification_key)
        .unwrap();
    let verifier =
        MediaCapabilityVerifier::new(keyring, "https://control.needletail.test", "av-contrib")
            .unwrap();
    let context = CurrentMediaAuthorizationContextV1 {
        tenant_id: &harness.ids.tenant,
        session_id: &harness.ids.session,
        session_epoch: 9,
        media_authorization_epoch: 14,
        subject_grant_epoch: 3,
        media_policy_version: 7,
        class_authorization_epoch: Some(4),
        binding_generation: 8,
        topology_generation: 52,
        participant_id: &harness.ids.participant,
        endpoint_id: &harness.ids.endpoint,
        contributor_id: Some(&harness.ids.contributor),
        operation: Operation::Publish,
        media_class: MediaClass::Program,
        source_id: Some(&harness.ids.source),
        audience_id: None,
        take_id: None,
        edge_id: &harness.ids.primary_edge,
        now: NOW,
        clock_skew_seconds: 5,
    };
    verifier
        .authorize(
            native.capability().expose(),
            &context,
            &mut ReplayGuard::default(),
        )
        .unwrap();

    let wrong_generation = CurrentMediaAuthorizationContextV1 {
        topology_generation: 53,
        ..context
    };
    let error = verifier
        .authorize(
            native.capability().expose(),
            &wrong_generation,
            &mut ReplayGuard::default(),
        )
        .unwrap_err();
    assert_eq!(
        error.code(),
        CapabilityVerifierErrorCode::AuthorizationRejected
    );
    assert_eq!(error.field(), "topology_generation");

    let expired = CurrentMediaAuthorizationContextV1 {
        now: NOW + 60,
        clock_skew_seconds: 0,
        ..context
    };
    assert!(verifier
        .authorize(
            native.capability().expose(),
            &expired,
            &mut ReplayGuard::default(),
        )
        .is_err());
}

#[test]
fn browser_exchange_has_independent_15_second_consume_and_90_second_lease_lifetimes() {
    let harness = harness(Operation::Subscribe);
    let request = browser_request(&harness.ids);
    let routes = vec![candidate(
        &harness.ids,
        MediaEndpointTransport::WebtransportDatagram,
        harness.ids.primary_edge.clone(),
        12,
    )];
    let issued = harness
        .controller
        .issue(
            BrokerCaller::SessionsBroker,
            &request,
            &desired(&harness.ids, &routes, false),
            NOW,
        )
        .unwrap();
    let IssuedAuthorization::BrowserPlayback(browser) = issued else {
        panic!("expected browser authorization");
    };
    assert_eq!(browser.exchange().expires_at(), NOW + 15);
    assert_eq!(browser.authorization_expires_at(), NOW + 90);
    let token = browser.exchange().token().expose().to_owned();
    let public_json = serde_json::to_string(&browser).unwrap();
    for forbidden in [
        "capability",
        "endpoints",
        "media-lon.needletail.test",
        "src_mix",
        "ep_logic",
        THUMBPRINT,
    ] {
        assert!(!public_json.contains(forbidden));
    }

    let wrong_proof = harness
        .controller
        .consume_exchange(&ExchangeConsumeRequest {
            token: &token,
            edge_id: &harness.ids.primary_edge,
            endpoint_id: &harness.ids.endpoint,
            endpoint_proof_thumbprint: "BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB",
            now: NOW + 1,
        })
        .unwrap_err();
    assert_eq!(
        wrong_proof.detail(),
        Some(ControllerErrorDetail::Exchange(
            ExchangeRejection::WrongEndpointProof
        ))
    );

    let lease = harness
        .controller
        .consume_exchange(&ExchangeConsumeRequest {
            token: &token,
            edge_id: &harness.ids.primary_edge,
            endpoint_id: &harness.ids.endpoint,
            endpoint_proof_thumbprint: THUMBPRINT,
            now: NOW + 1,
        })
        .unwrap();
    assert_eq!(lease.expires_at(), NOW + 90);
    assert_eq!(lease.descriptor().expires_at(), NOW + 90);
    assert!(!public_json.contains(lease.capability().expose()));

    let other_edge = EdgeId::new("edge_other").unwrap();
    let replay = harness
        .controller
        .consume_exchange(&ExchangeConsumeRequest {
            token: &token,
            edge_id: &other_edge,
            endpoint_id: &harness.ids.endpoint,
            endpoint_proof_thumbprint: THUMBPRINT,
            now: NOW + 2,
        })
        .unwrap_err();
    assert_eq!(
        replay.detail(),
        Some(ControllerErrorDetail::Exchange(
            ExchangeRejection::AlreadyConsumed
        ))
    );
    assert!(!format!("{browser:?}{replay:?}").contains(&token));
}

#[test]
fn browser_talkback_publish_uses_proof_bound_exchange_and_talkback_epoch() {
    let harness = harness(Operation::Publish);
    harness.identity.replace(talkback_fact(
        &harness.ids,
        Operation::Publish,
        5,
        9,
        NOW,
        Some(NOW + 300),
    ));
    let request = talkback_request(
        &harness.ids,
        AuthorizationMode::BrowserTalkback,
        Operation::Publish,
    );
    let routes = vec![talkback_candidate(
        &harness.ids,
        MediaEndpointTransport::WebtransportDatagram,
        harness.ids.primary_edge.clone(),
        7,
    )];
    let desired = DesiredMediaState {
        tenant_id: &harness.ids.tenant,
        session_id: &harness.ids.session,
        topology_generation: 52,
        class_authorization_epoch: Some(42),
        contributor_id: Some(&harness.ids.contributor),
        require_independent_repair: false,
        route_candidates: &routes,
    };

    let issued = harness
        .controller
        .issue(BrokerCaller::SessionsBroker, &request, &desired, NOW)
        .unwrap();
    let IssuedAuthorization::BrowserTalkback(browser) = issued else {
        panic!("expected browser talkback authorization");
    };
    assert_eq!(browser.exchange().expires_at(), NOW + 15);
    assert_eq!(browser.authorization_expires_at(), NOW + 90);

    let lease = harness
        .controller
        .consume_exchange(&ExchangeConsumeRequest {
            token: browser.exchange().token().expose(),
            edge_id: &harness.ids.primary_edge,
            endpoint_id: &harness.ids.endpoint,
            endpoint_proof_thumbprint: THUMBPRINT,
            now: NOW + 1,
        })
        .unwrap();
    assert_eq!(lease.operation(), Operation::Publish);
    assert_eq!(lease.media_class(), MediaClass::Talkback);

    let mut keyring = VerificationKeyring::new();
    keyring
        .insert_active("key_active_01", harness.verification_key)
        .unwrap();
    let verifier =
        MediaCapabilityVerifier::new(keyring, "https://control.needletail.test", "av-contrib")
            .unwrap();
    let authorized = verifier
        .authorize(
            lease.capability().expose(),
            &CurrentMediaAuthorizationContextV1 {
                tenant_id: &harness.ids.tenant,
                session_id: &harness.ids.session,
                session_epoch: 9,
                media_authorization_epoch: 14,
                subject_grant_epoch: 5,
                media_policy_version: 9,
                class_authorization_epoch: Some(42),
                binding_generation: 8,
                topology_generation: 52,
                participant_id: &harness.ids.participant,
                endpoint_id: &harness.ids.endpoint,
                contributor_id: Some(&harness.ids.contributor),
                operation: Operation::Publish,
                media_class: MediaClass::Talkback,
                source_id: None,
                audience_id: Some(&harness.ids.talkback_audience),
                take_id: None,
                edge_id: &harness.ids.primary_edge,
                now: NOW + 1,
                clock_skew_seconds: 5,
            },
            &mut ReplayGuard::default(),
        )
        .unwrap();
    assert_eq!(authorized.claims().class_authorization_epoch(), Some(42));
    assert!(authorized.claims().source_ids().is_empty());
    assert_eq!(
        authorized.claims().audience_ids(),
        std::slice::from_ref(&harness.ids.talkback_audience)
    );
}

#[test]
fn native_talkback_subscribe_uses_direct_short_lease_and_talkback_epoch() {
    let harness = harness(Operation::Subscribe);
    harness.identity.replace(talkback_fact(
        &harness.ids,
        Operation::Subscribe,
        6,
        10,
        NOW,
        Some(NOW + 300),
    ));
    let request = talkback_request(
        &harness.ids,
        AuthorizationMode::NativeMedia,
        Operation::Subscribe,
    );
    let routes = vec![talkback_candidate(
        &harness.ids,
        MediaEndpointTransport::NativeDatagram,
        harness.ids.primary_edge.clone(),
        6,
    )];
    let desired = DesiredMediaState {
        tenant_id: &harness.ids.tenant,
        session_id: &harness.ids.session,
        topology_generation: 52,
        class_authorization_epoch: Some(43),
        contributor_id: Some(&harness.ids.contributor),
        require_independent_repair: false,
        route_candidates: &routes,
    };

    let issued = harness
        .controller
        .issue(BrokerCaller::NativeBroker, &request, &desired, NOW)
        .unwrap();
    let IssuedAuthorization::NativeMedia(native) = issued else {
        panic!("expected native talkback authorization");
    };
    assert_eq!(native.expires_at(), NOW + 60);
    assert_eq!(native.endpoints()[0].edge_id(), &harness.ids.primary_edge);

    let mut keyring = VerificationKeyring::new();
    keyring
        .insert_active("key_active_01", harness.verification_key)
        .unwrap();
    let verifier =
        MediaCapabilityVerifier::new(keyring, "https://control.needletail.test", "av-mesh")
            .unwrap();
    let authorized = verifier
        .authorize(
            native.capability().expose(),
            &CurrentMediaAuthorizationContextV1 {
                tenant_id: &harness.ids.tenant,
                session_id: &harness.ids.session,
                session_epoch: 9,
                media_authorization_epoch: 14,
                subject_grant_epoch: 6,
                media_policy_version: 10,
                class_authorization_epoch: Some(43),
                binding_generation: 8,
                topology_generation: 52,
                participant_id: &harness.ids.participant,
                endpoint_id: &harness.ids.endpoint,
                contributor_id: None,
                operation: Operation::Subscribe,
                media_class: MediaClass::Talkback,
                source_id: None,
                audience_id: Some(&harness.ids.talkback_audience),
                take_id: None,
                edge_id: &harness.ids.primary_edge,
                now: NOW,
                clock_skew_seconds: 5,
            },
            &mut ReplayGuard::default(),
        )
        .unwrap();
    assert_eq!(authorized.claims().class_authorization_epoch(), Some(43));
}

#[test]
fn talkback_broker_rejects_wrong_mode_and_non_audience_scope_before_identity() {
    let harness = harness(Operation::Publish);
    let routes = vec![talkback_candidate(
        &harness.ids,
        MediaEndpointTransport::WebtransportDatagram,
        harness.ids.primary_edge.clone(),
        7,
    )];
    let desired = talkback_desired(&harness.ids, &routes, Some(42));
    let playback_mode = talkback_request(
        &harness.ids,
        AuthorizationMode::BrowserPlayback,
        Operation::Subscribe,
    );
    assert_eq!(
        harness
            .controller
            .issue(BrokerCaller::SessionsBroker, &playback_mode, &desired, NOW)
            .unwrap_err()
            .code(),
        ControllerErrorCode::InvalidRequest
    );

    let mut wrong_class = talkback_request(
        &harness.ids,
        AuthorizationMode::BrowserTalkback,
        Operation::Publish,
    );
    wrong_class.media_class = MediaClass::Program;
    assert_eq!(
        harness
            .controller
            .issue(BrokerCaller::SessionsBroker, &wrong_class, &desired, NOW)
            .unwrap_err()
            .code(),
        ControllerErrorCode::InvalidRequest
    );

    let mut source_scope = talkback_request(
        &harness.ids,
        AuthorizationMode::BrowserTalkback,
        Operation::Publish,
    );
    source_scope.source_ids.push(harness.ids.source.clone());
    assert_eq!(
        harness
            .controller
            .issue(BrokerCaller::SessionsBroker, &source_scope, &desired, NOW)
            .unwrap_err()
            .code(),
        ControllerErrorCode::InvalidRequest
    );

    let mut take_scope = talkback_request(
        &harness.ids,
        AuthorizationMode::BrowserTalkback,
        Operation::Publish,
    );
    take_scope.take_id = Some(TakeId::new("take_01").unwrap());
    assert_eq!(
        harness
            .controller
            .issue(BrokerCaller::SessionsBroker, &take_scope, &desired, NOW)
            .unwrap_err()
            .code(),
        ControllerErrorCode::InvalidRequest
    );

    assert_eq!(harness.identity.calls(), 0);
}

#[test]
fn talkback_broker_rejects_non_strict_format_and_missing_epoch_before_identity() {
    let harness = harness(Operation::Publish);
    let routes = vec![talkback_candidate(
        &harness.ids,
        MediaEndpointTransport::WebtransportDatagram,
        harness.ids.primary_edge.clone(),
        7,
    )];
    let desired = talkback_desired(&harness.ids, &routes, Some(42));

    let mut stereo = talkback_request(
        &harness.ids,
        AuthorizationMode::BrowserTalkback,
        Operation::Publish,
    );
    stereo.requested_channels = 2;
    assert_eq!(
        harness
            .controller
            .issue(BrokerCaller::SessionsBroker, &stereo, &desired, NOW)
            .unwrap_err()
            .code(),
        ControllerErrorCode::InvalidRequest
    );

    let mut pcm = talkback_request(
        &harness.ids,
        AuthorizationMode::BrowserTalkback,
        Operation::Publish,
    );
    pcm.client.requested_live_transport = LiveMonitorTransport::PcmIfAdmitted;
    pcm.client.supported_codecs = vec![Codec::Pcm];
    assert_eq!(
        harness
            .controller
            .issue(BrokerCaller::SessionsBroker, &pcm, &desired, NOW)
            .unwrap_err()
            .code(),
        ControllerErrorCode::InvalidRequest
    );

    let mut missing_format = talkback_request(
        &harness.ids,
        AuthorizationMode::BrowserTalkback,
        Operation::Publish,
    );
    missing_format.requested_frame_samples = None;
    assert_eq!(
        harness
            .controller
            .issue(BrokerCaller::SessionsBroker, &missing_format, &desired, NOW)
            .unwrap_err()
            .code(),
        ControllerErrorCode::InvalidRequest
    );

    let missing_epoch = talkback_desired(&harness.ids, &routes, None);
    let strict = talkback_request(
        &harness.ids,
        AuthorizationMode::BrowserTalkback,
        Operation::Publish,
    );
    assert_eq!(
        harness
            .controller
            .issue(BrokerCaller::SessionsBroker, &strict, &missing_epoch, NOW)
            .unwrap_err()
            .code(),
        ControllerErrorCode::InvalidControllerState
    );
    assert_eq!(harness.identity.calls(), 0);
}

#[test]
fn talkback_broker_rejects_multi_audience_publish_before_identity() {
    let harness = harness(Operation::Publish);
    let routes = vec![talkback_candidate(
        &harness.ids,
        MediaEndpointTransport::WebtransportDatagram,
        harness.ids.primary_edge.clone(),
        7,
    )];
    let desired = talkback_desired(&harness.ids, &routes, Some(42));
    let mut multi_publish = talkback_request(
        &harness.ids,
        AuthorizationMode::BrowserTalkback,
        Operation::Publish,
    );
    multi_publish
        .audience_ids
        .push(AudienceId::new("aud_session_cue").unwrap());
    assert_eq!(
        harness
            .controller
            .issue(BrokerCaller::SessionsBroker, &multi_publish, &desired, NOW)
            .unwrap_err()
            .code(),
        ControllerErrorCode::InvalidRequest
    );
    assert_eq!(harness.identity.calls(), 0);
}

#[test]
fn parallel_exchange_double_spend_has_exactly_one_success() {
    let harness = harness(Operation::Subscribe);
    let request = browser_request(&harness.ids);
    let routes = vec![candidate(
        &harness.ids,
        MediaEndpointTransport::WebtransportDatagram,
        harness.ids.primary_edge.clone(),
        12,
    )];
    let issued = harness
        .controller
        .issue(
            BrokerCaller::SessionsBroker,
            &request,
            &desired(&harness.ids, &routes, false),
            NOW,
        )
        .unwrap();
    let IssuedAuthorization::BrowserPlayback(browser) = issued else {
        panic!("expected browser authorization");
    };
    let token = browser.exchange().token().expose().to_owned();
    let barrier = Arc::new(Barrier::new(32));
    let handles: Vec<_> = (0..32)
        .map(|_| {
            let controller = Arc::clone(&harness.controller);
            let barrier = Arc::clone(&barrier);
            let token = token.clone();
            let edge = harness.ids.primary_edge.clone();
            let endpoint = harness.ids.endpoint.clone();
            thread::spawn(move || {
                barrier.wait();
                controller.consume_exchange(&ExchangeConsumeRequest {
                    token: &token,
                    edge_id: &edge,
                    endpoint_id: &endpoint,
                    endpoint_proof_thumbprint: THUMBPRINT,
                    now: NOW + 1,
                })
            })
        })
        .collect();
    let results: Vec<_> = handles
        .into_iter()
        .map(|handle| handle.join().unwrap())
        .collect();
    assert_eq!(results.iter().filter(|result| result.is_ok()).count(), 1);
    assert!(results
        .iter()
        .filter_map(|result| result.as_ref().err())
        .all(|error| {
            error.detail()
                == Some(ControllerErrorDetail::Exchange(
                    ExchangeRejection::AlreadyConsumed,
                ))
        }));
}

#[test]
fn public_browser_is_denied_before_identity_and_disagreement_is_redacted() {
    let harness = harness(Operation::Subscribe);
    let request = browser_request(&harness.ids);
    let routes = vec![candidate(
        &harness.ids,
        MediaEndpointTransport::WebtransportDatagram,
        harness.ids.primary_edge.clone(),
        12,
    )];
    let error = harness
        .controller
        .issue(
            BrokerCaller::PublicBrowser,
            &request,
            &desired(&harness.ids, &routes, false),
            NOW,
        )
        .unwrap_err();
    assert_eq!(error.code(), ControllerErrorCode::CallerNotAllowed);
    assert_eq!(harness.identity.calls(), 0);

    let mut wrong_ids = TestIds::new();
    wrong_ids.session = SessionId::new("ses_other").unwrap();
    harness.identity.replace(fact(
        &wrong_ids,
        Operation::Subscribe,
        3,
        7,
        NOW,
        Some(NOW + 300),
    ));
    let mismatch = harness
        .controller
        .issue(
            BrokerCaller::SessionsBroker,
            &request,
            &desired(&harness.ids, &routes, false),
            NOW,
        )
        .unwrap_err();
    assert_eq!(mismatch.code(), ControllerErrorCode::AuthorizationMismatch);
    let telemetry = serde_json::to_string(&harness.telemetry.events()).unwrap();
    for forbidden in [
        "ses_mix",
        "ses_other",
        "sub_zeroth_01",
        "ep_logic",
        "src_mix",
    ] {
        assert!(!telemetry.contains(forbidden));
        assert!(!format!("{mismatch:?}").contains(forbidden));
    }
}

#[test]
fn fail_closed_feature_gates_stop_before_identity_but_keep_public_jwks_available() {
    let harness = harness_with_gates(Operation::Publish, FeatureGates::default());
    let request = native_request(&harness.ids);
    let routes = vec![candidate(
        &harness.ids,
        MediaEndpointTransport::NativeDatagram,
        harness.ids.primary_edge.clone(),
        18,
    )];
    let error = harness
        .controller
        .issue(
            BrokerCaller::NativeBroker,
            &request,
            &desired(&harness.ids, &routes, false),
            NOW,
        )
        .unwrap_err();
    assert_eq!(error.code(), ControllerErrorCode::FeatureDisabled);
    assert_eq!(harness.identity.calls(), 0);
    assert_eq!(harness.controller.jwks(NOW).keys().len(), 1);
}

#[test]
fn expired_and_malformed_browser_exchanges_fail_closed_without_releasing_a_lease() {
    let harness = harness(Operation::Subscribe);
    let request = browser_request(&harness.ids);
    let routes = vec![candidate(
        &harness.ids,
        MediaEndpointTransport::WebtransportDatagram,
        harness.ids.primary_edge.clone(),
        12,
    )];
    let issued = harness
        .controller
        .issue(
            BrokerCaller::SessionsBroker,
            &request,
            &desired(&harness.ids, &routes, false),
            NOW,
        )
        .unwrap();
    let IssuedAuthorization::BrowserPlayback(browser) = issued else {
        panic!("expected browser authorization");
    };
    let expired = harness
        .controller
        .consume_exchange(&ExchangeConsumeRequest {
            token: browser.exchange().token().expose(),
            edge_id: &harness.ids.primary_edge,
            endpoint_id: &harness.ids.endpoint,
            endpoint_proof_thumbprint: THUMBPRINT,
            now: NOW + 15,
        })
        .unwrap_err();
    assert_eq!(
        expired.detail(),
        Some(ControllerErrorDetail::Exchange(ExchangeRejection::Expired))
    );

    let malformed = harness
        .controller
        .consume_exchange(&ExchangeConsumeRequest {
            token: "not-an-exchange-token",
            edge_id: &harness.ids.primary_edge,
            endpoint_id: &harness.ids.endpoint,
            endpoint_proof_thumbprint: THUMBPRINT,
            now: NOW + 1,
        })
        .unwrap_err();
    assert_eq!(
        malformed.detail(),
        Some(ControllerErrorDetail::Exchange(
            ExchangeRejection::MalformedToken
        ))
    );
}

#[test]
fn renewal_re_evaluates_identity_and_current_topology_instead_of_copying_old_claims() {
    let harness = harness(Operation::Publish);
    let request = native_request(&harness.ids);
    let original_routes = vec![candidate(
        &harness.ids,
        MediaEndpointTransport::NativeDatagram,
        harness.ids.primary_edge.clone(),
        18,
    )];
    harness
        .controller
        .issue(
            BrokerCaller::NativeBroker,
            &request,
            &desired(&harness.ids, &original_routes, false),
            NOW,
        )
        .unwrap();

    harness.identity.replace(fact(
        &harness.ids,
        Operation::Publish,
        4,
        8,
        NOW + 20,
        Some(NOW + 300),
    ));
    let mut current_route = candidate(
        &harness.ids,
        MediaEndpointTransport::NativeDatagram,
        harness.ids.primary_edge.clone(),
        18,
    );
    current_route.binding_generation = 9;
    current_route.topology_generation = 53;
    current_route.probe_observed_at = NOW + 20;
    let current_routes = vec![current_route];
    let current_desired = DesiredMediaState {
        tenant_id: &harness.ids.tenant,
        session_id: &harness.ids.session,
        topology_generation: 53,
        class_authorization_epoch: Some(4),
        contributor_id: Some(&harness.ids.contributor),
        require_independent_repair: false,
        route_candidates: &current_routes,
    };
    let renewed = harness
        .controller
        .renew(
            BrokerCaller::NativeBroker,
            &request,
            &current_desired,
            NOW + 20,
        )
        .unwrap();
    let IssuedAuthorization::NativeMedia(renewed) = renewed else {
        panic!("expected renewed native authorization");
    };
    assert_eq!(harness.identity.calls(), 2);
    assert_eq!(renewed.endpoints()[0].binding_generation(), 9);
    assert_eq!(renewed.endpoints()[0].topology_generation(), 53);

    let mut keyring = VerificationKeyring::new();
    keyring
        .insert_active("key_active_01", harness.verification_key)
        .unwrap();
    let verifier =
        MediaCapabilityVerifier::new(keyring, "https://control.needletail.test", "av-contrib")
            .unwrap();
    let authorized = verifier
        .authorize(
            renewed.capability().expose(),
            &CurrentMediaAuthorizationContextV1 {
                tenant_id: &harness.ids.tenant,
                session_id: &harness.ids.session,
                session_epoch: 9,
                media_authorization_epoch: 14,
                subject_grant_epoch: 4,
                media_policy_version: 8,
                class_authorization_epoch: Some(4),
                binding_generation: 9,
                topology_generation: 53,
                participant_id: &harness.ids.participant,
                endpoint_id: &harness.ids.endpoint,
                contributor_id: Some(&harness.ids.contributor),
                operation: Operation::Publish,
                media_class: MediaClass::Program,
                source_id: Some(&harness.ids.source),
                audience_id: None,
                take_id: None,
                edge_id: &harness.ids.primary_edge,
                now: NOW + 20,
                clock_skew_seconds: 5,
            },
            &mut ReplayGuard::default(),
        )
        .unwrap();
    assert_eq!(authorized.claims().subject_grant_epoch(), 4);
    assert_eq!(authorized.claims().media_policy_version(), 8);
    assert!(harness.telemetry.events().last().unwrap().renewal);
}

#[test]
fn route_admission_is_deterministic_and_lease_coverage_is_fail_closed() {
    let harness = harness(Operation::Publish);
    let request = native_request(&harness.ids);
    let slow_edge = EdgeId::new("edge_slow").unwrap();
    let fast_edge = EdgeId::new("edge_fast").unwrap();
    let routes = vec![
        candidate(
            &harness.ids,
            MediaEndpointTransport::NativeDatagram,
            slow_edge,
            40,
        ),
        candidate(
            &harness.ids,
            MediaEndpointTransport::NativeDatagram,
            fast_edge.clone(),
            8,
        ),
    ];
    let issued = harness
        .controller
        .issue(
            BrokerCaller::NativeBroker,
            &request,
            &desired(&harness.ids, &routes, false),
            NOW,
        )
        .unwrap();
    let IssuedAuthorization::NativeMedia(native) = issued else {
        panic!("expected native authorization");
    };
    assert_eq!(native.endpoints()[0].edge_id(), &fast_edge);

    let mut short_primary = candidate(
        &harness.ids,
        MediaEndpointTransport::NativeDatagram,
        harness.ids.primary_edge.clone(),
        5,
    );
    short_primary.healthy_until = NOW + 59;
    let primary_error = harness
        .controller
        .issue(
            BrokerCaller::NativeBroker,
            &request,
            &desired(&harness.ids, &[short_primary], false),
            NOW,
        )
        .unwrap_err();
    assert_eq!(
        primary_error.detail(),
        Some(ControllerErrorDetail::Route(
            RouteRefusalReason::LeaseUnhealthy
        ))
    );

    let mut short_repair = candidate(
        &harness.ids,
        MediaEndpointTransport::NativeDatagram,
        harness.ids.primary_edge.clone(),
        5,
    );
    short_repair.warm_repair.as_mut().unwrap().healthy_until = NOW + 59;
    let repair_error = harness
        .controller
        .issue(
            BrokerCaller::NativeBroker,
            &request,
            &desired(&harness.ids, &[short_repair], true),
            NOW,
        )
        .unwrap_err();
    assert_eq!(
        repair_error.detail(),
        Some(ControllerErrorDetail::Route(
            RouteRefusalReason::IndependentRepairUnavailable
        ))
    );

    let mut exhausted = candidate(
        &harness.ids,
        MediaEndpointTransport::NativeDatagram,
        harness.ids.primary_edge.clone(),
        5,
    );
    exhausted.available_session_slots = 0;
    let capacity_error = harness
        .controller
        .issue(
            BrokerCaller::NativeBroker,
            &request,
            &desired(&harness.ids, &[exhausted], false),
            NOW,
        )
        .unwrap_err();
    assert_eq!(
        capacity_error.detail(),
        Some(ControllerErrorDetail::Route(
            RouteRefusalReason::SessionCapacityExceeded
        ))
    );
}

#[test]
fn jwks_rotation_is_public_only_bounded_and_overlapping() {
    let old_signing_key = SigningKey::from_bytes(&[3; 32]);
    let old_public_key = old_signing_key.verifying_key().to_bytes();
    assert!(RetiringPublicKey::new("key_old", old_public_key, NOW, NOW + 94).is_err());
    let claims = MediaCapabilityClaimsV1::from_json_slice(include_bytes!(
        "fixtures/p00-media-capability-claims.json"
    ))
    .unwrap();
    let old_issuer = Ed25519CapabilityIssuer::new("key_old", old_signing_key, Vec::new()).unwrap();
    let pre_rotation_capability = old_issuer.sign(&claims).unwrap();
    let retiring = RetiringPublicKey::new("key_old", old_public_key, NOW, NOW + 95).unwrap();
    let issuer =
        Ed25519CapabilityIssuer::new("key_new", SigningKey::from_bytes(&[7; 32]), vec![retiring])
            .unwrap();
    let renewed_capability = issuer.sign(&claims).unwrap();
    let overlap = issuer.jwks(NOW + 94);
    assert_eq!(overlap.keys().len(), 2);
    assert_eq!(issuer.verification_keyring(NOW + 94).unwrap().len(), 2);
    let json = serde_json::to_string(&overlap).unwrap();
    assert!(!json.contains("\"d\""));
    assert!(!format!("{issuer:?}{overlap:?}").contains("key_new"));

    let ids = TestIds::new();
    let verifier = MediaCapabilityVerifier::new(
        issuer.verification_keyring(NOW + 20).unwrap(),
        "https://control.needletail.test",
        "av-contrib",
    )
    .unwrap();
    let context = CurrentMediaAuthorizationContextV1 {
        tenant_id: &ids.tenant,
        session_id: &ids.session,
        session_epoch: 9,
        media_authorization_epoch: 14,
        subject_grant_epoch: 3,
        media_policy_version: 7,
        class_authorization_epoch: Some(4),
        binding_generation: 8,
        topology_generation: 52,
        participant_id: &ids.participant,
        endpoint_id: &ids.endpoint,
        contributor_id: Some(&ids.contributor),
        operation: Operation::Publish,
        media_class: MediaClass::Program,
        source_id: Some(&ids.source),
        audience_id: None,
        take_id: None,
        edge_id: &ids.primary_edge,
        now: NOW + 20,
        clock_skew_seconds: 5,
    };
    let old_authorized = verifier
        .authorize(
            &pre_rotation_capability,
            &context,
            &mut ReplayGuard::default(),
        )
        .unwrap();
    let renewed_authorized = verifier
        .authorize(&renewed_capability, &context, &mut ReplayGuard::default())
        .unwrap();
    assert_eq!(
        old_authorized.key_state(),
        VerificationKeyState::Retiring {
            accept_until: NOW + 95
        }
    );
    assert_eq!(renewed_authorized.key_state(), VerificationKeyState::Active);
    assert_eq!(issuer.jwks(NOW + 95).keys().len(), 1);
}

#[test]
fn committed_p00_claim_fixture_parses_under_the_same_contract() {
    let claims = MediaCapabilityClaimsV1::from_json_slice(include_bytes!(
        "fixtures/p00-media-capability-claims.json"
    ))
    .unwrap();
    assert_eq!(claims.session_id().as_str(), "ses_mix");
    assert_eq!(claims.expires_at() - claims.issued_at(), 60);
}
