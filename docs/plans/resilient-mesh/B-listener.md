# Workstream B: a listener held on two edges, make-before-break moves, joins and last-mile loss (final specification, 23 Nov 2026)

Designs to lead decision L2. Code read at av-mesh-features `baf0449` (branch `low-latency`), needletail-features
`52c10e1`, needletail-compare `6c8c525`. Short names for files under `av-mesh-features/src`: E = `edge_udp.rs`,
S = `client_session.rs`, C = `client_compact.rs`, U = `client_udp.rs`, A = `assignment.rs`, M = `main.rs`,
L = `load.rs`, LA = `load_assign.rs`, B = `bin/needletail-udp-client.rs`, BL = `bin/needletail-load.rs`,
R = `bin/needletail-reader.rs`, FC = `fast_plane/edge_clients.rs`. `X:n` is line n of file X at that head.
CW = `needletail-compare/scripts/compare/change-window.py`; EC, LB, SR = `edge-campaign.py`, `compare/latency-bench.py`,
`compare/scale-run.py` under `needletail-features/scripts`. "Leg" = one subscription of a listener at one edge.
"Spare" = an edge at which the listener holds a token and nothing else. "r" = the listener's round trip to its region.

## 0. Departures from the lead's brief, and what the reading found

L2 stands. Six of its sentences cannot be built as written; each is given with the evidence and the nearest design
that works.

| L2 says | Evidence | What is built |
| --- | --- | --- |
| The second edge is "the first alternate" | The alternates of a playlist are in one order for all its readers (`failover_rank`, A:384-396): every listener's second leg would land on one edge | The second leg is another holder chosen by the session's weighted rendezvous, as first legs are (3.6) |
| "An edge that dies costs the listener nothing" | The rule compares a probe's window with the rest of the same probe (CW:88-92, CW:106-111), and the rest is first-of-two. Recomputed for this revision from `needletail-features/target/latency-bench/w23-opus-l64/lon-readers.uc{0,1}.arrivals.csv` (64 listeners an edge): first-of-two has a longest gap of 9.80 ms; each edge alone 12.64 and 17.60 ms, with 4 and 7 gaps over 9.80 + 1 ms in 35 s, and a p99 2.6 and 5.0 ms higher. Idle (`w23-opus-clean`): 2.22 against 4.03 and 2.23 ms | Nothing is missing or late in any region. The rule's gap and p99 clauses are claimed only where a third serving edge exists, and the time on one path after a kill is cut from 200 ms + 3 r to about 70 ms + r by a spare and a hedged `SUB` (3.8). In a region with two serving edges the claim is "no unit missing, nothing later than one path's own steady state" |
| "the client then finds a new second edge" | The entry costs 3 r, all of it on one path (above) | The replacement of a dead leg comes from the spare the listener already holds a token for (1 r); the entry is asked afterwards for a new spare |
| "The first SUB of a join goes to both edges (first OK wins both ways)" | A `SUB` with a token opens the playlist's demand at admission (E:2346-2350) and the edge then pulls and sends the stream; for 4K that is a second 247 Mbit/s feed held 30 s (M:2754) | Two-path audio: as stated. A one-leg session (4K, `--paths 1`): the tokenless `SUB` goes to both, the first `TOKEN` wins, the `SUB` with the token goes to one |
| Warm standby is a mode of one-path listeners, at "the alternate", "on a short silence" | The alternate is shared (above), so a dead edge's listeners would all hedge onto one edge; `WAIT` goes every 80 ms, sized so that one lost `WAIT` moves nobody (E:221-224) | One mechanism for both modes: the spare. A lone leg is hedged after 170 ms (two keepalives and 10 ms), a leg beside a delivering leg after 50 ms |
| "the edge keeps a subscriber it has asked to move until its UNSUB or a grace period" | A subscription that stops refreshing ends by itself after 10 s (`SUBSCRIPTION_TTL`, U:94); a new copy at an edge takes up to 3.2 s to clear its warm-up and `ADMIT_WAIT` is 4 s (E:51) | As stated, with the grace at 10 s and no release timer in the client for a drain: only the edge's grace cuts a leg that still delivers |

The reader (`needletail-reader`, LL-HLS) stays on one edge; "where it applies" is a faster move off a vanished host (3.10).

Found in the code, and fixed below:

- **The "long-burst NACK tail" is the edge's token expiring.** A token is a hash of (peer address, minute), valid in
  its minute and the next (`Tokens::valid`, E:1432-1435). The client keeps its first token and learns of a new one
  only when a 2 s refresh `SUB` is answered `TOKEN` (E:1585-1604, S:657-680). From the start of the second minute
  after issue until that refresh, every `NACK` (E:1727-1730) and `UNSUB` (E:1701-1704) is dropped. A hole is given
  up after 12 NACKs or 1 s (C:949-950, C:1791). In `target/latency-bench/w19-b1-ge` the token died at Unix
  1795436580 and parts 100143, 100214, 100215 and 100219 are the missing ones; the protocol review reproduced it
  from the arrivals and the cost review checked the code path. (2.6)
- **The compact plan flaps under burst loss**: 449 to 480 plan changes in 200 s, 64 to 98 in 40 s on the w23
  binaries; each is an immediate `SUB` (S:517-521) and a regroup of the fast lane (E:1616-1644). (2.7)
- **A refresh that names a part does not resend what the fast lane sent.** The refresh branch rewinds `next_part`
  (E:1624-1628), but the cache-fed lane passes over every part the fast lane claimed for the subscriber
  (`claim_cache`, E:2718-2725, FC:257). Asking a live edge again "from the hole" therefore needs edge rule 8 (2.3).
- **The second copy of every assignment confirmation is counted as refused** (the client sends it twice, B:576-581;
  `confirm_udp_assignment` returns false for a session already remembered, M:20601; E:1798-1806 books that as
  `assignment_confirms_refused`). A check "refused did not rise" cannot be written without a counter of its own (2.3).
- **The referee's client is not treated as a mover**: it sends `current=<edge it left>` (B:1140), and
  `moving` needs `current` absent (M:20399). (2.5)

## 1. Summary

1. A UDP listener of an audio stream holds two legs on two edges of its region. Both edges send every part; the
   client delivers the first arrival of each part number. Default `--paths 2` on `needletail-udp-client` and
   `needletail-load`; `--paths 1` is today's listener with the new moves.
2. Every listener also holds a spare: a token at a third edge, chosen for its session, no state at that edge. A leg
   that delivers nothing for 50 ms while the other delivers gets a hedged `SUB` at the spare. An edge kill leaves a
   two-path listener on one path for about 70 ms + r and never without audio. A one-path listener is late by
   190 ms + r instead of 200 ms + 3 r.
3. Every planned move is make-before-break on both sides, for one-path listeners too. The edge marks a subscriber
   it has sent `MOVE` as leaving and serves it until its `UNSUB` or 10 s. The client subscribes at the edge its
   entry names and sends `UNSUB` when the new leg is level with the old. A rebalance that finds no level edge
   returns the listener to the edge it was on.
