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

Subcommands:
  plan      measure routes, generate the relay program, compile it, and render
            one argument file per service under the artifact directory
  push      bootstrap hosts, install binaries, TLS, the host qdisc and
            services, then restart (--nodes a,b: only those nodes)
  restart   re-render arguments and the host qdisc from current settings and
            restart services
  start     --nodes a,b: take held-out nodes (standby edges, autoscale
            slots) into service and start them, and nothing else
  stop      --nodes a,b: hold nodes out and stop their service, and nothing
            else
  status    show service state, clock offset, and cache-mesh role per node

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
import functools
import json
import os
import pathlib
import re
import shlex
import subprocess
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
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
    "slot_kb": 2048,
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
DAW_MEDIA_PORT = 27100
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


def load_settings(overrides: list[str]) -> dict:
    saved = json.loads(SETTINGS_FILE.read_text()) if SETTINGS_FILE.is_file() else {}
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
    common.ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    SETTINGS_FILE.write_text(json.dumps(settings, indent=2, sort_keys=True) + "\n")
    return settings


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
    if node["role"] == "contributor":
        return "contrib"
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
    - "unused": the backbone relays that the topology does not use.
    Exits when the state cannot carry the topology."""
    mode = topology_mode(settings)
    contributors = common.nodes_by_role(state, "contributor")
    if len(contributors) != 1:
        sys.exit("the lab needs exactly one contributor")
    contrib = contributors[0]
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
        return {"mode": mode, "contrib": contrib, "first": [primary, secondary],
                "children": children, "unused": []}
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
    return {"mode": mode, "contrib": contrib, "first": first, "children": children,
            "unused": common.nodes_by_role(state, "backbone")}


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
    briefly to free its ingest ports.
    """
    settings = load_settings(getattr(args, "set", None) or [])
    state = common.load_state()
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
    HUNT_FILE.write_text(json.dumps(hunted, indent=2, sort_keys=True) + "\n")
    for key in sorted(hunted):
        item = hunted[key]
        classes = ("" if item.get("video_one_way_ms") is None else
                   f" (audio {item['audio_one_way_ms']:.2f}, video {item['video_one_way_ms']:.2f})")
        print(f"{key:<34} port {item['port']}  best {item['one_way_ms']:.2f} ms{classes}  "
              f"median {item['median_ms']:.2f}  worst {item['worst_ms']:.2f}  "
              f"({item['candidates']} ports)")
    print("run `restart` to apply the selected source ports")


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
    peer_role = "edge" if node["role"] == "distributor" else "distributor"
    # The roster: edges and distributors held out, and autoscale slots not
    # created yet, are peers that are down until they start.
    peers = [peer for peer in sorted(roster(state).values(), key=lambda item: item["name"])
             if peer["site"] == node["site"] and peer["role"] == peer_role
             and peer.get("vlan_ipv4")]
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
    audio hot path arguments of the contributor on `node`."""
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
        "--wall-clock-estimated-error-ms", "5",
        "--rist-bind", "0.0.0.0:27000", "--rtmp-bind", "0.0.0.0:19350",
        "--relay-local-id", "contrib",
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
    if "--client-udp-bind" in node_args:
        for key in WAVE8_CLIENT_UDP:
            if settings.get(key) is not None:
                out += ["--" + key.replace("_", "-"), flag(bool(settings[key]))]
    return out


# Wave 8 stage 2's settings of the nodes that serve UDP clients: each is the
# av-mesh flag of the same name.
WAVE8_CLIENT_UDP = ("client_udp_repair_thread", "client_udp_batched_admission",
                    "client_udp_resume_from_request", "client_udp_fanout_inline_shard")


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
            cpus = relay_class_cpus(node, plan)
            if cpus:
                out += ["--relay-class-cpus", cpus]
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
}


def check_cache_mesh_peers(state: dict, rendered: dict[str, dict]) -> None:
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
    site's nodes of the other cache-mesh role, cache_mesh_args)."""
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
                if down["site"] != node["site"] or down["role"] == node["role"]:
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
    shape: the contributor's two lanes end at two distributors of its own
    site and it has no direct lane (--relay-direct-target) to anything; no
    backbone relay runs; every other distributor takes its two parents
    among those first forwarders. An edge takes relay parents only on the
    fast plane, from the distributors of its own site (check_fast_plane
    checks those). An extra argument of a role can break it."""
    problems = []
    contrib = common.nodes_by_role(state, "contributor")[0]
    by_id = {relay_node_id(node): node for node in state["nodes"].values()}
    contrib_args = rendered[contrib["name"]]["args"]
    if "--relay-direct-target" in flag_names(contrib_args):
        problems.append("the contributor has a --relay-direct-target")
    first = [flag_value(contrib_args, "--relay-primary-id"),
             flag_value(contrib_args, "--relay-secondary-id")]
    for node_id in first:
        node = by_id.get(node_id or "")
        if not node or node["role"] != "distributor" or node["site"] != contrib["site"]:
            problems.append(f"the contributor feeds {node_id}, not a distributor of "
                            f"its site {contrib['site']}")
    if len(set(first)) != 2:
        problems.append(f"the contributor's two lanes end at {first}")
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


def direct_region_args(state: dict, services: dict) -> tuple[list[str], dict[str, list[str]]]:
    """contributor_direct_region: the contributor's --relay-direct-* arguments
    for the distributors of its site, and for each of those distributors the
    arguments that register the contributor's direct source endpoint as a
    parent. A direct target is the port on which the distributor takes its
    primary backbone parent; the symbols are the same."""
    contrib = common.nodes_by_role(state, "contributor")[0]
    mates = [node for node in common.nodes_by_role(state, "distributor")
             if node["site"] == contrib["site"]]
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
    """--canonical-gap-hold-ms for a distributor outside the contributor's
    region (canonical_gap_hold_long_haul_ms); nothing for any other node."""
    hold = settings.get("canonical_gap_hold_long_haul_ms")
    if not hold or node["role"] != "distributor":
        return []
    contributors = common.nodes_by_role(state, "contributor")
    if not contributors or contributors[0]["region"] == node["region"]:
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


def render(state: dict, settings: dict, plan: dict) -> dict[str, dict]:
    """Return {node name: {service, args, env}} for every service node."""
    services = {
        (item["service"], item["node_id"]): item for item in plan["services"]
    }
    check_settings(settings)
    classes = class_plan(settings)
    feed = fast_plane_feed(state, classes)
    direct_contrib, direct_mesh = (direct_region_args(state, services)
                                   if settings.get("contributor_direct_region") else ([], {}))
    rendered: dict[str, dict] = {}
    for node in state["nodes"].values():
        role = node["role"]
        if role == "contributor":
            args = contributor_args(services[("av_contrib", "contrib")], settings, node)
            args += direct_contrib
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
            # backbone at the end of that lane admits it.
            if (classes["relay_repair_both_lanes"]
                    and service["primary_parent"]["parent_node_id"] == "contrib"
                    and "--relay-primary-promoted" not in flag_names(
                        args + list(settings[f"extra_{role}_args"]))):
                args.append("--relay-primary-promoted")
            args += direct_mesh.get(node["name"], [])
        if slot_forwarder:
            # dist-c: a cache-mesh parent of its site's edges, fed as a
            # fast-plane lease child of its site's first two distributors;
            # it feeds no lease child of its own (edges keep their fixed
            # fast-plane parents until av-mesh ranks them among the live
            # distributors).
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
                args += yield_to(extra, fast_forwarder_args(None, settings))
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
        args += yield_to(extra, wave7_args(args, settings)) + extra
        rendered[node["name"]] = {
            "service": "needletail-mesh", "args": args,
            "rust_log": settings["rust_log_mesh"]}
    check_class_agreement(rendered)
    check_cache_mesh_peers(state, rendered)
    check_class_ports(rendered, classes)
    check_fast_plane(state, rendered)
    if topology_mode(settings) == "merged":
        check_merged(state, rendered)
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
    return f"""[Unit]
