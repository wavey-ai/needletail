# 18 July 2026: isolated HTTP/3 capacity and first fixed bottleneck

## Result

The isolated production `web_service::H2H3Server` test has found and fixed one
real HTTP/3 capacity bug. Ordinary media GET responses were constructing and
QPACK-encoding `Access-Control-Allow-Methods` and
`Access-Control-Allow-Headers`, fields needed on CORS preflight responses rather
than every media response. The method-aware fix keeps those permissions on
`OPTIONS` and removes them from ordinary GET responses.

On the same two-vCPU GCP server, the fix raised the saturated 64-byte H3 rate
from a two-run mean of 71,946 to 79,702 responses/s, a `10.78%` improvement.
The full 5,760-byte PCM-shaped path also used about `2.4%` less server CPU at 40
customers. This is the first production capacity fix from the isolation work;
the backend experiment and PMTU change did not improve capacity.

Component revisions:

- `web-services` `909f584d1f689396e1a8423eaa4ac6f41d418808`: method-aware H3 CORS response fields;
- `av-mesh` `60cf9aa`: capacity runner pinned to that canonical `web-service` Git revision;
- the unchanged Quinn load generator and reversal server used the pre-fix `h3-static-capacity-798189a` binary.

The relevant `web-services` discussion is in the
[HTTP/3 capacity investigation](https://github.com/wavey-ai/web-services/blob/main/docs/http3-capacity-investigation.md).
Raw, intentionally unversioned artifacts are under:

```text
target/gcp-qualification/h3-backend-ab/20260718T053138Z
target/gcp-qualification/h3-cors-capacity-fix/20260718T060810Z
```

## Test topology

The pure-Rust Quinn client ran in London and the production H3 server ran in
Amsterdam. Both were GCP `n2-standard-2` instances with two vCPUs. TLS 1.3 and
H3 ALPN verification were mandatory, connections were persistent, and the
client and server were on separate machines. The reference path RTT was about
13 ms.

Two response shapes were kept separate:

- 64 bytes, unpaced, to expose request/QPACK/allocation capacity without a
  bandwidth ceiling;
- 5,760 bytes, paced, representing one five-millisecond 48 kHz S24 PCM part for
  eight channels.

The paced 16-channel customer model uses two eight-channel connections and 400
part responses/s/customer.

## Controlled old/fixed/old reversal

The tiny-response test used 16 connections, pipeline depth 64, and ten-second
steps. The server was saturated at about 195% CPU in all four comparison runs.

| Build | Responses/s | Wire Mbit/s | p99 ms | Server CPU ticks |
| --- | ---: | ---: | ---: | ---: |
| pre-fix, reversal 1 | 71,878 | 146.63 | 18.44 | 1,958 |
| pre-fix, reversal 2 | 72,014 | 147.09 | 18.28 | 1,960 |
| fixed, repeat 1 | 79,751 | 142.40 | 16.10 | 1,951 |
| fixed, repeat 2 | 79,653 | 142.35 | 16.06 | 1,947 |

All runs completed with zero request errors. Restarting the pre-fix binary
immediately after the fixed runs returned the server to the old ceiling. The
means therefore show:

- `+10.78%` responses/s;
- `+11.35%` responses per server CPU tick;
- `-12.49%` wire traffic per response; and
- p99 improving from about 18.4 to 16.1 ms.

Before the fix, a 499 Hz profile attributed `7.13%` flat CPU to stateless QPACK
string encoding. After the fix that fell to `4.55%`. Allocation, QPACK decode,
and response-buffer copies remain visible and are the next profile targets.

## PCM-shaped media control

At 40 customer equivalents, 80 persistent connections each requested 200
5,760-byte responses/s. Both fixed-build repeats completed exactly 160,000 of
160,000 scheduled requests with zero error and zero backpressure.

| Build | Exact repeats | Mean server CPU | Mean wire Mbit/s | Mean p99 ms |
| --- | ---: | ---: | ---: | ---: |
| pre-fix Quinn | 2/2 | 150.8% | 790.8 | 12.95 |
| fixed Quinn | 2/2 | 147.2% | 786.6 | 13.20 |

The response packet count was unchanged, as expected for the same body and
path MTU. The CPU and byte reductions are real headroom, not a packet-count
change. The small p99 difference is not treated as significant with two short
runs.

At 56 customer equivalents the fixed build completed 223,896 of 224,000
requests (`99.9536%`) with zero request errors, but it recorded ten scheduling
backpressure events and missed 104 requests. Server CPU was about 183%. The
strict runner correctly marks this as a failure; client saturation still
prevents assigning a 56-customer server capacity.

## Backend control

The same Quinn client tested both server backends at 40 customers. Each backend
completed two exact runs.

| Backend | Mean responses/s | Mean server CPU | Mean server packets | Mean p99 ms |
| --- | ---: | ---: | ---: | ---: |
| Quinn | 15,984 | 150.6% | 916,819 | 12.95 |
| tokio-quiche | 15,982 | 159.5% | 961,029 | 12.80 |

Tokio-quiche consumed about `5.9%` more server CPU and emitted about `4.8%`
more packets. A single 48-customer run was also exact on both implementations,
with about 179.8% Quinn CPU and 189.9% tokio-quiche CPU. Raising tokio-quiche's
configured maximum UDP payload from 1,350 to 1,400 bytes made no measurable
difference. This experiment rules out the current Quinn path as the sole cause
of the capacity gap and does not justify switching the production default.

## Connection count is not request or media capacity

The configured 256 bidirectional H3 streams are a per-connection concurrency
limit, not a global client limit. The fixed Quinn server completed exact
three-second steps with 100, 500, and 1,000 simultaneous persistent
connections, one 5,760-byte request/s/connection, with zero errors or
backpressure.

The generator's soft file-descriptor limit was raised because the diagnostic
client currently creates one UDP endpoint per generated connection. The server
uses a shared UDP socket. This result disproves a 256-global-connection ceiling,
but it is not evidence for millions of simultaneous connections. That claim
requires a shared-endpoint generator, progressively larger idle and blocked-
reload populations, and server memory/task measurements.

## What is fixed and what remains

Fixed and proven:

- strict result accounting requires exact requests, zero errors, and zero
  generator backpressure;
- ordinary H3 responses no longer repeat preflight-only CORS fields;
- Quinn and tokio-quiche implement and test the same method-aware behavior;
- the production server has demonstrated 1,000 simultaneous persistent H3
  connections at low request rate.

Measured but not improved:

- tokio-quiche is currently slower than Quinn for this workload;
- the tested PMTU ceiling change did not alter packetization or capacity;
- the virtual NIC performs UDP segmentation in software on both tested cloud
  providers.

Next gates before resuming the six-node mesh endurance run:

1. share one client UDP endpoint across many generated connections and qualify
   connection count independently from requests/s;
2. add RSS/task/cancellation measurements for idle connections and blocked
   playlist reloads;
3. remove and A/B the duplicate path allocation and full request-header clone;
4. complete seeded-edge, range, disconnect, slow-reader, and flow-control
   correctness tests; and
5. rerun the full-media boundary with generator headroom before declaring a
   customer capacity above the current exact tier.
