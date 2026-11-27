# Two contributors: notes for the referee's harness

What the referee's harness (needletail-compare) needs once a lab has two
contributors, `contrib-lon` and the standby `contrib-lon-b`, both publishing
the same parts (A-contributor 7.5). All of it is small; nothing in the scorer
changes. Where A 7.5 quotes the pass rule of before 23 Nov, the referee's
current rule stands: `change-window.py` scores a change against the same
probe's own no-change windows and needs probes of 360 s or more.

## The sender: one encoder, both contributors

- `compare-audio.py`'s `aep1_command` and `compare-run.py`'s sender: every
  AEP1 epoch to both contributors, one `--target` each:
  `aep1-48k-probe send --target <contrib-lon>:27100 --target <contrib-lon-b>:27100 ...`
  (with `aep1_command` as it is, a target string of
  `f"{a}:27100 --target {b}:27100"` does it).
- `daw_command`: daw-test-source takes both upstreams as one argument,
  `<a>:27100,<b>:27100`.
- The addresses are each contributor's VLAN address, the origin first: the
  deploy tool's `plan` prints them (`contributor targets: <a>:27100 <b>:27100`);
  from a lab state, `small_mesh_common.contributor_targets(state)`.
- Two senders with one `--session-id` are not the way: their Opus state
  differs. The source contract: `docs/aep1-source-contract.md`.

## Numbering: nothing to change

Part N is epoch N of the session on both contributors, after a restart too, so
the Opus index (`number % units in 64 s`) holds through the loss of either.
The restart of the contributor at each configuration is no longer needed. If
it is kept: `linode-small-mesh-deploy.py restart --nodes contrib-lon` with no
stream flowing goes through workstream A's gate (an idle pair on one build may
lose either). `restart --roles contributor --set daw_hls_formats=...` between
configurations restarts both at once: fine, nothing is scored then.

## Before a headline run or a change row

Both contributors `ready` (their `/api/node`; `linode-small-mesh-deploy.py
status` prints each node's state and build) and on one build, so that the
headline runs and the kill rows use one configuration. A kill whose gate does
not see the other contributor carry every stream at both first forwarders
refuses with exit 2 and touches nothing: a refused row is a setup fault, not a
failed change.

## The change rows

Each is one command that returns when the change is complete. stdout carries
the change's two marks (`start <unix.ns> <label>`, `end ...`) and nothing
else; `--marks FILE` appends them to a file for `change-window.py --marks`.
Exit status: 0 done; 2 refused before anything changed; 3 stopped (the change
was made and its end was not seen, or the node did not come back ready).

| Row | Command | Label |
| --- | --- | --- |
| contributor kill | `linode-small-mesh-deploy.py kill --node contrib-lon` | `kill.contrib-lon` |
| (its restoration, not scored) | `linode-small-mesh-deploy.py return --node contrib-lon` | `return.contrib-lon` |
| contributor hand-over | `linode-small-mesh-deploy.py restart --nodes contrib-lon --hold-s 20` | `restart.contrib-lon` |
| rolling upgrade, contributors | `linode-small-mesh-deploy.py roll contributors` | `roll.contributors` (per node `roll.contributors.<node>` on stderr and `--node-marks`) |
| ingress kill | `linode-small-mesh-deploy.py kill --node lon-dist-a` | `kill.lon-dist-a` |
| ingress return (a row of its own) | `linode-small-mesh-deploy.py return --node lon-dist-a` | `return.lon-dist-a` |

- A killed node is held down (its unit is not restarted by systemd) until its
  `return`. `kill` takes a contributor or a first forwarder and refuses any
  other node; `--unprotected` skips the redundancy checks and the label gains
  `.unprotected` (such a row is an outage test, not a hitless one).
- A contributor that publishes the 4K stream is not killed, handed over or
  rolled without `--video-gap accept`: its loss is a 4K gap (below).
- Probes: 360 s or more, started before the command and running at least 15 s
  past its `end`, with about 300 s of steady probe outside the window for the
  null. `change-window.py` as it is.

## 4K

The throughput rungs are unchanged. A 4K fail-over row needs the feed loop on
the source host in place of one ffmpeg to one contributor:
`scripts/video-feed.sh --seconds N <contrib-lon> <contrib-lon-b> -- <ffmpeg
input and codec arguments> srt://{host}:27001?mode=caller&transtype=live&latency=80`
(or as a unit, `deploy/linode-small-mesh/needletail-video-feed.service` with
`/etc/needletail/video-feed.env`). It sends to the first contributor and moves
to the other when ffmpeg fails (a reset at once, or 2 s without a write), with
no fail-back. Expected and stated: 2.5 to 5 s without 4K, one reset of the
stream on every node per move. 4K is not hitless; audio is the hitless claim.
