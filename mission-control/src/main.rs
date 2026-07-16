#[cfg(target_arch = "wasm32")]
mod app {
    use gloo_net::http::Request;
    use gloo_timers::callback::Interval;
    use leptos::{mount::mount_to_body, prelude::*};
    use needletail_mission_control::{
        bounded_contrib_streams, bounded_edge_streams, bounded_edges, bounded_ingest_sessions,
        bounded_nodes, contributor_latency, effective_delivery, operational_activity,
        operational_alerts, publication_from_contrib, publication_from_edge, ContribStatus,
        DeliverySnapshot, DurationHistogram, EdgeNode, EdgeService, EventSource, IngestSession,
        ListenerStatus, MeshStatus, OperationalEvent, ProtocolRuntime, PublicationSnapshot,
        RelayNodeSession, RouteLane, MAX_EVENT_ROWS,
    };
    use serde::de::DeserializeOwned;
    use wasm_bindgen_futures::spawn_local;

    const DEFAULT_EDGE_API: &str = "/api/mesh";
    const DEFAULT_CONTRIB_API: &str = "https://local.bitneedle.com:19443/api/status";
    const POLL_INTERVAL_MS: u32 = 2_000;

    pub fn run() {
        console_error_panic_hook::set_once();
        mount_to_body(App);
    }

    #[derive(Clone, Debug, Default)]
    struct FeedState {
        last_ok_unix_ms: Option<u64>,
        error: Option<String>,
    }

    impl FeedState {
        fn ok() -> Self {
            Self {
                last_ok_unix_ms: Some(now_unix_ms()),
                error: None,
            }
        }

        fn error(message: String, previous: &Self) -> Self {
            Self {
                last_ok_unix_ms: previous.last_ok_unix_ms,
                error: Some(message),
            }
        }

        fn label(&self) -> &'static str {
            if self.error.is_some() {
                "error"
            } else if self.last_ok_unix_ms.is_some() {
                "healthy"
            } else {
                "connecting"
            }
        }

