#!/usr/bin/env python3
"""Deploy the Needletail mesh onto the small-instance Linode lab.

The relay DAG is described by a generated relay program and compiled by
`needletail-compile`, exactly as the larger qualification labs do. Its shape
is the `topology` setting: "merged" (the default; low-latency item 4) makes
relay and distributor one forwarder: contributor -> the two distributors of
its site -> every other distributor, fed by both of them; "legacy" is
contributor -> two backbone relays -> every distributor. Regional playback
edges are not compiled DAG members: they join their region's LL-HLS cache
mesh over the private VLAN and receive cache objects from the two regional
distributors, and (`edge_fast_plane`, the default) take the same two
distributors as fast-plane relay parents: each cache-mesh lease that a
distributor serves to an edge makes the edge a lease child of the stream,
fed the contributor's RaptorQ symbols as they arrive.

A lab may have a second contributor, the standby (a topology node of role
contributor with "standby": true, at the contributor's site and on its
VLAN). Both always publish the same parts: the standby is compiled as a
replica of the origin (`origin_standby`), with lanes of its own to the same
two first nodes, each of which takes it on its secondary lane. Two
contributors by two first nodes is the whole fan-out; the render refuses a
pair that would not publish the same parts (check_contributor_pair), and
`plan` prints the address of every contributor for the source to send to.

Subcommands:
  plan      measure routes, generate the relay program, compile it, and render
            one argument file per service under the artifact directory
  push      bootstrap hosts, stage the builds of the roots, TLS and the
            services' files, then activate (--nodes a,b: only those nodes)
  roll      TIER: one node at a time onto the roots' build, gated (C 3.5)
  restart   --nodes a,b: one node at a time, gated, on its recorded build
            (--rollback: its previous arguments); --roles: every node of a
            role at once, re-rendered from the current settings
  replace   --nodes X --budget-usd B: X on a new disk (or --mode new: a new
            instance), gated, on its tier's recorded build (C 3.7)
  start     --nodes a,b: take held-out nodes (standby edges, autoscale
            slots) into service and start them, and nothing else
  stop      --nodes a,b: hold nodes out and stop their service, and nothing
            else
  kill      --node X: a contributor or a first forwarder killed and held
            down, behind workstream A's gate (A 2.10)
  return    --node X: the node killed by `kill` started and made ready
  hunt      ECMP source ports of the long-haul links (a bring-up step)
  status    service state, build, readiness and the deployed record per node
  collect   --run-id ID [--nodes a,b] [--timeout-s N]: every node's journal
            since boot, its API at the end, its samplers' files, and the
            deployed record, settings, topology and lab state, into the run
            archive (~/needletail-data/<run id>/), marked collected or not

What is deployed is recorded in the lab state (`deployed`): the settings,
the routes and hunted ports, and each node's build and arguments (with the
`--set` values a node runs beyond the settings until every node runs them,
and an activation begun and not seen to end, which the next command settles
by what the node runs). A node runs a build from
/opt/needletail/builds/<git>-<unix>/, staged beside the one in force and
put in force by /opt/needletail/activate with a deadline; `roll`, `restart
--nodes` and `start --nodes` refuse settings that do not reproduce the
record. settings.json, routes.json and ecmp-ports.json name the lab they
belong to and are refused for another.

Joining without a restart (low-latency ops, D3). Every peer list is
rendered from the lab's roster, not from the nodes in service: every edge
and distributor of the lab, the standby edges, and the autoscale slots of
the topology (lab state "slots"), created or not. A node that is held out
or not created yet is a peer that is down: telemetry peers retry every
second and the cache mesh ignores a peer not heard within 2 s. So the
arguments of every node are the same whichever of the roster serve, and
`start --nodes` (or `push --nodes` for a node just created) starts the one
node and restarts nothing. An autoscale slot of role distributor (dist-c)
is a fast-plane lease child of its site's first two distributors and a
cache-mesh parent of its edges; it is never a compiled relay child, so no
other node's relay ports move.

Tuning knobs live in the settings file and can be changed with
`--set key=value` on plan, push, or restart.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import contextlib
import fcntl
import filecmp
import functools
import gzip
import hashlib
import importlib.util
import json
import os
import pathlib
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import traceback
import zlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import archived_run  # noqa: E402
import contributor_changes  # noqa: E402
import mesh_ops  # noqa: E402
import run_archive  # noqa: E402
import small_mesh_common as common  # noqa: E402

SETTINGS_FILE = common.ARTIFACT_DIR / "settings.json"
# Version 2: parts are the smallest frame by default and given in µs, samples
# or frames; segment and window are lengths of time. A settings file of
# version 1 named 5 ms parts and counted segments and windows in parts.
SETTINGS_VERSION = 2
# The part settings of version 1. They are still accepted and mapped onto the
# new ones (see part_plan). A value saved in a file of version 1 is dropped on
# load (it was the 5 ms default then), so that an old settings.json never
# silently overrides the smallest-part defaults; one saved by a version 2 run
# was given with --set and is kept until changed with --set (KEY=null clears).
LEGACY_PART_KEYS = ("part_ms", "parts_per_segment", "window_parts", "fmp4_part_ms",
                    "fmp4_video_part_ms")
DEFAULT_SETTINGS = {
    "settings_version": SETTINGS_VERSION,
    # The nominal part of the caches (av-mesh --part-us): 2.5 ms, one Opus
    # frame of 120 samples. Lossless parts are smaller still (one AEP1 epoch
    # of `pcm_epoch_samples`); the caches take each part's own duration.
    "part_us": 2500,
    # Segment and playlist window as lengths of time (av-mesh --segment-ms,
    # --window-ms), whatever the part.
    "segment_ms": 1000,
    "window_ms": 12000,
    # PART-HOLD-BACK floor in ms; 0 is 3 x PART-TARGET.
    "part_hold_back_ms": 0,
    # Version 1 part settings (None: not used). `part_ms` maps to `part_us`,
    # `parts_per_segment` and `window_parts` win over `segment_ms` and
    # `window_ms`. A value given with --set is kept by later runs; give
    # KEY=null to clear it.
    "part_ms": None,
    "parts_per_segment": None,
    "window_parts": None,
    # The largest part a cache slot takes (av-mesh --slot-kb). A larger part
    # is refused by every cache: never in a playlist, never answered to a
    # NACK, and a UDP client gets it from the fast lane alone. Wave 9 D: 8 MiB
    # (2048 before): the first frame of a 4K encode at 160-300 Mbit/s is
    # 4.6-5.9 MB (the lab's 4K clips lost their loop point's parts 0-3 at
    # every cache). A limit, not an allocation.
    "slot_kb": 8192,
    # Playlists that a node holds at one time: the streams of a region for a
    # relay and a distributor. An edge holds one less for its readers.
    "playlist_slots": 16,
    # Tracks of the audio album that the lab source plays (1, 2 or 8). Each
    # track is a lossless and an HQ stream. Only the lab tools read this.
    "daw_album_tracks": 1,
    "response_ms": None,
    "mesh_sync_interval_ms": 20,
    "mesh_repair_ratio": 0.03,
    "mesh_repair_symbols": 1,
    "mesh_max_repair_symbols": 32,
    "mesh_symbol_size": 1316,
    "mesh_max_symbol_size": 0,
    "cache_mesh_max_parents": 2,
    # Edges that one distributor serves in a sync tick (av-mesh
    # --cache-mesh-max-children truncates its peers to this many). A
    # distributor is rendered with at least the number of edges of its site,
    # so a site of more edges is never silently cut short.
    "cache_mesh_max_children": 8,
    # Regional cache features: a distributor pushes a playlist to an edge only
    # while the edge has readers for it ("demand"), or always ("all").
    "cache_mesh_replication": "demand",
    "cache_mesh_lease_ms": 5000,
    "cache_mesh_demand_idle_ms": 30000,
    "cache_mesh_warm_parts": 0,
    # The warm-up of a new copy as a length of time (low-latency ops D6): a
    # listener that moves onto a cold edge resumes from the part after its
    # last, which is up to the silence detection (1 s) plus the move behind
    # the head. None: cache_mesh_warm_parts as given. A number of ms: the
    # warm-up window in parts of the shortest audio part (so every stream
    # gets at least that long; one of longer parts gets more).
    "cache_mesh_warm_ms": None,
    "cache_mesh_warm_kbps": 20000,
    "cache_mesh_socket_buffer_bytes": 0,
    # Limits of one edge. They are what admission and assignment enforce.
    "edge_egress_capacity_bps": None,
    "edge_session_limit": 0,
    # Client UDP subscriptions that a node takes at most
    # (--client-udp-max-subscribers; None: the binary's default, 4096). The
    # scale run raises it so that what gives out is the edge, not its cap.
    "client_udp_max_subscribers": None,
    # Nodes that are held out of service: no service (push and restart stop
    # one that runs; `stop --nodes` adds to this list, `start --nodes` takes
    # from it). They stay in every peer list, the fast-plane feed and the
    # telemetry peers (a peer that is down), so a node taken into service
    # with `start --nodes` restarts nothing else. Standby edges and created
    # autoscale slots; the scale plan's second curve starts its edges with
    # `start --nodes`.
    "standby_edges": [],
    # RaptorQ FEC UDP to clients, on every distributor and edge (0: off).
    # The assignment API of a node names this port.
    "client_udp_port": 19446,
    # Address in the playback URL that a cache advertises: "public", or "vlan"
    # when readers reach the caches over the private network.
    "playback_address": "public",
    "assignment_spread_percent": 60,
    "assignment_alternates": 2,
    "steering_ttl_seconds": 5,
    # Edges of a region that hold each playlist that has readers. With two, a
    # reader whose cache fails moves to a cache that holds the playlist.
    "playlist_copies": 2,
    # Name the first distributor of every other region as the last pathway of
    # each reader, so that a reader moves there when its whole region is lost.
    "region_failover": True,
    # The renditions of each ladder, as the caches name them in a Multivariant
    # Playlist: "master:stream:peak bit/s:average bit/s:WIDTHxHEIGHT:frame
    # rate in mHz". The contributor receives each rendition as its own stream.
    "hls_renditions": [],
    "telemetry_interval_ms": 1000,
    # Kernel settings of a node that serves readers. A reader far from its
    # cache gets a 4K playlist only when the cache finds the rate of the path
    # quickly: CUBIC leaves slow start early on a path with jitter and then
    # needs ten seconds to reach 16 Mbit/s. A live stream also pauses between
    # parts, and the window must not close in each pause.
    #
    # The cache network sends a 4K part in symbols of up to 16 kB, which the
    # kernel fragments. A datagram that has lost a fragment waits 30 s for it
    # by default, and when 4 MB of fragments wait, the kernel drops every new
    # fragment. With 2% packet loss a node had 1 MB of such fragments a
    # second: 40% of its fragmented datagrams failed to reassemble, and 4K
    # stopped on every edge while audio, which is not fragmented, played. One
    # second and 64 MB keep the queue empty.
    #
    # The kernel also drops a datagram when more than 64 other fragments of
    # the same sender arrive between two of its fragments. A distributor
    # sends thousands of fragments a second, so a link that reorders packets
    # by a few milliseconds lost most 4K symbols. Zero turns that check off.
    "node_sysctls": {
        "net.ipv4.tcp_congestion_control": "bbr",
        "net.ipv4.tcp_slow_start_after_idle": "0",
        "net.ipv4.ipfrag_time": "1",
        "net.ipv4.ipfrag_high_thresh": "67108864",
        "net.ipv4.ipfrag_max_dist": "0",
    },
    "path_loss_ppm": 20000,
    # The contributor's legacy AEP1 lanes (wave 7 item 1): each AEP1 epoch
    # re-framed as SoundKit v2 with four repair datagrams and sent to the two
    # first forwarders (--audio-epoch-ingress-target and -redundant-), which
    # relay it on their slow path. Nothing on the RelaySession path consumes
    # it (edges, LL-HLS and UDP clients take RLS1); only WebTransport
    # WAVEY-AUDIO-EPOCH readers and native relay subscribers at a distributor
    # would. False renders no AEP1 target, and av-contrib's
    # --audio-epoch-mesh-lane auto (under --relay-exclusive, with no target)
    # sends no lane: 10 of the contributor's 16 datagrams per 1 ms part, and
    # the same 10 on each first forwarder's long haul. True renders the
    # lane's targets as before, and auto keeps the lane with them. No
    # --audio-epoch-mesh-lane is rendered, so an av-contrib older than the
    # flag still starts; one that the render finds without it (see
    # RENDER_BINARIES) gets the targets as before. linode-small-mesh-run.py
    # and -soak.py start their AEP1 readers at the distributors only with
    # the lane.
    "audio_epoch_lane": False,
    # Wave 7 items 2, 4 and 5. None renders nothing: the binaries' defaults
    # (the new behaviour) hold.
    # - relay_audio_copies: av-contrib --relay-audio-copies, the copies of a
    #   one-symbol (1 ms) audio part: "measured" (the default: sized to the
    #   loss that the first forwarders report on each lane, RLR1, none on a
    #   clean lane), "configured" (path_loss_ppm), "policy" (as before: two
    #   repair copies at 2 %).
    # - cache_mesh_fast_plane_push: av-mesh --cache-mesh-fast-plane-push on
    #   the distributors: "none" (the default: no cache-mesh push of an
    #   audio part to an edge that the fast plane feeds), "secondary" (only
    #   the edge's secondary distributor pushes), "all" (as before).
    # - relay_demand_renditions: --relay-demand-renditions of the
    #   contributor and every relay node: groups of renditions of one source
    #   ("2001,3001,4001;..."), of which a lane carries only what its
    #   subtree reads (or everything while it reads none of the group).
    #   "auto": the DAW lane's FLAC/Opus/PCM from daw_hls_base_stream_id.
    #   None (the binaries' default, empty): nothing is held back; the first
    #   reader of a held rendition does not start cleanly yet (wave 7 item 5
    #   in the low-latency results).
    "relay_audio_copies": None,
    "cache_mesh_fast_plane_push": None,
    "relay_demand_renditions": None,
    # Wave 8 stage 3. cache_mesh_fast_plane_video_push: av-mesh
    # --cache-mesh-fast-plane-video-push on the distributors, the same for a
    # video stream: "none" (the binary's default: no push of a video stream
    # to an edge that the fast plane feeds it to; the edge's failover and
    # hole repair cover a lost primary), "secondary" (the distributor that
    # feeds the edge the sources does not push; the one that feeds it repair
    # does), "all" (as before wave 8: an edge took 4K four ways, and every
    # edge dropped at its cache-mesh video socket at 160 Mbit/s in the local
    # lab). None renders nothing.
    "cache_mesh_fast_plane_video_push": None,
    # - relay_failover_promotion_backoff: av-mesh
    #   --relay-failover-promotion-backoff on every node with a failover
    #   controller (the edges): each promotion of the warm secondary within
    #   30 s doubles the primary silence the next one needs (S6: no switch,
    #   and no doubled video, for each short stall of a loaded forwarder).
    #   None renders nothing (the binary's default, on); false is the A/B.
    "relay_failover_promotion_backoff": None,
    # Wave 7 items 3 and 9. None renders nothing: the binaries' defaults (on)
    # hold; false is the A/B against wave 7.
    # - relay_compact_audio: av-contrib --relay-compact-audio, a 1 ms audio
    #   part as a compact RLC1 datagram (a slim object against the stream's
    #   descriptor, about 75 bytes and the part instead of about 850), whole
    #   every --relay-compact-whole-ms (100).
    # - client_udp_compact_short_header: the edges' --client-udp-compact-
    #   short-header, a part on the compact client lane with a 7-byte header
    #   instead of 11 to the clients that ask for it (needletail-udp-client
    #   --compact-short-header, on by default).
    # An av-mesh older than the compact relay datagram (6163c41) reads only
    # the whole parts that a contributor sends every 100 ms: with
    # relay_compact_audio None, a render that finds the av-contrib it
    # deploys with --relay-compact-audio and the av-mesh without RLC1 (no
    # --client-udp-compact-short-header) renders --relay-compact-audio false
    # (see RENDER_BINARIES). Restarting a new contributor alone in front of
    # older forwarders and edges is not caught: set it false then. A new
    # needletail-udp-client asks an older edge with the long form again.
    "relay_compact_audio": None,
    "client_udp_compact_short_header": None,
    # Wave 8 stage 2, the nodes that serve UDP clients (--client-udp-bind).
    # None renders nothing (the binaries' defaults); false is the A/B.
    # - client_udp_repair_thread: the real-time class thread takes no lock
    #   that ordinary tasks hold (window repair on a repair thread, NACK
    #   answers sent after the subscriber table is released; S1).
    # - client_udp_batched_admission: a subscribe spike rebuilds the fast
    #   lane's client snapshot once a batch (S3).
    # - client_udp_resume_from_request: a client that moved here resumes at
    #   the part it names while the cache holds it (S2).
    # - client_udp_fanout_inline_shard: the class thread sends one shard of
    #   a handed-off batch itself (B4).
    "client_udp_repair_thread": None,
    "client_udp_batched_admission": None,
    "client_udp_resume_from_request": None,
    "client_udp_fanout_inline_shard": None,
    # Wave 8 stage 4 (Phase AM 8, 9, 11), the nodes that serve UDP clients.
    # None renders nothing (the binaries' defaults, all on); false or 0 is
    # the A/B.
    # - client_udp_egress_load: the client UDP lanes' bytes count in the
    #   advertised load_permille and free capacity (AM 8; the autoscaler's
    #   trigger and assignment see UDP load).
    # - client_udp_admit_by_load: an overloaded edge refuses a new UDP
    #   subscription (edge-busy) but takes a client that moves to it (AM 8).
    # - client_udp_drain_spread_ms: a draining edge sends its UDP clients
    #   MOVE draining spread over this long (AM 9; av-mesh default 2000).
    # - client_udp_region_hold_ms: a region-wide silence this long, with a
    #   --failover-entry region fresh, sends the UDP clients there (AM 11;
    #   default 2000; needs region_failover).
    "client_udp_egress_load": None,
    "client_udp_admit_by_load": None,
    "client_udp_drain_spread_ms": None,
    "client_udp_region_hold_ms": None,
    # Wave 8 stage 5 (Phase AM 12): a distributor slot (dist-c) is a
    # fast-plane lease child of its site's first two distributors AND a
    # lease parent of the site's edges: it feeds them on its class threads
    # (--relay-lease-bind), subscribes at its two forwarders to every
    # playlist its children lease from it (--cache-mesh-upstream-demand),
    # and it and they list each other as cache-mesh peers. The edges rank
    # the live distributors of their site per playlist and the first two
    # feed them (--relay-lease-ranked, --relay-ranked-parents in av-mesh).
    # False: the slot feeds no lease child, as before.
    "forwarder_slot_feeds_edges": True,
    # Wave 8 stage 5 (AM 12, 13) on every mesh node. None renders nothing
    # (the binaries' defaults, all on; the total warm-up cap 40,000 kbit/s);
    # false or 0 is the A/B.
    # - relay_lease_ranked: a lease's rank decides primary or secondary.
    # - relay_ranked_parents: an edge admits every distributor of its site.
    # - cache_mesh_subscribe_rank: Subscribe frames carry the rank.
    # - cache_mesh_release_unranked_parents: a parent that leaves a
    #   playlist's ranked set is released at once.
    # - cache_mesh_avoid_draining_parents: a draining distributor ranks last.
    # - cache_mesh_warm_total_kbps: a distributor's warm-up windows together.
    "relay_lease_ranked": None,
    "relay_ranked_parents": None,
    "cache_mesh_subscribe_rank": None,
    "cache_mesh_release_unranked_parents": None,
    "cache_mesh_avoid_draining_parents": None,
    "cache_mesh_warm_total_kbps": None,
    # Wave 8 review fixes. None renders nothing (the binaries' defaults);
    # a value is the A/B.
    # - client_udp_keepalive_ms (nodes that serve UDP clients): WAIT to the
    #   subscribers of a quiet stream this often (av-mesh default 80, under
    #   the clients' 200 ms move silence; 0: 300 ms, before).
    # - client_udp_datagram_capacity (same): client datagrams a second that
    #   make an edge full; each counts in the load as at least capacity / 8
    #   / this many bytes (0: bytes alone, the default). scale-run settings
    #   --datagram-capacity sets it for the autoscale phases.
    # - client_udp_region_head_check (same): a failover region is fresh for
    #   MOVE region-lost only with parts newer than this region's head.
    # - cache_mesh_warm_second_parent (every mesh node): with a slot among
    #   the live distributors, the second parent warms a new copy too.
    # - relay_demand_report_lease_parents (relay nodes): edges and slots send
    #   RLD1 to their lease parents too (false by default: F-I1).
    # - relay_lease_drain_inbound (forwarders with --relay-lease-bind): read
    #   and count what arrives on the lease sockets.
    # - relay_class_fallback_nice (relay nodes): the nice step of the other
    #   threads where SCHED_FIFO is refused (av-mesh default 0, off).
    "client_udp_keepalive_ms": None,
    "client_udp_datagram_capacity": None,
    "client_udp_region_head_check": None,
    "cache_mesh_warm_second_parent": None,
    "relay_demand_report_lease_parents": None,
    "relay_lease_drain_inbound": None,
    "relay_class_fallback_nice": None,
    # Wave 8 local proofs: assignment_udp_in_region (every mesh node, the
    # entry points of /api/assignment): a UDP client that no cache has room
    # for by the entry point's estimate spills within its region rather than
    # being sent to the failover region (av-mesh default true; false: before).
    "assignment_udp_in_region": None,
    # Wave 9 (cold start), each the av-mesh flag of the same name (None: the
    # binary's default): malloc_tune and prefault (every mesh node, default
    # true), and
    # client_udp_prompt_sender (nodes that serve UDP clients: the stream's
    # sender does not wait for a playable copy), relay_lease_descriptors
    # (forwarders with --relay-lease-bind: a new lease child gets the
    # stream's descriptor before its first compact part).
    "malloc_tune": None,
    "prefault": None,
    "client_udp_prompt_sender": None,
    # Wave 9 B (time to first audio under loss): the nodes that serve UDP
    # clients answer a client's UDP ASSIGN at their client UDP port with the
    # /api/assignment answer (av-mesh --client-udp-assign, default true; None:
    # the binary's default). needletail-udp-client asks its entry point there
    # (its --assignment-udp-port, default 19446 = client_udp_port) and over
    # HTTPS 50 ms later, and takes the first answer.
    "client_udp_assign": None,
    "relay_lease_descriptors": None,
    # Wave 9 D (4K): relay_video_gro, the av-mesh flag --relay-video-gro on
    # the relays, distributors and edges with class threads (None: the
    # binary's default, true): their video sockets take UDP GRO, so a
    # parent's GSO burst is one receive message, not one per symbol.
    "relay_video_gro": None,
    # The audio fan-out's GSO (the stall hunt's GSO/GRO matrix): av-mesh
    # --client-udp-gso on the nodes that serve UDP clients (None: the
    # binary's default, true): with batch send, a client's datagrams of a
    # part leave the edge as one UDP GSO message. The fast lane's own audio
    # sends are sendmmsg without GSO (its video sends follow
    # relay_video_gso); the receiving side's GRO is needletail-udp-client
    # --gro.
    "client_udp_gso": None,
    # cache_mesh_video_warm_parts, av-mesh --cache-mesh-video-warm-parts on
    # every mesh node (None: the binary's default, 0): the warm-up window a
    # distributor sends a new copy of a video playlist, in parts, or
    # "window" for the audio window as before.
    "cache_mesh_video_warm_parts": None,
    # Wave 9 C (autoscaling and redistribution), each the av-mesh flag of the
    # same name (None: the binary's default). Every mesh node: cpu_load (an
    # edge's load_permille is at least its process's CPU over its CPUs, full
    # at cpu_load_full_percent, default 85), assignment_move_headroom (a
    # moving UDP client is assigned by the caches' headroom, those under
    # rebalance_target_permille first), rebalance_target_permille (default
    # 800). The nodes that serve UDP clients: client_udp_rebalance (an edge
    # over the target sends listeners to edges of its region with room,
    # MOVE rebalance), client_udp_rebalance_margin_permille (150) and
    # client_udp_rebalance_interval_ms (3000).
    "cpu_load": None,
    "cpu_load_full_percent": None,
    "assignment_move_headroom": None,
    "rebalance_target_permille": None,
    "client_udp_rebalance": None,
    "client_udp_rebalance_margin_permille": None,
    "client_udp_rebalance_interval_ms": None,
    # A listener held on two edges (resilient mesh, workstream B 2.3). Each
    # is the av-mesh flag of the same name, and the value here is that
    # flag's default. A flag is rendered only for an av-mesh that has it
    # (binary_takes), so an older build runs as it did; null renders
    # nothing. On the nodes that serve UDP clients:
    # - client_udp_move_grace_ms: how long an edge still serves a
    #   subscriber that it has sent MOVE (a drain, a rebalance), until its
    #   UNSUB or this long; 0: removed at the MOVE, as before (A/B only).
    # - client_udp_assign_confirms_per_source: the assignment
    #   confirmations that a node takes from one source address. A lab that
    #   runs needletail-load sets 4096: a load host confirms for every one
    #   of its listeners from one address.
    # - client_udp_rebalance_to_mean: an edge also sheds listeners when it
    #   is over its region's mean load by half the rebalance margin, so that
    #   an edge that joins or restarts takes legs; false: only over the
    #   rebalance target, as before.
    # On every node of the cache mesh (each can be the entry point that
    # assigns):
    # - assignment_two_path_max_bps: a playlist whose regional bit rate is
    #   above this is given one leg, whatever its client asks (4K; 1 ms PCM
    #   is 2.6 Mbit/s a leg).
    "client_udp_move_grace_ms": 10000,
    "client_udp_assign_confirms_per_source": 64,
    "client_udp_rebalance_to_mean": True,
    "assignment_two_path_max_bps": 8000000,
    "relay_min_repair_symbols": 0,
    # Send each part from the contributor straight to the distributors of its
    # own site too, one hop less than through a backbone relay (av-contrib
    # --relay-direct-target; each such distributor would get
    # --relay-direct-peer). Off, and refused when set: av-mesh has no
    # --relay-direct-peer or --relay-direct-id yet (a distributor started with
    # them would not start), and it rejects and logs every datagram from an
    # address that is not a backbone parent. See AV_MESH_DIRECT_PARENT.
    "contributor_direct_region": False,
    # The relay DAG (low-latency item 4). "merged": relay and distributor are
    # one forwarder. The two distributors of the contributor's site (by
    # name) are the first forwarders, fed by the contributor directly on its
    # two lanes, and every other distributor is a regional relay fed by both
    # of them, crossed (the first of a site: primary the first forwarder,
    # secondary the second; the next the mirror), with a repair lane and
    # failover control from its secondary. Three links in the contributor's
    # region: contributor -> distributor -> edge -> reader. Backbone relays
    # are not used: a node of role backbone gets no service, and push and
    # restart stop the one that runs. "legacy": contributor -> the
    # *-primary and *-secondary backbone relays -> every distributor.
    # Edges stay on the regional cache mesh in both.
    "topology": "merged",
    # Edges of a site that its distributors feed in the merged topology;
    # past it the site would need a level of regional relays, which the
    # deploy does not build yet, so plan refuses.
    "max_edges_per_forwarder": 16,
    # Edges fed by the fast plane (low-latency B5/B6/B9; wave 5 W0): each
    # distributor of a site is a forwarder of the site's edges as well. A
    # cache-mesh lease that it serves to an edge makes the edge a lease
    # child of the stream on its class threads (--relay-lease-bind on the
    # VLAN, port 22901), which hands it the contributor's RLS1 symbols as
    # they arrive. Each edge binds its site's two distributors as relay
    # parents (--relay-controlled-local, primary on 22001, secondary on
    # 22002, video on both plus class_port_offset, audio in full from both),
    # alternating which is primary so that each is primary for half of the
    # edges; the primary sends video in full and the secondary its repair,
    # until the edge's failover controller (22903) promotes it on its lease
    # failover listener (22902). The edge's class threads send each symbol
    # on to its fast-lane UDP clients (--client-udp-edge-rank: its place
    # among the edges of its site), and its tee commits the part for LL-HLS.
    # The cache mesh stays beside it. Needs the relay class split (the class
    # threads); off with it. False: edges are fed by the cache mesh alone,
    # as before.
    "edge_fast_plane": True,
    # --client-udp-fast-lane of every node that serves UDP clients: a node
    # with class threads sends the RLS1 symbols of a part on to the clients
    # that asked for the fast lane as they arrive. False keeps the
    # cut-through and the cache-fed lane alone.
    "client_udp_fast_lane": True,
    "failover_primary_silence_ms": 100,
    "failover_primary_recovery_ms": 500,
    "failover_secondary_warm_ms": 300,
    "failover_heartbeat_ms": 25,
    "failover_lease_ms": 300,
    "media_deadline_ms": 1000,
    # --wall-clock-estimated-error-ms of every contributor, in ms: the
    # confidence of the clock in each audio part's envelope. It is part of
    # what two contributors must render alike (check_contributor_pair): a
    # contributor on another value publishes another object version, and the
    # first forwarders merge nothing of the two.
    "contributor_clock_error_ms": 5,
    # --canonical-gap-hold-ms of a distributor outside the contributor's
    # region, fed over a long-haul path (low-latency A5). A hole that it
    # holds stops the contiguous watermark, and so cache-mesh replication to
    # every edge of its region, until the hole is filled or the hold ends.
    # The referee's long-haul test (mesh-w, av-mesh default 1 s) measured
    # the cost: under 1%, 5% or burst loss every 1 ms PCM part waited
    # 0.88-0.98 s at the New York, Tokyo and Sydney distributors. A 400 ms
    # hold caps that wait. The price: the contributor still repairs a part
    # for media_deadline_ms (1 s), and a part that reaches this distributor
    # more than the hold late is stored but not replicated; its edges get it
    # only by hole repair (a hole at most 500 ms behind their head) or the
    # once-a-second replica request, so live readers miss it and a 4K reader
    # loses that part's frame. load_settings says so whenever the hold is
    # below media_deadline_ms. (It is not a cure for edges moving their
    # clients: that was the edges' liveness, fixed by A1/A2.) The
    # contributor's region keeps the default. None or 0 renders nothing (the
    # av-mesh default everywhere).
    "canonical_gap_hold_long_haul_ms": 400,
    # The repair horizon of the live path (wave 5 W2, av-mesh
    # --canonical-repair-horizon on every mesh node): a hole holds the
    # parts behind it (the LL-HLS playlist, the cache-mesh push, the
    # cache-fed UDP lane) only this long after the first part beyond it
    # came, then they are published and the hole is listed as a gap
    # (EXT-X-PART GAP=YES, the UDP gap object); a repair that comes later
    # still fills the cache. "auto": twice the repair delay that the node
    # measures (10-250 ms, 60 ms before it has measured one); a number of
    # milliseconds; "off": the gap hold above alone, as before. Never longer
    # than the gap hold.
    "canonical_repair_horizon": "auto",
    # Burst copies on the long-haul lanes (wave 5 W2, av-mesh
    # --relay-burst-copy-ms on the nodes with class threads): each audio
    # source symbol forwarded to a compiled child goes again this many ms
    # later, so a loss burst shorter than that cannot take a part at the
    # next forwarder. Costs the audio sources once more on those lanes. 0:
    # no copies.
    "relay_burst_copy_ms": 20,
    # Class isolation (low-latency item 2): at no node does an audio datagram
    # share a socket, a thread or a qdisc band with 4K. Video streams travel
    # on the relay and cache-mesh ports plus `class_port_offset` (22001 ->
    # 23001, 29101 -> 30101), audio on the ports themselves. False renders
    # these three pieces off; the audio hot path, repair on both lanes, the
    # host qdisc and the capabilities below are settings of their own (see
    # `low_latency_baseline` for all of it at once). Each piece can be set on
    # its own (None follows class_isolation):
    # - relay_class_split: the contributor's class lane sockets
    #   (av-contrib --relay-class-lanes) and the per-class forward-first
    #   threads of relays and distributors (av-mesh --relay-class-threads).
    #   They share the port convention, so they go together.
    # - cache_mesh_class_split: video playlists on a second cache-mesh socket
    #   (--cache-mesh-class-port-offset; 0 is off).
    # - client_udp_class_sockets: the client UDP port as an audio/video
    #   socket pair (--client-udp-class-sockets).
    "class_isolation": True,
    "class_port_offset": 1000,
    "relay_class_split": None,
    "cache_mesh_class_split": None,
    "client_udp_class_sockets": None,
    # CPUs of the audio and video fast-plane threads of a relay or
    # distributor, "AUDIO,VIDEO": "auto" pins them to 0,1 on an instance of
    # two or more vCPUs (from its Linode type) and leaves them unpinned
    # elsewhere (the namespace lab shares one host); "" never pins.
    "relay_class_cpus": "auto",
    # SCHED_FIFO 10 for the audio thread, nice -10 for the video one
    # (refused without CAP_SYS_NICE: normal threads and one info line).
    "relay_class_realtime": True,
    # SO_MAX_PACING_RATE of the contributor's video sockets in Mbit/s under
    # the fq band (0: not paced).
    "relay_video_pacing_mbps": 0,
    # 4K symbols leave the contributor's video lanes and the relays' and
    # distributors' video fast-plane threads as UDP GSO messages (up to 64
    # symbols per pass through the kernel's stack instead of one each):
    # av-contrib and av-mesh --relay-video-gso. The 4K regression of wave 2
    # was relays saturated by one send per symbol per child. False sends
    # one datagram per symbol, as before.
    "relay_video_gso": True,
    # The contributor audio hot path (low-latency item 3): audio parts are
    # published on the packager's thread and each lane takes its symbols with
    # non-blocking sendmmsg, with no join across lanes
    # (--relay-audio-inline, --audio-hot-thread). `audio_hot_cpu` "auto" pins
    # that thread to CPU 2 on an instance of three or more vCPUs, else -1.
    "audio_hot_path": True,
    "audio_hot_cpu": "auto",
    # Repair on both contributor lanes (--relay-repair-both-lanes); the
    # primary backbone then admits repair (--relay-primary-promoted).
    "relay_repair_both_lanes": True,
    # Host qdisc of every mesh node and the contributor: "prio" is a 3-band
    # prio root on each NIC, band 0 (SO_PRIORITY 6: the audio sockets) a
    # pfifo and band 1 (priority 0: video, HTTP) an fq, so a 4K burst never
    # sits ahead of audio in the NIC queue. "none" puts the kernel default
    # back.
    "host_qdisc": "prio",
    # Give the services CAP_SYS_NICE (SCHED_FIFO, nice -10, pinning) and
    # CAP_NET_ADMIN (socket buffers beyond rmem_max) in their units.
    "service_capabilities": True,
    # The behaviour from before low-latency items 2 and 3, for an A/B against
    # it: true renders every piece above off, whatever it is set to (the
    # three class pieces, audio_hot_path, relay_repair_both_lanes,
    # host_qdisc "none", service_capabilities).
    "low_latency_baseline": False,
    # Part of the contributor's audio (av-contrib --audio-part): "epoch", one
    # AEP1 epoch per part whatever its sample count, or "<N>samples" or
    # "<N>us". Video (--video-part): "frame", one access unit per part, or
    # "<N>ms". Larger parts are a setting, never the default.
    "audio_part": "epoch",
    "video_part": "frame",
    # Epoch of the lab's AEP1 sender (aep1-48k-probe send --epoch-samples) and
    # of the DAW source's Opus frames: 48 samples (1 ms) for PCM and FLAC, and
    # 48 (1 ms Opus Custom, the low-latency Opus lane's default) for Opus.
    # 120 (2.5 ms) is standard Opus, for anything that must interoperate
    # (fMP4, WebRTC). Only the lab tools read these.
    "pcm_epoch_samples": 48,
    "opus_epoch_samples": 48,
    # Opus bitrate (CBR) of the lab's Opus sources (daw-test-source and
    # aep1-48k-probe send --opus-bitrate): 320 kb/s, the lane's default, the
    # smallest rate at which 1 ms frames are at least as good as standard
    # 5 ms at 192 kb/s; 256000 is the lower-bandwidth option. Lab tools only.
    "opus_bitrate": 320000,
    # Version 1: `fmp4_part_ms` maps to `audio_part` "<N*1000>us" (and to
    # `video_part` "<N>ms" when `fmp4_video_part_ms` is not given),
    # `fmp4_video_part_ms` to `video_part` "<N>ms". None: not used.
    "fmp4_part_ms": None,
    "fmp4_video_part_ms": None,
    # Segment of a stream with video: the key-frame interval of the encoder.
    "fmp4_video_segment_ms": None,
    "daw_hls_packaging": "opaque",
    "daw_hls_formats": "flac,opus",
    "daw_hls_base_stream_id": 2001,
    "tokio_worker_threads": None,
    "egress_capacity_bps": 1000000000,
    "extra_distributor_args": [],
    "extra_edge_args": [],
    "extra_backbone_args": [],
    "extra_contributor_args": [],
    # Environment of every service. The allocator settings are for a node
    # that holds 4K parts of 0.2 to 1 MB next to millions of small objects.
    # glibc raises its mmap threshold to the largest block that was freed, so
    # after the first large part every part comes from the heap, and a heap
    # with long-lived small objects in it does not shrink: an edge under
    # reader churn grew from 110 MB to 310 MB in 80 minutes, 200 MB of it
    # free memory that `malloc_trim` gave back. A fixed threshold keeps every
    # block of 64 kB or more in its own mapping, which goes back to the
    # kernel when the part is dropped. Two arenas bound what the threads of
    # the HTTP runtime can strand.
    "node_env": {
        "MALLOC_MMAP_THRESHOLD_": "65536",
        "MALLOC_TRIM_THRESHOLD_": "1048576",
        "MALLOC_ARENA_MAX": "2",
    },
    "rust_log_mesh": "av_mesh=info,av_web_service=warn",
    "rust_log_contrib": "av_contrib=info,av_web_service=warn",
    # The stall instruments (D 2.9). None renders nothing (the binaries'
    # defaults). Each flag is rendered only to a binary that has it; a value
    # given to a binary without its flag is refused.
    # - mlock: --mlock off|rings on every mesh node and the contributor:
    #   with rings each prefaulted ring is mlock()ed (D 2.3; av-mesh's
    #   default). Every unit has LimitMEMLOCK=MEMLOCK_LIMIT, whatever this is.
    # - stall_tick_us, stall_journal_capacity: av-mesh's --stall-tick-us and
    #   --stall-journal-capacity on every mesh node (D 2.1; 0 turns each off).
    "mlock": None,
    "stall_tick_us": None,
    "stall_journal_capacity": None,
    # The check at boot (D 2.4): with node_warm, or node_check_seconds above
    # 0, every mesh node and the contributor get the oneshot unit
    # needletail-node-check.service (`needletail-nodecheck boot`: a warm
    # pass, then that many seconds of watching for stops and steal against
    # the two limits), their service Wants= it and starts After= it, and
    # av-mesh reads its verdict (--node-warm-file) into its readiness. It runs once a boot, not
    # on a restart or a roll. Off until the stall lab decides (no placebo).
    "node_warm": False,
    "node_check_seconds": 0,
    "node_check_max_stop_ms": 20,
    "node_check_max_steal_permille": 10,
    # {node: {setting: value}}: settings of one node's render and units only
    # (D 2.9), for the node-local settings of NODE_SETTINGS.
    "node_overrides": {},
}

SITES = {
    "london": ("eu", 51.5074, -0.1278),
    "amsterdam": ("eu", 52.3676, 4.9041),
    "frankfurt": ("eu", 50.1109, 8.6821),
    "newyork": ("na", 40.7128, -74.0060),
    "tokyo": ("apac", 35.6762, 139.6503),
    "osaka": ("apac", 34.6937, 135.5023),
    "sydney": ("apac", -33.8688, 151.2093),
    "singapore": ("apac", 1.3521, 103.8198),
    "saopaulo": ("sa", -23.5505, -46.6333),
    "losangeles": ("na", 34.0522, -118.2437),
    "mumbai": ("apac", 19.0760, 72.8777),
}

MESH_PORT = 29101
HTTP_PORT = 19444
CONTRIB_HTTP_PORT = 19443
TELEMETRY_PORT = 27300
MEDIA_FEC_PORT = 22101
DAW_MEDIA_PORT = common.DAW_MEDIA_PORT
# Source port of the contributor's direct lanes to the distributors of its
# site (contributor_direct_region).
CONTRIB_DIRECT_PORT = 22303
# Whether av-mesh takes the contributor's direct lane as a parent
# (--relay-direct-peer, --relay-direct-id). It does not yet: av-contrib has
# the sending side only. Until it does, contributor_direct_region is refused
# rather than rendered into arguments that stop every distributor of the
# contributor's site from starting.
AV_MESH_DIRECT_PARENT = False
# The fast-plane feed of the edges (`edge_fast_plane`). A forwarder sends its
# lease children's symbols from LEASE_PORT on its VLAN address (audio and
# video from one address: the video socket is its twin on the same port) and
# takes their failover controllers' promotions on LEASE_FAILOVER_PORT. An
# edge takes its primary forwarder on EDGE_RELAY_PORTS[0] and its secondary
# on EDGE_RELAY_PORTS[1] (av-mesh --relay-lease-child-ports), video on each
# plus class_port_offset, and runs its failover controller on
# EDGE_FAILOVER_PORT. None of them meets 22001/22002 on a forwarder (its
# contributor lanes), 22200+, 22301+, 22401+ or 22501+ (10 per relay child).
LEASE_PORT = 22901
LEASE_FAILOVER_PORT = 22902
EDGE_FAILOVER_PORT = 22903
EDGE_RELAY_PORTS = (22001, 22002)
# Relay children (the distributors of the other sites, merged; every
# distributor, legacy) that the port plan holds: child i takes 22200/22201 +
# 10i, its parents send from 22401/22402 + 10i and listen for its failover
# controller on 22502 + 10i, so child 40 would take LEASE_FAILOVER_PORT
# (22902 = 22502 + 400) on its secondary parent and child 50 LEASE_PORT.
MAX_RELAY_CHILDREN = 40
# Kernel settings of a host of simulated listeners (a load node whose
# purpose is "listeners"): thousands of UDP sockets and TCP connections of
# their own, each with a port of its own (a socket per listener), and the
# bootstrap's 8 MB default receive buffer would let them hold gigabytes.
LISTENER_SYSCTLS = {
    "net.ipv4.ip_local_port_range": "1024 65000",
    "net.core.rmem_default": "1048576",
    "net.core.netdev_max_backlog": "65536",
    "net.ipv4.tcp_tw_reuse": "1",
    "fs.file-max": "4194304",
    "fs.nr_open": "4194304",
}

# The settings that `--set` on `roll` and `restart --nodes` may change (C
# 2.6): each is local to the node it renders on, so a node that runs the new
# value next to one that runs the old needs no peer to agree. Every other
# setting is taken as one that two peers must agree on and needs `push`. A
# key joins this list with a test that its render changes no wire parameter
# (test_merged_topology's Rollable).
ROLLABLE_SETTINGS = (
    "rust_log_mesh", "rust_log_contrib", "node_env", "tokio_worker_threads",
    "extra_edge_args", "extra_distributor_args", "extra_contributor_args",
    "mlock", "malloc_tune", "prefault", "relay_class_cpus", "relay_class_realtime",
    "audio_hot_cpu", "cpu_load", "cpu_load_full_percent", "client_udp_datagram_capacity",
    "egress_capacity_bps", "client_udp_drain_spread_ms", "rebalance_target_permille",
)

# The settings that `node_overrides` may give one node (D 2.9): those local
# to the node they render on (ROLLABLE_SETTINGS), the stall journal's and
# the check at boot's.
NODE_SETTINGS = ROLLABLE_SETTINGS + (
    "stall_tick_us", "stall_journal_capacity", "node_warm", "node_check_seconds",
    "node_check_max_stop_ms", "node_check_max_steal_permille",
)
MLOCK_MODES = ("off", "rings")
# LimitMEMLOCK of every service unit (D 2.3): the rings that --mlock rings
# locks are about 30 MB at their defaults and 190 MB with the hunt's hop
# journal; a finite limit, so mlockall of the whole process can never pass.
MEMLOCK_LIMIT = "512M"
# The check at boot (D 2.4): its unit, its record, and the bound of its
# watch. The first restart after the unit is new waits for the old
# service's stop (up to TimeoutStopSec=20) and then for the check (up to
# its TimeoutStartSec, 70 s plus the watch); push waits 150 s for an
# activation (activate_node: the 120 s deadline, the stop's 20 s and 10 s).
# A 50 s watch keeps the worst case, 20 + 120 = 140 s, 10 s inside it.
NODE_CHECK_UNIT = "needletail-node-check.service"
NODE_WARM_FILE = "/run/needletail/node-warm.json"
NODE_CHECK_MAX_SECONDS = 50


TOPOLOGIES = ("legacy", "merged")


def topology_mode(settings: dict) -> str:
    """The `topology` setting, checked; a settings file saved before it
    existed is merged, the default."""
    mode = settings.get("topology") or DEFAULT_SETTINGS["topology"]
    if mode not in TOPOLOGIES:
        raise ValueError(f"topology {mode!r} is not one of {', '.join(TOPOLOGIES)}")
    edges_per_forwarder(settings)
    return mode


def edges_per_forwarder(settings: dict) -> int:
    """The `max_edges_per_forwarder` setting, checked (absent: the default)."""
    value = settings.get("max_edges_per_forwarder")
    try:
        edges = int(DEFAULT_SETTINGS["max_edges_per_forwarder"] if value is None else value)
    except (TypeError, ValueError):
        raise ValueError("max_edges_per_forwarder is not a whole number") from None
    if edges < 1:
        raise ValueError("max_edges_per_forwarder must be positive")
    return edges


def standby_edges(settings: dict) -> list[str]:
    """The `standby_edges` setting, checked: a list of names."""
    value = settings.get("standby_edges") or []
    if isinstance(value, str):
        value = [name for name in value.split(",") if name]
    if not isinstance(value, list) or not all(isinstance(name, str) for name in value):
        raise ValueError("standby_edges is not a list of edge names")
    return sorted(set(value))


def lab_slots(state: dict) -> dict[str, dict]:
    """The autoscale slots of the lab: {name: node} with the VLAN address
    and no public one for a slot not created yet. From the lab state
    ("slots", which `up` records), else from its topology file."""
    slots = state.get("slots")
    if slots is None:
        path = pathlib.Path(state.get("topology_file") or "")
        slots = {}
        if path.is_file():
            topology = json.loads(path.read_text())
            named = set(topology.get("autoscale_slots") or [])
            for node in topology["nodes"]:
                if node.get("slot") or node["name"] in named:
                    slots[node["name"]] = {
                        **{key: node[key] for key in ("name", "role", "site", "region", "type")},
                        "vlan_ipv4": (node.get("vlan_ipv4") or "").split("/")[0] or None,
                        "slot": True}
    out = {}
    for name, node in slots.items():
        if node.get("role") not in ("edge", "distributor") or not node.get("vlan_ipv4"):
            sys.exit(f"slot {name}: only an edge or a distributor with a VLAN address")
        out[name] = {**node, "vlan_ipv4": str(node["vlan_ipv4"]).split("/")[0],
                     "public_ipv4": None, "slot": True}
    return out


def roster(state: dict) -> dict[str, dict]:
    """Every node that the peer lists name: the "roster" of a planned state
    (active_state), else its nodes."""
    return state.get("roster") or state["nodes"]


def slot_names(state: dict) -> set[str]:
    if "slot_names" in state:
        return set(state["slot_names"])
    return set(lab_slots(state))


def active_state(state: dict, settings: dict) -> dict:
    """The lab state that the services are planned for. "nodes": the nodes
    in service, every node of the lab but those held out (standby_edges);
    "roster": every edge and distributor the peer lists name, those held out
    and the autoscale slots (created or not) included, with the rest of the
    lab; "slot_names": the slots. Exits when a held-out name is neither an
    edge of the lab nor an autoscale slot."""
    standby = standby_edges(settings)
    slots = lab_slots(state)
    for name in standby:
        node = state["nodes"].get(name)
        if name in slots:
            continue
        if node is None or node["role"] != "edge":
            sys.exit(f"standby_edges: {name} is not an edge or an autoscale slot of the lab")
    full_roster = {**{name: node for name, node in slots.items()}, **state["nodes"]}
    for name in state["nodes"]:
        if name in slots:
            full_roster[name] = {**state["nodes"][name], "slot": True}
    serving = {name: node for name, node in state["nodes"].items() if name not in standby}
    return {**state, "nodes": serving, "roster": full_roster, "slot_names": sorted(slots)}


def check_settings(settings: dict) -> None:
    """Raise ValueError for settings the services cannot run with: the part
    settings (part_plan), the topology, contributor_direct_region and the
    class isolation settings (class_plan)."""
    part_plan(settings)
    standby_edges(settings)
    if settings.get("client_udp_max_subscribers") is not None:
        whole(settings["client_udp_max_subscribers"], "client_udp_max_subscribers")
    mode = topology_mode(settings)
    if mode == "merged" and settings.get("contributor_direct_region"):
        raise ValueError("contributor_direct_region is the legacy topology's direct lane; the "
                         "merged topology feeds the distributors of the contributor's site "
                         "directly already. Leave it false")
    if settings.get("contributor_direct_region") and not AV_MESH_DIRECT_PARENT:
        raise ValueError("contributor_direct_region needs av-mesh --relay-direct-peer and "
                         "--relay-direct-id, which av-mesh does not have yet; a distributor "
                         "would not start. Leave it false")
    class_plan(settings)
    check_stall_settings(settings)
    overrides = settings.get("node_overrides") or {}
    if not isinstance(overrides, dict) or not all(
            isinstance(given, dict) for given in overrides.values()):
        raise ValueError("node_overrides is not {node: {setting: value}}")
    for name, given in sorted(overrides.items()):
        other = sorted(set(given) - set(NODE_SETTINGS))
        if other:
            raise ValueError(f"node_overrides {name}: {', '.join(other)} is not a setting of "
                             "one node (NODE_SETTINGS)")
        try:
            class_plan({**settings, **given})
            check_stall_settings({**settings, **given})
        except ValueError as error:
            raise ValueError(f"node_overrides {name}: {error}") from None


def check_stall_settings(settings: dict) -> None:
    """Raise ValueError for a stall instrument or boot check setting (D 2.9)
    that cannot be rendered."""
    if settings.get("mlock") is not None and settings["mlock"] not in MLOCK_MODES:
        raise ValueError(f"mlock {settings['mlock']!r} is not one of {', '.join(MLOCK_MODES)}")
    if not isinstance(settings.get("node_warm"), (bool, type(None))):
        raise ValueError(f"node_warm {settings['node_warm']!r} is not true or false")
    for key in ("stall_tick_us", "stall_journal_capacity", "node_check_seconds",
                "node_check_max_stop_ms", "node_check_max_steal_permille"):
        if isinstance(settings.get(key), bool):
            raise ValueError(f"{key} {settings[key]!r} is not a whole number")
    for key in ("stall_tick_us", "stall_journal_capacity"):
        if settings.get(key) is not None:
            whole_or_zero(settings[key], key)
    seconds = whole_or_zero(settings.get("node_check_seconds") or 0, "node_check_seconds")
    if seconds > NODE_CHECK_MAX_SECONDS:
        raise ValueError(f"node_check_seconds {seconds} is over {NODE_CHECK_MAX_SECONDS}: the "
                         "first start after the check unit is installed waits for it (up to "
                         "70 s more), and push waits 150 s for an activation")
    for key in ("node_check_max_stop_ms", "node_check_max_steal_permille"):
        whole_or_zero(settings.get(key, DEFAULT_SETTINGS[key]), key)


def whole_or_zero(value, name: str) -> int:
    """A whole number of 0 or more (0 is a value: off, or none allowed)."""
    return 0 if str(value).strip() in ("0", "0.0") else whole(value, name)


def node_own_settings(settings: dict, name: str) -> dict:
    """The settings that node `name` is rendered and its units written with:
    the lab's, with its `node_overrides` on top."""
    return {**settings, **((settings.get("node_overrides") or {}).get(name) or {})}


