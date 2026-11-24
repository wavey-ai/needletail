# Workstream D: the first-session stall hunt, source hardening, long-haul flows, the 2.5 ms Opus lane, burst loss on the mesh side (final specification, 23 Nov 2026)

Paths: `MESH` = /home/jamieb/wavey.ai/av-mesh-features/src (`RL` = MESH/fast_plane/relay.rs, `AM` = MESH/main.rs,
`RI` = MESH/relay_ingress.rs), `CONTRIB` = /home/jamieb/wavey.ai/av-contrib-features/src (`AC` = CONTRIB/bin/av-contrib.rs,
`PR` = CONTRIB/bin/aep1-48k-probe.rs), `PL` = /home/jamieb/wavey.ai/playlists-features/src, `NT` =
/home/jamieb/wavey.ai/needletail-features, `DEP` = NT/scripts/linode-small-mesh-deploy.py, `LAB` =
NT/scripts/linode-small-mesh-lab.py, `EL` = NT/scripts/edge_lab.py, `LB` = NT/scripts/compare/latency-bench.py,
`REF` = /home/jamieb/wavey.ai/needletail-compare (the referee's, read only), `CW` = REF/scripts/compare/change-window.py,
`RUNS` = REF/target/compare/runs, `W7` = RUNS/waudio7, `EV7` = REF/target/compare/evidence-w7.
Heads read: av-mesh b31d641 (baf0449 plus two assignment commits made while this was written: `AM` below line 19,405
and every other file cited are as at baf0449), av-contrib 219c6b5, playlists c01662d, needletail 52c10e1,
needletail-compare 6c8c525. Times are seconds after Unix 1795380000 (22 Nov 20:40:00 UTC; 21:08:45 UTC = 1725).
Marks: [D] a figure of the draft that the review reproduced; [E] the review's figure; [R] computed for this revision
from the referee's files (appendix). Nothing was built, and nothing was run but stdlib Python over the referee's files.
Workstream ids used for seams: A (contributor), B (listener), C (operations); `C-operations.md` is final, A's and
B's are named by their drafts' commit ids (AC8, AC9, AM3) and by function.

## Where this specification departs from the lead's brief, and why

1. **The referee's rule fails on the steady state, for both systems, so no change can be judged by it alone.**
   Applied to 8 s windows with no change, slid by 1 s over the `nt-uc.probe.csv` of the four view hosts in the four
   cleanest wave 7 sessions, the rule fails 144 of 704 windows, 20 % (66 on the silence, 125 on p99, none missing)
   [R]; the review found 143 of 688, 30 % at 15 s and 55 % at 30 s [E]. The same test on LiveKit's lane
   (`lkregion.probe.csv`, same hosts and sessions) fails 147 of 676, 22 % (73 silence, 73 p99, 30 missing) [R].
   The silence criterion (CW:109, `gap_in <= gap_out + spacing`) compares the worst silence of one window with the
   single worst silence of the rest of the probe: for any stationary process with a tail it fails by chance up to
   about `(W+1)/(T-5)` of the time (W the window, T the probe, both in seconds): 16 % for 8 s in a 60 s probe; the
   two systems show 9 % and 11 % on silence alone. The nearest arrangement that works: every change result is
   quoted beside the null of the same probe (C's dry run per scenario, and the sliding null of 2.11 here); the steady state is
   held to the numbers of 3.1; and the referee is asked (7.7) for steady probes of 300 s or more around a change or
   a threshold calibrated on no-change windows. The lead and the referee get the measured null rates before the
   first scored change.
2. **L6, "at least one cause is upstream of the edges".** True of lon-dist-b's own freeze, false as the cause of
   any listener's hold: every hold over 22.5 ms was one edge instance that stopped while the region's other edge
   committed every part on time [D]. What is upstream and does reach every listener is another class, which the
   notes never counted: 14 to 90 episodes a minute of 2 to 21 ms before publish, in every session, the clean ones
   included [E]. L6's remedies are kept; the order changes: the sender's scheduling first.
3. **L6, "locked memory".** `mlockall` cannot be what locks a service: the kernel allows `MCL_CURRENT` under a
   finite `RLIMIT_MEMLOCK` only when the whole virtual size is within it (or with CAP_IPC_LOCK; the unit has
   CAP_SYS_NICE and CAP_NET_ADMIN, DEP:2873), and `MCL_FUTURE` is what the wave 9 review turned off. The design
   locks what it prefaults, ring by ring (`mlock(ptr, len)`, 2.3). The cache cannot be prefaulted or locked: it
   holds one `Bytes` per part (PL/chunk_cache.rs:117-122). The sender, a small process run as root, does use
   `mlockall`.
4. **L6, what the paid lab hunts.** The draft's lab tested one variable (first touch of memory). The files show
   three populations of stall (Evidence, E2), one of which (particular instances that stop for seconds all evening
   with flat memory and bursts of steal) first touch cannot explain. The lab now asks first which instances stop,
   whether they share a physical host (`host_uuid`), whether a check at boot finds them and whether a replacement
   is clean; first touch and transparent huge pages are branches. Its product may be a node quality gate, not a
   warm pass. It waits for workstream A's sender journal and contributor kernel stamps (the draft said it need not).
5. **L7, "a fixed small number per stream and region".** Two flows per long-haul path at most (the compiled one and
   one extra), but the extra carries media only while the link's probes show a second route class (300 us or more
   apart). On a link with none (London to Sydney on 19 Nov) a second flow is a copy on the same route at the same
   instant, and the 20 ms burst copy already covers time. That two classes exist at one instant on London to
   Tokyo is not shown by any file (the 19 Nov record is a summary of a serial probe from the old topology); the
   route survey decides whether flows earn their cost (7.6).
6. **The draft's `--encode-at ahead` is not built.** A live source cannot encode an epoch before its samples
   exist (PR:3433-3434 says so); by this design's own line between product and trick it is a trick.

## 1. Summary

1. **Listener.** No protocol change. With B's two paths the wave 7 hold rows go: per part, the earlier commit of a
   region's two edges gives London publish-to-commit p99 0.49 ms and max 22.5 ms in the first session (lon-edge-1
   alone 567.8 / 901.3) [D]. That is a p99 at the better edge, not proof that nothing else is heard: about 0.1
   overlapping pairs of stops are expected in the first minute over four regions if edges stop independently [E],
   every time if two edges share a host, and the source-side class is hidden by nothing.
2. **What stops.** Whole instances, below our processes: an unrelated shell loop on the node wakes late to the
   release instant, the kernel answers no ping, LiveKit's SFU froze 4.7 s in the same minute. Three populations
   (E2): an onset cluster on nearly every loaded 2-vCPU node in the first minute, particular instances that stop
   all evening, and gaps of 20 to 40 ms. Cause not proven; the lab of section 7 decides between the physical host,
   first touch and huge pages by rules fixed beforehand.