Description={description} (small-mesh lab)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=needletail
Group=needletail
Environment=RUST_LOG={rust_log}
{thread_line}ExecStart=/usr/local/bin/needletail-run-args /etc/needletail/{service}.args
Restart=always
RestartSec=2
NoNewPrivileges=yes
PrivateDevices=yes
ProtectHome=yes
LimitNOFILE=1048576

[Install]
WantedBy=multi-user.target
"""


def binaries_for(role: str, node: dict | None = None) -> list[pathlib.Path]:
    target = "target/x86_64-unknown-linux-gnu/release"
    # NEEDLETAIL_AV_MESH_ROOT selects another av-mesh checkout, such as a worktree
    # that carries a candidate patch.
    mesh = pathlib.Path(os.environ.get(
        "NEEDLETAIL_AV_MESH_ROOT", str(common.WORKSPACE / "av-mesh"))) / target
    # NEEDLETAIL_AV_CONTRIB_ROOT selects another av-contrib checkout.
    contrib = pathlib.Path(os.environ.get(
        "NEEDLETAIL_AV_CONTRIB_ROOT", str(common.WORKSPACE / "av-contrib"))) / target
    daw = common.WORKSPACE / "daw-nexus" / target
    probe = contrib / "aep1-48k-probe"
    if role == "contributor":
        return [contrib / "av-contrib", probe, daw / "daw-test-source"]
    if role == "load":
        tools = [probe, mesh / "needletail-reader", mesh / "needletail-udp-client",
                 common.WORKSPACE / "daw-nexus" / target / "daw-test-source",
                 common.WORKSPACE / "av-mesh" / target / "h3-static-capacity",
                 common.ROOT / "deploy" / "linode-small-mesh" / "soundkit-part-check"
                 / target / "soundkit-part-check"]
        if node is not None and common.load_purpose(node) == "listeners":
            # The simulated listeners of the scale run: needletail-load (UDP,
            # from av-mesh) and ntcmp (LL-HLS crowds, scripts/compare/gobuild.sh).
            tools += [mesh / "needletail-load", common.ROOT / "target" / "ntcmp"]
        return tools
    checker = (common.ROOT / "deploy" / "linode-small-mesh" / "soundkit-part-check"
               / target / "soundkit-part-check")
    return [mesh / "av-mesh", probe, checker]


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


def install_node(node: dict, rendered: dict, settings: dict, binaries: bool) -> str:
    if binaries:
        sources = [str(path) for path in binaries_for(node["role"], node)]
        for source in sources:
            if not pathlib.Path(source).is_file():
                raise RuntimeError(f"binary is missing: {source}")
        common.rsync_to(node, sources, "/usr/local/bin/")
    tls = common.ARTIFACT_DIR / "tls"
    common.rsync_to(node, [str(tls / "fullchain.pem"), str(tls / "privkey.pem")],
                    "/etc/needletail/tls/")
    common.rsync_to(node, [str(tls / "ca.crt")],
                    "/usr/local/share/ca-certificates/needletail-qualification.crt")
    commands = [
        "chown needletail:needletail /etc/needletail/tls/*.pem",
        "chmod 644 /etc/needletail/tls/fullchain.pem "
        "/usr/local/share/ca-certificates/needletail-qualification.crt",
        "chmod 600 /etc/needletail/tls/privkey.pem",
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
    item = rendered.get(node["name"])
    if not item and node["role"] in ("backbone", "edge"):
        # The merged topology uses no backbone relay: one left running from
        # a legacy deploy would still send to the distributors. A standby
        # edge (standby_edges) is out of the mesh until it is restarted in.
        commands.append("systemctl disable --now needletail-mesh.service >/dev/null 2>&1 || true")
    if item:
        apply_host_qdisc(node, settings)
        service = item["service"]
        args_file = common.ARTIFACT_DIR / "nodes" / f"{node['name']}.args"
        common.rsync_to(node, [str(args_file)], f"/etc/needletail/{service}.args")
        unit = unit_text(service, item["rust_log"], settings)
        commands += [
            f"chmod 644 /etc/needletail/{service}.args",
            f"cat >/etc/systemd/system/{service}.service <<'UNIT'\n{unit}UNIT",
            "systemctl daemon-reload",
            f"systemctl enable {service}.service >/dev/null 2>&1",
            f"systemctl restart {service}.service",
            "sleep 2",
            f"systemctl is-active {service}.service",
        ]
    return common.ssh_text(node, "set -e\n" + "\n".join(commands))


def command_plan(args: argparse.Namespace) -> tuple[dict, dict, dict]:
    """Plan and render the services of every node but the standby edges;
    returns the whole lab state (push installs the tools on every node and
    stops the service of a standby edge), the settings and the render."""
    full = common.load_state()
    settings = load_settings(args.set)
    state = active_state(full, settings)
    routes_path = common.ARTIFACT_DIR / "routes.json"
    routes = json.loads(routes_path.read_text()) if routes_path.is_file() else None
    # Routes measured over the other topology's relays seed the controller
    # with the wrong paths (a file without "topology" is legacy).
    if (args.remeasure or routes is None
            or routes.get("topology", "legacy") != topology_mode(settings)):
        routes = measure_routes(state, settings)
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
    state = common.load_state()
    only = named_nodes(state, getattr(args, "nodes", None))
    if only is not None:
        # A node pushed by name is taken into service (it may have been
        # held out); its peers already name it, so nothing else restarts.
        saved = load_settings(list(args.set))
        args.set = list(args.set) + [
            f"standby_edges={json.dumps(held_out_after(saved, start=only))}"]
    nodes = [node for node in state["nodes"].values() if only is None or node["name"] in only]
    if only is not None and (args.remeasure
                             or not (common.ARTIFACT_DIR / "routes.json").is_file()):
        sys.exit("push --nodes keeps the routes of the last whole push (measuring them "
                 "would reach other nodes): push the whole lab first, and no --remeasure")
    ensure_tls()
    if not args.skip_bootstrap:
        interfaces = common.parallel(
            nodes, lambda node: common.ssh_text(node, BOOTSTRAP, timeout=900))
        for node in nodes:
            expected = node.get("vlan_ipv4")
            if expected and f"={expected}/" not in interfaces[node["name"]]:
                raise RuntimeError(
                    f"{node['name']}: VLAN address {expected} is not configured: "
                    f"{interfaces[node['name']]}")
        print(f"bootstrapped {len(nodes)} hosts; every VLAN address is present")
    args.remeasure = args.remeasure or not (common.ARTIFACT_DIR / "routes.json").is_file()
    state, settings, rendered = command_plan(args)
    # Parents first so children find a listening relay when they start.
    order = ["backbone", "distributor", "edge", "contributor", "load"]
    for role in order:
        group = [node for node in common.nodes_by_role(state, role)
                 if only is None or node["name"] in only]
        if group:
            common.parallel(
                group,
                lambda node: install_node(node, rendered, settings, not args.skip_binaries))
            print(f"installed {len(group)} {role} node(s)")


def command_restart(args: argparse.Namespace) -> None:
    args.remeasure = getattr(args, "remeasure", False)
    state, settings, rendered = command_plan(args)
    roles = args.roles.split(",") if args.roles else [
        "backbone", "distributor", "edge", "contributor"]
    for role in ["backbone", "distributor", "edge", "contributor"]:
        if role not in roles:
            continue
        group = common.nodes_by_role(state, role)
        if group:
            common.parallel(
                group, lambda node: install_node(node, rendered, settings, False))
            print(f"restarted {len(group)} {role} node(s)")


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
    """Take held-out nodes into service and start them: render every node
    (their peers name them already, so no other node's arguments change)
    and install and start the named ones only. A node just created (lab
    add) needs `push --nodes`, which also bootstraps it and installs the
    binaries."""
    full = common.load_state()
    names = named_nodes(full, args.nodes)
    checked_for_start_stop(full, names, "start")
    saved = load_settings(list(args.set))
    args.set = list(args.set) + [
        f"standby_edges={json.dumps(held_out_after(saved, start=names))}"]
    args.remeasure = False
    state, settings, rendered = command_plan(args)
    group = [state["nodes"][name] for name in names]
    missing = [node["name"] for node in group if node["name"] not in rendered]
    if missing:
        sys.exit(f"start: no service rendered for {', '.join(missing)}")
    common.parallel(group, lambda node: install_node(node, rendered, settings, False))
    print(f"started {', '.join(names)}; no other node was restarted")


def command_stop(args: argparse.Namespace) -> None:
    """Hold nodes out of service and stop their service, and nothing else.
    Their peers keep naming them (a peer that is down)."""
    full = common.load_state()
    names = named_nodes(full, args.nodes)
    checked_for_start_stop(full, names, "stop")
    saved = load_settings(list(args.set))
    load_settings(list(args.set) + [
        f"standby_edges={json.dumps(held_out_after(saved, stop=names))}"])
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
    speeds = {}
    for name, line in results.items():
        if "cpu_mb_s=" in line:
            speeds[name] = float(line.rsplit("cpu_mb_s=", 1)[1] or 0)
    median = sorted(speeds.values())[len(speeds) // 2] if speeds else 0
    slow = sorted(name for name, speed in speeds.items() if speed < 0.75 * median)
    for name in sorted(results):
        node = state["nodes"][name]
        flag = "  <-- SLOW shared CPU" if name in slow else ""
        print(f"{name:<18} {node['role']:<12} {results[name]}{flag}")
    # Shared-CPU hosts vary by 2x in single-thread speed without reporting steal.
    print(f"median single-thread speed {median:.0f} MB/s; slow nodes "
          f"(<75% of median): {' '.join(slow) or 'none'}")


def main() -> None:
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
                           "into service); every other node is left as it runs")
    push.set_defaults(handler=command_push)
    start = sub.add_parser("start", help="take held-out nodes into service, nothing else")
    start.add_argument("--nodes", required=True)
    start.add_argument("--set", action="append", default=[])
    start.set_defaults(handler=command_start)
    stop = sub.add_parser("stop", help="hold nodes out and stop them, nothing else")
    stop.add_argument("--nodes", required=True)
    stop.add_argument("--set", action="append", default=[])
    stop.set_defaults(handler=command_stop)
    restart = sub.add_parser("restart")
    restart.add_argument("--set", action="append", default=[])
    restart.add_argument("--roles", default="")
    restart.set_defaults(handler=command_restart)
    hunt = sub.add_parser("hunt")
    hunt.add_argument("--set", action="append", default=[], metavar="KEY=VALUE",
                      help="a setting of this hunt (relay_class_split, class_port_offset), "
                           "saved as plan/push/restart save theirs")
    hunt.set_defaults(handler=command_hunt)
    sub.add_parser("status").set_defaults(handler=command_status)
    args = parser.parse_args()
    args.handler(args)


if __name__ == "__main__":
    main()