4. Bytes per listener, both edges together: clean, one datagram a part from each edge and nothing else (1.93 times
   today); 1 % and 5 % random loss, the same two datagrams (0.93 to 0.96 times today, the second leg is the copy
   today's plan sends); bursts, two datagrams plus window repair rows from one edge only (about 1.3 times today).
5. The assignment names the legs and the spare. A redundant leg is given only where it keeps the edge under its
   rebalance target (800 permille), and an overloaded edge refuses one. Streams over 8 Mbit/s get one leg. A leg
   is a subscription in every count: listeners per edge halve.
6. A join asks one entry, subscribes at both edges at once and plays the first part of either. An edge that sends
   no `TOKEN` in 400 ms is passed over (5 s today).
7. Operator: drain end is still `client_udp.subscribers == 0`, reached about 3 s + 3.5 r after the drain command,
   at most 13 s. SIGTERM is a drain (seam with C; stop wait 14 s). New gauges: `leaving`, `moves_completed`,
   `moves_expired`, `single_legs`, `legs_by_other`.
8. `needletail-load` runs the same session and edge-choosing code as the client, asks over UDP as the client does,
   and reports per interval the gap histogram, listeners interrupted, listeners with missing parts, time on one
   path and latency buckets.
9. Residual loss: tokens renewed before they expire, holes NACKed until the edge's ring has passed them (3 s), plan
   changes held for 1 s, a new one-leg session planned for 2 % loss until it has measured.
10. Not hitless, stated: two edges killed in the same 200 ms for listeners with both legs on them (late by
    220 ms + r, nothing lost); a region with fewer than three serving edges (gap and p99 not claimed after a kill);
    4K (one leg, RaptorQ, make-before-break at the new edge's first object).

## 2. Wire, CLI, API and file-format changes

### 2.1 NTU/1 control datagrams (U)

No new verb.

| datagram | change |
| --- | --- |
| `SUB <stream> <from\|-> <permille> <min> <ppm> <token\|- padding> [F1] [C..] [o=<node>[,<node>]] [r=1]` | `o=`: the nodes of the listener's other legs that deliver now (at most two). Its presence marks a redundant leg. Sent on every `SUB` of the leg, refreshes included, and at once when the set changes. `r=1` on a refresh that names a part: send again from that part (edge rule 8); the client sends it only when every leg went silent |
| `ASSIGN <stream> <nonce> <sid> <skip,..\|-> <current,..\|-> [p=<0\|1\|2>] [s=<0\|1>] [h=<node,..>] [t=<token>] [padding]` | `p=` legs wanted (absent: 1). `s=1` a spare wanted (absent: 0). `h=` the legs the client holds and keeps: never in the answer, never counted as edges it left. A request (no `t=`) carries `current` `-` (2.5). In the confirmation (`t=`) `current` lists every leg the listener holds after the change, one or two nodes |
| `ASSIGNED <stream> <nonce> <port> <node\|url\|0/1>... [p=<n>] [s=<0\|1>]` | The first `p` caches are the legs granted (fewer than asked when the playlist is over the two-path bit rate or no edge passes the gate of 3.6), the next is the spare when `s=1`, then the alternates and the failover entries as today. Absent: `p=1`, `s=0`. The cut that keeps the answer no longer than the request (E:1846-1856) never removes the first `p + s` caches |
| `MOVE <stream> <reason>` | Same text. At the edge the subscriber now stays in the table, marked leaving (2.3). The edge repeats it every 250 ms and answers a leaving subscriber's plain refresh with it |
| every verb | Forward-compatibility rule from this build on: new optional fields are words `<1 to 8 lowercase letters>=<value>` at the end. `Control::decode` skips those it does not know instead of refusing the datagram (U:627-629 refuses any extra word today); in the two verbs that read a list to the end (`NACK` parts, `ASSIGNED` caches, U:593-617) a word of that form ends the list |

`Control::Subscribe` gains `others: Vec<String>` and `resume: bool`. `Control::Assign` gains `paths: u8`, `spare: bool`,
`hold: Vec<String>`; `current: Option<String>` becomes `Vec<String>`. `Control::Assigned` gains `paths: u8`,
`spare: bool`. Defaults are not written (`p=1`, `s=0`, empty `h=`, empty `o=`).

### 2.2 HTTPS assignment (`assignment_api_response`, M:21149)

`GET /api/assignment?stream=&sid=[&skip=a,b][&hold=c,d][&paths=0|1|2][&spare=0|1]`. `current=` is still read for
LL-HLS stickiness; a `current` that is also in `skip` is ignored (2.5). The answer gains `"paths": <legs granted>`
and `"spare": "<node>" | null` at the top level; in `assigned` the second leg has `"reason": "second_path"` and the
spare `"reason": "spare"` (new `AssignmentReason::SecondPath`, `::Spare`). The `client_udp` block gains `leaving`,
`moves_completed`, `moves_expired`, `moves_taken_back`, `move_grace_ms`, `redundant_refusals`, `single_legs`,
`legs_by_other` and `assignment_confirms_limited` (2.3).

### 2.3 Edge (`av-mesh`): flags, state, rules

Flags (each with its `NEEDLETAIL_...` environment name by the usual rule):

| flag | default | meaning |
| --- | --- | --- |
| `--client-udp-move-grace-ms` | 10000 | How long a subscriber that was sent `MOVE` is still served. 0: removed at the `MOVE`, as before (A/B only). `EdgeUdpConfig::move_grace` |
| `--client-udp-assign-confirms-per-source` | 64 | `CONFIRMS_PER_SOURCE` (E:1887) as a setting. Labs that run `needletail-load` render 4096: a load host confirms for every listener from one address |
| `--assignment-two-path-max-bps` | 8000000 | A playlist whose regional bit rate (`regional_playlist_bps`, M:20359) is above this is given one leg whatever `p=` asks. 1 ms PCM is 2.6 Mbit/s a leg |
| `--client-udp-rebalance-to-mean` | true | An edge also sheds when it is over the region's mean load by half the rebalance margin (rule R3). false: only over the target, as before |

Unchanged: `--client-udp-keepalive-ms` (80), `--client-udp-drain-spread-ms` (2000), the token scheme, the NACK
budgets, the playout floor, `--rebalance-target-permille` (800), the margin (150), the interval (3000 ms).

State. `Subscriber` (E:1228) gains `leaving: Option<Leaving>` (`since: Instant`, `reason: &'static str`,
`reminded_at: Instant`) and `others: [u64; 2]` (tags of the nodes in `o=`: `assignment::node_tag`, FNV-1a of the
node id, never 0; 0 = none). Nothing else per listener: a leg is one table entry. `EdgeUdpStats` gains the counters
`moves_completed`, `moves_expired`, `moves_taken_back`, `redundant_refusals`, `assignment_confirms_limited`, and the
gauges `live_leaving()` and `single_legs()` (subscribers with no `o=`) beside `live_subscribers()` (E:388).
`ClientUdpGauges` (M:14032, M:19840) carries them, with `legs_by_other`: an object from node id to the number of
subscribers that name it in `o=` (tags matched against the region's advertised node ids; `""` for none).

Before any rule lands, `run_edge_udp`'s match arms (E:1573-1876) become the functions `on_subscribe`,
`on_unsubscribe`, `on_nack`, `on_assign`, and the housekeeping block of `run_stream_sender` (E:3112-3292) becomes
`housekeeping_moves`: no behaviour change, so that the commits below touch different functions.

Rules for a leaving subscriber:

1. `take_movable_aged` (E:3841) becomes `mark_movable_aged(entry, hold_down, min_age, now, limit, reason, avoid)`:
   it sets `leaving` on the subscribers it chooses and returns them; it removes nothing. Both callers (drain
   E:3143, rebalance E:3239) send `MOVE` to the returned peers as today. A drain also takes subscribers that are
   leaving for `rebalance`: the reason becomes `draining` and `since` starts again. A rebalance never takes a
   leaving subscriber, nor one whose `others` cover every receiver with room (`avoid`, rule R1).
   `DrainPace::initial` (E:3138) counts the subscribers not yet leaving for the drain.
2. The stream sender no longer returns when `kept == 0` (E:3199, E:3284). It serves leaving subscribers like any
   other (parts, rows, NACK answers, fast lane) until the table entry is empty (E:3070-3081 ends it).
3. While a stream has a leaving subscriber its housekeeping runs every 250 ms (`LEAVING_TICK`): one whose
   `since` is older than `move_grace` is removed (`moves_expired`, `fast_lane_changed`); one whose `reminded_at`
   is older than 250 ms is sent `MOVE <its reason>` again.
4. `UNSUB` removes at once (E:1700, unchanged). From a leaving subscriber it counts `moves_completed`, and
   `rebalance_moves` when the reason was `rebalance` (today that is counted at the `MOVE`, E:3263).
5. A `SUB` with a valid token from a leaving subscriber (the refresh branch, E:1608-1659). Without a part: answered
   `MOVE <reason>`; `expires_at` is never set past `since + move_grace`. With a part it is the client coming back:
   the mark is cleared when the reason no longer holds, that is `rebalance` whenever `move_reason` (E:2118) is
   `None`, and `parents-lost` within `--client-udp-move-hold-down-ms` of the mark (the take-back that `admit`
   gives today, E:2299-2312). Then `admitted_at = now` (a rebalance leaves it for `REBALANCE_MIN_AGE`, 10 s),
   `moves_taken_back` is counted, `RebalanceState::returned` is called for a `rebalance` mark (rule R2) and `OK`
   is sent. Otherwise `MOVE` again.
6. `run_edge_udp` at shutdown (E:1517-1522) sends `MOVE draining` once to every subscriber before it clears the
   table.
7. `plan_rebalance` (M:19471) uses `live_subscribers() - live_leaving()` as `own.subscribers`; the advertised
   `udp_subscribers` (M:19676) stays the whole table: leaving legs are still load.

Rules for every subscriber:

8. A refresh that carries `r=1` and names a part older than `next_part` and inside the window is a resume: beside
   the rewind of E:1624-1628 it sets `catching_up` (so `hold_to_playout`, E:3454, does not cut the backfill),
   lowers `first_part` to the part (so its NACKs are answered, E:1977), and replaces `fast` with
   `FastClient::new(part)` with a regroup, so that the cache-fed lane sends again what the fast lane had claimed.
   It applies to a leaving subscriber too, whatever rule 5 answers. A refresh that names a part without `r=1`
   (a repeated first request, a take-back) keeps today's rewind and nothing more.
9. A new subscription whose `SUB` carries `o=` is refused `edge-busy` while the edge is overloaded, whatever
   `from_part` (E:2330 exempts every `SUB` that names a part today); counted in `redundant_refusals`. A `SUB`
   without `o=` that names a part is still taken: that listener has nothing else.
10. The part sent with the `OK` (`first_part_at_once`, E:2477) goes in the long form: the client's time anchor
    (C:1519-1523) is then there with the first part (seam with A, section 5).

Rebalance:

- R1. `plan_rebalance` passes the tags of the receivers that have room (`high < target - margin`, E:3606) to
  `mark_movable_aged` as `avoid`: a subscriber is passed over when every such receiver is in its `others`.
- R2. `RebalanceState::returned(now)` takes one listener out of the newest `shed` entry and out of `check.sent`
  (E:3737-3749) and gives the share back to the round (`round.sent -= 1`), so the next tick offers it to another
  subscriber. A round of which more than half came back sets `paused_until` as an `Ineffective` verdict does
  (E:3797) and counts `rebalance_ineffective`.
- R3. With `--client-udp-rebalance-to-mean`, `rebalance_quota` (E:3576) uses
  `target' = min(target, mean + margin / 2)` in place of `target`, where `mean` is the mean `load_permille` of
  this edge and the receivers' list. Receivers and their fill follow from `target'` as today. Three edges at
  720 / 720 / 0 settle at 555 / 555 / 330 in one round; without it a restarted or joined edge takes no leg of a
  listener that already has two.

Assignment confirmations (`on_assign`, E:1778-1810): the limit is `config.assign_confirms_per_source`; a
confirmation turned away by it or by `MAX_ASSIGNMENTS_IN_FLIGHT` counts `assignment_confirms_limited`.
`assignment_confirms_refused` keeps its meaning (bad token, already charged).

Drain end is unchanged in meaning: `client_udp.draining && subscribers == 0 && hls_sessions == 0`.

### 2.4 Client and load tool flags (same names and defaults on `needletail-udp-client` and `needletail-load`)

| flag | default | meaning |
| --- | --- | --- |
| `--paths <1\|2>` | 2 | Legs held on a stream the entry grants two for |
| `--spare <bool>` | true | Hold a spare and hedge to it (3.8). false: a dead leg is replaced through the entry |
| `--entry URL[,URL...]` (repeatable) | required | Entry points. B:72 is one value today, BL:58 already a list |
| `--token-ms` | 400 | A leg with no `TOKEN` after this (or four round trips to its entry, if longer) is passed over: another edge is asked while it goes on trying |
| `--answer-ms` | 5000 | A leg with no `OK` this long after its first request has failed (unchanged value) |
| `--silence-ms` | 200 | A leg with no datagram for this long has failed; with no media for this long while another leg delivers, likewise |
| `--hedge-after-ms` | 50 | A leg with no media for this long while another leg delivers is hedged at the spare |
| `--lone-hedge-after-ms` | 170 | The only leg with no datagram at all for this long is hedged at the spare |
| `--rebalance-giveup-ms` | 3000 | After `MOVE rebalance`: a replacement not level by then is released and the old edge asked to take the listener back |
| `--level-wait-ms` | 1000 | After any other `MOVE`: a replacement that has delivered steadily for this long takes over though it is not level |
| `--first-double-ms` | 3 | A leg's first tokenless `SUB` and its first `SUB` with the token are each sent again this soon. 0: off. `ASSIGN` is never doubled |
| `--compact-residual-ppm` | 8000 | `RESIDUAL_TARGET` (C:132) as a setting; the value is today's |
| `--hole-give-up-ms` | 3000 | `HOLE_GIVE_UP` (C:949; 1000 today) |

Unchanged: `--assignment-udp-port`, `--assignment-https-after-ms` (50), `--assignment-timeout-ms` (1000),
`--first-retry-ms` (25), the NACK timing flags, `--move-from-hole`, `--silence-repeat-ms` (1000). Removed behaviour:
the media playlist is no longer fetched beside the subscription (B:1219-1229, B:1329-1347); it is read only with
`--playlist-first` or `--verify-http`. The load tool never did it, the token wait finds a dead edge sooner, and it
costs each edge a TCP and TLS handshake per join and per move.

`needletail-load` only: `--interrupt-ms` 30; `--settle-ms` 3500 (replaces the default of `--settle-parts` 1500,
L:673, set just past the old 1 s give-up; `--settle-parts` still overrides); `--interval-buckets total|edges|off`
(default `total`). `--entry` with no `--edges` is the normal mode; `--edges` alone stays for in-process tests (two
different edges by rendezvous of the sid, no spare).

### 2.5 The `current=` bug

Both sides. The UDP clients never send `current` in a request, only in the confirmation. `assign_reader` drops a
`current` that is in `skip` and computes `moving = reader.udp && !reader.skip.is_empty()` (M:20399). Test in M
beside `a_udp_assign_is_answered_with_the_assignment_of_the_https_api` (M:35190).

### 2.6 Tokens

Client side only; works against any edge build. Each leg records `token_at`. At a refresh, when
`now - token_at >= 45 s`, the leg also sends a tokenless padded `SUB`; the edge answers `TOKEN` and touches
nothing (E:1585-1604); the client stores it (the `Token` branch, S:657-680, already repeats the `SUB` when the token
changed). A token is valid at least 60 s from issue, so a leg never holds a dead one. The spare's token is renewed
every 15 s the same way; that renewal is also the spare's liveness probe (3.8). `Tokens` gets
`valid_at(peer, token, minute)` so a unit test can step the minute.

### 2.7 Loss plan, holes, plan changes (C, S)

- `RESIDUAL_TARGET` stays 0.008 and becomes `SessionConfig::residual_target`, a parameter of
  `CompactPlan::for_measured_with` (C:214).
- `HOLE_GIVE_UP` 1 s -> 3 s; a hole is also given up once it is 3,072 parts behind the newest (the edge's ring is
  4,096, E:83). `MAX_NACKS_PER_HOLE` (12, C:950) no longer abandons: after the 12th NACK the hole is asked for
  every 250 ms until the give-up. `MAX_HOLE_RUN` (256, C:952) is unchanged.
- A new session with one leg plans for an unmeasured path as the RaptorQ repair already does (`UNMEASURED_LOSS`
  2 %, S:273-278): `for_measured_with(0.02, 1.0, ..)`, which is `C1.16.2`, until its second loss sample (300 ms).
  Today it starts on `C0.32.1` (S:296). A session with two legs starts on `C0.32.0` each.
- A plan that asks for less (fewer copies or rows) is sent only after the estimate has asked for it for 1 s; one
  that asks for more goes at once. A path counts as bursty from a mean burst of 2.0 and stops below 1.5.
- Plans for two legs: 3.3.

### 2.8 Client report and arrivals file

Report schema `needletail.udp-client.v2`. Every v1 field keeps its name and meaning (`ntcmp udp-ingest --client`
embeds the file whole and reads none of it, needletail-compare/tools/ntcmp/udp.go:63-66). `expected_parts` counts
from the session floor (3.1). `moves` keeps one record per leg replacement, so latency-bench reads on. New:

- `paths`, `spare` (node or null), `cpu_seconds` (the process's user and system time);
- `legs`: per node `first_part`, `parts_first` (first arrivals), `duplicates`, `datagrams`, `wire_bytes`, `loss`,
  `rtt_ms`, `lag_ms` (median and p99 behind the first arrival), `longest_gap_ms` (between its own parts), `plan`;
- `replacements`: `{at_ms, from, to, reason, kind, handover_ms | exposure_ms}` with `kind` `handover` (planned),
  `hedge` (from the spare beside a live leg), `replace` (through the entry beside a live leg), `interrupt` (no leg
  delivered), `takeback`;
- `single_path_ms` (total) and `single_path_spans` (`[from_ms, to_ms]`: a two-path session's time in which fewer
  than two legs brought a part within the last max(20 ms, 8 part intervals)); `hedges`, `hedges_released` (the
  silent leg came back);
- `longest_silence_ms`, `interruptions` (gaps of 30 ms or more), `wire_bytes_per_part`;
- `before_start` (datagrams of parts below the floor), `stale_start` (a leg failed for a first part 4,096 behind),
  `cross_leg_mismatch`.

`--arrivals` CSV: a seventh column `edge` (the node whose datagram was the first arrival) after `kernel_unix_us`.
`ntcmp udp-ingest` reads the first three columns (udp.go:83-90).

### 2.9 Load tool output

`intervals.jsonl` schema `needletail.load.interval.v2`, `summary.json` `needletail.load.v2`. Every v1 field stays.
`moves`, `moves_out`, `moves_by_reason` and `moves_in_by_reason` count leg replacements, so `move_share_within` and
`balance_after_max_over_mean` keep their meaning in legs. Per edge, counters are booked on the edge whose datagram
was the first arrival (`parts`, `latency_ms`) or that sent the datagram (`datagrams`, `bytes_in`, `duplicates`);
`missing`, `gaps` and the gap figures on the listener's longest-held leg. New fields, in `total` and per edge:

| field | meaning |
| --- | --- |
| `legs` (gauge, per edge) | live legs on this edge |
| `paths` (gauge, total) | `{"two": n, "one": n, "none": n}` listeners by delivering legs |
| `part_gap_ms` | histogram (with `buckets_us`) of the time between consecutive first arrivals at one listener, kernel receive times |
| `longest_silence_ms` | the longest such gap that ended in the interval |
| `silent_now_max_ms` (gauge) | the longest silence still open |
| `interruptions`, `listeners_interrupted` | gaps (or open silences) of `--interrupt-ms` or more, and the listeners that had one (a silence counts once) |
| `listeners_with_missing` | listeners whose first missing part settled in the interval |
| `latency_ms.buckets_us` | the interval's latency histogram, so a window of intervals merges into a p99 |
| `leg_replacements` | by reason (`silence-200ms`, `behind-200ms`, `draining`, `rebalance`, `no-token`, `edge-busy`...) and by kind (2.8) |
| `handover_ms`, `exposure_ms` | histograms: `MOVE` to `UNSUB`; a leg's last media to the first part of its replacement |
| `single_path_ms` | histogram of the single-path spans that ended in the interval |
| `hedges`, `hedges_released` | hedged `SUB`s sent, and those whose silent leg came back |
| `time_to_move_ms` | kept, recorded only when a listener had no delivering leg |

The summary adds the run's distinct `listeners_interrupted` and `listeners_with_missing`, `longest_silence_ms`,
`bytes_per_listener_second`, and `worst_listeners`: the 20 worst by longest silence and by missing parts with sid,
legs and time. Latency is still publication to arrival; the gap figures are what shows a pause upstream of
publication.

## 3. State machines and timing

### 3.1 The session: legs

`UdpSession` holds up to four legs (two wanted, a replacement or a hedge each). A leg has what S:247-267 calls "the
edge in use": target, node, token, `token_at`, `from_part`, request timers, round trip, `last_datagram`,
`last_media`, its own loss, burst and plan. Shared: the receiver's delivered set, the holes, the part interval.

```
add_leg ──► Asking ──TOKEN──► Tokened ──OK + first media──► Live ──MOVE──► Leaving ──► released (UNSUB)
              │                  │                            │                │
     no TOKEN in token wait   no OK in 5 s               hedged / dead     taken back (rebalance)
     (passed over, goes on)   REFUSED, MOVE                    │           or cut by the edge's grace
              └──────────────────┴──────────► Failed (UNSUB if a token is held; node skipped, 3.5)
```

- **Attribution.** `on_datagram` gives a datagram to the leg whose target is exactly `peer`; failing that, to the
  only leg with that IP (S:651 compares the IP alone today, which cannot tell two edges on one host apart). A
  `TOKEN` from an entry's or the spare's address goes to the director. A datagram from an edge released in the
  last 10 s is answered with `UNSUB` (at most one per 100 ms); anything else is ignored.
- **Floor.** The session floor F is the first part delivered by any leg. A datagram of a part below F is counted
  (`before_start`) and dropped: never delivered, never a hole. Expected parts run from F. So when the two legs of
  a join start a part or two apart (each edge starts at its own head, E:2416), first audio is the faster leg's
  first part, which is also the lowest number in the arrivals file (the referee reads first audio there).
- **Numbering per leg.** The 16 bits of a short-form part (C:1489-1507) and the 32 bits of a long one (C:1378) are
  extended around the leg's own last part, else the part its `OK` named; never around another leg's.
- **A leg that starts stale.** A leg whose `OK` names a part more than 4,096 behind the session's newest is failed
  at once (`stale_start`; an edge hands out a stale head after its 4 s wait, E:2157-2161). If the session's only
  parts so far came from a leg that another leg shows to be more than 4,096 behind, that leg is failed and the
  floor moves to the other leg's first part.
- **Dedup.** The compact receiver's `delivered` set (C:1033, 4,096 parts) is shared: the first arrival of a part
  number is delivered, the others count as that leg's duplicates (C:1627-1629). A part that came as a gap from one
  edge and as the part from the other is delivered as the part (C:1615-1621). RaptorQ objects pass the same set
  (`delivered_elsewhere`, C:1370, U:1147-1156).
- **Per leg in the compact receiver**: the loss counts (`counts`, `window_counts`, C:1038-1039), the burst run,
  `last_copies`, the leg's highest part, its lag, and its windows (C:1035). A repair row enters only the window
  state of the leg it came from, so rows from two edges never meet in one decode. The parts' canonical bytes
  (`known`, C:1031) stay shared, with two rules: the bytes of the repair leg (3.3) replace another leg's, never the
  reverse (today the last writer wins, C:1383, C:1626); and a window whose recovered part fails its length, CRC or
  number (C:1730-1739) is kept, not removed (C:1727), and tried again when the repair leg's own bytes or a NACK
  answer come. A failed check on bytes of another leg counts `cross_leg_mismatch`: the listener-side alarm for
  L1's byte identity. A new source generation (C:1344-1366) resets every per-leg structure too.
- **Per leg in `ObjectReceiver`**: the four RaptorQ decoders and block counts (U:913-918), created on first use.
  Two edges' symbols of a part differ (test U:1923), so they never share a decoder. `new_flow` (U:982) is no longer
  called on a move, only for a new source epoch.
- **Round trip per leg.** Sampled from: the first request of each kind to its first answer while only the 3 ms
  double has gone (at most 3 ms high; today a repeated request gives no sample, S:665-669, and with the double that
  would be every request); each 2 s refresh to its `OK` or `MOVE` when no other request is outstanding on the leg;
  the answer to the only NACK of a hole, by its source address (C:1571-1580). `nack_retry` (C:1310) and
  `unanswered_wait` (S:350) use the leg's own value. The round trip to an entry is sampled from an unrepeated
  `ASSIGN`.
- **Lag, level, steady.** The receiver keeps, for the last 64 delivered parts, the first arrival and each leg's
  arrival. A leg's lag on a part is its arrival less the first arrival (0 when it was first). Over the session's
  last 32 delivered parts: N is **steady** when it brought at least 28 of them; N is **level with O** when it is
  steady and its median lag is at most O's median lag + 250 us (when O brought fewer than 16 of them, steady is
  enough).
- **Holes and NACK.** Holes stay shared. A hole shown by a later part is first asked for after
  max(250 us, the largest lag of a delivering leg + 250 us), at most 20 ms: the slower leg may still bring it. The
  first NACK goes to the eligible leg with the shortest round trip; retries go to every eligible leg. Eligible: a
  live or leaving leg whose first part is at or before the hole (E:1977), with its own token. The client's rate cap
  (200 parts a second) counts parts, not datagrams.
- **Refresh and keepalive.** Each leg refreshes every 2 s (`REFRESH`, U:92) with its own plan word, token and
  `o=`. A leaving leg keeps refreshing (the answer is `MOVE`).
- **Loss per leg, and jointly.** Each leg measures its datagram loss and burst as today, from its own arrivals.
  Every 150 ms the receiver also gives the joint figures: the share of parts that no leg delivered as an original
  or a copy (they needed a row or a NACK), and the mean run of such parts.
- **Silence is judged on an empty socket.** No silence, hedge or dead verdict is taken while datagrams are queued
  on the socket (`session.queued`, B:1380-1381; in the load tool, before the `recvmmsg` batch is consumed): today
  `poll` runs before the receive (B:1382), so a client host that stalls 200 ms sees every leg silent.

### 3.2 When a leg is suspect, hedged, dead

| condition | threshold | action |
| --- | --- | --- |
| suspect | another leg has delivered 8 parts newer than this leg's newest, and this leg no media for max(20 ms, 8 part intervals) | the plan is computed without it at once (3.3) |
| hedged | no media for `--hedge-after-ms` 50 while another leg delivered a first arrival in the last 20 ms | hedged `SUB` at the spare (3.8); the leg stays |
| dead | no datagram for `--silence-ms` 200 while another leg has sent one in that time; or no media for 200 ms while another leg delivers | `Failed`: `UNSUB` (in case it only stalled), node skipped 30 s; the hedge leg has its place |
| no token | `--token-ms` | passed over: node skipped 3 s, another edge asked; the leg goes on asking until `--answer-ms` and is used if it answers first |
| lone leg silent | the only leg, no datagram for `--lone-hedge-after-ms` 170 | hedged `SUB` at the spare from the oldest hole; then the next row at 200 ms |
| every leg silent | no leg (one or several) has sent a datagram for 200 ms | the silence says nothing about any one edge: no leg is failed and none is skipped. Each is asked again from the oldest hole (`SUB <part> <token> r=1`, edge rule 8) and the spare is hedged if it was not. With no spare (`--spare false`, none granted), or when nothing has delivered `--token-ms` later, the entry is asked at once with the silent legs in `skip` on the wire only. A leg that does not answer in `--token-ms` is then failed (skipped 30 s) and the hedge or new leg has its place; one that answers stays, and a hedge leg beside it is released once it is steady for `--level-wait-ms`. Repeated every `--silence-repeat-ms` |
| the stream is quiet | every leg sends `WAIT` | nobody moves, nobody hedges |

### 3.3 What each edge sends

Counted legs: live, not suspect, not leaving. The **repair leg** is the counted leg that has been live longest; it
alone is asked for rows. `p_i` = a leg's smoothed datagram loss, `planned_i = min(1.5 p_i + 0.002, 0.3)` as today
(C:218); `j` = the smoothed joint part loss of 3.1.

| path | repair leg | other leg | datagrams a part |
| --- | --- | --- | --- |
| clean (every `p_i` < 0.2 %) | `C0.32.0` | `C0.32.0` | 2 |
| random loss | `C<c>.16.<w>` | `C<c'>.16.0` | 2 + c + c' + w/16 |
| bursty: a leg's mean burst >= 2, or `j` > 4 `p_1 p_2` + 0.001 | `C0.16.<rows>`, rows = min(8, 1 + ceil(16 min(1.5 j + 0.002, 0.3)) + min(ceil(joint burst) - 1, 4)) | `C0.16.0` | 2 + rows/16 |
| one leg counted | `for_measured_with` as today | - | as today |

Random loss: copies are added, one at a time on the leg with less loss, while `planned_1^(1+c) planned_2^(1+c')`
is above the residual target. At 8,000 ppm that is no copy up to 5.8 % loss on both legs (1 %: 0.03 % of parts
left to rows and NACK; 5 %: 0.59 % planned, 0.25 % true). `w` is 1 when the repair leg's round trip is at least 8
part intervals, else 0: a row arrives on average 8 parts after the loss, a NACK answer one round trip after it, so
a row only helps a listener farther than that. The second condition of the bursty row is the guard against legs
that lose together (one access link carries both): the product is then no estimate, and copies would not help
either, so the plan goes to rows on the measured joint loss.

Why this is the cheapest plan that keeps either edge's loss invisible: when an edge dies its parts in flight die
with it, so each edge must already have sent every part; two datagrams a part is the floor, and the clean plan is
exactly that. Rows on a clean two-path listener would repair only a part lost on both legs, which NACK covers.
Under random loss the second leg is the copy today's plan sends anyway. Under burst loss it is not: both edges'
datagrams of a part cross the listener's access link within a fraction of a millisecond and one burst takes both,
as it takes a back-to-back copy (the reason today's burst plan has no copies, C:199-206), so rows are still
needed, from one edge.

Bytes on the wire per part and listener, both edges together, IPv4 and UDP included. D = 35 + P (a part in the
short form), R = 56 + P (a row). P = 56 B (1 ms Opus Custom), 288 B (1 ms PCM, `payload_bytes / received_parts` of
every w23 report), 96 B (2.5 ms Opus). "Today" clean and loss figures are measured on the w23 binaries; two-path
figures are arithmetic on the plans, to be confirmed by step 1 of section 6. NACK answers are not in the plan
figures (under gemodel they are what lifts today's measured 161 to 166 B above the plan's 126 to 140).

| lane | condition | today, one path | two paths | ratio | `--paths 1` after this work |
| --- | --- | --- | --- | --- | --- |
| Opus 1 ms | clean | `C0.32.1` 94.5 | 182 | 1.93 | 94.5 |
| | 1 % and 5 % | `C1.16.2` 196 (186 to 198 measured) | 182 (189 from r = 8 ms) | 0.93 to 0.96 | 196 |
| | gemodel 1 %/20 % | 161 to 166 measured | 210 + NACK answers | about 1.3 | 126 to 140 + NACK answers |
| PCM 1 ms | clean | 333.8 | 646 | 1.94 | 333.8 |
| | 1 % and 5 % | 689 (657 to 670 measured at 5 %) | 646 (667.5 from r = 8 ms) | 0.94 to 0.97 | 689 |
| | gemodel | 562 to 577 measured | 732 + NACK answers | about 1.3 | 441 to 485 + NACK answers |
| Opus 2.5 ms | clean | 135.8 | 262 | 1.93 | 135.8 |
| | 1 % and 5 % | 281 | 262 (271.5 from r = 20 ms) | 0.93 to 0.97 | 281 |
| | gemodel | 178.5 to 197.5 (plan) | 300 + NACK answers | about 1.5 to 1.7 | 178.5 to 197.5 |

Clean two-path rates per listener: 1.46 Mbit/s (Opus 1 ms), 5.17 (PCM 1 ms), 0.84 (Opus 2.5 ms), half from each
edge.

Capacity and money, 1 ms parts (the "capacity per edge, published" of the owner's goal, until the measured curve
replaces it). Edge budget 400,000 datagrams a second (the wave 7 estimate, SR:3185); the same estimate puts p99
within 1 ms of idle at 200,000 to 250,000 ("knee"). Not measured on Linode.

| mode, condition | datagrams a part, both legs | listeners per edge at 400k | at the knee |
| --- | ---: | ---: | ---: |
| one path, clean | 1.03 | 388 | 194 to 242 |
| one path, 1 % or 5 % | 2.1 | 190 | 95 to 119 |
| two paths, clean, 1 % or 5 % | 2.00 to 2.06 | 194 to 200 | 97 to 125 |
| two paths, gemodel | 2.25 + answers | 178 at most | 89 to 111 at most |

Per 1,000 listener-hours, clean (g6-dedicated-2 $0.054/h; transfer $0.005/GB past the pool): Opus 1 ms, edges
$0.14 with one path and $0.27 with two; transfer $1.70 (0.340 GB/h a listener) and $3.28 (0.655). PCM 1 ms,
transfer $6.01 and $11.63. Transfer is about twelve times the instances and it is what the second path doubles. In
a lab the listener traffic is on the VLAN and unbilled.

### 3.4 `RESIDUAL_TARGET` stays at 0.8 %

The draft lowered it to 0.2 % to win p99 at 5 % loss. The w23 reports with today's `C1.16.2.7` at 5 % show a p99 of
0.523 to 0.567 ms against 0.503 to 0.566 ms clean on eight clients; the parts that wait for a NACK are 0.19 to
0.30 % of all and sit above p99.75. The whole effect is the on-time distribution's p99.25 less its p99, 0.02 to
0.04 ms, for 48 % more bytes at 5 % loss in both modes. The value is a flag so the referee can score 2,000 as a
second pass of one session (`COMPARE_UC_EXTRA="--compact-residual-ppm 2000"`); it becomes the default only if the
p99-at-5 % rows improve on two lanes by more than the clean p99 differs between the two passes.

### 3.5 The edge-choosing code (`client_director.rs`, shared by both binaries)

The director owns the session, the skip set, the entry points, the spare and the failover state. It performs no
I/O: it returns the datagrams to send and the HTTPS requests to run, and takes their answers. Its invariant: **a
leg that still delivers is released only when its replacement is level (or steady for `--level-wait-ms` after a
`MOVE` that is not a rebalance), or when another leg delivers and no replacement exists.** Only the edge's grace
cuts a delivering leg otherwise.

Skip set, by cause (a node in it is sent as `skip` and not taken from an answer):

| cause | from | until |
| --- | --- | --- |
| the leg got `MOVE` | the `MOVE` (so the entry sees a mover, and a rebalance, M:20399-20418) | 3 s after the leg's release (one freshness window, M:19562: by then a draining edge is out of the candidates, and a resumed one may be taken again) |
| the leg failed by itself: dead beside another leg, `REFUSED`, or no answer to the question of 3.2 after every leg went silent | the failure | 30 s (`SKIP_FOR`, B:57) |
| no `TOKEN` in `--token-ms` | then | 3 s |
| every leg silent together | - | never skipped |

When every cache of the region in an answer is skipped, the one skipped longest ago is taken and its skip dropped
(today's rule, B:1183-1200, with a leg held or without).

The loop, run after every event and timer:

1. `want` = legs the entry last granted for `--paths` (it grants one for a stream over the two-path bit rate; the
   client also falls to one once 8 of the last 16 parts arrived as RaptorQ objects). When the entry granted fewer
   than `--paths` for want of an edge, the missing leg is asked for again after 1, 2, 4, then every 5 s.
2. `held` = legs Asking, Tokened or Live. If `held < want`, or the spare is missing, and no request is out: ask.
3. **The request.** `ASSIGN` from the listener's own media socket to the UDP port of one entry,
   `entries[entry_for(sid)]` (LA:169), with `p` = legs missing, `s` = 1 if the spare is missing, `skip`, and `h` =
   the legs kept (never the spare: the entry may name the spare's node as a leg, and its token is then already
   held). Not doubled; repeated on the `--first-retry-ms` schedule. If the entry has not answered after
   `--first-retry-ms`, or failed in the last 10 s, the same request also goes to the next entry. HTTPS to the first
   entry after `--assignment-https-after-ms`. The first answer wins; whole budget `--assignment-timeout-ms`.
   Today the client uses a socket of its own for this (B:495) and the load tool HTTPS only (LA:223).
   Timing: with no delivering leg, or for a leg that got `MOVE`, at once. Beside delivering legs with nothing
   leaving (a second leg later, a new spare), delayed 0 to 250 ms by a hash of the sid.
   The confirmation (`t=`, `current` = every leg held after the change) goes to every entry that answered, twice
   30 ms apart.
4. **The answer.** The first `p` caches that are in the region, not skipped and not legs become legs:
   `add_leg(node, addr, from_part)`. `from_part` is none at a join; the newest part + 1 when another leg delivers
   (holes stay with the legs that sent the parts); the oldest hole at most 4,096 parts back, else the newest + 1,
   when no leg delivers. The cache after them is the spare when `s=1`: its token is fetched at once. Fewer legs
   than asked, or none, is a normal answer.
5. **`MOVE draining`, `parents-lost`.** The leg is Leaving and out of `held`, so step 2 asks for its replacement.
   It is released (`UNSUB`, twice 20 ms apart) when a leg added after its `MOVE` is level with it, or has been
   steady for `--level-wait-ms`, and no hole older than that leg's first part is still open; or at once when the
   answer names no cache of the region and another leg delivers. A one-leg listener whose answer names no cache
   keeps asking every second and, for `parents-lost`, asks the edge to take it back (edge rule 5).
6. **`MOVE rebalance`.** As step 5, but the replacement must be level. When the answer names no cache of the
   region, or the replacement is not level by `--rebalance-giveup-ms`, the replacement is released and the old leg
   sent `SUB <newest + 1> <token>` (edge rule 5): the listener stays where it was. A `MOVE` that arrives within
   one round trip of that `SUB` and before its `OK` is ignored.
7. **A leg failed.** Removed, its node skipped by cause. If a hedge leg is live it takes the place; else step 2.
8. **No leg at all.** The request asks for `want` legs and a spare from the oldest hole. Two assignment failures in
   a row with no leg send the listener to a failover entry, as today (B:59, B:1152-1161). With a leg delivering,
   assignment failures are retried every second and never move the listener out of its region.
9. **`MOVE region-lost`**, and an answer that names only failover entries while every home leg is leaving: the home
   legs are Leaving, the home region's caches skipped, the failover entry asked for `want` legs and a spare; the
   home legs are released as the new ones become level (part numbers are the same in every region, so the dedup
   holds across the move). The return home after 30 s (B:1350-1378) works the same way.

### 3.6 The assignment (A:235 `assign`, M:20374 `assign_reader`)

`AssignmentRequest` gains `paths: u8`, `spare: bool`, `redundant: bool`; `ReaderRequest` gains `hold: &[String]`,
`paths`, `spare`; `Assignment` gains `legs: u8`, `spare: bool`; `AssignmentPolicy` gains `two_path_max_bps`.

- **Legs granted.** `paths`, or 1 when `playlist_bps > two_path_max_bps`.
- **Candidates.** `hold` is always removed. `skip` is removed unless that leaves no live cache of the region
  (`without_skipped`, M:19146, as today). For a rebalance (below) `skip` is removed strictly.
- **A leg the listener depends on** (it holds no leg: `hold` empty, the first leg of the answer): exactly as today,
  a new reader by the tiers (A:295-383), a mover by `apportion` (A:564).
- **A redundant leg** (the second leg of a join; any leg when `hold` is not empty). Gate: the cache admits
  (A:286-291) and its effective load with one more listener (`listener_permille`, M:595) stays under
  `rebalance_target_permille` (800). Choice among those: at a join or for a second leg later, another holder of
  the playlist by the session's weighted rendezvous (`rendezvous_score`, A:624), else the non-holder with the
  highest `standby_rank` (A:603), which keeps a small stream on two edges, its holder and its standby; for a
  mover (`skip` not empty), `apportion` over them at the target's level, so a drained edge's legs spread by
  headroom. No cache passes: the leg is not granted, and nothing spills (`assign_udp_in_region`, A:429, is for a
  listener with nothing).
- **A rebalance** (`rebalancing`, M:20412: the skipped edge is live, not draining and over the target). Only tier 0
  of A:301-311 counts: caches that stay under `rebalance_receiver_permille()` (725, A:141) with the listener
  counted. No such cache: no cache of the region is named, with `hold` or without, and no spill
  (M:20462-20468). Today tier 1 is used when tier 0 is empty (A:347-372), which hands a leg to an edge over the
  level.
- **The spare** (`s=1`). From the candidates without the legs: a holder of the playlist by the session's weighted
  rendezvous, else an admitting non-holder by the same. No load gate: a spare is no load, and its edge refuses a
  hedge when overloaded (edge rule 9). A one-path listener of a one-holder stream thereby gets the standby-copy
  edge, which holds the playlist.
- **Standby copies count UDP legs.** `PlaylistHolding.sessions` (M:19701) adds the stream's live UDP subscribers
  to the LL-HLS sessions, so `wants_standby_copy` (M:15176) gives a UDP-only stream its standby copy as it gives
  one with readers.
- **Memory and charges.** `remembered_assignments` (M:19130) holds up to two nodes per session. A request this
  entry has answered already (a repeat, the HTTPS hedge: the remembered nodes are in neither `skip` nor `hold`)
  gets the same nodes and is not charged again (`repeated_move`, M:20404, widened). An unproven UDP request
  charges and remembers nothing, as today. The confirmation replaces the memory with its `current` list and
  charges (`charge_reader`, M:20517) each node not already remembered; HTTPS with `sid` charges each leg it
  grants. The mover log (`log_mover`, M:19356) is keyed by session, skip list and the leg's place in the answer,
  so two legs of one session replaced within 5 s are two movers and a repeat is one.
- **Counting.** A leg is a subscriber: `client_udp.subscribers`, the advertised `udp_subscribers`,
  `--client-udp-max-subscribers`, `load_permille`, the rebalance's cost of a listener (E:3633) and
  `region_listener_permille` (M:595) all count legs and need no change.

### 3.7 The join

```
t = 0      ASSIGN (p=2 s=1) to one entry               [again at 25 ms, and to the next entry; HTTPS at 50 ms]
t = r      ASSIGNED: legs X, Y, spare Z.  Tokenless SUB to X, Y (each again at 3 ms) and to Z
t = 2r     TOKEN from each.               SUB with token to X and to Y (each again at 3 ms)
t = 3r     OK + first part from each (first_part_at_once, E:2477): the first to arrive is first audio
```

Clean: 3 r plus the edge's admission, as today (9.9 ms p50 in the lab), with the better of two edges at the tail.
One leg granted (`--paths 1`, a stream over the bit rate): the tokenless `SUB` still goes to the leg's edge and
the spare's; the `SUB` with the token goes to the leg's edge if its `TOKEN` comes within 10 ms of the spare's,
else to the spare's edge (the two swap roles).

Under loss, today each of the three exchanges fails 9.75 % of the time at 5 % loss each way and costs a 25 ms
repeat: 26 % of joins take at least one. With the two edge requests doubled and two edges, an edge stage fails
when both copies or both answers are lost on both legs: under 0.1 %. The `ASSIGN` is one exchange with one entry:
9.75 % of joins take one 25 ms repeat. It is not doubled because it is a padded 1,200 B request and a task on a
forwarder (E:1831; assignment work has starved a distributor before, M:20325-20330). Expected at 5 % random loss:
p95 as clean, p99 clean + 25 ms (today 62 ms in the lab).

An edge that died inside the 3 s its advertisement stays fresh (M:19562): its leg gets no `TOKEN`; the other leg
plays at 3 r; at 400 ms the dead edge is passed over. With one leg granted the same case costs first audio 10 ms.

### 3.8 The spare and the hedge

- The spare is named by the entry (3.6) and held as a token only: a tokenless padded `SUB` answered `TOKEN`
  (E:1585-1604), renewed every 15 s. Two renewals in a row unanswered within `--token-ms`: the spare is dropped
  and another asked for (delayed as in 3.5 step 3). Nothing is stored at the edge.
- **Hedge beside a delivering leg** (3.2 "hedged"): `SUB <newest + 1> <spare token> o=<delivering legs>` to the
  spare, delayed 0 to 20 ms by a hash of the sid so that a stalled edge's listeners do not arrive in one instant.
  The spare is now a leg. If the silent leg delivers again before 200 ms, both are kept until it has been steady
  for `--level-wait-ms`; then the hedge leg is released and is the spare again (`hedges_released`). If not, it is
  dead at 200 ms and the hedge leg has its place. The entry is asked for a new spare; the confirmation that
  follows names the hedge leg, so the entry charges it.
- **Hedge of a lone leg**, and of every leg silent: `SUB <oldest hole, else newest + 1> <spare token>` with no
  `o=`, delayed the same way. The edge takes it as a mover (it names a part, E:2330) and resumes from the hole.
- An overloaded spare refuses a hedge that carries `o=` (`edge-busy`): the listener stays on one path and asks its
  entry.
- A dead token at the spare (the edge restarted, the client's address changed): the `SUB` is answered `TOKEN` and
  repeated with it, one round trip more.
- Costs. Two paths, an edge killed: one path from the dead leg's last part to the hedge leg's first, 50 ms + up
  to 20 ms + r. One path, the edge killed: late by 170 ms + up to 20 ms + r, against 200 ms + 3 r today; nothing
  lost. That mode is not hitless by the rule; it is the cheap mode. Both figures hold where the spare's edge
  holds the playlist (another holder, or the standby copy); a spare that must take a copy first adds the copy's
  start, about a second.
- Why 50 ms: the longest one-edge silence in the loaded lab run is 17.6 ms; one edge instance of the wave 7 record
  stopped sending for 0.1 to 0.9 s (D's reading of `waudio7`), which is the case the hedge is for. Why 170 ms for a
  lone leg: it has no sibling to tell a dead edge from a quiet stream, only `WAIT` every 80 ms, and one lost `WAIT`
  must not send every listener of a paused stream to its spare.

### 3.9 4K and other streams over the two-path bit rate

One leg, RaptorQ, decided at the entry (`p=1` in the answer), so no second edge is asked to send video or take a
copy. Backstop for a stream whose bit rate no cache has measured yet: once 8 of the last 16 parts arrived as
RaptorQ objects the client's `want` is 1 and the leg that is not leading is released. A leaving leg of such a
stream is released at its replacement's first object, not when level. So a 4K viewer gets the smallest honest
improvement: a drain, rebalance or roll no longer costs it the three round trips of a break-before-make move, at
the price of at most half a round trip of doubled video; a kill costs 190 ms + r plus the spare edge's copy (a
holder when one exists), against 200 ms + 3 r today. Twice 247 Mbit/s per viewer is not a cost anyone would pay
for an edge fault, and video parts have no shared identity across contributors this round (L1).

### 3.10 The reader (`needletail-reader`): faster off a vanished host

LL-HLS readers stay on one edge. A drain already costs them nothing. What is slow is a host that vanishes: the
request limit is 1,500 ms + 4 r from the last byte (R:815, R:1396-1412), a stall for a 1 s buffer. For a reader
that has received parts from the cache:

- `stall` = max(`--stall-ms` 400, 8 part intervals) + 4 r replaces `timeout()` in the wait loop. 400 ms is two
  minimum TCP retransmission timeouts: a live connection on a lossy path does not look dead.
- At `stall / 2` without a byte the reader asks the current cache for its playlist on the connection it has
  (`load_playlist_within`, R:954), to be answered by `stall`. An answer means the cache lives: the existing rules
  apply (the stream has stopped: wait where it is; later parts are listed: skip the hole; R:1493-1524). No other
  cache is touched, so a pause of the stream opens no connection anywhere.
- No answer by `stall`: `fail_over` (R:906), and the connection to the next cache is opened then, delayed 0 to
  100 ms by a hash of the sid (the next cache is the same for every reader of a playlist on a cache, A:384-396).

Time to move off a vanished host: about 400 ms + 8 r + up to 100 ms, under a 1 s buffer for r up to about 60 ms;
today at least 1.5 s. `--request-timeout-ms` keeps its meaning for a first request.

### 3.11 The scored changes

r = 2 ms (local lab), 80 ms, 300 ms. "32 parts" is 32 ms at 1 ms parts, 80 ms at 2.5 ms; figures below use 1 ms.
All rows assume three or more serving edges in the region, as section 7 requires.

| change | what the listener sees | duration | on one path | late or lost |
| --- | --- | --- | --- | --- |
| **Edge drain** (each step of a roll, scale-down) | `MOVE`; entry asked (r); token (r, none when the named edge is the spare); `OK` (r); level (32 parts); `UNSUB`. The old edge serves throughout | edge side, mark to removal: 3.5 r + 32 parts = 39 ms / 0.31 s / 1.08 s. Drain end = housekeeping (<= 1 s) + spread 2 s + that = 3.0 / 3.3 / 4.1 s; hard bound 1 + 2 + 10 = 13 s | 0 | none, with one path or two |
| **Edge kill** | two paths: the other leg carries on; plan without the dead leg at 20 ms; hedge at 50 to 70 ms; the hedge leg's first part one round trip later; dead at 200 ms | - | 70 ms + r at most = 0.07 / 0.15 / 0.37 s | two paths: none. One path with the spare: late by up to 190 ms + r, none lost. `--spare false`: 200 ms + 3 r |
| **Edge crash-restart** (back in 2 s, empty cache, new token keys) | as a kill; the listener skips the edge 30 s; C's start-drained keeps it out of the assignment until it is warm; a later leg there is added beside live legs | as a kill | as a kill | as a kill |
| **Two edges drained at once** | a listener with both legs there replaces both, each make-before-break | as a drain | 0 | none |
| **Two edges killed at once** | a listener with both legs there has no leg; every leg silent at 200 ms; the spare answers | - | - | late by up to 220 ms + r, none lost. **Outside the claim.** Everyone else: as a kill |
| **Rebalance** (autoscale up, after a roll) | `MOVE rebalance` to legs older than 10 s; handover as a drain; a new edge whose copy is still catching up is not level, so the listener returns to its old edge at 3 s | as a drain | 0 | none |
| **Edge roll** | per edge: a drain, a stop with nobody on it, a start (drained until warm, C), then rule R3 brings legs back | per edge as a drain | 0 | none |

**Why each passes the rule, at any round trip.** No unit is missing: nothing depends on a retransmission from an
edge that is going or gone. In a planned move the listener has its old leg, its other leg and the new one until
the new one is level, so it is never below the two paths its steady state has: the longest silence and the p99
inside the window are first-of-two figures, as outside. After a kill it is on one path for the span above; that
span is the exposure, it is reported (`single_path_ms`) and bounded in every scenario's criteria, and the round
trips of the recovery are spent beside a path that needs none. The limits: `--rebalance-giveup-ms` 3 s and the
edge's grace 10 s bound the handover, so r up to about 2 s for a drain. A 1 ms lab hides none of this because
nothing in it is timed against r; section 6 runs every scenario at three round trips.

**With two serving edges** (the referee's headline topology, a region the autoscaler has drained to its floor): a
drain or kill of one leaves every listener on one path until an edge returns, for seconds. Nothing is missing and
nothing is later than one path's own steady state, but by the first table of section 0 the gap clause then fails
with a chance of about (seconds on one path)/140 on idle edges and almost surely under a crowd. Not claimed.

What the edge's grace protects against is a client that never answers `MOVE`: served 10 s more, then cut, which is
today's behaviour 10 s later. The autoscaler's 60 s wait for the gauges needs no change.

### 3.12 SIGTERM as a drain (seam with C)

C owns the signal handling (only SIGINT is handled today, M:4436). B's bound, which C asked for: on SIGTERM the
node drains as `ControlKind::DrainNode` does (M:21582) and waits until `live_subscribers() == 0` and no LL-HLS
session is active, or `drain_spread + move_grace + 2 s` = 14 s with the defaults (`--stop-drain-ms 14000`), then
shuts down; edge rule 6 sends a last `MOVE` to whoever is left. `TimeoutStopSec=20`. The typical wait is the drain
end of 3.11, 3 to 4 s. A second SIGTERM, or SIGKILL, is a kill.

## 4. Failure cases and races

| case | what happens |
| --- | --- |
| `MOVE` lost | the edge repeats it every 250 ms and answers the next refresh with it |
| `UNSUB` lost | a leaving leg: the edge cuts it at the grace. Any released leg: `UNSUB` is sent twice, and a datagram from a released edge within 10 s is answered with another |
| `UNSUB` or `NACK` with a dead token | cannot happen after 2.6 |
| The replacement refuses (`edge-busy`, `stale-copy`, `MOVE`) or sends no token | that leg fails or is passed over, its node is skipped by cause, the entry is asked again; the old leg stays (the edge's grace bounds it) |
| No entry answers while a leg is leaving | retried every second; at the grace the edge cuts the leg; the listener goes on with its other leg, or, with none, as after a kill (the spare) |
| Both legs get `MOVE` together | two replacements, each released separately; in a region with no third edge the listener asks every second and is cut at the grace |
| A roll of edges in a small region: X drained, restarted, then Y drained | X's skip ends 3 s after its release, so X is taken again as soon as it advertises; if every cache named is still skipped, the one skipped longest ago is taken. No listener is left without an edge by its own skip set |
| The answer names an edge the client holds (an older entry, a race) | ignored; if nothing else is named, as "no cache" |
| The answer names the spare's node as a leg | the spare is promoted with the token it holds; a new spare is asked for |
| An edge restarts within a leg's 200 ms | the refresh is answered `TOKEN`, the `SUB` admits the listener as new at the head; the parts between are on the other leg |
| A leg stalls for 60 ms and comes back | hedged at 50 ms; three legs for about a second; the hedge leg is released when the first is steady again (`hedges_released`) |
| The client host stalls for 300 ms | no verdict while datagrams are queued; the backlog is read first; nobody is failed |
| The last mile is out for 300 ms | every leg silent: each is asked again from the oldest hole (edge rule 8 sends the backlog again), the spare is hedged; duplicates from two or three edges once; nobody is skipped. An outage longer than 4,096 parts is not back-filled |
| One leg is later than the other by more than a part | the first NACK of a hole waits for the lag (up to 20 ms); a replacement that is steady but not level takes over only after `--level-wait-ms`, or never for a rebalance |
| The two legs of a join start two parts apart | the floor is the first part delivered; the slower leg's older parts are dropped (`before_start`) |
| A sick second edge answers after 4 s with a stale head | failed at once (`stale_start`); nothing of it is delivered, the newest part does not move |
| The two edges' bytes of a part differ (publication time unknown at one, E:2611; generation byte 0 at one) | the repair leg's bytes are the ones kept; a row that still fails its CRC counts `cross_leg_mismatch`, the window is kept and the part is NACKed |
| A gap from one edge, the part from the other | delivered as the part |
| The source epoch changes at the two edges a moment apart | the first datagram of the new generation starts the new flow (floor, per-leg state reset); the old generation's late datagrams are dropped (C:1344-1366) |
| A leg's `OK` has no `C` (an edge without the compact lane) | the leg is released and its node skipped while another leg has the lane; alone, it is used as today |
| A stall of the stream itself | every edge sends `WAIT`; no leg is silent; nobody moves or hedges |
| Every listener of a dead edge hedges | within 50 to 70 ms, each at its own spare (spread by the sessions' rendezvous); an overloaded spare refuses those that have another leg; the entries are asked for new spares over the next 250 ms; at most 256 `ASSIGN`s are in flight per node (E:1882), the rest repeat |
| A rebalance when every receiver is the listener's other leg | the edge passes that subscriber over (R1). If it is marked all the same, the entry names no cache and the listener is back at once (r), its share returned to the round (R2) |
| A rebalance onto an edge over the receiver level | cannot happen: tier 0 or nothing (3.6) |
| A subscriber leaving for `rebalance` when a drain begins | re-marked `draining`, grace restarted; reminders carry the new reason |
| Round trip over 100 ms and the 400 ms token wait | the wait is at least four round trips to the entry |
| A load host confirms for 2,000 listeners a second | the lab renders `--client-udp-assign-confirms-per-source 4096`; `assignment_confirms_limited` stays 0 |
| The probe's only entry is killed (an ingress or forwarder change) | legs and spare go on without it; a request made meanwhile is retried every second and goes to the second entry if one was given (section 7) |
| Mixed builds in a roll | a new client at an old edge is removed at `MOVE` and sees that leg go silent while its other leg plays; an old client at a new edge sends `UNSUB` when it starts its move and is removed at once. Unknown `k=v` words are skipped from this build on |
| Two serving edges in the region | every listener is on both, the spare is missing; a drain or kill of one leaves one leg until the edge is back; nothing missing, nothing late; the rule's gap and p99 clauses are not claimed (3.11) |

## 5. Implementation plan

Repo av-mesh, a branch off `low-latency`, except unit W (repo needletail, branch `low-latency`). Four units that
different engineers build at the same time. No two commits of different units touch the same function; within a
unit the commits are in order.

**Unit X: receiver and session** (U, C, S)

| # | files and functions | tests (new unless marked) |
| --- | --- | --- |
| X1 | U: `Control` (U:257): `Subscribe.{others, resume}`, `Assign.{paths, spare, hold}`, `current: Vec<String>`, `Assigned.{paths, spare}`; `encode` (U:340-455), `decode` (U:457-631): unknown `k=v` words skipped, list ends. Callers compile with defaults (B:504, B:567, E:1761, E:1836, S:372). **First commit of the whole work.** | U: `assign_with_paths_spare_and_hold_round_trips`, `assigned_with_paths_and_spare_round_trips`, `sub_with_others_round_trips`, `unknown_trailing_words_are_skipped_on_every_verb`; extends `assignment_datagrams_round_trip_and_the_request_is_padded` U:1505 |
| X2 | C: `LegId`, `open_leg`, `close_leg`, `leg_expect(leg, part, compact) -> LegStart`; `push(leg, ..)` C:1485 and `part_number(leg, ..)` C:1378 (per-leg references); `mark_delivered` C:1400 (floor, `before_start`); `remember` C:1382 (`set_prefer_leg`); `try_window` C:1698 (windows per leg, kept on a miss, `cross_leg_mismatch`); `take_counts(leg)` C:1905, `take_burst(leg)` C:1946, `take_joint`; the 64-part arrival ring, `leg_lag`, `steady`, `level`; `set_leg_rtt`; `new_flow` C:1167 resets the legs; a short part lengthened before any time anchor reports publication time 0 and is not kept in `known` (seam A) | C: `a_part_from_two_legs_is_delivered_once_and_counted_on_each`, `a_second_leg_that_starts_lower_delivers_nothing_below_the_first_part`, `a_stale_first_part_fails_its_leg_and_does_not_move_the_newest`, `short_parts_are_numbered_from_their_own_leg`, `each_leg_measures_its_own_loss_and_the_joint_loss_is_what_no_leg_brought`, `rows_of_one_leg_never_enter_another_legs_window`, `the_repair_legs_bytes_are_kept_and_a_mismatch_keeps_the_window`, `a_leg_is_steady_then_level`, `a_new_generation_resets_every_leg` |
| X3 | C: `nacks_due` C:1763 returns the parts per leg (first NACK to the shortest round trip after the lag wait, retries to all; no abandon at 12; slow NACK; give-up at `hole_give_up` or 3,072 parts); `PathPlans::for_legs` (3.3); `for_measured_with(.., residual_target)` C:214. S: the plan block of `poll` (S:483-523) becomes `plan_legs` with the 1 s hold and the 2.0 / 1.5 burst band; the start plan (S:296) | C: `a_hole_is_asked_for_until_the_ring_has_passed_it`, `a_hole_waits_for_the_slower_leg_and_asks_the_nearer_first`, `two_legs_plan_parts_alone_when_clean`, `two_legs_need_no_copy_at_five_percent`, `a_row_is_planned_only_from_eight_part_intervals_of_round_trip`, `legs_that_lose_together_get_rows_on_one_leg`, `one_leg_plans_as_before`; S: `a_plan_that_asks_for_less_waits_a_second`, `a_new_one_leg_session_plans_for_two_percent` |
| X4 | U: `ObjectReceiver` RaptorQ state per leg (U:913-918), `expect(leg, ..)` U:994, `push(leg, ..)` U:1077, `take_loss(leg, ..)` U:1206; `new_flow` U:982 only for a new source epoch | U: `two_legs_blocks_of_one_part_use_two_decoders_and_deliver_once` |
| X5 | S: legs (3.1, 3.2): `add_leg`, `release_leg`, `takeback_leg`, `reask_leg`; `on_datagram` S:641 (attribution); `poll` S:468 (per-leg timers; verdicts only on an empty socket); token wait, token renewal, doubled first requests, round-trip samples, `o=` on every `SUB`; events `Tokened`, `Live`, `Steady`, `Level`, `Moved`, `Suspect`, `HedgeDue`, `Failed`, `TakenBack`, `AllSilent`; `impl SessionLegs for UdpSession` (trait from Y1) | S: `two_legs_subscribe_and_each_part_comes_out_once`, `a_datagram_is_attributed_by_address_and_port`, `a_leg_without_media_beside_a_delivering_leg_is_hedged_then_dead`, `no_verdict_while_datagrams_are_queued`, `every_leg_silent_fails_nobody_and_asks_again_from_the_hole`, `a_leg_without_a_token_is_passed_over_and_used_if_it_answers`, `a_token_is_renewed_before_the_edge_forgets_it`, `a_doubled_request_still_gives_a_round_trip` (r = 2, 80, 500 ms), `a_refresh_gives_a_round_trip`, `a_nack_goes_to_every_leg_that_sent_the_part`, `a_suspect_leg_changes_the_plan_at_once`, `a_moved_leg_receives_until_it_is_released`; the 17 existing S tests ported to one leg |

**Unit Y: director, binaries, load tool** (new `client_director.rs`, B, L, LA, BL, `tests/`)

| # | files and functions | tests |
| --- | --- | --- |
| Y1 | new `src/client_director.rs` (3.5, 3.8, 3.9): `Director`, `trait SessionLegs` (the surface X5 implements; the director's tests use a scripted fake, so Y1 does not wait for X5), `Action { Send, Https, Resolve }`, the skip set, entries, spare, hedges, `MOVE` by reason, failover | `a_join_asks_one_entry_and_two_edges_and_plays_the_first`, `the_second_entry_is_asked_after_the_first_retry`, `a_move_keeps_the_old_edge_until_the_new_one_is_level`, `a_drain_has_no_release_timer`, `a_leaving_leg_is_released_at_once_when_no_cache_is_named_and_another_delivers`, `a_rebalance_with_no_cache_returns_at_once`, `a_rebalance_that_is_not_level_returns_at_three_seconds`, `a_dead_leg_is_replaced_from_the_spare_and_a_new_spare_asked`, `a_lone_leg_is_hedged_after_two_keepalives`, `a_hedge_whose_leg_comes_back_is_released`, `skips_end_by_cause_and_the_longest_skipped_is_taken_when_all_are`, `a_roll_of_two_edges_leaves_no_listener_without_an_edge`, `one_serving_edge_gives_one_leg_and_asks_again`, `region_lost_moves_make_before_break`, `a_stream_over_the_bit_rate_keeps_one_leg`, `a_request_carries_no_current_and_the_confirmation_names_every_leg`, `a_request_beside_a_live_leg_is_spread_over_250_ms` |
| Y2 | B on the director: flags of 2.4; the `'caches` loop (B:1122-1547) goes; `assignment`, `udp_assignment`, `https_assignment` (B:412-630) reduce to the HTTPS transport; report v2 (B:1605); arrivals column (B:1517-1527) | B: `two_entries_parse`, `the_report_names_each_leg_and_the_time_on_one_path`; `the_udp_assignment_is_repeated_until_answered` B:1768 moves to Y1 |
| Y3 | L: `Listener` (L:917) holds a `Director`; `Placing` (L:1058-1332) and `subscribe_at` (L:1336) go; `worker` (L:1603): verdicts after the `recvmmsg` batch, `ASSIGN` from the listener's socket; LA keeps the HTTPS hedge (`ask`, LA:223); BL flags | `src/tests/load_mode.rs` (existing `load_mode_counts_every_part_of_every_listener`, `load_mode_moves_spread_over_the_edges_left_when_one_is_killed` pass with `--paths 1 --spare false` and with defaults) |
| Y4 (after Y3) | L accounting: `Counters` (L:188), `merge` L:272, `to_json` L:327, the `Mark::New` branch of `handle_datagram` (L:1428), `collect_one` (L:1552), `PartTrack::settle` (L:506); `--settle-ms` (default at L:673), `--interrupt-ms`, `--interval-buckets`; summary `worst_listeners` | L: `the_gap_histogram_takes_first_arrivals_only`, `a_silence_interrupts_a_listener_once`, `interval_lines_carry_latency_buckets`, `time_on_one_path_is_a_span_per_listener`. `load_mode.rs`: `two_paths_lose_nothing_and_no_listener_is_interrupted_when_an_edge_is_killed`, `one_path_reports_the_interruption_of_a_kill`, `a_drain_interrupts_no_listener_with_one_path_or_two`, `a_pause_of_the_publisher_is_a_gap_at_listeners_that_do_not_move`, `a_falling_ramp_books_nothing_missing` |
| Y5 (last) | new `src/tests/two_paths.rs`: four in-process edges, two entries, the director | `kill`, `drain`, `crash_restart`, `two_drains`, `roll_of_three_edges` (asserts no listener without a leg and none on one path before each drain), `rebalance_onto_a_joining_edge`, `rebalance_at_850_500_780` (no leg lands on an edge over 725, moves stop within three rounds, at most one take-back in five moves), `rebalance_with_room_only_at_the_held_edge` (nobody is marked; forced marks are back within r + 250 ms, `rebalance_ineffective` rises), `the_36_legs_of_a_killed_edge` (three survivors, each within 1.3 of its headroom share), `a_token_expiry_across_a_stepped_minute_under_five_percent_loss`. Each asserts: no part missing, no gap over the steady gap plus one part, and the handover, exposure and single-path times |

**Unit Z: edge, assignment, reader** (E, A, M, R). Z1 then Z2 are one engineer; Z3 and Z4 can be another after Z0.

| # | files and functions | tests |
| --- | --- | --- |
| Z0 | E: `run_edge_udp`'s arms (E:1573-1876) become `on_subscribe`, `on_unsubscribe`, `on_nack`, `on_assign`; the housekeeping of `run_stream_sender` (E:3112-3292) becomes `housekeeping_moves`. No behaviour change | the existing E and M tests |
| Z1 | E: `Leaving`, `mark_movable_aged` (E:3841), `housekeeping_moves` (rules 1 to 3), `on_unsubscribe` (rule 4), the refresh branch of `on_subscribe` (rules 5, 8), shutdown (rule 6, E:1517), the long-form first part (rule 10, E:2477-2495), counters, `Tokens::valid_at` (E:1432). M: flag beside M:2409, `EdgeUdpConfig` M:4296, gauges M:14032 and M:19840, `plan_rebalance` M:19476 (rule 7) | E: `a_moved_subscriber_is_kept_until_its_unsub_or_the_grace`, `a_paced_drain_marks_its_shares` (replaces E:5117), `move_is_repeated_to_a_subscriber_that_stays`, `a_rebalanced_subscriber_that_asks_with_a_part_is_taken_back_unless_draining`, `a_subscriber_sent_away_for_lost_parents_is_taken_back_within_the_hold_down`, `a_drain_re_marks_a_rebalanced_subscriber`, `a_stopping_edge_sends_move_to_whoever_is_left`, `a_refresh_from_a_part_sends_again_what_the_fast_lane_sent`, `the_first_part_of_a_subscription_is_in_the_long_form`. M: `a_draining_edge_serves_its_udp_clients_until_they_have_left` (replaces M:36688), `the_gauges_show_leaving_subscribers` |
| Z2 | E: `Subscriber.others` set and updated in `on_subscribe`; `admit` E:2330 (rule 9); `RebalanceState::returned`, `settle` (E:3737) and the round's count (R2); `rebalance_moves` at the `UNSUB`; `rebalance_quota` E:3576 (R3); `avoid` in `mark_movable_aged` (R1). A: `node_tag`. M: `plan_rebalance` (receivers' tags, the mean), `RebalanceConfig` M:19167, flag `--client-udp-rebalance-to-mean`, gauges `single_legs`, `legs_by_other`, `redundant_refusals` | E: `a_redundant_leg_is_refused_by_an_overloaded_edge_and_a_lone_mover_is_not`, `a_rebalance_passes_over_a_subscriber_whose_other_leg_is_the_only_receiver`, `a_take_back_returns_its_share_to_the_round`, `a_round_that_mostly_comes_back_pauses`, `an_edge_over_the_mean_sheds_to_the_mean` (720 / 720 / 0 gives 555 / 555 / 330), `rebalance_moves_count_at_the_unsub`. M: `the_gauges_count_legs_by_their_other_edge` |
| Z3 | A: `AssignmentRequest.{paths, spare, redundant}`, `AssignmentReason::{SecondPath, Spare}`, `Assignment.{legs, spare}`, `AssignmentPolicy.two_path_max_bps`, `assign` A:235 (the redundant gate and choice, the spare, tier 0 only for a rebalance). M: `ReaderRequest` M:19176, `assign_reader` M:20374 (`hold`, `moving`, pair memory, charges), `log_mover` M:19356, `confirm_udp_assignment` M:20581, `client_assignment` M:21063, `udp_assignment` M:21122, `assignment_api_response` M:21149, flag `--assignment-two-path-max-bps` beside M:2881, `PlaylistHolding.sessions` M:19701. E: `on_assign` (fields through, `p=` and `s=` on the answer, the cut never below `p + s`, the limit from the config, `assignment_confirms_limited`), flag `--client-udp-assign-confirms-per-source` | A: `the_second_path_is_another_holder_by_headroom_else_the_standby_copy`, `a_redundant_leg_is_not_given_over_the_target`, `the_spare_is_a_holder_that_is_not_a_leg`, `one_serving_edge_gives_one_path_and_no_spare`, `a_rebalanced_listener_gets_a_receiver_under_the_level_or_none`, `two_replacements_are_apportioned_to_two_edges`. M: `an_assignment_for_two_paths_names_two_edges_and_a_spare`, `a_playlist_over_the_bit_rate_gets_one_leg`, `a_udp_mover_that_names_its_old_edge_as_current_is_a_mover`, `a_replacement_is_never_an_edge_the_client_holds`, `the_confirmation_charges_each_new_leg_once`, `a_udp_only_stream_gets_its_standby_copy`. E: `confirmations_over_the_limit_are_counted_apart` |
| Z4 | R: `stall_timeout`, the playlist question at half the stall, the delayed connection (R:815, R:954, R:1396-1412, R:906) | R: `a_cache_that_stops_sending_is_left_after_the_stall_time`, `a_cache_that_answers_its_playlist_is_kept`, `a_quiet_stream_opens_no_connection`, `readers_open_the_next_cache_spread_over_100_ms` |

**Unit W: scripts** (repo needletail)

| # | files and functions | tests |
| --- | --- | --- |
| W1 | `scripts/linode-small-mesh-deploy.py`: settings `client_udp_move_grace_ms` (10000), `client_udp_assign_confirms_per_source` (64), `assignment_two_path_max_bps` (8000000), `client_udp_rebalance_to_mean` (true) in the settings table and the mesh render | the render test asserts the four flags |
| W2 (after Y2) | `scripts/compare/latency-bench.py`: `--write-parts` on its UDP clients (LB:823-831), `--joins N`, the v2 report fields in `summary.txt` and `hops.json` (legs, replacements, time on one path, wire bytes a part, per-leg p99 and longest gap) | `test_latency_bench.py`: `test_joins_are_reported_per_host`, `test_the_two_path_report_is_read` |
| W3 (after Y4) | `scripts/edge-campaign.py`: the scenarios of section 6 (EC:3005, 3034, 3045, 3064 rewritten; the new ones), `listener_checks` (EC:2831) reads the new load fields, `udp_fault` (EC:2844) takes `paths`, `client_loss`, `reader_netem` | `test_edge_lab_slots.py` `Campaign`: registration and the judgement helpers |

Order: X1; then X2 to X4, Z0 to Z4, Y1 and W1 in parallel; X5 after X2 to X4; Y2 and Y3 after X5 and Y1; Y4 after
Y3; W2, W3, Y5 last. Z1 to Z3 are useful alone (an old client at a new edge still works). X5 with one leg must
reproduce today's client before anything else is judged (section 6, step 0).

Seams:

- **A (contributors).** (1) Shared `known` bytes rest on two edges making the same compact datagram of a part:
  the same publication time and generation byte. A's deterministic stamps keep that true with two contributors;
  `cross_leg_mismatch` reports when it is not. (2) Published becomes the source's time: the client's short form
  extends 24 bits of it around its own clock until the first long-form part (C:1502-1506), which is wrong for a
  source more than 8 s off. Edge rule 10 and the unanchored rule of X2 are B's answer; no client decision reads
  Published. (3) A run of numbers that never existed (both contributors down): the client already tracks no hole
  for a jump over 256 parts (C:952); the edge's sender must pass such a range without a gap object a number. That
  needs the cache's gap range from A's final specification and is one change in `run_stream_sender` (E:3294-3360),
  to be made with it. (4) The probe's entry may be a killed ingress: section 4.
- **C (operations).** SIGTERM as a drain, `--stop-drain-ms 14000`, `TimeoutStopSec=20` (3.12; C's draft has 8 s
  and 15). Start-drained: B changes nothing. The edge roll: its capacity gate must count that a leg cannot move to
  its listener's other edge; the drained edge's `legs_by_other` gives, per other edge, the legs that edge cannot
  take. Its gate after a restart is C's readiness; before the next drain it waits until the region's sum of
  `single_legs` is back at its value before the step (or 20 s): that is "no listener is on one path" read from
  the nodes. The autoscaler's `min_edges` becomes 3. The proving topology serves three edges a region (section 7).
  The local change-window runner. `scale-run.py` reads the new load fields in `interval_rows` and judges
  `listeners_interrupted`, `listeners_with_missing`, `longest_silence_ms`, `single_path_ms`, the window's latency
  p99 and `assignment_confirms_limited == 0`, and waits for `paths.one == 0` before a fault.
- **D (hunt).** The token finding and the give-up change close "residual loss on long burst windows"; D's 200 s
  gemodel windows are the proof (section 6).

## 6. Local proof

Rules of the lab as in the maps: one pass at a time unless partitioned, frozen binaries, `edge_lab.py down` before a
pass that must take a new binary. Every probe is `needletail-udp-client ... --arrivals A --write-parts P`, ingested
with `needletail-compare/target/ntcmp udp-ingest` and scored with
`needletail-compare/scripts/compare/change-window.py --frames F --marks M`. The runner that holds probes through a
change and writes the marks is C's; until it exists the same pass is `latency-bench.py --node-events ...` with
`--write-parts` (W2).

**Step 0, no regression with one path.** The matrix of the local-labs map (3 lanes x clean, 1 %, 5 %, gemodel;
`latency-bench.py --payload opus|pcm [--epoch-samples 120] --seconds 40 --udp-clients 2 [--client-loss ...]
--udp-client-args "--paths 1 --spare false"`) against the w23 final binaries: p50 and p99 within 0.1 ms, 0
missing, the same wire bytes a part after the first two seconds.

**Step 1, the loss matrix with two paths.** The same 12 passes with defaults, and with `--paths 1`. Pass: 0
missing; clean p99 no worse than step 0; p99 at 5 % within 0.3 ms of clean on all three lanes; wire bytes a part
within 3 % of the table in 3.3 on the clean and random rows (the burst rows are reported with their NACK answers
apart); `cross_leg_mismatch` 0; `hedges` 0 on the clean passes; plan changes under 10 in 40 s. Reported beside the
delivered figures: each leg's own p99 and longest gap. Then three 200 s gemodel windows per lane: 0 missing,
`holes_abandoned` 0, the edges' `bad_tokens` unchanged over the run (today it rises once or more per listener
every two minutes), worst part under 3 r + 16 parts. Then one pass with the probe's namespace held to two CPUs
beside three `ntcmp hls` readers: `kernel_to_arrival_ms` p99 with `--paths 2` within 0.05 ms of `--paths 1`
(Sydney's host of record has two vCPUs).

**Step 2, joins.** 200 joins per condition (`latency-bench.py --joins N`), London and New York: clean p50 and p99
no worse than today's (9.9 and 16.3 ms in London); at 1 % and 5 % random loss p95 within 5 ms of the clean p95 and
p99 under 45 ms (today 34.5 and 62.1); gemodel p99 under 100 ms (today 62 to 187). Then with `lon-edge-1` killed
1 s before each batch of joins: with two paths the clean figures; with `--paths 1` p99 within 12 ms of clean.

**Step 3, the changes.** The lab with its four London edges, all with `client_udp_datagram_capacity=40000
cache_mesh_warm_ms=4500 client_udp_assign_confirms_per_source=4096` (`slot-edge-join` and the autoscale scenario
hold `lon-edge-4` out as the slot, EC:2305); the 64 s loop source of latency-bench; stream 3001 at 1 ms, then PCM
4001, then 2.5 ms Opus. Listeners: two probes on `lon-readers` and two on `new-readers`; `needletail-load` on
`lon-readers-2` with the probes' flags: 40 two-path listeners (80 legs, 20 an edge, 500 permille; 667 with one
edge gone, under the 800 gate), or 80 with `--paths 1`; four `needletail-reader`s. Each scenario at three reader
round trips: the lab's (about 2 ms), `--reader-netem "delay 40ms"` (81 ms), `"delay 150ms"` (301 ms), and each
with `--paths 2` and `--paths 1`. Common criteria: (a) every probe passes `change-window.py`; (b) crowd:
`listeners_interrupted` 0, `listeners_with_missing` 0, `missing` 0, `paths.none` 0 in every interval,
`longest_silence_ms` inside the window <= the longest outside + 1 unit, latency p99 inside (from the buckets) <=
outside + 1 ms; (c) readers: 0 missing, no stall over the buffer.

| scenario | the change (one command, marks at its start and end) | pass |
| --- | --- | --- |
| `udp-edge-drain` (EC:3005 rewritten) | drain `lon-edge-1`, return when its gauges are zero; resume | (a), (b), (c); `single_path_ms` 0 in the window; `moves_expired` 0; drain end <= 3.2 s + 3.5 r. `--paths 1`: the same |
| `udp-edge-kill` (EC:3034) | SIGKILL `lon-edge-2`; start it 30 s later | two paths: (a), (b), (c); every single-path span and `exposure_ms` p99 <= 80 ms + r. One path: expected to fail (a) on the gap; 0 missing; lateness <= 200 ms + r; with `--spare false` <= 230 ms + 3 r |
| `udp-edge-crash-restart` (new) | SIGKILL `lon-edge-2` under a supervisor that restarts it after 2 s | as the kill; no probe or listener takes a first arrival from the restarted edge that is later than its other leg's |
| `udp-two-edges-drain` (EC:3045) | drain `lon-edge-1` and `-2` together | as the drain |
| `udp-two-edges-kill` (new) | SIGKILL `lon-edge-1` and `-2` in one instant | listeners with a leg left: (a), (b). Listeners with none: 0 missing, lateness <= 230 ms + r. Reported as outside the claim, with the share of listeners hit |
| `udp-edge-kill-two-edges` (new; reported, not a gate) | SIGKILL `new-edge-2` (New York has two edges) with the two New York probes and 20 two-path listeners | 0 missing, nothing later than one path's steady p99 + 1 ms; the rule's verdict and the seconds on one path are recorded as what a two-edge region gives |
| `slot-edge-join` (EC:3064) | start `lon-edge-4` under 30 two-path listeners on three edges (60 legs, 500 permille), 15 more at 35 s | (a), (b), (c); rebalance moves > 0 and every one a handover (`time_to_move_ms` empty); no old leg released to the joining edge before that leg is level; the joined edge's peak `load_permille` under 900; `assignment_confirms_limited` 0 |
| `udp-edge-roll` (new; `edge-rolling-restart` EC:535 stays for the readers) | for each London edge: drain, wait gauges, SIGTERM, start, wait serving, resume, wait `single_legs` (6 s steps at the least) | (a), (b), (c) over the whole roll and per edge; `paths.none` 0 in every interval and `paths.one` 0 before each drain; legs per serving edge max/mean <= 1.3 within 30 s of the roll's end |
| `autoscale-edge-lab` (EC:3251) | the controller alone, up and down, `--min-edges 3` | (a), (b), (c); otherwise its existing criteria |
| loss beside a change | `udp-edge-drain` and `udp-edge-kill` again with `--client-loss "loss 5%"` and `"loss gemodel 1% 20%"` on the readers | 0 missing; probes pass (a) with the lossy steady state as the outside |

**Step 4, the LL-HLS reader.** With the new reader: `reader-rate-cap` (EC:179), `reader-asymmetric` (EC:205),
`mesh-flap-edge` (EC:359), `edge-freeze-resume` (EC:519), `distributors-both-freeze` (EC:679),
`contributor-freeze` (EC:730), `edge-cpu-starved` (EC:771), `slow-and-stuck-readers` (EC:1382) and
`edge-rolling-restart` (EC:535): their own criteria, and moves no higher than the w23 baseline on those without a
fault. New `reader-host-vanishes`: the busiest edge's links cut; every reader on it plays from another cache
within 400 ms + 8 r + 100 ms and none stalls with a 1 s buffer.

In-process (`cargo test`, no lab up): Y5's tests assert the same three things on the director's own record.

## 7. Deploy and tooling changes

- **Deploy render** (W1): the four settings of 2.3. `TimeoutStopSec=20` and `--stop-drain-ms 14000` on the mesh
  unit are C's.
- **Topology: three serving edges a region is a requirement of every edge row.** The referee's
  `topology-dedicated.json` has two; its change sessions need `SITE-edge-3` (+ $0.054/h a region; if Sydney
  refuses a third g6-dedicated-2 its change rows are reported as two-edge rows, 3.11). C's `topology-proving.json`
  must serve three edges a region with its standby and slot beyond them, and the autoscaler's `min_edges` is 3.
  The probe should name two entries (`--entry https://SITE-dist-a:19444,https://SITE-dist-b:19444`) so that a
  change at `dist-a` does not take its only entry.
- **Sizing, in legs.** With E serving edges in a region and K the lower of 800 permille and the measured knee: legs
  an edge <= K (E - 1)/E, so that one edge's loss leaves every leg a place under the gate. E = 3 and
  `client_udp_datagram_capacity` 200,000: 150 two-path listeners of 1 ms a region (100 legs an edge, 500
  permille; 750 with one edge gone). A crowd sized as one leg a listener (240 a region on two edges in C's draft)
  is over full load before any change.
- **scale-run.py / SCALE.md** (C edits; B supplies the numbers): a listener is 2,000 datagrams a second at 1 ms
  with two paths, so a listener host drives about half as many (about 500 per g6-dedicated-8 at 1 ms, to be
  measured by `cal`). `udp_mbps` per listener: 1.46 (Opus 1 ms), 5.17 (PCM), 0.84 (Opus 2.5 ms). The scale run's
  headline is `--paths 2` at 1 ms; `--load-args "--paths 1"` appears only as a labelled control. The capacity
  curve on one edge is run for both and replaces the table of 3.3. `settings` renders
  `client_udp_assign_confirms_per_source=4096`.
- **latency-bench.py** (W2), **edge-campaign.py** (W3): as section 5.
- **Referee hand-over, per build**: default client flags (nothing in `COMPARE_UC_EXTRA`); the controls are
  `COMPARE_UC_EXTRA="--paths 1"` and `"--compact-residual-ppm 2000"`; bytes per listener are
  `wire_bytes_per_part` and `wire_bits_per_second` in the client report; time on one path is `single_path_ms`.

## 8. Risks, what stays open, what is out of scope

Risks:

1. `UdpSession` is rewritten around legs. Guard: step 0 before anything else is measured, and the 17 existing
   session tests kept.
2. Edge load doubles per listener on clean paths and no figure for a g6-dedicated-2 is measured. The first Linode
   window must be the capacity curve with two paths, before any change scenario.
3. The hedge at 50 ms is new behaviour under load: a stalled edge's listeners arrive at their spares within 20 ms.
   `hedges_released` in steps 1 and 3 is the measure of false hedges; the knob is `--hedge-after-ms`.
4. The plan for two legs is new. The loss matrix (step 1) is its only proof; the burst case is the one most likely
   to need tuning.
5. Rule R3 (shed to the mean) makes rebalances common where they were rare. They cost a listener nothing, but each
   is three legs for about 3.5 r. `rebalance_moves` and `moves_taken_back` in `udp-edge-roll` and `slot-edge-join`
   show whether it converges; the flag turns it off.
6. A second leg adds a copy of a small stream at one more edge: one more lease at each forwarder per stream with
   listeners. Fixed per stream, not per listener.
7. Fewer simulated listeners per load host: the scale run's headline count halves.

Open, stated plainly:

- A region with two serving edges does not meet the rule's gap and p99 clauses after a kill, a crash-restart or a
  roll without a surge edge. Two paths are N+1 with a third edge to restore them; Linode gives no failure domains
  to pair edges across.
- Two edges of a region lost in the same 200 ms interrupt the listeners that had both legs there (late by
  220 ms + r, not lost).
- The time on one path after a kill (70 ms + r) is exposed to one edge's own jitter. Under a crowd in the local
  lab a one-edge gap beyond the rule's allowance comes every 5 to 9 s per edge, so a probe fails the kill row
  with a chance of about 2 to 3 % at r = 80 ms and 4 to 7 % at 300 ms; on quiet edges about 0.1 to 0.3 %.
- A run of more than 256 parts lost on legs that stayed live is not back-filled (`MAX_HOLE_RUN`), as today. An
  outage that silences every leg is, by the resume of 3.2, up to 4,096 parts.
- Burst loss on the last mile is common to both legs; the second path buys nothing against it.
- The join still spends three round trips; a token the entry could hand out for the edges would save one. Not
  designed here.
- The edge's handling of a gap range (seam A, 3) waits for A's final cache interface.
- `needletail-load`'s reset rule (L:546) and its miscounts after a long silence are not touched beyond the gap
  accounting; contributor scenarios will meet them (A and C).

Out of scope: LL-HLS readers on two edges (they get 3.10 only); `ntcmp hls`, which cannot follow a move (the
referee's tool); 4K on two paths; a third leg as a steady state; any per-listener state at an edge beyond the
subscription entry; any change to the mesh between forwarders and edges.

## Reviews answered

Protocol review (`B-listener.critique-protocol.md`):

| finding | verdict | why |
| --- | --- | --- |
| 1 BLOCKER, the skip set strands a listener in a roll and after a joint silence | accepted | B:1183-1200, M:19146-19163 and B:1382 are as cited. Skips end by cause, the longest-skipped cache is taken when all are, joint silence fails nobody, no client release timer on a delivering leg, verdicts only on an empty socket (3.2, 3.5). One correction: asking a live edge again from a part does not resend what the fast lane sent (E:2718-2725), so edge rule 8 is added; and the roll's gate reads `single_legs`, because with three edges nobody returns to a restarted edge by retrying |
| 2 MAJOR, a rebalance cannot see the other leg | accepted | A:301-311 and A:347-372 use tier 1 when tier 0 is empty. Tier 0 or nothing, take-back at once, the share returned (R2), and `o=` decided now rather than after a test: two and three edges a region are the referee's and the proving lab's topologies, where the other leg is the only receiver as a rule (R1) |
| 3 MAJOR, no round trip with doubled requests | accepted | S:665-669 and C:1571-1580 sample only unrepeated exchanges. Samples defined in 3.1 |
| 4 MAJOR, two legs start at different parts | accepted | E:2416, E:2157-2161, C:1378, C:1400: the floor, per-leg numbering, a stale leg failed (3.1) |
| 5 MAJOR, a 4K join makes a second edge send video | accepted | E:2346-2350. Decided at the entry by bit rate; first `TOKEN` wins for one leg (3.6, 3.7, 3.9) |
| 6 MAJOR, warm standby herds onto one edge and fires on a lost `WAIT` | accepted | A:384-396, E:221-224. The spare is chosen per session; a lone leg hedges at 170 ms; the spare's token is renewed every 15 s (3.8) |
| 7 MAJOR, the confirmation limit of 64 a second per address | accepted | E:1786, E:1887. `--client-udp-assign-confirms-per-source`, 4096 in labs, and a counter of its own because the second copy of every confirmation already counts as refused |
| minors 8 to 16 | accepted | 8: rules 1 and 5 (parents-lost take-back, drain re-marks, take-back names newest + 1, late `MOVE` ignored). 9, 10: 3.5 steps 5 and 9. 11: the joint-loss guard of 3.3. 12: the repair leg's bytes, window kept. 13: mover log key, confirmation list. 14: `o=` and rule 9. 15: newest + 1 beside a delivering leg (the cost review's 12 asks the same; its form is taken over "newest - 256"). 16: per-leg p99 and gap reported; three edges a requirement |

Cost and referee review (`B-listener.critique-cost-referee.md`):

| finding | verdict | why |
| --- | --- | --- |
| 1 BLOCKER, one path judged against a baseline on two, in labs with two serving edges | accepted | CW:88-92, CW:106-111; its table 0.1 recomputed from the recorded arrivals and reproduced. The claim is restated (0, 3.11), three serving edges are a requirement (7), `single_path_ms` is judged, a two-edge scenario is reported |
| 2 MAJOR, 200 ms + 3 r on one path after a kill | accepted | The spare and the hedge at 50 ms (3.8). One part refuted: the `--paths 1` bound "130 ms + r" cannot hold, a lone leg cannot be told from a quiet stream before two keepalives (E:221-224); it is 190 ms + r |
| 3 MAJOR, capacity counted as one leg a listener | accepted | A:286-291, E:2330. Sizing in legs, the gate on redundant legs, `edge-busy` for a `SUB` with `o=`, the published table, headline at `--paths 2`. One number changed: the gate is the rebalance target (800), not the receiver level (725), because the review's own sizing (500 permille at three edges, 750 after a loss) leaves one listener in fifteen on one path at 725 |
| 4 MAJOR, UDP `ASSIGN` in the load tool loses the charges | accepted | as protocol 7 |
| 5 MAJOR, two entries undo the apportionment | accepted | M:20480-20482, M:19356. One entry an ask; the second after 25 ms without an answer; requests beside a live leg spread over 250 ms; the confirmation to every entry that answered. Taken further: `ASSIGN` is never doubled |
| 6 MAJOR, `RESIDUAL_TARGET` at 0.2 % | accepted | The default stays 8,000 ppm (3.4); the row of the random-loss plan depends on the round trip (3.3) |
| 7 MAJOR, the client lets go at 3 s; the grace of 5 s | accepted | U:94, E:51. No client timer for a drain, grace 10 s, level relative to the leaving leg over 32 parts. One addition: after a `MOVE` that is not a rebalance a replacement that is steady for 1 s takes over though not level, or an edge 0.3 ms slower would hold every drain to the grace |
| 8 MAJOR, rebalance with `hold` | accepted | as protocol 2; `RebalanceState::returned`, the pause when more than half come back, `rebalance_moves` at the `UNSUB` |
| 9 MAJOR, the reader's stall change | accepted | R:1396-1412, A:384-396. The playlist is asked on the existing connection first; the next cache is opened only at the stall, spread over 100 ms; step 4 of section 6 |
| minors 10 to 15 | accepted | 10: the byte table rebuilt from measured payloads, first NACK to the nearer leg. 11: step 3 sized at 20 legs an edge with the capacity setting on. 12: as protocol 15. 13: `PlaylistHolding.sessions` counts UDP legs. 14: rule R3 and the roll's balance criterion. 15: `cpu_seconds` and the two-CPU pass |
