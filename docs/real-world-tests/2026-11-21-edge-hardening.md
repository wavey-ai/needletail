# 21 November 2026: edge hardening

This record covers a fault campaign against the regional LL-HLS cache: what
was tested, what broke, what was fixed, and what is still open. The brief was
that the earlier tests of the regional cache were far too gentle. Every
scenario here has a question, a pass criterion, and at the end a pass, a fix,
or a gap that is written down with its evidence.

The ranked list of what a production media edge has and this one lacks is in
[Media edge gaps](../media-edge-gaps.md). The design is in
[Regional LL-HLS edge cache fabric](../edge-cache-fabric.md).

RESULT-SECTION

## How to run it

Everything runs from the `needletail` checkout with `av-mesh`, `av-contrib`
and `playlists` checked out beside it.

    # the lab: 13 nodes in namespaces, each with the limits of a Nanode
    scripts/edge_lab.py up
    scripts/edge_lab.py sources 3600
    scripts/edge_lab.py status
    scripts/edge_lab.py down

    # the campaign
    scripts/edge-campaign.py list
    scripts/edge-campaign.py run --out target/edge-campaign/run-1 --no-slow
    scripts/edge-campaign.py run --out target/edge-campaign/run-1 mesh-loss-2pct ladder
    scripts/edge-campaign.py summary target/edge-campaign/run-1

    # the same scenarios on real instances
    scripts/linode-small-mesh-lab.py up --topology deploy/linode-small-mesh/topology-regional-cache.json
    scripts/linode-small-mesh-lab.py watchdog --hours 3
    scripts/linode-small-mesh-deploy.py push $(scripts/linode-edge-campaign.py settings)
    scripts/linode-edge-campaign.py prepare
    scripts/linode-edge-campaign.py run
    scripts/linode-small-mesh-lab.py down

    # one playlist against the LL-HLS rules
    scripts/hls_spec.py lab lon-edge-1 1 --seconds 20

    # a real player (see scripts/players/FINDINGS.md)
    scripts/edge_lab.py sh lon-readers node scripts/players/hlsjs-play.mjs --url <master> --seconds 40

    # one versioned evidence file from a run
    scripts/edge-campaign-evidence.py --run-id ID --provider local --out FILE DIR

Each scenario writes `<out>/<scenario>/evidence.json`: the question, the
criterion, the conditions, the events with their times, every check with its
numbers, what each group of readers saw, and the CPU, memory, descriptors and
threads of every node.

## The lab

The local lab is one host. Each node is a network namespace with `eth0` on
the public network of its site and `eth1` on the private network of its
region, as a Linode has. The sites meet in a router namespace over links with
the delay of their distance (London 2 ms, Amsterdam 4 ms, New York 34 ms to
the router). Every link takes `tc netem` in each direction. Each service runs
in its own cgroup with one CPU, 1 GB and no swap, and with one runtime worker
thread, as on a Nanode. Nothing needs root.

Nodes: one contributor and one relay in London, one relay in Amsterdam, and
two distributors with four edges in London and two distributors with two
edges in New York. The sources are a 4K H.264 clip at 16.5 Mbit/s with AAC
(`tos24-16m.ts`) and one audio track as lossless FLAC and as HQ SoundKit Opus,
both in 5 ms parts. The ladder scenarios add 1080p, 720p and 360p encodes of
the clip with the same closed two-second group of pictures. The long-tail
scenarios use an album of eight tracks, which makes 17 streams.

An edge is set to 100 Mbit/s of egress, so that admission is reached with a
few 4K readers. The capacity scenarios lift that limit to find the ceiling of
the node itself.

A reader is `needletail-reader`, a player model: it asks for parts ahead,
keeps a 1 s buffer, counts a stall when the buffer runs dry, follows the
steering manifest, and reports the delay from publication to arrival of every
part. Real players are hls.js 1.7.4 in headless Chrome for Testing 158 and
ffmpeg 9.0.2.

The Linode lab is `deploy/linode-small-mesh/topology-regional-cache.json`: 13
Nanodes (1 vCPU, 1 GB) in London, Amsterdam and Newark with the same roles, a
4-vCPU load host in London and a Nanode load host in Newark. Readers reach the
caches at their public addresses. The cache network is the private VLAN of
each region. Faults are injected on the instances with `systemctl` and `tc`.

METHOD-NOTES

FINDINGS-SECTION

TABLES-SECTION