3. **Operator.** Every node gains a dated stall journal (`GET /api/stalls`: one tick thread per CPU, each event
   classed stopped, starved or own work), `needletail-nodecheck` (a check at boot that C's readiness reads),
   `--mlock rings`, `LAB hosts` covering every pair that backs each other up, per-flow long-haul statistics.
4. **Mesh.** Audio first transmissions on a second ECMP-diverse flow per long-haul path while one exists, chosen
   from 64 probed source ports and re-chosen within 3 s; first arrival through the existing dedup; the per-flow
   first-arrival share in the stats.
5. **Source.** The one tier that is not doubled: the sender's pacer runs SCHED_FIFO with a lateness journal; each
   contributor stamps AEP1 arrivals in the kernel and reports source gaps on one clock. Acceptance: no episode
   before publish more than 1 ms over the median, each over 0.5 ms attributed to a side.
6. **Steady state.** A null (no-change) score is part of every proof and of the lab; acceptance in 3.1.
7. **The paid lab.** 20 instances, two billed hours, $2.70 with its two replacements; one repeat at most ($5.40)
   (section 7 as revised after the wave 9 run).
8. **2.5 ms Opus.** The rows are lost to the referee's four-point mouth-to-ear estimate alone; nothing is built
   for them; three requests to the referee (7.7).
9. **Burst loss.** Nothing is known to be lost upstream of an edge; the design adds the join that says which side
   lost a part, three 200 s scenarios and one remedy held until they ask for it.
10. **Who builds.** Three units in D (instruments, tooling, flows) and one in A's code (sender and contributor),
    section 5.

## Evidence the design rests on

**E1. The large holds were single instances that stopped** [D, reproduced]. Commit gaps of an edge in the first Opus
minute (1709 to 1770), each seen alike by two to five probes (UDP, HTTP/2, HTTP/3) on both listener hosts of the
region and by no probe of the region's other edge:

| edge | commit gaps (from -> to, ms) |
| --- | --- |
| lon-edge-1 | 1725.252 -> 1725.931 (679), 1725.938 -> 1726.381 (443), 1752.492 -> 1752.792 (299) |
| lon-edge-2 | 1729.282 -> 1729.514 (232), 1766.953 -> 1767.071 (118) |
| tok-edge-2 | 1714.481 -> 1715.216 (735), 1737.278 -> 1737.536 (258) |
| tok-edge-1 | 1741.153 -> 1741.356 (204) |
| syd-edge-1 | 1743.521 -> 1743.943 (422), 1752.719 -> 1752.903 (184) |
| syd-edge-2 | 1734.037 -> 1734.140 (102) |
| new-edge-2 | 1713.169 -> 1713.281 (112), 1745.328 -> 1745.458 (130), 1760.265 -> 1760.410 (145) |

Later: lon-edge-1 1949.225 -> 1950.085 and 1970.545 -> 1971.362 (the two 5 % loss holds), syd-edge-1 1908.623 ->
1909.243, lon-edge-1 again in PCM 5 % loss. The "episode shared by London and Sydney" is two stops that overlap in
time (lon-edge-1 and syd-edge-1 at 1752.5 to 1752.9); the twins committed those parts on time. First of two edges,
p50 / p99 / max ms: London 0.22 / 0.49 / 22.51, Tokyo 108.80 / 109.36 / 131.42, Sydney 125.15 / 125.57 / 147.33, New
York 30.82 / 31.19 / 53.00; parts over 20 ms late on both edges: one a region (part 42905, late before publish) and
none in the loss, burst and PCM steps. Inside the 1.14 s London hold the edge committed 251 parts in 6 ms at
1725.931 and stopped again; the UDP client got the first 590 ms of parts in order at release and the last 580 ms
only by repair; no audio socket of lon-edge-1 dropped (`EV7/socket-drops.txt`): the loss was below the sockets.
Below our processes too: the referee's 5 s sampler (a shell loop, REF/scripts/compare/compare-run.py:78-88) woke
late to the release instant three times (lon-edge-1 +0.60 s at 1950.061, tok-edge-2 +0.22 s at 1737.534, lon-lk
+4.21 s at 1716.034 while LiveKit's audio stopped for 4.7 s); lon-edge-1 logged a failed ping to lon-dist-b's VLAN
address at 1721.945 (`EV7/lon-edge-1-journal-2105-2112.txt`) just before lon-dist-b's `deadline_drops=721`. Two
corrections to the draft: on lon-nano's HTTP/2 probe the replies to parts 29250 to 29260 did leave the node within
3.2 ms [E]; and the backlog model (first packets kept, a ring plus a queue) is one event on one interface: lon-lk
lost its oldest 2.88 s and kept later units.

**E2. Three populations, not one** [E; the sampler figures re-read, R].
- (i) *An onset cluster*: stops of 0.1 to 0.9 s on nearly every loaded 2-vCPU node in the first minute of first
  load. In the PCM baseline (edge RSS flat) six of seven edges have no gap of 20 ms or more; in the first Opus
  minute all eight have one. This is what first touch may explain. But the same dose (78 to 92 MB of RSS growth on
  every edge) gave 23 ms frozen on new-edge-1 and 1,681 ms on lon-edge-1: a 70-fold spread between instances.
- (ii) *Particular instances that stop for seconds all evening*, with flat memory, low load and bursts of steal.
  5 s sampler wakes late by more than 0.2 s in `waudio7`: lon-dist-b 3 (up to 1.98 s), syd-dist-b 4 (1.31 s at
  2326.8 with RSS 602,156 kB before and after), tok-dist-b 5 (up to 2.03 s); every -a distributor together 1.
  tok-dist-b again in `waudio7-std` (+2.33 s at 2760.8, RSS 680,524 kB before and after, 42 minutes after creation)
  and in `waudio7-unpin` (+1.02 s at 3828.6). Steal ticks in one 5 s interval: tok-dist-b 285 (28 % of two CPUs)
  and 127 in `waudio7-pin`, lon-dist-b 114; no other node over 21. Three idle instances (relay-primary,
  relay-secondary, lk-redis) have no late wake in six sampler sets. First touch cannot explain this population; a
  spinning thread of ours cannot either (tok-dist-b's frozen interval accounts 11 ticks of user and system time).
- (iii) *Short gaps* of 20 to 40 ms on the commit side (new-edge-2 has 31 of 20 ms or more), mixed into E1's table.
- Not every stop is the whole instance: lon-dist-b's 5 s loop woke 1.98 s late at 1754.494 while its 1 s loop kept
  cadence and the node sent 12,005 datagrams a second: one vCPU. And not every stop is a vCPU that is not run:
  lon-lk's interval counts 174 system ticks, tok-dist-b at 3399.3 shows `sy 518` with sends near zero: the guest
  kernel was busy. The instruments must tell these apart (2.1).
- The lab tool can read the physical host (`host_uuid`, LAB:1346-1375) and exits 1 when two distributors of a region
  share one. It was never run for wave 7 (no `host_uuid` in `REF/target/linode-qualification/
  small-mesh-lab-cmpd4.removed.json`), and it does not look at edges (LAB:1370).

**E3. The source side** [E; the contributor split re-computed, R]. `published_unix_us` minus the part's ideal
instant, episodes more than 2 ms above the step's median: first Opus minute 46 (largest 21.2 ms), loss 1 % 8 in
35 s, loss 5 % 34, burst 52, PCM baseline 21, `waudio7-std` 32, `waudio7-pin` (the clean later session) 63. Every
session, every region at once. The contributor's own journal of the PCM stream (`EV7/contrib-hops-4001.ndjson`,
104,857 parts): `sent_first - src_first` p50 0.084, p99 0.174, max 0.567 ms; all 29 episodes over 2 ms are already
there at `src_first`; the worst (part 278106) is a run stamped within microseconds, +7.50, +6.51, +5.52 ... ms.
So for this class the contributor after its receive stamp is excluded. What remains: the sender's pacer
(`wait_until_unix_ns`, PR:2995-3006: `thread::sleep` then a spin, normal scheduling), the VLAN link, or the
contributor's receive reading late: `src_first` is user time (`hop_journal::now_unix_us()` after `recv_from`,
AC:4784-4786). The receive does already run on a thread of its own (`audio-hot-0`, SCHED_FIFO 10, pinned to
`--audio-hot-cpu`; AC:1202-1216, AC:11230-11250), but that thread also runs the AEP1 recovery. The Opus stream's
journal is empty, and Opus had one inline hold (20.35 ms at 21:09:37, `EV7/contrib-journal.txt`; the warning
threshold is 20 ms, AC:148).

**E4. The null** (departure 1). Per region at 8 s [E]: London 23 %, New York 30 %, Tokyo 22 %, Sydney 9 %. Scoring
the kernel receive stamp instead of the client's does not help (23 %); publish to listener kernel alone fails 17 %.
Inside one clean 60 s probe (lon-view, `waudio7-pin`) the p99 of successive 8 s windows runs 2.92 to 6.61 ms and
the longest steady silence is 12.2 ms (New York 34.2, Sydney 25.6).

**E5. Hypotheses.**

| # | hypothesis | verdict |
| --- | --- | --- |
| H1, H2 | source pacing; contributor | excluded for holds over 25 ms; the cause class of E3 (H2 after its receive stamp excluded by E3) |
| H3 | an ingress's send side | no listener-visible case: lon-dist-b's freeze was covered by lon-dist-a |
| H4, H5 | edge commit alone; edge client send alone | excluded: UDP and LL-HLS stop and resume together; commit gap = UDP gap |
| H6, H7, H12 | client ordering; listener host; netem | excluded for the scoreboard's rows (the `-nano` hosts are a class of their own: every lane stops together) |
| H8 | page faults inside the process | excluded as the mechanism (kernel alive would keep the 16 MB socket backlog); a possible trigger of H10 |
| H9 | a SCHED_FIFO class thread holds a CPU | excluded for population (ii) by the accounting; unlikely for (i) (lon-lk has no such thread); the journal decides |
| H10 | the hypervisor does not run the vCPUs while the guest first touches memory | leading for (i); not proven |
| H11 | CPU frequency or scheduling of a fresh instance | excluded for the holds |
| H13 | the instance's physical host | leading for (ii): predicts persistence per instance, stops shared by co-hosted instances, steal, no effect of a warm pass |

## 2. Wire, CLI, API and file formats

### 2.1 Stall journal, av-mesh (new `MESH/stall_journal.rs`; no dependency on the rest of av-mesh)

A ring of `--stall-journal-capacity` events (default 4,096; 0 off) of 48 bytes, allocated, faulted in and locked at
start. Recording is one `try_lock` and a copy; a contended record is counted (`dropped`) and never waited for.

**Tick threads.** `--stall-tick-us` (default 1000; 0 off) starts one thread per CPU the process may run on
(`sched_getaffinity`), named `needletail-tick-<cpu>`, pinned to its CPU (`fast_path::pin_current_thread`),
SCHED_OTHER, timer slack 1 ns. Loop: `clock_nanosleep(CLOCK_MONOTONIC, TIMER_ABSTIME, due)`; `late = now - due`;
`pread` of the thread's own pre-opened `/proc/thread-self/schedstat` and `run_delay` = growth of its second field
(time waiting on a run queue) since the previous tick, so the figure is the delay of this wake only; when `late`
is 2,000 us or more, a `tick` event. The thread never preempts a class thread (SCHED_FIFO runs first). Cost: 1,000
wakes and reads a second per CPU, under 1 % of that CPU.

| kind | recorded by | when |
| --- | --- | --- |
| `tick` | the tick thread of a CPU | it woke 2,000 us or more late |
| `class_pass` | `ClassThread::run`, beside `record_loop` (RL:1238-1244) | a pass took 2,000 us or more |
| `class_gap` | same place | two data-bearing wakes 5,000 us or more apart |
| `rx_late` | `ClassThread::drain`, where `rx_us` is read (RL:1380) | the oldest datagram of a batch waited 2,000 us or more between its kernel stamp and the drain |
| `tee_lag` | `Tee::process` (RL:1742-1758) | an item reached the tee 10,000 us or more after `woken_us` |
| `send_slow` | `send_fast_plan` (the `elapsed` it already takes, RL:878) and `send_lease_plan` (RL:589) | one `sendmmsg` took 1,000 us or more |

Fields: `t_us` (CLOCK_REALTIME when detected), `kind`, `thread` (`tick-0`, `audio`, `video`, `tee-audio`,
`tee-video`), `cpu` (`sched_getcpu`), `dur_us`, `span_us` and `n` (consecutive events of one kind and thread less
than 10 ms apart merge: `dur_us` the longest, `span_us` first start to last end, `n` the count), `run_delay_us`
(tick events), `minflt`, `majflt` (class threads: growth since the thread's once-a-second `getrusage` reading at
RL:1245-1251: a hint, up to a second wide).

**Cause, computed when the journal is read** (`StallJournal::query`, off the hot path):
- a `tick` is `stopped` when `run_delay_us * 10 < dur_us` (the thread was asleep and its timer came late: the CPU
  was not run), else `starved` (it was runnable and kept waiting: overload, or a thread of ours or the kernel held
  the CPU). `scope` is `all` when every other CPU has a `stopped` tick overlapping it by half or more, else `one`.
- any other event is `stopped` when a `stopped` tick of its `cpu` overlaps half or more of `[t_us - dur_us, t_us]`;
  otherwise `own` (`class_pass`, `send_slow`) or `waited` (`class_gap`, `rx_late`, `tee_lag`).
  The class thread cannot carry its CPU's tick lateness itself: the tick thread of a CPU that was stopped under a
  class thread runs only after that pass ends. Hence the join at read time.
- a line of kind `sec` is kept per CPU and second that holds an event or any steal: growth of the CPU's `user`,
  `system`, `irq`, `softirq`, `steal` ticks (`/proc/stat`, read once a second by `tick-0`). A `starved` tick in a
  second that is mostly `system` is a stall inside the guest kernel.
A SIGSTOP or a cgroup freeze of the service reads as `stopped`, `scope: all`: from inside one process it is the
same thing. `needletail-nodecheck watch` beside it (2.2) tells a stopped process from a stopped node.

`GET /api/stalls?since_us=T` (route arm beside `HOPS_PATH`, AM:22463; handler beside `hops_response`, AM:19200):
NDJSON, oldest first, at most the ring:
`{"node":"lon-edge-1","t_us":1795381726393150,"kind":"tick","thread":"tick-1","cpu":1,"dur_us":673004,"span_us":673004,"n":1,"run_delay_us":212,"cause":"stopped","scope":"all"}`.
`/api/mesh/local` (`MeshApiSnapshot`, AM:13053) gains `stall`: `{"tick_threads":2,"tick_pinned":2,"events":0,
"dropped":0,"tick_late_max_us":0,"tick_late_over_2ms":0,"tick_late_over_100ms":0,"stopped_ms":0,"starved_ms":0,
"rx_late_max_us":0}` (`stopped_ms`: summed `dur_us` of `stopped` ticks of 20 ms or more, averaged over CPUs: the
lab's measure). It stays on in the product: an operator can then say "lon-edge-1 was not run for 673 ms at
21:08:45, both CPUs", which no counter says today (`audio_loop_gap_max_us` is a maximum since start with no time).

### 2.2 `needletail-nodecheck` (new `MESH/bin/needletail-nodecheck.rs`; uses `stall_journal.rs` and libc only)

- `watch --out PATH [--tick-us 1000] [--threshold-us 2000]`: the tick threads of 2.1 in a process of its own;
  NDJSON `tick` events in 2.1's format and, every second for every CPU, a `sec` line: the five tick growths,
  `late_max_us`, `late_over_2ms`, `work_us_max` (a fixed 200 us arithmetic loop timed every 100 ms: H11), and on
  CPU 0's line the growth of `thp_fault_alloc`, `thp_fault_fallback`, `compact_stall`, `pgfault`, `allocstall`
  (`/proc/vmstat`).
- `warm [--leave-mb 384] [--chunk-mb 64] [--max-seconds 60]`: allocates MemAvailable minus the reserve in chunks,
  writes one byte in every 4 KB page from one thread per CPU, frees it; prints the `warm` object below.
- `touch --mb N --rate-mb-s R [--thp always|never] --out PATH`: maps N MB (`madvise` MADV_HUGEPAGE or
  MADV_NOHUGEPAGE), touches it page by page at the rate, times every touch, records touches over 200 us and a line
  a second.
- `echo-serve --bind ADDR`; `echo-send --target ADDR --pps 2000 --bytes 150 --seconds N --out PATH`: numbered
  datagrams echoed with the server's kernel receive stamp. A run of equal stamps beside a loss run is E1's
  signature; the record says how long the held run was and whether the datagrams before or after it were lost.
- `boot --out PATH [--warm] [--check-seconds N] [--max-stop-ms 20] [--max-steal-permille 10]`: the product path.
  Optional warm pass, then N seconds of `watch` with the fixed work loop running; writes and prints
  ```
  {"schema":"needletail.node-warm.v2","complete":true,"verdict":"ok","unix_us":1795383000000000,
   "warm":{"ran":true,"finished":true,"gib":3.31,"seconds":2.84,"worst_page_us":310,"pages_over_1ms":0,"pages_over_100ms":0},
   "check":{"seconds":60,"cpus":2,"stops_over_20ms":0,"stop_max_ms":0,"stopped_ms":0,"steal_permille":0,"late_max_us":180}}
  ```
  `verdict`: `ok`, `warm-timeout`, `stops` (a `stopped` tick over `--max-stop-ms`), `steal`. `complete` is true
  only with `ok`. Exit status 0 always: the service starts, and readiness keeps the node out.

### 2.3 Memory, av-mesh (`MESH/memory.rs`)

`--mlock-all` (AM:2303-2316) and `lock_all` (memory.rs:38-74, call at AM:3551) are removed. New `--mlock
<off|rings>`, env `NEEDLETAIL_MLOCK`, default `rings`. New `memory::pin_region(name, ptr, len)`: with `--prefault`
(default true, unchanged) writes one byte a page, then with `rings` calls `mlock(ptr, len)`; a refusal is counted,
logged once per region and changes nothing else. Called by the owner of each region at construction, before any
class thread runs:

| region | where | bytes at defaults |
| --- | --- | --- |
| hop journal ring | `HopJournal::new` (hop_journal.rs:175-208; today prefault only) | 262,144 x 88 = 23 MB (185 MB at 2,097,152) |
| hop journal seen table | same | 0.5 MB |
| stall journal ring | `StallJournal::new` | 0.2 MB |
| first-arrival table, one a class thread | `FirstArrival::new` (dedup.rs:43-52; `vec!` of zeroes is not touched today) | 2 x 2.6 MB |
| receive batch buffers | `RecvBatch::with_slots` (sys.rs:230-246): prefault only | under 1 MB |

`/api/mesh/local` gains `memory`: `{"mode":"rings","prefaulted_bytes":..,"locked_bytes":..,"lock_refused_bytes":..,
"vm_size_kb":..,"vm_rss_kb":..,"vm_lck_kb":..,"thp":"always|madvise|never","allocator_tuned":true}`;
`FastPlaneSnapshot.memory_locked` (RL:342) becomes "locked_bytes > 0 and nothing refused". The unit carries
`LimitMEMLOCK=512M` always (2.9); under a smaller limit (the namespace lab's 8 MB) the large rings are prefaulted
and not locked. Not covered, said plainly: the cache's parts (a `Bytes` each, first fill takes fresh pages on the
tee's thread), and the tee copy that the class thread allocates per datagram (`Bytes::copy_from_slice`, RL:1521).
Their in-guest cost is a minor fault per 4 KB; against host-level first touch the only cover is the boot check's
warm pass, if the lab keeps it. `mlockall` with CAP_IPC_LOCK was rejected: it would hold a 4K sweep's 2.4 GB
resident on a 4 GB node for no measured gain.

`--thp <keep|off>` (default `keep`; `off` = `prctl(PR_SET_THP_DISABLE)` before the first allocation) is commit
D11, built only if rule R3 of 7.5 fires.

### 2.4 The boot check and readiness

Unit `needletail-node-check.service` (`Type=oneshot`, `RemainAfterExit=yes`, `RuntimeDirectory=needletail`,
`RuntimeDirectoryPreserve=yes`, `TimeoutStartSec` = 70 + check seconds, `ExecStart=/usr/local/bin/
needletail-nodecheck boot --out /run/needletail/node-warm.json` with `--warm` and `--check-seconds N` as set), and
`Requires=`/`After=needletail-node-check.service` in the mesh and contributor units, rendered only when `node_warm`
is true or `node_check_seconds` is above 0 (both off by default until the lab decides: no placebo). It runs once a
boot (so on create, rebuild in place, replace and for an autoscaled node), not on a service restart or a roll.
av-mesh takes `--node-warm-file PATH` (rendered with the unit), reads it at start into `node_warm` of
`/api/mesh/local` and of C's `/api/node` (`{"configured":true,"complete":true,"verdict":"ok","warm":{..},"check":{..}}`) and sets
`ReadinessInputs.node_warm_complete` (C's struct): C's rule "configured and not complete is not ready" needs no
other change, and a node on a bad host never becomes ready.

**Product or trick: the line.** Product: what every node does by itself on every boot, whether or not a measurement
follows, through the same unit for the deploy tool and the autoscaler, visible in its status, finished before the
node is offered listeners. A trick: anything keyed to the measured session or done by the harness (a throwaway
first session, a dummy stream before the referee starts, a sleep after the deploy, an encode before the samples
exist). None of those is in this design.

### 2.5 Hop journal

No code change. `HopJournal::events` (hop_journal.rs:281-313) copies the whole ring whatever `since_us` is, 1,024
events a hold of the lock that the tee's `record` takes, then sorts and builds one string: a 60 s pull of an
enlarged ring maps and first-touches about 100 MB (both buffers are above the 32 MB mmap threshold of memory.rs:34).
So `/api/hops` is never polled during a session, here or in the product: it is pulled once after a session ends.
The lab enlarges the ring (`NEEDLETAIL_HOP_JOURNAL_CAPACITY=2097152`, 185 MB) on half its mesh nodes only (7.2).

### 2.6 Long-haul flows (new `MESH/fast_plane/flows.rs`)

**Parent.** `--relay-flows <1|2>` (default 2). `--relay-flow-ports TARGET=FIRST-LAST` (repeatable; TARGET the
target of a `--relay-forward`; at most 64 ports on that path's bind address): one non-blocking socket per port,
SO_PRIORITY 6, held in `RelayForwardPath.flows` (AM:5889). The compiled source socket stays the **base flow** and
carries everything it carries today (audio, video, repair, burst copies, reports). The **extra flow** is one of the
64 sockets, named by an atomic index that the class thread reads; it carries audio source symbols only, first
transmissions only (`plan_fast`, RL:715-765, notes them in `ForwardPlan.flow`; `send_fast_plan`, RL:814-915, sends
them with one `send_batch` per path after the base sends; never a retry, never a wait). Burst copies stay on the
base flow, 20 ms later (RL:1468-1488): path diversity does not replace time diversity on a shared segment.

**Child.** `--relay-flow-peers <primary|secondary>=IP:FIRST-LAST` (repeatable). `configured_relay_udp_dispatch`
(AM:5523-5578) binds every address of the range to that parent's session with `bind_controlled_peer` (RI:1464-1481;
registering an equal session again is accepted, RI:705-725; the session is `Clone`, RI:225). Then
`peer_admissions` (RI:1505) holds those addresses and the class thread's test (RL:1412-1414) is unchanged, and so
is the tee's dispatch, which is keyed by the exact peer (`sessions_by_peer.get(&peer)`, RI:1533). This matters: a
range fallback on the class thread alone, as the draft had it, would forward a part whose extra-flow copy came
first, reject it at the tee (`AuthenticationRequired`, a warning a datagram, RL:1913-1920) and drop its base copy
as a duplicate: forwarded and never committed at the far forwarder. `demand_peers` (RL:1977-1983) leaves the flow
addresses out, so no demand report goes to sockets nobody reads. The trust mode makes no authentication claim
today either (RI:221-226); the lab firewall admits lab addresses on all ports (LAB:786-801).

**Probe `RLP1`**, parent to child from the base socket and from each of the 64, every `--relay-flow-probe-ms` 500,
20 bytes big-endian: `"RLP1"`, version 1 (u8), flags (u8: bit 0 base flow, bit 1 carries media now), flow index
(u8), 0, probe id (u32), send time (u64, Unix us, taken immediately before the send). The child's class thread
takes it in `drain` at the non-relay branch (RL:1388) and never hands it to the tee.
**Report `RLP2`**, child to the parent's base address from the lane socket of that parent, every 500 ms, sent by
the audio class thread beside `report_demand` (RL:1224-1229): `"RLP2"`, version 1 (u8), count n (u8, at most 96),
window ms (u16), report id (u32), then n entries of 12 bytes: source port (u16), probes seen in the window (u16),
relative delay in us (u32: the median over the port's last 3 probes of kernel receive stamp minus send time, minus
the smallest such median of this parent's ports; 0xFFFFFFFF none), media firsts (u16), media duplicates (u16).
Both stamps of one probe are on two clocks, but the offset is the same for every port of a parent and cancels; so
does a pause of the sender between two sends. The parent reads it where it reads demand (`spawn_demand_readers`,
AM:6422-6452, after `decode_demand` fails).
**Mixed builds.** A child with `--relay-flow-peers` sends an empty report from its start; a parent probes and uses
an extra flow only toward a child it has heard a report from in the last 5 s. An old parent ignores `RLP2`
(`decode_demand`, AM:6440); an old child never gets `RLP1`. A mixed pair runs on the base flow alone.

**Choice** (a controller task per forwarder, `fast_control::run_flow_controller`; no timer on a class thread). Per
path, from each report: `d(p)` the relative delay of port p, `b` the base port's, eligible ports those with 2 of
their last 3 probes seen. `--relay-flow-class-us` 300.
- Base slow (`b` >= 300): want the eligible port of smallest `d` (the current extra is kept while its `d` is within
  150 us of it).
- Base on the fastest class (`b` < 300): want the eligible port of smallest `d` among those with `d >= b + 300`
  (the nearest other class; the current extra is kept while it still qualifies and is within 150 us of it); none
  if no port qualifies.
- A wanted port must be wanted in `--relay-flow-hold-reports` 3 reports in a row, then the index changes at once
  (one part goes to exactly one extra socket, the base carries every part throughout, the child's dedup hides the
  change). "None" must hold for `--relay-flow-idle-s` 60 before the extra flow stops. No report for 3 s: nothing
  changes. Before the first decision the extra flow carries nothing.
The deploy's `hunt` stays as an optional seed of the base port at bring-up (C's rule: under the ops lock, refused
under listeners).

**Dedup, meters, failover.** Unchanged. `FirstArrival`'s key has no peer in it (dedup.rs:18-25, RL:1420-1434): a
second flow is a second parent to it. `LossMeter::observe` counts only the peer whose ledgers arrive
(loss_report.rs:147-151) and no forwarder sends ledgers (`encode_ledger` has no caller outside tests). Failover
inputs are stamped per lane socket (RL:1419). C's `ParentFeeds` is keyed by sender IP.

**Stats.** `/api/mesh/local` gains `relay_flows` (not inside `FastPlaneSnapshot`, which is `Copy`, RL:289):
child side `in[]`: `{"parent":"lon-dist-a","path":"primary","port":24007,"base":false,"carrying":true,"media":..,
"firsts":..,"duplicates":..,"first_share_permille":..,"first_share_permille_10s":..,"probes":..,"delay_us":..}`;
parent side `out[]`: `{"target":"139.162.1.5:22200","base_port":22401,"extra_port":24007,"state":"no-reports|
probing|diverse|single-class","classes":2,"media_sent":..,"would_block":..,"switches":..,"last_switch_unix_ms":..,
"candidates":[{"port":..,"delay_us":..,"loss_permille":..}]}`. The hop journal already names the flow
(`relay_first` carries the peer's address and port); NT/scripts/compare/hops.py gains the per-flow share per region.

**Cost.** While the extra carries: one more datagram per part, parent and far forwarder (6 instead of 4 per part
and far forwarder with the burst copies; about 178 bytes each on the wire): +5.7 Mbit/s per region and 1 ms stream,
+2.3 for 2.5 ms Opus; a month of one 1 ms stream to one region is +1.9 TB. Probes: 65 sockets x 2 a second x 48
bytes = 50 kbit/s a path, reports 13 kbit/s. A link with one route class pays the probes only.

### 2.7 The AEP1 sender (`PR`; workstream A's code, commit S1 inside its AC8; this is D's contract)

- `--lateness-journal PATH [--lateness-journal-us 500]`: NDJSON written by a thread of its own through a bounded
  queue; the pacer never blocks (a full queue drops and counts). One `late` line per epoch whose first datagram left
  500 us or more after its deadline: `{"kind":"late","epoch":..,"deadline_us":..,"woke_late_us":..,"encode_us":..,
  "late_first_us":..,"late_last_us":..}`; one `sec` line a second: `{"kind":"sec","unix_s":..,"epochs":..,
  "late_p50_us":..,"late_p99_us":..,"late_max_us":..,"woke_late_max_us":..,"encode_p50_us":..,"encode_max_us":..,
  "over_1ms":..,"over_5ms":..,"minflt":..,"majflt":..,"nivcsw":..,"run_delay_us":..,"dropped":..}`. Today's report
  is a reservoir with no times (PR:3011-3081, PR:2862-2864).
- `--realtime-priority N` (default 20; 0 off) and `--pace-cpu N`: the pacing thread through
  `class_lanes::make_current_thread_realtime` (CONTRIB/class_lanes.rs:188); `--mlock` (default true):
  `mlockall(MCL_CURRENT | MCL_FUTURE)` once the PCM file and encoders exist, best effort, one line; `mallopt` as
  `memory::tune_allocator`; `--preroll-epochs 200`: a scratch encoder is run and dropped before epoch 0 (the
  stream's bytes do not change). The final report gains `realtime`, `pinned`, `memory_locked`.
- The pacing rule is unchanged: wait (sleep, then spin `min(300 us, epoch / 4)`, PR:2892, PR:2925-2927), encode,
  send. daw-nexus takes the same journal and scheduling later; it is not the referee's source.

### 2.8 The contributor (`AC`; workstream A's code, commit S2 inside its AC9; D's contract)

- SO_TIMESTAMPNS on the DAW media socket (`run_daw_media_udp_ingest`, AC:4767-4786); `src_first` and `src_last` take
  the kernel stamp; `rx_wait_us` = user read minus kernel stamp, per datagram.
- `GET /api/stalls?since_us=` on its HTTP port, 2.1's format (it copies `stall_journal.rs`): `tick` (per CPU);
  `src_gap` (an epoch's kernel arrival 2,000 us or more after its turn, the turn being the previous epoch's arrival
  plus one epoch: one clock); `rx_wait` (1,000 us or more); `inline_publish` (2,000 us or more; the 20 ms warning
  stays); `lane_send` (1,000 us or more).
- Per source and stream, each second, in A's pacing record (`/api/pacing` in its draft): `src_gap_max_us`,
  `rx_wait_p50_us`, `rx_wait_max_us`, `rx_to_publish_p50_us`, `rx_to_publish_p99_us` (kernel receive of an epoch's
  last datagram to `sent_first`: one clock), and `src_late_p50_us`, `src_late_max_us` (kernel arrival minus
  `session_id + pts`: two clocks, labelled so and never used to split less than 0.3 ms).
- `--prefault`, `--mlock <off|rings>` as 2.3: the hop journal ring (2^20 events, 92 MB, "touched as the ring
  fills" today, CONTRIB/hop_journal.rs:28-38) is faulted in and locked at start; scheduling outcome (`realtime`,
  `pinned` per audio hot thread) in its status.
- S3, only if the local source check or the lab's first session shows an `rx_wait` of 1,000 us or more or a p99.9
  of 200 us or more: the AEP1 recovery moves off `audio-hot-0`, which then only receives and stamps.

### 2.9 Deploy settings (`DEP`)

| setting | default | renders |
| --- | --- | --- |
| `mlock` (replaces `mlock_all`, DEP:371, DEP:2109; in C's `ROLLABLE_SETTINGS` the name changes) | None | `--mlock` on mesh nodes and the contributor |
| `node_warm`, `node_check_seconds`, `node_check_max_stop_ms`, `node_check_max_steal_permille` | false, 0, 20, 10 | the check unit, `Requires=`, `--node-warm-file` (2.4) |
| `stall_tick_us`, `stall_journal_capacity` | None | the flags of 2.1 |
| `relay_flows` | None | `--relay-flows`; on each public long-haul link (`link_addresses`, DEP:1175; same-site VLAN links never) the parent's `--relay-flow-ports <target>=F-L` and the child's `--relay-flow-peers <path>=<parent ip>:F-L`, F = `24000 + (index*2 + lane)*64`, 64 ports (the hunt's ranges, DEP:1515) |
| `node_overrides` | {} | `{node: {setting: value}}`, applied to that node's render and unit only |

`unit_text` (DEP:2862-2899): `LimitMEMLOCK=512M` on both services, always (the `infinity` branch at DEP:2874-2878
goes); the check unit's two lines when set. `binaries_for` (DEP:2902) installs `needletail-nodecheck` on every
role. `--host-tag <first 12 hex of host_uuid>` is rendered when the state has `host_uuid` and the binary has the
flag (B's seam, 5).

### 2.10 Lab tool (`LAB`)

`command_hosts` (LAB:1346-1375) keeps writing `host_uuid` and gains `--json`. It exits 1 when two distributors or
backbones of a region share a host (today), when two contributors do, or when a region's edges sit on fewer than
two hosts; edges that share a host otherwise are listed (`SHARED HOST edges ...`). It is run after `up`, after
every `add`, `grow` and `replace`, and before `push`.

### 2.11 New scripts (needletail)

- `scripts/compare/null-window.py --frames F.csv [--frames ...] [--arrivals A.csv] [--lengths 8,15,30] [--step 1]
  [--json OUT]`: loads the referee's `score` from `$NEEDLETAIL_COMPARE_ROOT/scripts/compare/change-window.py` by
  path (nothing copied: it is the referee's arithmetic) and scores every window `[t, t + L]`, t from first arrival
  + 7 s every `--step` s to last arrival - 1.5 s. Prints per probe and length: windows, fails by criterion
  (silence, p99, missing), the chance floor `(L+1)/(T-5)`, the steady longest silence, the range of window p99s.
  With `--arrivals`: the same on the kernel stamp and on publish to kernel. Exit 0 (a measurement).
- `scripts/compare/hunt-report.py DIR`: 7.4. `scripts/compare/first-session.py`: N fresh local labs in the referee's
  order (deploy, idle, C's `edge_lab.py burn`, contributor restart, first stream), first session against a warm
  one, with each node's stall journal and the null. `scripts/compare/source-check.py --seconds 600`: sender to the
  lab's contributor, no listeners; E3's episode table split by `woke_late`, `encode`, link, `rx_wait`.
- `scripts/linode-stall-hunt.py plan|run|collect|down` and `scripts/linode-route-survey.py`: section 7.

## 3. State machines and timing

No change that the referee scores (drain, kill, hand-over, roll, autoscale) is in D's area. D owns the steady
state those changes are scored against, and three machines.

### 3.1 The steady state: acceptance, decided now

On every clean probe of 300 s or more (the lab's S5 and S6, and `change-run.py --seconds 300` locally), with
`null-window.py`, scored as the referee scores it:
- **p99 criterion**: fails in no more than 2 % of 8 s windows and 5 % of 30 s windows.
- **Silence criterion**: fails no more than its chance floor plus 2 points (floor at T = 300 s: 3.1 % at 8 s, 10.5 %
  at 30 s; at 600 s: 1.5 % and 5.2 %). No single figure for it can be met on a 60 s probe by any system (floor
  16 %).
- **Missing**: never.
- **Source component**: over a session, no episode of `published - ideal` more than 1 ms above the session's
  median, and every episode over 0.5 ms attributed by the journals to the pacer, the encode, the link, the
  contributor's read or its publish, or to a `stopped` tick of the source or contributor node (listed apart).
- Reported, not accepted against: kernel to client at the listener host (the referee's host).
Where the lab does not meet these, `hunt-report.py` names the component, and every later change result is quoted
beside its null rate. A window longer than a tenth of its probe (a whole roll) cannot be judged on silence at all:
its floor is over 10 %.

### 3.2 A node, from boot

boot -> `needletail-node-check` (service not started; warm at most 60 s, 3.3 GiB should take 2 to 4 s; check N s)
-> `/run/needletail/node-warm.json` -> service starts (regions faulted in and locked before the class threads;
tick threads up) -> C's readiness (`node_warm_complete` among its warm-up conditions) -> ready. With `verdict` other
than `ok` the node runs and is never ready; the operator or the autoscaler replaces it (D12, 5).

### 3.3 The flow controller, per path

`no-reports` (child silent over 5 s: no probe, no extra) -> `probing` (reports arrive) -> `diverse` (extra carries)
or `single-class` (60 s without a second class: extra stopped, probes go on) and back after 3 reports with one.
- One of two classes steps or dies while the pair is one flow on each: no part is late; if it was the extra's, the
  controller restores diversity when another class appears.
- Both carrying flows turn slow and a faster port exists: the median of 3 turns after 2 probes (1.0 s), the next
  report is at most 0.5 s away, the hold is 2 more reports (1.0 s), plus one probe phase: 3.0 s at most.
- All 64 ports move together: nothing to choose; the step is paid as today (a shared segment).

### 3.4 The sender, per epoch

wait (sleep, spin) -> encode -> send to every target -> journal entry (queued). A stall delays only the epochs it
covers, each then leaves at once (the deadlines are absolute), so a hold of n ms reads as n `late` lines falling
1 ms an epoch.

### 3.5 What the rule sees of D's mechanisms

A change of extra flow: no part missing (the base carries all), no silence (first arrival), p99 unchanged or
better. The boot check: before service, outside every window. The tick threads: SCHED_OTHER under the class
threads. Probes: 20 bytes on sockets of their own.

## 4. Failure cases and races

| case | what happens |
| --- | --- |
| the boot warm pass cannot finish | stops at 60 s, `verdict: warm-timeout`, service starts, node not ready |
| the boot check sees a stop or steal over its limits | `verdict: stops` or `steal`; not ready; replaced once by the tool that created it (D12) |
| a `mlock` refused (limit below the region) | counted in `lock_refused_bytes`, one line, the region stays prefaulted |
| stall journal ring full, or a storm | oldest overwritten; merged events carry `n` and `span_us`; `dropped` counts refused records |
| tick thread cannot be pinned (a lab cpuset) | runs unpinned; `tick_pinned` below `tick_threads`; `scope` is then not trusted by the report |
| a node at real overload | `starved` ticks with run delay near `dur_us`: read as overload, never as a stop |
| the service is frozen (SIGSTOP, cgroup) | `stopped`, `scope: all` in its own journal; `nodecheck watch` on the node shows nothing |
| one vCPU stopped | `stopped`, `scope: one` on that CPU; a class thread pinned there shows `class_pass` with cause `stopped` |
| child without `--relay-flow-peers` (old build in a roll) | parent hears no `RLP2`: no probe, no extra media |
| parent restarts | its flow sockets rebind the same ports; extra off until 3 reports |
| child restarts | its ranges are flags; it reports from start; the parent's state goes to `probing` after 5 s of silence |
| a flow port cannot be bound | that port is left out of the set; fewer than 2 bound: flows off on that path, said in `out[]` |
| the extra's route dies | first arrival is the base; the controller moves the extra after 3 reports |
| every port within 300 us (Sydney on 19 Nov) | `single-class`: probes only |
| three or more classes | the extra sits on the fastest or the nearest other; both rules still give two classes |
| a probe datagram reaches an old tee | cannot: probes go only to a child that reports; a new child drops `RLP1` on the class thread |
| a forged datagram from a parent's address and range | as today for the parent's own port: a controlled private network |
| the journal queue of the sender is full | the line is dropped and counted; the pacer does not wait |
| the source stalls 30 ms | every listener is 30 ms late for 30 parts; nothing in the mesh hides it; S1 and S2 name the side |
| `/api/hops` pulled during a session | not done (2.5); `hunt-report.py` refuses a pull time inside a session |
| two edges of a region on one host | `LAB hosts` lists it; B's alternate avoids it by host tag; fewer than two hosts in a region: exit 1 |

## 5. Implementation plan

Four units that can be built at the same time. Within a unit the commits are in order. The paid lab needs D1 to D6
and S1, S2; it does not need D7 to D9. Units I and F meet in three functions of `RL` (`ClassThread::run`, `drain`,
`send_fast_plan`): D1's single lines there land first, and D7's edits to those three are its last step, after D1
and after A's AM3; everything else of D7 (`flows.rs`, the dispatch, the controller, the tests) is built meanwhile.
Units T and F meet in `DEP` in different functions (`unit_text` and settings against the relay render).

| unit | # | repo | files, functions | tests |
| --- | --- | --- | --- | --- |
| I instruments | D1 | av-mesh | new `stall_journal.rs` (`StallJournal`, `record`, `query`, `classify`, `spawn_ticks`, `StallSnapshot`); `RL`: two lines in `ClassThread::run` after `record_loop` (1238-1244), one in `drain` after 1380, one in `Tee::process` (1745), one in `send_fast_plan` (after 878), a timing pair and one line in `send_lease_plan`; `AM`: flags, tick start beside the hop journal (3543), route arm (22463), `stalls_response`, `MeshApiSnapshot.stall` | `ring_wraps_and_merges_events_closer_than_10_ms`; `a_late_tick_with_no_run_delay_is_stopped`; `a_late_tick_with_run_delay_is_starved`; `a_long_pass_is_own_unless_its_cpu_was_stopped`; `one_cpu_stopped_is_scope_one`; `a_contended_record_is_dropped_and_counted`; `stalls_endpoint_filters_by_since_us` |
| | D2 | av-mesh | new `bin/needletail-nodecheck.rs` (`watch`, `warm`, `touch`, `echo-serve`, `echo-send`, `boot`) | `warm_touches_what_it_reports_and_stops_at_its_limit`; `watch_books_an_injected_delay_on_its_cpu_only`; `touch_counts_a_slow_page`; `echo_finds_a_held_run_and_which_end_was_lost`; `boot_says_stops_and_exits_zero` |
| | D3 | av-mesh | `memory.rs` (`LockMode`, `pin_region`, `report`; `lock_all` removed); `--mlock` replacing `--mlock-all` (`AM`:2303-2316, 3551); `HopJournal::new`; `FirstArrival::new`; `RecvBatch::with_slots`; `--node-warm-file`, `MeshApiSnapshot.memory` and `.node_warm`; `ReadinessInputs.node_warm_complete` (C's struct, after its readiness commit) | `a_region_is_locked_under_a_finite_limit`; `a_refused_lock_is_counted_and_the_node_runs`; `mlock_defaults_to_rings_and_mlock_all_is_gone`; `the_first_arrival_table_takes_no_fault_on_first_use`; `the_node_warm_file_reaches_the_snapshot` |
| T tooling | D4 | needletail | `DEP`: settings of 2.9, `check_settings`, `render` (per-node overrides), `unit_text` (after C's CT2), check unit, `binaries_for`, `--host-tag`; `LAB`: `command_hosts` | `test_unit_has_a_finite_memlock`; `test_check_unit_only_when_set`; `test_node_override_reaches_only_its_node`; `test_host_tag_from_host_uuid`; `test_hosts_flags_contributors_and_a_region_with_one_edge_host` |
| | D5 | needletail | `deploy/low-latency/topology-hunt.json`; `scripts/linode-stall-hunt.py`; `scripts/linode-route-survey.py` | cost under the ceiling; the timeline's order; a dry run prints every command; survey record round trip; the three policies on a synthetic record (the chooser fixture `flows-chooser.json` is kept byte for byte in both repos; a test compares them when `NEEDLETAIL_AV_MESH_ROOT` is set) |
| | D6 | needletail | `scripts/compare/null-window.py`, `hunt-report.py`, `first-session.py`, `source-check.py` | `test_null_uses_the_referee_s_score`; `test_floor`; `test_gap_join_names_a_cause_and_counts_the_unexplained`; `test_host_grouping_and_simultaneity`; one synthetic input per rule of 7.5; the wave 7 files give E1's table and E4's rates when present |
| | D9 | needletail | `LB` and hops.py: `missing_at_edge`, `missing_last_mile`; the three burst scenarios of 6 | `test_missing_parts_are_split_by_side` |
| F flows | D7 | av-mesh | new `fast_plane/flows.rs` (wire, `FlowSet`, `FlowStats`, `Chooser`, snapshot); `RL`: `plan_fast` and `send_fast_plan` (extra sends), `drain` (two sites: `RLP1` at 1388, per-flow count at the dedup outcome 1431), `run` (the report beside `report_demand`), `demand_peers` (1980); `AM`: flags, `configured_relay_udp_dispatch` (the range loop after A's bindings), `spawn_demand_readers` (`RLP2`), `RelayForwardPath.flows`, `MeshApiSnapshot.relay_flows`; `fast_control.rs`: `run_flow_controller` | `probe_and_report_round_trip`; `chooser_takes_the_fast_port_when_the_base_is_slow`; `chooser_takes_the_other_class_when_the_base_is_fastest`; `no_second_class_no_media`; `a_choice_needs_three_reports`; `every_flow_address_is_bound_to_its_parent_s_session`; `a_part_whose_extra_copy_came_first_commits_at_the_child`; `flows_carry_audio_first_transmissions_only`; `flow_ports_get_no_demand_report`; `a_probe_never_reaches_the_tee`; `an_old_child_gets_no_probe`; `both_flows_slow_recovers_within_three_seconds` |
| | D8 | needletail | `DEP` render of the flow flags; `EL`: `set_wan_routes`, verbs `routes`, `stall`, `stall-source` (each prints `start`/`end` marks); `LB`: `--route-step`, `--stall-source`, the flows table; hops.py per-flow share | `test_flow_flags_on_public_links_and_their_mirror`; `test_no_flow_flags_on_vlan_links`; `test_wan_routes_filter_text`; `test_per_flow_share_from_a_journal_fixture` |
| S source (A's engineer) | S1 | av-contrib | `PR`: 2.7 (in AC8, beside the repeatable `--target`) | `the_journal_never_blocks_the_pacer`; `a_late_epoch_is_one_late_line`; `preroll_leaves_the_stream_s_bytes_unchanged` |
| | S2 | av-contrib | `AC`: 2.8 (in AC9); a copy of `stall_journal.rs` | `the_daw_socket_stamps_in_the_kernel`; `a_source_gap_is_booked_on_one_clock`; `the_ring_is_faulted_in_at_start` |
| | S3 | av-contrib | conditional (2.8) | `the_receive_thread_only_receives` |
| after the lab | D10 | av-mesh | only if D9 asks: `--relay-lease-burst-copy-ms` in `send_lease_plan`, secondary parent only | copy from the secondary only; ring bound |
| | D11 | av-mesh | only on R3: `--thp off` | the flag sets the process attribute |
| | D12 | needletail | only on R1 or R2: defaults of `node_warm` or `node_check_seconds`; C's `advance_join` and `push` replace a node whose verdict is `stops` or `steal` once (`LAB replace`, `push --nodes`) | a failed check leads to one replace and a second check |
| | D13 | needletail | only if the survey finds no gain on any link: `relay_flows` 1 in the scale run's settings | - |

Order of landing: D1, D2, D3; D4 (after D2 and D3 for their flags, and after C's CT2); D5 and D6 beside D4; S1 and
S2 beside all of these; then the paid lab; then D10 to D13 as its rules and D9 say. D7 with D8, and D9, at any time
(D7's `RL` edits as above). Each commit builds and passes its tests alone; D3 removes `--mlock-all` and D4 stops
rendering it in the same push to the two repos' branches.

**Seams.**
- C: `AM` route arms and snapshot fields (D1, D3: other arms and fields than C's `/api/node`); `ReadinessInputs`
  (D3 adds one field and its read); `unit_text` (D4 after CT2; first start after a boot takes the check's time
  longer, which the readiness wait must allow when the settings are on); `EL` `main()` (D8's verbs are other arms
  than C's); `LAB` (D4 edits `command_hosts` only); scale-run's collector pulls `/api/stalls?since_us=<phase
  start>` per phase (one call, helper in `hunt-report.py`); C's `Config.min_join_age_s` is not needed (the idle-age
  branch is dropped); `change-run.py` supplies the probes D's local proofs score. C's CR4 call in `drain` is not
  affected: D7 no longer touches the admission test.
- A: `ClassThread::run` and `drain` and `FastPlaneStats` are also edited by A's AM3 (clock table, per-lane counters,
  4 batches a turn): D1 lands first and is five single lines; D7 lands after AM3 and adds its count directly after
  A's per-lane count. `configured_relay_udp_dispatch`: D7's loop follows A's second-contributor binding.
  `Tee::process_symbol` is A's; D does not edit it. A's `wall_late_parts` replaces `deadline_drops` as the "fed
  late" figure once deadlines are on the stream's timeline: `hunt-report.py` reads whichever exists. S1, S2, S3 are
  A's commits to D's contract.
- B: `--host-tag STRING` on av-mesh, carried in the edge's advertisement and shown as `caches[].host` in
  `/api/assignment`; a listener's second edge is the best-ranked edge whose `host` differs from the first's when
  both are set. The client lane's `send_slow` (one call into `stall_journal`). `missing_last_mile` (D9) is B's to
  act on; E1's first-of-two figures are B's evidence. The two-path client in 6's stall scenario.

## 6. Local proof (no Linode)

The stop itself does not happen on the dev box (14 sessions in the previous lead's record). Locally the
instruments, the flows and the null are proven; SCHED_FIFO and a finite memlock above 8 MB are not available in the
rootless lab, so the sender's scheduling remedy and the locks are proven on Linode only (7).

- Unit and in-process tests: section 5.
- **Null**: `change-run.py run --name steady-300 --lanes opus-1,pcm-1 --seconds 300 --at 150 -- sleep 1` (C's
  runner: the referee's client, `ntcmp udp-ingest`), then `null-window.py` on its frames and arrivals. Pass: 3.1's
  numbers, three runs. If the lab cannot meet them, that is reported with the component (C's `lab-parity.py`
  layout Q1 is the reference), and local change proofs are quoted beside it.
- **Source**: `source-check.py --seconds 600` for opus 48 and pcm 48. Pass: every episode over 0.5 ms attributed;
  the count over 1 ms is recorded as the normal-scheduling baseline.
- **A stopped edge**: `change-run.py run --name stall-edge --seconds 120 --at 45 -- scripts/edge_lab.py stall
  lon-edge-1 300`. lon-edge-1's `/api/stalls` has one `tick` per CPU of 300 ms +- 5, `stopped`, `scope: all`, and
  any `class_pass` of that interval is `stopped`, not `own`; no other node has an event. With one-path clients the
  window fails (expected, recorded); with B's two paths it passes the rule.
- **Overload is not a stop**: `edge_lab.py burn lon-edge-1 20` (C's verb) under 40 listeners: every tick event of
  lon-edge-1 is `starved`; none is `stopped`.
- **A stalled source**: `... -- scripts/edge_lab.py stall-source 30`: 30 `late` lines falling 1 ms an epoch with
  `woke_late_us` carrying it, the contributor's `src_gap` shows the same 30 ms, `rx_wait` near 0; verdict
  "source". The window fails by design.
- **Routes**: far site at Tokyo's distance (`NEEDLETAIL_EDGE_LAB_SITE_DELAY_MS=newyork=101`), `edge_lab.py routes
  newyork 104 108` (`set_wan_routes`: on `w-newyo` in namespace `core`, a `prio` root with two netem bands and a
  u32 filter on the UDP source port's parity). (a) `routes step` (the filter swaps the bands) at 45 s: with
  `relay_flows=2` the window passes the rule, the far p50 stays within 0.1 ms of the fast band, the far forwarders'
  `in[]` show a first share on two ports; with `relay_flows=1` the p50 moves 3.9 ms (recorded: the wave 7 case).
  (b) `routes drop slow 5`: 0 missing, p99 within 1 ms. (c) both bands 108 then one back to 104 with the base on
  the slow parity: the extra is on the fast band within 3.0 s (the bound of 3.3). (d) both bands equal: `out[]`
  reads `single-class` after 60 s and the far forwarders receive 4 datagrams a part, not 6.
- **Burst** (D9), each 3 x 200 s, Opus 1 ms, 2 UDP clients a host, `loss gemodel 1% 20%` on one link class at a
  time: the far site's WAN link (`edge_lab.py wan newyork both`), every London edge's VLAN link (`net <edge> vlan
  both`), the contributor's VLAN link. Pass: 0 `missing_at_edge`; parts that reached an edge only by repair: p99
  lateness 10 ms or less in London, 20 ms on the far side. If the edge-VLAN case is later than that, D10.
- **No regression**, Opus and PCM clean, 3 pairs each, base against this build (journal, rings, flows 2 with two
  bands): London p50 within 0.05 ms, p99 within 0.15 ms, 0 missing; edge CPU within 1 point; first forwarders: one
  more datagram a part and far child while `diverse`, and their CPU up by no more than 5 us per extra datagram (3
  points of one CPU for a 1 ms stream to six far children); tick threads under 1 % of a CPU each.
- **First session**: `first-session.py --labs 5`: first session against a warm one, per node stall journal, null.

## 7. Deploy, tooling and the paid lab (revised after the wave 9 run, 24 Nov 2026)

The wave 9 run (`.resilient/reports/w9-stall-analysis.md`) showed stops of 0.3 to 2 s at the edges around a UDP
client's arrival, on the audio class thread, sometimes on that thread alone, and both London edges stopped
together for 446 ms. The fan-out fix (av-mesh `rm/fanout`, `.resilient/reports/fanout-stall.md`) moves every
first-use start and allocation off the class thread but could not show what makes the stops. The lab below asks,
in this order: does any edge still stop for 100 ms or more in the referee's first session on a fresh deploy of the
fixed build, with UDP clients arriving at every edge; for each stop that remains, was the class thread runnable and
not run, or blocked in our code, and on which wait; do the arena warm-up, first touch of the guest's memory, the
codec of the first session and listener churn change it. The text this replaces is kept under "Before the wave 9
run" at the end of this section.

### 7.0 The commands, in order (the lead runs them; dry run first)

**The lab cannot start yet.** A real run refuses (step 2 names each) until all of these exist; none is in this
unit, and the lead decides whether to wait for them:
- av-mesh `--relay-class-arena-warm-mb N` (the no_arena_warm arm; 7.8): `ClientSender::new` in
  `fast_plane/edge_clients.rs`, the fan-out fix's code (rm/fanout's follow-up). Not on any av-mesh branch on 24 Nov.
- `needletail-nodecheck touch --hold` (the touched arm; 7.8): av-mesh `bin/needletail-nodecheck.rs`, D2 (unit I).
  Not in av-mesh `resilient-mesh` eb1f5c4 nor rm/av-mesh-di 7876bd7.
- The watchdog's bounded collect: needletail rm/lab2 (the lab tool's `up --collect-timeout-s`) and rm/arch2 (the
  deploy tool's `collect --run-id`, which that collect runs), merged into `resilient-mesh`.

From the integration worktree (`/home/jamieb/wavey.ai/needletail-resilient`, branch `resilient-mesh` with this
work merged), in tmux:

```
export NEEDLETAIL_AV_MESH_ROOT=<av-mesh checkout of the integration build: rm/fanout, D1 to D3, the arena switch>
export NEEDLETAIL_AV_CONTRIB_ROOT=<av-contrib checkout with S1 and S2>
export NEEDLETAIL_COMPARE_ROOT=/home/jamieb/wavey.ai/needletail-compare
# the zigbuild release set: $NEEDLETAIL_AV_MESH_ROOT/target/x86_64-unknown-linux-gnu/release/{av-mesh,
#   needletail-nodecheck,needletail-load,needletail-udp-client}, $NEEDLETAIL_AV_CONTRIB_ROOT/.../{av-contrib,
#   aep1-48k-probe}; target/release/needletail-compile of this checkout
```

1. `scripts/linode-stall-hunt.py plan`. No lab, no token, no request. It prints first:
   ```
   20 instances (16 g6-dedicated-2, 4 g6-dedicated-4): $1.296 an hour; ceilings $1.50 an hour and 24 nodes; watchdog 1.9 h
   2 billed hours $2.59; with 2 replacement instances $2.70; one repeat at most: $5.40
   the sessions end by minute 80.2 at worst; collect at 82, down at 88, the watchdog at minute 114
   ```
   then every command of the run under `# minute N: <step>` headers, from `run_archive.py ... create` and the lab
   tool's `up` to `down` and `run_archive.py ... finish <run> --status complete`. It must print no line ending in
   `failed: ...`.
2. `scripts/linode-stall-hunt.py run --dry --budget-usd 300`. The same, through the code of a real run, with one line
   `# a real run would refuse: <why>` for each thing a real run would refuse (a missing binary, an av-mesh without
   `--stall-journal-capacity`, `--hop-journal-capacity` or `--relay-class-arena-warm-mb`, a `needletail-nodecheck
   touch` without `--hold`, an `aep1-48k-probe send` without `--lateness-journal`, a lab tool without
   `--watchdog-hours`, `--collect-timeout-s` or `--budget-usd` on `up`, a deploy tool without `collect --run-id`,
   another lab's state or settings in this checkout). Go on only when it prints none.
3. `scripts/linode-stall-hunt.py run --budget-usd 300 [--spent-before-usd S]` (S: what the ledger says was spent;
   default the lab tool's ledger). It prints `run hunt<MMDDHHMM>: records in target/stall-hunt/hunt<MMDDHHMM>`, the
   lab tool's `up` (each instance created, tagged and firewalled, the watchdog armed with its collect of at most
   240 s), then each step's commands
   as it runs them; a failed step prints `<step> failed: <why>` and the run goes on; `collect` and `down` always run
   (also on Ctrl-C, SIGTERM or a hangup). Exit 0 when every step passed. About 90 minutes.
4. `scripts/compare/hunt-report.py target/stall-hunt/<run> --json target/stall-hunt/<run>/report.json`. Prints the
   sections of 7.4; section 7 ends with the rules H1 to H7, one line each, then `first session, <edge>: ...` for
   every edge. It exits 1 with `no report of ...: the /api/hops pull after S1 ... is inside ...` for a record whose
   pull fell inside a session (the pull's first touch would be inside a measured window); the driver pulls once,
   between S1's end and S2's start, so its own runs never are.
5. Only if the run was cut off before its own end: `scripts/linode-stall-hunt.py collect`, then
   `scripts/linode-stall-hunt.py down`. If `down` is refused because the run is not marked collected, collect again;
   `linode-small-mesh-lab.py down --uncollected` records the run as failed. The watchdog deletes the lab at 1.9 h
   whatever happens.
6. The cost: `linode-small-mesh-lab.py` writes the lab to the ledger at `down`; add it to
   `.resilient/ledger.md`. Expected $2.59 to $2.70.

### 7.1 The lab (`deploy/low-latency/topology-hunt.json`, `scripts/linode-stall-hunt.py`)

| group | nodes | plan | $/h |
| --- | --- | --- | --- |
| mesh, London | contrib-lon, lon-src, lon-view | g6-dedicated-4 | 0.324 |
| | lon-dist-a, lon-dist-b; lon-edge-1 and -2, lon-edge-3 and -4 (two pairs of twins) | g6-dedicated-2 | 0.324 |
| mesh, Tokyo | tok-dist-a, tok-dist-b, tok-edge-1, tok-edge-2 (one pair) | g6-dedicated-2 | 0.216 |
| | tok-view | g6-dedicated-4 | 0.108 |
| bare (route survey, 7.6 below) | lon-bare-3, lon-bare-4, tok-bare-1, new-bare-1, syd-bare-1, ams-bare-1 | g6-dedicated-2 | 0.324 |
| | 20 instances | | **1.296** |

Dedicated plans only. Two billed hours: $2.59; with two replacement instances of an hour ($0.054 each): **$2.70**.
One repeat at most (H2, "no stop in the run"): $5.40 in all. Every instance is created and deleted by the lab
tool (`up --run-id hunt<MMDDHHMM> --optional-sites sydney --watchdog-hours 1.9 --collect-timeout-s 240 --budget-usd
B`, `remove`, `grow`, `down`) with its own state file (`target/linode-qualification/hunt-lab.json`) and ceilings
(`NEEDLETAIL_LINODE_MAX_HOURLY_USD=1.50`, `NEEDLETAIL_LINODE_MAX_NODES=24`); at its deadline the watchdog runs the
deploy tool's `collect --run-id` for at most 240 s, then deletes (rm/lab2 and rm/arch2; the preflight refuses a lab
tool or deploy tool without them). The driver never reads the token. Everything it collects is copied into the run archive
(`~/needletail-data/<run>/`, `run_archive.py add-tree`, then `collected`) before `down`. Compared with the plan
before the wave 9 run: one more instance in Tokyo (a pair of twins), no bare memory-touch nodes; no node is warmed,
soaked or kept as the wave 7 control.

### 7.2 Arms and per-node settings

- **Fresh deploy of the fixed build.** `push --remeasure` of the integration build (`rm/fanout` merged), every mesh
  node with `stall_journal_capacity=65536`. Nothing runs on the mesh before the first session except the referee's
  idle and burn, the watch, the sampler and the touched arm.
- **Touched** (half the edges, one of each pair of twins): lon-edge-1, lon-edge-3, tok-edge-1. At minute 15
  `needletail-nodecheck touch --mb <MemFree - margin> --rate-mb-s 1024 --hold --out touch-guest.ndjson` writes
  every page of the guest's free memory once and holds it; at minute 19.5 it is sent SIGTERM and the memory goes
  back to the guest just before the first session (`touch-marks.txt`: `touch <unix> <MB> <MemFree MB> <margin
  MB>`, `released <unix> finished|unfinished`). The margin is 384 MB, or twice the zones' `high` watermarks
  (`/proc/zoneinfo`) when that is more, so the kernel neither reclaims nor swaps for the touch. MemFree, not
  MemAvailable: MemAvailable counts the page cache (av-mesh's own text, libc, the journal), which the touch would
  evict and the class thread would then fault back in, inside S1; and the instances keep Linode's 512 MB swap
  disk, which pressure would use. The sampler records swap and refaults, `system.txt` the swap; H4 leaves out a
  touched edge whose touch made either grow. `--hold` is what makes this the test: without it the memory is given
  back at minute 15, and a guest whose free pages are reported to the host (virtio-balloon free page reporting,
  feature bit 5; the bits are recorded) may have them taken back before the session.
- **Without the arena warm-up** (one region's edges): tok-edge-1, tok-edge-2 run
  `--relay-class-arena-warm-mb 0` (`node_overrides`, `extra_edge_args`); London's edges keep the fix's 32 MB. The
  same hour, the same build, no extra instance. The region is the other variable (Tokyo is 106 ms from the
  contributor), which the rule states. `--relay-class-arena-warm-mb` does not exist yet: see 7.8.
- **Hop journals** of 2,097,152 events on both London forwarders and the first pair of London twins (lon-dist-a,
  lon-dist-b, lon-edge-1, lon-edge-2), pulled once after S1 (`/api/hops?since_us=<S1 start>&stream=3001`), never
  inside a session: a pull allocates 50 to 80 MB on the node.

### 7.3 Timeline, minutes after `up`

| minute | step |
| --- | --- |
| 0 | `lab hosts` (`host_uuid` into the state); on every node, first command: `needletail-nodecheck watch`; `push` (the fresh deploy); on every mesh node the sampler (7.4); the route survey; media to the hosts |
| 15 | the touched edges touch and hold their free memory |
| 18 | the referee's 20 s burn on every mesh node |
| 19.5 | the touched edges give their memory back |
| 20 | **S1, the first session**: Opus Custom 1 ms, the referee's order (contributor restarted, the sender at the next 64 s boundary with `--lateness-journal`, listeners 12 s after it), 70 s. lon-view: 4 UDP probes through lon-dist-a, an LL-HLS probe (HTTP/3, window 40) at each London edge's own media playlist, a `needletail-load` crowd of 40 (8 sampled) over the four London edges; tok-view: 2 UDP probes, a probe at each Tokyo edge, a crowd of 20 (4 sampled) over both. The crowd has no entry point, so its listener i starts at edge i mod n and UDP clients arrive at every edge (through `/api/assignment` new readers go to the edges that already hold the playlist: in wave 9 every lon-view lane sat on lon-edge-1); the report checks it per edge. Then the one `/api/hops` pull |
| 24 | S2, PCM 1 ms, 70 s (the referee's second session) |
| 28 | S3, listener churn on every edge: the listeners of S1 join together for 50 s, leave, and join again 12 s later, three rounds (`rounds.txt` holds each round's start; the sender runs 15 s a round longer than the rounds, as each ends only when its probes have) |
| 35 | the edge not run most in S1 to S3 (the watch's stopped ms that ended in a session plus the sampler's seconds in a session in which the class thread waited runnable 50 ms or more; never the deploy, the burn or the touch, which would choose a touched edge for its arm) is replaced: its records fetched, `remove`, `grow`, `hosts`, watch, `push --nodes`, sampler (its VLAN address is kept, so no peer is re-rendered). None if no edge was not run |
| 44 | every forwarder and edge restarted at once (`deploy restart --roles distributor,edge`): new processes on guests that have run a session, and on the new guest of the replaced edge |
| 46 | S4, PCM 1 ms, 70 s (**the repeat, PCM first**) |
| 50 | S5, Opus Custom 1 ms, 70 s |
| 54, 63, 72 | S6 Opus Custom 1 ms 420 s, S7 PCM 1 ms 420 s, S8 Opus 2.5 ms 300 s (3.1's steady probes, the 2.5 ms lane of 7.7) |
| 82 | collect: samplers and watches stopped, every node's records, the sessions, the survey, `lab hosts`; into the run archive |
| 88 | `down` (the watchdog is at 1.9 h) |

### 7.4 What is recorded

- **The sampler** (`scripts/hunt-sampler.sh`, one process a mesh node, `nodes/<node>/sampler.ndjson`), once a second:
  the audio class thread (`needletail-fast` with a real-time policy, else the lowest tid of that name: `/proc` cuts
  `needletail-fast-audio` and `-video` alike, and av-mesh starts the audio one first; av-contrib's `audio-hot-0`)'s
  `/proc/<pid>/task/<tid>/schedstat` (run time, run delay, timeslices) and `status` (voluntary and involuntary
  context switches) and its last CPU; every thread's policy, priority and last CPU (`ps -L`); `/proc/vmstat`
  (`pgfault`, `pgmajfault`, `thp_fault_alloc`, `thp_fault_fallback`, `thp_collapse_alloc`, `compact_stall`,
  `compact_fail`, `allocstall_*`, `pgscan_direct`, `pgsteal_direct`, `pswpin`, `pswpout`,
  `workingset_refault_anon`, `workingset_refault_file`); `MemFree`; and `/api/mesh/local` cut to these
  counters at their paths (`MESH_PATHS`, the same list in `stall_records.py`, each checked against av-mesh's
  MeshApiSnapshot): `stall.` `tick_threads`, `tick_pinned`, `tick_late_max_us`, `stopped_ms`, `starved_ms`;
  `client_udp.fast_lane.` `fanout_started`, `fanout_policies`, `fanout_cpus`, `fanout_threads`,
  `fanout_first_dispatch_faults`, `fanout_dispatch_max_us`, `fanout_shed`, `fanout_waited`, `class_plan_grows`,
  `class_compact_max_us`, `class_send_max_us`, `class_repair_max_us`, `class_arena_warm_bytes`;
  `relay_session.fast_plane.` `audio_loop_busy_max_us`, `audio_loop_busy_over_5ms`, `audio_loop_busy_over_50ms`,
  `audio_loop_gap_max_us`, `audio_thread_minflt`, `audio_thread_majflt`, `process_minflt`, `process_majflt`,
  `kernel_dropped`, `tee_dropped`; `status.ingest.` `audio_deadline_drops`, `video_deadline_drops`. Every
  tenth second `/api/stalls?since_us=` (`stalls.ndjson`) and, on the contributor, `/api/pacing`, pulled in the
  background one at a time so that a slow answer never widens a one-second line, and once more when the sampler
  ends or is sent SIGTERM (collect and the replacement wait for it), so the journal's last seconds are kept.
  Nothing is polled from the dev box.
- `needletail-nodecheck watch` on every node from minute 0 (`watch.ndjson`); the touch files; `system.txt` (THP
  setting, the balloon and its feature bits, clocksource, swap, `chronyc tracking`).
- Per session and view host: the UDP probes (arrivals, frames, `uc.json` with the caches each used), the LL-HLS probe
  of each edge (its rows carry the edge's commit stamp of every part), the crowd's output (`intervals.jsonl`: its
  listeners subscribed and its parts per edge, each second), `rounds.txt`; the sender's lateness journal and
  `source.start`.
- `hunt.json` (schema `needletail.stall-hunt.v2`): periods (`session` with its `role`, `touch`, `restart`),
  sessions (`role`, `rounds`, the edges each host's probes cover, the one `hops_pull`), `arms`, `touch`,
  `replaced` (with `not_run_ms_before`, `host_uuid_before/after`, `public_ipv4_before`, `watched`, `pushed`,
  `sampled`), `not_run_ms_at_replace`, `hosts`, `nodes`.

`scripts/compare/hunt-report.py` prints: 1. per node and period the watch's and the journal's stops and F; 2. nodes
sharing a physical host and their stops together against chance; 3. per session and edge every commit stop of
100 ms or more (time order) **classified**, the UDP clients that reached each edge, the twins stopped at once, the
first of two edges per pair; 4. listener
silences joined to their causes; 5. the null of every probe; 6. the source side; 7. the rules.

### 7.5 Decision rules, fixed before the run

**Each stop of 100 ms or more** (an edge's commit gap `[a, b]`, length L) is classified from that edge's sampler
lines just before `a` and just after `b`, its `/api/stalls` (the replaced instance's too) and its watch
(`stall_records.classify`), in this order. "Run delay grew" is the growth between those two lines less the
thread's own usual rate times their distance (the median rate over the ten seconds before and after): the window is
the stop plus up to two seconds.
1. **Not run.** A stopped tick of the class thread's CPU (or of scope `all`) covers L/2 or more: *the hypervisor
   did not run the vCPU*. On a node whose service could not pin every tick thread (the sampler's `tick_pinned`
   below `tick_threads`) its journal's ticks count only on the class thread's own CPU, never by `scope: all`. Or the class thread's run delay grew by L/2 or more: it was runnable and waited, and the
   record names where: *the guest kernel* (direct reclaim or compaction grew in vmstat), *another real-time thread
   on its CPU* (named from `ps -L`), else *the hypervisor or the guest kernel, not named*.
2. **Blocked in our code.** Run delay grew by under L/10, and a pass of the class loop was L/2 or more
   (`audio_loop_busy_max_us` rose to it, `audio_loop_busy_over_50ms` grew on a stop of 100 ms or less, since it
   counts passes over 50 ms, or the journal has a `class_pass` of the audio class thread's own over it). Only this
   node's counters count: `/api/mesh/local` also carries every peer's telemetry (`relay_nodes`, `nodes`, `peers`,
   `edge_services`, `streams`, `connections`), and the sampler reads only its own paths. The wait is named by the first that applies: a counter that rose to L/2 (`fanout_dispatch_max_us`:
   the hand-off; `class_compact_max_us`: the compact path; `class_send_max_us`: a send; `class_repair_max_us`: a
   window repair); page faults in the long pass itself (the journal's `class_pass` over the stop carries the
   thread's faults since its last once-a-second reading); the guest's reclaim; else "a lock or wait that no
   counter names" (fanout-stall.md items 1 to 6). The sampler's `audio_thread_minflt`/`majflt` is that
   once-a-second reading, up to a second stale: it is shown with the stop and never names the wait. With no
   voluntary switch it is "a spin or a long computation".
3. **Waited for input.** Run delay grew by under L/10 and no long pass: the thread slept in `poll()` and nothing
   came: upstream, or the kernel did not deliver (the London hop journals say which).
4. **Undetermined.** Run delay between L/10 and L/2, or the process changed inside the stop. **No sampler**: no
   line on one side.

**The rules** (section 7 of the report, one line each):
- **H1, the fix.** Per edge: a stop of 100 ms or more in S1, or not. An edge is measured when its LL-HLS probe saw
  its commits and UDP clients reached it in S1 (a UDP probe's caches, or the crowd's listeners subscribed there
  while parts came); otherwise *not measured* (a stop on an edge without a client is still a stop, said so). No
  edge stopping, and every edge measured: *no stop remains: the fan-out fix holds on a fresh deploy*.
- **H2, what each stop was, and what follows.** Over the whole run:
  - any *blocked in our code*: a fault of av-mesh, fixed at the named wait (for page faults or the allocator: the
    pooled tee buffer and the other first-use allocations of fanout-stall.md);
  - any *not run*: the instances and their `host_uuid` are listed, co-hosted edges marked; the replaced edge
    against its predecessor decides: new instance clean in S4 to S8: *the instance*, so the boot check
    (`node_check_seconds`, limits from this record) with D12's replace-once; it stops again on the same host: *a
    platform matter for the owner*, with the host ids; it stops again on another host: *not one instance but guests
    under first load* (H10 broadened): the record goes to the owner and two paths at every tier are the answer;
  - any *waited for input*: upstream or the kernel's delivery, read in the London hop journals;
  - no stop of 100 ms or more anywhere in the run: one repeat on fresh instances ($2.70), then stop; the instruments
    stay in.
- **H3, the arena warm-up.** In the first sessions of a process (S1, S4, S5): stop ms an edge on Tokyo's edges
  (without it) against London's (with it). *Matters* when without is 100 ms or more, 5 times with or more, and a
  stop without it is *blocked in our code*. Both under 100 ms: the warm-up is not what removes them.
- **H4, first touch.** In S1: stop ms an edge on the untouched edges against the touched. *Matters* when untouched is
  100 ms or more, 5 times touched or more, and an untouched stop is *not run* or blocked on faults in the pass or
  on reclaim: then `node_warm` (a warm pass of the whole guest at boot) defaults true and is part of readiness. A
  touched edge is in neither arm when its marks have no `released` line or say `unfinished`, or when `pswpin`,
  `pswpout` or a `workingset_refault_*` grew from its touch to the end of S1 (the touch then made the faults it is
  meant to rule out). Per touched edge the line says whether its balloon reports free pages (feature bit 5): there
  the host may take the given-back pages again before S1, and the arm tests the guest's own first touch, not the
  host's backing (H10).
- **H5, twins.** Every pair of stops of two edges of one region that overlap or come within 50 ms, with the time
  both were stopped, and the parts late on both of a pair. Any: *two paths do not hide every stop* (wave 9: London,
  446 ms); none: they do, in this run.
- **H6, PCM first.** After the restart: stops in S4 (PCM) against S5 (Opus). PCM stops: the first session of a
  process stops whatever its codec (time since start). Only Opus: the Opus stream and its compact path. Neither:
  a restarted process on a guest that has run a session is clean, so S1's stops (if any) are the guest's or the
  instance's.
- **H7, churn.** S3's stops that begin within 5 s after a round's start (`rounds.txt`) against the rest. At least
  one, and at least as many as elsewhere: *joins trigger stops*.
- Independent of all rules: 3.1's acceptance per component, from S6 to S8 (and the null of every probe).

The tests hold one synthetic input for each class and each rule (`scripts/compare/test_hunt_report.py`), and the
wave 9 first session (referee's files, read only) gives back w9-stall-analysis.md's 13 stops, 5,146 ms, with the
London twins stopped together for 446 ms (and 31 ms just before), new-edge-1 clean, and syd-edge-2 stopped
(168 and 152 ms) with no UDP client on it.

### 7.6 and 7.7

The route survey (bare nodes, unchanged) and the requests to the referee stand as written under "Before the wave
9 run", 7.6 and 7.7.

### 7.8 What the lab needs that is not built yet, and the departures

- **av-mesh: `--relay-class-arena-warm-mb N`** (env `NEEDLETAIL_RELAY_CLASS_ARENA_WARM_MB`), default 32, 0 for no
  warm-up: `ClientSender::new` passes `N << 20` to `warm_thread_arena` instead of `CLASS_ARENA_WARM_BYTES`, and
  `class_arena_warm_bytes` reports what was warmed (0 with 0). Until av-mesh has it the preflight refuses a real
  run (the alternative was the wave 9 binary on Tokyo's edges: it has no `/api/stalls`, no readiness for C's roll
  engine and no wire compatibility with this build, so it was not taken).
- **needletail-nodecheck `touch --hold`**: after the last page and the `touch_done` line, keep the mapping until
  SIGTERM or SIGINT, then exit 0 (rm/av-mesh-di's `touch` drops it at once). The preflight refuses without it.
  Nothing else is needed of `touch`: the hunt computes `--mb` itself (MemFree less the margin).
- **The watchdog's bounded collect**: rm/lab2 (`up --collect-timeout-s`, the watchdog running the collect command
  recorded by `up` before it deletes) and rm/arch2 (`linode-small-mesh-deploy.py collect --run-id`) merged into
  `resilient-mesh`. Without them the watchdog deletes an uncollected lab; the preflight refuses.
- **The LL-HLS probe at each edge** reads `https://<edge>:19444/live/<stream>/stream.m3u8`, the edge's media
  playlist (the path av-mesh's reader and the referee's lossless scripts use): through `master.m3u8` the reader
  takes the first pathway, so a probe addressed to one edge can be served by another. No dry run can show that the
  edge serves the Opus rendition there; if S1's per-edge probes have no rows, the report says "not measured" for
  those edges under H1.
- The dev box no longer polls `/api/stalls` and `/api/mesh/local` every 5 s: the node's own sampler does, at 1 s and
  10 s, and needs `curl` and `jq` there (the bootstrap installs `jq`; `curl` is installed if the image lacks it).
- The soak rounds, the warm arm, the wave 7 control and the bare nodes' touch programme are gone, and with them R1 to
  R6: H1 to H7 replace them. The host comparison of R1 lives in H2 (instances, `host_uuid`, the replacement).

### Before the wave 9 run

The plan of 23 Nov, as it stood before the wave 9 run; superseded by 7.0 to 7.5 and 7.8 above, except 7.6 and 7.7.

#### 7.1 The lab (`NT/deploy/low-latency/topology-hunt.json`, `NT/scripts/linode-stall-hunt.py`)

| group | nodes | plan | $/h |
| --- | --- | --- | --- |
| mesh, London | contrib-lon, lon-src, lon-view | g6-dedicated-4 | 0.324 |
| | lon-dist-a, lon-dist-b, lon-edge-1..4 | g6-dedicated-2 | 0.324 |
| mesh, Tokyo | tok-dist-a, tok-dist-b, tok-edge-1 | g6-dedicated-2 | 0.162 |
| | tok-view | g6-dedicated-4 | 0.108 |
| bare, London | lon-bare-1, -2 (memory and echo), lon-bare-3, -4 (survey senders only) | g6-dedicated-2 | 0.216 |
| bare, far | tok-bare-1, new-bare-1, syd-bare-1, ams-bare-1 (survey ends) | g6-dedicated-2 | 0.216 |
| | 21 instances | | **1.350** |

Dedicated plans only. Two billed hours planned: $2.70, plus two replacement instances of one hour ($0.054 each): $2.81.
`up --run-id hunt<date> --optional-sites sydney --watchdog-hours 1.9`; own state file
(`NEEDLETAIL_LINODE_LAB_STATE=target/linode-qualification/hunt-lab.json`), `NEEDLETAIL_LINODE_MAX_HOURLY_USD=1.50`,
`NEEDLETAIL_LINODE_MAX_NODES=24`. One repeat at most (rule R6): $5.62 in all. The budget is the owner's of 23 Nov
(about $300 for this work, about $38 spent; AGENTS.md's earlier $100), C caps the proving run at a cumulative $60;
the lab's cost is reported at the end. Binaries are the dev box's zigbuild set (av-mesh with D1 to D3, av-contrib
with S1 and S2); no build host. Bare nodes have role `load` and are never pushed to. Tokyo has the far
distributors because the worst wave 7 instances were far -b distributors at 3 % CPU.

#### 7.2 Per-node settings (`node_overrides`)

All mesh nodes: stall journal 65,536 events. Cold (nothing before the first session): lon-edge-1, lon-edge-4,
tok-edge-1, lon-dist-b, tok-dist-a, contrib-lon, and the bare nodes lon-bare-1, lon-bare-3, tok-bare-1, new-bare-1,
syd-bare-1, ams-bare-1. Warmed at boot (`nodecheck warm` before the push): lon-edge-3, lon-dist-a, tok-dist-b,
lon-bare-2, lon-bare-4 (one -a and one -b distributor, one near and one far; one survey sender in each arm). The
wave 7 control: lon-edge-2 (cold) with `malloc_tune=false`, `prefault=false`, `mlock=off` (the head build no
longer behaves as 07a9aea did: both default true, AM:2317-2329). Hop journal 2,097,152 events on
lon-edge-1, lon-edge-3, lon-dist-a, tok-dist-a, tok-edge-1; default on the others. `mlock` is not an arm: it is
judged by `locked_bytes`, `vm_lck_kb` and the class threads' fault counts, not by stops.

#### 7.3 Timeline, minutes after `up`

- 0 to 6: `LAB hosts` (`host_uuid` into the state). On every node, first command: `needletail-nodecheck watch` (it
  runs to the end; it allocates nothing). `warm` on the warmed arm. Push.
- 6 to 19: idle, as the referee's 16.5 minutes. Soak rounds at 7, 12 and 17, 60 s each: `echo` pairs inside a site
  at 2,000 a second (lon-edge-1 with -2, -3 with -4, lon-dist-a with -b, tok-dist-a with -b, lon-bare-1 with -2,
  tok-edge-1 with tok-bare-1). No memory is touched: the nodes stay cold. A node's `watch` record of the rounds is
  its soak score.
- 18: the referee's `steal-check.py`-style 20 s burn on the mesh nodes.
- 20: S1, Opus Custom 1 ms, the referee's sender command at the next 64 s boundary with `--lateness-journal`, 70 s.
  lon-view: 4 `needletail-udp-client --arrivals --write-parts`, 4 `ntcmp hls` HTTP/3 window 40, `needletail-load`
  40 listeners `--sample 8`; tok-view: 2 UDP clients, 2 `ntcmp hls`. 23: S2, PCM 1 ms (contributor restarted first,
  as the referee does). 26: S3, standard Opus 2.5 ms. After each session ends: one `/api/hops` pull.
- 20 to 33, bare nodes lon-bare-1 (cold), lon-bare-2 (warmed), tok-bare-1: `touch --mb 400 --rate-mb-s 2 --thp
  always` on fresh memory; 25: `--thp never` on a fresh 400 MB; 30: the first 400 MB freed and taken again.
- 33: `warm` on every node not yet warmed (its duration and slow pages on a 30-minute-old instance).
- 35: the edge or bare node with the most `stopped_ms` so far is replaced (`LAB remove <name> --any-lab-node`,
  `grow`, `hosts`, `push --nodes <name>`; an edge is named by its peers by VLAN address only, which the new
  instance keeps): does the new one land on another host, and is it clean in S4 to S7? If the worst instance of
  all is a distributor it is replaced at 76 instead, after the last session, and given two soak rounds only: its
  public address is compiled into other nodes' arguments, and no session may depend on re-rendering them.
- 40: S4, Opus Custom 1 ms, 70 s (the later session). 45: S5, Opus Custom 1 ms steady, 600 s. 57: S6, PCM 1 ms
  steady, 600 s. 70: S7, standard Opus 2.5 ms, 300 s.
- 82: collect; `LAB hosts` again. 88: `down` (inside the second billed hour; the watchdog is at 1.9 h).
- All through: `linode-route-survey.py` (7.6).

#### 7.4 Collected, and the report

Every 5 s from every mesh node: `/api/stalls?since_us=`, `/api/mesh/local`; from the contributor `/api/stalls` and
its pacing record. Once after each session: `/api/hops` from the enlarged-ring nodes, the probes, the sender's
journal. At the end: every `watch`, `touch` and `echo` file, `/sys/kernel/mm/transparent_hugepage/enabled`, the
balloon's presence and feature bits (`/sys/bus/virtio/drivers/virtio_balloon`), clocksource, `chronyc tracking`.
`hunt-report.py` prints:
1. per node and period (soak, each session, between): stops of 20 ms and of 100 ms or more by `scope` and cause,
   `stopped_ms` per node-minute (**F**, the measure of 7.5; the node's `/api/stalls` and its `watch` must agree
   within 10 %), `starved_ms`, steal, the guest-kernel share from the `sec` lines;
2. by `host_uuid`: which nodes share a host; stops of co-hosted nodes within 50 ms of each other against the count
   expected by chance;
3. the share of all frozen time on the worst quarter of instances; soak rank against session rank;
4. per session and listener: every arrival silence of 3 ms or more joined, in this order, to the sender's `late`
   lines, the contributor's `src_gap`, `rx_wait` and `inline_publish`, the stall events of the nodes on its path,
   the listener host's `watch`; the count and summed ms per cause and the unexplained remainder;
5. first of two edges per part (two clients on different edges, or B's two-path client);
6. the null of every probe (3.1), as scored, on the kernel stamp, and publish to kernel, with the floor;
7. E3's source table per session with the side of each episode; the 2.5 ms split of 7.7;
8. one line per rule below.

#### 7.5 Decision rules, fixed before the run

- **R1, the host (H13).** Half or more of all frozen time on a quarter or fewer of the instances, and the worst
  quarter by soak score are in the worst half by session F: the product measure is the boot check
  (`node_check_seconds` set to the shortest soak that found every such instance, limits from the record) with D12's
  replace-once. If co-hosted nodes stop together (within 50 ms, above chance), 2.10's host rule is the reason and
  is kept as written. The replacement at minute 35 says whether replacing helps; if the new instance is on the
  same host and as bad, the record goes to the owner as a platform matter with the host ids.
- **R2, first touch (H10).** On both cold bare nodes that run `touch` (lon-bare-1, tok-bare-1) F during the fresh
  touch is 5 times or more F during their re-touch and soak rounds, and lon-bare-2 (warmed) shows no such ratio;
  and the warmed mesh nodes' F in S1 is a fifth or less of the cold mesh nodes' median F in S1. Then `node_warm`
  defaults true and is part of readiness. If the first holds and the second does not, the host takes pages back
  or the trigger is load: no warm pass; the balloon's features and the re-touch timings are in the record.
- **R3, huge pages.** Stops or touches over 1 ms under `--thp always` at 5 times the rate under `--thp never` on
  the same node: D11 (`--thp off` for the services, a guest setting) and no warm pass.
- **R4, our own threads (H9).** Any `class_pass` of 20 ms or more with cause `own`: a fault of av-mesh, named by
  the event; fixed there.
- **R5, the guest kernel.** `starved` ticks in seconds that are mostly `system` or `softirq`, with `compact_stall`
  or `allocstall` growing: reported with the vmstat lines; no product change in this round unless R3 fires too.
- **R6, nothing.** F under 5 ms a node-minute on every node through S1 to S4: one repeat on fresh instances, then
  stop. The instruments stay in, so the referee's first session diagnoses itself.
- lon-edge-2 against lon-edge-1 says only whether wave 9's defaults changed the fault counts; one edge against one
  decides nothing about stops (70-fold spread, E2).
- Independent of all rules: 3.1's acceptance, per component, sent to the lead and the referee.

#### 7.6 The route survey and whether flows default on

Senders lon-bare-3, lon-bare-4 and ams-bare-1; receivers tok-bare-1, new-bare-1, syd-bare-1, and ams-bare-1 for the
London to Amsterdam leg (11 links). 64 source ports and the two default ports per link, 5 probe sets a second for
the whole lab, every probe kept (16 bytes: port, link, probe id, receive minus send in ns; about 19 MB a link).
Intervals in which an end's own `watch` shows a `stopped` tick are left out (a stopped receiver delays every port
alike and would read as a route step).
What it must show, because no file does: how many route classes exist at one instant and for how long, whether all
ports of a link move together, and how often routes step. Three policies replayed offline on the record: the
default port; the best port at minute 0, kept; base plus extra as designed (the chooser of 2.6 at 2 probes a
second, the same fixture as D7's test). Per link and policy: median and p99 of the first-arrival delay, share of
time within 0.3 ms of the best port of the moment, steps (a change of 1 ms or more lasting 5 s), time to recover.
- **Gain**: on a link, base plus extra is 1 ms or more ahead of the default port for 10 % of the time or more.
  Flows stay on by default if any long-haul link shows it; otherwise D13 and the record says no diversity was found
  to use (the code stays: it sends nothing without a second class).
- **Chooser**: its excess over the best port of the moment is 0.3 ms or less at p99 outside the 3 s after a step,
  and no step costs more than 3.0 s of excess.
- Wave 7 is not claimed as beaten: Tokyo's Opus median was 111.04 with all four mesh flows on the 109 class
  (108.69 to 108.99) and LiveKit's one flow on 109.0 to 109.1; a 105 class existed on 19 Nov and after 1795382019,
  but no file shows a port on it during the Opus configuration.
- nl-ams as a relay: no. London to Amsterdam 2.4 to 2.6 ms plus Amsterdam to Tokyo 107.1 is 109.6: 4.6 ms behind
  London's best class, 3 ms behind to Sydney, 9 ms to New York (19 Nov; the survey's Amsterdam node rechecks it),
  and a third ingress is against the fixed fan-out.

#### 7.7 The 2.5 ms Opus lane, and requests to the referee (their files, their call)

Nothing is built for the lane. In `waudio7-std` Needletail's unit median is already ahead in all four regions
(London 3.92 against 3.98, Tokyo 108.14 against 108.28, Sydney 130.87 against 130.92); the scoreboard rows are lost
to the estimate of request 1 alone. The "0.17 ms before publish" is not established: the same 1 ms lane reads
0.610 in `waudio7` and 0.437 and 0.390 half an hour later, a spread of 0.22 ms from the clock term between two
hosts. S3 and S7 therefore measure intervals on one clock only: the sender's `encode_us` and `late_first_us` (S1),
and the contributor's `rx_to_publish` p50 and p99 per stream (S2), compared between the lanes' sessions (the two
Opus lanes share stream 3001 and cannot run together). A contributor change is considered only if its share for
2.5 ms epochs is 0.05 ms or more above the 1 ms lanes'.
1. `REF/scripts/compare/compare-audio-analyse.py:153-155`: four points per unit are 0.625 ms apart for a 2.5 ms
   unit, wider than Needletail's unit spread (p10 to p90 3.84 to 4.10), so the median falls in a gap set by the 2 %
   tails. With one point per sample: London 5.20 against LiveKit's 5.27 (now 5.37 / 5.28), Tokyo 109.45 / 109.56,
   Sydney 132.22 / 132.25, New York 35.45 / 40.76; LiveKit moves 0.01 at most; p99 rises for both, ours more
   (London 6.45 to 6.57 against 6.86 to 6.95). Ask: one point per sample for every lane.
2. `REF/tools/ntcmp/audio.go:271`: LiveKit's publisher sends units encoded in advance; our sender encodes after the
   deadline, 0.11 to 0.22 ms. Ask: a live encode in `ntcmp-lk pub`; if not, the record states the asymmetry with
   the sender's measured `encode_us`.
3. Keep each node's `/api/stalls` and the output of `LAB hosts` beside the samplers of a session; note that
   LiveKit's London clean p99 of 1,457 ms in wave 7 was its SFU instance stopped for 4.7 s, the same event class.
4. The null (departure 1): steady probes of 300 s or more around a change, or a threshold calibrated on no-change
   windows of the same length; and the null scored for both systems.

## 8. Risks, what stays open, what is out of scope

- **The cause is not known.** Host, first touch and huge pages are hypotheses with rules; the lab may show stops
  that no guest measure removes. Then nothing but the instruments and the host rule ships, two paths at every tier
  are the answer, and LL-HLS players and one-path listeners keep the exposure, which the record says plainly.
- **Two hours of 20 instances is a small sample.** Wave 7 had about 1.75 stops per edge-minute in its first
  minute; a clean lab is itself news, and H2 allows one repeat only.
- **The steady state may not meet 3.1**, on Linode or in the lab. Then every change is quoted beside its null and
  the referee's rule cannot separate a failed change from noise at the measured rate; the component table says
  where the work is. The listener host's share is not ours.
- **The source is single.** A source-host stop stalls both systems; a sender stall only ours. S1 and S2 shrink
  and expose it; nothing hides it.
- **Flows.** Linode may hash only on addresses on some links, or move all ports together: probes paid, nothing
  gained (the survey says). Classes closer than 300 us are one class to the chooser, so equal-delay parallel paths
  give no measured diversity. Three seconds of excess is possible when both carrying flows turn slow.
- **The boot check can reject a good node** on thresholds taken from one lab; it is off until R1, and a rejected
  node costs one replace, not an outage.
- **Open**: the side of the 21.8 ms event and of the 2 to 21 ms class (S1, S2 decide); whether edge-VLAN repair is
  late (D9 decides D10); whether co-hosting explains simultaneous stops (the lab); the allocation the class thread
  makes per datagram for the tee (RL:1521), left as it is unless `own` passes carry faults.
- **Out of scope**: the client and the edge's client lane (B); readiness, the units' life cycle, the roll, `LAB
  replace` (C); part identity, the second contributor, deadlines on the stream's timeline (A); 4K; daw-nexus's
  sender; Linode placement groups as a way to keep twins on separate hosts (not checked: no API call was allowed
  here; `LAB hosts` with replace does the job with what exists); polling `/api/hops` in the product.

## Reviews answered (`D-hunt.critique-evidence.md`)

| # | finding | answer | why, and what changed |
| --- | --- | --- | --- |
| 1 | BLOCKER: the steady state fails the rule; no null, no number | **Accepted** | Reproduced: 144 of 704 at 8 s [R]; LiveKit's lane 147 of 676 [R]. Departure 1, 3.1, `null-window.py`, S5 to S7, the 3 ms join, summary line 1 corrected. One amendment with evidence: "2 % at 8 s" as one figure cannot be met by any system on a 60 s probe (the silence criterion's chance floor is 16 %), so the acceptance is per criterion and names the probe length |
| 2 | BLOCKER: "when" is wrong; the lab tests the wrong variable | **Accepted** | Re-read from the samplers [R]: tok-dist-b +2.33 s at 2760.8 with flat RSS, +1.02 s at 3828.6; steal 285, 127, 114; -b distributors 12 late wakes against 1. E2 has three populations, H13 is added, `LAB hosts` at both ends, grouping by `host_uuid`, the soak score, the replacement at minute 35, R1 |
| 3 | MAJOR: the source side is counted nowhere and the lab may run without its instruments | **Accepted**, one fact corrected | The contributor split reproduces (p50 0.084, max 0.567 ms; 29 episodes present at `src_first`) [R]. S1 and S2 are prerequisites of the lab; acceptance in 3.1; the sender's FIFO pacer comes first. The DAW receive is already on a real-time thread of its own (AC:1202-1216, AC:11230-11250), so the remedy is the kernel stamp and, on evidence, the recovery off that thread (S3), not a new receive thread |
| 4 | MAJOR: confounded arms, three nodes an arm, all-or-nothing criterion, no wave 7 control | **Accepted** | 7.2: warmed arm with one -a and one -b distributor, near and far; survey senders in both arms; lon-edge-2 as the wave 7 control; far distributors in Tokyo; `mlock` out of the arms; the idle-age branch dropped; first touch judged within an instance on bare nodes (fresh against re-touch) with ratios, not "none of 20 ms"; balloon and re-touch figures in the record |
| 5 | MAJOR: two edges are assumed to stop independently | **Accepted** | `command_hosts` covers contributors and edges (2.10); the host tag for a listener's second edge (seam to B); the summary states the residual |
| 6 | MAJOR: the journal misreads a one-vCPU stall and dilutes run delay | **Accepted**, one requirement moved | RL:1247 is `getrusage`, not schedstat (confirmed). One pinned tick thread per CPU, `pread` at every tick; per-CPU, per-second accounting and vmstat growth in `watch`; R3. "`class_pass` carries its CPU's tick lateness" is done when the journal is read, because the tick thread of a stopped CPU can only record after the pass ends |
| 7 | MAJOR: the collector injects first touch every minute | **Accepted** | `events` copies the whole ring whatever `since_us` is (hop_journal.rs:281-313, confirmed). One pull after each session; enlarged ring on half the mesh nodes; no polling in the product, so no change to `events` |
| 8 | MAJOR: `--mlock current` refuses, stalls or misses the memory | **Accepted** | `mlockall(MCL_CURRENT)` needs the whole virtual size within the limit; the unit is `User=needletail` without CAP_IPC_LOCK (DEP:2873, DEP:2886). `--mlock rings` with `mlock(ptr, len)` per region, regions and sizes in 2.3, the cache named as not coverable, `vm_size_kb` and `vm_lck_kb` reported, `--mlock-all` and `LimitMEMLOCK=infinity` removed |
| 9 | MAJOR: no evidence of two classes at one instant; two numbers do not follow | **Accepted**, and one defect added | The 19 Nov file is a summary of a serial probe from `relay-primary` (confirmed). All 64 ports probed all the time, no candidate window or rotation, the extra carries only with a measured second class, the survey keeps every probe and has two criteria, wave 7 is not claimed. Neither the draft nor the review saw that the tee's dispatch is keyed by the exact peer (RI:1533): admission by range on the class thread would have left parts forwarded and uncommitted; every flow address is bound to its parent's session instead (2.6) |
| 10 | MAJOR: `--encode-at ahead` is a trick; the 0.17 ms is not established | **Accepted**, taken further | Not built at all, rather than kept as a parity option: one way to do each thing, and no live sender could use it. 7.7 says the rows are lost to the four-point estimate alone; S3 and S7 measure one-clock intervals; the three lanes cannot share a session (stream 3001 is both Opus lanes), so the comparison is between sessions on single clocks |
| 11-13 | MINOR | taken | E1's two corrections and the unused ping; `echo` records which end was lost; the edge-gap table is said to mix populations; the probe carries its send stamp and the report a median (2.6); the no-regression criterion states the flows' expected cost (6) |

## Appendix: how the [R] figures were computed (stdlib Python at low priority, read only, nothing kept)

- Null windows: `CW`'s `score` (CW:73-112) re-implemented line for line; rows from `nt-uc.probe.csv` and
  `lkregion.probe.csv` of lon-, new-, tok- and syd-view in `RUNS/waudio7-pin`, `waudio7-unpin` (Opus baseline),
  `waudio7/audio.pcm.baseline.clean`, `waudio7-std` (baseline); windows `[t, t + 8 s]`, t from first arrival + 7 s
  in 1 s steps to last arrival - 1.5 s, skip 5 s. The windows overlap: rates are estimates, not independent trials;
  the build is wave 7's (07a9aea).
- Sampler lateness and steal: interval between first fields of `RUNS/<session>/samplers/<node>.<unix>.log` minus the
  file's median, late if over 0.2 s; steal is the growth of the eighth `/proc/stat` field per interval; RSS the
  last field of the `av-mesh` group.
- Contributor split: per `seq` of `EV7/contrib-hops-4001.ndjson`, `src_first` and `sent_first`; ideal instant
  `1795382080 s + (seq + 1) ms`; episodes are maximal runs more than 2 ms above the median.
