#[cfg(target_arch = "wasm32")]
mod app {
    use gloo_net::http::Request;
    use gloo_timers::callback::{Interval, Timeout};
    use leptos::{mount::mount_to_body, prelude::*};
    use needletail_mission_control::{
        bounded_contrib_streams, bounded_edge_streams, bounded_edges, bounded_ingest_sessions,
        bounded_nodes, bounded_relay_nodes, contributor_latency, effective_delivery,
        monotonic_rate_per_second, operational_activity, operational_alerts, state_tone,
        ContribStatus, DeliverySnapshot, DurationHistogram, EdgeNode, EdgeService, EventSource,
        IngestSession, ListenerStatus, MeshStatus, OperationalEvent, OperationsCollectorStatus,
        ProtocolRuntime, PublicationSnapshot, RelayNodeSession, RouteLane,
        OPERATIONS_SNAPSHOT_SCHEMA,
    };
    use wasm_bindgen::{closure::Closure, JsCast};
    use wasm_bindgen_futures::spawn_local;

    const DEFAULT_EDGE_API: &str = "/api/mesh";
    const POLL_INTERVAL_MS: u32 = 5_000;
    const FETCH_TIMEOUT_MS: u32 = 4_000;
    const MAX_SNAPSHOT_BYTES: usize = 2 * 1024 * 1024;
    const SNAPSHOT_STALE_AFTER_MS: u64 = 15_000;
    const SNAPSHOT_CLOCK_SKEW_MS: u64 = 2_000;
    const RATE_HISTORY_POINTS: usize = 72;

    pub fn run() {
        console_error_panic_hook::set_once();
        mount_to_body(App);
    }

    #[derive(Clone, Debug, Default)]
    struct FeedState {
        last_ok_unix_ms: Option<u64>,
        error: Option<String>,
        attempted: bool,
    }

    #[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
    enum Page {
        #[default]
        Overview,
        Network,
        Streams,
        Ingest,
        Nodes,
        Routes,
        Performance,
        Activity,
    }

    impl Page {
        fn from_hash(hash: &str) -> Self {
            match hash.trim_start_matches('#').trim_matches('/') {
                "network" => Self::Network,
                "streams" => Self::Streams,
                "ingest" => Self::Ingest,
                "nodes" => Self::Nodes,
                "routes" => Self::Routes,
                "performance" => Self::Performance,
                "activity" => Self::Activity,
                _ => Self::Overview,
            }
        }

        fn slug(self) -> &'static str {
            match self {
                Self::Overview => "overview",
                Self::Network => "network",
                Self::Streams => "streams",
                Self::Ingest => "ingest",
                Self::Nodes => "nodes",
                Self::Routes => "routes",
                Self::Performance => "performance",
                Self::Activity => "activity",
            }
        }

