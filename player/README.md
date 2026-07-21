# Needletail Player

`player/` is the product-owned viewer UI hosted by every `av-mesh` playback
edge. It uses a same-origin LL-HLS playlist so the browser never needs a
separate media endpoint or permissive cross-origin configuration.

Build and check the static bundle:

```sh
npm ci --prefix player
npm run check --prefix player
npm run build --prefix player
```

Open `/<unsigned-64-bit-id>` for a stream. Open `/1` for the default stream.
The Player control contains `Native` and `HLS.js`. The initial selection uses
native HLS when the browser reports support. Other browsers use the bundled
HLS.js player. The bundle has no runtime CDN dependency.

The latency slider has a range of 100 ms to 5 seconds. The delay value includes
a one-second rolling average. The timeline shows playback, buffered ranges, and
the live edge.

The UI reports live ingest and edge publication. It does not assume which
contributor application sent the media.

Run the browser smoke check while a live stream is available:

```sh
npm run test:browser --prefix player -- \
  'https://local.infidelity.io:19444/1'
```

Set `CHROME_BINARY` if Google Chrome is not in its default macOS location. The
check forces HLS.js playback. It requires decoded video and the live player
state. Use `npm run test:native-safari` for native HLS conformance.