def check_unit_wanted(settings: dict) -> bool:
    """Whether a node with these settings has the check at boot (D 2.4)."""
    return bool(settings.get("node_warm")) or int(settings.get("node_check_seconds") or 0) > 0


# SO_PRIORITY of the audio sockets: band 0 of the prio qdisc's default
# priomap (TC_PRIO_INTERACTIVE). Priority 0, every other socket, is band 1.
AUDIO_SOCKET_PRIORITY = 6
HOST_QDISCS = ("prio", "none")
CLASS_CPUS = re.compile(r"^(auto|[0-9]+,[0-9]+|)$")


def flag(value: bool) -> str:
    return "true" if value else "false"


def class_plan(settings: dict) -> dict:
    """The class isolation and audio hot path pieces that the services are
    started with. A piece set to None follows `class_isolation`;
    `low_latency_baseline` turns every piece off whatever it is set to.
    Raises ValueError for a setting that cannot be rendered."""
    baseline = bool(settings.get("low_latency_baseline", False))
    master = bool(settings.get("class_isolation", True)) and not baseline

    def piece(key: str) -> bool:
        value = settings.get(key)
        return master if value is None or baseline else bool(value)

    def switch(key: str) -> bool:
        return bool(settings.get(key, True)) and not baseline

    plan = {
        "low_latency_baseline": baseline,
        "relay_class_split": piece("relay_class_split"),
        "cache_mesh_class_split": piece("cache_mesh_class_split"),
        "client_udp_class_sockets": piece("client_udp_class_sockets"),
        "audio_hot_path": switch("audio_hot_path"),
        "relay_repair_both_lanes": switch("relay_repair_both_lanes"),
        "relay_class_realtime": bool(settings.get("relay_class_realtime", True)),
        "relay_video_gso": switch("relay_video_gso"),
        "relay_video_gro": settings.get("relay_video_gro"),
        "service_capabilities": switch("service_capabilities"),
        "client_udp_fast_lane": switch("client_udp_fast_lane"),
    }
    # The edges' fast-plane feed runs on the class threads of the
    # forwarders and the edges: without the relay class split there is none.
    plan["edge_fast_plane"] = switch("edge_fast_plane") and plan["relay_class_split"]
    try:
        offset = int(settings.get("class_port_offset", 1000))
    except (TypeError, ValueError):
        raise ValueError(f"class_port_offset {settings.get('class_port_offset')!r} "
                         "is not a whole number") from None
    if (plan["relay_class_split"] or plan["cache_mesh_class_split"]) and not 0 < offset < 30000:
        raise ValueError(f"class_port_offset={offset}: the video class ports are the ports "
                         "plus 1 to 29999")
    plan["class_port_offset"] = offset
    cpus = "" if settings.get("relay_class_cpus") is None else str(settings["relay_class_cpus"])
    if not CLASS_CPUS.match(cpus):
        raise ValueError(f"relay_class_cpus {cpus!r} is not auto, \"\" or AUDIO,VIDEO")
    plan["relay_class_cpus"] = cpus
    hot_cpu = settings.get("audio_hot_cpu", "auto")
    if hot_cpu != "auto":
        try:
            hot_cpu = int(hot_cpu)
        except (TypeError, ValueError):
            raise ValueError(f"audio_hot_cpu {hot_cpu!r} is not auto or a CPU") from None
    plan["audio_hot_cpu"] = hot_cpu
    try:
        plan["relay_video_pacing_mbps"] = int(settings.get("relay_video_pacing_mbps") or 0)
    except (TypeError, ValueError):
        raise ValueError("relay_video_pacing_mbps is not a whole number of Mbit/s") from None
    horizon = settings.get("canonical_repair_horizon", "auto")
    if horizon in (None, False, 0, "0", "off", "false"):
        horizon = "off"
    elif horizon not in ("auto", True, "true"):
        try:
            horizon = str(whole(horizon, "canonical_repair_horizon"))
        except ValueError:
            raise ValueError(f"canonical_repair_horizon {horizon!r} is not auto, off or a "
                             "whole number of milliseconds") from None
    else:
        horizon = "auto"
    plan["canonical_repair_horizon"] = horizon
    copy_ms = settings.get("relay_burst_copy_ms")
    try:
        plan["relay_burst_copy_ms"] = int(copy_ms or 0)
    except (TypeError, ValueError):
        raise ValueError("relay_burst_copy_ms is not a whole number of ms") from None
    if plan["relay_burst_copy_ms"] < 0:
        raise ValueError("relay_burst_copy_ms is below 0")
    plan["media_deadline_ms"] = whole(
        settings.get("media_deadline_ms", DEFAULT_SETTINGS["media_deadline_ms"]),
        "media_deadline_ms")
    qdisc = settings.get("host_qdisc") or "none"
    if qdisc not in HOST_QDISCS:
        raise ValueError(f"host_qdisc {qdisc!r} is not one of {', '.join(HOST_QDISCS)}")
    plan["host_qdisc"] = "none" if baseline else qdisc
    return plan


def node_vcpus(node: dict) -> int | None:
    """vCPUs of a Linode type (g6-dedicated-2: 2, g6-nanode-1: 1); None for
    anything else, such as a node of the namespace lab."""
    match = re.match(r"^g[0-9]+-[a-z]+-([0-9]+)$", str(node.get("type") or ""))
    return int(match.group(1)) if match else None


def relay_class_cpus(node: dict, plan: dict) -> str:
    """--relay-class-cpus of a node: "auto" pins the audio thread to CPU 0
    and the video thread to CPU 1 where the instance has both."""
    if plan["relay_class_cpus"] != "auto":
        return plan["relay_class_cpus"]
    vcpus = node_vcpus(node)
    return "0,1" if vcpus is not None and vcpus >= 2 else ""


def audio_hot_cpu(node: dict, plan: dict) -> int:
    """--audio-hot-cpu of the contributor: "auto" is CPU 2 where the
    instance has a CPU 2 and one more for the rest (the design's
    g6-dedicated-4: ingest on 0-1, audio on 2, video on 3), else unpinned."""
    if plan["audio_hot_cpu"] != "auto":
        return int(plan["audio_hot_cpu"])
    vcpus = node_vcpus(node)
    return 2 if vcpus is not None and vcpus >= 4 else -1


def class_ports(port: int, offset: int, split: bool) -> list[int]:
    """The ports of one class-split endpoint: audio on the port, video on the
    port plus the offset."""
    return [port, port + offset] if split and offset else [port]


def class_split_ports(port: int, settings: dict, kind: str) -> list[int]:
    """A port and, when the settings split that kind of endpoint by class
    ("relay": a relay or distributor ingest port; "mesh": the cache-mesh
    port), its video twin at the port plus class_port_offset. A fault, a
    counter or a shaping rule on the audio port alone leaves 4K untouched.
    Settings saved before the class settings existed get their defaults."""
    plan = class_plan({**DEFAULT_SETTINGS, **settings})
    split = plan["relay_class_split" if kind == "relay" else "cache_mesh_class_split"]
    return class_ports(port, plan["class_port_offset"], split)


def parse_overrides(overrides: list[str]) -> dict:
    """`key=value` settings of this run; a value is JSON or a bare string."""
    out = {}
    for override in overrides:
        key, _, raw = override.partition("=")
        if key not in DEFAULT_SETTINGS:
            sys.exit(f"unknown setting: {key}")
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            value = raw
        out[key] = value
    return out


def saved_version(saved: dict) -> int:
    """The settings_version of a saved file; a file without one is version 1."""
    try:
        return int(saved.get("settings_version") or 1)
    except (TypeError, ValueError):
        return 1


def merge_settings(saved: dict, given: dict) -> tuple[dict, dict]:
    """The settings of a run: the defaults, then the saved file, then the
    `--set` values of this run. In a saved file of version 1 the part keys
    are dropped (they named 5 ms parts, which were the defaults then, and
    would override the new defaults); they count only when given in this
    run. In a file of version 2 those keys default to None, so a value there
    was given with --set by an earlier run and is kept: a later `plan` or
    `restart --roles ...` renders the same parts as the last push. Returns
    the settings and what was dropped."""
    settings = dict(DEFAULT_SETTINGS)
    saved = dict(saved)
    dropped = {}
    if saved_version(saved) < SETTINGS_VERSION:
        dropped = {key: saved[key] for key in LEGACY_PART_KEYS
                   if saved.get(key) is not None and key not in given}
        for key in LEGACY_PART_KEYS:
            saved.pop(key, None)
    saved.pop("settings_version", None)
    settings.update(saved)
    settings.update(given)
    settings["settings_version"] = SETTINGS_VERSION
    return settings, dropped


def settings_off_default(settings: dict) -> dict:
    """The settings whose value is not today's default, in key order (the
    version marker excepted)."""
    return {key: value for key, value in sorted(settings.items())
            if key != "settings_version" and DEFAULT_SETTINGS.get(key, None) != value}


def load_settings(overrides: list[str], lab_run_id: str | None = None) -> dict:
    """The settings of a run, saved back. `lab_run_id` (the Linode
    commands): the saved file must be this lab's (lab_file_refused)."""
    saved = json.loads(SETTINGS_FILE.read_text()) if SETTINGS_FILE.is_file() else {}
    owner = saved.pop("lab_run_id", None)
    if lab_run_id is not None and SETTINGS_FILE.is_file():
        lab_file_refused(SETTINGS_FILE, owner, lab_run_id)
    settings, dropped = merge_settings(saved, parse_overrides(overrides))
    if dropped:
        print("settings: dropped the saved version 1 part settings "
              + ", ".join(f"{key}={value}" for key, value in sorted(dropped.items()))
              + "; give them with --set in this run to use them", file=sys.stderr)
    try:
        check_settings(settings)
    except ValueError as error:
        sys.exit(f"settings: {error}")
    note = gap_hold_tradeoff(settings)
    if note:
        print(f"settings: {note}", file=sys.stderr)
    # Values saved by an earlier run's --set persist; name every one that
    # differs from today's defaults, so a run never measures old tuning
    # without saying so (KEY=null clears one).
    changed = settings_off_default(settings)
    if changed:
        print("settings: not the defaults: "
              + ", ".join(f"{key}={json.dumps(value)} (default {json.dumps(DEFAULT_SETTINGS.get(key))})"
                          for key, value in changed.items()), file=sys.stderr)
    save_settings(settings, lab_run_id or owner)
    return settings


def save_settings(settings: dict, lab_run_id: str | None) -> None:
    """settings.json, with the run id of the lab it belongs to."""
    common.ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    document = {**settings, **({"lab_run_id": lab_run_id} if lab_run_id else {})}
    SETTINGS_FILE.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n")


def lab_file_refused(path: pathlib.Path, owner, run_id: str) -> None:
    """Exit 2 unless a file of the worktree (settings.json, routes.json,
    ecmp-ports.json) belongs to lab `run_id` (C 2.6): the files persist per
    worktree, and an earlier lab's routes or `--set` values were silently
    carried into later measured runs. Nothing is merged or reused."""
    if owner == run_id:
        return
    whose = f"lab {owner}'s" if owner else "of no recorded lab (written before files named their lab)"
    print(f"{path} is {whose}, not this lab's ({run_id}); move it away (settings.json and "
          "ecmp-ports.json) or push with --remeasure (routes.json)", file=sys.stderr)
    sys.exit(mesh_ops.EXIT_REFUSED)


AUDIO_PART = re.compile(r"^(epoch|[1-9][0-9]*(samples|us))$")
VIDEO_PART = re.compile(r"^(frame|[1-9][0-9]*ms)$")
# Opus frames: Opus Custom (48, 64, 72 samples: 1, 1.33, 1.5 ms; SoundKit Opus
# on the opus-custom-1ms branch) and standard Opus (120 to 960).
OPUS_EPOCH_SAMPLES = (48, 64, 72, 120, 240, 480, 960)
SAMPLE_RATE = 48_000
# The lossless epoch of the DAW source (daw-test-source: 1 ms, 48 samples,
# unless it is given --lossless-epoch-samples). With audio_part "epoch" its
# FLAC and PCM parts are this long, whatever pcm_epoch_samples says.
DAW_LOSSLESS_EPOCH_SAMPLES = 48


def smallest_audio_part_us(audio_part: str, pcm_samples: int, opus_samples: int) -> float:
    """The shortest audio part that the deploy publishes, in µs: with
    "epoch" the smallest epoch of its sources (the lab's AEP1 sender at
    pcm_epoch_samples, the DAW source's lossless epoch, an Opus frame),
    else the part that audio_part names."""
    if audio_part == "epoch":
        samples = min(pcm_samples, DAW_LOSSLESS_EPOCH_SAMPLES, opus_samples)
        return samples * 1_000_000 / SAMPLE_RATE
    if audio_part.endswith("samples"):
        return int(audio_part[:-len("samples")]) * 1_000_000 / SAMPLE_RATE
    return float(int(audio_part[:-len("us")]))


