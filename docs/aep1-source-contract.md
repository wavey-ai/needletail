# The AEP1 source contract

What a source of AEP1 audio epochs (an `aep1-48k-probe send`, daw-nexus, any
encoder that feeds av-contrib's `--daw-media-bind`) must do so that two
contributors fed the same epochs publish the same parts, byte for byte
(A-contributor 2.1 to 2.3, 2.8). The mesh merges the two contributors by first
arrival and compares nothing but each part's key, so every byte a contributor
puts on the wire for an audio part is computed from what the source sent:
part number, playlist place, envelope times, deadline, block ids and packet
sequences. This contract is what makes that computation the same on both.

## The rules

They hold per rendition: one session id, one group id, one payload kind.

1. **Session.** `session_id` is the session's start in Unix nanoseconds and is
   never reused: a source that restarts takes a new, larger one. The probe
   refuses a session id more than 60 s old.
2. **Geometry.** `sample_rate` and `frame_count` do not change inside a
   session. A source that changes either starts a new session.
3. **Sample count.** `pts_samples` starts at 0 and counts the samples the
   source has sent in this session: each epoch's pts is the previous epoch's
   plus `frame_count`. It never follows a transport position (a DAW's
   playhead, a loop, a locate). An epoch may be sent late, or again, with its
   own pts. Any other step, back or forward, is a new session.
4. **Clock.** `session_id + pts_samples / sample_rate` stays within 35 minutes
   of Unix time (a client extends a part's 32-bit time around its own clock).
   A source idle for 30 s starts a new session.
5. **Not identity.** `epoch_id` and the SoundKit header id are not read to
   number anything (daw-nexus shares one `next_epoch_id` between its Opus and
   lossless epochs).

A new session is a new key epoch: one stream reset on every node, as any new
session is today.

## What a contributor does with the epochs

Part `N` of a rendition is `first_pts_samples / P`, `P` the part's samples
(`--audio-part epoch`: one epoch; otherwise the configured part rounded up to
whole epochs). A part is published only with all of its epochs. Its times are
the source's: Published and Packaged are `session_id + last_pts_samples / rate`
(the instant the source sent the part's last epoch); its deadline is that
time plus `--relay-deadline-ms`, a point on the stream's own timeline. A
contributor that starts, or restarts, while the source runs publishes the
running session's parts at their true numbers from its first complete part.

Input outside the contract is not published, and is counted per stream in
the contributor's `GET /api/node` (`streams[]`):

| Case | What the contributor does | Counter |
| --- | --- | --- |
| A number above every number published | publishes it | |
| A number inside the last 8,192, not yet published (a late track, an epoch completed by a late repair) | publishes it | `late_fills` |
| A number already published, or older than the window | drops it | `numbers_reused` |
| `frame_count` or `sample_rate` other than the rendition's first epoch | drops every part until a new session | `geometry_changed` |
| The part's time more than 2 s ahead of the contributor's clock (a pts that did not start at 0, a locate) | publishes it | `time_ahead` |

A drop logs one line at counts 1, 2, 4, 8 and so on. `time_ahead` is not a
refusal: a contributor's clock must not decide what is published, or two
contributors whose clocks differ would stop publishing the same parts. Its
cost: `hls-msn` and PROGRAM-DATE-TIME run ahead, and the next session's can be
lower. A contract counter that grows makes the gates of a kill, a hand-over and
a roll refuse (`scripts/contributor_changes.py`, K4).

## Sending to two contributors

One encoder, every datagram to each contributor in turn: two encoders with
one session id are not the same source (their Opus state differs).

- `aep1-48k-probe send --target A:27100 --target B:27100 ...`: `--target` is
  repeatable. Each datagram goes to every target before the next datagram; a
  failed send to one target is counted (`target_errors` in the report) and
  does not stop the others.
- daw-nexus: `DAW_NEXUS_AUDIO_EPOCH_EXTRA_TARGETS=ip:port[,ip:port]` beside its
  upstream; each datagram goes to the upstream and then to each extra target.
  An extra target's error is counted and is never the socket's failure.
  `daw-test-source` takes the upstreams as one argument, `A:27100,B:27100`.
- The addresses: the deploy tool's `plan` prints `contributor targets: <a>:27100
  <b>:27100` (each contributor's VLAN address, the origin first); in the local
  lab, `edge_lab.py sources` sends from `lon-src` to `10.77.1.5:27100` and
  `10.77.1.7:27100`.

## The sources as they stand

- **aep1-48k-probe** meets the contract as it is: `pts = k * frame_count` from
  0, paced on Unix time, a new session id at each start.
- **daw-nexus** meets it with its transport clock and session rules (A DN2,
  DN3): every new epoch of a session gets `pts = samples sent so far` whatever
  the project position (loop, rewind, locate, pre-roll), a late track keeps the
  pts it had, and a new session starts when a rendition's `frame_count` or
  `sample_rate` changes or when no epoch was sent for 30 s.
- A source of its own: follow the five rules; check with two contributors that
  `numbers_reused`, `geometry_changed` and `time_ahead` stay 0 and that the
  contributors' window digests (`streams[].windows`) agree.

## Not covered

- Video. A 4K feed goes to one contributor at a time and fails over by itself
  (`scripts/video-feed.sh`); its parts are not the same on two contributors,
  and each reconnect is one reset of the stream on every node.
- A redundant source. The source is not doubled; losing it is an outage.
- Authentication. AEP1 ingest is unauthenticated; a second target does not
  change that.
