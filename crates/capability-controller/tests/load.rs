mod support;

use std::collections::{BTreeSet, HashSet};
use std::sync::{Arc, Barrier};
use std::thread;

use capability_controller::{BrokerCaller, IssuedAuthorization};
use media_object::{MediaEndpointTransport, Operation};

use support::{candidate, desired, harness, native_request, TestIds, NOW};

const WORKERS: usize = 16;
const ISSUES_PER_WORKER: usize = 64;

#[test]
fn concurrent_issuance_load_keeps_capabilities_unique_and_spreads_renewals() {
    let harness = harness(Operation::Publish);
    let controller = Arc::clone(&harness.controller);
    let request = native_request(&harness.ids);
    let barrier = Arc::new(Barrier::new(WORKERS));
    let handles: Vec<_> = (0..WORKERS)
        .map(|_| {
            let controller = Arc::clone(&controller);
            let request = request.clone();
            let barrier = Arc::clone(&barrier);
            thread::spawn(move || {
                let ids = TestIds::new();
                let routes = vec![candidate(
                    &ids,
                    MediaEndpointTransport::NativeDatagram,
                    ids.primary_edge.clone(),
                    18,
                )];
                barrier.wait();
                (0..ISSUES_PER_WORKER)
                    .map(|_| {
                        let issued = controller
                            .issue(
                                BrokerCaller::NativeBroker,
                                &request,
                                &desired(&ids, &routes, false),
                                NOW,
                            )
                            .unwrap();
                        let IssuedAuthorization::NativeMedia(native) = issued else {
                            panic!("expected native authorization");
                        };
                        (native.capability().expose().to_owned(), native.renew_at())
                    })
                    .collect::<Vec<_>>()
            })
        })
        .collect();

    let outcomes: Vec<_> = handles
        .into_iter()
        .flat_map(|handle| handle.join().unwrap())
        .collect();
    assert_eq!(outcomes.len(), WORKERS * ISSUES_PER_WORKER);
    let unique_capabilities: HashSet<_> =
        outcomes.iter().map(|(capability, _)| capability).collect();
    assert_eq!(unique_capabilities.len(), outcomes.len());

    let renewal_seconds: BTreeSet<_> = outcomes.iter().map(|(_, renew_at)| *renew_at).collect();
    assert!(renewal_seconds.len() >= 8);
    assert!(renewal_seconds
        .iter()
        .all(|renew_at| (NOW + 35..=NOW + 45).contains(renew_at)));
    assert_eq!(harness.identity.calls(), outcomes.len());
}