        fn tone(&self) -> &'static str {
            if self.error.is_some() {
                "error"
            } else if self.last_ok_unix_ms.is_some() {
                "healthy"
            } else {
                "warn"
            }
        }

        fn detail(&self) -> String {
            if let Some(error) = &self.error {
                return error.clone();
            }
            self.last_ok_unix_ms
                .map(|last| format!("updated {}", format_age(now_unix_ms().saturating_sub(last))))
                .unwrap_or_else(|| "opening telemetry feed".to_owned())
        }
    }

    #[component]
    fn App() -> impl IntoView {
        let (edge_api, set_edge_api) = signal(endpoint_from_query("edge", DEFAULT_EDGE_API));
        let (contrib_api, set_contrib_api) =
            signal(endpoint_from_query("contrib", DEFAULT_CONTRIB_API));
        let (edge, set_edge) = signal(None::<MeshStatus>);
        let (contrib, set_contrib) = signal(None::<ContribStatus>);
        let (edge_feed, set_edge_feed) = signal(FeedState::default());
        let (contrib_feed, set_contrib_feed) = signal(FeedState::default());

        let refresh = move || {
            let edge_url = edge_api.get();
            let contrib_url = contrib_api.get();
            spawn_local(async move {
                match fetch_json::<MeshStatus>(&edge_url).await {
                    Ok(snapshot) => {
                        set_edge.set(Some(snapshot));
                        set_edge_feed.set(FeedState::ok());
                    }
                    Err(error) => {
                        set_edge_feed.update(|state| *state = FeedState::error(error, state));
                    }
                }
                match fetch_json::<ContribStatus>(&contrib_url).await {
                    Ok(snapshot) => {
                        set_contrib.set(Some(snapshot));
                        set_contrib_feed.set(FeedState::ok());
                    }
                    Err(error) => {
                        set_contrib_feed.update(|state| *state = FeedState::error(error, state));
                    }
                }
            });
        };

        refresh();
        Interval::new(POLL_INTERVAL_MS, refresh).forget();

        view! {
            <div class="app-shell">
                <header class="topbar">
                    <a class="brand-lockup" href="#overview" aria-label="Needletail operations dashboard overview">
                        <div class="mark" aria-hidden="true"><span></span><span></span><span></span></div>
                        <div>
                            <p class="eyebrow">"NEEDLETAIL"</p>
                            <h1>"Operations"</h1>
                        </div>
                    </a>
                    <div class="feed-strip">
                        <FeedChip label="Contributor" state=contrib_feed />
                        <FeedChip label="Playback edge" state=edge_feed />
                    </div>
                    <details class="feed-settings">
                        <summary>"Feeds"</summary>
                        <div class="feed-form">
                            <label>
                                <span>"Playback-edge snapshot"</span>
                                <input
                                    prop:value=move || edge_api.get()
                                    on:input=move |event| set_edge_api.set(event_target_value(&event))
                                />
                            </label>
                            <label>
                                <span>"Contributor snapshot"</span>
                                <input
                                    prop:value=move || contrib_api.get()
                                    on:input=move |event| set_contrib_api.set(event_target_value(&event))
                                />
                            </label>
                            <button on:click=move |_| refresh()>"Apply & refresh"</button>
                        </div>
                    </details>
                </header>

                <nav class="section-nav" aria-label="Operations dashboard sections">
                    <a href="#overview">"Overview"</a>
                    <a href="#streams">"Live streams"</a>
                    <a href="#contributor">"Contributor ingest"</a>
                    <a href="#nodes">"Nodes & edges"</a>
                    <a href="#routes">"Routes"</a>
                    <a href="#performance">"Performance"</a>
                    <a href="#alerts">"Alerts & activity"</a>
                </nav>

                <main>
                    <section id="overview" class="section-block anchor-section">
                        <SectionHeading
                            kicker="SYSTEM STATUS"
                            title="Ingest, relay, and playback status"
                            detail="Current service state, route assignment, object deadlines, and RaptorQ symbol forwarding."
                        />
                        <div class="hero-grid">
                            <ServiceHealth contrib edge />
                            <RouteAssignmentSummary contrib edge />
                            <DeadlineHealth contrib edge />
                        </div>
                        <div class="lane-flow-wrap">
                            <p class="lane-legend">"MEDIA OBJECT FLOW · PRIMARY SOURCE SYMBOLS · SECONDARY REPAIR SYMBOLS"</p>
                            <div class="lane-flow">
                                <IngestLane contrib />
                                <div class="parallel-lanes">
                                    <SourceLane contrib />
                                    <RepairLane contrib />
                                </div>
                                <EdgeLane edge />
                            </div>
                        </div>
                    </section>

                    <section id="streams" class="section-block anchor-section">
                        <SectionHeading
                            kicker="STREAM PUBLICATION"
                            title="Active streams and publication watermarks"
                            detail="Contributor and playback-edge object counters, contiguous watermarks, known gaps, and lag."
                        />
                        <div class="split-grid publication-split">
                            <PublicationPanel contrib edge />
                            <StreamSummary contrib edge />
                        </div>
                        <ContributorStreams contrib />
                        <EdgeStreams edge />
                    </section>

                    <section id="contributor" class="section-block anchor-section">
                        <SectionHeading
                            kicker="CONTRIBUTOR INGEST"
                            title="Ingest listeners and sessions"
                            detail="RIST, SRT, and RTMP listener state, session counters, codec detection, fMP4 packaging, and relay output."
                        />
                        <ContributorSummary contrib />
                        <ListenerTable contrib />
                        <IngestSessionTable contrib />
                    </section>

                    <section id="nodes" class="section-block anchor-section">
                        <SectionHeading
                            kicker="NODES & PLAYBACK EDGES"
                            title="Playback nodes and edge services"
                            detail="Service state, storage use, stream count, readers, response outcomes, and telemetry age."
                        />
                        <FleetSummary edge />
                        <NodeTable edge />
                        <EdgeServiceTable edge />
                    </section>

                    <section id="routes" class="section-block anchor-section">
                        <SectionHeading
                            kicker="ROUTES"
                            title="Relay topology and assigned routes"
                            detail="DAG and low-latency route assignments, primary source, secondary repair, trust, stretch, and generation."
                        />
                        <RouteAssignmentPanel contrib edge />
                        <RelayFabricTable edge />
                        <RouteTable contrib edge />
                    </section>

                    <section id="performance" class="section-block anchor-section">
                        <SectionHeading
                            kicker="REALTIME PERFORMANCE"
                            title="Latency, deadlines, and RaptorQ recovery"
                            detail="Contributor stage histograms, relay availability, LL-HLS response latency, RaptorQ outcomes, and clock confidence."
                        />
                        <RaptorSummary contrib edge />
                        <LatencyPanel contrib edge />
                    </section>

                    <section id="alerts" class="section-block anchor-section">
                        <SectionHeading
                            kicker="ALERTS & ACTIVITY"
                            title="Alerts and service events"
                            detail="Current alerts and recent events reported by contributor and playback-edge services."
                        />
                        <AlertActivity contrib edge />
                    </section>

                    <AwaitedTelemetry contrib edge />
                </main>
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
                let alerts = operational_alerts(contrib.as_ref(), edge.as_ref()).len();
                format!(
                    "{alerts} active alert{}",
                    if alerts == 1 { "" } else { "s" }
                )
            }
        };
        let tone = move || {
            let contrib = contrib.get();
            let edge = edge.get();
            if contrib.is_none() || edge.is_none() {
                "warn"
            } else if operational_alerts(contrib.as_ref(), edge.as_ref()).is_empty() {
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
                    <b>{move || edge.get().map(|s| format!("{} current / {} stale", s.telemetry.fresh_remote_count, s.telemetry.stale_remote_count)).unwrap_or_else(|| "—".to_owned())}</b>
                </div>
            </article>
        }
    }

    #[component]
    fn RouteAssignmentSummary(
        contrib: ReadSignal<Option<ContribStatus>>,
        edge: ReadSignal<Option<MeshStatus>>,
    ) -> impl IntoView {
        view! {
            <article class="hero-card delivery">
                <p class="card-label">"Relay topology"</p>
                <div class="hero-value">
                    <strong>{move || {
                        let delivery = effective_delivery(contrib.get().as_ref(), edge.get().as_ref());
                        delivery.fabric_label().unwrap_or("Topology not reported")
                    }}</strong>
                </div>
                <div class="detail-row">
                    <span>"Delivery class"</span>
                    <b>{move || effective_delivery(contrib.get().as_ref(), edge.get().as_ref()).delivery_class.unwrap_or_else(|| "pending".to_owned())}</b>
                </div>
                <div class="detail-row">
                    <span>"Generation"</span>
                    <b>{move || optional_u64(effective_delivery(contrib.get().as_ref(), edge.get().as_ref()).generation)}</b>
                </div>
                <div class="detail-row">
                    <span>"Route state"</span>
                    <b>{move || {
                        let delivery = effective_delivery(contrib.get().as_ref(), edge.get().as_ref());
                        delivery
                            .route_state
                            .clone()
                            .unwrap_or_else(|| delivery.readiness_label().to_owned())
                    }}</b>
                </div>
                <div class="detail-row">
                    <span>"Path stretch"</span>
                    <b>{move || effective_delivery(contrib.get().as_ref(), edge.get().as_ref()).path_stretch.map(|value| format!("{value:.2}×")).unwrap_or_else(|| "pending".to_owned())}</b>
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
                    let trust = trust_label(s.mesh.relay_trust.as_deref(), Some(&carrier));
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
                <p>{move || contrib.get().map(|s| if s.mesh.relay_secondary_configured { "Warm-secondary repair lane" } else { "Primary repair fallback" }).unwrap_or("Repair lane")}</p>
                <strong>{move || contrib.get().and_then(|s| s.mesh.relay_secondary_target).or_else(|| contrib.get().and_then(|s| s.mesh.relay_primary_target)).unwrap_or_else(|| "Warm path pending".to_owned())}</strong>
                <span>{move || contrib.get().map(|s| if s.mesh.relay_secondary_configured { "repair path configured; independence telemetry pending" } else { "repair shares the primary carrier" }).unwrap_or("route telemetry pending")}</span>
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
                    <Watermark title="Contributor" publication=move || contrib.get().as_ref().map(publication_from_contrib).unwrap_or_default() />
                    <Watermark title="Playback edge" publication=move || edge.get().as_ref().map(publication_from_edge).unwrap_or_default() />
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
                <div><span>"Source epoch"</span><strong>{move || optional_u64(publication.get_value()().canonical_epoch)}</strong></div>
                <div><span>"Epoch activation"</span><strong>{move || format_optional_duration(publication.get_value()().canonical_epoch_activation_delay_us)}</strong></div>
                <div><span>"Contiguous"</span><strong>{move || optional_u64(publication.get_value()().contiguous_object)}</strong></div>
                <div><span>"Head"</span><strong>{move || optional_u64(publication.get_value()().head_object)}</strong></div>
                <div><span>"Known gaps"</span><strong>{move || optional_u64(publication.get_value()().gap_count)}</strong></div>
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
                    <SmallMetric label="Latest fMP4" value=move || contrib.get().and_then(|s| publication_from_contrib(&s).head_object).map(|v| v.to_string()).unwrap_or_else(|| "pending".to_owned()) />
                    <SmallMetric label="Known edge gaps" value=move || edge.get().and_then(|s| publication_from_edge(&s).gap_count).map(|v| v.to_string()).unwrap_or_else(|| "pending".to_owned()) />
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
                                    <td>{optional_u64(stream.canonical_epoch)}</td>
                                    <td class="mono-cell">{format_optional_duration(stream.canonical_epoch_activation_delay_us)}</td>
                                    <td>{optional_u64(stream.head_object)}</td>
                                    <td>{optional_u64(stream.contiguous_object)}</td>
                                    <td>{stream.mesh_lag_parts.map(|lag| format!("{lag} parts")).unwrap_or_else(|| "pending".to_owned())}</td>
                                    <td>{optional_u64(stream.gap_count)}</td>
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
                <SummaryCard label="Visible nodes" value=move || edge.get().map(|s| s.aggregate.node_count.max(s.nodes.len()).to_string()).unwrap_or_else(|| "—".to_owned()) detail=move || edge.get().map(|s| format!("{} current · {} stale snapshots", s.telemetry.fresh_remote_count.saturating_add(1), s.telemetry.stale_remote_count)).unwrap_or_else(|| "telemetry opening".to_owned()) />
                <SummaryCard label="Active streams" value=move || edge.get().map(|s| s.aggregate.active_streams.to_string()).unwrap_or_else(|| "—".to_owned()) detail=move || edge.get().map(|s| format!("{} contributor-origin streams", s.aggregate.contributor_streams)).unwrap_or_else(|| "telemetry opening".to_owned()) />
                <SummaryCard label="Active readers" value=move || edge.get().map(|s| s.edge_services.iter().map(|service| service.active_readers).sum::<u64>().to_string()).unwrap_or_else(|| "—".to_owned()) detail=move || edge.get().map(|s| format!("{} edge responses", s.edge_services.iter().map(|service| service.responses_total).sum::<u64>())).unwrap_or_else(|| "telemetry opening".to_owned()) />
                <SummaryCard label="Control dispatch" value=move || edge.get().map(|s| if s.orchestration.control_dispatch_ready { "enabled" } else { "disabled" }.to_owned()).unwrap_or_else(|| "—".to_owned()) detail=move || edge.get().map(|s| format!("{} stale node snapshots", s.telemetry.stale_remote_count)).unwrap_or_else(|| "telemetry opening".to_owned()) />
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
                        <thead><tr><th>"Node"</th><th>"Region"</th><th>"State"</th><th>"Active streams"</th><th>"Storage"</th><th>"Egress capacity"</th></tr></thead>
                        <tbody>
                            <For
                                each=move || edge.get().map(|status| bounded_nodes(&status)).unwrap_or_default()
                                key=|node| node.node_id.clone()
                                let(node)
                            >
                                <NodeRow node />
                            </For>
                        </tbody>
                    </table>
                </div>
                {move || edge.get().is_some_and(|status| status.nodes.is_empty()).then(|| view! { <p class="awaiting">"Waiting for the node inventory snapshot."</p> })}
            </section>
        }
    }

    #[component]
    fn NodeRow(node: EdgeNode) -> impl IntoView {
        let state = if node.draining {
            "draining"
        } else {
            "accepting traffic"
        };
        let node_id = nonempty_owned(node.node_id.clone(), "node");
        let location = format!(
            "{} · {}",
            nonempty_owned(node.region.clone(), "region pending"),
            nonempty_owned(node.continent.clone(), "continent pending")
        );
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
                <td>{location}</td>
                <td><StatePill state=state.to_owned() /></td>
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
                        <strong>{move || effective_delivery(contrib.get().as_ref(), edge.get().as_ref()).fabric_label().unwrap_or("Awaiting assignment")}</strong>
                        <span
                            class=move || format!("state-pill {}", tone_for_state(effective_delivery(contrib.get().as_ref(), edge.get().as_ref()).readiness_label()))
                        >
                            {move || effective_delivery(contrib.get().as_ref(), edge.get().as_ref()).readiness_label()}
                        </span>
                    </div>
                    <div class="key-value-grid">
                        <span>"Delivery class"</span><b>{move || effective_delivery(contrib.get().as_ref(), edge.get().as_ref()).delivery_class.unwrap_or_else(|| "pending".to_owned())}</b>
                        <span>"Generation"</span><b>{move || optional_u64(effective_delivery(contrib.get().as_ref(), edge.get().as_ref()).generation)}</b>
                        <span>"Route state"</span><b>{move || effective_delivery(contrib.get().as_ref(), edge.get().as_ref()).route_state.unwrap_or_else(|| "pending".to_owned())}</b>
                        <span>"Path stretch"</span><b>{move || effective_delivery(contrib.get().as_ref(), edge.get().as_ref()).path_stretch.map(|value| format!("{value:.3}×")).unwrap_or_else(|| "pending".to_owned())}</b>
                        <span>"RaptorQ path input"</span><b>{move || contrib.get().map(|status| nonempty_owned(status.mesh.relay_path_observation_source, "pending")).unwrap_or_else(|| "pending".to_owned())}</b>
                        <span>"Path observation age"</span><b>{move || contrib.get().and_then(|status| status.mesh.relay_path_observed_at_unix_ms).map(|observed| format_age(now_unix_ms().saturating_sub(observed))).unwrap_or_else(|| "pending".to_owned())}</b>
                        <span>"Observed queue delay"</span><b>{move || contrib.get().filter(|status| status.mesh.relay_path_queue_delay_ms > 0.0).map(|status| format!("{:.2} ms", status.mesh.relay_path_queue_delay_ms)).unwrap_or_else(|| "pending".to_owned())}</b>
                    </div>
                </article>
                <RouteLaneCard role="Primary source" lane=move || effective_delivery(contrib.get().as_ref(), edge.get().as_ref()).primary />
                <RouteLaneCard role="Warm secondary" lane=move || effective_delivery(contrib.get().as_ref(), edge.get().as_ref()).secondary />
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
    fn RouteTable(
        contrib: ReadSignal<Option<ContribStatus>>,
        edge: ReadSignal<Option<MeshStatus>>,
    ) -> impl IntoView {
        view! {
            <section class="data-panel table-panel">
                <PanelTitle title="Active route inventory" detail="Bounded stream/cohort delivery assignments" />
                <div class="table-shell">
                    <table>
                        <thead><tr><th>"Stream / cohort"</th><th>"Fabric"</th><th>"Class"</th><th>"Generation"</th><th>"Primary"</th><th>"Warm secondary"</th><th>"Stretch"</th><th>"Readiness"</th></tr></thead>
                        <tbody>
                            <For
                                each=move || route_rows(contrib.get().as_ref(), edge.get().as_ref())
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
                                each=move || edge.get().map(|status| status.relay_nodes).unwrap_or_default()
                                key=|node| node.node_id.clone()
                                let(node)
                            >
                                <RelayFabricRow node />
                            </For>
                        </tbody>
                    </table>
                </div>
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
                    <Metric label="Epoch activation max" value=move || edge.get().and_then(|s| publication_from_edge(&s).canonical_epoch_activation_delay_us).map(format_duration_us).unwrap_or_else(|| "pending".to_owned()) detail="contributor incarnation → first object" />
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
                each=move || edge.get().map(|status| status.relay_nodes).unwrap_or_default()
                key=|node| format!("relay-processing:{}:{}", node.node_id, node.region)
                let(node)
            >
                <RelayProcessingRow node />
            </For>
            {move || edge.get().is_some_and(|status| status.relay_nodes.iter().all(|node| node.relay_session.processing_duration_count == 0)).then(|| view! {
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
                each=move || edge.get().map(|status| status.relay_nodes).unwrap_or_default()
                key=|node| format!("relay-availability:{}:{}", node.node_id, node.region)
                let(node)
            >
                <RelayAvailabilityRow node />
            </For>
            {move || edge.get().is_some_and(|status| status.relay_nodes.iter().all(|node| node.relay_session.publication_to_available_count == 0)).then(|| view! {
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
            <article class=format!("event-row {} {}", tone_for_state(&event.level), source_class)>
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
        let tone = tone_for_state(&state);
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
                        let delivery = effective_delivery(contrib.get().as_ref(), edge.get().as_ref());
                        (delivery.delivery_class.is_none() || delivery.generation.is_none() || delivery.fabric_label().is_none())
                            .then(|| view! { <li>"Delivery class, fabric, generation, and installed route state"</li> })
                    }}
                    {move || {
                        let delivery = effective_delivery(contrib.get().as_ref(), edge.get().as_ref());
                        (delivery.primary.as_ref().and_then(|lane| lane.node_id.as_ref()).is_none()
                            || delivery.secondary.as_ref().and_then(|lane| lane.node_id.as_ref()).is_none()
                            || delivery.path_stretch.is_none())
                            .then(|| view! { <li>"Parent identities, failure-domain diversity, RTT, jitter, loss, and path stretch"</li> })
                    }}
                    {move || contrib.get().is_some_and(|s| s.runtime.relay_session.deadline_hits.is_none() || s.runtime.relay_session.deadline_misses.is_none()).then(|| view! { <li>"Per-object deadline hit/miss and sender expiry totals"</li> })}
                    {move || {
                        let contributor_publication = contrib.get().as_ref().map(publication_from_contrib).unwrap_or_default();
                        let edge_publication = edge.get().as_ref().map(publication_from_edge).unwrap_or_default();
                        (contributor_publication.contiguous_object.is_none() || contributor_publication.gap_count.is_none() || edge_publication.contiguous_object.is_none() || edge_publication.gap_count.is_none())
                            .then(|| view! { <li>"Contiguous publication watermarks and known-gap totals"</li> })
                    }}
                    {move || contrib.get().is_some_and(|s| s.runtime.protocols.iter().all(|protocol| protocol.last_seen_age_ms.is_none())).then(|| view! { <li>"Per-session RTT, jitter, loss, reconnect, and end-reason telemetry"</li> })}
                </ul>
            </section>
        }
    }

    fn route_rows(
        contrib: Option<&ContribStatus>,
        edge: Option<&MeshStatus>,
    ) -> Vec<DeliverySnapshot> {
        if let Some(status) = edge.filter(|status| !status.routes.is_empty()) {
            return status.routes.iter().take(12).cloned().collect();
        }
        let delivery = effective_delivery(contrib, edge);
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
        [
            listener.backend.as_deref(),
            listener.profile.as_deref(),
            listener.flow_id.as_deref(),
        ]
        .into_iter()
        .flatten()
        .collect::<Vec<_>>()
        .join(" · ")
        .pipe_nonempty("standard listener")
    }

    trait NonemptyString {
        fn pipe_nonempty(self, fallback: &str) -> String;
    }

    impl NonemptyString for String {
        fn pipe_nonempty(self, fallback: &str) -> String {
            if self.is_empty() {
                fallback.to_owned()
            } else {
                self
            }
        }
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
        [video, audio_codec.clone()]
            .into_iter()
            .flatten()
            .collect::<Vec<_>>()
            .join(" + ")
            .pipe_nonempty("codec pending")
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

    async fn fetch_json<T: DeserializeOwned>(url: &str) -> Result<T, String> {
        let response = Request::get(url)
            .header("accept", "application/json")
            .send()
            .await
            .map_err(|error| format!("feed request: {error}"))?;
        if !response.ok() {
            return Err(format!("feed returned HTTP {}", response.status()));
        }
        response
            .json::<T>()
            .await
            .map_err(|error| format!("snapshot decode: {error}"))
    }

    fn endpoint_from_query(name: &str, fallback: &str) -> String {
        web_sys::window()
            .and_then(|window| window.location().search().ok())
            .and_then(|search| web_sys::UrlSearchParams::new_with_str(&search).ok())
            .and_then(|params| params.get(name))
            .filter(|value| !value.trim().is_empty())
            .unwrap_or_else(|| fallback.to_owned())
    }

    fn trust_label<'a>(trust: Option<&'a str>, carrier: Option<&str>) -> &'a str {
        trust.unwrap_or(match carrier {
            Some("private-udp") => "controlled network",
            _ => "trust profile pending",
        })
    }

    fn tone_for_state(state: &str) -> &'static str {
        match state.to_ascii_lowercase().as_str() {
            "healthy"
            | "active"
            | "ready"
            | "current"
            | "publishing"
            | "listening"
            | "compiled"
            | "installed"
            | "accepting traffic"
            | "serving"
            | "sessions established" => "healthy",
            "attention" | "degraded" | "stale" | "stalled" | "error" | "lagging" | "failed" => {
                "error"
            }
            _ => "warn",
        }
    }

    fn optional_u64(value: Option<u64>) -> String {
        value
            .map(|value| value.to_string())
            .unwrap_or_else(|| "pending".to_owned())
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
        code.trim_start_matches("mesh_")
            .replace('_', " ")
            .pipe_nonempty("service event")
    }

    fn nonempty_owned(value: String, fallback: &str) -> String {
        value.pipe_nonempty(fallback)
    }

    fn now_unix_ms() -> u64 {
        js_sys::Date::now().max(0.0) as u64
    }

    const _: usize = MAX_EVENT_ROWS;
}

#[cfg(target_arch = "wasm32")]
fn main() {
    app::run();
}

#[cfg(not(target_arch = "wasm32"))]
fn main() {
    println!("Needletail operations dashboard builds for wasm32-unknown-unknown");
}
