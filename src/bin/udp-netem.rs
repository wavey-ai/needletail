use anyhow::{bail, Context, Result};
use clap::Parser;
use serde::Serialize;
use std::cmp::Ordering;
use std::collections::BinaryHeap;
use std::net::SocketAddr;
use std::time::{Duration, SystemTime, UNIX_EPOCH};
use tokio::net::UdpSocket;
use tokio::time::{interval, sleep_until, Instant, MissedTickBehavior};

const MAX_DATAGRAM_BYTES: usize = 65_535;

#[derive(Debug, Parser)]
#[command(
    name = "udp-netem",
    about = "User-space UDP delay/loss emulator for Needletail qualification"
)]
struct Args {
    #[arg(long)]
    bind: SocketAddr,

    #[arg(long, requires = "endpoint_b", conflicts_with = "target")]
    endpoint_a: Option<SocketAddr>,

    #[arg(long, requires = "endpoint_a", conflicts_with = "target")]
    endpoint_b: Option<SocketAddr>,

    #[arg(long, conflicts_with_all = ["endpoint_a", "endpoint_b"])]
    target: Option<SocketAddr>,

    #[arg(long, default_value_t = 0)]
    delay_ms: u64,

    #[arg(long, default_value_t = 0)]
    jitter_ms: u64,

    #[arg(long, default_value_t = 0.0)]
    loss_pct: f64,

    #[arg(long, default_value_t = 65_536)]
    queue_limit: usize,

    #[arg(long, default_value_t = 1_000)]
    stats_interval_ms: u64,

    #[arg(long, default_value_t = 0x57a7_e5ed_d15c_a11e)]
    seed: u64,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Route {
    OneWay { target: SocketAddr },
    Pair { a: SocketAddr, b: SocketAddr },
}

impl Route {
    fn from_args(args: &Args) -> Result<Self> {
        match (args.target, args.endpoint_a, args.endpoint_b) {
            (Some(target), None, None) => Ok(Self::OneWay { target }),
            (None, Some(a), Some(b)) if a != b => Ok(Self::Pair { a, b }),
            (None, Some(_), Some(_)) => bail!("endpoint-a and endpoint-b must be different"),
            _ => bail!("pass either --target or both --endpoint-a and --endpoint-b"),
        }
    }

    fn target_for(self, source: SocketAddr) -> Option<SocketAddr> {
        match self {
            Self::OneWay { target } if source != target => Some(target),
            Self::OneWay { .. } => None,
            Self::Pair { a, b } if source == a => Some(b),
            Self::Pair { a, b } if source == b => Some(a),
            Self::Pair { .. } => None,
        }
    }

    fn description(self) -> String {
        match self {
            Self::OneWay { target } => format!("one-way->{target}"),
            Self::Pair { a, b } => format!("pair:{a}<->{b}"),
        }
    }
}

#[derive(Debug)]
struct SplitMix64 {
    state: u64,
}

impl SplitMix64 {
    fn new(seed: u64) -> Self {
        Self { state: seed }
    }

    fn next(&mut self) -> u64 {
        self.state = self.state.wrapping_add(0x9e37_79b9_7f4a_7c15);
        let mut value = self.state;
        value = (value ^ (value >> 30)).wrapping_mul(0xbf58_476d_1ce4_e5b9);
        value = (value ^ (value >> 27)).wrapping_mul(0x94d0_49bb_1331_11eb);
        value ^ (value >> 31)
    }

    fn drops(&mut self, loss_pct: f64) -> bool {
        if loss_pct <= 0.0 {
            return false;
        }
        if loss_pct >= 100.0 {
            return true;
        }
        let threshold = (loss_pct * 10_000.0).round() as u64;
        self.next() % 1_000_000 < threshold
    }

