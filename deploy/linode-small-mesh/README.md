# Small-instance Linode mesh lab

This lab runs the complete Needletail audio mesh on the smallest Linode plans.
Every node in the committed topologies is a `g6-nanode-1` (1 shared vCPU,
1 GB). The 27-node global topology costs $0.2025 per hour.

The lab tests three things together:

- the relay DAG on public paths (contributor, two backbone relays, regional
  distributors);
- the regional LL-HLS cache fabric on a private VLAN (two distributors feed
  the playback edges of their region);
- the listener lanes: native UDP with RaptorQ FEC, WebTransport, and LL-HLS
  over HTTP/3, for one listener and for many.

## Topology

```text
London VLAN 10.77.1.0/24                 public Internet                region VLAN 10.77.N.0/24
contributor --(VLAN)--> relay-primary ------------------+--> distributor A --+--> edge 1
     |                                                  |                    +--> edge 2
     +--(public)-----> relay-secondary (Amsterdam) -----+--> distributor B --+--> edge 3
                         repair lane, warm failover                              load host
```

The primary relay is in the contributor's region. The contributor-to-primary
hop uses the private VLAN, so the primary route has no path stretch. The
secondary relay is in a different region for failure-domain diversity.

Each distributor is a relay-DAG leaf with two parents. It packages LL-HLS
locally and pushes cache objects to the edges of its region over the VLAN.
Edges are not DAG members and send no media to another node.

| File | Nodes | Regions |
| --- | ---: | --- |
| `topology-core.json` | 13 | London, New York |
| `topology-global.json` | 27 | London, New York, Tokyo, Sydney |

## Tools

All tools read the Linode token from `LINODE_TOKEN`, `LINODE_PAT`, or the file
`../LINODE_PAT`. They do not print or store it.

```sh
# Create, grow, inspect, and delete the lab.
scripts/linode-small-mesh-lab.py up --topology deploy/linode-small-mesh/topology-core.json
scripts/linode-small-mesh-lab.py watchdog --hours 4
scripts/linode-small-mesh-lab.py grow --topology deploy/linode-small-mesh/topology-global.json
scripts/linode-small-mesh-lab.py replace new-dist-a
scripts/linode-small-mesh-lab.py status
scripts/linode-small-mesh-lab.py down

# Install and tune the services.
scripts/linode-small-mesh-deploy.py push
scripts/linode-small-mesh-deploy.py status
scripts/linode-small-mesh-deploy.py restart --roles distributor,edge --set mesh_sync_interval_ms=1
scripts/linode-small-mesh-deploy.py hunt

# Run one measured publication.
scripts/linode-small-mesh-run.py --name baseline --source lori --duration 60
```

### Cost controls

`up` refuses a topology with more than 40 nodes or a price above $1.00 per
hour. It accepts only `nanode` and `standard` plans. Change the limits with
`NEEDLETAIL_LINODE_MAX_NODES` and `NEEDLETAIL_LINODE_MAX_HOURLY_USD`.

The state file records each instance and firewall identifier. `down` deletes
those resources and every resource that has the run tag. It deletes nothing
else on the account.

`watchdog` starts a detached process. The process deletes the lab at the
deadline, also when the operator session has ended. Run the command again to
move the deadline.

Linode bills instance time by the hour. Outbound transfer above the account
pool costs $0.005 per GB in most regions. Traffic on a VLAN is free.

### Binaries

The deploy tool installs binaries that were built on the operator host:

```sh
target=x86_64-unknown-linux-gnu.2.36
(cd ../av-mesh    && cargo zigbuild --release --locked --target $target --bin av-mesh --bin h3-static-capacity)
(cd ../av-contrib && cargo zigbuild --release --locked --no-default-features --target $target --bin av-contrib --bin aep1-48k-probe)
(cd ../daw-nexus  && cargo zigbuild --release --target $target --bin daw-test-source)
cargo build --release --locked --bin needletail-compile
```