        fn title(self) -> &'static str {
            match self {
                Self::Overview => "Overview",
                Self::Network => "Network map",
                Self::Streams => "Streams",
                Self::Ingest => "Contributor ingest",
                Self::Nodes => "Nodes and edges",
                Self::Routes => "Routes",
                Self::Performance => "Performance",
                Self::Activity => "Alerts and activity",
            }
        }
    }

    #[derive(Clone, Copy, Debug, Default)]
    struct TrafficRates {
        at_unix_ms: u64,
        input_bps: Option<f64>,
        relay_bps: Option<f64>,
        delivery_bps: Option<f64>,
        objects_per_second: Option<f64>,
    }

    #[derive(Clone, Copy, Debug)]
    struct ContribCounters {
        at_unix_ms: u64,
        input_bytes: u64,
        relay_bytes: u64,
    }

    #[derive(Clone, Copy, Debug)]
    struct EdgeCounters {
        at_unix_ms: u64,
        delivery_bytes: u64,
        decoded_objects: u64,
    }

    impl FeedState {
        fn ok(snapshot_unix_ms: u64) -> Self {
            Self {
                last_ok_unix_ms: (snapshot_unix_ms > 0).then_some(snapshot_unix_ms),
                error: None,
                attempted: true,
            }
        }

        fn error(message: String, previous: &Self) -> Self {
            Self {
                last_ok_unix_ms: previous.last_ok_unix_ms,
                error: Some(message),
                attempted: true,
            }
        }

        fn label(&self) -> &'static str {
            if !self.attempted {
                "opening"
            } else if self.error.is_some() && self.last_ok_unix_ms.is_none() {
                "unavailable"
            } else if self
                .last_ok_unix_ms
                .and_then(snapshot_age_ms)
                .is_some_and(|age| age <= SNAPSHOT_STALE_AFTER_MS)
                && self.error.is_none()
            {
                "live"
            } else {
                "stale"
            }
        }

        fn tone(&self) -> &'static str {
            if self.error.is_some() && self.last_ok_unix_ms.is_none() {
                "error"
            } else if self.label() == "live" {
                "healthy"
            } else {
                "warn"
            }
        }

        fn detail(&self) -> String {
            if !self.attempted {
                return "opening telemetry feed".to_owned();
            }
            if let Some(error) = &self.error {
                return self
                    .last_ok_unix_ms
                    .map(|last| {
                        format!(
                            "last snapshot {}; request failed: {error}",
                            snapshot_age_ms(last)
                                .map(format_age)
                                .unwrap_or_else(|| "with an invalid timestamp".to_owned())
                        )
                    })
                    .unwrap_or_else(|| error.clone());
            }
            self.last_ok_unix_ms
                .map(|last| {
                    snapshot_age_ms(last)
                        .map(|age| format!("snapshot {}", format_age(age)))
                        .unwrap_or_else(|| "snapshot timestamp invalid".to_owned())
                })
                .unwrap_or_else(|| "snapshot timestamp unavailable".to_owned())
        }

        fn compact_label(&self) -> String {
            self.last_ok_unix_ms
                .and_then(snapshot_age_ms)
                .map(|age| format!("Snapshot {} · {}", self.label(), format_age(age)))
                .unwrap_or_else(|| format!("Snapshot {}", self.label()))
        }
    }

    #[component]
    fn App() -> impl IntoView {
        let (page, set_page) = signal(current_page());
        let (edge, set_edge) = signal(None::<MeshStatus>);
        let (contrib, set_contrib) = signal(None::<ContribStatus>);
        let (fleet_feed, set_fleet_feed) = signal(FeedState::default());
        let (fetch_in_flight, set_fetch_in_flight) = signal(false);
        let (contrib_counters, set_contrib_counters) = signal(None::<ContribCounters>);
        let (edge_counters, set_edge_counters) = signal(None::<EdgeCounters>);
        let (rates, set_rates) = signal(TrafficRates::default());
        let (rate_history, set_rate_history) = signal(Vec::<TrafficRates>::new());

        if let Some(window) = web_sys::window() {
            let on_hash_change = Closure::<dyn FnMut(web_sys::Event)>::new(move |_| {
                set_page.set(current_page());
            });
            window.set_onhashchange(Some(on_hash_change.as_ref().unchecked_ref()));
            on_hash_change.forget();
        }

        let refresh = move || {
            if !fetch_in_flight.get_untracked() {
                set_fetch_in_flight.set(true);
                spawn_local(async move {
                    match fetch_mesh_snapshot(DEFAULT_EDGE_API).await {
                        Ok(snapshot) => {
                            let snapshot_unix_ms = snapshot.updated_unix_ms;
                            let sampled_at_unix_ms = now_unix_ms();
                            let next_edge = edge_counter_sample(&snapshot, sampled_at_unix_ms);
                            let previous_edge = edge_counters.get_untracked();
                            let embedded_contrib = snapshot.contributor.clone();
                            let next_contrib = embedded_contrib
                                .as_ref()
                                .map(|status| contrib_counter_sample(status, sampled_at_unix_ms));
                            let previous_contrib = contrib_counters.get_untracked();

                            let mut next_rates = rates.get_untracked();
                            next_rates.at_unix_ms = sampled_at_unix_ms;
                            next_rates.delivery_bps = previous_edge.and_then(|previous| {
                                monotonic_rate_per_second(
                                    previous.delivery_bytes,
                                    next_edge.delivery_bytes,
                                    sampled_at_unix_ms.saturating_sub(previous.at_unix_ms),
                                )
                                .map(|rate| rate * 8.0)
                            });
                            next_rates.objects_per_second = previous_edge.and_then(|previous| {
                                monotonic_rate_per_second(
                                    previous.decoded_objects,
                                    next_edge.decoded_objects,
                                    sampled_at_unix_ms.saturating_sub(previous.at_unix_ms),
                                )
                            });
                            next_rates.input_bps = previous_contrib.zip(next_contrib).and_then(
                                |(previous, current)| {
                                    monotonic_rate_per_second(
                                        previous.input_bytes,
                                        current.input_bytes,
                                        sampled_at_unix_ms.saturating_sub(previous.at_unix_ms),
                                    )
                                    .map(|rate| rate * 8.0)
                                },
                            );
                            next_rates.relay_bps = previous_contrib.zip(next_contrib).and_then(
                                |(previous, current)| {
                                    monotonic_rate_per_second(
                                        previous.relay_bytes,
                                        current.relay_bytes,
                                        sampled_at_unix_ms.saturating_sub(previous.at_unix_ms),
                                    )
                                    .map(|rate| rate * 8.0)
                                },
                            );

                            set_rates.set(next_rates);
                            if previous_edge.is_some()
                                || (previous_contrib.is_some() && next_contrib.is_some())
                            {
                                set_rate_history
                                    .update(|history| record_rate_history(history, next_rates));
                            }
                            set_edge_counters.set(Some(next_edge));
                            set_contrib_counters.set(next_contrib);
                            set_contrib.set(embedded_contrib);
                            set_edge.set(Some(snapshot));
                            set_fleet_feed.set(FeedState::ok(snapshot_unix_ms));
                        }
                        Err(error) => {
                            set_fleet_feed.update(|state| *state = FeedState::error(error, state));
                        }
                    }
                    set_fetch_in_flight.set(false);
                });
            }
        };

        refresh();
        Interval::new(POLL_INTERVAL_MS, move || {
            if document_is_visible() {
                refresh();
            }
        })
        .forget();

        view! {
            <div class="app-shell">
                <aside class="sidebar">
                    <a
                        class="brand-lockup"
                        href="#overview"
                        aria-label="Needletail overview"
                        on:click=move |_| set_page.set(Page::Overview)
                    >
                        <div class="mark" aria-hidden="true"><span></span><span></span><span></span></div>
                        <div><strong>"Needletail"</strong><span>"Operations"</span></div>
                    </a>
                    <nav class="primary-nav" aria-label="Operations views">
                        <NavItem target=Page::Overview current=page set_current=set_page />
                        <NavItem target=Page::Network current=page set_current=set_page />
                        <NavItem target=Page::Streams current=page set_current=set_page />
                        <NavItem target=Page::Ingest current=page set_current=set_page />
                        <NavItem target=Page::Nodes current=page set_current=set_page />
                        <NavItem target=Page::Routes current=page set_current=set_page />
                        <NavItem target=Page::Performance current=page set_current=set_page />
                        <NavItem target=Page::Activity current=page set_current=set_page />
                    </nav>
                    <div class="sidebar-feeds">
                        <FeedChip label="Global snapshot" state=fleet_feed />
                    </div>
                </aside>

                <div class="workspace">
                    <header class="workspace-bar">
                        <div class="page-identity">
                            <span>"Needletail"</span>
                            <h1>{move || page.get().title()}</h1>
                        </div>
                        <div class="workspace-actions">
                            <span
                                class=move || format!("cadence {}", fleet_feed.get().tone())
                                title=move || fleet_feed.get().detail()
                                aria-live="polite"
                            >
                                <i></i><span>{move || fleet_feed.get().compact_label()}</span>
                            </span>
                            <button class="refresh-button" on:click=move |_| refresh()>"Refresh now"</button>
                        </div>
                    </header>

                    <main class="page-stage">
                        {move || match page.get() {
                            Page::Overview => view! {
                                <OverviewPage contrib edge rates rate_history />
                            }.into_any(),
                            Page::Network => view! {
                                <NetworkPage edge rates />
                            }.into_any(),
                            Page::Streams => view! {
                                <StreamsPage contrib edge />
                            }.into_any(),
                            Page::Ingest => view! {
                                <IngestPage contrib />
                            }.into_any(),
                            Page::Nodes => view! {
                                <NodesPage edge />
                            }.into_any(),
                            Page::Routes => view! {
                                <RoutesPage contrib edge />
                            }.into_any(),
                            Page::Performance => view! {
                                <PerformancePage contrib edge rates rate_history />
                            }.into_any(),
                            Page::Activity => view! {
                                <ActivityPage contrib edge />
                            }.into_any(),
                        }}
                    </main>
                </div>
            </div>
        }
    }

    #[component]
    fn NavItem(
        target: Page,
        current: ReadSignal<Page>,
        set_current: WriteSignal<Page>,
    ) -> impl IntoView {
        view! {
            <a
                href=format!("#{}", target.slug())
                class=move || (current.get() == target).then_some("active")
                aria-current=move || (current.get() == target).then_some("page")
                on:click=move |_| set_current.set(target)
            >
                <span class="nav-indicator"></span>
                {target.title()}
            </a>
        }
    }

    #[component]
    fn OverviewPage(
        contrib: ReadSignal<Option<ContribStatus>>,
        edge: ReadSignal<Option<MeshStatus>>,
        rates: ReadSignal<TrafficRates>,
        rate_history: ReadSignal<Vec<TrafficRates>>,
    ) -> impl IntoView {
        view! {
            <div class="page-view overview-page">
                <CollectorStatusBar edge />
                <section class="section-block">
                    <SectionHeading kicker="CURRENT STATUS" title="Delivery health" detail="Ingest, relay, and playback edge" />
                    <div class="hero-grid">
                        <ServiceHealth contrib edge />
                        <RouteAssignmentSummary edge />
                        <DeadlineHealth contrib edge />
                    </div>
                </section>
                <ThroughputPanel rates rate_history />
                <section class="section-block">
                    <SectionHeading kicker="CURRENT PATH" title="Media flow" detail="Primary source and warm repair lanes" />
                    <div class="lane-flow">
                        <IngestLane contrib />
                        <div class="parallel-lanes">
                            <SourceLane contrib />
                            <RepairLane contrib />
                        </div>
                        <EdgeLane edge />
                    </div>
                </section>
                <div class="split-grid publication-split">
                    <PublicationPanel contrib edge />
                    <StreamSummary contrib edge />
                </div>
            </div>
        }
    }

    #[component]
    fn NetworkPage(
        edge: ReadSignal<Option<MeshStatus>>,
        rates: ReadSignal<TrafficRates>,
    ) -> impl IntoView {
        view! {
            <div class="page-view">
                <CollectorStatusBar edge />
                <section class="section-block">
                    <SectionHeading kicker="DEPLOYMENT" title="Network map" detail="Node and link health" />
                    <NetworkMap edge rates />
                </section>
                <RelayFabricTable edge />
            </div>
        }
    }

    #[component]
    fn StreamsPage(
        contrib: ReadSignal<Option<ContribStatus>>,
        edge: ReadSignal<Option<MeshStatus>>,
    ) -> impl IntoView {
        view! {
            <div class="page-view">
                <div class="split-grid publication-split">
                    <PublicationPanel contrib edge />
                    <StreamSummary contrib edge />
                </div>
                <ContributorStreams contrib />
                <EdgeStreams edge />
            </div>
        }
    }

    #[component]
    fn IngestPage(contrib: ReadSignal<Option<ContribStatus>>) -> impl IntoView {
        view! {
            <div class="page-view">
                <ContributorSummary contrib />
                <ListenerTable contrib />
                <IngestSessionTable contrib />
            </div>
        }
    }

    #[component]
    fn NodesPage(edge: ReadSignal<Option<MeshStatus>>) -> impl IntoView {
        view! {
            <div class="page-view">
                <CollectorStatusBar edge />
                <FleetSummary edge />
                <NodeTable edge />
                <EdgeServiceTable edge />
            </div>
        }
    }

    #[component]
    fn RoutesPage(
        contrib: ReadSignal<Option<ContribStatus>>,
        edge: ReadSignal<Option<MeshStatus>>,
    ) -> impl IntoView {
        view! {
            <div class="page-view">
                <RouteAssignmentPanel contrib edge />
                <RouteTable edge />
                <RelayFabricTable edge />
            </div>
        }
    }

    #[component]
    fn PerformancePage(
        contrib: ReadSignal<Option<ContribStatus>>,
        edge: ReadSignal<Option<MeshStatus>>,
        rates: ReadSignal<TrafficRates>,
        rate_history: ReadSignal<Vec<TrafficRates>>,
    ) -> impl IntoView {
        view! {
            <div class="page-view">
                <ThroughputPanel rates rate_history />
                <RaptorSummary contrib edge />
                <TelemetryTransportPanel edge />
                <LatencyPanel contrib edge />
            </div>
        }
    }

    #[component]
    fn ActivityPage(
        contrib: ReadSignal<Option<ContribStatus>>,
        edge: ReadSignal<Option<MeshStatus>>,
    ) -> impl IntoView {
        view! {
            <div class="page-view">
                <AlertActivity contrib edge />
                <AwaitedTelemetry contrib edge />
            </div>
        }
    }

    #[component]
    fn FeedChip(label: &'static str, state: ReadSignal<FeedState>) -> impl IntoView {
        view! {
            <div class=move || format!("feed-chip {}", state.get().tone())>
                <i></i>
                <div>
                    <span>{label}</span>
                    <strong>{move || state.get().label()}</strong>
                </div>
                <small>{move || state.get().detail()}</small>
            </div>
        }
    }

    #[derive(Clone, Copy, Debug)]
    enum RateMetric {
        Input,
        Relay,
        Delivery,
        Objects,
    }

    impl RateMetric {
        fn value(self, rates: &TrafficRates) -> Option<f64> {
            match self {
                Self::Input => rates.input_bps,
                Self::Relay => rates.relay_bps,
                Self::Delivery => rates.delivery_bps,
                Self::Objects => rates.objects_per_second,
            }
        }
    }

    #[component]
    fn ThroughputPanel(
        rates: ReadSignal<TrafficRates>,
        rate_history: ReadSignal<Vec<TrafficRates>>,
    ) -> impl IntoView {
        view! {
            <section class="section-block throughput-section">
                <SectionHeading kicker="LIVE RATES" title="Throughput" detail="Five-second counter deltas" />
                <div class="throughput-grid">
                    <RateCard label="Input" metric=RateMetric::Input rates rate_history unit="bit/s" />
                    <RateCard label="Relay output" metric=RateMetric::Relay rates rate_history unit="bit/s" />
                    <RateCard label="Playback delivery" metric=RateMetric::Delivery rates rate_history unit="bit/s" />
                    <RateCard label="Decoded objects" metric=RateMetric::Objects rates rate_history unit="objects/s" />
                </div>
            </section>
        }
    }

    #[component]
    fn RateCard(
        label: &'static str,
        metric: RateMetric,
        rates: ReadSignal<TrafficRates>,
        rate_history: ReadSignal<Vec<TrafficRates>>,
        unit: &'static str,
    ) -> impl IntoView {
        view! {
            <article class="rate-card">
                <div class="rate-card-heading">
                    <span>{label}</span>
                    <small>{unit}</small>
                </div>
                <strong>{move || format_rate(metric, metric.value(&rates.get()))}</strong>
                <Sparkline metric history=rate_history />
                <span class="rate-sample-count">
                    {move || format!("{} samples", rate_history.get().len())}
                </span>
            </article>
        }
    }

    #[component]
    fn Sparkline(metric: RateMetric, history: ReadSignal<Vec<TrafficRates>>) -> impl IntoView {
        view! {
            <svg class="sparkline" viewBox="0 0 240 52" preserveAspectRatio="none" aria-hidden="true">
                <path class="sparkline-baseline" d="M0 51.5 H240"></path>
                <path class="sparkline-path" d=move || sparkline_path(&history.get(), metric)></path>
            </svg>
        }
    }

    #[component]
    fn CollectorStatusBar(edge: ReadSignal<Option<MeshStatus>>) -> impl IntoView {
        let collector = move || {
            edge.get()
                .map(|status| status.orchestration.collector)
                .unwrap_or_default()
        };
        let snapshot_unix_ms = move || {
            edge.get()
                .map(|status| status.updated_unix_ms)
                .unwrap_or_default()
        };
        view! {
            <section class=move || format!("collector-status {}", collector_tone(&collector(), snapshot_unix_ms()))>
                <div class="collector-identity">
                    <span class=move || format!("state-mark {}", collector_tone(&collector(), snapshot_unix_ms()))></span>
                    <div>
                        <p>"GLOBAL TELEMETRY"</p>
                        <strong>{move || collector_state_label(&collector(), snapshot_unix_ms())}</strong>
                        <small>{move || collector_detail(&collector())}</small>
                    </div>
                </div>
                <div class="collector-facts">
                    <div><span>"Collector"</span><b>{move || verified_collector_node_id(&collector(), snapshot_unix_ms()).unwrap_or_else(|| "unavailable".to_owned())}</b></div>
                    <div><span>"Term / generation"</span><b>{move || format!("{} / {}", optional_u64(collector().term), optional_u64(collector().fencing_generation))}</b></div>
                    <div><span>"Quorum"</span><b>{move || format_collector_quorum(&collector())}</b></div>
                    <div><span>"Lease"</span><b>{move || effective_collector_lease_ms(&collector(), snapshot_unix_ms()).map(format_age_ms).unwrap_or_else(|| "unreported".to_owned())}</b></div>
                    <div><span>"Fleet snapshots"</span><b>{move || format_global_snapshot_freshness(edge.get().as_ref())}</b></div>
                </div>
            </section>
        }
    }

    #[derive(Clone, Debug)]
    struct NetworkLink {
        key: String,
        from: EdgeNode,
        to: EdgeNode,
        role: String,
        state: String,
        tone: &'static str,
        throughput_bps: Option<u64>,
        rtt_us: Option<u64>,
        generation: Option<u64>,
        path: String,
    }

    #[derive(Clone, Debug)]
    struct MapCluster {
        key: String,
        nodes: Vec<EdgeNode>,
        left: f64,
        top: f64,
    }

    #[component]
    fn NetworkMap(
        edge: ReadSignal<Option<MeshStatus>>,
        rates: ReadSignal<TrafficRates>,
    ) -> impl IntoView {
        let (selected_node, set_selected_node) = signal(None::<String>);
        view! {
            <div class="network-tool">
                <div class="network-toolbar">
                    <div class="network-summary">
                        <strong>{move || edge.get().map(|status| format_map_node_scale(&status)).unwrap_or_else(|| "0 / 0".to_owned())}</strong>
                        <span>"nodes mapped"</span>
                        <strong>{move || {
                            edge.get().map(|status| format_map_link_scale(&status)).unwrap_or_else(|| "0 / 0".to_owned())
                        }}</strong>
                        <span>"links shown"</span>
                        <strong>{move || format_rate(RateMetric::Delivery, rates.get().delivery_bps)}</strong>
                        <span>"delivery"</span>
                        <strong class="collector-summary">{move || edge.get()
                            .and_then(|status| verified_collector_node_id(&status.orchestration.collector, status.updated_unix_ms))
                            .unwrap_or_else(|| "unreported".to_owned())}</strong>
                        <span>"collector"</span>
                    </div>
                    <div class="map-legend" aria-label="Map status legend">
                        <span class="selected"><i></i>"Collector"</span>
                        <span class="healthy"><i></i>"Healthy"</span>
                        <span class="warn"><i></i>"Degraded"</span>
                        <span class="error"><i></i>"Unavailable"</span>
                    </div>
                </div>
                <div class="network-map-layout">
                    <div class="world-map" aria-label="Deployed Needletail nodes">
                        <img src="/world-map.png" alt="" aria-hidden="true" />
                        <svg class="network-links" viewBox="0 0 1000 500" preserveAspectRatio="none" aria-hidden="true">
                            <For
                                each=move || edge.get().map(|status| {
                                    network_links(&status)
                                }).unwrap_or_default()
                                key=|link| link.key.clone()
                                children=move |link| {
                                    let title = format_network_link_title(&link);
                                    view! {
                                        <path
                                            class=format!("network-link {} {}", link.tone, link_role_class(&link.role))
                                            d=link.path.clone()
                                        >
                                            <title>{title}</title>
                                        </path>
                                    }
                                }
                            />
                        </svg>
                        <For
                            each=move || edge.get().map(|status| map_clusters(&status)).unwrap_or_default()
                            key=|cluster| cluster.key.clone()
                            children=move |cluster| {
                                let representative = cluster.nodes[0].clone();
                                let selected_id = representative.node_id.clone();
                                let node_names = cluster.nodes.iter().map(|node| node.node_id.as_str()).collect::<Vec<_>>().join(", ");
                                let region = nonempty_owned(representative.region.clone(), "region pending");
                                let label = if cluster.nodes.len() == 1 {
                                    representative.node_id.clone()
                                } else {
                                    format!("{} · {} nodes", region, cluster.nodes.len())
                                };
                                let tone = edge.get().map(|status| cluster_health_tone(&status, &cluster)).unwrap_or("warn");
                                let is_collector = edge.get().is_some_and(|status| {
                                    verified_collector_node_id(
                                        &status.orchestration.collector,
                                        status.updated_unix_ms,
                                    )
                                    .is_some_and(|leader| {
                                        cluster.nodes.iter().any(|node| node.node_id == leader)
                                    })
                                });
                                view! {
                                    <button
                                        class=format!("map-node {tone} {}", if is_collector { "collector" } else { "" })
                                        style=format!("left:{:.3}%;top:{:.3}%", cluster.left, cluster.top)
                                        title=format!("{} · {}", node_names, region)
                                        on:click=move |_| set_selected_node.set(Some(selected_id.clone()))
                                    >
                                        <i></i><span>{if is_collector { format!("{label} · collector") } else { label }}</span>
                                    </button>
                                }
                            }
                        />
                        <div class="map-empty" class:hidden=move || edge.get().is_some_and(|status| !map_clusters(&status).is_empty())>
                            "Waiting for node coordinates"
                        </div>
                        <div class="map-topology-notice" class:hidden=move || !edge.get().is_some_and(|status| network_links(&status).is_empty())>
                            "Topology links unavailable"
                        </div>
                    </div>
                    <MapNodeDetail edge selected=selected_node />
                </div>
                <NetworkLinkTable edge />
            </div>
        }
    }

    #[component]
    fn NetworkLinkTable(edge: ReadSignal<Option<MeshStatus>>) -> impl IntoView {
        view! {
            <section class="map-link-details" aria-label="Topology link details">
                <div class="map-link-heading">
                    <strong>"Topology links"</strong>
                    <span>{move || edge.get().map(|status| format_map_link_scale(&status)).unwrap_or_else(|| "0 / 0".to_owned())}" shown"</span>
                </div>
                <div class="table-shell">
                    <table>
                        <thead>
                            <tr><th>"From"</th><th>"To"</th><th>"Lane"</th><th>"State"</th><th>"RTT"</th><th>"Throughput"</th><th>"Generation"</th></tr>
                        </thead>
                        <tbody>
                            <For
                                each=move || edge.get().map(|status| network_links(&status)).unwrap_or_default()
                                key=|link| link.key.clone()
                                let(link)
                            >
                                <tr>
                                    <td class="strong-cell">{link.from.node_id}</td>
                                    <td class="strong-cell">{link.to.node_id}</td>
                                    <td>{link.role}</td>
                                    <td><StatePill state=nonempty_owned(link.state, "unreported") /></td>
                                    <td class="mono-cell">{format_optional_duration(link.rtt_us)}</td>
                                    <td class="mono-cell">{link.throughput_bps.map(format_bps).unwrap_or_else(|| "unreported".to_owned())}</td>
                                    <td class="mono-cell">{optional_u64(link.generation)}</td>
                                </tr>
                            </For>
                        </tbody>
                    </table>
                </div>
            </section>
        }
    }

    #[component]
    fn MapNodeDetail(
        edge: ReadSignal<Option<MeshStatus>>,
        selected: ReadSignal<Option<String>>,
    ) -> impl IntoView {
        view! {
            <aside class="map-detail">
                {move || {
                    let status = edge.get();
                    let selected_id = selected.get();
                    let node = status.as_ref().and_then(|status| {
                        selected_id
                            .as_ref()
                            .and_then(|id| status.nodes.iter().find(|node| &node.node_id == id))
                            .or_else(|| status.nodes.iter().find(|node| node.node_id == status.node.node_id))
                            .or_else(|| status.nodes.first())
                    });
                    match (status.as_ref(), node) {
                        (Some(status), Some(node)) => {
                            let tone = node_health_tone(status, node);
                            let collector = &status.orchestration.collector;
                            let collector_current =
                                collector_is_current(collector, status.updated_unix_ms);
                            let cluster_size = status
                                .nodes
                                .iter()
                                .filter(|candidate| same_node_location(candidate, node))
                                .count();
                            let reported_leader = collector
                                .leader_node_id
                                .as_ref()
                                .is_some_and(|leader| leader == &node.node_id);
                            let collector_role = if collector_current && reported_leader {
                                "Active collector"
                            } else if reported_leader {
                                "Reported leader · unverified"
                            } else if collector_current {
                                "Follower"
                            } else if collector.reported() {
                                "Election state unverified"
                            } else {
                                "Unreported"
                            };
                            view! {
                                <div class="map-detail-content">
                                    <div class="map-detail-title">
                                        <span class=format!("state-mark {tone}")></span>
                                        <div>
                                            <strong>{node.node_id.clone()}</strong>
                                            <span>{format!("{} · {}", nonempty_owned(node.region.clone(), "Region pending"), collector_role)}</span>
                                        </div>
                                    </div>
                                    <dl>
                                        <div><dt>"Status"</dt><dd>{node_health_label(status, node)}</dd></div>
                                        <div><dt>"Role"</dt><dd>{nonempty_owned(node.role.clone(), "unreported")}</dd></div>
                                        <div><dt>"Provider / zone"</dt><dd>{format!("{} / {}", nonempty_owned(node.provider.clone(), "unreported"), nonempty_owned(node.zone.clone(), "unreported"))}</dd></div>
                                        <div><dt>"Nodes at location"</dt><dd>{cluster_size}</dd></div>
                                        <div><dt>"Location"</dt><dd>{format_node_location(node)}</dd></div>
                                        <div><dt>"Snapshot"</dt><dd>{node_snapshot_label(status, node)}</dd></div>
                                        <div><dt>"Active streams"</dt><dd>{node.active_streams}</dd></div>
                                        <div><dt>"Contributor streams"</dt><dd>{node.contributor_streams}</dd></div>
                                        <div><dt>"Egress capacity"</dt><dd>{format_bps(node.egress_capacity_bps)}</dd></div>
                                        <div><dt>"Storage"</dt><dd>{node.storage_percent().map(|value| format!("{value:.1}%")).unwrap_or_else(|| "pending".to_owned())}</dd></div>
                                    </dl>
                                    <a href="#nodes">"Open node details"</a>
                                </div>
                            }.into_any()
                        }
                        _ => view! {
                            <div class="map-detail-empty"><strong>"No node selected"</strong><span>"Node details will appear here."</span></div>
                        }.into_any(),
                    }
                }}
            </aside>
        }
    }

    #[component]
    fn SectionHeading(
        kicker: &'static str,
        title: &'static str,
        detail: &'static str,
    ) -> impl IntoView {
        view! {
            <header class="section-heading">
                <div><p>{kicker}</p><h2>{title}</h2></div>
                <span>{detail}</span>
            </header>
        }
    }

    #[component]
    fn ServiceHealth(
        contrib: ReadSignal<Option<ContribStatus>>,
        edge: ReadSignal<Option<MeshStatus>>,
    ) -> impl IntoView {
        let state = move || {
            let contrib = contrib.get();
            let edge = edge.get();
            if contrib.is_none() || edge.is_none() {
                "Telemetry unavailable".to_owned()
            } else {
                let alerts = actionable_alert_count(contrib.as_ref(), edge.as_ref());
                format!(
                    "{alerts} actionable alert{}",
                    if alerts == 1 { "" } else { "s" }
                )
            }
        };
        let tone = move || {
            let contrib = contrib.get();
            let edge = edge.get();
            if contrib.is_none() || edge.is_none() {
                "warn"
            } else if actionable_alert_count(contrib.as_ref(), edge.as_ref()) == 0 {
                "healthy"
            } else {
                "error"
            }
        };
        view! {
            <article class=move || format!("hero-card service {}", tone())>
                <p class="card-label">"Service alerts"</p>
                <div class="hero-value"><i></i><strong>{state}</strong></div>
                <div class="service-pair">
                    <span>"Contributor state"</span>
                    <b>{move || contrib.get().map(|s| nonempty_owned(s.health.state, "waiting")).unwrap_or_else(|| "connecting".to_owned())}</b>
                    <span>"Relay ingress errors"</span>
                    <b>{move || edge.get().map(|s| s.relay_session.errors().to_string()).unwrap_or_else(|| "—".to_owned())}</b>
                    <span>"Remote node snapshots"</span>
                    <b>{move || format_global_snapshot_freshness(edge.get().as_ref())}</b>
                </div>
            </article>
        }
    }

    #[component]
    fn RouteAssignmentSummary(edge: ReadSignal<Option<MeshStatus>>) -> impl IntoView {
        view! {
            <article class="hero-card delivery">
                <p class="card-label">"Relay topology"</p>
                <div class="hero-value">
                    <strong>{move || {
                        let delivery = effective_delivery(edge.get().as_ref());
                        delivery.fabric_label().unwrap_or("Topology not reported")
                    }}</strong>
                </div>
                <div class="detail-row">
                    <span>"Delivery class"</span>
                    <b>{move || effective_delivery(edge.get().as_ref()).delivery_class.unwrap_or_else(|| "pending".to_owned())}</b>
                </div>
                <div class="detail-row">
                    <span>"Generation"</span>
                    <b>{move || optional_u64(effective_delivery(edge.get().as_ref()).generation)}</b>
                </div>
                <div class="detail-row">
                    <span>"Route state"</span>
                    <b>{move || {
                        let delivery = effective_delivery(edge.get().as_ref());
                        delivery
                            .route_state
                            .clone()
                            .unwrap_or_else(|| delivery.readiness_label().to_owned())
                    }}</b>
                </div>
                <div class="detail-row">
                    <span>"Path stretch"</span>
                    <b>{move || effective_delivery(edge.get().as_ref()).path_stretch.map(|value| format!("{value:.2}×")).unwrap_or_else(|| "pending".to_owned())}</b>
                </div>
            </article>
        }
    }

    #[component]
    fn DeadlineHealth(
        contrib: ReadSignal<Option<ContribStatus>>,
        edge: ReadSignal<Option<MeshStatus>>,
    ) -> impl IntoView {
        let tone = move || {
            let contributor_misses = contrib
                .get()
                .and_then(|status| status.runtime.relay_session.deadline_misses)
                .unwrap_or(0);
            let edge_drops = edge
                .get()
                .map(|status| status.relay_session.deadline_drops)
                .unwrap_or(0);
            if contributor_misses > 0 || edge_drops > 0 {
                "error"
            } else if contrib
                .get()
                .and_then(|status| status.runtime.relay_session.last_deadline_headroom_us)
                .is_some()
            {
                "healthy"
            } else {
                "warn"
            }
        };
        view! {
            <article class=move || format!("hero-card deadline {}", tone())>
                <p class="card-label">"Deadline health"</p>
                <div class="hero-value">
                    <strong>{move || contrib.get().and_then(|s| s.runtime.relay_session.last_deadline_headroom_us)
                        .map(|value| format!("{} headroom", format_duration_us(value)))
                        .unwrap_or_else(|| "Awaiting first object".to_owned())}</strong>
                </div>
                <div class="detail-row">
                    <span>"Object budget"</span>
                    <b>{move || contrib.get().map(|s| if s.mesh.relay_deadline_ms == 0 { "pending".to_owned() } else { format!("{} ms", s.mesh.relay_deadline_ms) }).unwrap_or_else(|| "pending".to_owned())}</b>
                </div>
                <div class="detail-row">
                    <span>"Receiver deadline drops"</span>
                    <b>{move || edge.get().map(|s| s.relay_session.deadline_drops.to_string()).unwrap_or_else(|| "—".to_owned())}</b>
                </div>
                <div class="detail-row">
                    <span>"Emission hits / misses"</span>
                    <b>{move || contrib.get().map(|s| {
                        let relay = s.runtime.relay_session;
                        match (relay.deadline_hits, relay.deadline_misses) {
                            (Some(hits), Some(misses)) => format!("{hits} / {misses}"),
                            _ => "telemetry pending".to_owned(),
                        }
                    }).unwrap_or_else(|| "pending".to_owned())}</b>
                </div>
            </article>
        }
    }

    #[component]
    fn IngestLane(contrib: ReadSignal<Option<ContribStatus>>) -> impl IntoView {
        view! {
            <article class="lane-card ingress">
                <div class="lane-index">"01"</div>
                <p>"Contributor ingest"</p>
                <strong>{move || contrib.get().map(|s| nonempty_owned(s.health.state, "waiting")).unwrap_or_else(|| "connecting".to_owned())}</strong>
                <span>{move || contrib.get().map(|s| format!("{} canonical fMP4 objects", s.runtime.fmp4.parts)).unwrap_or_else(|| "No contributor telemetry".to_owned())}</span>
                <small>{move || contrib.get().and_then(|s| s.health.last_input_age_ms).map(|age| format!("input {}", format_age(age))).unwrap_or_else(|| "waiting for contributor media".to_owned())}</small>
            </article>
        }
    }

    #[component]
    fn SourceLane(contrib: ReadSignal<Option<ContribStatus>>) -> impl IntoView {
        view! {
            <article class=move || format!("lane-card source {}", contrib.get().map(|s| if s.mesh.relay_primary_configured { "healthy" } else { "warn" }).unwrap_or("warn"))>
                <div class="lane-index">"02A"</div>
                <p>"Primary source lane"</p>
                <strong>{move || contrib.get().and_then(|s| s.mesh.relay_primary_target).unwrap_or_else(|| "Awaiting route".to_owned())}</strong>
                <span>{move || contrib.get().map(|s| {
                    let carrier = s.mesh.relay_carrier.unwrap_or_else(|| "carrier pending".to_owned());
                    let trust = trust_label(s.mesh.relay_trust.as_deref());
                    format!("{carrier} · {trust}")
                }).unwrap_or_else(|| "carrier telemetry pending".to_owned())}</span>
                <small>{move || contrib.get().map(|s| format!("{} source symbols · {}", s.runtime.relay_session.source_datagrams, s.mesh.relay_primary_bind.unwrap_or_else(|| "automatic bind".to_owned()))).unwrap_or_default()}</small>
            </article>
        }
    }

    #[component]
    fn RepairLane(contrib: ReadSignal<Option<ContribStatus>>) -> impl IntoView {
        view! {
            <article class=move || format!("lane-card repair {}", contrib.get().map(|s| if s.mesh.relay_secondary_configured { "healthy" } else { "warn" }).unwrap_or("warn"))>
                <div class="lane-index">"02B"</div>
                <p>"Warm-secondary repair lane"</p>
                <strong>{move || contrib.get().and_then(|s| s.mesh.relay_secondary_target).unwrap_or_else(|| "Warm path unavailable".to_owned())}</strong>
                <span>{move || contrib.get().map(|s| if s.mesh.relay_secondary_configured { "repair path configured; independence telemetry pending" } else { "repair path not reported" }).unwrap_or("route telemetry pending")}</span>
                <small>{move || contrib.get().map(|s| format!("{} repair symbols · {} fallback objects", s.runtime.relay_session.repair_datagrams, s.runtime.relay_session.repair_primary_fallback_objects)).unwrap_or_default()}</small>
            </article>
        }
    }

    #[component]
    fn EdgeLane(edge: ReadSignal<Option<MeshStatus>>) -> impl IntoView {
        view! {
            <article class=move || format!("lane-card edge {}", edge.get().map(|s| if s.relay_session.errors() == 0 { "healthy" } else { "error" }).unwrap_or("warn"))>
                <div class="lane-index">"03"</div>
                <p>"Playback-edge recovery"</p>
                <strong>{move || edge.get().map(|s| format!("{} · {}", nonempty_owned(s.node.node_id, "edge"), nonempty_owned(s.node.region, "region pending"))).unwrap_or_else(|| "connecting".to_owned())}</strong>
                <span>{move || edge.get().map(|s| format!("{} primary · {} secondary sessions", s.relay_session.primary_sessions, s.relay_session.secondary_sessions)).unwrap_or_default()}</span>
                <small>{move || edge.get().map(|s| format!("{} decoded · {} FEC-recovered objects · {} recovered source symbols", s.relay_session.decoded_objects, s.relay_session.fec_recovered_objects, s.relay_session.fec_recovered_source_symbols)).unwrap_or_default()}</small>
            </article>
        }
    }

    #[component]
    fn PublicationPanel(
        contrib: ReadSignal<Option<ContribStatus>>,
        edge: ReadSignal<Option<MeshStatus>>,
    ) -> impl IntoView {
        view! {
            <section class="data-panel publication-panel">
                <SectionHeading kicker="PUBLICATION" title="Contiguous object progress" detail="Contributor and edge commit watermarks." />
                <div class="watermark-grid">
                    <Watermark title="Contributor" publication=move || contrib.get().map(|status| status.publication).unwrap_or_default() />
                    <Watermark title="Playback edge" publication=move || edge.get().map(|status| status.publication).unwrap_or_default() />
                </div>
            </section>
        }
    }

    #[component]
    fn Watermark<P>(title: &'static str, publication: P) -> impl IntoView
    where
        P: Fn() -> PublicationSnapshot + Clone + Send + Sync + 'static,
    {
        let publication = StoredValue::new(publication);
        view! {
            <article class="watermark">
                <p>{title}</p>
                <div><span>"Source epoch"</span><strong>{move || format_publication_value(publication.get_value()().canonical_epoch)}</strong></div>
                <div><span>"Epoch activation"</span><strong>{move || format_publication_duration(publication.get_value()().canonical_epoch_activation_delay_us)}</strong></div>
                <div><span>"Contiguous"</span><strong>{move || format_publication_value(publication.get_value()().contiguous_object)}</strong></div>
                <div><span>"Head"</span><strong>{move || format_publication_value(publication.get_value()().head_object)}</strong></div>
                <div><span>"Known gaps"</span><strong>{move || format_publication_value(publication.get_value()().gap_count)}</strong></div>
            </article>
        }
    }

    #[component]
    fn StreamSummary(
        contrib: ReadSignal<Option<ContribStatus>>,
        edge: ReadSignal<Option<MeshStatus>>,
    ) -> impl IntoView {
        view! {
            <section class="data-panel compact-summary">
                <SectionHeading kicker="LIVE STATE" title="Publication health" detail="Bounded service-side stream inventory." />
                <div class="summary-metrics four">
                    <SmallMetric label="Contributor streams" value=move || contrib.get().map(|s| s.runtime.streams.len().to_string()).unwrap_or_else(|| "—".to_owned()) />
                    <SmallMetric label="Active delivery streams" value=move || edge.get().map(|s| s.aggregate.active_streams.to_string()).unwrap_or_else(|| "—".to_owned()) />
                    <SmallMetric label="Latest fMP4" value=move || contrib.get().and_then(|status| status.publication.head_object).map(|v| v.to_string()).unwrap_or_else(|| "not reported".to_owned()) />
                    <SmallMetric label="Known edge gaps" value=move || edge.get().and_then(|status| status.publication.gap_count).map(|v| v.to_string()).unwrap_or_else(|| "not reported".to_owned()) />
                </div>
            </section>
        }
    }

    #[component]
    fn ContributorStreams(contrib: ReadSignal<Option<ContribStatus>>) -> impl IntoView {
        view! {
            <section class="data-panel table-panel">
                <PanelTitle title="Contributor publications" detail="Input → fMP4 → RelaySession" />
                <div class="table-shell">
                    <table>
                        <thead><tr><th>"Stream"</th><th>"State"</th><th>"Input"</th><th>"fMP4"</th><th>"Codecs"</th><th>"Last output"</th><th>"Errors"</th></tr></thead>
                        <tbody>
                            <For
                                each=move || contrib.get().map(|status| bounded_contrib_streams(&status)).unwrap_or_default()
                                key=|stream| stream.stream_id_text.clone()
                                let(stream)
                            >
                                <tr>
                                    <td class="strong-cell">{nonempty_owned(stream.stream_id_text.clone(), "unnamed")}</td>
                                    <td><StatePill state=stream.state.clone() /></td>
                                    <td>{format!("{} · {}", stream.input_units, format_bytes(stream.input_bytes))}</td>
                                    <td>{format!("{} parts · head {}", stream.fmp4_parts, optional_u64(stream.latest_fmp4_sequence))}</td>
                                    <td>{codec_summary(&stream.video_codec, stream.video_width, stream.video_height, &stream.audio_codec)}</td>
                                    <td>{stream.last_fmp4_age_ms.map(format_age).unwrap_or_else(|| "pending".to_owned())}</td>
                                    <td>{stream.mesh_errors.saturating_add(stream.fmp4_publish_errors)}</td>
                                </tr>
                            </For>
                        </tbody>
                    </table>
                </div>
                {move || contrib.get().is_some_and(|status| status.runtime.streams.is_empty()).then(|| view! { <p class="awaiting">"Waiting for the first contributor stream."</p> })}
            </section>
        }
    }

    #[component]
    fn EdgeStreams(edge: ReadSignal<Option<MeshStatus>>) -> impl IntoView {
        view! {
            <section class="data-panel table-panel">
                <PanelTitle title="Playback-edge publications" detail="Canonical object identity, contiguous availability, gaps, and staleness" />
                <div class="table-shell">
                    <table>
                        <thead><tr><th>"Stream"</th><th>"Node"</th><th>"Source epoch"</th><th>"Epoch activation"</th><th>"Canonical head"</th><th>"Contiguous"</th><th>"Lag"</th><th>"Known gaps"</th><th>"Last ingest"</th><th>"State"</th></tr></thead>
                        <tbody>
                            <For
                                each=move || edge.get().map(|status| bounded_edge_streams(&status)).unwrap_or_default()
                                key=|stream| format!("{}:{}", stream.node_id, stream.stream_id_text)
                                let(stream)
                            >
                                <tr>
                                    <td class="strong-cell">{nonempty_owned(stream.stream_id_text.clone(), "unnamed")}</td>
                                    <td>{nonempty_owned(stream.node_id.clone(), "edge")}</td>
                                    <td>{format_publication_value(stream.canonical_epoch)}</td>
                                    <td class="mono-cell">{format_publication_duration(stream.canonical_epoch_activation_delay_us)}</td>
                                    <td>{format_publication_value(stream.head_object)}</td>
                                    <td>{format_publication_value(stream.contiguous_object)}</td>
                                    <td>{stream.mesh_lag_parts.map(|lag| format!("{lag} parts")).unwrap_or_else(|| "pending".to_owned())}</td>
                                    <td>{format_publication_value(stream.gap_count)}</td>
                                    <td>{stream.last_ingest_age_ms.map(format_age).unwrap_or_else(|| "pending".to_owned())}</td>
                                    <td><StatePill state=edge_stream_state(stream.stale(), stream.mesh_lag_parts) /></td>
                                </tr>
                            </For>
                        </tbody>
                    </table>
                </div>
                {move || edge.get().is_some_and(|status| status.streams.is_empty()).then(|| view! { <p class="awaiting">"Waiting for playback-edge stream telemetry."</p> })}
            </section>
        }
    }

    #[component]
    fn ContributorSummary(contrib: ReadSignal<Option<ContribStatus>>) -> impl IntoView {
        view! {
            <div class="summary-grid">
                <SummaryCard label="Ingest sessions" value=move || contrib.get().map(|s| s.runtime.ingest_sessions.active.to_string()).unwrap_or_else(|| "—".to_owned()) detail=move || contrib.get().map(|s| format!("{} started · {} ended", s.runtime.ingest_sessions.started, s.runtime.ingest_sessions.ended)).unwrap_or_else(|| "telemetry opening".to_owned()) />
                <SummaryCard label="fMP4 output" value=move || contrib.get().map(|s| s.runtime.fmp4.parts.to_string()).unwrap_or_else(|| "—".to_owned()) detail=move || contrib.get().map(|s| format!("{} · {}", codec_summary(&s.runtime.fmp4.video_codec, s.runtime.fmp4.video_width, s.runtime.fmp4.video_height, &s.runtime.fmp4.audio_codec), format_bytes(s.runtime.fmp4.bytes))).unwrap_or_else(|| "codec telemetry pending".to_owned()) />
                <SummaryCard label="MPEG-TS input" value=move || contrib.get().map(|s| s.runtime.mpeg_ts.slots.to_string()).unwrap_or_else(|| "—".to_owned()) detail=move || contrib.get().map(|s| format!("{} continuity errors · {} drops", s.runtime.mpeg_ts.continuity_errors, s.runtime.mpeg_ts.payload_drops)).unwrap_or_else(|| "telemetry opening".to_owned()) />
                <SummaryCard label="Contributor LL-HLS" value=move || contrib.get().map(|s| s.runtime.hls.responses_total.to_string()).unwrap_or_else(|| "—".to_owned()) detail=move || contrib.get().map(|s| format!("{} errors · {} not found", s.runtime.hls.response_errors, s.runtime.hls.response_not_found)).unwrap_or_else(|| "telemetry opening".to_owned()) />
            </div>
        }
    }

    #[component]
    fn SummaryCard<V, D>(label: &'static str, value: V, detail: D) -> impl IntoView
    where
        V: Fn() -> String + Send + Sync + 'static,
        D: Fn() -> String + Send + Sync + 'static,
    {
        view! {
            <article class="summary-card">
                <span>{label}</span>
                <strong>{value}</strong>
                <small>{detail}</small>
            </article>
        }
    }

    #[component]
    fn SmallMetric<V>(label: &'static str, value: V) -> impl IntoView
    where
        V: Fn() -> String + Send + Sync + 'static,
    {
        view! { <article class="small-metric"><span>{label}</span><strong>{value}</strong></article> }
    }

    #[component]
    fn ListenerTable(contrib: ReadSignal<Option<ContribStatus>>) -> impl IntoView {
        view! {
            <section class="data-panel table-panel">
                <PanelTitle title="Ingest listeners" detail="Configuration joined with live protocol counters" />
                <div class="table-shell">
                    <table>
                        <thead><tr><th>"Protocol"</th><th>"Listener"</th><th>"Session state"</th><th>"Units / bytes"</th><th>"Output"</th><th>"Protocol detail"</th></tr></thead>
                        <tbody>
                            <For
                                each=move || contrib.get().map(|status| status.listeners.into_iter().take(8).collect::<Vec<_>>()).unwrap_or_default()
                                key=|listener| listener.protocol.clone()
                                let(listener)
                            >
                                <ListenerRow listener contrib />
                            </For>
                        </tbody>
                    </table>
                </div>
            </section>
        }
    }

    #[component]
    fn ListenerRow(
        listener: ListenerStatus,
        contrib: ReadSignal<Option<ContribStatus>>,
    ) -> impl IntoView {
        let protocol = listener.protocol.clone();
        let protocol_for_state = protocol.clone();
        let runtime_state = move || {
            contrib
                .get()
                .and_then(|status| protocol_runtime(&status, &protocol_for_state))
                .unwrap_or_default()
        };
        let runtime_totals = move || {
            contrib
                .get()
                .and_then(|status| protocol_runtime(&status, &protocol))
                .unwrap_or_default()
        };
        let configured_state = if listener.enabled {
            "listening"
        } else {
            "available"
        };
        view! {
            <tr>
                <td class="strong-cell protocol-name">{listener.protocol.to_ascii_uppercase()}</td>
                <td>
                    <StatePill state=configured_state.to_owned() />
                    <small class="cell-detail">{listener.bind.clone().unwrap_or_else(|| "listener disabled".to_owned())}</small>
                </td>
                <td>{move || {
                    let runtime = runtime_state();
                    format!("{} active · {} ended", runtime.active_sessions, runtime.ended_sessions)
                }}</td>
                <td>{move || {
                    let runtime = runtime_totals();
                    format!("{} · {}", runtime.units, format_bytes(runtime.bytes))
                }}</td>
                <td>{format!("stream {} · {}", nonempty_owned(listener.output_stream_id.clone(), "pending"), nonempty_owned(listener.output_hls_path.clone(), "path pending"))}</td>
                <td>{listener_detail(&listener)}</td>
            </tr>
        }
    }

    #[component]
    fn IngestSessionTable(contrib: ReadSignal<Option<ContribStatus>>) -> impl IntoView {
        view! {
            <section class="data-panel table-panel">
                <PanelTitle title="Recent ingest sessions" detail="Latest bounded session activity across enabled protocols" />
                <div class="table-shell">
                    <table>
                        <thead><tr><th>"Session"</th><th>"Protocol"</th><th>"State"</th><th>"Stream"</th><th>"Input"</th><th>"Last activity"</th><th>"Endpoint"</th></tr></thead>
                        <tbody>
                            <For
                                each=move || contrib.get().map(|status| bounded_ingest_sessions(&status)).unwrap_or_default()
                                key=|session| format!("{}:{}", session.protocol, session.session_id)
                                let(session)
                            >
                                <IngestSessionRow session />
                            </For>
                        </tbody>
                    </table>
                </div>
                {move || contrib.get().is_some_and(|status| status.runtime.ingest_sessions.recent.is_empty()).then(|| view! { <p class="awaiting">"Waiting for the first protocol session."</p> })}
            </section>
        }
    }

    #[component]
    fn IngestSessionRow(session: IngestSession) -> impl IntoView {
        view! {
            <tr>
                <td class="mono-cell">{session.session_id}</td>
                <td class="strong-cell">{session.protocol.to_ascii_uppercase()}</td>
                <td><StatePill state=session.state.clone() /></td>
                <td>{format!("{} → {}", nonempty_owned(session.stream_id_text, "pending"), session.output_stream_id_text.unwrap_or_else(|| "pending".to_owned()))}</td>
                <td>{format!("{} · {} AU · {}", format_bytes(session.bytes), session.access_units, session.body_slots)}</td>
                <td>{format_age(session.age_ms)}</td>
                <td>{session.peer.or(session.path).unwrap_or_else(|| "endpoint pending".to_owned())}</td>
            </tr>
        }
    }

    #[component]
    fn FleetSummary(edge: ReadSignal<Option<MeshStatus>>) -> impl IntoView {
        view! {
            <div class="summary-grid">
                <SummaryCard label="Visible nodes" value=move || edge.get().map(|s| s.nodes.len().to_string()).unwrap_or_else(|| "—".to_owned()) detail=move || format_global_snapshot_freshness(edge.get().as_ref()) />
                <SummaryCard label="Active streams" value=move || edge.get().map(|s| s.aggregate.active_streams.to_string()).unwrap_or_else(|| "—".to_owned()) detail=move || edge.get().map(|s| format!("{} contributor-origin streams", s.aggregate.contributor_streams)).unwrap_or_else(|| "telemetry opening".to_owned()) />
                <SummaryCard label="Active readers" value=move || edge.get().map(|s| s.edge_services.iter().map(|service| service.active_readers).sum::<u64>().to_string()).unwrap_or_else(|| "—".to_owned()) detail=move || edge.get().map(|s| format!("{} edge responses", s.edge_services.iter().map(|service| service.responses_total).sum::<u64>())).unwrap_or_else(|| "telemetry opening".to_owned()) />
                <SummaryCard label="Control dispatch" value=move || edge.get().map(|s| if s.orchestration.control_dispatch_ready { "enabled" } else { "disabled" }.to_owned()).unwrap_or_else(|| "—".to_owned()) detail=move || format_global_snapshot_freshness(edge.get().as_ref()) />
            </div>
        }
    }

    #[component]
    fn NodeTable(edge: ReadSignal<Option<MeshStatus>>) -> impl IntoView {
        view! {
            <section class="data-panel table-panel">
                <PanelTitle title="Node fleet" detail="Capacity, storage, streams, and service state" />
                <div class="table-shell">
                    <table>
                        <thead><tr><th>"Node"</th><th>"Role"</th><th>"Provider / region"</th><th>"State"</th><th>"Active streams"</th><th>"Storage"</th><th>"Egress capacity"</th></tr></thead>
                        <tbody>
                            <For
                                each=move || edge.get().map(|status| {
                                    let leader = verified_collector_node_id(
                                        &status.orchestration.collector,
                                        status.updated_unix_ms,
                                    );
                                    bounded_nodes(&status)
                                        .into_iter()
                                        .map(|node| {
                                            let is_collector = leader.as_ref().is_some_and(|id| id == &node.node_id);
                                            let state = node_health_label(&status, &node).to_owned();
                                            (node, is_collector, state)
                                        })
                                        .collect::<Vec<_>>()
                                }).unwrap_or_default()
                                key=|(node, _, _)| node.node_id.clone()
                                let(entry)
                            >
                                <NodeRow node=entry.0 is_collector=entry.1 state=entry.2 />
                            </For>
                        </tbody>
                    </table>
                </div>
                {move || edge.get().is_some_and(|status| status.nodes.is_empty()).then(|| view! { <p class="awaiting">"Waiting for the node inventory snapshot."</p> })}
            </section>
        }
    }

    #[component]
    fn NodeRow(node: EdgeNode, is_collector: bool, state: String) -> impl IntoView {
        let node_id = nonempty_owned(node.node_id.clone(), "node");
        let location = format!(
            "{} · {}",
            nonempty_owned(node.provider.clone(), "provider pending"),
            nonempty_owned(node.region.clone(), "region pending"),
        );
        let role = nonempty_owned(node.role.clone(), "role unreported");
        let role = if is_collector {
            format!("collector · {role}")
        } else {
            role
        };
        let storage = node
            .storage_percent()
            .map(|value| {
                format!(
                    "{value:.1}% · {} / {}",
                    format_bytes(node.used_storage_bytes),
                    format_bytes(node.total_storage_bytes)
                )
            })
            .unwrap_or_else(|| "capacity pending".to_owned());
        view! {
            <tr>
                <td class="strong-cell">{node_id}</td>
                <td>{role}</td>
                <td>{location}</td>
                <td><StatePill state /></td>
                <td>{format!("{} · {} origin", node.active_streams, node.contributor_streams)}</td>
                <td>{storage}</td>
                <td>{format_bps(node.egress_capacity_bps)}</td>
            </tr>
        }
    }

    #[component]
    fn EdgeServiceTable(edge: ReadSignal<Option<MeshStatus>>) -> impl IntoView {
        view! {
            <section class="data-panel table-panel">
                <PanelTitle title="Playback-edge services" detail="Readers, responses, latency, errors, and recency" />
                <div class="table-shell">
                    <table>
                        <thead><tr><th>"Edge"</th><th>"State"</th><th>"Readers"</th><th>"Responses"</th><th>"Errors"</th><th>"p95"</th><th>"Last response"</th><th>"Playback"</th></tr></thead>
                        <tbody>
                            <For
                                each=move || edge.get().map(|status| bounded_edges(&status)).unwrap_or_default()
                                key=|service| format!("{}:{}", service.node_id, service.region)
                                let(service)
                            >
                                <EdgeServiceRow service />
                            </For>
                        </tbody>
                    </table>
                </div>
            </section>
        }
    }

    #[component]
    fn EdgeServiceRow(service: EdgeService) -> impl IntoView {
        let state = if service.draining {
            "draining"
        } else if service.response_errors > 0 {
            "attention"
        } else {
            "serving"
        };
        view! {
            <tr>
                <td class="strong-cell">{format!("{} · {}", nonempty_owned(service.node_id.clone(), "edge"), nonempty_owned(service.region.clone(), "region pending"))}</td>
                <td><StatePill state=state.to_owned() /></td>
                <td>{service.active_readers}</td>
                <td>{format!("{} · {}", service.responses_total.max(service.requests_served), format_bytes(service.bytes_served))}</td>
                <td>{format!("{} · {} 404", service.response_errors, service.response_not_found)}</td>
                <td class="mono-cell">{format_optional_duration(service.percentile_us(95))}</td>
                <td>{service.last_response_unix_ms.map(|seen| format_age(now_unix_ms().saturating_sub(seen))).unwrap_or_else(|| "pending".to_owned())}</td>
                <td>{service.playback_base_url.unwrap_or_else(|| "URL pending".to_owned())}</td>
            </tr>
        }
    }

    #[component]
    fn RouteAssignmentPanel(
        contrib: ReadSignal<Option<ContribStatus>>,
        edge: ReadSignal<Option<MeshStatus>>,
    ) -> impl IntoView {
        view! {
            <div class="route-assignment-grid">
                <article class="data-panel route-policy">
                    <PanelTitle title="Route assignment" detail="Controller assignment and measured constraints" />
                    <div class="route-assignment-value">
                        <strong>{move || effective_delivery(edge.get().as_ref()).fabric_label().unwrap_or("Awaiting assignment")}</strong>
                        <span
                            class=move || format!("state-pill {}", state_tone(effective_delivery(edge.get().as_ref()).readiness_label()))
                        >
                            {move || effective_delivery(edge.get().as_ref()).readiness_label()}
                        </span>
                    </div>
                    <div class="key-value-grid">
                        <span>"Delivery class"</span><b>{move || effective_delivery(edge.get().as_ref()).delivery_class.unwrap_or_else(|| "pending".to_owned())}</b>
                        <span>"Generation"</span><b>{move || optional_u64(effective_delivery(edge.get().as_ref()).generation)}</b>
                        <span>"Route state"</span><b>{move || effective_delivery(edge.get().as_ref()).route_state.unwrap_or_else(|| "pending".to_owned())}</b>
                        <span>"Path stretch"</span><b>{move || effective_delivery(edge.get().as_ref()).path_stretch.map(|value| format!("{value:.3}×")).unwrap_or_else(|| "pending".to_owned())}</b>
                        <span>"RaptorQ path input"</span><b>{move || contrib.get().map(|status| nonempty_owned(status.mesh.relay_path_observation_source, "pending")).unwrap_or_else(|| "pending".to_owned())}</b>
                        <span>"Path observation age"</span><b>{move || contrib.get().and_then(|status| status.mesh.relay_path_observed_at_unix_ms).map(|observed| format_age(now_unix_ms().saturating_sub(observed))).unwrap_or_else(|| "pending".to_owned())}</b>
                        <span>"Observed queue delay"</span><b>{move || contrib.get().filter(|status| status.mesh.relay_path_queue_delay_ms > 0.0).map(|status| format!("{:.2} ms", status.mesh.relay_path_queue_delay_ms)).unwrap_or_else(|| "pending".to_owned())}</b>
                    </div>
                </article>
                <RouteLaneCard role="Primary source" lane=move || effective_delivery(edge.get().as_ref()).primary />
                <RouteLaneCard role="Warm secondary" lane=move || effective_delivery(edge.get().as_ref()).secondary />
            </div>
        }
    }

    #[component]
    fn RouteLaneCard<L>(role: &'static str, lane: L) -> impl IntoView
    where
        L: Fn() -> Option<RouteLane> + Clone + Send + Sync + 'static,
    {
        let lane = StoredValue::new(lane);
        view! {
            <article class="data-panel route-lane-card">
                <PanelTitle title=role detail="Assigned carrier path" />
                <strong class="route-node">{move || lane.get_value()().as_ref().and_then(|lane| lane.node_id.clone().or(lane.target.clone())).unwrap_or_else(|| "assignment pending".to_owned())}</strong>
                <div class="key-value-grid">
                    <span>"Carrier"</span><b>{move || lane.get_value()().and_then(|lane| lane.carrier).unwrap_or_else(|| "pending".to_owned())}</b>
                    <span>"Trust"</span><b>{move || lane.get_value()().and_then(|lane| lane.trust).unwrap_or_else(|| "pending".to_owned())}</b>
                    <span>"Lane state"</span><b>{move || lane.get_value()().and_then(|lane| lane.state).unwrap_or_else(|| "pending".to_owned())}</b>
                    <span>"Observation"</span><b>{move || lane.get_value()().and_then(|lane| lane.observation_source).unwrap_or_else(|| "pending".to_owned())}</b>
                    <span>"RTT / jitter"</span><b>{move || lane.get_value()().map(|lane| format!("{} / {}", format_optional_duration(lane.rtt_us), format_optional_duration(lane.jitter_us))).unwrap_or_else(|| "pending".to_owned())}</b>
                    <span>"Loss / deadline miss"</span><b>{move || lane.get_value()().map(|lane| format!("{} / {}", format_optional_ppm(lane.loss_ppm), format_optional_ppm(lane.deadline_miss_ppm))).unwrap_or_else(|| "pending".to_owned())}</b>
                </div>
            </article>
        }
    }

    #[component]
    fn RouteTable(edge: ReadSignal<Option<MeshStatus>>) -> impl IntoView {
        view! {
            <section class="data-panel table-panel">
                <PanelTitle title="Active route inventory" detail="Bounded stream/cohort delivery assignments" />
                <div class="table-shell">
                    <table>
                        <thead><tr><th>"Stream / cohort"</th><th>"Fabric"</th><th>"Class"</th><th>"Generation"</th><th>"Primary"</th><th>"Warm secondary"</th><th>"Stretch"</th><th>"Readiness"</th></tr></thead>
                        <tbody>
                            <For
                                each=move || route_rows(edge.get().as_ref())
                                key=|route| format!("{}:{}:{}", route.stream_id_text.as_deref().unwrap_or("default"), route.destination.as_deref().unwrap_or("default"), route.generation.unwrap_or_default())
                                let(route)
                            >
                                <tr>
                                    <td class="strong-cell">{format!("{} · {}", route.stream_id_text.clone().unwrap_or_else(|| "current stream".to_owned()), route.destination.clone().unwrap_or_else(|| "current cohort".to_owned()))}</td>
                                    <td>{route.fabric_label().unwrap_or("pending")}</td>
                                    <td>{route.delivery_class.clone().unwrap_or_else(|| "pending".to_owned())}</td>
                                    <td>{optional_u64(route.generation)}</td>
                                    <td>{route_lane_compact(route.primary.as_ref())}</td>
                                    <td>{route_lane_compact(route.secondary.as_ref())}</td>
                                    <td>{route.path_stretch.map(|value| format!("{value:.3}×")).unwrap_or_else(|| "pending".to_owned())}</td>
                                    <td><StatePill state=route.readiness_label().to_owned() /></td>
                                </tr>
                            </For>
                        </tbody>
                    </table>
                </div>
            </section>
        }
    }

    #[component]
    fn RelayFabricTable(edge: ReadSignal<Option<MeshStatus>>) -> impl IntoView {
        view! {
            <section class="data-panel table-panel">
                <PanelTitle title="Relay RaptorQ counters" detail="Per-node source, secondary repair, forwarding, recovery, failover, and carrier latency" />
                <div class="table-shell">
                    <table>
                        <thead><tr><th>"Node"</th><th>"Assignment"</th><th>"State"</th><th>"Parents"</th><th>"Received source / repair"</th><th>"Children"</th><th>"Forwarded source / repair"</th><th>"Failover"</th><th>"FEC recovered"</th><th>"Warm replay"</th><th>"Publish → available p95"</th><th>"Processing p95 / p99"</th><th>"Forward p95 / max"</th><th>"Errors"</th></tr></thead>
                        <tbody>
                            <For
                                each=move || edge.get().map(|status| bounded_relay_nodes(&status)).unwrap_or_default()
                                key=|node| node.node_id.clone()
                                let(node)
                            >
                                <RelayFabricRow node />
                            </For>
                        </tbody>
                    </table>
                </div>
                {move || edge.get().and_then(|status| {
                    let shown = bounded_relay_nodes(&status).len();
                    (status.relay_nodes.len() > shown).then(|| view! {
                        <p class="table-limit-note">{format!("Showing {shown} of {} relay nodes.", status.relay_nodes.len())}</p>
                    })
                })}
                {move || edge.get().is_some_and(|status| status.relay_nodes.is_empty()).then(|| view! { <p class="awaiting">"Waiting for relay-node telemetry."</p> })}
            </section>
        }
    }

    #[component]
    fn RelayFabricRow(node: RelayNodeSession) -> impl IntoView {
        let relay = &node.relay_session;
        let assignment = if relay.primary_sessions > 0 && relay.secondary_sessions > 0 {
            "playback edge"
        } else if relay.downstream_children > 0 && relay.secondary_sessions > 0 {
            "warm repair relay"
        } else if relay.downstream_children > 0 {
            "primary source relay"
        } else {
            "relay endpoint"
        };
        let state = if relay.errors() > 0
            || relay.forward_errors > 0
            || relay.failover_controller_state == "secondary_unavailable"
        {
            "attention"
        } else if relay.controlled_sessions > 0 || relay.authenticated_sessions > 0 {
            "sessions established"
        } else {
            "waiting"
        };
        let parents = format!(
            "{} primary · {} secondary",
            relay.primary_sessions, relay.secondary_sessions
        );
        let received = format!("{} / {}", relay.source_datagrams, relay.repair_datagrams);
        let forwarded = format!(
            "{} / {}",
            relay.forwarded_source_datagrams, relay.forwarded_repair_datagrams
        );
        let failover = if relay.failover_controller_enabled > 0 {
            format!(
                "{} · {}↑ {}↓",
                nonempty_owned(relay.failover_controller_state.clone(), "arming"),
                relay.failover_promotions,
                relay.failover_demotions
            )
        } else if relay.failover_listeners > 0 {
            format!(
                "{} promoted / {} warm",
                relay.failover_promoted_children, relay.failover_listeners
            )
        } else {
            "—".to_owned()
        };
        let forward_latency = format!(
            "{} / {}",
            format_optional_duration(relay.forward_percentile_us(95)),
            if relay.forward_duration_count > 0 {
                format_duration_us(relay.forward_duration_max_us)
            } else {
                "—".to_owned()
            }
        );
        let processing_latency = format!(
            "{} / {}",
            format_optional_duration(relay.processing_percentile_us(95)),
            format_optional_duration(relay.processing_percentile_us(99))
        );
        let availability_latency = relay
            .publication_to_available_percentile_us(95)
            .map(|duration_us| {
                format!(
                    "{} · clock ±{}",
                    format_duration_us(duration_us),
                    format_duration_us(relay.publication_clock_error_max_us)
                )
            })
            .unwrap_or_else(|| "pending".to_owned());
        let errors = relay
            .datagrams_rejected
            .saturating_add(relay.forward_errors);
        let downstream_children = relay.downstream_children;
        let fec_recovered = format!(
            "{} objects / {} symbols",
            relay.fec_recovered_objects, relay.fec_recovered_source_symbols
        );
        let warm_replay = format!(
            "{} buffered / {} replayed",
            relay.warm_source_buffered_datagrams, relay.warm_source_replayed_datagrams
        );
        let node_label = format!(
            "{} · {}",
            nonempty_owned(node.node_id, "relay"),
            nonempty_owned(node.region, "region pending")
        );
        view! {
            <tr>
                <td class="strong-cell">{node_label}</td>
                <td>{assignment}</td>
                <td><StatePill state=state.to_owned() /></td>
                <td>{parents}</td>
                <td class="mono-cell">{received}</td>
                <td>{downstream_children}</td>
                <td class="mono-cell">{forwarded}</td>
                <td class="mono-cell">{failover}</td>
                <td>{fec_recovered}</td>
                <td>{warm_replay}</td>
                <td class="mono-cell">{availability_latency}</td>
                <td class="mono-cell">{processing_latency}</td>
                <td class="mono-cell">{forward_latency}</td>
                <td>{errors}</td>
            </tr>
        }
    }

    #[component]
    fn RaptorSummary(
        contrib: ReadSignal<Option<ContribStatus>>,
        edge: ReadSignal<Option<MeshStatus>>,
    ) -> impl IntoView {
        view! {
            <div>
                <div class="raptor-grid">
                    <Metric label="Objects emitted" value=move || contrib.get().map(|s| s.runtime.relay_session.objects_sent.to_string()).unwrap_or_else(|| "—".to_owned()) detail="canonical objects" />
                    <Metric label="Source symbols" value=move || contrib.get().map(|s| s.runtime.relay_session.source_datagrams.to_string()).unwrap_or_else(|| "—".to_owned()) detail="primary lane" />
                    <Metric label="Repair symbols" value=move || contrib.get().map(|s| s.runtime.relay_session.repair_datagrams.to_string()).unwrap_or_else(|| "—".to_owned()) detail="warm-secondary lane" />
                    <Metric label="Repair overhead" value=move || contrib.get().and_then(|s| s.runtime.relay_session.repair_overhead_percent()).map(|value| format!("{value:.1}%")).unwrap_or_else(|| "—".to_owned()) detail="repair / all symbols" />
                    <Metric label="Primary lane" value=move || contrib.get().map(|s| format!("{} · {} ok / {} failed", nonempty_owned(s.runtime.relay_session.primary_lane_state, "unknown"), s.runtime.relay_session.primary_lane_objects_succeeded, s.runtime.relay_session.primary_lane_objects_failed)).unwrap_or_else(|| "—".to_owned()) detail="current state · cumulative outcomes" />
                    <Metric label="Warm lane" value=move || contrib.get().map(|s| format!("{} · {} ok / {} failed", nonempty_owned(s.runtime.relay_session.secondary_lane_state, "unknown"), s.runtime.relay_session.secondary_lane_objects_succeeded, s.runtime.relay_session.secondary_lane_objects_failed)).unwrap_or_else(|| "—".to_owned()) detail="current state · cumulative outcomes" />
                    <Metric label="Surviving-lane delivery" value=move || contrib.get().map(|s| s.runtime.relay_session.surviving_lane_objects.to_string()).unwrap_or_else(|| "—".to_owned()) detail="one parent completed" />
                    <Metric label="All lanes failed" value=move || contrib.get().map(|s| s.runtime.relay_session.all_lanes_failed_objects.to_string()).unwrap_or_else(|| "—".to_owned()) detail="availability failures" />
                    <Metric label="Contributor total p95" value=move || contrib.get().and_then(|s| s.runtime.relay_session.stages.total.percentile_us(95)).map(format_duration_us).unwrap_or_else(|| "pending".to_owned()) detail="object → emitted symbols" />
                    <Metric label="RaptorQ encode p95" value=move || contrib.get().and_then(|s| s.runtime.relay_session.stages.encode.percentile_us(95)).map(format_duration_us).unwrap_or_else(|| "pending".to_owned()) detail="canonical object protection" />
                    <Metric label="Scheduler p95" value=move || contrib.get().and_then(|s| s.runtime.relay_session.stages.schedule.percentile_us(95)).map(format_duration_us).unwrap_or_else(|| "pending".to_owned()) detail="source-first path assignment" />
                    <Metric label="Primary source send p95" value=move || contrib.get().and_then(|s| s.runtime.relay_session.stages.primary_source_send.percentile_us(95)).map(format_duration_us).unwrap_or_else(|| "pending".to_owned()) detail="per source symbol" />
                    <Metric label="Warm repair send p95" value=move || contrib.get().and_then(|s| s.runtime.relay_session.stages.secondary_repair_send.percentile_us(95)).map(format_duration_us).unwrap_or_else(|| "pending".to_owned()) detail="per repair symbol" />
                    <Metric label="Emission deadlines" value=move || contrib.get().map(|s| format!("{} hit / {} miss", s.runtime.relay_session.deadline_hits.unwrap_or(0), s.runtime.relay_session.deadline_misses.unwrap_or(0))).unwrap_or_else(|| "pending".to_owned()) detail="complete symbol plan" />
                    <Metric label="Decoded" value=move || edge.get().map(|s| s.relay_session.decoded_objects.to_string()).unwrap_or_else(|| "—".to_owned()) detail="verified objects" />
                    <Metric label="FEC-recovered objects" value=move || edge.get().map(|s| s.relay_session.fec_recovered_objects.to_string()).unwrap_or_else(|| "—".to_owned()) detail="objects decoded with missing source symbols" />
                    <Metric label="Recovered source symbols" value=move || edge.get().map(|s| s.relay_session.fec_recovered_source_symbols.to_string()).unwrap_or_else(|| "—".to_owned()) detail="exact source-symbol deficit reconstructed by RaptorQ" />
                    <Metric label="Repair-assisted decodes" value=move || edge.get().map(|s| s.relay_session.repair_assisted_objects.to_string()).unwrap_or_else(|| "—".to_owned()) detail="repair admitted before decode; does not imply source loss" />
                    <Metric label="Warm source buffer" value=move || edge.get().map(|s| format!("{} datagrams / {}", s.relay_session.warm_source_buffered_datagrams, format_bytes(s.relay_session.warm_source_buffered_bytes))).unwrap_or_else(|| "—".to_owned()) detail="unexpired source datagrams retained for promotion" />
                    <Metric label="Warm source replay" value=move || edge.get().map(|s| format!("{} datagrams / {}", s.relay_session.warm_source_replayed_datagrams, format_bytes(s.relay_session.warm_source_replayed_bytes))).unwrap_or_else(|| "—".to_owned()) detail="source state replayed immediately after promotion" />
                    <Metric label="Warm replay removals" value=move || edge.get().map(|s| format!("{} retired / {} expired / {} evicted", s.relay_session.warm_source_retired_datagrams, s.relay_session.warm_source_expired_datagrams, s.relay_session.warm_source_evicted_datagrams)).unwrap_or_else(|| "—".to_owned()) detail="completed-window retirement, deadline expiry, and hard-bound eviction" />
                    <Metric label="Relay processing p95" value=move || edge.get().and_then(|s| s.relay_session.processing_percentile_us(95)).map(format_duration_us).unwrap_or_else(|| "pending".to_owned()) detail="per datagram receive path" />
                    <Metric label="Relay processing p99" value=move || edge.get().and_then(|s| s.relay_session.processing_percentile_us(99)).map(format_duration_us).unwrap_or_else(|| "pending".to_owned()) detail="per datagram receive path" />
                    <Metric label="Publish → edge p95" value=move || edge.get().and_then(|s| s.relay_session.publication_to_available_percentile_us(95)).map(format_duration_us).unwrap_or_else(|| "pending".to_owned()) detail="verified cache availability" />
                    <Metric label="Epoch activation max" value=move || edge.get().and_then(|status| status.publication.canonical_epoch_activation_delay_us).map(format_duration_us).unwrap_or_else(|| "pending".to_owned()) detail="contributor incarnation → first object" />
                    <Metric label="Latency clock error" value=move || edge.get().filter(|s| s.relay_session.publication_to_available_count > 0).map(|s| format!("±{}", format_duration_us(s.relay_session.publication_clock_error_max_us))).unwrap_or_else(|| "pending".to_owned()) detail="source timestamp bound" />
                    <Metric label="Failover state" value=move || edge.get().filter(|s| s.relay_session.failover_controller_enabled > 0).map(|s| nonempty_owned(s.relay_session.failover_controller_state, "arming")).unwrap_or_else(|| "pending".to_owned()) detail="automatic warm-secondary control" />
                    <Metric label="Primary source age" value=move || edge.get().filter(|s| s.relay_session.failover_controller_enabled > 0).map(|s| format_age_ms(s.relay_session.failover_primary_source_age_ms)).unwrap_or_else(|| "pending".to_owned()) detail="failure detector input" />
                    <Metric label="Warm repair age" value=move || edge.get().filter(|s| s.relay_session.failover_controller_enabled > 0).map(|s| format_age_ms(s.relay_session.failover_secondary_repair_age_ms)).unwrap_or_else(|| "pending".to_owned()) detail="secondary readiness input" />
                    <Metric label="Last detection" value=move || edge.get().filter(|s| s.relay_session.failover_promotions > 0).map(|s| format_duration_us(s.relay_session.failover_last_detection_us)).unwrap_or_else(|| "not exercised".to_owned()) detail="primary silence → promotion" />
                    <Metric label="Promotion → source" value=move || edge.get().filter(|s| s.relay_session.failover_last_promotion_to_source_us > 0).map(|s| format_duration_us(s.relay_session.failover_last_promotion_to_source_us)).unwrap_or_else(|| "not exercised".to_owned()) detail="warm path activation" />
                    <Metric label="Maximum failover gap" value=move || edge.get().filter(|s| s.relay_session.failover_max_media_gap_us > 0).map(|s| format_duration_us(s.relay_session.failover_max_media_gap_us)).unwrap_or_else(|| "not exercised".to_owned()) detail="verified cache completions" />
                    <Metric label="Expired" value=move || edge.get().map(|s| s.relay_session.expired_objects.to_string()).unwrap_or_else(|| "—".to_owned()) detail="bounded receive state" />
                    <Metric label="Rejected" value=move || edge.get().map(|s| s.relay_session.datagrams_rejected.to_string()).unwrap_or_else(|| "—".to_owned()) detail="all receive drops" />
                </div>
                <div class="drop-strip">
                    <span>{move || edge.get().map(|s| format!("{} auth", s.relay_session.authentication_drops)).unwrap_or_else(|| "— auth".to_owned())}</span>
                    <span>{move || edge.get().map(|s| format!("{} conflicts", s.relay_session.conflict_drops)).unwrap_or_else(|| "— conflicts".to_owned())}</span>
                    <span>{move || edge.get().map(|s| format!("{} deadline", s.relay_session.deadline_drops)).unwrap_or_else(|| "— deadline".to_owned())}</span>
                    <span>{move || edge.get().map(|s| format!("{} duplicates", s.relay_session.duplicate_datagrams)).unwrap_or_else(|| "— duplicates".to_owned())}</span>
                    <span>{move || edge.get().map(|s| format!("{} authenticated / {} controlled sessions", s.relay_session.authenticated_sessions, s.relay_session.controlled_sessions)).unwrap_or_else(|| "trust telemetry pending".to_owned())}</span>
                    <span>{move || edge.get().map(|s| format!("{} active objects / {} buffered symbols", s.relay_session.active_objects, s.relay_session.buffered_datagrams)).unwrap_or_else(|| "receiver telemetry pending".to_owned())}</span>
                    <span>{move || edge.get().map(|s| format!("{} promotions / {} make-before-break demotions", s.relay_session.failover_promotions, s.relay_session.failover_demotions)).unwrap_or_else(|| "failover telemetry pending".to_owned())}</span>
                    <span>{move || edge.get().map(|s| format!("{} control errors / {} secondary-unavailable events", s.relay_session.failover_command_send_errors, s.relay_session.failover_secondary_unavailable_events)).unwrap_or_else(|| "failover health pending".to_owned())}</span>
                </div>
            </div>
        }
    }

    #[component]
    fn Metric<V>(label: &'static str, value: V, detail: &'static str) -> impl IntoView
    where
        V: Fn() -> String + Send + Sync + 'static,
    {
        view! {
            <article class="metric-card">
                <span>{label}</span>
                <strong>{value}</strong>
                <small>{detail}</small>
            </article>
        }
    }

    #[component]
    fn TelemetryTransportPanel(edge: ReadSignal<Option<MeshStatus>>) -> impl IntoView {
        view! {
            <section class="data-panel">
                <PanelTitle title="Global telemetry collection" detail="One elected fleet poller; followers copy the canonical snapshot" />
                <div class="telemetry-grid">
                    <Metric label="Mode" value=move || edge.get().map(|s| nonempty_owned(s.orchestration.telemetry_collection.mode, "pending")).unwrap_or_else(|| "pending".to_owned()) detail="collector transport" />
                    <Metric label="Fleet pollers" value=move || edge.get().map(|s| if s.orchestration.telemetry_collection.single_fleet_poller { "1 elected" } else { "unverified" }.to_owned()).unwrap_or_else(|| "pending".to_owned()) detail="duplicate polling guard" />
                    <Metric label="Sources current" value=move || edge.get().map(|s| format!("{} / {}", s.orchestration.telemetry_collection.sources_current, s.orchestration.telemetry_collection.sources_expected)).unwrap_or_else(|| "—".to_owned()) detail="latest canonical snapshot" />
                    <Metric label="Sources stale" value=move || edge.get().map(|s| s.orchestration.telemetry_collection.sources_stale.to_string()).unwrap_or_else(|| "—".to_owned()) detail="requires operator attention" />
                    <Metric label="Snapshot endpoint" value=move || edge.get().and_then(|s| s.orchestration.telemetry_collection.snapshot_endpoint).unwrap_or_else(|| "pending".to_owned()) detail="current collector API" />
                    <Metric label="Legacy FEC publishers" value=move || edge.get().map(|s| s.orchestration.telemetry_fec.publisher_targets.to_string()).unwrap_or_else(|| "—".to_owned()) detail="must remain zero in elected mode" />
                </div>
            </section>
        }
    }

    #[component]
    fn LatencyPanel(
        contrib: ReadSignal<Option<ContribStatus>>,
        edge: ReadSignal<Option<MeshStatus>>,
    ) -> impl IntoView {
        view! {
            <div class="performance-grid">
                <section class="data-panel latency-panel">
                    <PanelTitle title="Contributor stage histograms" detail="Cumulative processing time; p50/p95/p99" />
                    <div class="latency-stack">
                        <LatencyRow label="Ingest → relay" histogram=move || contrib.get().map(|s| contributor_latency(&s).clone()).unwrap_or_default() />
                        <LatencyRow label="Media encode wait" histogram=move || contrib.get().map(|s| s.runtime.mesh_forward.media_stages.encode_wait).unwrap_or_default() />
                        <LatencyRow label="Media encode" histogram=move || contrib.get().map(|s| s.runtime.mesh_forward.media_stages.encode).unwrap_or_default() />
                        <LatencyRow label="Media send" histogram=move || contrib.get().map(|s| s.runtime.mesh_forward.media_stages.send).unwrap_or_default() />
                        <LatencyRow label="Media telemetry" histogram=move || contrib.get().map(|s| s.runtime.mesh_forward.media_stages.telemetry).unwrap_or_default() />
                        <LatencyRow label="Stream encode wait" histogram=move || contrib.get().map(|s| s.runtime.mesh_forward.stream_stages.encode_wait).unwrap_or_default() />
                        <LatencyRow label="Stream send" histogram=move || contrib.get().map(|s| s.runtime.mesh_forward.stream_stages.send).unwrap_or_default() />
                    </div>
                </section>
                <section class="data-panel latency-panel">
                    <PanelTitle title="Relay and playback latency" detail="Publication-to-cache availability plus LL-HLS response handling" />
                    <div class="latency-stack">
                        <RelayProcessingRows edge />
                        <RelayAvailabilityRows edge />
                        <EdgeLatencyRows edge />
                    </div>
                    <div class="clock-strip">
                        <span>"Source epoch"</span>
                        <strong>{move || contrib.get().and_then(|s| s.mesh.media_object_source_epoch).map(|epoch| epoch.to_string()).unwrap_or_else(|| "pending".to_owned())}</strong>
                        <span>"Media-object clock"</span>
                        <strong>{move || contrib.get().map(|s| nonempty_owned(s.mesh.media_object_clock_id, "pending")).unwrap_or_else(|| "pending".to_owned())}</strong>
                        <span>"Confidence"</span>
                        <strong>{move || contrib.get().map(|s| nonempty_owned(s.mesh.media_object_clock_confidence, "pending")).unwrap_or_else(|| "pending".to_owned())}</strong>
                        <span>"Estimated error"</span>
                        <strong>{move || contrib.get().map(|s| format!("±{} ms", s.mesh.media_object_clock_estimated_error_ms)).unwrap_or_else(|| "pending".to_owned())}</strong>
                    </div>
                </section>
            </div>
        }
    }

    #[component]
    fn LatencyRow<H>(label: &'static str, histogram: H) -> impl IntoView
    where
        H: Fn() -> DurationHistogram + Clone + Send + Sync + 'static,
    {
        let histogram = StoredValue::new(histogram);
        view! {
            <article class="latency-row">
                <p>{label}</p>
                <div><span>"p50"</span><strong>{move || format_optional_duration(histogram.get_value()().percentile_us(50))}</strong></div>
                <div><span>"p95"</span><strong>{move || format_optional_duration(histogram.get_value()().percentile_us(95))}</strong></div>
                <div><span>"p99"</span><strong>{move || format_optional_duration(histogram.get_value()().percentile_us(99))}</strong></div>
            </article>
        }
    }

    #[component]
    fn RelayProcessingRows(edge: ReadSignal<Option<MeshStatus>>) -> impl IntoView {
        view! {
            <For
                each=move || edge.with(|status| {
                    status
                        .as_ref()
                        .map(bounded_relay_nodes)
                        .unwrap_or_default()
                })
                key=|node| format!("relay-processing:{}:{}", node.node_id, node.region)
                let(node)
            >
                <RelayProcessingRow node />
            </For>
            {move || edge.with(|status| status.as_ref().is_some_and(|status| bounded_relay_nodes(status).iter().all(|node| node.relay_session.processing_duration_count == 0))).then(|| view! {
                <p class="awaiting">"Relay processing latency is waiting for received media datagrams."</p>
            })}
        }
    }

    #[component]
    fn RelayProcessingRow(node: RelayNodeSession) -> impl IntoView {
        let relay = node.relay_session;
        let label = format!(
            "{} · {} relay processing",
            nonempty_owned(node.node_id, "relay"),
            nonempty_owned(node.region, "region pending")
        );
        let p50 = relay.processing_percentile_us(50);
        let p95 = relay.processing_percentile_us(95);
        let p99 = relay.processing_percentile_us(99);
        let class = if p95.is_some_and(|duration_us| duration_us > 1_000) {
            "latency-row attention"
        } else {
            "latency-row"
        };
        view! {
            <article class=class>
                <p>{label}</p>
                <div><span>"p50"</span><strong>{format_optional_duration(p50)}</strong></div>
                <div><span>"p95"</span><strong>{format_optional_duration(p95)}</strong></div>
                <div><span>"p99"</span><strong>{format_optional_duration(p99)}</strong></div>
            </article>
        }
    }

    #[component]
    fn RelayAvailabilityRows(edge: ReadSignal<Option<MeshStatus>>) -> impl IntoView {
        view! {
            <For
                each=move || edge.with(|status| {
                    status
                        .as_ref()
                        .map(bounded_relay_nodes)
                        .unwrap_or_default()
                })
                key=|node| format!("relay-availability:{}:{}", node.node_id, node.region)
                let(node)
            >
                <RelayAvailabilityRow node />
            </For>
            {move || edge.with(|status| status.as_ref().is_some_and(|status| bounded_relay_nodes(status).iter().all(|node| node.relay_session.publication_to_available_count == 0))).then(|| view! {
                <p class="awaiting">"Publication-to-cache latency is waiting for clock-qualified canonical media."</p>
            })}
        }
    }

    #[component]
    fn RelayAvailabilityRow(node: RelayNodeSession) -> impl IntoView {
        let relay = node.relay_session;
        let label = format!(
            "{} · {} publish → available",
            nonempty_owned(node.node_id, "relay"),
            nonempty_owned(node.region, "region pending")
        );
        let p50 = relay.publication_to_available_percentile_us(50);
        let p95 = relay.publication_to_available_percentile_us(95);
        let p99 = relay.publication_to_available_percentile_us(99);
        view! {
            <article class="latency-row">
                <p>{label}</p>
                <div><span>"p50"</span><strong>{format_optional_duration(p50)}</strong></div>
                <div><span>"p95"</span><strong>{format_optional_duration(p95)}</strong></div>
                <div><span>"p99"</span><strong>{format_optional_duration(p99)}</strong></div>
            </article>
        }
    }

    #[component]
    fn EdgeLatencyRows(edge: ReadSignal<Option<MeshStatus>>) -> impl IntoView {
        view! {
            <For
                each=move || edge.get().map(|status| bounded_edges(&status).into_iter().take(8).collect::<Vec<_>>()).unwrap_or_default()
                key=|service| format!("{}:{}", service.node_id, service.region)
                let(service)
            >
                <EdgeLatencyRow service />
            </For>
            {move || edge.get().is_some_and(|status| status.edge_services.is_empty()).then(|| view! {
                <p class="awaiting">"Playback-edge latency is waiting for its first LL-HLS response."</p>
            })}
        }
    }

    #[component]
    fn EdgeLatencyRow(service: EdgeService) -> impl IntoView {
        let label = format!(
            "{} · {} LL-HLS",
            nonempty_owned(service.node_id.clone(), "edge"),
            nonempty_owned(service.region.clone(), "region pending")
        );
        let p50 = service.percentile_us(50);
        let p95 = service.percentile_us(95);
        let p99 = service.percentile_us(99);
        let class = if service.response_errors > 0 {
            "latency-row attention"
        } else {
            "latency-row"
        };
        view! {
            <article class=class>
                <p>{label}</p>
                <div><span>"p50"</span><strong>{format_optional_duration(p50)}</strong></div>
                <div><span>"p95"</span><strong>{format_optional_duration(p95)}</strong></div>
                <div><span>"p99"</span><strong>{format_optional_duration(p99)}</strong></div>
            </article>
        }
    }

    #[component]
    fn AlertActivity(
        contrib: ReadSignal<Option<ContribStatus>>,
        edge: ReadSignal<Option<MeshStatus>>,
    ) -> impl IntoView {
        view! {
            <div class="event-grid">
                <section class="data-panel event-panel">
                    <PanelTitle title="Active alerts" detail="Latest bounded health signals" />
                    <EventList events=move || operational_alerts(contrib.get().as_ref(), edge.get().as_ref()) empty="No active service alerts." />
                </section>
                <section class="data-panel event-panel">
                    <PanelTitle title="Recent activity" detail="Latest bounded delivery events" />
                    <EventList events=move || operational_activity(contrib.get().as_ref(), edge.get().as_ref()) empty="Waiting for service activity." />
                </section>
            </div>
        }
    }

    #[component]
    fn EventList<E>(events: E, empty: &'static str) -> impl IntoView
    where
        E: Fn() -> Vec<OperationalEvent> + Clone + Send + Sync + 'static,
    {
        let events = StoredValue::new(events);
        view! {
            <div class="event-list">
                <For
                    each=move || events.get_value()()
                    key=|event| format!("{}:{}:{}", event.source.label(), event.code, event.seen_unix_ms)
                    let(event)
                >
                    <EventRow event />
                </For>
                {move || events.get_value()().is_empty().then(|| view! { <p class="awaiting">{empty}</p> })}
            </div>
        }
    }

    #[component]
    fn EventRow(event: OperationalEvent) -> impl IntoView {
        let source_class = match event.source {
            EventSource::Contributor => "contributor",
            EventSource::Delivery => "delivery",
        };
        view! {
            <article class=format!("event-row {} {}", state_tone(&event.level), source_class)>
                <div class="event-meta">
                    <span>{event.source.label()}</span>
                    <b>{humanize_code(&event.code)}</b>
                    <time>{format_event_time(event.seen_unix_ms)}</time>
                </div>
                <p>{event.message}</p>
                <small>{format!("{} occurrence{}{}", event.count, if event.count == 1 { "" } else { "s" }, event.context.map(|context| format!(" · {context}")).unwrap_or_default())}</small>
            </article>
        }
    }

    #[component]
    fn PanelTitle(title: &'static str, detail: &'static str) -> impl IntoView {
        view! { <header class="panel-title"><h3>{title}</h3><span>{detail}</span></header> }
    }

    #[component]
    fn StatePill(state: String) -> impl IntoView {
        let tone = state_tone(&state);
        view! { <span class=format!("state-pill {tone}")>{state}</span> }
    }

    #[component]
    fn AwaitedTelemetry(
        contrib: ReadSignal<Option<ContribStatus>>,
        edge: ReadSignal<Option<MeshStatus>>,
    ) -> impl IntoView {
        view! {
            <section class="awaited">
                <div><p>"MISSING TELEMETRY"</p><h2>"Fields not reported by the current services"</h2></div>
                <ul>
                    {move || {
                        let delivery = effective_delivery(edge.get().as_ref());
                        (delivery.delivery_class.is_none() || delivery.generation.is_none() || delivery.fabric_label().is_none())
                            .then(|| view! { <li>"Delivery class, fabric, generation, and installed route state"</li> })
                    }}
                    {move || {
                        let delivery = effective_delivery(edge.get().as_ref());
                        (delivery.primary.as_ref().and_then(|lane| lane.node_id.as_ref()).is_none()
                            || delivery.secondary.as_ref().and_then(|lane| lane.node_id.as_ref()).is_none()
                            || delivery.path_stretch.is_none())
                            .then(|| view! { <li>"Parent identities, failure-domain diversity, RTT, jitter, loss, and path stretch"</li> })
                    }}
                    {move || contrib.get().is_some_and(|s| s.runtime.relay_session.deadline_hits.is_none() || s.runtime.relay_session.deadline_misses.is_none()).then(|| view! { <li>"Per-object deadline hit/miss and sender expiry totals"</li> })}
                    {move || {
                        let contributor_publication = contrib.get().map(|status| status.publication).unwrap_or_default();
                        let edge_publication = edge.get().map(|status| status.publication).unwrap_or_default();
                        (contributor_publication.contiguous_object.is_none() || contributor_publication.gap_count.is_none() || edge_publication.contiguous_object.is_none() || edge_publication.gap_count.is_none())
                            .then(|| view! { <li>"Contiguous publication watermarks and known-gap totals"</li> })
                    }}
                    {move || contrib.get().is_some_and(|s| s.runtime.protocols.iter().all(|protocol| protocol.last_seen_age_ms.is_none())).then(|| view! { <li>"Per-session RTT, jitter, loss, reconnect, and end-reason telemetry"</li> })}
                </ul>
            </section>
        }
    }

    fn route_rows(edge: Option<&MeshStatus>) -> Vec<DeliverySnapshot> {
        if let Some(status) = edge.filter(|status| !status.routes.is_empty()) {
            return status.routes.iter().take(12).cloned().collect();
        }
        let delivery = effective_delivery(edge);
        delivery
            .has_assignment()
            .then_some(delivery)
            .into_iter()
            .collect()
    }

    fn route_lane_compact(lane: Option<&RouteLane>) -> String {
        lane.map(|lane| {
            [
                lane.node_id.as_deref(),
                lane.target.as_deref(),
                lane.carrier.as_deref(),
                lane.state.as_deref(),
            ]
            .into_iter()
            .flatten()
            .collect::<Vec<_>>()
            .join(" · ")
        })
        .filter(|summary| !summary.is_empty())
        .unwrap_or_else(|| "pending".to_owned())
    }

    fn protocol_runtime(status: &ContribStatus, protocol: &str) -> Option<ProtocolRuntime> {
        status
            .runtime
            .protocols
            .iter()
            .find(|runtime| runtime.protocol.eq_ignore_ascii_case(protocol))
            .cloned()
    }

    fn listener_detail(listener: &ListenerStatus) -> String {
        nonempty_owned(
            [
                listener.backend.as_deref(),
                listener.profile.as_deref(),
                listener.flow_id.as_deref(),
            ]
            .into_iter()
            .flatten()
            .collect::<Vec<_>>()
            .join(" · "),
            "standard listener",
        )
    }

    fn codec_summary(
        video_codec: &Option<String>,
        width: Option<u64>,
        height: Option<u64>,
        audio_codec: &Option<String>,
    ) -> String {
        let video = video_codec.as_deref().map(|codec| match (width, height) {
            (Some(width), Some(height)) => format!("{codec} {width}×{height}"),
            _ => codec.to_owned(),
        });
        nonempty_owned(
            [video, audio_codec.clone()]
                .into_iter()
                .flatten()
                .collect::<Vec<_>>()
                .join(" + "),
            "codec pending",
        )
    }

    fn edge_stream_state(stale: Option<bool>, lag: Option<u64>) -> String {
        if stale == Some(true) {
            "stale".to_owned()
        } else if lag.is_some_and(|value| value > 0) {
            "lagging".to_owned()
        } else if stale == Some(false) {
            "current".to_owned()
        } else {
            "telemetry pending".to_owned()
        }
    }

    fn current_page() -> Page {
        let hash = web_sys::window()
            .and_then(|window| window.location().hash().ok())
            .unwrap_or_default();
        Page::from_hash(&hash)
    }

    fn document_is_visible() -> bool {
        web_sys::window()
            .and_then(|window| window.document())
            .is_none_or(|document| document.visibility_state() != web_sys::VisibilityState::Hidden)
    }

    fn contrib_counter_sample(status: &ContribStatus, at_unix_ms: u64) -> ContribCounters {
        let protocol_bytes = status
            .runtime
            .protocols
            .iter()
            .fold(0_u64, |total, protocol| {
                total.saturating_add(protocol.bytes)
            });
        let input_bytes = if protocol_bytes > 0 {
            protocol_bytes
        } else {
            status
                .runtime
                .media_access_units
                .payload_bytes
                .max(status.runtime.raw_http.bytes)
                .max(status.runtime.mpeg_ts.bytes)
                .max(status.runtime.rtmp.bytes)
        };
        ContribCounters {
            at_unix_ms,
            input_bytes,
            relay_bytes: status
                .runtime
                .relay_session
                .source_datagram_bytes
                .saturating_add(status.runtime.relay_session.repair_datagram_bytes),
        }
    }

    fn edge_counter_sample(status: &MeshStatus, at_unix_ms: u64) -> EdgeCounters {
        EdgeCounters {
            at_unix_ms,
            delivery_bytes: status.edge_services.iter().fold(0_u64, |total, service| {
                total.saturating_add(service.bytes_served)
            }),
            decoded_objects: status.relay_session.decoded_objects,
        }
    }

    fn record_rate_history(history: &mut Vec<TrafficRates>, sample: TrafficRates) {
        if sample.input_bps.is_none()
            && sample.relay_bps.is_none()
            && sample.delivery_bps.is_none()
            && sample.objects_per_second.is_none()
        {
            return;
        }
        if let Some(last) = history
            .last_mut()
            .filter(|last| sample.at_unix_ms.saturating_sub(last.at_unix_ms) < 1_000)
        {
            *last = sample;
        } else {
            history.push(sample);
        }
        if history.len() > RATE_HISTORY_POINTS {
            history.drain(..history.len() - RATE_HISTORY_POINTS);
        }
    }

    fn format_rate(metric: RateMetric, value: Option<f64>) -> String {
        let Some(value) = value.filter(|value| value.is_finite() && *value >= 0.0) else {
            return "collecting".to_owned();
        };
        if matches!(metric, RateMetric::Objects) {
            if value >= 100.0 {
                format!("{value:.0}/s")
            } else {
                format!("{value:.1}/s")
            }
        } else {
            format_throughput_bps(value)
        }
    }

    fn format_throughput_bps(value: f64) -> String {
        if value >= 1_000_000_000.0 {
            format!("{:.2} Gbit/s", value / 1_000_000_000.0)
        } else if value >= 1_000_000.0 {
            format!("{:.2} Mbit/s", value / 1_000_000.0)
        } else if value >= 1_000.0 {
            format!("{:.2} Kbit/s", value / 1_000.0)
        } else {
            format!("{value:.0} bit/s")
        }
    }

    fn sparkline_path(history: &[TrafficRates], metric: RateMetric) -> String {
        let values = history
            .iter()
            .filter_map(|sample| metric.value(sample))
            .filter(|value| value.is_finite() && *value >= 0.0)
            .collect::<Vec<_>>();
        if values.len() < 2 {
            return String::new();
        }
        let maximum = values.iter().copied().fold(0.0_f64, f64::max).max(1.0);
        let denominator = (values.len() - 1) as f64;
        values
            .iter()
            .enumerate()
            .map(|(index, value)| {
                let x = index as f64 * 240.0 / denominator;
                let y = 48.0 - (value / maximum * 44.0);
                format!("{} {x:.2} {y:.2}", if index == 0 { "M" } else { "L" })
            })
            .collect::<Vec<_>>()
            .join(" ")
    }

    fn network_links(status: &MeshStatus) -> Vec<NetworkLink> {
        let nodes = bounded_nodes(status);
        status
            .topology_links
            .iter()
            .take(64)
            .enumerate()
            .filter_map(|(index, link)| {
                let from = nodes
                    .iter()
                    .find(|node| node.node_id == link.from_node_id)?;
                let to = nodes.iter().find(|node| node.node_id == link.to_node_id)?;
                if from.node_id == to.node_id {
                    return None;
                }
                let path = map_link_path(from, to, &link.role)?;
                Some(NetworkLink {
                    key: format!(
                        "{}:{}:{}:{}:{}",
                        link.role,
                        from.node_id,
                        to.node_id,
                        link.generation.unwrap_or_default(),
                        index
                    ),
                    from: from.clone(),
                    to: to.clone(),
                    role: nonempty_owned(link.role.clone(), "Topology link"),
                    state: link.state.clone(),
                    tone: worst_tone(
                        state_tone(&link.state),
                        worst_tone(node_health_tone(status, from), node_health_tone(status, to)),
                    ),
                    throughput_bps: link.throughput_bps,
                    rtt_us: link.rtt_us,
                    generation: link.generation,
                    path,
                })
            })
            .collect()
    }

    fn format_map_node_scale(status: &MeshStatus) -> String {
        let shown = map_clusters(status)
            .iter()
            .map(|cluster| cluster.nodes.len())
            .sum::<usize>();
        let total = status.nodes.len().max(status.aggregate.node_count);
        format!("{shown} / {total}")
    }

    fn format_map_link_scale(status: &MeshStatus) -> String {
        format!(
            "{} / {}",
            network_links(status).len(),
            status.topology_links.len()
        )
    }

    fn node_coordinates(node: &EdgeNode) -> Option<(f64, f64)> {
        let latitude = node.latitude?;
        let longitude = node.longitude?;
        (latitude.is_finite()
            && longitude.is_finite()
            && (-90.0..=90.0).contains(&latitude)
            && (-180.0..=180.0).contains(&longitude))
        .then_some((latitude, longitude))
    }

    fn project_node(node: &EdgeNode) -> Option<(f64, f64)> {
        let (latitude, longitude) = node_coordinates(node)?;
        Some((
            (longitude + 180.0) / 360.0 * 100.0,
            (90.0 - latitude) / 180.0 * 100.0,
        ))
    }

    fn map_link_path(from: &EdgeNode, to: &EdgeNode, role: &str) -> Option<String> {
        let (from_x, from_y) = project_node(from)?;
        let (to_x, to_y) = project_node(to)?;
        if (from_x - to_x).abs() < 0.01 && (from_y - to_y).abs() < 0.01 {
            let x = from_x * 10.0;
            let y = from_y * 5.0;
            let direction = if link_role_class(role) == "warm" {
                1.0
            } else {
                -1.0
            };
            return Some(format!(
                "M {:.2} {:.2} C {:.2} {:.2}, {:.2} {:.2}, {:.2} {:.2}",
                x - 16.0,
                y,
                x - 16.0,
                y + 34.0 * direction,
                x + 16.0,
                y + 34.0 * direction,
                x + 16.0,
                y
            ));
        }
        let from_x = from_x * 10.0;
        let from_y = from_y * 5.0;
        let to_x = to_x * 10.0;
        let to_y = to_y * 5.0;
        let dx = to_x - from_x;
        let dy = to_y - from_y;
        let distance = (dx * dx + dy * dy).sqrt().max(1.0);
        let bend = (distance * 0.12).clamp(10.0, 42.0);
        let direction = if link_role_class(role) == "warm" {
            -1.0
        } else {
            1.0
        };
        let control_x = (from_x + to_x) / 2.0 - dy / distance * bend * direction;
        let control_y = (from_y + to_y) / 2.0 + dx / distance * bend * direction;
        Some(format!(
            "M {from_x:.2} {from_y:.2} Q {control_x:.2} {control_y:.2} {to_x:.2} {to_y:.2}"
        ))
    }

    fn format_network_link_title(link: &NetworkLink) -> String {
        let mut details = vec![format!(
            "{}: {} to {}",
            link.role, link.from.node_id, link.to.node_id
        )];
        if let Some(rtt_us) = link.rtt_us {
            details.push(format!("RTT {}", format_duration_us(rtt_us)));
        }
        if let Some(throughput_bps) = link.throughput_bps {
            details.push(format!("throughput {}", format_bps(throughput_bps)));
        }
        if let Some(generation) = link.generation {
            details.push(format!("generation {generation}"));
        }
        details.join(" · ")
    }

    fn link_role_class(role: &str) -> &'static str {
        let role = role.to_ascii_lowercase();
        if role.contains("warm") || role.contains("secondary") || role.contains("repair") {
            "warm"
        } else if role.contains("control") {
            "control"
        } else {
            "primary"
        }
    }

    fn node_health_tone(status: &MeshStatus, node: &EdgeNode) -> &'static str {
        let snapshot_tone = match snapshot_age_ms(status.updated_unix_ms) {
            None => "error",
            Some(age) if age > SNAPSHOT_STALE_AFTER_MS => "warn",
            Some(_) => "healthy",
        };
        let node_tone = if status
            .telemetry
            .stale_nodes
            .iter()
            .any(|stale| stale.node_id == node.node_id)
        {
            "error"
        } else if node.draining {
            "warn"
        } else if node.node_id == status.node.node_id {
            relay_health_tone(&status.relay_session)
        } else if let Some(relay) = status
            .relay_nodes
            .iter()
            .find(|relay| relay.node_id == node.node_id)
        {
            relay_health_tone(&relay.relay_session)
        } else {
            "error"
        };
        worst_tone(snapshot_tone, node_tone)
    }

    fn node_health_label(status: &MeshStatus, node: &EdgeNode) -> &'static str {
        if status
            .telemetry
            .stale_nodes
            .iter()
            .any(|stale| stale.node_id == node.node_id)
        {
            return "Stale";
        }
        match snapshot_age_ms(status.updated_unix_ms) {
            None => return "Unavailable",
            Some(age) if age > SNAPSHOT_STALE_AFTER_MS => return "Stale",
            Some(_) => {}
        }
        match node_health_tone(status, node) {
            "error" => "Unavailable",
            "warn" => "Degraded",
            _ => "Healthy",
        }
    }

    fn node_snapshot_label(status: &MeshStatus, node: &EdgeNode) -> String {
        if let Some(stale) = status
            .telemetry
            .stale_nodes
            .iter()
            .find(|stale| stale.node_id == node.node_id)
        {
            return format!("stale {}", format_age(stale.age_ms));
        }
        node.updated_unix_ms
            .map(|updated| format_age(now_unix_ms().saturating_sub(updated)))
            .unwrap_or_else(|| {
                if node.node_id == status.node.node_id {
                    "current".to_owned()
                } else {
                    "age unreported".to_owned()
                }
            })
    }

    fn relay_health_tone(relay: &needletail_mission_control::RelayIngress) -> &'static str {
        let state = relay.failover_controller_state.to_ascii_lowercase();
        if relay.errors() > 0
            || state.contains("unavailable")
            || state.contains("failed")
            || state.contains("error")
        {
            "error"
        } else if state.contains("degraded") {
            "warn"
        } else {
            "healthy"
        }
    }

    fn worst_tone(left: &'static str, right: &'static str) -> &'static str {
        if left == "error" || right == "error" {
            "error"
        } else if left == "warn" || right == "warn" {
            "warn"
        } else {
            "healthy"
        }
    }

    fn map_clusters(status: &MeshStatus) -> Vec<MapCluster> {
        let mut clusters = Vec::<MapCluster>::new();
        for node in bounded_nodes(status) {
            let Some((latitude, longitude)) = node_coordinates(&node) else {
                continue;
            };
            let Some((left, top)) = project_node(&node) else {
                continue;
            };
            let key = format!("{latitude:.3}:{longitude:.3}");
            if let Some(cluster) = clusters.iter_mut().find(|cluster| cluster.key == key) {
                cluster.nodes.push(node);
            } else {
                clusters.push(MapCluster {
                    key,
                    nodes: vec![node],
                    left,
                    top,
                });
            }
        }
        clusters
    }

    fn same_node_location(left: &EdgeNode, right: &EdgeNode) -> bool {
        match (node_coordinates(left), node_coordinates(right)) {
            (Some((left_latitude, left_longitude)), Some((right_latitude, right_longitude))) => {
                (left_latitude - right_latitude).abs() < 0.001
                    && (left_longitude - right_longitude).abs() < 0.001
            }
            _ => false,
        }
    }

    fn format_node_location(node: &EdgeNode) -> String {
        node_coordinates(node)
            .map(|(latitude, longitude)| format!("{latitude:.3}, {longitude:.3}"))
            .unwrap_or_else(|| "unreported".to_owned())
    }

    fn cluster_health_tone(status: &MeshStatus, cluster: &MapCluster) -> &'static str {
        cluster.nodes.iter().fold("healthy", |tone, node| {
            worst_tone(tone, node_health_tone(status, node))
        })
    }

    async fn fetch_mesh_snapshot(url: &str) -> Result<MeshStatus, String> {
        let controller = web_sys::AbortController::new()
            .map_err(|_| "feed request: browser abort control unavailable".to_owned())?;
        let signal = controller.signal();
        let abort_controller = controller.clone();
        let timeout = Timeout::new(FETCH_TIMEOUT_MS, move || abort_controller.abort());
        let response = Request::get(url)
            .header("accept", "application/json")
            .abort_signal(Some(&signal))
            .send()
            .await
            .map_err(|error| format!("feed request: {error}"))?;
        if !response.ok() {
            return Err(format!("feed returned HTTP {}", response.status()));
        }
        if let Some(length) = response.headers().get("content-length") {
            let length = length
                .parse::<usize>()
                .map_err(|_| "feed returned an invalid content-length".to_owned())?;
            if length > MAX_SNAPSHOT_BYTES {
                return Err(format!(
                    "snapshot exceeds the {} byte limit",
                    MAX_SNAPSHOT_BYTES
                ));
            }
        }
        let bytes = response
            .binary()
            .await
            .map_err(|error| format!("snapshot body: {error}"))?;
        if bytes.len() > MAX_SNAPSHOT_BYTES {
            return Err(format!(
                "snapshot exceeds the {} byte limit",
                MAX_SNAPSHOT_BYTES
            ));
        }
        let snapshot = serde_json::from_slice::<MeshStatus>(&bytes)
            .map_err(|error| format!("snapshot decode: {error}"))?;
        drop(timeout);
        if snapshot.schema != OPERATIONS_SNAPSHOT_SCHEMA {
            return Err(format!(
                "snapshot schema unsupported: expected {OPERATIONS_SNAPSHOT_SCHEMA}"
            ));
        }
        Ok(snapshot)
    }

    fn trust_label(trust: Option<&str>) -> &str {
        trust.unwrap_or("trust profile pending")
    }

    fn actionable_alert_count(contrib: Option<&ContribStatus>, edge: Option<&MeshStatus>) -> usize {
        operational_alerts(contrib, edge)
            .into_iter()
            .filter(|event| {
                matches!(
                    event.level.to_ascii_lowercase().as_str(),
                    "alert"
                        | "attention"
                        | "critical"
                        | "degraded"
                        | "emergency"
                        | "error"
                        | "fatal"
                        | "warn"
                        | "warning"
                )
            })
            .count()
    }

    fn effective_collector_lease_ms(
        collector: &OperationsCollectorStatus,
        snapshot_unix_ms: u64,
    ) -> Option<u64> {
        let snapshot_age_ms = snapshot_age_ms(snapshot_unix_ms)?;
        collector
            .lease_remaining_ms
            .map(|lease| lease.saturating_sub(snapshot_age_ms))
    }

    fn collector_is_current(collector: &OperationsCollectorStatus, snapshot_unix_ms: u64) -> bool {
        collector.has_committed_live_lease()
            && effective_collector_lease_ms(collector, snapshot_unix_ms)
                .is_some_and(|lease| lease > 0)
            && snapshot_age_ms(snapshot_unix_ms).is_some_and(|age| age <= SNAPSHOT_STALE_AFTER_MS)
    }

    fn verified_collector_node_id(
        collector: &OperationsCollectorStatus,
        snapshot_unix_ms: u64,
    ) -> Option<String> {
        collector_is_current(collector, snapshot_unix_ms)
            .then(|| collector.leader_node_id.clone())
            .flatten()
    }

    fn collector_tone(
        collector: &OperationsCollectorStatus,
        snapshot_unix_ms: u64,
    ) -> &'static str {
        let effective_lease_ms = effective_collector_lease_ms(collector, snapshot_unix_ms);
        if !collector.reported() {
            "warn"
        } else if collector.quorum_healthy == Some(false) || effective_lease_ms == Some(0) {
            "error"
        } else if collector_is_current(collector, snapshot_unix_ms) {
            "healthy"
        } else {
            "warn"
        }
    }

    fn collector_state_label(
        collector: &OperationsCollectorStatus,
        snapshot_unix_ms: u64,
    ) -> String {
        let snapshot_age_ms = snapshot_age_ms(snapshot_unix_ms);
        let effective_lease_ms = effective_collector_lease_ms(collector, snapshot_unix_ms);
        if !collector.reported() {
            "Election telemetry unavailable".to_owned()
        } else if collector.quorum_healthy == Some(false) {
            "Quorum unavailable — collector fenced".to_owned()
        } else if effective_lease_ms == Some(0) {
            "Collector lease expired".to_owned()
        } else if snapshot_age_ms.is_none_or(|age| age > SNAPSHOT_STALE_AFTER_MS) {
            "Collector proof stale".to_owned()
        } else if !collector.has_committed_live_lease() {
            "Election proof incomplete".to_owned()
        } else {
            match collector.role.to_ascii_lowercase().as_str() {
                "leader" | "collector" => "Collecting global snapshots".to_owned(),
                "candidate" => "Election in progress".to_owned(),
                _ => collector
                    .leader_node_id
                    .as_ref()
                    .map(|leader| format!("Following {leader}"))
                    .unwrap_or_else(|| "Collector pending".to_owned()),
            }
        }
    }

    fn collector_detail(collector: &OperationsCollectorStatus) -> String {
        if !collector.reported() {
            return "The active feed does not expose quorum, lease, or fencing state.".to_owned();
        }
        let authority = nonempty_owned(collector.authority.clone(), "node quorum");
        let region = collector
            .leader_region
            .clone()
            .unwrap_or_else(|| "region unreported".to_owned());
        let change = collector
            .last_change_reason
            .clone()
            .unwrap_or_else(|| "leadership reason unreported".to_owned());
        let changed = collector
            .last_leadership_change_unix_ms
            .map(format_event_time)
            .unwrap_or_else(|| "change time unreported".to_owned());
        format!("{authority} · {region} · {change} · {changed}")
    }

    fn format_collector_quorum(collector: &OperationsCollectorStatus) -> String {
        match (
            collector.quorum_healthy,
            collector.voters_online,
            collector.voters_total,
        ) {
            (Some(healthy), Some(online), Some(total)) => format!(
                "{online}/{total} {}",
                if healthy { "healthy" } else { "unavailable" }
            ),
            (Some(healthy), _, _) => {
                if healthy {
                    "healthy".to_owned()
                } else {
                    "unavailable".to_owned()
                }
            }
            _ => "unreported".to_owned(),
        }
    }

    fn format_global_snapshot_freshness(status: Option<&MeshStatus>) -> String {
        let Some(status) = status else {
            return "unavailable".to_owned();
        };
        let collector = &status.orchestration.collector;
        match (
            collector.nodes_current,
            collector.nodes_stale,
            collector.nodes_awaiting,
        ) {
            (Some(current), Some(stale), Some(awaiting)) => {
                format!("{current} current · {stale} stale · {awaiting} awaiting")
            }
            _ => "fleet freshness unavailable".to_owned(),
        }
    }

    fn optional_u64(value: Option<u64>) -> String {
        value
            .map(|value| value.to_string())
            .unwrap_or_else(|| "pending".to_owned())
    }

    fn format_publication_value(value: Option<u64>) -> String {
        value
            .map(|value| value.to_string())
            .unwrap_or_else(|| "not reported".to_owned())
    }

    fn format_publication_duration(value: Option<u64>) -> String {
        value
            .map(format_duration_us)
            .unwrap_or_else(|| "not measured".to_owned())
    }

    fn format_optional_duration(value: Option<u64>) -> String {
        value
            .map(format_duration_us)
            .unwrap_or_else(|| "pending".to_owned())
    }

    fn format_optional_ppm(value: Option<u64>) -> String {
        value
            .map(|value| format!("{:.3}%", value as f64 / 10_000.0))
            .unwrap_or_else(|| "pending".to_owned())
    }

    fn format_duration_us(value: u64) -> String {
        if value >= 1_000_000 {
            format!("{:.2} s", value as f64 / 1_000_000.0)
        } else if value >= 1_000 {
            format!("{:.2} ms", value as f64 / 1_000.0)
        } else {
            format!("{value} µs")
        }
    }

    fn format_age_ms(value: u64) -> String {
        if value >= 1_000 {
            format!("{:.2} s", value as f64 / 1_000.0)
        } else {
            format!("{value} ms")
        }
    }

    fn format_age(value: u64) -> String {
        if value >= 60_000 {
            format!("{:.1} min ago", value as f64 / 60_000.0)
        } else if value >= 1_000 {
            format!("{:.1} s ago", value as f64 / 1_000.0)
        } else {
            format!("{value} ms ago")
        }
    }

    fn format_event_time(unix_ms: u64) -> String {
        if unix_ms == 0 {
            "time pending".to_owned()
        } else {
            format_age(now_unix_ms().saturating_sub(unix_ms))
        }
    }

    fn format_bytes(value: u64) -> String {
        const KIB: f64 = 1_024.0;
        const MIB: f64 = KIB * KIB;
        const GIB: f64 = MIB * KIB;
        let value = value as f64;
        if value >= GIB {
            format!("{:.2} GiB", value / GIB)
        } else if value >= MIB {
            format!("{:.2} MiB", value / MIB)
        } else if value >= KIB {
            format!("{:.1} KiB", value / KIB)
        } else {
            format!("{} B", value as u64)
        }
    }

    fn format_bps(value: u64) -> String {
        if value >= 1_000_000_000 {
            format!("{:.2} Gbps", value as f64 / 1_000_000_000.0)
        } else if value >= 1_000_000 {
            format!("{:.2} Mbps", value as f64 / 1_000_000.0)
        } else if value >= 1_000 {
            format!("{:.2} Kbps", value as f64 / 1_000.0)
        } else if value == 0 {
            "capacity pending".to_owned()
        } else {
            format!("{value} bps")
        }
    }

    fn humanize_code(code: &str) -> String {
        nonempty_owned(
            code.trim_start_matches("mesh_").replace('_', " "),
            "service event",
        )
    }

    fn nonempty_owned(value: String, fallback: &str) -> String {
        if value.is_empty() {
            fallback.to_owned()
        } else {
            value
        }
    }

    fn now_unix_ms() -> u64 {
        js_sys::Date::now().max(0.0) as u64
    }

    fn snapshot_age_ms(snapshot_unix_ms: u64) -> Option<u64> {
        let now = now_unix_ms();
        (snapshot_unix_ms > 0 && snapshot_unix_ms <= now.saturating_add(SNAPSHOT_CLOCK_SKEW_MS))
            .then_some(now.saturating_sub(snapshot_unix_ms))
    }
}

#[cfg(target_arch = "wasm32")]
fn main() {
    app::run();
}

#[cfg(not(target_arch = "wasm32"))]
fn main() {
    println!("Needletail Operations builds for wasm32-unknown-unknown");
}