    fn delay(&mut self, delay_ms: u64, jitter_ms: u64) -> Duration {
        if jitter_ms == 0 {
            return Duration::from_millis(delay_ms);
        }
        let width = jitter_ms.saturating_mul(2).saturating_add(1);
        let offset = (self.next() % width) as i128 - jitter_ms as i128;
        let delayed = (delay_ms as i128 + offset).max(0) as u64;
        Duration::from_millis(delayed)
    }
}

#[derive(Debug, Default, Clone, Copy)]
struct Counters {
    received: u64,
    received_bytes: u64,
    scheduled: u64,
    sent: u64,
    sent_bytes: u64,
    loss_drops: u64,
    overflow_drops: u64,
    unknown_source_drops: u64,
    send_errors: u64,
}

#[derive(Debug)]
struct ScheduledDatagram {
    due: Instant,
    order: u64,
    payload: Vec<u8>,
    target: SocketAddr,
}

impl PartialEq for ScheduledDatagram {
    fn eq(&self, other: &Self) -> bool {
        self.due == other.due && self.order == other.order
    }
}

impl Eq for ScheduledDatagram {}

impl PartialOrd for ScheduledDatagram {
    fn partial_cmp(&self, other: &Self) -> Option<Ordering> {
        Some(self.cmp(other))
    }
}

impl Ord for ScheduledDatagram {
    fn cmp(&self, other: &Self) -> Ordering {
        other
            .due
            .cmp(&self.due)
            .then_with(|| other.order.cmp(&self.order))
    }
}

fn record_send(counters: &mut Counters, result: std::io::Result<usize>) {
    match result {
        Ok(bytes) => {
            counters.sent = counters.sent.saturating_add(1);
            counters.sent_bytes = counters.sent_bytes.saturating_add(bytes as u64);
        }
        Err(error) => {
            counters.send_errors = counters.send_errors.saturating_add(1);
            eprintln!("udp-netem send failed: {error}");
        }
    }
}

#[derive(Debug, Serialize)]
struct Stats<'a> {
    kind: &'static str,
    unix_ms: u64,
    uptime_ms: u64,
    bind: SocketAddr,
    route: &'a str,
    delay_ms: u64,
    jitter_ms: u64,
    loss_pct: f64,
    in_flight: usize,
    received: u64,
    received_bytes: u64,
    scheduled: u64,
    sent: u64,
    sent_bytes: u64,
    loss_drops: u64,
    overflow_drops: u64,
    unknown_source_drops: u64,
    send_errors: u64,
}

fn emit_stats(
    args: &Args,
    route_description: &str,
    started: Instant,
    counters: Counters,
    in_flight: usize,
) {
    let unix_ms = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_millis()
        .min(u64::MAX as u128) as u64;
    let stats = Stats {
        kind: "udp_netem_stats",
        unix_ms,
        uptime_ms: started.elapsed().as_millis().min(u64::MAX as u128) as u64,
        bind: args.bind,
        route: route_description,
        delay_ms: args.delay_ms,
        jitter_ms: args.jitter_ms,
        loss_pct: args.loss_pct,
        in_flight,
        received: counters.received,
        received_bytes: counters.received_bytes,
        scheduled: counters.scheduled,
        sent: counters.sent,
        sent_bytes: counters.sent_bytes,
        loss_drops: counters.loss_drops,
        overflow_drops: counters.overflow_drops,
        unknown_source_drops: counters.unknown_source_drops,
        send_errors: counters.send_errors,
    };
    match serde_json::to_string(&stats) {
        Ok(line) => println!("{line}"),
        Err(error) => eprintln!("failed to encode udp-netem stats: {error}"),
    }
}