def window_parts_for(window_ms: int, part_us: int, smallest_us: float) -> int | None:
    """--window-parts that keep `window_ms` of the shortest part, or None
    when --window-ms does (no part is shorter than the nominal one).

    av-mesh turns --window-ms into a count of nominal parts
    (window_ms / part_us: 4800 at 2.5 ms), and then holds no stream to more
    parts than that count (stream_window_parts clamps to it). A stream of
    1 ms parts would keep 4.8 s, of 0.25 ms parts 1.2 s: shorter than three
    target durations (RFC 8216bis 6.2.2) and than HOLD-BACK. A count of
    shortest parts keeps window_ms of them. A stream of longer parts keeps
    up to count x part_us x 4/3 (av-mesh's margin), more than window_ms:
    40 s of video at the defaults, 160 s with 0.25 ms parts."""
    if smallest_us >= part_us:
        return None
    return -(-window_ms * 1000 * 1_000_000 // round(smallest_us * 1_000_000))


def whole(value, name: str, scale: int = 1) -> int:
    """value x scale as a positive integer, or ValueError."""
    try:
        scaled = float(value) * scale
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a number, not {value!r}") from None
    if scaled <= 0 or abs(scaled - round(scaled)) > 1e-6:
        raise ValueError(f"{name}={value} is not a positive whole number"
                         + (f" of 1/{scale}" if scale != 1 else ""))
    return int(round(scaled))


def part_plan(settings: dict) -> dict:
    """The part geometry that the services are started with: the nominal part
    of the caches in µs and their av-mesh arguments, and the contributor's
    --audio-part and --video-part. The version 1 keys, when given, are mapped
    onto the new ones. Raises ValueError for a setting that cannot be
    expressed."""
    part_us = whole(settings["part_us"], "part_us")
    if settings.get("part_ms") is not None:
        part_us = whole(settings["part_ms"], "part_ms", 1000)
    mesh = ["--part-us", str(part_us)]
    if settings.get("parts_per_segment") is not None:
        mesh += ["--parts-per-segment", str(whole(settings["parts_per_segment"], "parts_per_segment"))]
    else:
        mesh += ["--segment-ms", str(whole(settings["segment_ms"], "segment_ms"))]
    if settings.get("part_hold_back_ms"):
        hold_back = ["--part-hold-back-ms", str(whole(settings["part_hold_back_ms"], "part_hold_back_ms"))]
    else:
        hold_back = []

    audio = str(settings["audio_part"])
    video = str(settings["video_part"])
    legacy_audio = settings.get("fmp4_part_ms")
    if legacy_audio is not None:
        audio = f"{whole(legacy_audio, 'fmp4_part_ms', 1000)}us"
    if settings.get("fmp4_video_part_ms") is not None:
        video = f"{whole(settings['fmp4_video_part_ms'], 'fmp4_video_part_ms')}ms"
    elif legacy_audio is not None and float(legacy_audio) == int(float(legacy_audio)):
        # Version 1: --fmp4-part-ms was the part of video too.
        video = f"{int(float(legacy_audio))}ms"
    if not AUDIO_PART.match(audio):
        raise ValueError(f"audio_part {audio!r} is not epoch, <N>samples or <N>us")
    if not VIDEO_PART.match(video):
        raise ValueError(f"video_part {video!r} is not frame or <N>ms")

    pcm = whole(settings["pcm_epoch_samples"], "pcm_epoch_samples")
    if pcm < 12:
        raise ValueError(f"pcm_epoch_samples={pcm}: the smallest epoch is 12 samples")
    opus = whole(settings["opus_epoch_samples"], "opus_epoch_samples")
    if opus not in OPUS_EPOCH_SAMPLES:
        raise ValueError(f"opus_epoch_samples={opus}: Opus frames are {OPUS_EPOCH_SAMPLES} samples")
    opus_bitrate = whole(settings.get("opus_bitrate", 320000), "opus_bitrate")
    if not 6000 <= opus_bitrate <= 512000:
        raise ValueError(f"opus_bitrate={opus_bitrate}: Opus takes 6000 to 512000 b/s")

    smallest_us = smallest_audio_part_us(audio, pcm, opus)
    window_ms = whole(settings["window_ms"], "window_ms")
    if settings.get("window_parts") is not None:
        window_parts = whole(settings["window_parts"], "window_parts")
        mesh += ["--window-parts", str(window_parts)]
    else:
        window_parts = window_parts_for(window_ms, part_us, smallest_us)
        mesh += (["--window-ms", str(window_ms)] if window_parts is None
                 else ["--window-parts", str(window_parts)])
    return {"part_us": part_us, "mesh_args": mesh + hold_back, "audio_part": audio,
            "video_part": video, "pcm_epoch_samples": pcm, "opus_epoch_samples": opus,
            "opus_bitrate": opus_bitrate,
            "smallest_part_us": smallest_us,
            "window_parts": window_parts if window_parts is not None else -(-window_ms * 1000 // part_us)}


def epoch_samples(settings: dict, payload: str) -> int:
    """Samples of one AEP1 epoch of the lab's sender for a payload (pcm, flac
    or opus). FLAC frames are at least 32 samples."""
    plan = part_plan(settings)
    if payload == "opus":
        return plan["opus_epoch_samples"]
    samples = plan["pcm_epoch_samples"]
    return max(32, samples) if payload == "flac" else samples


def stream_part_us(settings: dict, epoch_samples: int) -> float:
    """Duration in µs of one audio part of a stream whose source sends
    epochs (or Opus frames) of `epoch_samples`, under the contributor's
    audio_part: one epoch, or the samples or µs that it names."""
    audio = part_plan(settings)["audio_part"]
    if audio == "epoch":
        return epoch_samples * 1_000_000 / SAMPLE_RATE
    if audio.endswith("samples"):
        return int(audio[:-len("samples")]) * 1_000_000 / SAMPLE_RATE
    return float(int(audio[:-len("us")]))


def whole_part_ms(settings: dict, epoch_samples: int, what: str) -> int:
    """stream_part_us in whole ms, for the checkers that take only that
    (aep1-48k-probe receive-hls and load-hls --part-ms, soundkit-part-check);
    ValueError when the part is not a whole number of ms."""
    ms = stream_part_us(settings, epoch_samples) / 1000
    if ms <= 0 or abs(ms - round(ms)) > 1e-9:
        raise ValueError(
            f"{what} are {ms:g} ms (audio_part {part_plan(settings)['audio_part']}, "
            f"{epoch_samples}-sample epochs); aep1-48k-probe receive-hls, load-hls and "
            "soundkit-part-check take whole ms parts: send epochs of a multiple of 48 "
            "samples, or deploy with --set audio_part=<N*1000>us")
    return int(round(ms))


def deployed_part_ms(settings: dict) -> float:
    """The nominal part of a deployed settings file in ms, for the tools that
    still speak ms (version 1 files have `part_ms`, version 2 `part_us`)."""
    if settings.get("part_ms") is not None:
        return float(settings["part_ms"])
    return int(settings.get("part_us") or 2500) / 1000


def relay_node_id(node: dict) -> str:
    """The id of a node in the relay program and on the wire: its name, but
    "contrib" for the contributor and "contrib-b" for the standby
    contributor (the topology's `"standby": true`)."""
    if node["role"] == "contributor":
        return "contrib-b" if node.get("standby") else "contrib"
    return node["name"]


def link_addresses(a: dict, b: dict) -> tuple[str, str]:
    """Private VLAN addresses when both ends share a site VLAN, else public."""
    if a["site"] == b["site"] and a.get("vlan_ipv4") and b.get("vlan_ipv4"):
        return a["vlan_ipv4"], b["vlan_ipv4"]
    return a["public_ipv4"], b["public_ipv4"]


def measure_rtt_us(source: dict, target_ip: str, count: int = 7) -> tuple[int, int]:
    output = common.ssh_text(
        source, f"ping -q -c {count} -i 0.2 -W 2 {target_ip}", timeout=60
    )
    for line in output.splitlines():
        if line.startswith("rtt "):
            values = line.split("=")[1].strip().split()[0].split("/")
            return int(float(values[1]) * 1000), int(float(values[3]) * 1000)
    raise RuntimeError(f"could not measure {source['name']} path to {target_ip}")


def backbone(state: dict) -> tuple[dict, dict]:
    relays = common.nodes_by_role(state, "backbone")
    primary = [node for node in relays if node["name"].endswith("primary")]
    secondary = [node for node in relays if node["name"].endswith("secondary")]
    if len(primary) != 1 or len(secondary) != 1:
        sys.exit("the lab needs exactly one *-primary and one *-secondary backbone")
    return primary[0], secondary[0]


def relay_tree(state: dict, settings: dict) -> dict:
    """Who feeds whom in the relay DAG of the `topology` setting:
    - "first": the contributor's two children, the source lane's and the
      source-and-repair lane's (legacy: the *-primary and *-secondary
      backbones; merged: the two distributors of the contributor's site, by
      name), at level 1;
    - "children": every other relaying node at level 2, each with its
      "primary" and "secondary" parent and its "index" (its ports: receiver
      22200/22201 + 10 x index, the parents' senders 22401/22402 + 10 x
      index, failover 22501/22502 + 10 x index);
    - "unused": the backbone relays that the topology does not use;
    - "contrib": the contributor, and "standby": the standby contributor or
      None. A lab has one contributor, or two of which one is marked
      `"standby": true`: it publishes the same parts on lanes of its own to
      the same two "first" nodes, from the contributor's site and VLAN.
    Exits when the state cannot carry the topology."""
    mode = topology_mode(settings)
    contributors = common.contributors(state)
    origins = [node for node in contributors if not node.get("standby")]
    standbys = [node for node in contributors if node.get("standby")]
    if len(origins) != 1 or len(standbys) > 1:
        sys.exit("the lab needs one contributor, or two of which one is the standby "
                 "(\"standby\": true in the topology); found "
                 + (", ".join(f"{node['name']}{' (standby)' if node.get('standby') else ''}"
                              for node in contributors) or "none"))
    contrib = origins[0]
    standby = standbys[0] if standbys else None
    if standby is not None and (
            standby["site"] != contrib["site"] or not contrib.get("vlan_ipv4")
            or not standby.get("vlan_ipv4") or standby["vlan_ipv4"] == contrib["vlan_ipv4"]):
        sys.exit(f"the standby contributor {standby['name']} must be at the contributor's "
                 f"site {contrib['site']}, on its VLAN, with an address of its own (it is at "
                 f"{standby['site']}, VLAN address {standby.get('vlan_ipv4')}; "
                 f"{contrib['name']} has {contrib.get('vlan_ipv4')})")
    # An autoscale slot (dist-c) is a fast-plane lease child, never a
    # compiled relay child: a child of its own would take an index and move
    # the ports of every child after it.
    slots = slot_names(state)
    distributors = [node for node in common.nodes_by_role(state, "distributor")
                    if node["name"] not in slots]
    if not distributors:
        sys.exit("the lab needs at least one distributor")
    if mode == "legacy":
        primary, secondary = backbone(state)
        children = [{"node": node, "primary": primary, "secondary": secondary, "index": index}
                    for index, node in enumerate(distributors)]
        return {"mode": mode, "contrib": contrib, "standby": standby,
                "first": [primary, secondary], "children": children, "unused": []}
    local = [node for node in distributors if node["site"] == contrib["site"]]
    if len(local) < 2:
        sys.exit(f"the merged topology needs two distributors at the contributor's site "
                 f"{contrib['site']} (its first forwarders); found {len(local)}. "
                 "Use --set topology=legacy")
    first = local[:2]
    others = [node for node in distributors if node not in first]
    children = []
    by_site: dict[str, int] = {}
    for index, node in enumerate(sorted(others, key=lambda item: (item["site"], item["name"]))):
        rank = by_site.get(node["site"], 0)
        by_site[node["site"]] = rank + 1
        primary, secondary = first if rank % 2 == 0 else first[::-1]
        children.append({"node": node, "primary": primary, "secondary": secondary,
                         "index": index})
    return {"mode": mode, "contrib": contrib, "standby": standby, "first": first,
            "children": children, "unused": common.nodes_by_role(state, "backbone")}


def check_edge_fanout(state: dict, settings: dict) -> None:
    """Exit when a site of the merged topology has more edges than one
    forwarder may feed (max_edges_per_forwarder): each distributor of a
    site feeds every edge of it, and the level of regional relays that
    would split them is not built yet."""
    limit = edges_per_forwarder(settings)
    every = {"nodes": roster(state)}
    for site in sorted({node["site"] for node in common.nodes_by_role(every, "distributor")}):
        edges = [node for node in common.nodes_by_role(every, "edge") if node["site"] == site]
        if len(edges) > limit:
            sys.exit(f"site {site} has {len(edges)} edges; a forwarder of the merged topology "
                     f"feeds at most max_edges_per_forwarder={limit}, and the level of "
                     "regional relays that would split them is not built yet")


def build_program(state: dict, settings: dict, routes: dict) -> dict:
    tree = relay_tree(state, settings)
    merged = tree["mode"] == "merged"
    if len(tree["children"]) > MAX_RELAY_CHILDREN:
        sys.exit(f"{len(tree['children'])} relay children; the port plan holds "
                 f"{MAX_RELAY_CHILDREN} (22200 + 10 x index, 22502 + 10 x index below the "
                 "lease ports)")
    if merged:
        check_edge_fanout(state, settings)
    contrib = tree["contrib"]
    primary, secondary = tree["first"]
    distributors = [node for node in common.nodes_by_role(state, "distributor")
                    if node["name"] not in slot_names(state)]

    def domain(node: dict) -> dict:
        return {"provider": "linode", "region": node["region"], "asn": 63949,
                "zone": node["region"]}

    topology_nodes = [
        {"node_id": "contrib", "level": 0, "role": "origin",
         "failure_domain": domain(contrib)},
        {"node_id": relay_node_id(primary), "level": 1, "role": "backbone",
         "failure_domain": domain(primary)},
        {"node_id": relay_node_id(secondary), "level": 1, "role": "backbone",
         "failure_domain": domain(secondary)},
    ]
    parent_links = [
        {"parent_node_id": "contrib", "child_node_id": relay_node_id(primary),
         "role": "primary"},
        {"parent_node_id": "contrib", "child_node_id": relay_node_id(secondary),
         "role": "primary"},
    ]
    contrib_to_primary = link_addresses(contrib, primary)
    contrib_to_secondary = link_addresses(contrib, secondary)
    carrier_links = [
        {"parent_node_id": "contrib", "child_node_id": relay_node_id(primary),
         "role": "primary", "lane": "source",
         "sender_bind": f"{contrib_to_primary[0]}:22301",
         "sender_peer": f"{contrib_to_primary[0]}:22301",
         "receiver_bind": "0.0.0.0:22001",
         "receiver_target": f"{contrib_to_primary[1]}:22001"},
        {"parent_node_id": "contrib", "child_node_id": relay_node_id(secondary),
         "role": "primary", "lane": "source_and_repair",
         "sender_bind": f"{contrib_to_secondary[0]}:22302",
         "sender_peer": f"{contrib_to_secondary[0]}:22302",
         "receiver_bind": "0.0.0.0:22002",
         "receiver_target": f"{contrib_to_secondary[1]}:22002"},
    ]
    # The standby contributor is a replica of the origin, not a topology
    # node: the same two lanes from its own address, each to the socket that
    # the contributor's other lane leaves free on that first node (its
    # secondary lane). Two contributors by two first nodes, and no more.
    origin_standby = None
    if tree["standby"] is not None:
        standby_to_primary = link_addresses(tree["standby"], primary)
        standby_to_secondary = link_addresses(tree["standby"], secondary)
        origin_standby = {
            "node_id": relay_node_id(tree["standby"]),
            "failure_domain": domain(tree["standby"]),
            "carrier_links": [
                {"child_node_id": relay_node_id(primary), "lane": "source",
                 "sender_bind": f"{standby_to_primary[0]}:22301",
                 "sender_peer": f"{standby_to_primary[0]}:22301",
                 "receiver_bind": "0.0.0.0:22002",
                 "receiver_target": f"{standby_to_primary[1]}:22002"},
                {"child_node_id": relay_node_id(secondary), "lane": "source_and_repair",
                 "sender_bind": f"{standby_to_secondary[0]}:22302",
                 "sender_peer": f"{standby_to_secondary[0]}:22302",
                 "receiver_bind": "0.0.0.0:22001",
                 "receiver_target": f"{standby_to_secondary[1]}:22001"},
            ]}
    failover_links = []
    hunted = load_hunted_ports()

    def sender_port(parent: dict, node: dict, default: int) -> int:
        entry = hunted.get(f"{relay_node_id(parent)}->{relay_node_id(node)}")
        return entry["port"] if entry else default

    shared_domain_parents = []
    for child in tree["children"]:
        node, primary, secondary = child["node"], child["primary"], child["secondary"]
        node_id = relay_node_id(node)
        offset = child["index"] * 10
        # Legacy: a distributor is a leaf of the DAG. Merged: a regional
        # relay, which a later level (or the lease children of item 5) can
        # forward from.
        topology_nodes.append({"node_id": node_id, "level": 2,
                               "role": "regional_relay" if merged else "playback_edge",
                               "failure_domain": domain(node)})
        if domain(primary) == domain(secondary):
            # Both first forwarders share the contributor's region: a
            # declared gap of the compiled plan, never a silent pass.
            shared_domain_parents.append(node_id)
        parent_links.append({"parent_node_id": relay_node_id(primary),
                             "child_node_id": node_id, "role": "primary"})
        parent_links.append({"parent_node_id": relay_node_id(secondary),
                             "child_node_id": node_id, "role": "secondary"})
        primary_pair = link_addresses(primary, node)
        secondary_pair = link_addresses(secondary, node)
        carrier_links.append({
            "parent_node_id": relay_node_id(primary), "child_node_id": node_id,
            "role": "primary", "lane": "source",
            "sender_bind": f"{primary_pair[0]}:{sender_port(primary, node, 22401 + offset)}",
            "sender_peer": f"{primary_pair[0]}:{sender_port(primary, node, 22401 + offset)}",
            "receiver_bind": f"0.0.0.0:{22200 + offset}",
            "receiver_target": f"{primary_pair[1]}:{22200 + offset}"})
        carrier_links.append({
            "parent_node_id": relay_node_id(secondary), "child_node_id": node_id,
            "role": "secondary", "lane": "repair",
            "sender_bind": f"{secondary_pair[0]}:{sender_port(secondary, node, 22402 + offset)}",
            "sender_peer": f"{secondary_pair[0]}:{sender_port(secondary, node, 22402 + offset)}",
            "receiver_bind": f"0.0.0.0:{22201 + offset}",
            "receiver_target": f"{secondary_pair[1]}:{22201 + offset}"})
        failover_links.append({
            "forwarder_node_id": relay_node_id(secondary),
            "controller_node_id": node_id,
            "controller_bind": f"{secondary_pair[1]}:{22501 + offset}",
            "controller_peer": f"{secondary_pair[1]}:{22501 + offset}",
            "listener_bind": f"{secondary_pair[0]}:{22502 + offset}",
            "listener_target": f"{secondary_pair[0]}:{22502 + offset}"})

    def observation(rtt_us: int, jitter_us: int, loss_ppm: int) -> dict:
        return {
            "source": "linode-icmp-route-probe+controlled-loss-profile",
            "observed_at_unix_ms": int(time.time() * 1000),
            "best_direct_rtt_us": routes["best_direct_rtt_us"],
            "rtt_us": rtt_us, "jitter_us": jitter_us,
            "loss_ppm": loss_ppm, "queue_delay_us": 0,
        }

    shape = ({"forwarding": "merged", "shared_domain_parents": shared_domain_parents,
              "max_edges_per_forwarder": edges_per_forwarder(settings)}
             if merged else
             {"shared_domain_parents": shared_domain_parents} if shared_domain_parents else {})
    return {
        **shape,
        **({"origin_standby": origin_standby} if origin_standby else {}),
        "purpose": "single_provider_qualification",
        "carrier": "controlled_public_udp",
        "subscription_id": 1,
        "media_deadline_ms": settings["media_deadline_ms"],
        "audio_epoch_redundant_ingress": True,
        "source_path_observation": observation(
            routes["primary_rtt_us"], routes["primary_jitter_us"],
            settings["path_loss_ppm"]),
        "secondary_path_observation": observation(
            routes["secondary_rtt_us"], routes["secondary_jitter_us"], 0),
        "failover_policy": {
            "primary_silence_ms": settings["failover_primary_silence_ms"],
            "primary_recovery_ms": settings["failover_primary_recovery_ms"],
            "secondary_warm_ms": settings["failover_secondary_warm_ms"],
            "heartbeat_ms": settings["failover_heartbeat_ms"],
            "lease_ms": settings["failover_lease_ms"],
        },
        "failover_control_links": failover_links,
        "topology": {
            "generation": 1,
            "nodes": topology_nodes,
            "parent_links": parent_links,
            "limits": {"max_origin_children": 2,
                       "max_downstream_children": max(4, len(distributors))},
        },
        "carrier_links": carrier_links,
    }


HUNT_FILE = common.ARTIFACT_DIR / "ecmp-ports.json"
HUNT_CANDIDATES = 40

HUNT_RECEIVER = r"""
import json, socket, struct, sys, time
port, seconds = int(sys.argv[1]), float(sys.argv[2])
sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.bind(("0.0.0.0", port)); sock.settimeout(0.5)
best = {}
deadline = time.time() + seconds
while time.time() < deadline:
    try:
        data, _ = sock.recvfrom(64)
    except OSError:
        continue
    arrival = time.time_ns()
    source_port, sent = struct.unpack("!IQ", data[:12])
    delay = (arrival - sent) / 1e6
    if source_port not in best or delay < best[source_port]:
        best[source_port] = delay
print(json.dumps(best))
"""

HUNT_SENDER = r"""
import socket, struct, sys, time
bind, target, port, first, count = sys.argv[1], sys.argv[2], int(sys.argv[3]), int(sys.argv[4]), int(sys.argv[5])
for source_port in range(first, first + count):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.bind((bind, source_port))
    except OSError:
        continue
    for _ in range(8):
        sock.sendto(struct.pack("!IQ", source_port, time.time_ns()), (target, port))
        time.sleep(0.01)
    sock.close()
"""


def load_hunted_ports() -> dict:
    return json.loads(HUNT_FILE.read_text()) if HUNT_FILE.is_file() else {}


def pick_hunted_port(delays: dict[int, dict[int, float]], audio_port: int,
                     reference: int | None = None) -> dict | None:
    """The source port to use on one link, from the best one-way delay (ms)
    of each source port at each receiver port of the link: the audio port
    and, with the relay class split, its video twin. The forwarder sends
    both classes from that one source port, to different receiver ports, so
    routers hash them as two 5-tuples onto paths that can differ: the port
    whose slower class is fastest wins, and only a port seen at every
    receiver port counts. `reference` is the default port's probe, the
    baseline. None when no port was seen at every receiver port."""
    seen = [set(by_source) - {reference} for by_source in delays.values()]
    ports = set.intersection(*seen) if seen else set()
    if not ports:
        return None
    worst = {port: max(by_source[port] for by_source in delays.values()) for port in ports}
    ordered = sorted(worst.values())
    best = min(sorted(worst), key=worst.get)
    video = [receiver for receiver in sorted(delays) if receiver != audio_port]
    baseline = ([by_source[reference] for by_source in delays.values() if reference in by_source]
                if reference is not None else [])
    return {
        "port": best, "one_way_ms": round(worst[best], 3),
        "audio_one_way_ms": (round(delays[audio_port][best], 3)
                             if audio_port in delays else None),
        "video_one_way_ms": round(delays[video[0]][best], 3) if video else None,
        "receiver_ports": sorted(delays),
        "candidates": len(worst),
        "median_ms": round(ordered[len(ordered) // 2], 3),
        "worst_ms": round(ordered[-1], 3),
        "reference_port_ms": (round(max(baseline), 3)
                              if baseline and len(baseline) == len(delays) else None),
    }


def command_hunt(args: argparse.Namespace) -> None:
    """Pick, per public relay-to-distributor link, the UDP source port whose
    ECMP path has the lowest one-way delay to the real receiver port.

    Routers hash the 5-tuple, so the source port alone selects among parallel
    long-haul paths that differ by several milliseconds. Clocks are chrony
    synchronised, so receiver arrival minus sender timestamp ranks the paths.
    With the relay class split the forwarder sends video from the same source
    port to the receiver port plus class_port_offset, another 5-tuple: both
    receiver ports are probed and the port whose slower class is fastest is
    picked (pick_hunted_port). The distributor's mesh service is stopped
    briefly to free its ingest ports: every remote distributor, four at a
    time, which is an outage of its region (C 2.6). So the hunt is a
    bring-up step, between `push` and the first session and followed by
    `restart --roles distributor`: it takes the ops lock and is refused
    while an edge of the lab has a listener or a session, or does not say,
    unless `--outage`.
    """
    state = common.load_state()
    lab_files_checked(state, hunting=True)
    settings = load_settings(getattr(args, "set", None) or [], lab_run_id=state["run_id"])
    with ops_locked():
        if not args.outage:
            busy = edges_in_use(LinodeOps(state, settings))
            if busy:
                print("hunt stops every remote distributor: refused while edges serve "
                      f"({'; '.join(busy)}); --outage hunts all the same", file=sys.stderr)
                sys.exit(mesh_ops.EXIT_REFUSED)
        hunt_ports(state, settings)


def edges_in_use(ops: "LinodeOps") -> list[str]:
    """The edges in service that hold a UDP subscriber or an LL-HLS session,
    or that do not say so (no answer, no figure), each with why."""
    names = [record["name"] for record in ops.nodes()
             if record["role"] == "edge" and record["serving"]]
    busy = []
    for name, document in ops.statuses(names).items():
        status = mesh_ops.status_of(document)
        counts = {gauge: status.udp(gauge) if status else None
                  for gauge in ("subscribers", "hls_sessions")}
        if status is None:
            busy.append(f"{name}: no answer")
        elif None in counts.values():
            busy.append(f"{name}: no subscriber or session figure")
        elif any(counts.values()):
            busy.append(f"{name}: {counts['subscribers']} subscribers, "
                        f"{counts['hls_sessions']} sessions")
    return busy


def hunt_ports(state: dict, settings: dict) -> None:
    """The hunt itself (command_hunt), under the ops lock."""
    tree = relay_tree(state, settings)
    links = []
    for child in tree["children"]:
        node, index = child["node"], child["index"]
        for lane, (parent, receiver_port) in enumerate((
                (child["primary"], 22200 + index * 10), (child["secondary"], 22201 + index * 10))):
            pair = link_addresses(parent, node)
            if pair[0] == parent.get("vlan_ipv4"):
                continue  # same-site VLAN link: one L2 path, nothing to hunt
            links.append({
                "key": f"{relay_node_id(parent)}->{relay_node_id(node)}",
                "parent": parent, "node": node, "bind": pair[0], "target": pair[1],
                "receiver_port": receiver_port,
                "receiver_ports": class_split_ports(receiver_port, settings, "relay"),
                "first": 24000 + (index * 2 + lane) * 64,
                "default": (22401 if lane == 0 else 22402) + index * 10,
            })
    # A node sends or receives probes, never both (a first forwarder of the
    # merged topology is a parent only): one script each.
    receivers = {link["node"]["name"]: link["node"] for link in links}
    senders = {link["parent"]["name"]: link["parent"] for link in links}
    for name, node in sorted({**senders, **receivers}.items()):
        script = HUNT_RECEIVER if name in receivers else HUNT_SENDER
        common.ssh(node, f"cat >/tmp/needletail-hunt.py <<'PY'\n{script}\nPY")
    distributors = [child["node"] for child in tree["children"]]

    def hunt_distributor(node: dict) -> dict:
        mine = [link for link in links if link["node"]["name"] == node["name"]]
        if not mine:
            return {}
        start = "systemctl stop needletail-mesh.service; sleep 0.5; " + " ".join(
            f"(python3 /tmp/needletail-hunt.py {port} {14 * len(link['receiver_ports'])} "
            f">/tmp/needletail-hunt-{port}.json 2>/dev/null &);"
            for link in mine for port in link["receiver_ports"])
        common.ssh(node, start)
        time.sleep(1.5)
        try:
            for link in mine:
                # The default port is probed too, as the comparison baseline.
                common.ssh(link["parent"], "; ".join(
                    f"python3 /tmp/needletail-hunt.py {link['bind']} {link['target']} "
                    f"{port} {link['first']} {HUNT_CANDIDATES}; "
                    f"python3 /tmp/needletail-hunt.py {link['bind']} {link['target']} "
                    f"{port} {link['default'] + 2000} 1"
                    for port in link["receiver_ports"]), timeout=120 * len(link["receiver_ports"]))
            time.sleep(13 * len(mine[0]["receiver_ports"]))
            found = {}
            for link in mine:
                delays = {}
                for port in link["receiver_ports"]:
                    raw = json.loads(common.ssh_text(
                        node, f"cat /tmp/needletail-hunt-{port}.json") or "{}")
                    delays[port] = {int(source): delay for source, delay in raw.items()}
                picked = pick_hunted_port(delays, link["receiver_port"], link["default"] + 2000)
                if picked:
                    found[link["key"]] = picked
            return found
        finally:
            common.ssh(node, "systemctl start needletail-mesh.service", check=False)

    results = common.parallel(distributors, hunt_distributor, workers=4)
    hunted = {key: value for found in results.values() for key, value in found.items()}
    HUNT_FILE.write_text(json.dumps({**hunted, "lab_run_id": state["run_id"]}, indent=2,
                                    sort_keys=True) + "\n")
    for key in sorted(hunted):
        item = hunted[key]
        classes = ("" if item.get("video_one_way_ms") is None else
                   f" (audio {item['audio_one_way_ms']:.2f}, video {item['video_one_way_ms']:.2f})")
        print(f"{key:<34} port {item['port']}  best {item['one_way_ms']:.2f} ms{classes}  "
              f"median {item['median_ms']:.2f}  worst {item['worst_ms']:.2f}  "
              f"({item['candidates']} ports)")
    print("run `restart --roles distributor` to apply the selected source ports (the parent's "
          "sender and the child's peer change together)")


def measure_routes(state: dict, settings: dict | None = None) -> dict:
    """Route seeds for the adaptive RaptorQ controller: the farthest
    distributor, over the relay DAG of the `topology` setting (merged: a
    first forwarder is a route of one leg)."""
    tree = relay_tree(state, settings or DEFAULT_SETTINGS)
    contrib = tree["contrib"]
    first = tree["first"]
    to_first = {}
    for parent in first:
        to_first[parent["name"]] = measure_rtt_us(contrib, link_addresses(contrib, parent)[1])

    def route(child: dict) -> dict:
        node = child["node"]
        direct, _ = measure_rtt_us(contrib, node["public_ipv4"])
        primary_leg, primary_jitter = measure_rtt_us(
            child["primary"], link_addresses(child["primary"], node)[1])
        secondary_leg, secondary_jitter = measure_rtt_us(
            child["secondary"], link_addresses(child["secondary"], node)[1])
        primary_rtt, primary_first_jitter = to_first[child["primary"]["name"]]
        secondary_rtt, secondary_first_jitter = to_first[child["secondary"]["name"]]
        return {"direct_rtt_us": direct, "primary_leg_rtt_us": primary_leg,
                "primary_leg_jitter_us": primary_jitter,
                "secondary_leg_rtt_us": secondary_leg,
                "secondary_leg_jitter_us": secondary_jitter,
                "primary_route_rtt_us": primary_rtt + primary_leg,
                "primary_route_jitter_us": primary_first_jitter + primary_jitter,
                "secondary_route_rtt_us": secondary_rtt + secondary_leg,
                "secondary_route_jitter_us": secondary_first_jitter + secondary_jitter}

    children = {child["node"]["name"]: child for child in tree["children"]}
    per_distributor = common.parallel(
        [child["node"] for child in tree["children"]],
        lambda node: route(children[node["name"]])) if children else {}
    for index, parent in enumerate(first):
        # A first forwarder is its own route (merged), or a backbone
        # (legacy, not a distributor: no entry).
        if parent["role"] != "distributor":
            continue
        rtt, jitter = to_first[parent["name"]]
        direct, _ = measure_rtt_us(contrib, parent["public_ipv4"])
        per_distributor[parent["name"]] = {
            "direct_rtt_us": direct, "primary_route_rtt_us": rtt,
            "primary_route_jitter_us": jitter,
            "secondary_route_rtt_us": to_first[first[1 - index]["name"]][0],
            "secondary_route_jitter_us": to_first[first[1 - index]["name"]][1]}
    farthest = max(per_distributor.values(), key=lambda v: v["primary_route_rtt_us"])
    return {
        "measured_unix": int(time.time()),
        "topology": tree["mode"],
        "contrib_primary_rtt_us": to_first[first[0]["name"]][0],
        "contrib_secondary_rtt_us": to_first[first[1]["name"]][0],
        "best_direct_rtt_us": farthest["direct_rtt_us"],
        "primary_rtt_us": farthest["primary_route_rtt_us"],
        "primary_jitter_us": farthest["primary_route_jitter_us"],
        "secondary_rtt_us": farthest["secondary_route_rtt_us"],
        "secondary_jitter_us": farthest["secondary_route_jitter_us"],
        "distributors": per_distributor,
    }


def compiler_stale_by(compiler: pathlib.Path) -> str | None:
    """The newest source of the compiler (src/**/*.rs, Cargo.toml,
    Cargo.lock) that is newer than the binary, or None."""
    built = compiler.stat().st_mtime
    root = common.ROOT
    sources = [*sorted((root / "src").rglob("*.rs")), root / "Cargo.toml", root / "Cargo.lock"]
    newer = [path for path in sources if path.is_file() and path.stat().st_mtime > built]
    if not newer:
        return None
    return str(max(newer, key=lambda path: path.stat().st_mtime).relative_to(root))


def compile_plan(program: dict) -> dict:
    program_path = common.ARTIFACT_DIR / "relay-program.json"
    plan_path = common.ARTIFACT_DIR / "compiled-plan.json"
    program_path.write_text(json.dumps(program, indent=2) + "\n")
    compiler = common.ROOT / "target" / "release" / "needletail-compile"
    if not compiler.is_file():
        sys.exit("build needletail-compile first: cargo build --release --bin needletail-compile")
    stale = compiler_stale_by(compiler)
    if stale:
        # A compiler older than its sources judges today's program by old
        # rules (an old binary rejected the merged topology's declared
        # shared-domain parents as parent_failure_domain_overlap).
        sys.exit(f"needletail-compile is older than {stale}; rebuild it: "
                 "cargo build --release --bin needletail-compile")
    result = subprocess.run(
        [str(compiler), "--program", str(program_path), "--pretty"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        sys.exit(f"relay program did not compile:\n{result.stderr}")
    plan_path.write_text(result.stdout)
    return json.loads(result.stdout)


def geo_args(node: dict) -> list[str]:
    continent, latitude, longitude = SITES[node["site"]]
    return ["--region", node["region"], "--continent", continent,
            f"--latitude={latitude}", f"--longitude={longitude}"]


def common_mesh_args(node: dict, settings: dict, state: dict) -> list[str]:
    # Every site mate of the roster, held out or not created yet included:
    # a telemetry peer that is down is retried every second.
    site_mates = [
        other for other in roster(state).values()
        if other["site"] == node["site"] and other["name"] != node["name"]
        and other["role"] in ("distributor", "edge")
    ]
    private_ip = node.get("vlan_ipv4") or node["public_ipv4"]
    playback_ip = (private_ip if settings["playback_address"] == "vlan"
                   else node["public_ipv4"])
    args = [
        "/usr/local/bin/av-mesh",
        "--cert", "/etc/needletail/tls/fullchain.pem",
        "--key", "/etc/needletail/tls/privkey.pem",
        *geo_args(node),
        "--node-id", relay_node_id(node),
        "--http-port", str(HTTP_PORT),
        "--playback-base-url", f"https://{playback_ip}:{HTTP_PORT}/live",
        "--media-fec-bind", f"0.0.0.0:{MEDIA_FEC_PORT}",
        "--telemetry-bind", f"0.0.0.0:{TELEMETRY_PORT}",
        "--telemetry-dns-name", common.TLS_SERVER_NAME,
        "--telemetry-private-ipv4", private_ip,
        "--telemetry-interval-ms", str(settings["telemetry_interval_ms"]),
        "--stream-id", "1",
        *part_plan(settings)["mesh_args"],
        "--slot-kb", str(settings["slot_kb"]),
        "--playlist-slots", str(settings["playlist_slots"]),
    ]
    capacity = settings["egress_capacity_bps"]
    if node["role"] == "edge":
        capacity = settings["edge_egress_capacity_bps"] or capacity
        args += ["--edge-session-limit", str(settings["edge_session_limit"])]
    args += ["--egress-capacity-bps", str(capacity)]
    if node["role"] in ("distributor", "edge") and settings.get("client_udp_port"):
        args += ["--client-udp-bind", f"0.0.0.0:{settings['client_udp_port']}"]
        if settings.get("client_udp_max_subscribers") is not None:
            args += ["--client-udp-max-subscribers",
                     str(whole(settings["client_udp_max_subscribers"],
                               "client_udp_max_subscribers"))]
    if node["role"] in ("distributor", "edge"):
        # Every node of a region can be an entry point for its readers.
        args += [
            "--assignment-spread-percent", str(settings["assignment_spread_percent"]),
            "--assignment-alternates", str(settings["assignment_alternates"]),
            "--steering-ttl-seconds", str(settings["steering_ttl_seconds"]),
            "--playlist-copies", str(settings["playlist_copies"]),
        ]
        for rendition in settings.get("hls_renditions") or []:
            args += ["--hls-rendition", rendition]
        if settings.get("region_failover"):
            neighbours = {}
            slots = slot_names(state)
            for other in sorted(state["nodes"].values(), key=lambda item: item["name"]):
                if (other["role"] == "distributor" and other["site"] != node["site"]
                        and other["name"] not in slots):
                    neighbours.setdefault(other["region"], other)
            for region, other in sorted(neighbours.items()):
                args += ["--failover-entry",
                         f"{region}=https://{other['public_ipv4']}:{HTTP_PORT}"]
    if settings["response_ms"]:
        args += ["--response-ms", str(settings["response_ms"])]
    if node["role"] in ("distributor", "edge"):
        for mate in sorted(site_mates, key=lambda item: item["name"]):
            mate_ip = mate.get("vlan_ipv4") or mate["public_ipv4"]
            args += ["--telemetry-peer", f"{mate_ip}:{TELEMETRY_PORT}"]
    return args


def cache_mesh_args(node: dict, settings: dict, state: dict) -> list[str]:
    """Regional cache plane: distributors push to edges over the site VLAN."""
    if not node.get("vlan_ipv4"):
        return ["--mesh-bind", f"0.0.0.0:{MESH_PORT}"]
    peers = mesh_peer_nodes(node, settings, state)
    children = int(settings["cache_mesh_max_children"])
    if node["role"] == "distributor":
        children = max(children, len(peers))
    args = [
        "--mesh-bind", f"{node['vlan_ipv4']}:{MESH_PORT}",
        "--cache-mesh-role", node["role"],
        "--cache-mesh-region", f"{node['region']}-cache",
        "--cache-mesh-max-parents", str(settings["cache_mesh_max_parents"]),
        "--cache-mesh-max-children", str(children),
        "--mesh-sync-interval-ms", str(settings["mesh_sync_interval_ms"]),
        "--mesh-repair-ratio", str(settings["mesh_repair_ratio"]),
        "--mesh-repair-symbols", str(settings["mesh_repair_symbols"]),
        "--mesh-max-repair-symbols", str(settings["mesh_max_repair_symbols"]),
        "--mesh-symbol-size", str(settings["mesh_symbol_size"]),
        "--mesh-max-symbol-size", str(settings["mesh_max_symbol_size"]),
        "--cache-mesh-replication", settings["cache_mesh_replication"],
        "--cache-mesh-lease-ms", str(settings["cache_mesh_lease_ms"]),
        "--cache-mesh-demand-idle-ms", str(settings["cache_mesh_demand_idle_ms"]),
        "--cache-mesh-warm-parts", str(warm_parts(settings)),
        "--cache-mesh-warm-kbps", str(settings["cache_mesh_warm_kbps"]),
        "--cache-mesh-socket-buffer-bytes",
        str(settings["cache_mesh_socket_buffer_bytes"]),
    ]
    for peer in peers:
        args += ["--peer", f"{peer['vlan_ipv4']}:{MESH_PORT}"]
    return args


def mesh_peer_nodes(node: dict, settings: dict, state: dict) -> list[dict]:
    """The cache-mesh peers of an edge or distributor (its --peer list): its
    site's nodes of the other role (a distributor lists its edges, an edge
    its distributors), and with forwarder_slot_feeds_edges a distributor
    slot and its site's first two distributors (its lease parents) list each
    other: the slot subscribes at them for its children's leases (AM 12).
    The roster: edges and distributors held out, and autoscale slots not
    created yet, are peers that are down until they start."""
    peer_role = "edge" if node["role"] == "distributor" else "distributor"
    every = sorted(roster(state).values(), key=lambda item: item["name"])
    peers = [peer for peer in every
             if peer["site"] == node["site"] and peer["role"] == peer_role
             and peer.get("vlan_ipv4")]
    if node["role"] == "distributor" and settings.get("forwarder_slot_feeds_edges"):
        slots = slot_names(state)
        site_forwarders = [peer for peer in every
                           if peer["site"] == node["site"] and peer["role"] == "distributor"
                           and peer.get("vlan_ipv4")]
        parents = [peer for peer in site_forwarders if peer["name"] not in slots][:2]
        if node["name"] in slots:
            peers += parents
        elif node["name"] in {peer["name"] for peer in parents}:
            peers += [peer for peer in site_forwarders if peer["name"] in slots]
    return peers


def warm_parts(settings: dict) -> int:
    """--cache-mesh-warm-parts: cache_mesh_warm_ms in parts of the shortest
    audio part when it is given, else cache_mesh_warm_parts."""
    warm_ms = settings.get("cache_mesh_warm_ms")
    if not warm_ms:
        return int(settings["cache_mesh_warm_parts"])
    smallest = part_plan(settings)["smallest_part_us"]
    return int(-(-whole(warm_ms, "cache_mesh_warm_ms") * 1000 // smallest))


def relay_args(service: dict) -> list[str]:
    """Port of deploy/gcp-lab/av-mesh-run for one compiled av_mesh service."""
    primary = service["primary_parent"]
    args = [
        "--fec-bind", primary["bind"],
        "--relay-controlled-local",
        "--relay-primary-bind", primary["bind"],
        "--relay-primary-peer", primary["peer"],
        "--relay-primary-id", primary["parent_node_id"],
    ]
    if primary["lane"] == "source_and_repair":
        args.append("--relay-primary-promoted")
    secondary = service.get("secondary_parent")
    if secondary:
        args += [
            "--relay-secondary-bind", secondary["bind"],
            "--relay-secondary-peer", secondary["peer"],
            "--relay-secondary-id", secondary["parent_node_id"],
        ]
        if secondary["lane"] == "source_and_repair" or service.get("failover_controller"):
            args.append("--relay-secondary-promoted")
    standby = service.get("standby_origin_parent")
    if standby:
        # The standby contributor on a first forwarder's secondary lane
        # (never beside a secondary parent: the compiler refuses that). It
        # sends all that the contributor sends, so it is admitted in full.
        args += [
            "--relay-secondary-bind", standby["bind"],
            "--relay-secondary-peer", standby["peer"],
            "--relay-secondary-id", standby["parent_node_id"],
            "--relay-secondary-promoted",
        ]
    args += [
        "--relay-topology-generation", str(service["topology_generation"]),
        "--relay-subscription-id", str(service["subscription_id"]),
        "--relay-max-downstream-children", str(service["max_downstream_children"]),
    ]
    for forward in service.get("forwards", []):
        lane = "all" if forward["lane"] == "source_and_repair" else forward["lane"]
        args += ["--relay-forward", f"{forward['bind']}={forward['target']},{lane}"]
    for listener in service.get("failover_listeners") or []:
        args += ["--relay-failover-listener",
                 f"{listener['bind']}={listener['peer']},{listener['forward_target']}"]
    controller = service.get("failover_controller")
    if controller:
        args += ["--relay-failover-controller",
                 f"{controller['bind']}={controller['target']}"]
    policy = service.get("failover_policy")
    if policy:
        args += [
            "--relay-primary-silence-ms", str(policy["primary_silence_ms"]),
            "--relay-primary-recovery-ms", str(policy["primary_recovery_ms"]),
            "--relay-secondary-warm-ms", str(policy["secondary_warm_ms"]),
            "--relay-failover-heartbeat-ms", str(policy["heartbeat_ms"]),
            "--relay-failover-lease-ms", str(policy["lease_ms"]),
        ]
    return args


def flag_names(args: list[str]) -> set[str]:
    """The flags that `args` give, written `--flag value` or `--flag=value`."""
    return {arg.split("=", 1)[0] for arg in args if arg.startswith("--")}


def flag_value(args: list[str], flag: str) -> str | None:
    """The value that a service started with `args` takes for a flag that
    has one: the last `--flag value` or `--flag=value`; None if not given."""
    value = None
    for index, arg in enumerate(args):
        if arg == flag and index + 1 < len(args):
            value = args[index + 1]
        elif arg.startswith(flag + "="):
            value = arg[len(flag) + 1:]
    return value


def yield_to(extra: list[str], pairs: list[str]) -> list[str]:
    """The `--flag value` pairs that the extra arguments of a role do not
    give themselves, in either form: clap refuses a flag given twice, and an
    extra argument is how one run overrides a rendered one."""
    given = flag_names(extra)
    out = []
    for index in range(0, len(pairs), 2):
        if pairs[index] not in given:
            out += pairs[index:index + 2]
    return out


def contributor_args(service: dict, settings: dict, node: dict | None = None) -> list[str]:
    """Port of deploy/gcp-lab/av-contrib-run, with the class isolation and
    audio hot path arguments of the contributor on `node`. `service` is the
    compiled av_contrib service of that contributor: the standby's carries
    its own id and lanes and everything else of the contributor's."""
    args = [
        "/usr/local/bin/av-contrib",
        "--cert", "/etc/needletail/tls/fullchain.pem",
        "--key", "/etc/needletail/tls/privkey.pem",
        "--http-port", str(CONTRIB_HTTP_PORT),
        "--mesh-fec-target", service["primary"]["target"],
        "--mesh-media-fec-target", service["primary"]["target"],
        "--daw-media-bind", f"0.0.0.0:{DAW_MEDIA_PORT}",
        "--daw-hls-queue-capacity", "4096",
        "--daw-hls-packaging", settings["daw_hls_packaging"],
        "--daw-hls-formats", settings["daw_hls_formats"],
        "--daw-hls-base-stream-id", str(settings["daw_hls_base_stream_id"]),
        "--stream-id", "1", "--rist-stream-id", "1", "--rtmp-stream-id", "1",
        "--audio-part", part_plan(settings)["audio_part"],
        "--video-part", part_plan(settings)["video_part"],
        *(["--fmp4-video-segment-ms", str(settings["fmp4_video_segment_ms"])]
          if settings.get("fmp4_video_segment_ms") else []),
        "--wall-clock-estimated-error-ms",
        str(whole(settings["contributor_clock_error_ms"], "contributor_clock_error_ms")),
        "--rist-bind", "0.0.0.0:27000", "--rtmp-bind", "0.0.0.0:19350",
        "--relay-local-id", service["node_id"],
        "--relay-primary-bind", service["primary"]["bind"],
        "--relay-primary-target", service["primary"]["target"],
        "--relay-primary-id", service["primary"]["child_node_id"],
        "--relay-secondary-bind", service["warm_secondary"]["bind"],
        "--relay-secondary-target", service["warm_secondary"]["target"],
        "--relay-secondary-id", service["warm_secondary"]["child_node_id"],
        "--relay-secondary-seed-source",
        "--relay-exclusive",
        "--relay-topology-generation", str(service["topology_generation"]),
        "--relay-subscription-id", str(service["subscription_id"]),
        "--relay-deadline-ms", str(service["media_deadline_ms"]),
        "--relay-min-repair-symbols", str(settings["relay_min_repair_symbols"]),
    ]
    if (settings.get("audio_epoch_lane")
            or binary_has("av-contrib", "--audio-epoch-mesh-lane") is False):
        # As before wave 7 item 1: av-contrib's `auto` keeps the lane when
        # its targets are given; a binary older than the flag always had
        # the lane, and keeps these targets.
        args += ["--audio-epoch-ingress-target", service["audio_epoch_ingress_target"]]
        if service.get("audio_epoch_redundant_ingress_target"):
            args += ["--audio-epoch-redundant-ingress-target",
                     service["audio_epoch_redundant_ingress_target"]]
    for key, prefix in (("source_path_observation", "--relay-path"),
                        ("secondary_path_observation", "--relay-secondary-path")):
        observed = service.get(key)
        if not observed:
            continue
        args += [
            f"{prefix}-loss-fraction", str(observed["loss_ppm"] / 1_000_000),
            f"{prefix}-best-direct-rtt-ms", str(observed["best_direct_rtt_us"] / 1000),
            f"{prefix}-rtt-ms", str(observed["rtt_us"] / 1000),
            f"{prefix}-jitter-ms", str(observed["jitter_us"] / 1000),
            f"{prefix}-queue-delay-ms", str(observed["queue_delay_us"] / 1000),
        ]
        if observed.get("observed_at_unix_ms") is not None:
            args += [f"{prefix}-observed-at-unix-ms", str(observed["observed_at_unix_ms"])]
    extra = list(settings["extra_contributor_args"])
    if node:
        # What /api/node reports the contributor as: the node's own name
        # (the two contributors' relay ids are not node names) and its
        # region, as av-mesh has them.
        args += yield_to(extra, binary_takes("av-contrib", ["--node-id", node["name"],
                                                            "--region", node["region"]]))
    args += yield_to(extra, contributor_class_args(node or {}, class_plan(settings)))
    args += yield_to(extra, wave7_args(args, settings))
    return args + extra


def demand_renditions(settings: dict) -> str | None:
    """--relay-demand-renditions to render, or None for the binaries'
    default (nothing held back); "auto" is the DAW lane's renditions."""
    value = settings.get("relay_demand_renditions")
    if value != "auto":
        return None if value is None else str(value)
    base = int(settings.get("daw_hls_base_stream_id") or 2001)
    return f"{base},{base + 1000},{base + 2000}"


# The binaries whose flags a render looks at ({"av-mesh": path,
# "av-contrib": path}): `plan`, `push` and `restart` set those they deploy
# (binaries_for), the local labs those they run. Empty (unit tests, other
# callers): none, and the render assumes current binaries.
RENDER_BINARIES: dict = {}


@functools.lru_cache(maxsize=None)
def binary_flags(path: str, mtime_ns: int) -> frozenset | None:
    """The long flags that `path --help` lists, or None if it does not run."""
    del mtime_ns  # part of the cache key only
    try:
        result = subprocess.run([path, "--help"], capture_output=True, text=True,
                                timeout=30, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return frozenset(re.findall(r"--[a-z0-9][a-z0-9-]*", result.stdout))


def binary_has(kind: str, name: str) -> bool | None:
    """Whether the `kind` binary ("av-mesh", "av-contrib") that the render is
    for takes flag `name`; None when it cannot tell (no such file, it does
    not run here)."""
    path = RENDER_BINARIES.get(kind)
    if path is None:
        return None
    path = pathlib.Path(path)
    try:
        mtime_ns = path.stat().st_mtime_ns
    except OSError:
        return None
    flags = binary_flags(str(path), mtime_ns)
    return None if flags is None else name in flags


# The flags that binary_takes left out of the render under way, with why
# ({(kind, flag): reason}): render names the settings that did not apply.
UNRENDERED_FLAGS: dict = {}

# The setting that a flag left out by binary_takes renders, where it is not
# the flag's own name (--client-udp-move-grace-ms: client_udp_move_grace_ms).
FLAG_SETTINGS = {"--relay-media-deadline-ms": "media_deadline_ms"}


def binary_takes(kind: str, pairs: list[str]) -> list[str]:
    """The `--flag value` pairs of `pairs` whose flag the `kind` binary that
    the render is for lists in its --help. The flags that the resilient mesh
    adds are rendered through this, so that the tools work before the
    binaries have them and a roll can render an older build beside a newer
    one: a binary without a flag, and one that the render cannot see (no
    RENDER_BINARIES, as in the unit tests), gets none and runs on its
    default. Each flag left out is recorded in UNRENDERED_FLAGS."""
    out = []
    for index in range(0, len(pairs), 2):
        name = pairs[index]
        has = binary_has(kind, name)
        if has:
            out += pairs[index:index + 2]
            continue
        path = RENDER_BINARIES.get(kind)
        UNRENDERED_FLAGS[(kind, name)] = (
            f"the render names no {kind} binary" if path is None
            else f"{kind} at {path} lacks {name}" if has is False
            else f"cannot read {path} --help")
    return out


def report_unrendered(settings: dict, rendered: dict[str, dict]) -> None:
    """One stderr line naming each setting not at its default whose flag
    binary_takes left out of every node, with why: the binary runs on its
    own default instead (B 7: confirmations per source at a load host)."""
    given = set()
    for item in rendered.values():
        given |= flag_names(item["args"])
    unapplied = []
    for (_kind, name), reason in sorted(UNRENDERED_FLAGS.items()):
        key = FLAG_SETTINGS.get(name, name[2:].replace("-", "_"))
        if (name in given or key not in settings
                or settings[key] == DEFAULT_SETTINGS.get(key)):
            continue
        unapplied.append(f"{key}={json.dumps(settings[key])} ({reason})")
    if unapplied:
        print("render: settings not applied, the binaries run on their defaults: "
              + "; ".join(unapplied), file=sys.stderr)


def two_path_args(node_args: list[str], settings: dict) -> list[str]:
    """The `--flag value` pairs of the listener on two edges (workstream B
    2.3) for an av-mesh service rendered with `node_args`: the two-path bit
    rate on every node of the cache mesh, and the move grace, the
    confirmations per source and the rebalance to the mean on the nodes that
    serve UDP clients. Only the flags that the av-mesh of this render has,
    and only the settings that are not null."""
    pairs = []

    def given(key: str) -> bool:
        return settings.get(key) is not None

    if "--mesh-bind" in node_args and given("assignment_two_path_max_bps"):
        pairs += ["--assignment-two-path-max-bps",
                  str(whole(settings["assignment_two_path_max_bps"],
                            "assignment_two_path_max_bps"))]
    if "--client-udp-bind" in node_args:
        if given("client_udp_move_grace_ms"):
            grace = settings["client_udp_move_grace_ms"]
            # 0 is the behaviour of before: no grace.
            pairs += ["--client-udp-move-grace-ms",
                      "0" if grace in (0, "0") else str(whole(grace, "client_udp_move_grace_ms"))]
        if given("client_udp_assign_confirms_per_source"):
            pairs += ["--client-udp-assign-confirms-per-source",
                      str(whole(settings["client_udp_assign_confirms_per_source"],
                                "client_udp_assign_confirms_per_source"))]
        if given("client_udp_rebalance_to_mean"):
            pairs += ["--client-udp-rebalance-to-mean",
                      flag(bool(settings["client_udp_rebalance_to_mean"]))]
        if given("client_udp_gso"):
            pairs += ["--client-udp-gso", flag(bool(settings["client_udp_gso"]))]
    return binary_takes("av-mesh", pairs)


def wave7_args(node_args: list[str], settings: dict) -> list[str]:
    """The `--flag value` pairs of wave 7 items 2, 3, 4, 5 and 9 for a service
    rendered with `node_args`: only the settings that are given."""
    out = []
    renditions = demand_renditions(settings)
    if is_contributor_args(node_args):
        if settings.get("relay_audio_copies") is not None:
            out += ["--relay-audio-copies", str(settings["relay_audio_copies"])]
        if settings.get("relay_compact_audio") is not None:
            out += ["--relay-compact-audio", flag(bool(settings["relay_compact_audio"]))]
        elif (binary_has("av-contrib", "--relay-compact-audio")
              and binary_has("av-mesh", "--client-udp-compact-short-header") is False):
            # The forwarders and edges deployed with it read no RLC1.
            out += ["--relay-compact-audio", "false"]
        if renditions is not None:
            out += ["--relay-demand-renditions", renditions]
        return out
    if "--relay-controlled-local" in node_args and renditions is not None:
        out += ["--relay-demand-renditions", renditions]
    # Every forwarder (a distributor of the cache mesh), the slots that feed
    # no lease child yet included: inert where there is none (wave 7 review
    # C5: a slot renders the wave 7 settings exactly like the others).
    if ((flag_value(node_args, "--cache-mesh-role") == "distributor"
         or "--relay-lease-bind" in node_args)
            and settings.get("cache_mesh_fast_plane_push") is not None):
        out += ["--cache-mesh-fast-plane-push", str(settings["cache_mesh_fast_plane_push"])]
    if ("--relay-failover-controller" in node_args
            and settings.get("relay_failover_promotion_backoff") is not None):
        out += ["--relay-failover-promotion-backoff",
                flag(bool(settings["relay_failover_promotion_backoff"]))]
    if ((flag_value(node_args, "--cache-mesh-role") == "distributor"
         or "--relay-lease-bind" in node_args)
            and settings.get("cache_mesh_fast_plane_video_push") is not None):
        out += ["--cache-mesh-fast-plane-video-push",
                str(settings["cache_mesh_fast_plane_video_push"])]
    if ("--client-udp-bind" in node_args
            and settings.get("client_udp_compact_short_header") is not None):
        out += ["--client-udp-compact-short-header",
                flag(bool(settings["client_udp_compact_short_header"]))]
    if "--mesh-bind" in node_args:
        for key in AM_MESH_BOOL + PROOF_MESH_BOOL + COLD_MESH_BOOL + SCALE_MESH_BOOL:
            if settings.get(key) is not None:
                out += ["--" + key.replace("_", "-"), flag(bool(settings[key]))]
        for key in SCALE_MESH_COUNT:
            if settings.get(key) is not None:
                out += ["--" + key.replace("_", "-"), str(whole(settings[key], key))]
        if settings.get("cache_mesh_warm_total_kbps") is not None:
            total = settings["cache_mesh_warm_total_kbps"]
            out += ["--cache-mesh-warm-total-kbps",
                    "0" if float(total) == 0 else str(whole(total, "cache_mesh_warm_total_kbps"))]
    if "--client-udp-bind" in node_args:
        for key in (WAVE8_CLIENT_UDP + AM_CLIENT_UDP_BOOL + REVIEW_CLIENT_UDP_BOOL
                    + COLD_CLIENT_UDP_BOOL + SCALE_CLIENT_UDP_BOOL):
            if settings.get(key) is not None:
                out += ["--" + key.replace("_", "-"), flag(bool(settings[key]))]
        for key in SCALE_CLIENT_UDP_COUNT:
            if settings.get(key) is not None:
                out += ["--" + key.replace("_", "-"), str(whole(settings[key], key))]
        for key in AM_CLIENT_UDP_MS + REVIEW_CLIENT_UDP_COUNT:
            if settings.get(key) is not None:
                # 0 is the behaviour of before (no spread, no region move).
                value = 0 if float(settings[key]) == 0 else whole(settings[key], key)
                out += ["--" + key.replace("_", "-"), str(value)]
    if "--mesh-bind" in node_args and settings.get("cache_mesh_video_warm_parts") is not None:
        value = str(settings["cache_mesh_video_warm_parts"]).strip().lower()
        if value != "window":
            # 0 (no warm-up) is a value here.
            value = "0" if value in ("0", "0.0") else str(
                whole(settings["cache_mesh_video_warm_parts"], "cache_mesh_video_warm_parts"))
        out += ["--cache-mesh-video-warm-parts", value]
    if "--mesh-bind" in node_args and settings.get("cache_mesh_warm_second_parent") is not None:
        out += ["--cache-mesh-warm-second-parent",
                flag(bool(settings["cache_mesh_warm_second_parent"]))]
    if "--relay-controlled-local" in node_args:
        if settings.get("relay_demand_report_lease_parents") is not None:
            out += ["--relay-demand-report-lease-parents",
                    flag(bool(settings["relay_demand_report_lease_parents"]))]
        if settings.get("relay_class_fallback_nice") is not None:
            out += ["--relay-class-fallback-nice",
                    str(whole(settings["relay_class_fallback_nice"], "relay_class_fallback_nice")
                        if float(settings["relay_class_fallback_nice"]) else 0)]
    if "--relay-lease-bind" in node_args and settings.get("relay_lease_drain_inbound") is not None:
        out += ["--relay-lease-drain-inbound", flag(bool(settings["relay_lease_drain_inbound"]))]
    if "--relay-lease-bind" in node_args and settings.get("relay_lease_descriptors") is not None:
        out += ["--relay-lease-descriptors", flag(bool(settings["relay_lease_descriptors"]))]
    return out


# Wave 8 stage 2's settings of the nodes that serve UDP clients: each is the
# av-mesh flag of the same name.
WAVE8_CLIENT_UDP = ("client_udp_repair_thread", "client_udp_batched_admission",
                    "client_udp_resume_from_request", "client_udp_fanout_inline_shard")
# Wave 8 stage 5 (Phase AM 12, 13): on every mesh node, the av-mesh flag of
# the same name.
AM_MESH_BOOL = ("relay_lease_ranked", "relay_ranked_parents", "cache_mesh_subscribe_rank",
                "cache_mesh_release_unranked_parents", "cache_mesh_avoid_draining_parents")
# Wave 8 stage 4 (Phase AM 8, 9, 11): the same, on/off and in ms.
AM_CLIENT_UDP_BOOL = ("client_udp_egress_load", "client_udp_admit_by_load")
AM_CLIENT_UDP_MS = ("client_udp_drain_spread_ms", "client_udp_region_hold_ms")
# The wave 8 review's settings of the nodes that serve UDP clients.
REVIEW_CLIENT_UDP_BOOL = ("client_udp_region_head_check",)
REVIEW_CLIENT_UDP_COUNT = ("client_udp_keepalive_ms", "client_udp_datagram_capacity")
# Wave 8 local proofs: on every mesh node, the av-mesh flag of the same name.
PROOF_MESH_BOOL = ("assignment_udp_in_region",)
# Wave 9 (cold start): on every mesh node, and on the nodes that serve UDP
# clients, the av-mesh flag of the same name.
COLD_MESH_BOOL = ("malloc_tune", "prefault")
COLD_CLIENT_UDP_BOOL = ("client_udp_prompt_sender", "client_udp_assign")
# Wave 9 C (autoscaling and redistribution): on every mesh node, and on the
# nodes that serve UDP clients, the av-mesh flag of the same name.
SCALE_MESH_BOOL = ("cpu_load", "assignment_move_headroom")
SCALE_MESH_COUNT = ("cpu_load_full_percent", "rebalance_target_permille")
SCALE_CLIENT_UDP_BOOL = ("client_udp_rebalance",)
SCALE_CLIENT_UDP_COUNT = ("client_udp_rebalance_margin_permille",
                          "client_udp_rebalance_interval_ms")


def is_contributor_args(args: list[str]) -> bool:
    """Whether `args` are av-contrib's: it alone takes the DAW media ingest
    (and, with audio_epoch_lane, the AEP1 ingress targets)."""
    return "--daw-media-bind" in args or "--audio-epoch-ingress-target" in args


def contributor_class_args(node: dict, plan: dict) -> list[str]:
    """av-contrib arguments of class isolation and the audio hot path. Every
    flag is rendered, on or off, so that a setting of false is the
    behaviour before the change whatever the binary's defaults are."""
    split = plan["relay_class_split"]
    hot = plan["audio_hot_path"]
    return [
        "--relay-class-lanes", flag(split),
        "--relay-class-port-offset", str(plan["class_port_offset"]),
        "--relay-audio-socket-priority", str(AUDIO_SOCKET_PRIORITY if split else 0),
        "--relay-video-pacing-mbps", str(plan["relay_video_pacing_mbps"] if split else 0),
        "--relay-audio-inline", flag(hot),
        "--audio-hot-thread", flag(hot),
        "--audio-hot-cpu", str(audio_hot_cpu(node, plan)),
        "--relay-repair-both-lanes", flag(plan["relay_repair_both_lanes"]),
        "--relay-video-gso", flag(plan.get("relay_video_gso", True)),
    ]


def mesh_class_args(node: dict, args: list[str], plan: dict) -> list[str]:
    """av-mesh arguments of class isolation for a node rendered with `args`:
    the cache-mesh class socket on every node (every node of a mesh uses the
    same offset), the forward-first class threads on relays and
    distributors, and the client socket pair where clients are served."""
    out = ["--cache-mesh-class-port-offset",
           str(plan["class_port_offset"] if plan["cache_mesh_class_split"] else 0),
           "--canonical-repair-horizon", plan.get("canonical_repair_horizon", "auto")]
    if "--relay-controlled-local" in args:
        split = plan["relay_class_split"]
        out += ["--relay-class-threads", flag(split),
                "--relay-class-port-offset", str(plan["class_port_offset"])]
        if split:
            out += ["--relay-class-realtime", flag(plan["relay_class_realtime"]),
                    "--relay-video-gso", flag(plan.get("relay_video_gso", True)),
                    "--relay-burst-copy-ms", str(plan.get("relay_burst_copy_ms", 0))]
            if plan.get("relay_video_gro") is not None:
                out += ["--relay-video-gro", flag(bool(plan["relay_video_gro"]))]
            cpus = relay_class_cpus(node, plan)
            if cpus:
                out += ["--relay-class-cpus", cpus]
        # The budget on a stream's own timeline within which a relay node
        # still takes a part (media_deadline_ms, the contributors'
        # --relay-deadline-ms): a part that far behind its stream's head is
        # shed, whatever any wall clock says.
        out += binary_takes("av-mesh", [
            "--relay-media-deadline-ms",
            str(plan.get("media_deadline_ms", DEFAULT_SETTINGS["media_deadline_ms"]))])
    if "--client-udp-bind" in args:
        out += ["--client-udp-class-sockets", flag(plan["client_udp_class_sockets"])]
    return out


# The arguments that bind a UDP socket, and whether the class split gives the
# socket a video twin at the port plus the offset ("relay": with
# relay_class_split on a relay or distributor; "mesh": with
# cache_mesh_class_split). A value is ADDR:PORT, or BIND=... for the relay
# forward and failover arguments. The HTTP port is QUIC (HTTP/3,
# WebTransport) too.
UDP_BIND_ARGS = {
    "--fec-bind": "relay", "--relay-primary-bind": "relay",
    "--relay-secondary-bind": "relay", "--mesh-bind": "mesh",
    "--media-fec-bind": None, "--telemetry-bind": None, "--client-udp-bind": None,
    "--relay-forward": None, "--relay-failover-listener": None,
    "--relay-failover-controller": None, "--relay-direct-bind": None,
    # The lease children's video socket is a twin on the same address.
    "--relay-lease-bind": None, "--relay-lease-failover-bind": None,
    "--daw-media-bind": None, "--rist-bind": None, "--srt-bind": None,
    "--http-port": None,
}


# The binaries' defaults of the class flags, for a service whose arguments do
# not give one (the render gives every one).
CLASS_FLAG_DEFAULTS = {"--relay-class-lanes": "true", "--relay-class-threads": "true",
                       "--relay-class-port-offset": "1000",
                       "--cache-mesh-class-port-offset": "1000"}


def class_offsets(args: list[str]) -> dict | None:
    """The class ports that a service started with `args` uses: "relay", the
    offset of the video twin of its relay ports (the contributor's lanes send
    video there; a relay's or distributor's class threads listen there and
    forward video to its children's ports plus it; 0 without the split), and
    "mesh", its cache-mesh offset (None for the contributor). None for a
    service that takes no part in either."""
    def value(flag: str) -> str:
        found = flag_value(args, flag)
        return CLASS_FLAG_DEFAULTS[flag] if found is None else found

    def whole(flag: str) -> int:
        try:
            return int(value(flag))
        except ValueError:
            raise ValueError(f"{flag} {value(flag)!r} is not a port offset") from None

    if is_contributor_args(args):
        on = value("--relay-class-lanes") == "true"
        return {"relay": whole("--relay-class-port-offset") if on else 0, "mesh": None}
    given = flag_names(args)
    if not given & {"--mesh-bind", "--cache-mesh-class-port-offset", "--relay-controlled-local"}:
        return None
    out = {"relay": None, "mesh": whole("--cache-mesh-class-port-offset")}
    if "--relay-controlled-local" in args:
        on = value("--relay-class-threads") == "true"
        out["relay"] = whole("--relay-class-port-offset") if on else 0
    return out


def check_class_agreement(rendered: dict[str, dict]) -> None:
    """Raise ValueError when the services would not agree on the class ports:
    the contributor and every relay and distributor must split the relay
    ports with one offset or not at all (a parent that splits sends its
    video to the child's port plus the offset, where a child that does not
    has no socket, and a child that splits waits there for video that a
    parent that does not never sends), and every cache-mesh node must use
    one cache-mesh offset. An extra argument of one role can break it."""
    problems = []
    for kind, what in (("relay", "relay class split (--relay-class-lanes, --relay-class-threads, "
                                 "--relay-class-port-offset)"),
                       ("mesh", "cache-mesh class offset (--cache-mesh-class-port-offset)")):
        groups: dict[int, list[str]] = {}
        for name, item in sorted(rendered.items()):
            offsets = class_offsets(item["args"])
            if offsets is not None and offsets[kind] is not None:
                groups.setdefault(offsets[kind], []).append(name)
        if len(groups) > 1:
            problems.append(f"the {what} differs: " + "; ".join(
                f"offset {offset} on {', '.join(names)}" for offset, names in sorted(groups.items())))
    if problems:
        raise ValueError("the services disagree on the class ports, check extra_*_args: "
                         + " | ".join(problems))


# av-mesh flags that went with the merged playlists line (playlists f592da0):
# av-mesh refuses them at start.
REMOVED_MESH_FLAGS = {
    "--mesh-sync-on-publish": "the cache mesh always sends on publish",
    "--mesh-liveness-on-receipt": "a peer is always heard on the receipt of any datagram",
    "--mlock-all": "--mlock rings locks each prefaulted ring (the mlock setting)",
}


def check_cache_mesh_peers(state: dict, rendered: dict[str, dict],
                           settings: dict | None = None) -> None:
    """Raise ValueError when an av-mesh service would not start or its cache
    mesh would refuse a peer: a flag that av-mesh no longer has
    (REMOVED_MESH_FLAGS), or a --peer that does not list its node back. The
    merged playlists mesh takes datagrams only from authorized peers (--peer,
    or discovery's add_peer) and learns no child from its hello, so a
    distributor must list its edges and an edge its distributors. A peer's
    class socket (its --mesh-bind port plus --cache-mesh-class-port-offset)
    is authorized with its main address, so a --peer covers both sockets.

    A peer that is a node of the roster held out of this render (a standby
    edge, an autoscale slot not in service: down until it starts) is
    checked by the rule that renders its own list when it starts (its
    site's nodes of the other cache-mesh role, and a distributor slot's
    pairing with its lease parents: mesh_peer_nodes)."""
    settings = settings if settings is not None else DEFAULT_SETTINGS
    problems = []
    for name, item in sorted(rendered.items()):
        if item["service"] != "needletail-mesh":
            continue
        for gone in sorted(flag_names(item["args"]) & set(REMOVED_MESH_FLAGS)):
            problems.append(f"{name}: av-mesh has no {gone} ({REMOVED_MESH_FLAGS[gone]})")
    peers = {name: [item["args"][index + 1] for index, arg in enumerate(item["args"][:-1])
                    if arg == "--peer"]
             for name, item in rendered.items() if item["service"] == "needletail-mesh"}
    address = {}
    for node in state["nodes"].values():
        bind = flag_value(rendered.get(node["name"], {}).get("args", []), "--mesh-bind")
        if node["name"] in peers and bind and not bind.startswith("0.0.0.0:"):
            address[bind] = node["name"]
    own = {name: bind for bind, name in address.items()}
    held = {f"{node['vlan_ipv4']}:{MESH_PORT}": node for node in roster(state).values()
            if node["name"] not in rendered and node.get("vlan_ipv4")
            and node["role"] in ("edge", "distributor")}
    for name, listed in sorted(peers.items()):
        for peer in listed:
            other = address.get(peer)
            if other is None and peer in held:
                node, down = state["nodes"].get(name) or roster(state)[name], held[peer]
                if node["name"] not in {item["name"] for item in
                                        mesh_peer_nodes(down, settings, state)}:
                    problems.append(f"{name} lists {down['name']} ({peer}), which would "
                                    f"not list {name} when it starts")
                continue
            if other is None:
                problems.append(f"{name}: --peer {peer} is no node's cache-mesh address")
            elif own.get(name) not in peers.get(other, []):
                problems.append(f"{name} lists {other} ({peer}) but {other} does not list "
                                f"{name}: its mesh refuses {name}'s datagrams")
    if problems:
        raise ValueError("the cache mesh would refuse a peer or a flag, check extra_*_args: "
                         + " | ".join(problems))


def udp_binds(args: list[str], plan: dict) -> list[tuple[str, int, str]]:
    """(socket, port, argument) of every UDP socket that a service rendered
    with `args` binds, the video twins of the class split included (from the
    service's own class flags, an extra argument included; `plan` where it
    gives none). Two arguments that name the same address are one socket
    (av-mesh's --fec-bind and --relay-primary-bind). The contributor's lane
    sockets have no twin port: its class pair shares the lane's endpoint."""
    contributor = is_contributor_args(args)
    relay = "--relay-controlled-local" in args
    offsets = {"relay": plan["class_port_offset"] if plan["relay_class_split"] else 0,
               "mesh": plan["class_port_offset"] if plan["cache_mesh_class_split"] else 0}
    if any(flag in flag_names(args) for flag in CLASS_FLAG_DEFAULTS):
        own = class_offsets(args) or {}
        offsets.update({kind: value for kind, value in own.items() if value is not None})
    pairs = []
    for index, arg in enumerate(args):
        if arg.startswith("--") and "=" in arg:
            pairs.append(tuple(arg.split("=", 1)))
        elif index + 1 < len(args):
            pairs.append((arg, args[index + 1]))
    out = []
    for arg, value in pairs:
        if arg not in UDP_BIND_ARGS:
            continue
        bind = value.split("=", 1)[0] if "=" in value else value
        if arg == "--http-port":
            bind = f"0.0.0.0:{value}"
        port = int(bind.rsplit(":", 1)[1])
        out.append((bind, port, arg))
        twin = UDP_BIND_ARGS[arg]
        offset = (offsets["relay"] if twin == "relay" and relay and not contributor
                  else offsets["mesh"] if twin == "mesh" else 0)
        if offset:
            out.append((f"{bind} video", port + offset, f"{arg} (video)"))
    return out


def check_class_ports(rendered: dict[str, dict], plan: dict) -> None:
    """Raise ValueError when a video class port is past 65535, collides with
    another socket of its node, or is a port that a compiled relay endpoint
    or another service uses anywhere in the lab (22001, 22002, 22200+,
    22301+, 22401+, 22501+, the cache mesh, the HTTP and client ports)."""
    problems = []
    base_ports: dict[int, str] = {}
    video_ports: dict[int, str] = {}
    for name, item in sorted(rendered.items()):
        sockets: dict[int, str] = {}
        for socket, port, arg in udp_binds(item["args"], plan):
            if port > 65535:
                problems.append(f"{name}: {arg} {socket} would bind port {port}")
                continue
            if sockets.setdefault(port, socket) != socket:
                problems.append(f"{name}: port {port} is bound by {sockets[port]} "
                                f"and {socket} ({arg})")
            target = video_ports if socket.endswith(" video") else base_ports
            target.setdefault(port, f"{name} {arg}")
    for port in sorted(set(base_ports) & set(video_ports)):
        problems.append(f"video class port {port} ({video_ports[port]}) is the port of "
                        f"{base_ports[port]}")
    if problems:
        raise ValueError("class ports collide (class_port_offset "
                         f"{plan['class_port_offset']}): " + "; ".join(problems))


def check_merged(state: dict, rendered: dict[str, dict]) -> None:
    """Raise ValueError when a render of the merged topology is not its
    shape: each contributor's two lanes end at two distributors of its own
    site and it has no direct lane (--relay-direct-target) to anything; the
    standby contributor's lanes end where the contributor's do; each of
    those first forwarders admits each contributor's lane from the address
    it is sent from, on the port it is sent to (the contributor on its
    primary lane, the standby on its secondary lane); no backbone relay
    runs; every other distributor takes its two parents among the first
    forwarders. An edge takes relay parents only on the fast plane, from
    the distributors of its own site (check_fast_plane checks those). An
    extra argument of a role can break it."""
    problems = []
    by_id = {relay_node_id(node): node for node in state["nodes"].values()}
    first: list = []
    for contrib in common.contributors(state):
        who = f"the contributor {contrib['name']}"
        contrib_args = rendered[contrib["name"]]["args"]
        if "--relay-direct-target" in flag_names(contrib_args):
            problems.append(f"{who} has a --relay-direct-target")
        lanes = [flag_value(contrib_args, "--relay-primary-id"),
                 flag_value(contrib_args, "--relay-secondary-id")]
        for node_id in lanes:
            node = by_id.get(node_id or "")
            if not node or node["role"] != "distributor" or node["site"] != contrib["site"]:
                problems.append(f"{who} feeds {node_id}, not a distributor of "
                                f"its site {contrib['site']}")
        if len(set(lanes)) != 2:
            problems.append(f"the two lanes of {who} end at {lanes}")
        if not first:
            first = lanes
        elif lanes != first:
            problems.append(f"the lanes of {who} end at {lanes}, the contributor's at {first}")
        # The forwarder at the end of a lane takes the contributor as its
        # primary parent and the standby on its secondary lane.
        taken = "secondary" if contrib.get("standby") else "primary"
        for lane, node_id in zip(("primary", "secondary"), lanes):
            node = by_id.get(node_id or "")
            if not node or node["name"] not in rendered:
                continue
            node_args = rendered[node["name"]]["args"]
            sent_from = flag_value(contrib_args, f"--relay-{lane}-bind")
            sent_to = flag_value(contrib_args, f"--relay-{lane}-target") or ""
            bind = flag_value(node_args, f"--relay-{taken}-bind") or ""
            if (flag_value(node_args, f"--relay-{taken}-id") != relay_node_id(contrib)
                    or flag_value(node_args, f"--relay-{taken}-peer") != sent_from
                    or bind.rsplit(":", 1)[-1] != sent_to.rsplit(":", 1)[-1]):
                problems.append(
                    f"{node['name']} does not admit the {lane} lane of {who} "
                    f"({sent_from} to {sent_to}) on its {taken} lane: it takes "
                    f"{flag_value(node_args, f'--relay-{taken}-id')} from "
                    f"{flag_value(node_args, f'--relay-{taken}-peer')} on {bind or None}")
    for name, item in sorted(rendered.items()):
        node = state["nodes"][name]
        args = item["args"]
        if node["role"] == "backbone":
            problems.append(f"{name}: a backbone relay is rendered")
        elif node["role"] == "edge" and "--relay-controlled-local" in args:
            for lane in ("primary", "secondary"):
                parent = by_id.get(flag_value(args, f"--relay-{lane}-id") or "")
                if parent is not None and parent["role"] != "distributor":
                    problems.append(f"{name}: an edge takes the {parent['role']} "
                                    f"{parent['name']} as its {lane} relay parent")
        elif node["role"] == "distributor" and name in slot_names(state):
            # A slot forwarder takes its site's distributors on the fast
            # plane (check_fast_plane), never a compiled relay parent.
            if "--relay-forward" in flag_names(args):
                problems.append(f"{name}: an autoscale slot is rendered as a compiled relay")
        elif node["role"] == "distributor" and relay_node_id(node) not in first:
            parents = [flag_value(args, "--relay-primary-id"),
                       flag_value(args, "--relay-secondary-id")]
            if None in parents or set(parents) != set(first):
                problems.append(f"{name}: parents {parents}, not the first forwarders {first}")
    if problems:
        raise ValueError("the merged topology is not rendered as it is compiled: "
                         + "; ".join(problems))


# What two contributors must be started alike with: the flags that decide
# the bytes of an audio part and of its datagrams (packaging, formats, stream
# ids, part, segment, deadline, clock confidence, the RelaySession identity,
# the compact form), those that decide which datagrams are sent (the whole
# part's grid, the repair floor, the encoder path) and the nodes their two
# lanes end at. Rendered or left to the binary's default, they must be the
# same on both: the first forwarders merge the two by first arrival and
# compare nothing but the key.
CONTRIBUTOR_PAIR_FLAGS = (
    "--daw-hls-packaging", "--daw-hls-formats", "--daw-hls-base-stream-id", "--audio-part",
    "--fmp4-segment-ms", "--relay-deadline-ms", "--wall-clock-estimated-error-ms",
    "--relay-topology-generation", "--relay-subscription-id", "--relay-compact-audio",
    "--relay-compact-whole-ms", "--relay-min-repair-symbols", "--relay-send-batch",
    "--relay-source-first", "--relay-primary-id", "--relay-secondary-id")
# And every flag of the path observations that seed their repair.
CONTRIBUTOR_PAIR_PREFIXES = ("--relay-path-", "--relay-secondary-path-")


def contributor_identity(args: list[str]) -> dict | None:
    """The identity that the av-contrib of this render computes for a
    contributor started with `args`: the object that `av-contrib ARGS
    --print-fingerprint` prints (fingerprint, numbering, envelope, wire,
    fields), run here with the binary that the render is for and binding
    nothing. None when that binary has no --print-fingerprint or the render
    cannot see it. Raises ValueError when it has the flag and does not
    answer: an identity that cannot be read is not known to be equal."""
    if not binary_has("av-contrib", "--print-fingerprint"):
        return None
    return contributor_changes.identity_of(RENDER_BINARIES["av-contrib"], args)


def check_contributor_pair(state: dict, rendered: dict[str, dict]) -> None:
    """Raise ValueError unless the contributor and its standby are rendered
    to publish the same parts: opaque packaging on both (fMP4 audio is not
    numbered from the source), equal values of CONTRIBUTOR_PAIR_FLAGS and of
    the path observation flags, and, where the av-contrib of this render
    prints one, equal fingerprints. Nothing to check with one contributor.
    An extra argument cannot break it (both take the same); a setting of one
    node alone can."""
    pair = [node for node in common.contributors(state) if node["name"] in rendered]
    if len(pair) < 2:
        return
    one, other = (rendered[node["name"]]["args"] for node in pair[:2])
    names = f"{pair[0]['name']} and {pair[1]['name']}"
    problems = []
    for node, args in zip(pair, (one, other)):
        packaging = flag_value(args, "--daw-hls-packaging")
        if packaging != "opaque":
            problems.append(f"{node['name']} packages audio as {packaging}; two contributors "
                            "publish the same parts only with --daw-hls-packaging opaque")
    flags = set(CONTRIBUTOR_PAIR_FLAGS) | {
        name for name in flag_names(one) | flag_names(other)
        if name.startswith(CONTRIBUTOR_PAIR_PREFIXES)}
    for name in sorted(flags):
        values = (flag_value(one, name), flag_value(other, name))
        if values[0] != values[1]:
            problems.append(f"{name} is {values[0]} and {values[1]}")
    if not problems:
        identities = [contributor_identity(args) for args in (one, other)]
        if None not in identities and (identities[0]["fingerprint"]
                                       != identities[1]["fingerprint"]):
            parts = [part for part in ("numbering", "envelope", "wire")
                     if identities[0].get(part) != identities[1].get(part)]
            problems.append(f"their fingerprints are {identities[0]['fingerprint']} and "
                            f"{identities[1]['fingerprint']}"
                            + (f" ({', '.join(parts)} differ)" if parts else ""))
    if problems:
        raise ValueError(f"the contributors {names} would not publish the same parts: "
                         + "; ".join(problems))


def direct_region_args(state: dict, services: dict) -> tuple[list[str], dict[str, list[str]]]:
    """contributor_direct_region: the contributor's --relay-direct-* arguments
    for the distributors of its site, and for each of those distributors the
    arguments that register the contributor's direct source endpoint as a
    parent. A direct target is the port on which the distributor takes its
    primary backbone parent; the symbols are the same. The direct lanes are
    the contributor's alone: a standby contributor has none."""
    contrib = next((node for node in common.contributors(state)
                    if not node.get("standby")), None)
    mates = [node for node in common.nodes_by_role(state, "distributor")
             if contrib is not None and node["site"] == contrib["site"]]
    if not mates:
        return [], {}
    bind = f"{link_addresses(contrib, mates[0])[0]}:{CONTRIB_DIRECT_PORT}"
    contrib_args = ["--relay-direct-bind", bind]
    mesh_args = {}
    for node in sorted(mates, key=lambda item: item["name"]):
        source, target = link_addresses(contrib, node)
        if f"{source}:{CONTRIB_DIRECT_PORT}" != bind:
            continue
        port = services[("av_mesh", relay_node_id(node))]["primary_parent"]["bind"].rsplit(":", 1)[1]
        contrib_args += ["--relay-direct-target", f"{target}:{port}",
                         "--relay-direct-id", relay_node_id(node)]
        mesh_args[node["name"]] = ["--relay-direct-peer", bind,
                                   "--relay-direct-id", relay_node_id(contrib)]
    return contrib_args, mesh_args


def gap_hold_args(node: dict, settings: dict, state: dict) -> list[str]:
    """--canonical-gap-hold-ms for a distributor outside the contributors'
    region (canonical_gap_hold_long_haul_ms); nothing for any other node."""
    hold = settings.get("canonical_gap_hold_long_haul_ms")
    if not hold or node["role"] != "distributor":
        return []
    regions = {contributor["region"] for contributor in common.contributors(state)}
    if not regions or node["region"] in regions:
        return []
    return ["--canonical-gap-hold-ms", str(int(hold))]


def gap_hold_tradeoff(settings: dict) -> str | None:
    """What a long-haul gap hold below the media deadline gives up, or None
    when it is not below it (or renders nothing)."""
    hold = settings.get("canonical_gap_hold_long_haul_ms")
    deadline = settings.get("media_deadline_ms")
    if not hold or not deadline or int(hold) >= int(deadline):
        return None
    return (f"canonical_gap_hold_long_haul_ms={int(hold)} is below media_deadline_ms="
            f"{int(deadline)}: a part that reaches a distributor outside the contributor's "
            f"region {int(hold)}-{int(deadline)} ms late is not replicated to its edges (only "
            "hole repair or the replica request fetch it), so live readers there miss it; "
            "in exchange no part waits more than the hold behind a lost one. Set it to "
            f"{int(deadline)} (or 0) to keep every late part")


def fast_plane_feed(state: dict, plan: dict) -> dict:
    """Who feeds which edge on the fast plane (`edge_fast_plane`):
    - "edges": {edge name: {"node", "primary", "secondary" (None on a site
      of one distributor), "rank"}}. The parents are the first two
      distributors of the edge's site by name; edge i of the site (by name)
      takes the first as its primary when i is even, the second when it is
      odd, so that each is primary for half of the edges. Its rank
      (--client-udp-edge-rank) is i, distinct within the site.
    - "forwarders": {distributor name: {"node", "primary_children": [VLAN
      addresses of the edges it is primary for], "children": [every edge it
      feeds]}}.
    - "slot_forwarders": {distributor name: {"node", "primary",
      "secondary", "rank"}}: an autoscale slot of role distributor
      (dist-c) is a lease child of its site's two first distributors, as an
      edge is (primary the first), with the next rank after the edges'.
    Empty when the setting is off (or the relay class split is). An edge
    or a distributor without a VLAN address takes no part: the lease child
    is the edge's cache-mesh address, which is its VLAN address. Built from
    the roster (active_state): an edge held out or not created yet keeps
    its rank and its place in its forwarders' lists, so that no node's
    arguments change when it starts."""
    out: dict = {"edges": {}, "forwarders": {}, "slot_forwarders": {}}
    if not plan.get("edge_fast_plane"):
        return out
    by_name = lambda item: item["name"]  # noqa: E731
    every = {"nodes": roster(state)}
    slots = slot_names(state)
    for site in sorted({node["site"] for node in common.nodes_by_role(every, "edge")}):
        parents = sorted((node for node in common.nodes_by_role(every, "distributor")
                          if node["site"] == site and node.get("vlan_ipv4")
                          and node["name"] not in slots), key=by_name)[:2]
        if not parents:
            continue
        edges = sorted((node for node in common.nodes_by_role(every, "edge")
                        if node["site"] == site and node.get("vlan_ipv4")), key=by_name)
        children = [(edge, "edges") for edge in edges] + [
            (node, "slot_forwarders") for node in sorted(
                (item for item in common.nodes_by_role(every, "distributor")
                 if item["site"] == site and item.get("vlan_ipv4") and item["name"] in slots),
                key=by_name)]
        for rank, (edge, kind) in enumerate(children):
            turn = rank if kind == "edges" else 0
            primary = parents[turn % len(parents)]
            secondary = parents[(turn + 1) % len(parents)] if len(parents) > 1 else None
            out[kind][edge["name"]] = {"node": edge, "primary": primary,
                                       "secondary": secondary, "rank": rank}
            for parent in (primary, secondary):
                if parent is None:
                    continue
                entry = out["forwarders"].setdefault(
                    parent["name"], {"node": parent, "primary_children": [], "children": []})
                entry["children"].append(edge["name"])
                if parent is primary:
                    entry["primary_children"].append(edge["vlan_ipv4"])
    return out


def lease_bind(node: dict) -> str:
    """--relay-lease-bind of a forwarder: an explicit VLAN address, because
    an edge binds it as its parent's source address."""
    return f"{node['vlan_ipv4']}:{LEASE_PORT}"


def fast_edge_args(feed: dict, services: dict, settings: dict) -> tuple[list[str], list[str]]:
    """The arguments of an edge fed by the fast plane: (flags without a
    value, `--flag value` pairs). Audio comes in full from both forwarders,
    so both relationships are promoted; video in full from the primary and
    as repair from the secondary until the edge's failover controller
    promotes it. The RelaySession identity (generation, subscription) is the
    contributor's, from the compiled plan of the primary forwarder."""
    edge, primary, secondary = feed["node"], feed["primary"], feed["secondary"]
    service = services.get(("av_mesh", relay_node_id(primary))) or {}
    switches = ["--relay-controlled-local", "--relay-primary-promoted"]
    pairs = [
        "--fec-bind", f"0.0.0.0:{EDGE_RELAY_PORTS[0]}",
        "--relay-primary-bind", f"0.0.0.0:{EDGE_RELAY_PORTS[0]}",
        "--relay-primary-peer", lease_bind(primary),
        "--relay-primary-id", relay_node_id(primary),
    ]
    if secondary is not None:
        switches.append("--relay-secondary-promoted")
        pairs += [
            "--relay-secondary-bind", f"0.0.0.0:{EDGE_RELAY_PORTS[1]}",
            "--relay-secondary-peer", lease_bind(secondary),
            "--relay-secondary-id", relay_node_id(secondary),
            "--relay-failover-controller",
            f"{edge['vlan_ipv4']}:{EDGE_FAILOVER_PORT}="
            f"{secondary['vlan_ipv4']}:{LEASE_FAILOVER_PORT}",
            "--relay-primary-silence-ms", str(settings["failover_primary_silence_ms"]),
            "--relay-primary-recovery-ms", str(settings["failover_primary_recovery_ms"]),
            "--relay-secondary-warm-ms", str(settings["failover_secondary_warm_ms"]),
            "--relay-failover-heartbeat-ms", str(settings["failover_heartbeat_ms"]),
            "--relay-failover-lease-ms", str(settings["failover_lease_ms"]),
        ]
    pairs += [
        "--relay-topology-generation", str(service.get("topology_generation", 1)),
        "--relay-subscription-id", str(service.get("subscription_id", 1)),
        "--edge-fast-ingest", "true",
        "--client-udp-edge-rank", str(feed["rank"]),
    ]
    return switches, pairs


def slot_forwarder_feed(node: dict, state: dict, feed: dict, settings: dict) -> dict | None:
    """The forwarder entry of a distributor slot that feeds its site's edges
    (forwarder_slot_feeds_edges, AM 12): every edge of its site, none named
    a primary child (the edges' ranks decide); None when it feeds none."""
    if not settings.get("forwarder_slot_feeds_edges") or not feed.get("edges"):
        return None
    every = {"nodes": roster(state)}
    children = sorted(edge["name"] for edge in common.nodes_by_role(every, "edge")
                      if edge["site"] == node["site"] and edge["name"] in feed["edges"])
    if not children:
        return None
    return {"node": node, "children": children, "primary_children": []}


def fast_forwarder_args(forwarder: dict | None, settings: dict) -> list[str]:
    """`--flag value` pairs of a distributor that feeds edges on the fast
    plane (`forwarder`, an entry of fast_plane_feed's "forwarders"), or
    of one that feeds none (None: lease children off)."""
    if forwarder is None:
        return ["--relay-lease-children", "false"]
    node = forwarder["node"]
    pairs = [
        "--relay-lease-children", "true",
        "--relay-lease-bind", lease_bind(node),
        "--relay-lease-child-ports", f"{EDGE_RELAY_PORTS[0]},{EDGE_RELAY_PORTS[1]}",
        "--relay-max-lease-children",
        str(max(edges_per_forwarder(settings), len(forwarder["children"]))),
        "--relay-lease-failover-bind", f"{node['vlan_ipv4']}:{LEASE_FAILOVER_PORT}",
    ]
    if forwarder["primary_children"]:
        pairs += ["--relay-lease-primary-children", ",".join(forwarder["primary_children"])]
    return pairs


def check_fast_plane(state: dict, rendered: dict[str, dict]) -> None:
    """Raise ValueError when an edge that takes relay parents would not be
    fed: each parent is a distributor of its site whose --relay-lease-bind
    is the peer that the edge binds; its primary sends to its first port and
    names it a primary child, its secondary sends to its second port and
    does not, and its failover controller promotes the secondary's lease
    failover listener; and no two edges of a site share a fast-lane rank.
    An extra argument of a role can break it."""
    problems = []
    by_id = {relay_node_id(node): node for node in state["nodes"].values()}
    ranks: dict[tuple[str, str], str] = {}
    for name, item in sorted(rendered.items()):
        node = state["nodes"][name]
        args = item["args"]
        lease_child = node["role"] == "edge" or name in slot_names(state)
        if not lease_child or "--relay-controlled-local" not in args:
            continue
        address = node.get("vlan_ipv4") or node["public_ipv4"]
        rank = flag_value(args, "--client-udp-edge-rank") or "0"
        other = ranks.setdefault((node["site"], rank), name)
        if other != name:
            problems.append(f"{name} and {other} share --client-udp-edge-rank {rank}")
        lanes = (("primary", "--fec-bind", 0), ("secondary", "--relay-secondary-bind", 1))
        for lane, bind_flag, port_index in lanes:
            parent_id = flag_value(args, f"--relay-{lane}-id")
            peer = flag_value(args, f"--relay-{lane}-peer")
            if peer is None:
                continue
            parent = by_id.get(parent_id or "")
            if (not parent or parent["role"] != "distributor" or parent["site"] != node["site"]
                    or parent["name"] not in rendered):
                problems.append(f"{name}: {lane} relay parent {parent_id} is not a distributor "
                                f"of its site {node['site']}")
                continue
            parent_args = rendered[parent["name"]]["args"]
            if flag_value(parent_args, "--relay-lease-bind") != peer:
                problems.append(f"{name}: {lane} peer {peer} is not the --relay-lease-bind of "
                                f"{parent['name']}")
            ports = (flag_value(parent_args, "--relay-lease-child-ports")
                     or f"{EDGE_RELAY_PORTS[0]},{EDGE_RELAY_PORTS[1]}").split(",")
            bind = flag_value(args, bind_flag) or ""
            if bind.rsplit(":", 1)[-1] != ports[port_index]:
                problems.append(f"{name}: {bind_flag} {bind}, but {parent['name']} sends its "
                                f"{lane} children's symbols to port {ports[port_index]}")
            primaries = (flag_value(parent_args, "--relay-lease-primary-children") or "").split(",")
            if (address in primaries) != (lane == "primary"):
                problems.append(f"{name}: {parent['name']} is its {lane} forwarder but "
                                + ("does not name" if lane == "primary" else "names")
                                + " it in --relay-lease-primary-children")
            if lane == "secondary":
                controller = flag_value(args, "--relay-failover-controller") or ""
                listener = flag_value(parent_args, "--relay-lease-failover-bind")
                if not listener or controller.split("=", 1)[-1] != listener:
                    problems.append(f"{name}: its failover controller {controller or '(none)'} "
                                    f"does not reach {parent['name']}'s lease failover "
                                    f"listener {listener}")
    if problems:
        raise ValueError("the fast-plane feed of the edges is not rendered as planned: "
                         + "; ".join(problems))


def host_tag(node: dict) -> str | None:
    """The first 12 hex digits of the node's physical host (`host_uuid`,
    which the lab tool's `hosts` records), or None when it is not known."""
    digits = re.sub(r"[^0-9a-f]", "", str(node.get("host_uuid") or "").lower())
    return digits[:12] or None


def stall_args(kind: str, node: dict, settings: dict) -> list[str]:
    """The stall instruments' flags of the `kind` service ("av-mesh",
    "av-contrib") on `node` (D 2.9): the given settings, refused for a
    binary without their flag; the check's record (--node-warm-file) when
    the node has the check at boot, and its host (--host-tag) when the state
    knows it, each only to a binary that has the flag."""
    given = [("--mlock", "mlock", settings.get("mlock"))]
    if kind == "av-mesh":
        given += [(f"--{key.replace('_', '-')}", key, settings.get(key))
                  for key in ("stall_tick_us", "stall_journal_capacity")]
    out = []
    for name, key, value in given:
        if value is None:
            continue
        if binary_has(kind, name) is False:
            raise ValueError(f"{node['name']}: setting {key} needs {kind} {name}, which the "
                             f"{kind} deployed does not have")
        out += [name, str(value if key == "mlock" else whole_or_zero(value, key))]
    if kind != "av-mesh":
        return out
    if check_unit_wanted(settings) and binary_has(kind, "--node-warm-file") is not False:
        out += ["--node-warm-file", NODE_WARM_FILE]
    tag = host_tag(node)
    if tag and binary_has(kind, "--host-tag") is not False:
        out += ["--host-tag", tag]
    return out


def render(state: dict, lab_settings: dict, plan: dict) -> dict[str, dict]:
    """Return {node name: {service, args, rust_log}} for every service node,
    each rendered with its own settings (node_own_settings: its
    node_overrides on top of the lab's)."""
    services = {
        (item["service"], item["node_id"]): item for item in plan["services"]
    }
    check_settings(lab_settings)
    classes = class_plan(lab_settings)
    feed = fast_plane_feed(state, classes)
    direct_contrib, direct_mesh = (direct_region_args(state, services)
                                   if lab_settings.get("contributor_direct_region") else ([], {}))
    unknown = sorted(set(lab_settings.get("node_overrides") or {}) - set(roster(state)))
    if unknown:
        raise ValueError(f"node_overrides: {', '.join(unknown)} is no node of the lab")
    contributor_ids = {relay_node_id(node) for node in common.contributors(state)}
    UNRENDERED_FLAGS.clear()
    rendered: dict[str, dict] = {}
    for node in state["nodes"].values():
        settings = node_own_settings(lab_settings, node["name"])
        classes = class_plan(settings)
        role = node["role"]
        if role == "contributor":
            service = services.get(("av_contrib", relay_node_id(node)))
            if service is None:
                raise ValueError(f"{node['name']}: the compiled plan has no contributor "
                                 f"service {relay_node_id(node)}")
            args = contributor_args(service, settings, node)
            if not node.get("standby"):
                args += direct_contrib
            args += yield_to(list(settings["extra_contributor_args"]),
                             stall_args("av-contrib", node, settings))
            rendered[node["name"]] = {
                "service": "needletail-contrib", "args": args,
                "rust_log": settings["rust_log_contrib"]}
            continue
        if role not in ("backbone", "distributor", "edge"):
            continue
        service = services.get(("av_mesh", relay_node_id(node)))
        if role == "backbone" and service is None:
            continue  # the merged topology uses no backbone relay
        slot_forwarder = role == "distributor" and node["name"] in slot_names(state)
        if role == "distributor" and service is None and not slot_forwarder:
            raise ValueError(f"{node['name']}: the compiled plan has no relay service for it")
        args = common_mesh_args(node, settings, state)
        if role in ("backbone", "distributor") and not slot_forwarder:
            args += relay_args(service)
            # The contributor sends repair on its primary lane too: the
            # backbone at the end of that lane admits it. (The standby's
            # lanes are admitted in full already: relay_args.)
            if (classes["relay_repair_both_lanes"]
                    and service["primary_parent"]["parent_node_id"] in contributor_ids
                    and "--relay-primary-promoted" not in flag_names(
                        args + list(settings[f"extra_{role}_args"]))):
                args.append("--relay-primary-promoted")
            args += direct_mesh.get(node["name"], [])
        if slot_forwarder:
            # dist-c: a cache-mesh parent of its site's edges, fed as a
            # fast-plane lease child of its site's first two distributors.
            # With forwarder_slot_feeds_edges (AM 12) it feeds the edges that
            # rank it among their first two parents of a playlist, as a lease
            # parent, and subscribes at its forwarders for their leases;
            # otherwise it feeds no lease child of its own.
            args += cache_mesh_args(node, settings, state)
            args += ["--edge-webtransport"]
            extra = list(settings["extra_distributor_args"])
            args += yield_to(extra, gap_hold_args(node, settings, state))
            feed_item = feed["slot_forwarders"].get(node["name"])
            if feed_item is not None:
                switches, pairs = fast_edge_args(feed_item, services, settings)
                given = flag_names(extra)
                args += [switch for switch in switches if switch not in given]
                args += yield_to(extra, pairs)
            if classes["relay_class_split"]:
                args += yield_to(extra, fast_forwarder_args(
                    slot_forwarder_feed(node, state, feed, settings), settings))
                if feed_item is not None and slot_forwarder_feed(node, state, feed, settings):
                    args += yield_to(extra, ["--cache-mesh-upstream-demand", "true"])
        elif role == "backbone":
            args += ["--mesh-bind", f"0.0.0.0:{MESH_PORT}", "--edge-webtransport"]
            extra = list(settings["extra_backbone_args"])
        elif role == "distributor":
            args += cache_mesh_args(node, settings, state)
            args += ["--edge-webtransport"]
            extra = list(settings["extra_distributor_args"])
            args += yield_to(extra, gap_hold_args(node, settings, state))
            if classes["relay_class_split"]:
                args += yield_to(extra, fast_forwarder_args(
                    feed["forwarders"].get(node["name"]), settings))
        else:
            args += cache_mesh_args(node, settings, state)
            args += ["--edge-http3"]
            extra = list(settings["extra_edge_args"])
            if node["name"] in feed["edges"]:
                switches, pairs = fast_edge_args(feed["edges"][node["name"]], services, settings)
                given = flag_names(extra)
                args += [switch for switch in switches if switch not in given]
                args += yield_to(extra, pairs)
        # The tail of every av-mesh service, slot forwarders included (wave 7
        # review C5: a slot rendered without the wave 7 flags would run the
        # binaries' defaults while the other forwarders run the settings).
        if "--client-udp-bind" in args:
            args += yield_to(extra, ["--client-udp-fast-lane",
                                     flag(classes["client_udp_fast_lane"])])
        args += yield_to(extra, mesh_class_args(node, args, classes))
        args += yield_to(extra, two_path_args(args, settings))
        args += yield_to(extra, wave7_args(args, settings))
        args += yield_to(extra, stall_args("av-mesh", node, settings)) + extra
        rendered[node["name"]] = {
            "service": "needletail-mesh", "args": args,
            "rust_log": settings["rust_log_mesh"]}
    report_unrendered(lab_settings, rendered)
    check_class_agreement(rendered)
    check_cache_mesh_peers(state, rendered, lab_settings)
    check_class_ports(rendered, class_plan(lab_settings))
    check_fast_plane(state, rendered)
    if topology_mode(lab_settings) == "merged":
        check_merged(state, rendered)
    check_contributor_pair(state, rendered)
    out = common.ARTIFACT_DIR / "nodes"
    out.mkdir(parents=True, exist_ok=True)
    for name, item in rendered.items():
        (out / f"{name}.args").write_text("\n".join(item["args"]) + "\n")
    return rendered


def ensure_tls() -> None:
    tls = common.ARTIFACT_DIR / "tls"
    if (tls / "fullchain.pem").is_file() and (tls / "privkey.pem").is_file():
        return
    tls.mkdir(parents=True, exist_ok=True)
    name = common.TLS_SERVER_NAME
    run = lambda *command: subprocess.run(command, check=True, capture_output=True)
    run("openssl", "ecparam", "-name", "prime256v1", "-genkey", "-noout",
        "-out", str(tls / "ca.key"))
    run("openssl", "req", "-x509", "-new", "-key", str(tls / "ca.key"),
        "-sha256", "-days", "30", "-subj", "/CN=Needletail small-mesh lab CA",
        "-out", str(tls / "ca.crt"))
    # PKCS#8 ("BEGIN PRIVATE KEY"): the contributor's RTMP frontend does not
    # accept a SEC1 "EC PRIVATE KEY" document.
    run("openssl", "genpkey", "-algorithm", "EC",
        "-pkeyopt", "ec_paramgen_curve:P-256", "-out", str(tls / "privkey.pem"))
    run("openssl", "req", "-new", "-key", str(tls / "privkey.pem"),
        "-subj", f"/CN={name}", "-out", str(tls / "leaf.csr"))
    (tls / "leaf.ext").write_text(
        f"subjectAltName=DNS:{name},DNS:*.{name}\n"
        "keyUsage=digitalSignature\nextendedKeyUsage=serverAuth,clientAuth\n"
        "basicConstraints=CA:FALSE\n")
    run("openssl", "x509", "-req", "-in", str(tls / "leaf.csr"),
        "-CA", str(tls / "ca.crt"), "-CAkey", str(tls / "ca.key"),
        "-CAcreateserial", "-days", "30", "-sha256",
        "-extfile", str(tls / "leaf.ext"), "-out", str(tls / "leaf.crt"))
    (tls / "fullchain.pem").write_text(
        (tls / "leaf.crt").read_text() + (tls / "ca.crt").read_text())
    (tls / "privkey.pem").chmod(0o600)


BOOTSTRAP = r"""
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
if ! command -v chronyc >/dev/null 2>&1 || ! command -v iptables >/dev/null 2>&1 \
  || ! command -v jq >/dev/null 2>&1 || ! command -v rsync >/dev/null 2>&1; then
  for attempt in 1 2 3 4 5; do
    if apt-get update -q >/dev/null 2>&1 && apt-get install -y -q \
      chrony iptables iproute2 jq rsync procps >/dev/null 2>&1; then break; fi
    sleep 5
  done
fi
command -v chronyc >/dev/null
cat >/etc/sysctl.d/60-needletail-udp.conf <<'EOF'
net.core.rmem_max=67108864
net.core.wmem_max=67108864
net.core.rmem_default=8388608
net.core.wmem_default=8388608
net.core.netdev_max_backlog=4096
EOF
sysctl -q --system
if ! id needletail >/dev/null 2>&1; then
  useradd --system --home-dir /var/lib/needletail --create-home \
    --shell /usr/sbin/nologin needletail
fi
install -d -m 755 /etc/needletail /var/lib/needletail-test-media /var/lib/needletail-runs
install -d -m 750 -o needletail -g needletail /etc/needletail/tls
cat >/etc/chrony/sources.d/needletail.sources <<'EOF'
server time.cloudflare.com iburst minpoll 4 maxpoll 6
server time.google.com iburst minpoll 4 maxpoll 6
EOF
if grep -q '^pool ' /etc/chrony/chrony.conf; then
  sed -i 's/^pool .*/pool 2.debian.pool.ntp.org iburst minpoll 4 maxpoll 6/' /etc/chrony/chrony.conf
fi
systemctl disable --now systemd-timesyncd.service >/dev/null 2>&1 || true
systemctl enable chrony.service >/dev/null 2>&1
systemctl restart chrony.service
cat >/usr/local/bin/needletail-run-args <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
mapfile -t args <"$1"
exec "${args[@]}"
EOF
chmod 755 /usr/local/bin/needletail-run-args
ip -4 -br addr show | awk '{print $1"="$3}' | tr '\n' ' '
"""


def unit_text(service: str, rust_log: str, settings: dict) -> str:
    description = ("Needletail contributor" if service == "needletail-contrib"
                   else "Needletail AV Mesh node")
    threads = settings["tokio_worker_threads"]
    thread_line = f"Environment=TOKIO_WORKER_THREADS={threads}\n" if threads else ""
    for key, value in sorted((settings.get("node_env") or {}).items()):
        thread_line += f"Environment={key}={value}\n"
    if class_plan(settings)["service_capabilities"]:
        # SCHED_FIFO, nice -10 and pinning of the fast-plane and audio
        # threads; socket buffers beyond rmem_max (SO_RCVBUFFORCE). The
        # service is still the needletail user, and NoNewPrivileges holds.
        thread_line += "AmbientCapabilities=CAP_SYS_NICE CAP_NET_ADMIN\n"
    # D 2.3: a finite limit on both services, always: --mlock rings locks
    # its rings under it, and nothing can lock the whole process.
    thread_line += f"LimitMEMLOCK={MEMLOCK_LIMIT}\n"
    # D 2.4: the check at boot runs, once a boot, before the service starts.
    # Wants=, not Requires=: a check that fails, times out or is missing
    # still lets the service start (D 2.2: the service starts, and its
    # readiness keeps a node without a complete record out).
    check = (f"Wants={NODE_CHECK_UNIT}\nAfter={NODE_CHECK_UNIT}\n"
             if check_unit_wanted(settings) else "")
    # C 2.5: no start limit (with RestartSec=1, five crashes in ten seconds
    # would leave a media node failed for good); a stop is bounded by B's
    # edge drain (--stop-drain-ms 14000) plus the shutdown and a spare
    # (TimeoutStopSec=20, mesh_ops.UNIT_TIMEOUT_STOP_S); StateDirectory is
    # where an operator's drain is kept across a restart.
    return f"""[Unit]
Description={description} (small-mesh lab)
After=network-online.target
Wants=network-online.target
{check}StartLimitIntervalSec=0

[Service]
Type=simple
User=needletail
Group=needletail
Environment=RUST_LOG={rust_log}
{thread_line}ExecStart=/usr/local/bin/needletail-run-args /etc/needletail/{service}.args
Restart=always
RestartSec=1
TimeoutStopSec={int(mesh_ops.UNIT_TIMEOUT_STOP_S)}
StateDirectory=needletail
NoNewPrivileges=yes
PrivateDevices=yes
ProtectHome=yes
LimitNOFILE=1048576

[Install]
WantedBy=multi-user.target
"""


def check_unit_text(settings: dict) -> str | None:
    """needletail-node-check.service of a node with these settings (D 2.4),
    or None when it has no check at boot. A oneshot that stays active, so
    it runs once a boot (the services Want it and start after it): an
    optional warm pass, then N seconds of watching for stops and steal; its
    record is NODE_WARM_FILE. needletail-nodecheck exits 0 whatever its
    verdict, and the `-` on ExecStart makes any other exit (an old build
    that refuses a flag, a crash, a missing binary) a success too: the
    service starts, and its readiness keeps a node with another verdict or
    no record out."""
    if not check_unit_wanted(settings):
        return None
    seconds = whole_or_zero(settings.get("node_check_seconds") or 0, "node_check_seconds")
    stop_ms = whole_or_zero(settings["node_check_max_stop_ms"], "node_check_max_stop_ms")
    steal = whole_or_zero(settings["node_check_max_steal_permille"],
                          "node_check_max_steal_permille")
    warm = " --warm" if settings.get("node_warm") else ""
    return f"""[Unit]
Description=Needletail node check at boot (small-mesh lab)
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
RuntimeDirectory=needletail
RuntimeDirectoryPreserve=yes
TimeoutStartSec={70 + seconds}
ExecStart=-/usr/local/bin/needletail-nodecheck boot --out {NODE_WARM_FILE}{warm} \\
  --check-seconds {seconds} --max-stop-ms {stop_ms} --max-steal-permille {steal}
"""


def binaries_for(role: str, node: dict | None = None) -> list[pathlib.Path]:
    """The files that `push` installs on a node of `role`: the service's
    binary first (av-mesh, av-contrib), then its companions."""
    target = "target/x86_64-unknown-linux-gnu/release"
    # NEEDLETAIL_AV_MESH_ROOT selects another av-mesh checkout, such as a worktree
    # that carries a candidate patch.
    mesh = pathlib.Path(os.environ.get(
        "NEEDLETAIL_AV_MESH_ROOT", str(common.WORKSPACE / "av-mesh"))) / target
    # NEEDLETAIL_AV_CONTRIB_ROOT selects another av-contrib checkout.
    contrib = pathlib.Path(os.environ.get(
        "NEEDLETAIL_AV_CONTRIB_ROOT", str(common.WORKSPACE / "av-contrib"))) / target
    # NEEDLETAIL_DAW_NEXUS_ROOT selects the daw-nexus checkout of the source,
    # NEEDLETAIL_H3_CAPACITY_BIN the h3-static-capacity binary.
    daw = pathlib.Path(os.environ.get(
        "NEEDLETAIL_DAW_NEXUS_ROOT", str(common.WORKSPACE / "daw-nexus"))) / target
    h3_capacity = pathlib.Path(os.environ.get(
        "NEEDLETAIL_H3_CAPACITY_BIN",
        str(common.WORKSPACE / "av-mesh" / target / "h3-static-capacity")))
    probe = contrib / "aep1-48k-probe"
    # D 2.2: the check at boot and the stall hunt's tools, on every role.
    nodecheck = mesh / "needletail-nodecheck"
    if role == "contributor":
        return [contrib / "av-contrib", probe, nodecheck, daw / "daw-test-source"]
    if role == "load":
        tools = [probe, mesh / "needletail-reader", mesh / "needletail-udp-client",
                 daw / "daw-test-source", h3_capacity, nodecheck,
                 common.ROOT / "deploy" / "linode-small-mesh" / "soundkit-part-check"
                 / target / "soundkit-part-check"]
        if node is not None and common.load_purpose(node) == "listeners":
            # The simulated listeners of the scale run: needletail-load (UDP,
            # from av-mesh) and ntcmp (LL-HLS crowds, scripts/compare/gobuild.sh).
            tools += [mesh / "needletail-load", common.ROOT / "target" / "ntcmp"]
        return tools
    checker = (common.ROOT / "deploy" / "linode-small-mesh" / "soundkit-part-check"
               / target / "soundkit-part-check")
    return [mesh / "av-mesh", probe, checker, nodecheck]


def node_sysctls(settings: dict) -> dict:
    """Kernel settings of every mesh node, from the `node_sysctls` setting.

    A settings file that was saved before a default was added does not name
    it: the defaults apply to every key that the setting leaves out. An empty
    value removes a key."""
    merged = {**DEFAULT_SETTINGS["node_sysctls"], **(settings.get("node_sysctls") or {})}
    return {str(key): str(value) for key, value in merged.items() if str(value) != ""}


PRIOMAP = "1 2 2 2 1 2 0 0 1 1 1 1 1 1 1 1"
# Every NIC of the host (a device under /sys/class/net/*/device: eth0, and
# eth1 on the VLAN; not lo or the ifb of a loss injection). The prio root
# puts SO_PRIORITY 6 (the audio sockets of the contributor, the relays and
# the edges) in band 0 and priority 0 (video, HTTP, and any socket that sets
# no priority: the cache mesh's audio socket is in band 1 until the playlists
# build that av-mesh uses sets SO_PRIORITY 6 on it) in band 1. Band 1 is fq, so that SO_MAX_PACING_RATE paces a video socket and
# flows share it fairly; its per-flow limit holds a 4K part's burst of a few
# thousand datagrams, where fq's default of 100 would drop most of it.
HOST_QDISC_SCRIPT = {
    "prio": r"""
modprobe sch_prio 2>/dev/null || true
modprobe sch_fq 2>/dev/null || true
for path in /sys/class/net/*; do
  [ -e "$path/device" ] || continue
  dev=${path##*/}
  tc qdisc replace dev "$dev" root handle 1: prio bands 3 priomap PRIOMAP
  tc qdisc replace dev "$dev" parent 1:1 handle 10: pfifo limit 10000
  tc qdisc replace dev "$dev" parent 1:2 handle 20: fq limit 65536 flow_limit 4096
  echo "== $dev"
  tc -s qdisc show dev "$dev"
done
""".replace("PRIOMAP", PRIOMAP),
    "none": r"""
for path in /sys/class/net/*; do
  [ -e "$path/device" ] || continue
  dev=${path##*/}
  if tc qdisc show dev "$dev" | head -1 | grep -q '^qdisc prio 1: root'; then
    tc qdisc del dev "$dev" root
  fi
  echo "== $dev"
  tc -s qdisc show dev "$dev"
done
""",
}


def check_host_qdisc(output: str, qdisc: str) -> list[str]:
    """The NICs of a `HOST_QDISC_SCRIPT` output that do not carry the
    qdisc; ["no NIC"] when the output names none."""
    sections = {}
    for block in output.split("== ")[1:]:
        device, _, shown = block.partition("\n")
        sections[device.strip()] = shown
    if not sections:
        return ["no NIC"]
    wrong = []
    for device, shown in sorted(sections.items()):
        lines = [line for line in shown.splitlines() if line.startswith("qdisc ")]
        has_prio = bool(lines) and lines[0].startswith("qdisc prio 1: root")
        if qdisc == "prio":
            ok = (has_prio and any(line.startswith("qdisc pfifo 10: parent 1:1") for line in lines)
                  and any(line.startswith("qdisc fq 20: parent 1:2") for line in lines))
        else:
            ok = not has_prio
        if not ok:
            wrong.append(device)
    return wrong


def netem_under_host_qdisc(device: str, netem: str, qdisc: str) -> str:
    """Shell commands that put a fault (a `tc netem` specification) at the
    root of one NIC with the host qdisc `qdisc` as its child, so that a
    fault and the class isolation hold together: what leaves the netem
    leaves audio (band 0) first. The child's bands are FIFOs, as in the
    namespace lab (edge_lab.LAB_HOST_QDISC): fq under netem stalls a paced
    flow. With "none" the netem is the root alone, as before."""
    lines = [f"tc qdisc del dev {device} root 2>/dev/null || true",
             f"tc qdisc add dev {device} root handle 1: netem {netem}"]
    if qdisc == "prio":
        lines += [
            f"tc qdisc add dev {device} parent 1:1 handle 10: prio bands 3 priomap {PRIOMAP}",
            f"tc qdisc add dev {device} parent 10:1 handle 11: pfifo limit 10000",
            f"tc qdisc add dev {device} parent 10:2 handle 12: pfifo limit 50000",
        ]
    return "; ".join(lines) + "; "


def apply_host_qdisc(node: dict, settings: dict, clear: tuple[str, ...] = ()) -> str:
    """Put the host qdisc of `host_qdisc` on every NIC of a node, check it
    with `tc -s qdisc` and record what the node shows under the artifact
    directory (qdisc/<node>.txt). The roots of the devices in `clear` (a
    fault's netem) are deleted first, whatever they are."""
    qdisc = class_plan(settings)["host_qdisc"]
    first = "".join(f"tc qdisc del dev {device} root 2>/dev/null || true\n" for device in clear)
    output = common.ssh_text(node, "set -e\n" + first + HOST_QDISC_SCRIPT[qdisc], timeout=60)
    directory = common.ARTIFACT_DIR / "qdisc"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{node['name']}.txt").write_text(output)
    wrong = check_host_qdisc(output, qdisc)
    if wrong:
        raise RuntimeError(f"{node['name']}: host qdisc {qdisc} is not on {', '.join(wrong)}: "
                           f"{output[-600:]}")
    return output


# ------------------------------------------------- a node's files and its activation (C 2.5)

# The binaries of one build on a node: /opt/needletail/builds/<git>-<unix>/.
NODE_BUILDS = "/opt/needletail/builds"
ACTIVATE_PATH = "/opt/needletail/activate"
NODE_ETC = "/etc/needletail"
# B 3.12: on SIGTERM an edge waits at most this long for its listeners to
# leave (the unit's TimeoutStopSec covers it, unit_text).
EDGE_STOP_DRAIN_MS = 14000
# The last word of `--version`: `av-mesh 0.1.0 (3f2a9c1d77e0 1763900000)`.
VERSION_LINE = re.compile(r"\(([0-9A-Za-z._-]+) ([0-9]+)\)\s*$")
TIER_ROLE = {"edges": "edge", "forwarders": "distributor", "contributors": "contributor"}
SERVICE_ROLES = ("backbone", "distributor", "edge", "contributor")

# /opt/needletail/activate SERVICE --not-after UNIX_MS [--rollback], run
# detached by systemd-run (activate_node) so that a dropped ssh cannot split
# it. It refuses when the node's clock is past the deadline ("late") or when
# line 1 of what it would put in force is not an executable here; otherwise
# it installs what is staged and differs (kernel settings, the host qdisc,
# the unit), puts the staged arguments in force with the previous ones kept
# beside them (`.args` is never absent: a copy, then a rename), links the
# build's binaries into /usr/local/bin and restarts the service. With
# --rollback it puts `.args.prev` (and the unit before the last activation)
# back instead. Line 1 must name a binary of a build directory: an `.args.prev`
# of the layout before builds (/usr/local/bin/av-mesh) is refused, never put
# in force. Its result is one JSON line in /run/needletail. The test of the
# shell (test_merged_topology's Activation) replaces the six paths of its
# head.
ACTIVATE_SCRIPT = r"""#!/usr/bin/env bash
set -u
ETC=/etc/needletail
RUN=/run/needletail
BIN=/usr/local/bin
UNITS=/etc/systemd/system
SYSCTL=/etc/sysctl.d/90-needletail.conf
BUILDS=/opt/needletail/builds

service=${1:?usage: activate SERVICE --not-after UNIX_MS [--rollback]}
shift
not_after=""
rollback=0
while [ $# -gt 0 ]; do
  case "$1" in
    --not-after) not_after=${2:-}; shift 2 ;;
    --rollback) rollback=1; shift ;;
    *) echo "activate: unknown argument $1" >&2; exit 2 ;;
  esac
done
mkdir -p "$RUN"
args="$ETC/$service.args"
unit="$UNITS/$service.service"
result="$RUN/activate-$service.json"
from=$(head -n 1 "$args" 2>/dev/null || true)
to=""
qdisc=false
finish() {
  printf '{"ok": %s, "why": "%s", "unix_ms": %s, "from": "%s", "to": "%s", "qdisc": %s}\n' \
    "$1" "$2" "$(date +%s%3N)" "$from" "$to" "$qdisc" >"$result.tmp"
  mv -f "$result.tmp" "$result"
  if [ "$1" = true ]; then exit 0; fi
  exit 1
}
case "$not_after" in ''|*[!0-9]*) finish false "no deadline" ;; esac
[ "$(date +%s%3N)" -le "$not_after" ] || finish false late
if [ "$rollback" = 1 ]; then source="$args.prev"; else source="$args.next"; fi
[ -f "$source" ] || finish false "nothing to put in force: no $source"
to=$(head -n 1 "$source")
case "$(dirname "$to")" in
  "$BUILDS"/?*) ;;
  *) finish false "not a binary of a build in $BUILDS: $to" ;;
esac
{ [ -f "$to" ] && [ -x "$to" ]; } || finish false "not an executable on this node: $to"
if [ "$rollback" = 1 ]; then
  if [ -f "$ETC/$service.unit.prev" ] && ! cmp -s "$ETC/$service.unit.prev" "$unit"; then
    { cp "$ETC/$service.unit.prev" "$unit.tmp" && mv -f "$unit.tmp" "$unit" \
      && systemctl daemon-reload; } || finish false "the previous unit could not be put back"
  fi
  if [ -f "$args" ]; then cp -p "$args" "$args.failed"; fi
  { cp -p "$args.prev" "$args.tmp" && mv -f "$args.tmp" "$args"; } \
    || finish false "the previous arguments could not be put back"
else
  if [ -f "$ETC/$service.sysctl.next" ] && ! cmp -s "$ETC/$service.sysctl.next" "$SYSCTL"; then
    modprobe tcp_bbr 2>/dev/null || true
    { cp "$ETC/$service.sysctl.next" "$SYSCTL.tmp" && mv -f "$SYSCTL.tmp" "$SYSCTL" \
      && sysctl -q -p "$SYSCTL"; } || finish false "the kernel settings failed"
  fi
  # The applied qdisc is remembered in /run, so a host that rebooted gets
  # it again at its next activation.
  if [ -f "$ETC/$service.qdisc.next" ] && ! cmp -s "$ETC/$service.qdisc.next" "$RUN/$service.qdisc"; then
    bash "$ETC/$service.qdisc.next" >"$RUN/qdisc-$service.txt" 2>&1 \
      || finish false "the host qdisc failed: $(tail -n 1 "$RUN/qdisc-$service.txt" | tr -d '"\\\000-\037')"
    cp "$ETC/$service.qdisc.next" "$RUN/$service.qdisc"
    qdisc=true
  fi
  # D 2.4: the check at boot's unit, before the unit that Wants= it.
  if [ -f "$ETC/$service.check.next" ] && ! cmp -s "$ETC/$service.check.next" "$UNITS/needletail-node-check.service"; then
    { cp "$ETC/$service.check.next" "$UNITS/needletail-node-check.service.tmp" \
      && mv -f "$UNITS/needletail-node-check.service.tmp" "$UNITS/needletail-node-check.service" \
      && systemctl daemon-reload; } || finish false "the node check unit could not be installed"
  fi
  if [ -f "$ETC/$service.unit.next" ]; then
    if [ -f "$unit" ]; then cp -p "$unit" "$ETC/$service.unit.prev"; fi
    if ! cmp -s "$ETC/$service.unit.next" "$unit"; then
      { cp "$ETC/$service.unit.next" "$unit.tmp" && mv -f "$unit.tmp" "$unit" \
        && systemctl daemon-reload; } || finish false "the unit could not be installed"
    fi
  fi
  if [ -f "$args" ]; then cp -p "$args" "$args.prev"; fi
  mv -f "$args.next" "$args" || finish false "the arguments could not be put in force"
fi
for file in "$(dirname "$to")"/*; do
  [ -f "$file" ] || continue
  name=$(basename "$file")
  ln -sfn "$file" "$BIN/.$name.new" && mv -Tf "$BIN/.$name.new" "$BIN/$name"
done
systemctl enable --quiet "$service.service" 2>/dev/null
systemctl restart "$service.service" || finish false "systemctl restart failed"
finish true ""
"""

# Builds on a node beyond the newest three are removed, never one that an
# `.args`, `.args.prev` or `.args.next` names (C 2.5).
PRUNE_BUILDS = r"""keep=" "
for file in /etc/needletail/*.args /etc/needletail/*.args.prev /etc/needletail/*.args.next; do
  if [ -f "$file" ]; then keep="$keep$(dirname "$(head -n 1 "$file")") "; fi
done
ls -1dt /opt/needletail/builds/*/ 2>/dev/null | tail -n +4 | while read -r dir; do
  dir=${dir%/}
  case "$keep" in *" $dir "*) ;; *) rm -rf "$dir" ;; esac
done"""


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def sha256_file(path: pathlib.Path) -> str | None:
    path = pathlib.Path(path)
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def args_text(args: list[str]) -> str:
    """An `.args` file: one argument a line, the binary first."""
    return "\n".join(args) + "\n"


def args_sha256(args: list[str]) -> str:
    """What av-mesh reports as `process.args_sha256` (C 2.3): every argument,
    argv[0] included, joined by newlines."""
    return sha256_text("\n".join(args))


# The value after these flags is the time of the render (build_program's
# path observation of the contributor): no later render can give it again.
RENDER_TIME_FLAG = "-observed-at-unix-ms"
# RENDER_SHA256 on a node: the same digest of a node's `.args` file.
RENDER_SHA256 = ("awk 'mask {{ print \"*\"; mask = 0; next }} /" + RENDER_TIME_FLAG
                 + "$/ {{ mask = 1 }} {{ print }}' {path} | head -c -1 | sha256sum | cut -d' ' -f1")


def render_sha256(args: list[str]) -> str:
    """args_sha256 of the arguments with the time of the render masked: what
    a later render of the same settings and build gives again."""
    masked, mask = [], False
    for arg in args:
        masked.append("*" if mask else arg)
        mask = not mask and arg.endswith(RENDER_TIME_FLAG)
    return sha256_text("\n".join(masked))


def build_of(path: pathlib.Path) -> dict:
    """The build that a binary names in `--version` (C 2.2, A 2.6):
    {"git", "unix", "dirty", "key"}; the key `<git>-<unix>` names its
    directory. Refused for a binary that is missing or names none."""
    path = pathlib.Path(path)
    if not path.is_file():
        raise mesh_ops.Refused(f"binary is missing: {path}")
    try:
        result = subprocess.run([str(path), "--version"], capture_output=True, text=True,
                                timeout=30, check=False)
    except (OSError, subprocess.SubprocessError) as error:
        raise mesh_ops.Refused(f"{path} --version does not run: {error}") from None
    match = VERSION_LINE.search(result.stdout.strip()) if result.returncode == 0 else None
    if not match:
        raise mesh_ops.Refused(f"{path} prints no build id (--version: "
                               f"{(result.stdout or result.stderr).strip()[:120]!r})")
    git, unix = match.group(1), int(match.group(2))
    return {"git": git, "unix": unix, "dirty": git.endswith("-dirty"), "key": f"{git}-{unix}"}


def key_build(key: str) -> dict:
    """{"git", "unix", "dirty"} of a build key `<git>-<unix>`."""
    git, _, unix = key.rpartition("-")
    return {"git": git, "unix": int(unix), "dirty": git.endswith("-dirty")}


def builds_dir() -> pathlib.Path:
    """small-mesh-artifacts/builds/: the binaries of every build deployed
    from this worktree, one directory a build key."""
    return common.ARTIFACT_DIR / "builds"


def cache_build(role: str) -> dict:
    """Copy the binaries that `push` installs on a node of `role` from the
    roots (binaries_for) into builds/<key>/, the key from the service's
    binary as copied (a build that finishes meanwhile cannot land under
    another's key); its build. A cached build is never written again: the
    copy goes in whole by a rename, and a root whose service binary names a
    cached build but differs from it is refused. Only push and roll take a
    build from the roots."""
    files = binaries_for(role)
    missing = [str(path) for path in files if not path.is_file()]
    if missing:
        raise mesh_ops.Refused(f"binary is missing: {', '.join(missing)}")
    builds_dir().mkdir(parents=True, exist_ok=True)
    copy = pathlib.Path(tempfile.mkdtemp(prefix=".copy-", dir=builds_dir()))
    try:
        for path in files:
            shutil.copy2(path, copy / path.name)
        build = build_of(copy / files[0].name)
        target = builds_dir() / build["key"]
        if not target.is_dir():
            try:
                copy.rename(target)
                return build
            except OSError:
                if not target.is_dir():
                    raise
        cached = target / files[0].name
        if not cached.is_file() or not filecmp.cmp(cached, copy / files[0].name, shallow=False):
            raise mesh_ops.Refused(
                f"{files[0]} names build {build['key']}, and {target} holds another "
                f"{files[0].name} of that build: a build id names one binary (rebuild it)")
        return build
    finally:
        shutil.rmtree(copy, ignore_errors=True)


def cached(key: str, binary: str) -> bool:
    """Whether builds/<key>/ holds the service's binary (a build directory
    is only ever made whole, cache_build and fetch_build)."""
    return (builds_dir() / key / binary).is_file()


def build_files(key: str) -> list[pathlib.Path]:
    return sorted(path for path in (builds_dir() / key).iterdir()
                  if path.is_file() and not path.name.startswith("."))


def build_flags(key: str, binary: str) -> frozenset:
    """The long flags that a cached build's binary lists in `--help`."""
    path = builds_dir() / key / binary
    if not path.is_file():
        return frozenset()
    return binary_flags(str(path), path.stat().st_mtime_ns) or frozenset()


def fetch_build(node: dict, key: str, binary: str) -> None:
    """A build that this worktree's cache lacks, from a node that has it:
    fetched aside and renamed into place only with its service binary, so
    an interrupted fetch is never taken for the build."""
    target = builds_dir() / key
    builds_dir().mkdir(parents=True, exist_ok=True)
    fetched = pathlib.Path(tempfile.mkdtemp(prefix=".fetch-", dir=builds_dir()))
    try:
        common.rsync_from(node, f"{NODE_BUILDS}/{key}/", str(fetched) + "/")
        if not (fetched / binary).is_file():
            raise RuntimeError(f"{NODE_BUILDS}/{key}/ on {node['name']} has no {binary}")
        if target.exists() and not cached(key, binary):
            shutil.rmtree(target)
        if not target.exists():
            fetched.rename(target)
    finally:
        shutil.rmtree(fetched, ignore_errors=True)


def installed_args(item: dict, role: str, key: str) -> list[str]:
    """The arguments that a node runs on build `key`: the render's, with the
    binary of that build on line 1 (C 2.5) and the node's lifecycle flags
    (C 2.2) where the build has them: the operator's drain kept in the
    state directory, and on an edge B's stop bound."""
    binary = "av-contrib" if item["service"] == "needletail-contrib" else "av-mesh"
    args = [f"{NODE_BUILDS}/{key}/{binary}", *item["args"][1:]]
    if binary == "av-mesh":
        flags = build_flags(key, binary)
        given = flag_names(args)
        if "--drain-state-file" in flags and "--drain-state-file" not in given:
            args += ["--drain-state-file", f"/var/lib/needletail/{item['service']}.drained"]
        if role == "edge" and "--stop-drain-ms" in flags and "--stop-drain-ms" not in given:
            args += ["--stop-drain-ms", str(EDGE_STOP_DRAIN_MS)]
    return args


# The end of a staged host qdisc script: what check_host_qdisc reads, on
# every NIC, or the script fails, and the activation refuses before it puts
# anything in force (a roll then gates again and stops, exit 3: the host,
# not the build).
HOST_QDISC_VERIFY = r"""
nics=0
for path in /sys/class/net/*; do
  [ -e "$path/device" ] || continue
  dev=${path##*/}
  nics=$((nics + 1))
  shown=$(tc qdisc show dev "$dev")
  root=$(printf '%s\n' "$shown" | grep '^qdisc ' | head -n 1)
  CHECK
done
[ "$nics" -gt 0 ] || { echo "the host qdisc: no NIC"; exit 1; }
"""
HOST_QDISC_CHECK = {
    "prio": r"""case "$root" in "qdisc prio 1: root"*) ;; *) echo "the host qdisc prio is not the root of $dev"; exit 1 ;; esac
  printf '%s\n' "$shown" | grep -q '^qdisc pfifo 10: parent 1:1' \
    || { echo "the host qdisc prio has no pfifo 10: on $dev"; exit 1; }
  printf '%s\n' "$shown" | grep -q '^qdisc fq 20: parent 1:2' \
    || { echo "the host qdisc prio has no fq 20: on $dev"; exit 1; }""",
    "none": r"""case "$root" in "qdisc prio 1: root"*) echo "the host qdisc prio is still on $dev"; exit 1 ;; esac""",
}


def node_files(args: list[str], unit: str, settings: dict) -> dict[str, str]:
    """The `.next` files of a service: its arguments, its unit, the kernel
    settings and the host qdisc script, which checks what it put on every
    NIC (HOST_QDISC_VERIFY)."""
    sysctls = node_sysctls(settings)
    qdisc = class_plan(settings)["host_qdisc"]
    files = {"args": args_text(args), "unit": unit,
             "sysctl": "".join(f"{key} = {value}\n" for key, value in sysctls.items()),
             "qdisc": "set -e\n" + HOST_QDISC_SCRIPT[qdisc]
             + HOST_QDISC_VERIFY.replace("CHECK", HOST_QDISC_CHECK[qdisc])}
    check = check_unit_text(settings)
    if check is not None:
        files["check"] = check
    return files


def stage_node(node: dict, service: str, key: str, files: dict[str, str]) -> None:
    """Files only (C 2.6): the build's binaries in /opt/needletail/builds/
    <key>/, the TLS files, the service's `.next` files and the activation
    script. Nothing is restarted, no kernel setting or qdisc is applied.
    Every file is checked on the node by its sha256; old builds are pruned
    (PRUNE_BUILDS)."""
    binaries = build_files(key)
    staging = common.ARTIFACT_DIR / "stage" / node["name"]
    staging.mkdir(parents=True, exist_ok=True)
    named = {}
    for suffix, text in files.items():
        path = staging / f"{service}.{suffix}.next"
        path.write_text(text)
        named[f"{NODE_ETC}/{path.name}"] = path
    activate = staging / "activate"
    activate.write_text(ACTIVATE_SCRIPT)
    remote = f"{NODE_BUILDS}/{key}"
    tls = common.ARTIFACT_DIR / "tls"
    common.ssh_text(node, f"mkdir -p {remote} {NODE_ETC}/tls")
    common.rsync_to(node, [str(path) for path in binaries], remote + "/")
    common.rsync_to(node, [str(tls / "fullchain.pem"), str(tls / "privkey.pem")],
                    f"{NODE_ETC}/tls/")
    common.rsync_to(node, [str(tls / "ca.crt")],
                    "/usr/local/share/ca-certificates/needletail-qualification.crt")
    common.rsync_to(node, [str(path) for path in named.values()], f"{NODE_ETC}/")
    common.rsync_to(node, [str(activate)], ACTIVATE_PATH)
    expected = {f"{remote}/{path.name}": sha256_file(path) for path in binaries}
    expected.update({remote_path: sha256_file(path) for remote_path, path in named.items()})
    expected[ACTIVATE_PATH] = sha256_text(ACTIVATE_SCRIPT)
    for name in ("fullchain.pem", "privkey.pem"):
        expected[f"{NODE_ETC}/tls/{name}"] = sha256_file(tls / name)
    output = common.ssh_text(node, "set -e\n" + "\n".join([
        f"chown needletail:needletail {NODE_ETC}/tls/*.pem",
        f"chmod 644 {NODE_ETC}/tls/fullchain.pem "
        "/usr/local/share/ca-certificates/needletail-qualification.crt",
        f"chmod 600 {NODE_ETC}/tls/privkey.pem",
        f"chmod 644 {' '.join(sorted(named))}",
        f"chmod 755 {ACTIVATE_PATH}",
        f"touch {remote}",
        PRUNE_BUILDS,
        "sha256sum " + " ".join(sorted(expected)),
    ]))
    found = {}
    for line in output.splitlines():
        parts = line.split()
        if len(parts) == 2:
            found[parts[1]] = parts[0]
    wrong = sorted(path for path, digest in expected.items() if found.get(path) != digest)
    if wrong:
        raise RuntimeError(f"{node['name']}: staged files differ on the node: {', '.join(wrong)}")


def activate_node(node: dict, service: str, not_after_ms: int, rollback: bool = False) -> dict:
    """Run the node's activation script detached and return once its result
    is written: {"ok", "why"} (why "late": refused at its deadline), with
    what is then in force: "args_sha256", "render_sha256", "binary",
    "unit_sha256", and the
    host qdisc's output when it was applied ("qdisc", else None)."""
    wait_s = max(0.0, not_after_ms / 1000 - time.time()) + mesh_ops.UNIT_TIMEOUT_STOP_S + 10
    result = f"/run/needletail/activate-{service}.json"
    command = (f"{ACTIVATE_PATH} {service} --not-after {int(not_after_ms)}"
               + (" --rollback" if rollback else ""))
    script = f"""rm -f {result}
if ! systemd-run --quiet --collect --unit needletail-activate-{service} {command} >/dev/null 2>&1; then
  echo '{{"ok": false, "why": "systemd-run refused it: an activation of {service} may be running"}}'
else
  i=0
  while [ ! -f {result} ] && [ $i -lt {int(wait_s * 10)} ]; do sleep 0.1; i=$((i + 1)); done
  if [ -f {result} ]; then head -n 1 {result}
  else echo '{{"ok": false, "why": "no result in {wait_s:.0f} s"}}'; fi
fi
echo "args_sha256 $(head -c -1 {NODE_ETC}/{service}.args 2>/dev/null | sha256sum | cut -d' ' -f1)"
echo "render_sha256 $({RENDER_SHA256.format(path=f"{NODE_ETC}/{service}.args")})"
echo "binary $(head -n 1 {NODE_ETC}/{service}.args 2>/dev/null)"
echo "unit_sha256 $(sha256sum /etc/systemd/system/{service}.service 2>/dev/null | cut -d' ' -f1)"
if grep -q '"qdisc": true' {result} 2>/dev/null; then
  echo "--- qdisc"; cat /run/needletail/qdisc-{service}.txt
fi
"""
    output = common.ssh_text(node, script, timeout=int(wait_s) + 60)
    head, _, qdisc = output.partition("--- qdisc\n")
    lines = head.splitlines()
    try:
        outcome = json.loads(lines[0])
    except (IndexError, ValueError):
        outcome = {"ok": False, "why": f"no readable result: {output[-300:]!r}"}
    out = {"ok": outcome.get("ok") is True, "why": str(outcome.get("why") or ""),
           "qdisc": qdisc if qdisc else None}
    for line in lines[1:]:
        name, _, value = line.partition(" ")
        if name in ("args_sha256", "render_sha256", "binary", "unit_sha256"):
            out[name] = value.strip() or None
    return out


def qdisc_checked(name: str, output: str | None, settings: dict) -> None:
    """The host qdisc that an activation applied (its output, None when it
    applied none) is on every NIC; the output is kept in qdisc/<node>.txt."""
    if output is None:
        return
    qdisc = class_plan(settings)["host_qdisc"]
    directory = common.ARTIFACT_DIR / "qdisc"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{name}.txt").write_text(output)
    wrong = check_host_qdisc(output, qdisc)
    if wrong:
        raise RuntimeError(f"{name}: host qdisc {qdisc} is not on {', '.join(wrong)}: "
                           f"{output[-600:]}")


def install_tools(node: dict, settings: dict, binaries: bool) -> str:
    """A host without a service (a load host): its tools as plain files in
    /usr/local/bin, the TLS files, and its kernel settings at once."""
    if binaries:
        sources = [str(path) for path in binaries_for(node["role"], node)]
        for source in sources:
            if not pathlib.Path(source).is_file():
                raise RuntimeError(f"binary is missing: {source}")
        common.rsync_to(node, sources, "/usr/local/bin/")
    tls = common.ARTIFACT_DIR / "tls"
    common.rsync_to(node, [str(tls / "fullchain.pem"), str(tls / "privkey.pem")],
                    f"{NODE_ETC}/tls/")
    common.rsync_to(node, [str(tls / "ca.crt")],
                    "/usr/local/share/ca-certificates/needletail-qualification.crt")
    commands = [
        f"chown needletail:needletail {NODE_ETC}/tls/*.pem",
        f"chmod 644 {NODE_ETC}/tls/fullchain.pem "
        "/usr/local/share/ca-certificates/needletail-qualification.crt",
        f"chmod 600 {NODE_ETC}/tls/privkey.pem",
    ]
    sysctls = node_sysctls(settings)
    if sysctls:
        lines = "\n".join(f"{key} = {value}" for key, value in sysctls.items())
        commands += [
            "modprobe tcp_bbr 2>/dev/null || true",
            f"cat >/etc/sysctl.d/90-needletail.conf <<'SYSCTL'\n{lines}\nSYSCTL",
            "sysctl -q -p /etc/sysctl.d/90-needletail.conf",
        ]
    if common.load_purpose(node) == "listeners":
        lines = "\n".join(f"{key} = {value}" for key, value in LISTENER_SYSCTLS.items())
        commands += [
            f"cat >/etc/sysctl.d/91-needletail-listeners.conf <<'SYSCTL'\n{lines}\nSYSCTL",
            "sysctl -q -p /etc/sysctl.d/91-needletail-listeners.conf",
        ]
    return common.ssh_text(node, "set -e\n" + "\n".join(commands))


def stop_service(node: dict, service: str = "needletail-mesh") -> str:
    """Stop a service that must not run and keep it from starting: a
    backbone relay the merged topology does not use (one left from a legacy
    deploy would still send to the distributors), a standby edge."""
    return common.ssh_text(node, f"systemctl disable --now {service}.service >/dev/null 2>&1 "
                                 "|| true")


# ------------------------------------------------------------ what is deployed (C 2.6)

@functools.lru_cache(maxsize=None)
def lab_tool():
    """The lab tool as a module: its watchdog check and its state lock
    (C 2.8), which the deploy's record and checks go through."""
    spec = importlib.util.spec_from_file_location(
        "small_mesh_lab", common.ROOT / "scripts" / "linode-small-mesh-lab.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# What the deploy calls of the lab tool in-process (C CT3).
LAB_TOOL_CALLS = ("change_state", "watchdog_ensure")

# What `replace` calls of the lab tool in-process to find, before it
# touches the node, what the lab tool's replace and add would refuse (C
# CT3): LinodeOps.lab_refusals.
REPLACE_LAB_CALLS = ("watchdog_problems", "MAX_LIFE_HOURS", "ADD_CUTOFF_S", "without_sites",
                     "Api", "owned_instance", "topology_cost", "lab_ceilings", "lab_hourly",
                     "removed_record", "held_to_budget")

# How long after a replace begins its last create or rebuild may come:
# a forwarder's slot created, pushed and ready, the forwarder drained to no
# pairs and gated (C 3.7). The lab's watchdog must be at least this beyond
# the lab tool's own cutoff (ADD_CUTOFF_S) when the replace begins.
REPLACE_LEAD_S = 1200


def lab_tool_checked() -> None:
    """Exit 2, before anything is touched, when the lab tool lacks the
    state lock or the watchdog check that every change command and its
    record go through (C CT3)."""
    missing = [name for name in LAB_TOOL_CALLS if not hasattr(lab_tool(), name)]
    if missing:
        print(f"scripts/linode-small-mesh-lab.py has no {', '.join(missing)}: the deployed "
              "record and the watchdog check need the lab tool of C CT3", file=sys.stderr)
        sys.exit(mesh_ops.EXIT_REFUSED)


def lab_argv(verb: str, *rest: str) -> list[str]:
    """The lab tool's arguments for `verb` on this lab: the deploy's lab
    state (common.state_path()) given as the lab tool's top-level `--state`,
    so both tools act on one lab whatever the environment of the child."""
    return ["--state", str(common.state_path()), verb, *rest]


def run_lab(*arguments: str) -> subprocess.CompletedProcess:
    """The lab tool's command line, for what bills (replace, add, remove):
    its own watchdog and budget checks apply. Its output goes to stderr
    (a change command's stdout is its marks).

    The lab tool runs a sequence of billing API calls (a create and then
    its state write, a rebuild and then its config and boot), so no signal
    of this command may cut it: it is spawned in a session of its own with
    no signal blocked (signals are held in this process across the spawn,
    so an interrupt cannot land between the fork and its pid), and an
    interrupt of this command (the engine's Interrupted, or Ctrl-C) waits
    for it to end, by its own timeouts, and is raised after its result is
    read."""
    command = [sys.executable, str(common.ROOT / "scripts" / "linode-small-mesh-lab.py"),
               *lab_argv(*arguments)]
    read_end, write_end = os.pipe()
    try:
        with mesh_ops.signals_held():
            pid = os.posix_spawn(command[0], command, os.environ, setsid=True, setsigmask=(),
                                 setsigdef=(signal.SIGINT, signal.SIGTERM, signal.SIGHUP,
                                            signal.SIGPIPE),
                                 file_actions=[(os.POSIX_SPAWN_DUP2, write_end, 1),
                                               (os.POSIX_SPAWN_DUP2, write_end, 2),
                                               (os.POSIX_SPAWN_CLOSE, read_end)])
    finally:
        os.close(write_end)
    chunks: list[bytes] = []
    interrupted: BaseException | None = None
    status = None
    with os.fdopen(read_end, "rb") as output:
        while status is None:
            try:
                chunk = output.read1(65536)
                if chunk:
                    chunks.append(chunk)
                    continue
                status = os.waitpid(pid, 0)[1]
            except (mesh_ops.Interrupted, KeyboardInterrupt) as caught:
                if interrupted is None:
                    print(f"interrupted ({caught!r}): the lab tool's {arguments[0]} runs to its "
                          "end first (it is never cut part-way through a create or a rebuild)",
                          file=sys.stderr, flush=True)
                interrupted = caught
    text = b"".join(chunks).decode(errors="replace")
    sys.stderr.write(text)
    result = subprocess.CompletedProcess(command, os.waitstatus_to_exitcode(status), text)
    if interrupted is not None:
        raise interrupted
    return result


def tier_builds(nodes: dict, roles: dict) -> dict:
    """The build of each tier: the one most of its nodes run by the record
    (a tie goes to the newer). A roll stopped part-way leaves the tier on
    the build most of it runs; a node that joins later gets that one."""
    counts: dict[str, dict[str, int]] = {}
    for name, entry in nodes.items():
        tier = mesh_ops.ROLE_TIER.get(roles.get(name))
        if tier and entry.get("build"):
            builds = counts.setdefault(tier, {})
            builds[entry["build"]] = builds.get(entry["build"], 0) + 1
    return {tier: max(builds, key=lambda key: (builds[key], key_build(key)["unix"]))
            for tier, builds in sorted(counts.items())}


# What the record keeps of a node besides its build, as the node reported
# it after its activation.
RECORD_FIELDS = ("args_sha256", "render_sha256", "unit_sha256")


def record_deployed(full: dict, entries: dict, settings: dict | None = None,
                    standby: list[str] | None = None, files: bool = False) -> None:
    """state["deployed"] (C 2.6), under the lab state's lock. `entries`:
    per node `{"build", "args_sha256", "render_sha256", "unit_sha256"}` as
    the node reported them after its activation, with "set" (the `--set`
    values it runs beyond the deployed settings), "prev" (what its
    `.args.prev` holds) and "activating" (an activation begun and not yet
    seen to end) where they apply; None drops a node (held out). `settings`:
    the settings in force (a push, a fold, an adoption), `standby` the edges
    held out, `files`: the routes and hunted ports rendered with. Only nodes
    of the lab are kept, and the tiers' builds follow what they run
    (tier_builds)."""
    routes = sha256_file(common.ARTIFACT_DIR / "routes.json")
    ecmp = sha256_file(HUNT_FILE)

    def change(current: dict) -> None:
        deployed = current.setdefault("deployed", {})
        if settings is not None:
            deployed["settings"] = dict(settings)
        if standby is not None:
            deployed.setdefault("settings", {})["standby_edges"] = list(standby)
        if files:
            deployed.update(routes_sha256=routes, ecmp_sha256=ecmp)
        created = current.get("nodes") or {}
        nodes = {**(deployed.get("nodes") or {}), **entries}
        deployed["nodes"] = {name: entry for name, entry in nodes.items()
                             if entry is not None and name in created}
        deployed["tiers"] = tier_builds(deployed["nodes"],
                                        {name: created[name]["role"] for name in deployed["nodes"]})

    lab_tool().change_state(common.state_path(), full, change)


def setting(settings: dict, key: str):
    """A setting's value, a key that `settings` lacks taken at its default."""
    return settings.get(key, DEFAULT_SETTINGS.get(key))


def bare(entry: dict) -> dict:
    """A record entry without what it keeps beside the node's own state."""
    return {key: value for key, value in entry.items() if key not in ("prev", "activating")}


def rebased(entry: dict, old: dict, new: dict) -> dict:
    """A record entry whose "set" is against the deployed settings `old`,
    against `new`: every value the node runs stays as it is (its "prev" and
    "activating" too)."""
    own = entry.get("set") or {}
    effective = {**old, **own}
    keys = set(own) | {key for key in set(old) | set(new)
                       if key not in ("standby_edges", "settings_version")
                       and setting(old, key) != setting(new, key)}
    delta = {key: setting(effective, key) for key in sorted(keys)
             if setting(effective, key) != setting(new, key)}
    out = {key: value for key, value in entry.items() if key != "set"}
    if delta:
        out["set"] = delta
    for inner in ("prev", "activating"):
        if isinstance(entry.get(inner), dict):
            out[inner] = rebased(entry[inner], old, new)
    return out


def binary_build(binary: str | None) -> str | None:
    """The build key that line 1 of an `.args` names
    (/opt/needletail/builds/<key>/<binary>); None for any other path (the
    layout before builds: /usr/local/bin/av-mesh)."""
    match = re.fullmatch(re.escape(NODE_BUILDS) + r"/([0-9A-Za-z._-]+-[0-9]+)/[^/]+", binary or "")
    return match.group(1) if match else None


def settings_differ(recorded: dict, current: dict) -> dict:
    """{key: (deployed, here)} for every setting that differs, a key one of
    them lacks taken at its default. `standby_edges` is which edges serve,
    not how they run, and is left out."""
    keys = (set(recorded) | set(current)) - {"standby_edges", "settings_version"}
    out = {}
    for key in sorted(keys):
        old = recorded.get(key, DEFAULT_SETTINGS.get(key))
        new = current.get(key, DEFAULT_SETTINGS.get(key))
        if old != new:
            out[key] = (old, new)
    return out


def lab_files_checked(full: dict, remeasure: bool = False, hunting: bool = False) -> None:
    """routes.json and ecmp-ports.json must be this lab's (lab_file_refused),
    unless the command writes them again (`--remeasure`, `hunt`)."""
    routes = common.ARTIFACT_DIR / "routes.json"
    if routes.is_file() and not remeasure:
        lab_file_refused(routes, json.loads(routes.read_text()).get("lab_run_id"), full["run_id"])
    if HUNT_FILE.is_file() and not hunting:
        lab_file_refused(HUNT_FILE, load_hunted_ports().get("lab_run_id"), full["run_id"])


@contextlib.contextmanager
def ops_locked():
    """The ops lock (C 2.6: `<lab state>.ops.lock`) for a command that does
    not run through the engine (push, restart --roles, hunt, change); a
    command started by the holder proceeds with its token
    (NEEDLETAIL_OPS_LOCK_TOKEN). Refused: exit 2."""
    path = common.state_path()
    lock = mesh_ops.OpsLock(path.with_name(path.name + ".ops.lock"))
    try:
        lock.acquire()
    except mesh_ops.Refused as refused:
        print(refused, file=sys.stderr)
        sys.exit(mesh_ops.EXIT_REFUSED)
    previous = os.environ.get(lock.ENV)
    os.environ[lock.ENV] = lock.token
    try:
        yield lock
    finally:
        if previous is None:
            os.environ.pop(lock.ENV, None)
        else:
            os.environ[lock.ENV] = previous
        lock.release()


class LinodeOps(mesh_ops.Ops):
    """The change engine's backend on a Linode lab (mesh_ops.Ops; C 2.13).
    `roll`, `restart --nodes` and `replace` run through mesh_ops on it;
    `push`, `restart --roles` and `start --nodes` use its staging,
    activation and record directly, so every install goes one way.

    `settings` are the worktree's (this lab's settings.json), `given` this
    command's `--set`; every node is rendered with all created nodes in
    service (a standby edge or a slot gets the arguments it runs once taken
    in; no other node's change, D3), on a build of its own: its binary
    from the build cache, line 1 of its arguments that build's path on the
    node (installed_args), and with settings of its own: the worktree's,
    its record's "set" and this command's `--set` (node_settings). A `--set`
    goes into the deployed settings and settings.json only once every
    running node runs it (fold); until then each node's record says what
    it runs, so a command stopped part-way can be run again or resumed."""

    # C 3.5: the ssh control connection lapses after 900 s idle
    # (ControlPersist); a node in hand gets a fresh one before its gate.
    SSH_REFRESH_S = 600.0

    def __init__(self, full: dict, settings: dict, given: dict | None = None,
                 adopt: bool = False, http: mesh_ops.Http | None = None):
        self.full = full
        self.base = dict(settings)
        self.given = dict(given or {})
        self.settings = {**self.base, **self.given}
        self.adopt = adopt
        self.adopting = False
        self.http = http or mesh_ops.Http()
        path = common.state_path()
        self.lock_path = path.with_name(path.name + ".ops.lock")
        self.evidence_dir = common.ARTIFACT_DIR / "rolls"
        self.staged: dict[str, dict] = {}
        self._plans: dict[str, dict] = {}
        self._renders: dict[tuple, dict] = {}
        self._ssh_at: dict[str, float] = {}

    # ---------------------------------------------------------- the lab
    def deployed(self) -> dict:
        return self.full.get("deployed") or {}

    def entry(self, name: str) -> dict:
        """Node `name`'s entry of the deployed record (record_deployed)."""
        return dict((self.deployed().get("nodes") or {}).get(name) or {})

    def held_out(self, name: str) -> bool:
        return name in standby_edges(self.settings)

    def node_settings(self, name: str, given: dict | None = None) -> dict:
        """The settings node `name` runs by the record (the worktree's with
        its own "set"; a node held out takes the worktree's when started),
        with `given` on top."""
        own = {} if self.held_out(name) else (self.entry(name).get("set") or {})
        return {**self.base, **own, **(given or {})}

    def set_of(self, settings: dict) -> dict:
        """The values of a node's settings that are not the worktree's."""
        return {key: value for key, value in sorted(settings.items())
                if value != setting(self.base, key)}

    def role(self, name: str) -> str | None:
        node = self.full["nodes"].get(name) or lab_slots(self.full).get(name)
        return node["role"] if node else None

    def service(self, name: str) -> str:
        return "needletail-contrib" if self.role(name) == "contributor" else "needletail-mesh"

    def address(self, name: str) -> tuple[str, int] | None:
        node = self.full["nodes"].get(name)
        if not node or not node.get("public_ipv4"):
            return None
        return node["public_ipv4"], CONTRIB_HTTP_PORT if node["role"] == "contributor" else HTTP_PORT

    def reload(self) -> None:
        """The lab state again from disk (an instance replaced or added)."""
        fresh = common.load_state()
        self.full.clear()
        self.full.update(fresh)
        self._plans.clear()
        self._renders.clear()

    def nodes(self) -> list[dict]:
        standby = set(standby_edges(self.settings))
        slots = lab_slots(self.full)
        created = self.full["nodes"]
        tree = relay_tree(active_state(self.full, self.settings), self.settings)
        parents = {child["node"]["name"]: (child["primary"]["name"], child["secondary"]["name"])
                   for child in tree["children"]}
        records = []
        for name in sorted({*created, *slots}, key=mesh_ops.natural):
            node = created.get(name) or slots[name]
            if node["role"] not in mesh_ops.ROLE_TIER:
                continue
            exists = name in created
            primary, secondary = parents.get(name, (None, None))
            records.append({
                "name": name, "role": node["role"], "site": node["site"],
                "vlan": node.get("vlan_ipv4"), "public": node.get("public_ipv4") if exists else None,
                "serving": exists and name not in standby, "slot": name in slots,
                "standby": exists and name in standby, "created": exists,
                "primary": primary, "secondary": secondary})
        return records

    def status(self, node: str):
        """The engine asks this of the node in hand (its gate, drain and
        readiness), and of no other: the ssh control connection to a node
        that answers is opened, or refreshed, here (fresh_ssh)."""
        target = self.address(node)
        if target is None:
            return None
        code, document = self.http.get(*target, "/api/node")
        if code != 200:
            return None
        self.fresh_ssh(node)
        return document

    def fresh_ssh(self, node: str) -> None:
        """An ssh to the node unless this command reached it within
        SSH_REFRESH_S, so that its activation, 1.5 s after a gate, runs on
        an open control connection (C 3.5). Best effort: the activation
        opens one itself when this fails."""
        now = time.monotonic()
        if now - self._ssh_at.get(node, -self.SSH_REFRESH_S) < self.SSH_REFRESH_S:
            return
        self._ssh_at[node] = now
        try:
            common.ssh_text(self.full["nodes"][node], "true", check=False, timeout=10)
        except (KeyError, OSError, RuntimeError, subprocess.SubprocessError):
            pass

    def statuses(self, nodes) -> dict:
        targets = {name: (*self.address(name), "/api/node") for name in nodes if self.address(name)}
        answers = self.http.poll(targets)
        return {name: (answers[name][1] if name in answers and answers[name][0] == 200 else None)
                for name in nodes}

    def get(self, node: str, path: str) -> tuple:
        target = self.address(node)
        return (None, None) if target is None else self.http.get(*target, path)

    def post(self, node: str, path: str, body: dict) -> int | None:
        target = self.address(node)
        return None if target is None else self.http.post(*target, path, body)[0]

    # ---------------------------------------------------------- builds and the render
    def recorded(self, name: str) -> str | None:
        """The build key node `name` runs by the record, else (a node not
        recorded, or held out: it is started on it) its tier's."""
        deployed = self.deployed()
        tier = mesh_ops.ROLE_TIER.get(self.role(name))
        own = None if self.held_out(name) else self.entry(name).get("build")
        return own or (deployed.get("tiers") or {}).get(tier)

    def ensure_cached(self, key: str | None, binary: str) -> None:
        if key is None or cached(key, binary):
            return
        holders = [name for name, entry in (self.deployed().get("nodes") or {}).items()
                   if entry.get("build") == key and name in self.full["nodes"]]
        for name in holders:
            try:
                fetch_build(self.full["nodes"][name], key, binary)
                return
            except Exception as error:  # noqa: BLE001 - the next holder may answer
                print(f"build {key} from {name}: {error}", file=sys.stderr)
        raise mesh_ops.Refused(f"build {key} is neither in {builds_dir()} nor on a node that runs it")

    def plan(self, settings: dict) -> dict:
        key = json.dumps(settings, sort_keys=True, default=str)
        if key not in self._plans:
            path = common.ARTIFACT_DIR / "routes.json"
            if not path.is_file():
                raise mesh_ops.Refused("no routes.json: push the whole lab first")
            program = build_program(self.in_service(settings), settings,
                                    json.loads(path.read_text()))
            self._plans[key] = compile_plan(program)
        return self._plans[key]

    def in_service(self, settings: dict) -> dict:
        return active_state(self.full, {**settings, "standby_edges": []})

    def rendered(self, settings: dict, mesh_key: str | None, contrib_key: str | None) -> dict:
        cache = (json.dumps(settings, sort_keys=True, default=str), mesh_key, contrib_key)
        if cache not in self._renders:
            self.ensure_cached(mesh_key, "av-mesh")
            self.ensure_cached(contrib_key, "av-contrib")
            RENDER_BINARIES.clear()
            RENDER_BINARIES.update({
                "av-mesh": builds_dir() / mesh_key / "av-mesh" if mesh_key else None,
                "av-contrib": builds_dir() / contrib_key / "av-contrib" if contrib_key else None})
            self._renders[cache] = render(self.in_service(settings), settings, self.plan(settings))
        return self._renders[cache]

    def node_args(self, name: str, settings: dict, key: str) -> tuple[list[str], str, str]:
        """(arguments, unit, service) of node `name` on build `key`. The
        render of a service looks at the flags of the other kind's binary
        too (wave7_args): that is the build its tier runs."""
        deployed_tiers = self.deployed().get("tiers") or {}
        role = self.role(name)
        if role == "contributor":
            mesh_key, contrib_key = deployed_tiers.get("forwarders"), key
        else:
            mesh_key, contrib_key = key, deployed_tiers.get("contributors")
        self.ensure_cached(key, "av-contrib" if role == "contributor" else "av-mesh")
        item = self.rendered(settings, mesh_key, contrib_key).get(name)
        if item is None:
            raise mesh_ops.Refused(f"{name}: no service is rendered for it")
        args = installed_args(item, role, key)
        unit = unit_text(item["service"], item["rust_log"], node_own_settings(settings, name))
        return args, unit, item["service"]

    # ---------------------------------------------------------- checks (C 2.6)
    def check(self, command: str, nodes: list[str]) -> None:
        """The watchdog; the `--set` rules; the reproduce check: the
        worktree's settings, routes and hunted ports are the deployed ones,
        and each node of the command that answers runs exactly what its
        recorded build renders with them. `--adopt-settings` goes on and
        says what it adopts."""
        problems = lab_tool().watchdog_ensure(self.full, common.state_path())
        if problems:
            raise mesh_ops.Refused("the lab's watchdog: " + "; ".join(problems))
        self.settle()
        deployed = self.deployed()
        if not deployed.get("nodes"):
            raise mesh_ops.Refused("the lab state records nothing deployed: push the lab first")
        outside = sorted(set(self.given) - set(ROLLABLE_SETTINGS))
        if outside:
            raise mesh_ops.Refused(
                f"--set {', '.join(outside)}: not a setting that {command} may change "
                "(ROLLABLE_SETTINGS); a setting that two peers must agree on needs push")
        differ = settings_differ(deployed.get("settings") or {}, self.base)
        files = [label for label, path, field in (
            ("routes.json", common.ARTIFACT_DIR / "routes.json", "routes_sha256"),
            ("ecmp-ports.json", HUNT_FILE, "ecmp_sha256")) if sha256_file(path) != deployed.get(field)]
        statuses = self.statuses(nodes)
        mismatched = []
        for name in nodes:
            status = mesh_ops.status_of(statuses.get(name))
            if status is None:
                continue  # not running (a node to start): nothing to compare
            entry = (deployed["nodes"]).get(name)
            if entry is None:
                mismatched.append(f"{name} (not in the record)")
                continue
            if entry.get("activating"):
                mismatched.append(f"{name} (an activation of it did not end, and it runs neither "
                                  "the arguments before it nor those it put in force)")
                continue
            if not entry.get("build"):
                mismatched.append(f"{name} (runs no build of {NODE_BUILDS})")
                continue
            args, unit, _service = self.node_args(name, self.node_settings(name), entry["build"])
            if status.args_sha256 != entry.get("args_sha256"):
                mismatched.append(f"{name} (runs other arguments than the record)")
            elif (render_sha256(args), sha256_text(unit)) != \
                    (entry.get("render_sha256"), entry.get("unit_sha256")):
                mismatched.append(name)
        if differ or files or mismatched:
            text = "; ".join(
                ([("settings differ from the deployed record: " + ", ".join(
                    f"{key}={json.dumps(new)} (deployed {json.dumps(old)})"
                    for key, (old, new) in differ.items()))] if differ else [])
                + ([f"{' and '.join(files)} not the deployed one"] if files else [])
                + ([f"running other arguments than they render: {', '.join(mismatched)}"]
                   if mismatched else []))
            if not self.adopt:
                raise mesh_ops.Refused(f"these settings do not reproduce what is deployed: {text} "
                                       "(--adopt-settings adopts them)")
            print(f"--adopt-settings: adopting what this worktree renders: {text}", file=sys.stderr)
            # The deployed settings become the worktree's at this command's
            # first activation (record).
            self.adopting = bool(differ or files)
        if self.given:
            beyond = [name for name in self.changed_by_set() if name not in nodes]
            if beyond:
                raise mesh_ops.Refused(f"--set {', '.join(sorted(self.given))} changes "
                                       f"{', '.join(beyond)}, outside the nodes of this {command}")
            self.fold()
        if command == "replace":
            self.check_replace(nodes[0])

    def changed_by_set(self) -> list[str]:
        """The nodes that run (recorded, not held out) whose arguments or
        unit `--set` changes, each on its recorded build with its recorded
        settings. A node held out takes the deployed settings when it is
        started."""
        out = []
        for name in sorted(self.deployed().get("nodes") or {}, key=mesh_ops.natural):
            key = self.recorded(name)
            if name not in self.full["nodes"] or self.held_out(name) or key is None:
                continue
            try:
                before = self.node_args(name, self.node_settings(name), key)
            except mesh_ops.Refused:
                continue  # no service (a backbone the topology does not use)
            after = self.node_args(name, self.node_settings(name, self.given), key)
            if (render_sha256(before[0]), before[1]) != (render_sha256(after[0]), after[1]):
                out.append(name)
        return out

    def settle(self) -> None:
        """Activations begun and not seen to end (the tool killed or its ssh
        dropped during one, a push that failed part-way), each settled by
        what the node runs: the arguments it was to put in force (it ran:
        recorded as such) or those it had (it did not). A node that does
        not answer stays as it is, and one that runs neither is named by
        the reproduce check."""
        nodes = self.deployed().get("nodes") or {}
        pending = [name for name, entry in nodes.items()
                   if entry.get("activating") and name in self.full["nodes"]]
        if not pending:
            return
        statuses = self.statuses(pending)
        settled = {}
        for name in pending:
            status = mesh_ops.status_of(statuses.get(name))
            entry = nodes[name]
            if status is None:
                continue
            if status.args_sha256 == entry["activating"].get("args_sha256"):
                settled[name] = entry["activating"]
                print(f"{name}: an activation that did not end had run; recorded",
                      file=sys.stderr)
            elif status.args_sha256 == entry.get("args_sha256"):
                settled[name] = {key: value for key, value in entry.items() if key != "activating"}
                print(f"{name}: an activation that did not end had not run", file=sys.stderr)
        if settled:
            record_deployed(self.full, settled)

    def fold(self) -> None:
        """Once every running node runs this command's `--set`, it becomes
        the deployed settings, and the keys it sets are written into this
        worktree's settings.json as that file is now (another command may
        have changed it meanwhile): the next command reproduces the lab
        from them. The record's entries keep what each node runs."""
        new = {**self.base, **self.given}
        if not settings_differ(self.base, new) or self.changed_by_set():
            return
        entries = {}
        for name, entry in (self.deployed().get("nodes") or {}).items():
            # Each node renders the same with the new value as with its own
            # (none is changed by the --set), and one held out takes the
            # deployed settings when it starts: its own values of these
            # keys go. What it ran before ("prev") keeps its values.
            own = {key: value for key, value in (entry.get("set") or {}).items()
                   if key not in self.given and value != setting(new, key)}
            entry = {key: value for key, value in rebased(entry, self.base, new).items()
                     if key != "set"}
            entries[name] = {**entry, "set": own} if own else entry
        record_deployed(self.full, entries, settings=new)
        document = json.loads(SETTINGS_FILE.read_text()) if SETTINGS_FILE.is_file() else {}
        document.update(self.given)
        document["lab_run_id"] = self.full["run_id"]
        SETTINGS_FILE.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n")
        self.base = new
        print(f"--set {', '.join(sorted(self.given))}: every node runs it; it is now the "
              f"deployed settings and {SETTINGS_FILE}'s", file=sys.stderr)

    def target(self, tier: str) -> dict:
        build = cache_build(TIER_ROLE[tier])
        return {"git": build["git"], "unix": build["unix"], "dirty": build["dirty"]}

    # ---------------------------------------------------------- staging and activation
    def stage(self, nodes: list[str], build: dict | None) -> None:
        plans = {}
        for name in nodes:
            key = f"{build['git']}-{build['unix']}" if build else self.recorded(name)
            if key is None:
                raise mesh_ops.Refused(f"{name} has no recorded build (push first)")
            settings = self.node_settings(name, self.given)
            args, unit, service = self.node_args(name, settings, key)
            args = self.kept_stamp(name, key, args, unit)
            plans[name] = (service, key, node_files(args, unit, node_own_settings(settings, name)))
            self.staged[name] = self.intended(key, args, unit, settings)
        common.parallel([self.full["nodes"][name] for name in nodes],
                        lambda node: stage_node(node, *plans[node["name"]]))
        now = time.monotonic()
        self._ssh_at.update({name: now for name in nodes})

    def intended(self, key: str, args: list[str], unit: str, settings: dict) -> dict:
        """The record entry of a node once `args` (rendered with `settings`
        on build `key`) and `unit` are in force."""
        entry = {"build": key, "args_sha256": args_sha256(args),
                 "render_sha256": render_sha256(args), "unit_sha256": sha256_text(unit)}
        own = self.set_of(settings)
        return {**entry, "set": own} if own else entry

    def kept_stamp(self, name: str, key: str, args: list[str], unit: str) -> list[str]:
        """The arguments the node runs, when they are today's render but for
        the time of their render (a contributor's path observation,
        RENDER_TIME_FLAG): staged again as they are, so that the node is on
        the target and skipped, and a forced restart keeps what it ran."""
        old = self.entry(name)
        if old.get("args_sha256") in (None, args_sha256(args)) or \
                (old.get("build"), old.get("render_sha256"), old.get("unit_sha256")) != \
                (key, render_sha256(args), sha256_text(unit)):
            return args
        running = common.ssh_text(self.full["nodes"][name],
                                  f"cat {NODE_ETC}/{self.service(name)}.args",
                                  check=False).splitlines()
        if running and args_sha256(running) == old["args_sha256"] and \
                render_sha256(running) == render_sha256(args):
            return running
        return args

    def expected(self, node: str, rollback: bool = False) -> dict:
        if rollback:
            text = common.ssh_text(self.full["nodes"][node],
                                   f"cat {NODE_ETC}/{self.service(node)}.args.prev", check=False)
            args = text.splitlines()
            if not args:
                raise mesh_ops.Refused(f"{node} has no previous arguments to roll back to")
            key = binary_build(args[0])
            if key is None:
                raise mesh_ops.Refused(f"{node}: its previous arguments run {args[0]}, not a "
                                       f"build of {NODE_BUILDS}; they cannot be put back")
            build = key_build(key)
            return {"git": build["git"], "unix": build["unix"], "args_sha256": args_sha256(args)}
        staged = self.staged.get(node)
        if staged is None:
            raise mesh_ops.Refused(f"{node}: nothing is staged")
        build = key_build(staged["build"])
        return {"git": build["git"], "unix": build["unix"], "args_sha256": staged["args_sha256"]}

    def activation_of(self, node: str, old: dict, rollback: bool) -> dict | None:
        """The entry node `node` has once its activation has run: what is
        staged, or with `rollback` what its `.args.prev` holds by the record
        (None when the record does not know). Either keeps what it replaces
        as its "prev"."""
        if rollback:
            prev = old.get("prev")
            return {**prev, "prev": prev} if prev else None
        staged = self.staged.get(node)
        if staged is None:
            return None
        return {**staged, "prev": bare(old)} if old.get("build") else dict(staged)

    @staticmethod
    def settled(old: dict, intended: dict | None, result: dict) -> dict | None:
        """The record entry after an activation, by what the node then has
        in force (`result`, read on the node after its script): what was
        intended, what was there before (nothing was put in force), or,
        when it is neither, what the node reports. None: nothing to
        record."""
        read = {field: result.get(field) for field in RECORD_FIELDS}
        if read["args_sha256"] is None:
            return None
        if intended is not None and read["args_sha256"] == intended["args_sha256"]:
            return {**intended, **read}
        if read["args_sha256"] == old.get("args_sha256"):
            return {key: value for key, value in old.items() if key != "activating"}
        return {"build": binary_build(result.get("binary")), **read}

    def activate(self, node: str, not_after_ms: int, rollback: bool = False) -> dict:
        """The activation, recorded as begun ("activating") before it runs:
        if this command dies or its ssh drops during it, the next command
        settles the record by what the node runs (settle)."""
        old = self.entry(node)
        intended = self.activation_of(node, old, rollback)
        if intended is not None:
            record_deployed(self.full, {node: {**old, "activating": intended}})
        result = activate_node(self.full["nodes"][node], self.service(node), not_after_ms, rollback)
        self._ssh_at[node] = time.monotonic()
        entry = self.settled(old, intended, result)
        if entry is not None:
            self.record({node: entry})
        qdisc_checked(node, result.get("qdisc"), self.node_settings(node))
        return {"ok": result["ok"], "why": result["why"]}

    def record(self, entries: dict) -> None:
        """The record of nodes this command activated; with
        `--adopt-settings`, the worktree's settings as the deployed ones;
        then the fold of this command's `--set`."""
        if self.adopting:
            record_deployed(self.full, entries, settings=self.base, files=True)
            self.adopting = False
        else:
            record_deployed(self.full, entries)
        if self.given:
            self.fold()

    def install(self, names: list[str] | None, builds: dict | None, binaries: bool = True) -> None:
        """push and restart --roles (C 2.6): by role, parents first, every
        node of a role in parallel: a node in service staged on its tier's
        build (`builds`, else the recorded one) and activated, a standby
        edge staged and left stopped, a load host's tools and kernel
        settings; a backbone the topology does not use is stopped. Every
        node to activate is recorded as such first, and each node that
        activated is recorded even when another of its role failed; the
        settings, routes and hunted ports with them."""
        standby = set(standby_edges(self.settings))
        for role in ("backbone", "distributor", "edge", "contributor", "load"):
            group = [node for node in common.nodes_by_role(self.full, role)
                     if names is None or node["name"] in names]
            if not group:
                continue
            # Rendered here, not in the workers: RENDER_BINARIES is one
            # module global.
            plans: dict[str, tuple | None] = {}
            for node in group:
                name = node["name"]
                if role == "load":
                    plans[name] = None
                    continue
                build = (builds or {}).get(mesh_ops.ROLE_TIER[role])
                key = f"{build['git']}-{build['unix']}" if build else self.recorded(name)
                if key is None:
                    raise mesh_ops.Refused(f"{name} has no recorded build (push it with its "
                                           "binaries)")
                try:
                    args, unit, service = self.node_args(name, self.settings, key)
                except mesh_ops.Refused:
                    if role != "backbone":
                        raise
                    plans[name] = None
                    continue
                plans[name] = (service, key, node_files(
                    args, unit, node_own_settings(self.settings, name)))
                self.staged[name] = self.intended(key, args, unit, self.settings)
            olds = {name: self.entry(name) for name in plans}
            intended = {name: self.activation_of(name, olds[name], False) for name in plans
                        if plans[name] is not None and name not in standby}
            if intended:
                record_deployed(self.full, {name: {**olds[name], "activating": entry}
                                            for name, entry in intended.items()})
            entries: dict[str, dict] = {}

            def one(node: dict) -> str:
                name = node["name"]
                if role == "load":
                    install_tools(node, self.settings, binaries)
                    return "tools"
                if plans[name] is None:
                    stop_service(node)
                    return "stopped (not in the topology)"
                stage_node(node, *plans[name])
                if name in standby:
                    stop_service(node)
                    return "staged, held out"
                result = activate_node(node, plans[name][0], int((time.time() + 120) * 1000))
                entry = self.settled(olds[name], intended[name], result)
                if entry is not None:
                    entries[name] = entry
                if not result["ok"]:
                    raise RuntimeError(f"activation: {result['why']}")
                qdisc_checked(name, result.get("qdisc"), self.settings)
                return "activated"

            try:
                results = common.parallel(group, one)
            finally:
                if intended:
                    record_deployed(self.full, entries, settings=self.settings, files=True)
            print(f"installed {len(group)} {role} node(s): "
                  + ", ".join(f"{name} {what}" for name, what in sorted(results.items())),
                  file=sys.stderr)

    # ---------------------------------------------------------- the node's service manager
    def boot_changed(self, node: str, since_ms: int) -> bool | None:
        """The service's start time to the microsecond (systemd 248 and
        later; whole seconds would read a restart in the activation's own
        second as none) against `since_ms`."""
        service = self.service(node)
        try:
            text = common.ssh_text(self.full["nodes"][node],
                                   f"t=$(systemctl show {service}.service -p ExecMainStartTimestamp "
                                   f'--timestamp=us+utc --value); [ -n "$t" ] && date -d "$t" +%s%6N',
                                   check=False, timeout=30)
            return int(text.strip()) > since_ms * 1000
        except (ValueError, KeyError, OSError, RuntimeError, subprocess.SubprocessError):
            return None

    def unit_restarts(self, node: str) -> int | None:
        try:
            text = common.ssh_text(self.full["nodes"][node],
                                   f"systemctl show {self.service(node)}.service -p NRestarts "
                                   "--value", timeout=30)
            return int(text.strip())
        except (ValueError, KeyError, OSError, RuntimeError, subprocess.SubprocessError):
            return None

    def clock_offset(self, node: str) -> float | None:
        """The node's clock minus this host's, from the quickest of three
        reads over the kept ssh connection (the midpoint of each)."""
        best = None
        for _ in range(3):
            began = time.time()
            try:
                text = common.ssh_text(self.full["nodes"][node], "date +%s.%N", timeout=30)
                remote = float(text.strip())
            except (ValueError, OSError, RuntimeError, subprocess.SubprocessError):
                continue
            ended = time.time()
            if best is None or ended - began < best[0]:
                best = (ended - began, remote - (began + ended) / 2)
        return None if best is None else best[1]

    def surge_candidates(self, site: str) -> list[str]:
        standby = set(standby_edges(self.settings))
        slots = lab_slots(self.full)
        return sorted((name for name, node in self.full["nodes"].items()
                       if node["site"] == site and node["role"] == "edge" and name in standby),
                      key=lambda name: (name in slots, mesh_ops.natural(name)))

    def hold(self, name: str, out: bool) -> None:
        """Hold a node out of service, or take it in (standby_edges): this
        worktree's settings.json, this command's settings and the record (a
        node held out is no longer recorded: it runs nothing)."""
        standby = held_out_after(self.settings, stop=[name]) if out else \
            held_out_after(self.settings, start=[name])
        for settings in (self.base, self.settings):
            settings["standby_edges"] = standby
        document = json.loads(SETTINGS_FILE.read_text()) if SETTINGS_FILE.is_file() else {}
        document.update(standby_edges=standby, lab_run_id=self.full["run_id"])
        SETTINGS_FILE.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n")
        record_deployed(self.full, {name: None} if out else {}, standby=standby)

    def start_node(self, node: str) -> None:
        record = self.full["nodes"].get(node)
        if record is None:
            raise mesh_ops.Refused(f"{node} is not created")
        if node in self.staged:
            result = self.activate(node, int((time.time() + 60) * 1000))
            if not result["ok"]:
                if result["why"] == "systemctl restart failed" or result["why"].startswith("no "):
                    raise RuntimeError(f"{node}: {result['why']}")
                raise mesh_ops.Refused(f"{node}: {result['why']}")
        else:
            common.ssh_text(record, f"systemctl enable --now {self.service(node)}.service")
        self.hold(node, out=False)

    def stop_node(self, node: str, not_after_ms: int | None = None) -> None:
        record = self.full["nodes"][node]
        service = self.service(node)
        if not_after_ms is not None:
            text = common.ssh_text(record, f'if [ "$(date +%s%3N)" -gt {int(not_after_ms)} ]; then '
                                           f"echo late; else systemctl stop {service}.service "
                                           "&& echo stopped; fi")
            if text.strip() != "stopped":
                raise mesh_ops.Refused(f"{node}: the stop came after its deadline ({text.strip()})")
            return
        if record["role"] == "edge" or node in lab_slots(self.full):
            stop_service(record, service)
            self.hold(node, out=True)
        else:
            common.ssh_text(record, f"systemctl stop {service}.service")

    # ---------------------------------------------------------- instances (replace)
    # The replace this command makes (command_replace): {"mode", "no_slot",
    # "resume", "budget_usd", "spent_before_usd"}; None for every other
    # command.
    replacing: dict | None = None

    @property
    def budget_args(self) -> tuple[str, ...]:
        """`--budget-usd B [--spent-before-usd S]` of the replace, passed to
        the lab tool's replace and add, which hold every create to them."""
        if self.replacing is None:
            return ()
        spent = self.replacing["spent_before_usd"]
        return ("--budget-usd", str(self.replacing["budget_usd"])) + (
            ("--spent-before-usd", str(spent)) if spent is not None else ())

    def check_replace(self, name: str) -> None:
        """What `replace` refuses before its first mark, under the ops lock
        and after the reproduce check (C 3.7: "the watchdog and the budget
        checked first"): a new instance whose public address other nodes'
        arguments name, and anything the lab tool would refuse once X was
        emptied and stopped (lab_refusals). A resume after the lab tool's
        replace was done needs nothing more of it (the slot's `remove`
        refuses nothing that matters), so a node left stopped near the
        deadline can still be pushed back."""
        if self.replacing["mode"] == "new":
            try:
                dependents = self.address_dependents(name)
            except mesh_ops.Refused:
                raise
            except Exception as error:  # noqa: BLE001 - a render or a fetch: refused
                raise mesh_ops.Refused(f"the nodes that name {name}'s public address could not "
                                       f"be rendered: {error}") from None
            if dependents:
                raise mesh_ops.Refused(
                    f"replace --mode new: the arguments of {', '.join(dependents)} name {name}'s "
                    f"public address, which a new instance changes; they and it cannot agree "
                    f"while {name} is replaced, so no gate or readiness could pass between them. "
                    "--mode rebuild keeps both addresses. Two forwarders that `lab hosts` found "
                    "on one physical host need a new instance: in this round that is an outage "
                    f"(the lab tool's `replace {name} --mode new`, then `push` of {name} and "
                    "these nodes)")
        if self.replacing["resume"]:
            journals = [journal for journal in mesh_ops.Journal.unfinished(self.evidence_dir,
                                                                           "replace")
                        if journal.data.get("label") == f"replace.{name}"]
            done = ((journals[0].data.get("context") or {}).get("done_steps") or []) \
                if journals else []
            if "rebuild" in done:
                return  # the lab tool has nothing left to create or rebuild
        problems = self.lab_refusals(name)
        if problems:
            raise mesh_ops.Refused("the lab tool would refuse this replace after "
                                   f"{name} was emptied and stopped: " + "; ".join(problems))

    def replace_slot(self, name: str) -> str | None:
        """The slot a forwarder's replace creates (the engine's choice: the
        first forwarder slot of its region), None when it creates none."""
        if self.replacing["no_slot"] or mesh_ops.ROLE_TIER.get(self.role(name)) != "forwarders":
            return None
        site = (self.full["nodes"].get(name) or {}).get("site")
        slots = sorted((slot for slot, spec in lab_slots(self.full).items()
                        if spec["site"] == site and spec["role"] == "distributor"),
                       key=mesh_ops.natural)
        return slots[0] if slots and slots[0] not in self.full["nodes"] else None

    def lab_refusals(self, name: str) -> list[str]:
        """The lab tool's own refusals of this replace's `replace NAME`
        and, for a forwarder, its slot's `add`, checked now with the lab
        tool's functions (C CT3), since the engine reaches the lab tool only
        after the node was emptied and stopped: the watchdog (armed, its
        unit active, within the lab's life, and its deadline far enough
        ahead for the whole replace: REPLACE_LEAD_S beyond the lab tool's
        ADD_CUTOFF_S); the node's VLAN address as the topology gives it;
        its live instance carrying the run's tag and its label; and, for
        every instance created (`--mode new`, the slot), dedicated plans in
        regions that are up, the node and hourly ceilings that `up`
        recorded, and the budget with the new instances (the node deleted
        now is priced to now, as the lab tool prices it). The API token
        stays inside the lab tool's Api."""
        lab = lab_tool()
        state = self.full
        now = time.time()
        problems = list(lab.watchdog_problems(state, now, lab.MAX_LIFE_HOURS))
        deadline = state.get("watchdog_deadline_unix") or 0
        if not problems and deadline - now < lab.ADD_CUTOFF_S + REPLACE_LEAD_S:
            problems.append(
                f"the watchdog deletes the lab in {(deadline - now) / 60:.1f} min: the lab "
                f"tool's last create or rebuild of a replace comes up to "
                f"{REPLACE_LEAD_S // 60} min after it begins, and it makes none within "
                f"{lab.ADD_CUTOFF_S // 60} min of the deadline")
        if problems:
            return problems
        node = state["nodes"].get(name)
        if node is None:
            return [f"{name} is not created: there is nothing to replace"]
        topology = json.loads(pathlib.Path(state["topology_file"]).read_text())
        known = {item["site"] for item in topology["nodes"]}
        topology = lab.without_sites(topology, [site for site in state.get("sites_left_out", [])
                                                if site in known])
        spec = next((dict(item) for item in topology["nodes"] if item["name"] == name), None)
        if spec is None:
            return [f"{name} is not a node of the lab's topology {state['topology_file']}"]
        if (spec.get("vlan_ipv4") or "").split("/")[0] != (node.get("vlan_ipv4") or ""):
            return [f"the topology gives {name} the VLAN address {spec.get('vlan_ipv4')}, the "
                    f"lab made it at {node.get('vlan_ipv4')}"]
        api = lab.Api()
        try:
            live = lab.owned_instance(api, state, name, "replace")
        except SystemExit as refused:
            return [str(refused)]
        mode = self.replacing["mode"]
        if mode == "rebuild" and not live:
            return [f"{name}: the instance is gone, there is nothing to rebuild"]
        slot = self.replace_slot(name)
        new = ([spec] if mode == "new" else []) + (
            [dict(lab_slots(state)[slot])] if slot else [])
        if not new:
            return []
        types = {item["id"]: item for item in api.list_all("/linode/types")}
        regions = {item["id"]: item for item in api.list_all("/regions")}
        _hourly, problems = lab.topology_cost({"nodes": new}, types, regions)
        if problems:
            return problems
        max_nodes, max_hourly = lab.lab_ceilings(state)
        count = len(state["nodes"]) + (1 if slot else 0)
        if count > max_nodes:
            problems.append(f"the lab would have {count} nodes; the ceiling that up recorded is "
                            f"{max_nodes}")
        hourly = lab.lab_hourly(state) + sum(item["_hourly"] for item in new) - (
            node.get("hourly_usd", 0.0) if mode == "new" else 0.0)
        if hourly > max_hourly + 1e-9:
            problems.append(f"the lab would cost ${hourly:.4f}/hour; the ceiling that up "
                            f"recorded is ${max_hourly:.2f}")
        after = state if mode == "rebuild" else {
            **state, "nodes": {other: item for other, item in state["nodes"].items()
                               if other != name},
            "removed": [*state.get("removed", []), lab.removed_record(state, node, now)]}
        try:
            lab.held_to_budget("replace", api, after,
                               [{"name": item["name"], "hourly_usd": item["_hourly"]}
                                for item in new],
                               self.replacing["budget_usd"], self.replacing["spent_before_usd"])
        except SystemExit as refused:
            problems.append(str(refused))
        return problems

    def rebuild(self, node: str, mode: str) -> None:
        """The lab tool's `replace NODE --mode M` with the command's budget,
        then the host's key forgotten (common.forget_host) at its address
        before and after: a rebuild keeps the address and changes the key, a
        new instance has another address. The state is read again and the
        keys forgotten whatever the lab tool's result, an interrupt
        included, for it may have rebuilt part-way. A `--mode new` whose
        create failed after the delete leaves the node absent from the lab:
        named, with the way back."""
        old = (self.full["nodes"].get(node) or {}).get("public_ipv4")
        try:
            result = run_lab("replace", node, "--mode", mode, *self.budget_args)
        finally:
            self.reload()
            common.forget_host(old, (self.full["nodes"].get(node) or {}).get("public_ipv4"))
        if node not in self.full["nodes"]:
            again = (f"`linode-small-mesh-lab.py add {node}`" if node in lab_slots(self.full)
                     else "`linode-small-mesh-lab.py grow --topology "
                          f"{self.full.get('topology_file')}` (it creates every topology node "
                          "the lab lacks)")
            raise RuntimeError(
                f"the lab tool deleted {node} and did not create it again: {node} is gone. "
                f"Create it with {again} and the budget, then `replace --nodes {node} --mode "
                "rebuild --resume`")
        if result.returncode != 0:
            raise RuntimeError(f"the lab tool's replace of {node} failed")

    def address_dependents(self, node: str) -> list[str]:
        """The running nodes (recorded, not held out) whose arguments name
        `node`'s public address: each rendered on its recorded build with
        that address and with another (TEST-NET-1), the ones that differ.
        What a new instance of `node` (`replace --mode new`) would make
        restart-bound (C 3.7); peers that name it by its VLAN address are
        not among them."""
        other = json.loads(json.dumps(self.full))
        other["nodes"][node]["public_ipv4"] = "192.0.2.1"
        moved = LinodeOps(other, self.base)
        out = []
        for name in sorted(self.deployed().get("nodes") or {}, key=mesh_ops.natural):
            key = self.recorded(name)
            if name == node or name not in self.full["nodes"] or self.held_out(name) \
                    or key is None:
                continue
            settings = self.node_settings(name)
            try:
                here = self.node_args(name, settings, key)[0]
            except mesh_ops.Refused:
                continue  # no service (a backbone the topology does not use)
            if render_sha256(here) != render_sha256(moved.node_args(name, settings, key)[0]):
                out.append(name)
        return out

    def push(self, node: str) -> None:
        """Bootstrap, the node's tier's recorded build, its arguments, start;
        the node taken into service."""
        bootstrap([self.full["nodes"][node]])
        if node in standby_edges(self.settings):
            self.hold(node, out=False)
        self.install([node], None)

    def add_slot(self, node: str) -> None:
        """The lab tool's `add` with the budget, then the slot pushed. Its
        address may have been another instance's (Linode gives released
        addresses out again): forgotten before the push."""
        try:
            failed = run_lab("add", node, *self.budget_args).returncode != 0
        finally:
            self.reload()
        if node not in self.full["nodes"]:
            raise mesh_ops.Refused(f"the lab tool did not create {node}")
        if failed:
            raise RuntimeError(f"the lab tool's add of {node} failed part-way")
        common.forget_host(self.full["nodes"][node]["public_ipv4"])
        self.push(node)

    def remove_slot(self, node: str) -> None:
        """The lab tool's `remove`; the slot's address forgotten (another
        instance may get it)."""
        address = (self.full["nodes"].get(node) or {}).get("public_ipv4")
        try:
            if node in self.full["nodes"] and run_lab("remove", node).returncode != 0:
                raise RuntimeError(f"the lab tool's remove of {node} failed")
        finally:
            self.reload()
        if node not in self.full["nodes"]:
            common.forget_host(address)

    # ---------------------------------------------------------- workstream A's kill and return
    def hold_path(self, node: str) -> str:
        return f"/run/systemd/system/{self.service(node)}.service.d/hold.conf"

    def hold_down(self, node: str) -> None:
        """Its unit is not started again after the kill (A 2.10): a drop-in
        under /run (gone at a reboot) with Restart=no."""
        path = self.hold_path(node)
        directory = str(pathlib.PurePosixPath(path).parent)
        common.ssh_text(self.full["nodes"][node],
                        f"mkdir -p {shlex.quote(directory)} && printf '[Service]\\nRestart=no\\n'"
                        f" > {shlex.quote(path)} && systemctl daemon-reload", timeout=30)

    def kill(self, node: str) -> None:
        common.ssh_text(self.full["nodes"][node],
                        f"systemctl kill -s KILL {self.service(node)}.service", timeout=30)

    def release(self, node: str) -> None:
        """The hold taken away and the unit started on what it runs."""
        common.ssh_text(self.full["nodes"][node],
                        f"rm -f {shlex.quote(self.hold_path(node))} && systemctl daemon-reload"
                        f" && systemctl start {self.service(node)}.service", timeout=60)

    def staged_identity(self, node: str) -> dict | None:
        """The `--print-fingerprint` identity of what is staged for
        contributor `node` (its staged build's av-contrib from the build
        cache, its staged arguments), run here; None when nothing is
        staged."""
        staged = self.staged.get(node)
        if staged is None:
            return None
        key = staged["build"]
        args, _unit, _service = self.node_args(node, self.node_settings(node, self.given), key)
        return contributor_changes.identity_of(builds_dir() / key / "av-contrib", args)


def linode_ops(args: argparse.Namespace) -> LinodeOps:
    """The backend of a change command: this lab's state, the worktree's
    settings (which must be this lab's, as its routes and hunted ports),
    this command's `--set` and `--adopt-settings`."""
    lab_tool_checked()
    full = common.load_state()
    lab_files_checked(full)
    settings = load_settings([], lab_run_id=full["run_id"])
    return LinodeOps(full, settings, parse_overrides(getattr(args, "set", None) or []),
                     adopt=getattr(args, "adopt_settings", False))


def engine_options(args: argparse.Namespace) -> mesh_ops.Options:
    return mesh_ops.Options(
        force=getattr(args, "force", False), dry=getattr(args, "dry_marks", False),
        allow_dirty=getattr(args, "allow_dirty", False), gate_timeout_s=args.gate_timeout_s,
        drain_timeout_s=args.drain_timeout_s, ready_timeout_s=args.ready_timeout_s,
        on_failure=args.on_failure, surge=args.surge, parity_ms=args.parity_ms,
        video_gap=args.video_gap, hold_s=getattr(args, "hold_s", 0.0),
        rollback=getattr(args, "rollback", False), marks_file=args.marks,
        node_marks_file=args.node_marks)


def names_of(text: str | None) -> list[str] | None:
    return [name for name in text.split(",") if name] if text else None


def bootstrap(nodes: list[dict]) -> None:
    """BOOTSTRAP on every node at once; each VLAN address must be there."""
    interfaces = common.parallel(
        nodes, lambda node: common.ssh_text(node, BOOTSTRAP, timeout=900))
    for node in nodes:
        expected = node.get("vlan_ipv4")
        if expected and f"={expected}/" not in interfaces[node["name"]]:
            raise RuntimeError(
                f"{node['name']}: VLAN address {expected} is not configured: "
                f"{interfaces[node['name']]}")
    print(f"bootstrapped {len(nodes)} hosts; every VLAN address is present", file=sys.stderr)


def command_plan(args: argparse.Namespace) -> tuple[dict, dict, dict]:
    """Plan and render the services of every node but the standby edges;
    returns the whole lab state (push installs the tools on every node and
    stops the service of a standby edge), the settings and the render."""
    full = common.load_state()
    lab_files_checked(full, remeasure=args.remeasure)
    settings = load_settings(args.set, lab_run_id=full["run_id"])
    state = active_state(full, settings)
    routes_path = common.ARTIFACT_DIR / "routes.json"
    routes = json.loads(routes_path.read_text()) if routes_path.is_file() else None
    # Routes measured over the other topology's relays seed the controller
    # with the wrong paths (a file without "topology" is legacy).
    if (args.remeasure or routes is None
            or routes.get("topology", "legacy") != topology_mode(settings)):
        routes = {**measure_routes(state, settings), "lab_run_id": full["run_id"]}
        routes_path.write_text(json.dumps(routes, indent=2) + "\n")
    program = build_program(state, settings, routes)
    plan = compile_plan(program)
    # The render looks at the flags of the binaries that push deploys.
    RENDER_BINARIES.clear()
    RENDER_BINARIES.update({"av-mesh": binaries_for("edge")[0],
                            "av-contrib": binaries_for("contributor")[0]})
    rendered = render(state, settings, plan)
    unused = relay_tree(state, settings)["unused"]
    print(f"compiled {len(plan['services'])} relay services ({topology_mode(settings)} "
          f"topology); rendered {len(rendered)} service argument files; primary route "
          f"{routes['primary_rtt_us']} us, secondary {routes['secondary_rtt_us']} us"
          + (f"; backbone relays not used (stopped by push and restart): "
             f"{', '.join(node['name'] for node in unused)}" if unused else "")
          + (f"; standby edges (stopped): {', '.join(standby_edges(settings))}"
             if standby_edges(settings) else ""))
    print("contributor targets: " + " ".join(common.contributor_targets(state)))
    return full, settings, rendered


def named_nodes(state: dict, text: str | None) -> list[str] | None:
    """The names of --nodes (comma-separated), each a created node of the
    lab; None when --nodes is not given."""
    if not text:
        return None
    names = [name for name in text.split(",") if name]
    missing = [name for name in names if name not in state["nodes"]]
    if missing:
        slots = lab_slots(state)
        sys.exit("--nodes: not created in the lab: " + ", ".join(
            f"{name}{' (an autoscale slot: lab add first)' if name in slots else ''}"
            for name in missing))
    return names


def held_out_after(settings: dict, start: list[str] = (), stop: list[str] = ()) -> list[str]:
    """standby_edges without `start` and with `stop`."""
    held = [name for name in standby_edges(settings) if name not in start]
    return sorted(set(held) | set(stop))


def command_push(args: argparse.Namespace) -> None:
    """Bootstrap, then stage and activate every node, role by role, parents
    first, on the roots' builds; the deployed record follows each role. Only
    a whole push (and roll) moves a tier's build: `--nodes` (a node taken
    into service, an autoscaler's slot, a replaced instance) and
    `--skip-binaries` install each tier's recorded build."""
    state = common.load_state()
    only = named_nodes(state, getattr(args, "nodes", None))
    if only is not None:
        # A node pushed by name is taken into service (it may have been
        # held out); its peers already name it, so nothing else restarts.
        saved = load_settings(list(args.set), lab_run_id=state["run_id"])
        args.set = list(args.set) + [
            f"standby_edges={json.dumps(held_out_after(saved, start=only))}"]
    nodes = [node for node in state["nodes"].values() if only is None or node["name"] in only]
    if only is not None and (args.remeasure
                             or not (common.ARTIFACT_DIR / "routes.json").is_file()):
        sys.exit("push --nodes keeps the routes of the last whole push (measuring them "
                 "would reach other nodes): push the whole lab first, and no --remeasure")
    lab_tool_checked()
    with ops_locked():
        ensure_tls()
        if not args.skip_bootstrap:
            bootstrap(nodes)
        args.remeasure = args.remeasure or not (common.ARTIFACT_DIR / "routes.json").is_file()
        full, settings, _rendered = command_plan(args)
        ops = LinodeOps(full, settings)
        tiers = sorted({mesh_ops.ROLE_TIER[node["role"]] for node in nodes
                        if node["role"] in mesh_ops.ROLE_TIER})
        try:
            builds = None if args.skip_binaries or only is not None else {
                tier: ops.target(tier) for tier in tiers}
            ops.install(only, builds, binaries=not args.skip_binaries)
        except mesh_ops.Refused as refused:
            print(refused, file=sys.stderr)
            sys.exit(mesh_ops.EXIT_REFUSED)


def command_restart(args: argparse.Namespace) -> None:
    """`restart --nodes a,b`: one node at a time through the engine's gates
    and readiness, each on its recorded build (`--rollback`: `.args.prev`
    in force; `--hold-s`: held stopped that long; a contributor through
    workstream A's gate and readiness, so `--hold-s 20` on a contributor
    is A's hand-over). `restart --roles`: every node of a role at once, on
    its recorded build, for a cold bring-up."""
    if args.nodes:
        if args.roles:
            sys.exit("restart takes --nodes or --roles, not both")
        names = names_of(args.nodes)
        ops = linode_ops(args)
        sys.exit(mesh_ops.restart_nodes(ops, names, options=engine_options(args),
                                        resume=args.resume))
    if args.rollback or args.hold_s or args.resume:
        sys.exit("--rollback, --hold-s and --resume go with --nodes")
    print("restart --roles restarts every node of a role at once; it is not a rolling restart "
          "(roll, restart --nodes)", file=sys.stderr)
    lab_tool_checked()
    with ops_locked():
        args.remeasure = getattr(args, "remeasure", False)
        full, settings, _rendered = command_plan(args)
        roles = args.roles.split(",") if args.roles else list(SERVICE_ROLES)
        names = [node["name"] for role in SERVICE_ROLES if role in roles
                 for node in common.nodes_by_role(full, role)]
        try:
            LinodeOps(full, settings).install(names, None)
        except mesh_ops.Refused as refused:
            print(refused, file=sys.stderr)
            sys.exit(mesh_ops.EXIT_REFUSED)


def command_roll(args: argparse.Namespace) -> None:
    """`roll edges|forwarders|contributors|all` (C 3.5): one node at a time
    onto the roots' build, through the engine (mesh_ops.roll). stdout
    carries the change's two marks only; the exit status is the engine's."""
    ops = linode_ops(args)
    sys.exit(mesh_ops.roll(ops, args.tier, nodes=names_of(args.nodes), sites=names_of(args.sites),
                           options=engine_options(args), resume=args.resume))


def command_replace(args: argparse.Namespace) -> None:
    """`replace --nodes X` (C 3.7): one node on a new disk (`--mode
    rebuild`, the default: both addresses kept) or a new instance (`--mode
    new`), through the engine (mesh_ops.replace): an edge emptied first, a
    forwarder through its region's slot (`--no-slot`: without), a
    contributor through workstream A's gate and readiness; then pushed on
    its tier's recorded build. Every instance the lab tool rebuilds or
    creates (the node, the slot) is held to `--budget-usd` and
    `--spent-before-usd`. Before the first mark, under the ops lock
    (LinodeOps.check_replace): a new instance whose public address other
    nodes' arguments name (a compiled forwarder: its parents'
    --relay-forward, its children's peers, the other regions'
    --failover-entry) is refused, naming them, for their arguments and its
    address cannot agree while it is replaced; and so is a replace that
    the lab tool would refuse once the node was emptied and stopped (the
    watchdog, the instance's tag, the ceilings, the budget). stdout carries
    the change's two marks only; the exit status is the engine's."""
    names = names_of(args.nodes) or []
    if len(names) != 1:
        print("replace --nodes takes one node", file=sys.stderr)
        sys.exit(mesh_ops.EXIT_REFUSED)
    name = names[0]
    ops = linode_ops(args)
    missing = [call for call in REPLACE_LAB_CALLS if not hasattr(lab_tool(), call)]
    if missing:
        print(f"scripts/linode-small-mesh-lab.py has no {', '.join(missing)}: replace checks "
              "the lab tool's refusals before it touches the node (C CT3)", file=sys.stderr)
        sys.exit(mesh_ops.EXIT_REFUSED)
    ops.replacing = {"mode": args.mode, "no_slot": args.no_slot, "resume": args.resume,
                     "budget_usd": args.budget_usd, "spent_before_usd": args.spent_before_usd}
    sys.exit(mesh_ops.replace(ops, name, mode=args.mode, no_slot=args.no_slot,
                              keep_slot=args.keep_slot, resume=args.resume,
                              options=engine_options(args)))


def command_change(args: argparse.Namespace) -> None:
    """`kill --node X` and `return --node X` (A 2.10): workstream A's
    contributor and ingress changes (contributor_changes.run) under the ops
    lock. stdout carries the change's two marks only; the exit status is
    the change's."""
    ops = linode_ops(args)
    with ops_locked():
        sys.exit(contributor_changes.run(ops, args.command, args))


def checked_for_start_stop(state: dict, names: list[str], verb: str) -> None:
    """Exit unless every name is an edge of the lab or an autoscale slot: a
    contributor or a site's first distributors are never started or stopped
    by name (they are not held out; the relay DAG needs them)."""
    slots = lab_slots(state)
    for name in names:
        node = state["nodes"][name]
        if node["role"] != "edge" and name not in slots:
            sys.exit(f"{verb} --nodes: {name} is a {node['role']}; only edges and autoscale "
                     "slots are taken in and out of service by name")


def command_start(args: argparse.Namespace) -> None:
    """Take held-out nodes into service and start them, each on its tier's
    recorded build from the build cache (never a root: only push and roll
    move a tier's build), after the reproduce check (LinodeOps.check):
    every node is rendered with the others in service, so no other node's
    arguments change. A node just created (lab add) needs `push --nodes`,
    which also bootstraps it."""
    full = common.load_state()
    names = named_nodes(full, args.nodes)
    checked_for_start_stop(full, names, "start")
    ops = linode_ops(args)
    try:
        ops.check("start", names)
        ops.stage(names, None)
        for name in names:
            ops.start_node(name)
    except mesh_ops.Refused as refused:
        print(refused, file=sys.stderr)
        sys.exit(mesh_ops.EXIT_REFUSED)
    print(f"started {', '.join(names)} on "
          + ", ".join(sorted({ops.staged[name]['build'] for name in names}))
          + "; no other node was restarted")


def command_stop(args: argparse.Namespace) -> None:
    """Hold nodes out of service and stop their service, and nothing else.
    Their peers keep naming them (a peer that is down)."""
    full = common.load_state()
    names = named_nodes(full, args.nodes)
    checked_for_start_stop(full, names, "stop")
    saved = load_settings(list(args.set), lab_run_id=full["run_id"])
    load_settings(list(args.set) + [
        f"standby_edges={json.dumps(held_out_after(saved, stop=names))}"], lab_run_id=full["run_id"])
    common.parallel([full["nodes"][name] for name in names], lambda node: common.ssh_text(
        node, "systemctl disable --now needletail-mesh.service >/dev/null 2>&1 || true; "
              "systemctl is-active needletail-mesh.service || true"))
    print(f"stopped {', '.join(names)} and held them out (standby_edges); no other node "
          "was touched")


def command_status(args: argparse.Namespace) -> None:
    state = common.load_state()

    def probe(node: dict) -> str:
        return common.ssh_text(node, r"""
service=none
for candidate in needletail-mesh needletail-contrib; do
  if systemctl cat $candidate.service >/dev/null 2>&1; then service=$candidate; fi
done
active=$(systemctl is-active $service.service 2>/dev/null || true)
restarts=$(systemctl show $service.service -p NRestarts --value 2>/dev/null || echo -)
memory=$(systemctl show $service.service -p MemoryCurrent --value 2>/dev/null || echo 0)
offset=$(chronyc -c tracking 2>/dev/null | awk -F, '{printf "%.3f", $5*1000}')
steal=$(awk '/^cpu /{printf "%.2f", $9*100/($2+$3+$4+$5+$6+$7+$8+$9)}' /proc/stat)
speed=$(python3 -c "
import hashlib, time
block = b'x' * 65536
best = 0.0
for _ in range(3):
    start = time.perf_counter(); digest = hashlib.sha256()
    for _ in range(1500): digest.update(block)
    best = max(best, 1500 * 65536 / 1e6 / (time.perf_counter() - start))
print(round(best))")
echo "$service $active restarts=$restarts mem_mb=$((${memory:-0}/1048576)) clock_offset_ms=$offset steal_pct=$steal load=$(cut -d' ' -f1 /proc/loadavg) cpu_mb_s=$speed"
""", check=False, timeout=60)

    results = common.parallel(list(state["nodes"].values()), probe)
    targets = {name: (node["public_ipv4"],
                      CONTRIB_HTTP_PORT if node["role"] == "contributor" else HTTP_PORT, "/api/node")
               for name, node in state["nodes"].items()
               if node["role"] in mesh_ops.ROLE_TIER and node.get("public_ipv4")}
    answers = mesh_ops.Http().poll(targets)
    recorded = (state.get("deployed") or {}).get("nodes") or {}
    speeds = {}
    for name, line in results.items():
        if "cpu_mb_s=" in line:
            speeds[name] = float(line.rsplit("cpu_mb_s=", 1)[1] or 0)
    median = sorted(speeds.values())[len(speeds) // 2] if speeds else 0
    slow = sorted(name for name, speed in speeds.items() if speed < 0.75 * median)
    for name in sorted(results):
        node = state["nodes"][name]
        flag = "  <-- SLOW shared CPU" if name in slow else ""
        served = ""
        if name in targets:
            code, document = answers.get(name, (None, None))
            served = node_summary(document if code == 200 else None, recorded.get(name))
        print(f"{name:<18} {node['role']:<12} {results[name]}{served}{flag}")
    # Shared-CPU hosts vary by 2x in single-thread speed without reporting steal.
    print(f"median single-thread speed {median:.0f} MB/s; slow nodes "
          f"(<75% of median): {' '.join(slow) or 'none'}")
    for line in deployed_summary(state):
        print(line)


def node_summary(document, entry: dict | None) -> str:
    """A node's build, state, drain reasons and boot time from its
    `/api/node`, and whether it runs what the deployed record says."""
    status = mesh_ops.status_of(document)
    if status is None:
        return " node=silent"
    boot = (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(status.boot_id / 1e6))
            if status.boot_id else "-")
    if not entry:
        record = "record=none"
    elif (f"{status.build_git}-{status.build_unix}", status.args_sha256) == \
            (entry.get("build"), entry.get("args_sha256")):
        record = "record=same"
    else:
        record = f"record=DIFFERS({entry.get('build')})"
    return (f" build={status.build_git}-{status.build_unix} state={status.state} "
            f"drain={','.join(status.drain_reasons or []) or '-'} boot={boot} {record}")


def deployed_summary(state: dict) -> list[str]:
    """The deployed record (tiers, settings against this worktree's) and
    every change command that did not finish, with the nodes it left."""
    deployed = state.get("deployed") or {}
    lines = [f"deployed: tiers {json.dumps(deployed.get('tiers') or {}, sort_keys=True)}, "
             f"{len(deployed.get('nodes') or {})} node(s) recorded"]
    if SETTINGS_FILE.is_file():
        saved = json.loads(SETTINGS_FILE.read_text())
        if saved.pop("lab_run_id", None) != state.get("run_id"):
            lines.append(f"deployed: {SETTINGS_FILE} is not this lab's")
        else:
            differ = settings_differ(deployed.get("settings") or {},
                                     merge_settings(saved, {})[0])
            lines.append("deployed: this worktree's settings are the deployed ones" if not differ
                         else "deployed: this worktree's settings differ in "
                              + ", ".join(sorted(differ)))
    for journal in mesh_ops.Journal.unfinished(common.ARTIFACT_DIR / "rolls"):
        left = [f"{hand['node']} ({hand.get('phase')})" for hand in mesh_ops.Journal.hands(journal.data)]
        lines.append(f"unfinished: {journal.data.get('label') or journal.data.get('command')} "
                     f"({journal.path.name}): " + (", ".join(left) or "no node in hand"))
    return lines


# ------------------------------------------------------------ collect (the run archive)

COLLECT_TOOL = "deploy-collect"
COLLECT_TIMEOUT_S = 300.0
COLLECT_WORKERS = 32
# collect's own failure (not a node's): the run is marked not collected.
EXIT_COLLECT_FAILED = 70
# A file the pull ended in, kept under its name with this suffix.
COLLECT_PARTIAL = ".partial"
# Beside the archive root: what collects pull, until each file is moved in.
COLLECT_STAGING = ".needletail-collect"
# What collect asks of a node's own HTTP service, once, at the end: (file
# stem, path, extension). /api/hops with no stream is the hop journal of
# every stream the node holds. api-stats and api-stalls are the names of the
# referee's collect-nodes (needletail-compare scripts/compare/archive-run.py).
MESH_APIS = (("api-node", "/api/node", "json"), ("api-mesh-local", "/api/mesh/local", "json"),
             ("api-stats", "/api/stats", "json"), ("api-stalls", "/api/stalls", "json"),
             ("api-hops", "/api/hops", "ndjson"))
CONTRIBUTOR_APIS = (("api-node", "/api/node", "json"), ("api-stalls", "/api/stalls", "json"),
                    ("api-hops", "/api/hops", "ndjson"))
# The samplers' files of a node, read where they are and left there: the
# lead's tools' run directories (their samplers, listener and probe
# outputs) and the referee's sampler files.
NODE_RUNS = "/var/lib/needletail-runs"
REFEREE_SAMPLERS = ("/var/tmp/compare-*.log", "/var/tmp/compare-*.carry")
COLLECT_FILES = (
    (r"^journal\.", "node-logs", "the node's journal since boot (journalctl -b -o "
                                 "short-iso-precise), gzipped", None),
    (r"^api-node\.", "node-api", "the node's /api/node at collection: build, state, readiness",
     None),
    (r"^api-mesh-local\.", "node-api", "the node's /api/mesh/local at collection", None),
    (r"^api-stats\.", "node-api", "the node's /api/stats at collection", None),
    (r"^api-stalls\.", "node-api", "the node's stop journal (/api/stalls) at collection", None),
    (r"^api-hops\.", "node-api", "the node's hop journal (/api/hops, every stream) at collection",
     {"t_us": "us"}),
    (r"^http-status\.", "node-api", "the HTTP status of each API answer of this collect", None),
)


def collect_apis(node: dict) -> tuple:
    if node["role"] == "contributor":
        return CONTRIBUTOR_APIS
    return MESH_APIS if node["role"] in mesh_ops.ROLE_TIER else ()


def collect_script(node: dict, stamp: str) -> str:
    """What one ssh runs on a node: a gzipped tar on its stdout of the
    journal since boot (gzipped), each API's body with its HTTP status and
    curl's exit status, and the samplers' files. Nothing on the node is
    changed or removed but its own temporary directory."""
    port = CONTRIB_HTTP_PORT if node["role"] == "contributor" else HTTP_PORT
    lines = ["set -u", f"T={stamp}",
             "D=$(mktemp -d /tmp/needletail-collect.XXXXXX) || exit 70",
             "trap 'rm -rf \"$D\"' EXIT",
             "journalctl -b --no-pager -o short-iso-precise 2>/dev/null "
             "| gzip -c >\"$D/journal.$T.txt.gz\""]
    for stem, path, extension in collect_apis(node):
        url = shlex.quote(f"https://{node['public_ipv4']}:{port}{path}")
        lines += [f"code=$(curl -sk --max-time 30 -o \"$D/{stem}.$T.{extension}\" "
                  f"-w '%{{http_code}}' {url} 2>/dev/null); rc=$?",
                  f"echo \"{stem} ${{code:-000}} $rc\" >>\"$D/http-status.$T.txt\""]
    parent, name = NODE_RUNS.rsplit("/", 1)
    lines += ["set -- -C \"$D\" .",
              f"[ -d {NODE_RUNS} ] && set -- \"$@\" -C {parent} {name}",
              f"for f in {' '.join(REFEREE_SAMPLERS)}; do "
              "[ -f \"$f\" ] && set -- \"$@\" -C /var/tmp \"${f#/var/tmp/}\"; done",
              "tar -cf - \"$@\" 2>/dev/null | gzip -1", "exit 0"]
    return "\n".join(lines) + "\n"


def collect_member_path(name: str) -> str | None:
    """Where a member of a node's tar goes under nodes/<node>/: the node's
    own files (`./...`) at the top, its run directories under
    samplers/needletail-runs/, the referee's sampler files under
    samplers/var-tmp/. None for a name that would leave the directory, or
    one that ends as a cut member's does."""
    parts = pathlib.PurePosixPath(name).parts
    if name.startswith("/") or ".." in parts or name.endswith(COLLECT_PARTIAL):
        return None
    if name.startswith("./"):
        return pathlib.PurePosixPath(*parts).as_posix() if parts else None
    if parts and parts[0] == NODE_RUNS.rsplit("/", 1)[1]:
        return "samplers/" + pathlib.PurePosixPath(*parts).as_posix()
    return f"samplers/var-tmp/{pathlib.PurePosixPath(*parts).as_posix()}" if parts else None


def unpack_stream(stream, directory: pathlib.Path) -> tuple[list[str], str | None, str | None]:
    """The regular files of a node's tar, read as it streams in, into its
    staging directory: (the files whole, the one cut part-way, why the tar
    ended early or None). Each member is written as <name>.partial and
    renamed when whole, so one the tar ended in stays marked as cut."""
    files: list[str] = []
    current: tuple[str, pathlib.Path] | None = None
    try:
        with tarfile.open(fileobj=stream, mode="r|gz") as tar:
            for member in tar:
                rel = collect_member_path(member.name)
                if not member.isfile() or rel is None:
                    continue
                target = directory / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                current = (rel, target.with_name(target.name + COLLECT_PARTIAL))
                with tar.extractfile(member) as source, open(current[1], "wb") as out:
                    shutil.copyfileobj(source, out)
                current[1].rename(target)
                current = None
                files.append(rel)
    except (tarfile.TarError, OSError, EOFError, zlib.error) as error:
        cut = current[0] + COLLECT_PARTIAL if current and current[1].is_file() else None
        return files, cut, f"its tar ended early: {error}"
    return files, None, None


def pull_node(node: dict, stamp: str, directory: pathlib.Path, deadline: float) -> dict:
    """One node's files into its staging directory as they stream in, by
    one ssh killed at `deadline` (time.monotonic). Whatever arrived is kept,
    also from a node that ran out of time."""
    record = {"node": node["name"], "role": node["role"], "error": None, "files": [],
              "partial": None, "http": {}, "curl_exit": {}}
    began = time.monotonic()
    left = deadline - began
    if left < 1:
        record.update(error="no time left within the collect's --timeout-s", seconds=0.0)
        return record
    command = ["ssh", *common.ssh_base(), f"root@{node['public_ipv4']}",
               "bash -c " + shlex.quote(collect_script(node, stamp))]
    killed = threading.Event()
    with tempfile.TemporaryFile() as errors:
        try:
            process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                       stderr=errors)
        except OSError as error:
            record.update(error=f"ssh: {error}", seconds=round(time.monotonic() - began, 3))
            return record

        def stop() -> None:
            if process.poll() is None:
                killed.set()
                process.kill()

        timer = threading.Timer(left, stop)
        timer.daemon = True
        timer.start()
        try:
            record["files"], record["partial"], ended = unpack_stream(process.stdout, directory)
            # A tar that ended wrong: ssh is given a moment to end by
            # itself (its own error says more), else stopped.
            stopped = False
            if ended:
                try:
                    process.wait(timeout=min(5.0, max(0.0, deadline - time.monotonic())))
                except subprocess.TimeoutExpired:
                    stopped = True
                    process.kill()
            returncode = process.wait()
        finally:
            timer.cancel()
            process.stdout.close()
        errors.seek(0)
        said = errors.read().decode(errors="replace").strip()[-300:]
    if killed.is_set():
        record["error"] = f"timed out after {left:.0f} s"
    elif returncode != 0 and not stopped:
        record["error"] = f"ssh exited {returncode}: {said}" + (f" ({ended})" if ended else "")
    elif ended:
        record["error"] = ended
    for status in directory.glob("http-status.*.txt"):
        for line in status.read_text(errors="replace").splitlines():
            if not line.strip():
                continue
            stem, code, curl_exit = (line.split() + ["", ""])[:3]
            record["http"][stem] = code
            record["curl_exit"][stem] = curl_exit
    record["seconds"] = round(time.monotonic() - began, 3)
    return record


def node_missing(node: dict, record: dict, directory: pathlib.Path) -> list[str]:
    """What of a node this collect could not have: the node (its ssh failed
    or ran out of time, or its tar was cut), else each file: the journal
    when empty, an API that did not answer 200 whole (curl exited 0)."""
    if record["error"]:
        return [node["name"]]
    out = []
    journals = sorted(directory.glob("journal.*.txt.gz"))
    try:
        with gzip.open(journals[0], "rb") as handle:
            empty = not handle.read(1)
    except (IndexError, OSError, EOFError):
        empty = True
    if empty:
        out.append(f"{node['name']}/journal")
    out += [f"{node['name']}/{stem}" for stem, _path, _extension in collect_apis(node)
            if record["http"].get(stem) != "200" or record["curl_exit"].get(stem) != "0"]
    return out


def collect_describe(rel: str) -> tuple[str, str, dict | None]:
    whole = rel[:-len(COLLECT_PARTIAL)] if rel.endswith(COLLECT_PARTIAL) else rel
    for pattern, kind, text, units in COLLECT_FILES:
        if re.search(pattern, whole):
            return kind, (f"CUT PART-WAY (the pull ended in it): {text}" if whole != rel
                          else text), units
    return "raw", "", None


def remove_empty(directory: pathlib.Path) -> None:
    """The empty directories under `directory`, and it when empty; a
    directory that still holds a file is left (never a file removed)."""
    for path in sorted(directory.rglob("*"), key=lambda item: len(item.parts), reverse=True):
        if path.is_dir() and not path.is_symlink():
            with contextlib.suppress(OSError):
                path.rmdir()
    with contextlib.suppress(OSError):
        directory.rmdir()


def archive_node(run: archived_run.Run, name: str, directory: pathlib.Path) -> list[str]:
    """Every file of a node's staging directory moved into nodes/<node>/:
    its own files at the top (a cut one as <name>.partial), its samplers'
    files under samplers/. A file the archive refuses as a credential is
    deleted, never kept; any other that could not be moved stays in
    staging for the next collect. Returns what could not be moved."""
    failed = []
    for path in sorted(item for item in directory.iterdir() if item.is_file()):
        kind, text, units = collect_describe(path.name)
        if run.add_file(path, f"nodes/{name}/{path.name}", kind, text, units, move=True) is None:
            failed.append(f"{name}/{path.name}")
    samplers = directory / "samplers"
    if samplers.is_dir():
        result = run.add_tree(
            samplers, f"nodes/{name}/samplers", "samples",
            f"the node's run directories ({NODE_RUNS}: samplers, listener and probe outputs) "
            "and the referee's sampler files (/var/tmp/compare-*), as they were at collection "
            f"(a name ending {COLLECT_PARTIAL}: cut part-way)", move=True)
        if result is None:
            failed.append(f"{name}/samplers")
        else:
            for rel in result["refused"]:
                (samplers / rel).unlink(missing_ok=True)
                failed.append(f"{name}/samplers/{rel} (refused: a credential)")
    remove_empty(directory)
    return failed


def scrubbed_record(deployed: dict) -> dict:
    """The deployed record with its settings scrubbed of secrets; the
    nodes' build keys and digests kept as they are."""
    if not isinstance(deployed.get("settings"), dict):
        return dict(deployed)
    return {**deployed, "settings": archived_run.scrub_settings(deployed["settings"])}


def mesh_worktree() -> pathlib.Path:
    """The av-mesh worktree the deploy takes its binaries from (binaries_for)."""
    return pathlib.Path(os.environ.get("NEEDLETAIL_AV_MESH_ROOT", str(common.WORKSPACE / "av-mesh")))


def collect_code_paths() -> list[pathlib.Path]:
    """The worktrees whose builds the deploy installs (binaries_for)."""
    return archived_run.code_paths(
        mesh_worktree(),
        os.environ.get("NEEDLETAIL_AV_CONTRIB_ROOT", str(common.WORKSPACE / "av-contrib")))


def deployed_builds(state: dict) -> dict:
    """What the lab ran, from the deployed record rather than from the
    worktrees collect happens to see: each repository's build keys (av-mesh
    on the mesh tiers, av-contrib on the contributor) with the nodes that
    run each, and the playlists sources that av-mesh's Cargo.lock pins at
    each av-mesh build's commit (read from the av-mesh object store, which
    its worktrees share; a dirty build's own lock may differ)."""
    nodes = (state.get("deployed") or {}).get("nodes") or {}
    out: dict = {"av-mesh": {}, "av-contrib": {}, "playlists": {}}
    for name, entry in sorted(nodes.items()):
        role = (state["nodes"].get(name) or {}).get("role")
        repo = "av-contrib" if role == "contributor" else \
            "av-mesh" if role in mesh_ops.ROLE_TIER else None
        if repo and entry.get("build"):
            out[repo].setdefault(entry["build"], []).append(name)
    for key in sorted(out["av-mesh"]):
        commit = key_build(key)["git"].removesuffix("-dirty")
        try:
            lock = subprocess.run(["git", "-C", str(mesh_worktree()), "show", f"{commit}:Cargo.lock"],
                                  capture_output=True, text=True, timeout=30, check=False)
        except (OSError, subprocess.SubprocessError) as error:
            out["playlists"][key] = {"error": f"git: {error}"}
            continue
        if lock.returncode != 0:
            out["playlists"][key] = {"error": f"git show {commit}:Cargo.lock: "
                                              f"{lock.stderr.strip()[-200:]}"}
            continue
        out["playlists"][key] = re.findall(
            r'name = "playlists"\nversion = "[^"]*"\nsource = "([^"]+)"', lock.stdout)
    return out


def topology_path(state: dict) -> pathlib.Path | None:
    raw = state.get("topology_file")
    if not raw:
        return None
    path = pathlib.Path(raw)
    return path if path.is_absolute() else common.ROOT / path


def collect_run(state: dict) -> archived_run.Run:
    """The lab's run in the archive (the lab's run id), made with the lab's
    facts when it is not there yet, else opened to append to; either way
    the deployed settings (scrubbed) go into its manifest's deploy_settings.
    Collect never ends the run: the run's own tool and the lab's down do."""
    topology = topology_path(state)
    run = archived_run.Run(
        state["run_id"], "linode-lab", COLLECT_TOOL, code=collect_code_paths(),
        topology=topology if topology and topology.is_file() else None,
        regions=sorted({node["region"] for node in state["nodes"].values() if node.get("region")}),
        finish=False)
    settings = (state.get("deployed") or {}).get("settings")
    if settings:
        run.guarded("record the deploy settings", run.archive.set_deploy_settings,
                    archived_run.scrub_settings(settings))
    return run


def collect_lab(run: archived_run.Run, state: dict, stamp: str) -> list[str]:
    """The lab's own files: its state (with every node's physical host),
    the hosts map, the deployed record and the builds it names, the
    settings, the routes and hunted ports, the topology; the lab's
    instances in the manifest. Returns what could not be had."""
    missing = []
    deployed = state.get("deployed") or {}
    run.write_json(f"lab/state.{stamp}.json", {**state, "deployed": scrubbed_record(deployed)},
                   "lab", "the lab state at collection: nodes, addresses, plans, watchdog, "
                          "budget and the deployed record")
    run.write_json(f"lab/hosts.{stamp}.json",
                   {name: {key: node.get(key) for key in
                           ("id", "label", "region", "site", "role", "type", "host_uuid")}
                    for name, node in sorted(state["nodes"].items(), key=lambda item:
                                             mesh_ops.natural(item[0]))},
                   "lab", "the physical Linode host of every node (host_uuid as the lab tool's "
                          "`hosts` recorded it in the lab state; null: never recorded)")
    if deployed:
        run.write_json(f"lab/deployed.{stamp}.json", scrubbed_record(deployed), "lab",
                       "the deployed record: settings in force, routes and ports digests, each "
                       "node's build and argument digests")
        run.write_json(f"lab/builds.{stamp}.json", deployed_builds(state), "lab",
                       "the builds the lab ran, from the deployed record: av-mesh and av-contrib "
                       "build keys (<git>-<unix>) with their nodes, and the playlists sources "
                       "av-mesh's Cargo.lock pins at each av-mesh build's commit")
    else:
        missing.append("lab/deployed")
    own = {}
    for label, path, read in (
            ("settings", SETTINGS_FILE, lambda text: json.loads(text)),
            ("routes", common.ARTIFACT_DIR / "routes.json", lambda text: json.loads(text)),
            ("ecmp-ports", HUNT_FILE, lambda text: json.loads(text))):
        try:
            document = read(path.read_text())
        except (OSError, ValueError):
            continue
        if isinstance(document, dict) and document.get("lab_run_id") == state["run_id"]:
            own[label] = document
    if "settings" in own:
        run.write_json(f"lab/settings.{stamp}.json", archived_run.scrub_settings(own.pop("settings")),
                       "settings", "this checkout's settings.json for the lab (scrubbed)")
    elif not deployed.get("settings"):
        missing.append("lab/settings")
    for label, document in own.items():
        run.write_json(f"lab/{label}.{stamp}.json", document, "lab",
                       f"this checkout's {label} file for the lab")
    topology = topology_path(state)
    if topology is None or run.add_file(topology, f"topology/{topology.name}", "topology",
                                        "the lab's topology") is None:
        missing.append("topology")
    created = state.get("created_unix")
    run.guarded("record the lab", run.archive.set_lab,
                run_tag=state.get("tag") or f"needletail-small-mesh-{state['run_id']}",
                instances=[{"label": node.get("label"), "id": node.get("id"),
                            "plan": node.get("type"), "region": node.get("region"),
                            "role": node.get("role"), "hourly_usd": node.get("hourly_usd")}
                           for _name, node in sorted(state["nodes"].items())],
                created_at_utc=time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(created))
                if created else None)
    return missing


def collect_staging(run: archived_run.Run) -> pathlib.Path:
    """Where collects of this run stage what they pull:
    <archive root's parent>/.needletail-collect/<run id>/, on the archive's
    filesystem so that a move is a hard link, and outside the root, which
    refuses to move its own files. One directory per collect, held under
    its .lock while that collect lives."""
    return run.root.parent / COLLECT_STAGING / run.run_id


def adopt_staged(run: archived_run.Run, staging: pathlib.Path,
                 own: pathlib.Path) -> dict[str, list[str]]:
    """What earlier collects of this run pulled and never moved (one that
    was killed, or a move that failed) into nodes/<node>/ now: each staged
    collect but this one (`own`) whose .lock no live collect holds.
    Returns, per staged collect, what still could not be moved (left in
    place for the next)."""
    left: dict[str, list[str]] = {}
    for earlier in sorted(path for path in staging.iterdir() if path.is_dir() and path != own):
        lock = earlier / ".lock"
        try:
            fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
        except OSError:
            continue
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                continue
            failed = []
            for node in sorted(path for path in earlier.iterdir() if path.is_dir()):
                failed += archive_node(run, node.name, node)
            lock.unlink(missing_ok=True)
            remove_empty(earlier)
            if failed:
                left[earlier.name] = failed
        finally:
            os.close(fd)
    return left


def collect_nodes(run: archived_run.Run, state: dict, names: list[str], deadline: float,
                  stamp: str, mine: pathlib.Path) -> tuple[dict, list[str]]:
    """Every named node at once, each by one ssh killed at `deadline`, and
    each node moved into nodes/<node>/ as soon as its pull ends (a line in
    collect/progress.<t>.ndjson says so, which a collect killed later still
    shows). Returns each node's record and what could not be had. One node
    never stops the others."""
    records: dict[str, dict] = {}
    missing: list[str] = []
    began = time.monotonic()

    def one(node: dict) -> dict:
        directory = mine / node["name"]
        directory.mkdir()
        try:
            record = pull_node(node, stamp, directory, deadline)
            record["missing"] = node_missing(node, record, directory)
        except Exception as error:  # noqa: BLE001 - what arrived is still moved in
            record = {"node": node["name"], "role": node["role"], "seconds": None,
                      "error": f"{type(error).__name__}: {error}", "partial": None,
                      "missing": [node["name"]]}
        record["unmoved"] = archive_node(run, node["name"], directory)
        record["moved_at_s"] = round(time.monotonic() - began, 3)
        run.append_ndjson(f"collect/progress.{stamp}.ndjson",
                          {key: record[key] for key in ("node", "error", "partial", "missing",
                                                        "unmoved", "seconds", "moved_at_s")},
                          "lab", "each node as this collect moved it into the archive, in the order "
                                 "they ended", {"seconds": "s", "moved_at_s": "s"})
        return record

    nodes = [state["nodes"][name] for name in names]
    with concurrent.futures.ThreadPoolExecutor(
            max_workers=max(1, min(COLLECT_WORKERS, len(nodes)))) as pool:
        futures = {pool.submit(one, node): node for node in nodes}
        for future in concurrent.futures.as_completed(futures):
            node = futures[future]
            try:
                records[node["name"]] = future.result()
            except Exception as error:  # noqa: BLE001 - one node never stops the collect
                records[node["name"]] = {"node": node["name"], "role": node["role"],
                                         "error": f"{type(error).__name__}: {error}",
                                         "missing": [node["name"]], "unmoved": []}
    for name in names:
        missing += records[name]["missing"] + records[name]["unmoved"]
    return records, missing


def earlier_collects(run: archived_run.Run) -> list[dict]:
    """The records (collect/collect.<t>.json) of this run's earlier
    collects, oldest first."""
    out = []
    for entry in run.archive.manifest["files"]:
        if re.fullmatch(r"collect/collect\.[^/]+\.json", entry["path"]):
            try:
                out.append(json.loads((run.path / entry["path"]).read_text()))
            except (OSError, ValueError):
                continue
    return out


def carried_missing(earlier: list[dict], name: str) -> list[str]:
    """What of a node outside --nodes is still missing: what the latest
    earlier collect that pulled it could not have of it, or the node when
    none pulled it."""
    for record in reversed(earlier):
        if name in (record.get("nodes") or {}):
            return [item for item in record.get("missing") or []
                    if item == name or item.startswith(f"{name}/")]
    return [name]


def command_collect(args: argparse.Namespace) -> None:
    """`collect --run-id ID`: every node of the lab (or --nodes), in
    parallel and within --timeout-s of the start, into the run archive's
    nodes/<node>/ (each node as its pull ends), and the lab's files into
    lab/ and topology/; then the run is marked collected, complete or with
    every node and file that could not be had (a node not in --nodes keeps
    what the latest earlier collect that pulled it could not have). Exit 0
    when complete, 1 when not, 2 when refused, 70 when collect itself
    failed (the run is then marked not collected, with why). No Linode API
    call: the hosts map is the one the lab tool's `hosts` recorded."""
    deadline = time.monotonic() + args.timeout_s
    try:
        state = common.load_state()
        names = named_nodes(state, args.nodes) or sorted(state["nodes"], key=mesh_ops.natural)
    except SystemExit as refusal:
        print(f"collect: {refusal.code}", file=sys.stderr)
        sys.exit(mesh_ops.EXIT_REFUSED)
    if state.get("run_id") != args.run_id:
        print(f"collect --run-id {args.run_id}: the lab of {common.state_path()} is "
              f"{state.get('run_id')!r}; a lab's data goes into its own run", file=sys.stderr)
        sys.exit(mesh_ops.EXIT_REFUSED)
    try:
        run = collect_run(state)
    except run_archive.ArchiveError as error:
        print(f"collect: the run archive refused: {error}", file=sys.stderr)
        sys.exit(mesh_ops.EXIT_REFUSED)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    try:
        collect_staging(run).mkdir(parents=True, exist_ok=True)
        mine = pathlib.Path(tempfile.mkdtemp(prefix=f"{stamp}-", dir=collect_staging(run)))
        lock = os.open(mine / ".lock", os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            earlier = earlier_collects(run)
            missing = collect_lab(run, state, stamp)
            records, pulled = collect_nodes(run, state, names, deadline, stamp, mine)
            missing += pulled
            missing += [item for name in sorted(set(state["nodes"]) - set(names),
                                                key=mesh_ops.natural)
                        for item in carried_missing(earlier, name)]
            (mine / ".lock").unlink(missing_ok=True)
        finally:
            os.close(lock)
        remove_empty(mine)
        left_over = adopt_staged(run, collect_staging(run), mine)
        missing += [f"staged {name}: {item}" for name, items in sorted(left_over.items())
                    for item in items]
        missing += [f"archive: {problem}" for problem in run.problems]
        written = len(run.problems)
        run.write_json(f"collect/collect.{stamp}.json",
                       {"run_id": args.run_id, "stamp_utc": stamp, "timeout_s": args.timeout_s,
                        "nodes": records, "missing": missing}, "lab",
                       "what each node gave this collect: its files, a file cut part-way, its "
                       "API answers' HTTP status and curl's exit status, its error, its "
                       "seconds, when it was moved into the archive", {"seconds": "s",
                                                                      "moved_at_s": "s"})
        missing += [f"archive: {problem}" for problem in run.problems[written:]]
    except Exception as error:  # noqa: BLE001 - said, and the run marked not collected
        traceback.print_exc()
        run.guarded("mark the run not collected", run.archive.mark_collected, False,
                    [f"deploy collect failed: {type(error).__name__}: {error}"])
        sys.exit(EXIT_COLLECT_FAILED)
    run.guarded("mark the run collected", run.archive.mark_collected, not missing, missing)
    print(f"collect {args.run_id}: {len(names)} node(s) into {run.path}; missing: "
          + (", ".join(missing) or "none"))
    sys.exit(0 if not missing else 1)


def change_options(parser: argparse.ArgumentParser) -> None:
    """The options of the engine's commands (C 2.6; mesh_ops.Options)."""
    parser.add_argument("--set", action="append", default=[], metavar="KEY=VALUE",
                        help="a setting of ROLLABLE_SETTINGS for the nodes of this command")
    parser.add_argument("--force", action="store_true",
                        help="restart nodes already on the target build too")
    parser.add_argument("--allow-dirty", action="store_true")
    parser.add_argument("--marks", default=None, help="append the change's two marks here")
    parser.add_argument("--node-marks", default=None, help="per-node marks go here")
    parser.add_argument("--dry-marks", action="store_true",
                        help="every check and gate for real, sleeps in place of restarts")
    parser.add_argument("--resume", action="store_true",
                        help="finish what the newest unfinished command left, then go on")
    parser.add_argument("--adopt-settings", action="store_true",
                        help="go on with settings that do not reproduce what is deployed")
    parser.add_argument("--gate-timeout-s", type=float, default=30.0)
    parser.add_argument("--drain-timeout-s", type=float, default=30.0)
    parser.add_argument("--ready-timeout-s", type=float, default=60.0)
    parser.add_argument("--on-failure", choices=("stop", "rollback-all"), default="stop")
    parser.add_argument("--surge", choices=("auto", "never"), default="auto")
    parser.add_argument("--parity-ms", type=float, default=1.0)
    parser.add_argument("--video-gap", choices=("refuse", "accept"), default="refuse")


def build_parser() -> argparse.ArgumentParser:
    """The deploy tool's command line (main's), for the tools that build
    its commands to check them against it."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    plan = sub.add_parser("plan")
    plan.add_argument("--set", action="append", default=[])
    plan.add_argument("--remeasure", action="store_true")
    plan.set_defaults(handler=command_plan)
    push = sub.add_parser("push")
    push.add_argument("--set", action="append", default=[])
    push.add_argument("--remeasure", action="store_true")
    push.add_argument("--skip-bootstrap", action="store_true")
    push.add_argument("--skip-binaries", action="store_true")
    push.add_argument("--nodes", default=None,
                      help="comma-separated: bootstrap, install and start only these (taken "
                           "into service) on their tier's recorded build; every other node is "
                           "left as it runs")
    push.set_defaults(handler=command_push)
    start = sub.add_parser("start", help="take held-out nodes into service, nothing else")
    start.add_argument("--nodes", required=True)
    start.add_argument("--set", action="append", default=[])
    start.set_defaults(handler=command_start)
    stop = sub.add_parser("stop", help="hold nodes out and stop them, nothing else")
    stop.add_argument("--nodes", required=True)
    stop.add_argument("--set", action="append", default=[])
    stop.set_defaults(handler=command_stop)
    restart = sub.add_parser("restart", help="--nodes: one node at a time, gated, on its "
                                             "recorded build; --roles: a cold restart")
    restart.add_argument("--roles", default="")
    restart.add_argument("--nodes", default=None)
    restart.add_argument("--hold-s", type=float, default=0.0,
                         help="keep each node stopped this long before it starts again")
    restart.add_argument("--rollback", action="store_true",
                         help="put .args.prev in force: the rollback of a node an earlier "
                              "command left")
    change_options(restart)
    restart.set_defaults(handler=command_restart)
    roll = sub.add_parser("roll", help="one node at a time onto the roots' build (C 3.5)")
    roll.add_argument("tier", choices=("edges", "forwarders", "contributors", "all"))
    roll.add_argument("--nodes", default=None)
    roll.add_argument("--sites", default=None)
    change_options(roll)
    roll.set_defaults(handler=command_roll)
    replace = sub.add_parser("replace", help="one node on a new disk or a new instance, "
                                             "gated (C 3.7)")
    replace.add_argument("--nodes", required=True, help="the node to replace (one)")
    replace.add_argument("--mode", choices=("rebuild", "new"), default="rebuild",
                         help="rebuild: a new disk, both addresses kept (the lab tool's "
                              "replace); new: a new instance with a new public address")
    replace.add_argument("--no-slot", action="store_true",
                         help="a forwarder without its region's slot standing in")
    replace.add_argument("--keep-slot", action="store_true",
                         help="leave the slot stopped afterwards, not deleted")
    replace.add_argument("--budget-usd", type=float, required=True,
                         help="what the work may cost in all, for the lab tool's creates")
    replace.add_argument("--spent-before-usd", type=float, default=None,
                         help="what the work cost before this lab (the lab tool's default)")
    replace.add_argument("--marks", default=None, help="append the change's two marks here")
    replace.add_argument("--node-marks", default=None, help="per-node marks go here")
    replace.add_argument("--resume", action="store_true",
                         help="go on after the last step done of a cut-off replace of the node")
    replace.add_argument("--adopt-settings", action="store_true",
                         help="go on with settings that do not reproduce what is deployed")
    replace.add_argument("--gate-timeout-s", type=float, default=30.0)
    replace.add_argument("--drain-timeout-s", type=float, default=30.0)
    replace.add_argument("--ready-timeout-s", type=float, default=60.0)
    replace.add_argument("--surge", choices=("auto", "never"), default="auto")
    replace.add_argument("--parity-ms", type=float, default=1.0)
    replace.set_defaults(handler=command_replace, on_failure="stop", video_gap="refuse")
    for verb, text in (("kill", "a contributor or a first forwarder killed and held down, "
                                "gated (A 2.10)"),
                       ("return", "the node `kill` held down started and made ready")):
        change = sub.add_parser(verb, help=text)
        contributor_changes.add_options(change, verb)
        change.set_defaults(handler=command_change)
    hunt = sub.add_parser("hunt")
    hunt.add_argument("--set", action="append", default=[], metavar="KEY=VALUE",
                      help="a setting of this hunt (relay_class_split, class_port_offset), "
                           "saved as plan/push/restart save theirs")
    hunt.add_argument("--outage", action="store_true",
                      help="hunt while edges serve: every remote region goes silent")
    hunt.set_defaults(handler=command_hunt)
    sub.add_parser("status").set_defaults(handler=command_status)
    collect = sub.add_parser("collect", help="every node's journal, API and samplers, and the "
                                             "lab's files, into the run archive")
    collect.add_argument("--run-id", required=True, help="the lab's run id (its archive run)")
    collect.add_argument("--nodes", default=None, help="comma-separated: only these nodes")
    collect.add_argument("--timeout-s", type=float, default=COLLECT_TIMEOUT_S,
                         help="the bound of the nodes' pulls, together, from collect's start "
                              "(each node is moved into the archive as its pull ends)")
    collect.set_defaults(handler=command_collect)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    args.handler(args)


if __name__ == "__main__":
    main()