A 1 GB node cannot build the services. The glibc 2.36 target runs on Debian 12
and Debian 13.

### Settings

`push`, `plan`, and `restart` accept `--set key=value`. The tool stores the
values in `target/linode-qualification/small-mesh-artifacts/settings.json`.

| Setting | Tool default | Qualified value | Effect |
| --- | --- | --- | --- |
| `fmp4_part_ms` | 200 | 5 | Contributor LL-HLS part duration. Audio needs 5 ms. |
| `mesh_sync_interval_ms` | 20 | 1 | Distributor-to-edge push tick. |
| `mesh_symbol_size` | 1316 | 900 | Cache-mesh RaptorQ symbol size. |
| `path_loss_ppm` | 20000 | 20000 | Loss seed for the contributor RaptorQ policy. Do not use 0. |
| `extra_distributor_args` | none | `--udp-socket-buffer-bytes 524288` | Shallow relay ingest queues. |
| `response_ms` | none | none | Server limit for aggregated tail responses. |

### Source-port selection

`hunt` measures the one-way delay from each backbone relay to the real ingest
port of each distributor for 40 UDP source ports. Routers select a path from
the address and port 5-tuple, so the source port selects one of several
long-haul paths. The command records the fastest port for each public link in
`ecmp-ports.json`. `restart` then compiles the relay program with those ports.
The command stops each distributor for approximately 16 seconds.

### Shared-CPU check

`deploy status` reports a single-thread speed for each node and marks a node
below 75% of the fleet median. A shared host can run at half speed for several
minutes and report no steal time. Use `lab replace <node>` when a node stays
slow.

## Sources

| `--source` | Publication |
| --- | --- |
| `synthetic` | Probe-generated FLAC. |
| `lori` | Probe-encoded FLAC from one raw S24LE stereo file on the contributor. |
| `daw` | `daw-test-source` with a prepared PCM directory: FLAC and SoundKit Opus for each track. |

Prepare the PCM fixtures from the Lori Asha confirmation mixes with
`scripts/multicloud-qualification/prepare-qualification-pcm.py`. The preparer
does not crop a source, so use a duration of 300 seconds. Copy the directories
to `/var/lib/needletail-test-media/` on the contributor.

## Fault and load options

| Option | Action |
| --- | --- |
| `--loss-relay P` | Drops P% of primary-lane datagrams at each distributor. |
| `--loss-mesh P` | Drops P% of cache-mesh datagrams at each edge. |
| `--failover-at S` | Stops `relay-primary` S seconds into the stream, then restores it. |
| `--readers N` | Starts N concurrent LL-HLS readers for each edge from the load host. |
| `--load-edges K` | Loads only the first K edges of each region. |
| `--late-join S` | Adds a reader that joins S seconds into the stream. |
| `--blackout-ms N` | Drops the complete primary lane at each distributor for N ms. |
| `--verify-opus-crc` | Verifies the SoundKit packet CRC32 of every Opus part on every edge. |

### Opus integrity

The probe check for `soundkit-opus` also requires the encrypted flag. The
qualification source sends Opus packets that are not encrypted, so the probe
counts a mismatch for each part. `--verify-opus-crc` saves each part on the
edge and runs `soundkit-part-check`. The tool verifies the packet CRC32, the
codec, the sample rate, and the frame count of each part, and reports missing
and repeated parts. The run summary then uses the CRC result as the integrity
count and keeps the probe count as `probe_policy_mismatches`.

Build the tool once before `push`:

```sh
(cd deploy/linode-small-mesh/soundkit-part-check && \
  cargo zigbuild --release --target x86_64-unknown-linux-gnu.2.36)
```

### Candidate builds

Set `NEEDLETAIL_AV_MESH_ROOT` to another `av-mesh` checkout to install its
binary, for example a worktree that holds a patch.

Each run writes raw reports and `summary.json` below
`target/linode-qualification/small-mesh-runs/<run id>/`.