#[tokio::main(flavor = "current_thread")]
async fn main() -> Result<()> {
    let args = Args::parse();
    if !args.loss_pct.is_finite() || !(0.0..=100.0).contains(&args.loss_pct) {
        bail!("loss-pct must be a finite percentage between 0 and 100");
    }
    if args.queue_limit == 0 {
        bail!("queue-limit must be greater than zero");
    }
    if args.stats_interval_ms == 0 {
        bail!("stats-interval-ms must be greater than zero");
    }

    let route = Route::from_args(&args)?;
    let route_description = route.description();
    let socket = UdpSocket::bind(args.bind)
        .await
        .with_context(|| format!("failed to bind UDP impairment proxy on {}", args.bind))?;
    println!(
        "udp-netem ready bind={} route={} delay_ms={} jitter_ms={} loss_pct={} queue_limit={}",
        args.bind,
        route_description,
        args.delay_ms,
        args.jitter_ms,
        args.loss_pct,
        args.queue_limit
    );

    let started = Instant::now();
    let mut rng = SplitMix64::new(args.seed);
    let mut counters = Counters::default();
    let mut queue = BinaryHeap::<ScheduledDatagram>::new();
    let mut next_order = 0_u64;
    let mut buffer = vec![0_u8; MAX_DATAGRAM_BYTES];
    let mut stats_tick = interval(Duration::from_millis(args.stats_interval_ms));
    stats_tick.set_missed_tick_behavior(MissedTickBehavior::Skip);
    stats_tick.tick().await;

    loop {
        let next_due = queue
            .peek()
            .map(|datagram| datagram.due)
            .unwrap_or_else(|| Instant::now() + Duration::from_secs(86_400));
        tokio::select! {
            received = socket.recv_from(&mut buffer) => {
                let (len, source) = received.context("udp-netem receive failed")?;
                counters.received = counters.received.saturating_add(1);
                counters.received_bytes = counters.received_bytes.saturating_add(len as u64);

                let Some(target) = route.target_for(source) else {
                    counters.unknown_source_drops = counters.unknown_source_drops.saturating_add(1);
                    continue;
                };
                if rng.drops(args.loss_pct) {
                    counters.loss_drops = counters.loss_drops.saturating_add(1);
                    continue;
                }

                let delay = rng.delay(args.delay_ms, args.jitter_ms);
                counters.scheduled = counters.scheduled.saturating_add(1);

                if delay.is_zero() {
                    record_send(&mut counters, socket.send_to(&buffer[..len], target).await);
                } else if queue.len() >= args.queue_limit {
                    counters.scheduled = counters.scheduled.saturating_sub(1);
                    counters.overflow_drops = counters.overflow_drops.saturating_add(1);
                } else {
                    queue.push(ScheduledDatagram {
                        due: Instant::now() + delay,
                        order: next_order,
                        payload: buffer[..len].to_vec(),
                        target,
                    });
                    next_order = next_order.wrapping_add(1);
                }
            }
            _ = sleep_until(next_due), if !queue.is_empty() => {
                let now = Instant::now();
                for _ in 0..256 {
                    let Some(datagram) = queue.peek() else {
                        break;
                    };
                    if datagram.due > now {
                        break;
                    }
                    let datagram = queue.pop().expect("scheduled datagram disappeared");
                    record_send(
                        &mut counters,
                        socket.send_to(&datagram.payload, datagram.target).await,
                    );
                }
            }
            _ = stats_tick.tick() => {
                emit_stats(&args, &route_description, started, counters, queue.len());
            }
            signal = tokio::signal::ctrl_c() => {
                signal.context("failed to wait for ctrl-c")?;
                break;
            }
        }
    }

    while let Some(datagram) = queue.pop() {
        if datagram.due > Instant::now() {
            sleep_until(datagram.due).await;
        }
        record_send(
            &mut counters,
            socket.send_to(&datagram.payload, datagram.target).await,
        );
    }
    emit_stats(&args, &route_description, started, counters, 0);
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn pair_route_only_crosses_between_declared_endpoints() {
        let a = "127.0.0.1:10001".parse().unwrap();
        let b = "127.0.0.1:10002".parse().unwrap();
        let route = Route::Pair { a, b };
        assert_eq!(route.target_for(a), Some(b));
        assert_eq!(route.target_for(b), Some(a));
        assert_eq!(route.target_for("127.0.0.1:10003".parse().unwrap()), None);
    }

    #[test]
    fn one_way_route_does_not_loop_target_packets() {
        let source = "127.0.0.1:10001".parse().unwrap();
        let target = "127.0.0.1:10002".parse().unwrap();
        let route = Route::OneWay { target };
        assert_eq!(route.target_for(source), Some(target));
        assert_eq!(route.target_for(target), None);
    }

    #[test]
    fn deterministic_loss_and_jitter_stay_in_range() {
        let mut first = SplitMix64::new(7);
        let mut second = SplitMix64::new(7);
        let first_drops = (0..1_000).map(|_| first.drops(5.0)).collect::<Vec<_>>();
        let second_drops = (0..1_000).map(|_| second.drops(5.0)).collect::<Vec<_>>();
        assert_eq!(first_drops, second_drops);
        assert!(first_drops.iter().any(|dropped| *dropped));
        assert!(first_drops.iter().any(|dropped| !*dropped));

        for _ in 0..1_000 {
            let delay = first.delay(20, 5);
            assert!((15..=25).contains(&delay.as_millis()));
        }
    }

    #[test]
    fn scheduled_datagrams_are_ordered_by_due_time_then_arrival() {
        let now = Instant::now();
        let target = "127.0.0.1:10002".parse().unwrap();
        let mut queue = BinaryHeap::new();
        for (delay_ms, order) in [(20, 0), (10, 1), (10, 2)] {
            queue.push(ScheduledDatagram {
                due: now + Duration::from_millis(delay_ms),
                order,
                payload: Vec::new(),
                target,
            });
        }

        assert_eq!(queue.pop().unwrap().order, 1);
        assert_eq!(queue.pop().unwrap().order, 2);
        assert_eq!(queue.pop().unwrap().order, 0);
    }
}
