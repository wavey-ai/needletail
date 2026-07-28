#!/usr/bin/env node

import { mkdir, readFile, writeFile } from "node:fs/promises";
import path from "node:path";

const [runDirectory, outputDirectory] = process.argv.slice(2);
if (!runDirectory || !outputDirectory) {
  console.error("usage: render-global-run-charts.mjs RUN_DIRECTORY OUTPUT_DIRECTORY");
  process.exit(2);
}

const seriesPath = path.join(runDirectory, "edge-latency-time-series.json");
const topologyPath = path.join(
  runDirectory,
  "map-data",
  "after",
  "relay-program.json",
);
const seriesDocument = JSON.parse(await readFile(seriesPath, "utf8"));
const topologyDocument = JSON.parse(await readFile(topologyPath, "utf8"));

await mkdir(outputDirectory, { recursive: true });

const escapeXml = (value) =>
  String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");

const normalizeSvg = (value) =>
  `${value.replace(/[ \t]+$/gm, "").trimEnd()}\n`;

const median = (values) => {
  const sorted = values.filter(Number.isFinite).sort((left, right) => left - right);
  if (sorted.length === 0) return null;
  const middle = Math.floor(sorted.length / 2);
  return sorted.length % 2
    ? sorted[middle]
    : (sorted[middle - 1] + sorted[middle]) / 2;
};

const quantile = (values, fraction) => {
  const sorted = values.filter(Number.isFinite).sort((left, right) => left - right);
  if (sorted.length === 0) return null;
  return sorted[Math.min(sorted.length - 1, Math.floor(sorted.length * fraction))];
};

function aggregateLatency(transport, node) {
  const matching = seriesDocument.series.filter(
    (entry) =>
      entry.node === node &&
      entry.transport === transport &&
      entry.format === "flac",
  );
  const secondBuckets = Array.from({ length: 600 }, (_, second) => {
    const values = matching
      .map((entry) => entry.points[second]?.p99_ms)
      .filter(Number.isFinite);
    return median(values);
  });
  return Array.from({ length: 60 }, (_, bucket) => {
    const values = secondBuckets.slice(bucket * 10, bucket * 10 + 10);
    return {
      second: bucket * 10 + 5,
      value: median(values),
    };
  });
}

function pathForPoints(points, xScale, yScale) {
  let drawing = "";
  let open = false;
  for (const point of points) {
    if (!Number.isFinite(point.value)) {
      open = false;
      continue;
    }
    drawing += `${open ? "L" : "M"}${xScale(point.second).toFixed(1)},${yScale(
      point.value,
    ).toFixed(1)}`;
    open = true;
  }
  return drawing;
}

function renderLatencyChart() {
  const nodes = [
    ["edge-tokyo", "Tokyo"],
    ["edge-australia", "Azure Australia"],
    ["edge-sydney", "Sydney"],
    ["edge-japan", "Azure Japan"],
    ["edge-london", "London"],
  ];
  const width = 1200;
  const height = 900;
  const left = 94;
  const right = 1160;
  const top = 112;
  const panelHeight = 132;
  const panelGap = 18;
  const yMinimum = 100;
  const yMaximum = 650;
  const xScale = (second) => left + (second / 600) * (right - left);
  const elements = [];

  elements.push(`
    <rect width="${width}" height="${height}" fill="#08111f"/>
    <text x="${left}" y="44" fill="#f7fbff" font-size="28" font-family="Inter,Arial,sans-serif" font-weight="600">Global FLAC latency over time</text>
    <text x="${left}" y="73" fill="#9fb1c7" font-size="16" font-family="Inter,Arial,sans-serif">Eight stereo tracks · 600 seconds · each point is a 10-second median of per-track, one-second P99 values</text>
    <line x1="${left}" y1="92" x2="${left + 34}" y2="92" stroke="#55c2ff" stroke-width="4"/>
    <text x="${left + 44}" y="97" fill="#d8e3ef" font-size="15" font-family="Inter,Arial,sans-serif">UDP with FEC</text>
    <line x1="${left + 190}" y1="92" x2="${left + 224}" y2="92" stroke="#ffb454" stroke-width="4"/>
    <text x="${left + 234}" y="97" fill="#d8e3ef" font-size="15" font-family="Inter,Arial,sans-serif">FLAC LL-HLS availability</text>
  `);

  nodes.forEach(([node, label], index) => {
    const panelTop = top + index * (panelHeight + panelGap);
    const panelBottom = panelTop + panelHeight;
    const yScale = (value) =>
      panelBottom -
      ((value - yMinimum) / (yMaximum - yMinimum)) * panelHeight;
    const udp = aggregateLatency("udp_fec", node);
    const hls = aggregateLatency("ll_hls", node);
    const udpValues = udp.map((point) => point.value);
    const hlsValues = hls.map((point) => point.value);
    const udpP99 = quantile(udpValues, 0.99);
    const hlsP99 = quantile(hlsValues, 0.99);

    elements.push(`
      <rect x="${left}" y="${panelTop}" width="${right - left}" height="${panelHeight}" rx="8" fill="#0d1a2b" stroke="#1d3047"/>
      <text x="${left + 12}" y="${panelTop + 24}" fill="#f7fbff" font-size="16" font-family="Inter,Arial,sans-serif" font-weight="600">${escapeXml(label)}</text>
      <text x="${right - 12}" y="${panelTop + 24}" text-anchor="end" fill="#9fb1c7" font-size="13" font-family="Inter,Arial,sans-serif">high bucket: UDP ${udpP99.toFixed(0)} ms · LL-HLS ${hlsP99.toFixed(0)} ms</text>
    `);
    for (const tick of [200, 400, 600]) {
      const y = yScale(tick);
      elements.push(`
        <line x1="${left}" y1="${y}" x2="${right}" y2="${y}" stroke="#20334a" stroke-width="1"/>
        <text x="${left - 10}" y="${y + 5}" text-anchor="end" fill="#71869e" font-size="12" font-family="Inter,Arial,sans-serif">${tick}</text>
      `);
    }
    for (const tick of [0, 120, 240, 360, 480, 600]) {
      const x = xScale(tick);
      elements.push(
        `<line x1="${x}" y1="${panelTop}" x2="${x}" y2="${panelBottom}" stroke="#15263a" stroke-width="1"/>`,
      );
      if (index === nodes.length - 1) {
        elements.push(
          `<text x="${x}" y="${panelBottom + 24}" text-anchor="middle" fill="#9fb1c7" font-size="12" font-family="Inter,Arial,sans-serif">${tick}s</text>`,
        );
      }
    }
    elements.push(`
      <path d="${pathForPoints(hls, xScale, yScale)}" fill="none" stroke="#ffb454" stroke-width="2.5" stroke-linejoin="round" stroke-linecap="round"/>
      <path d="${pathForPoints(udp, xScale, yScale)}" fill="none" stroke="#55c2ff" stroke-width="2.5" stroke-linejoin="round" stroke-linecap="round"/>
    `);
    if (node === "edge-tokyo") {
      const x = xScale(385);
      elements.push(`
        <line x1="${x}" y1="${panelTop + 34}" x2="${x}" y2="${panelBottom}" stroke="#ff667a" stroke-width="2" stroke-dasharray="5 5"/>
        <circle cx="${x}" cy="${panelBottom - 8}" r="5" fill="#ff667a"/>
        <text x="${x + 8}" y="${panelBottom - 10}" fill="#ffb8c2" font-size="12" font-family="Inter,Arial,sans-serif">one unrecovered 5 ms UDP unit</text>
      `);
    }
  });

  elements.push(`
    <text x="22" y="${height / 2}" transform="rotate(-90 22 ${height / 2})" text-anchor="middle" fill="#9fb1c7" font-size="14" font-family="Inter,Arial,sans-serif">capture-to-delivery latency (ms)</text>
    <text x="${right}" y="${height - 20}" text-anchor="end" fill="#71869e" font-size="12" font-family="Inter,Arial,sans-serif">Run 20260728T113000Z · loss markers are not interpolated</text>
  `);

  return `<svg xmlns="http://www.w3.org/2000/svg" role="img" aria-labelledby="title description" viewBox="0 0 ${width} ${height}">
    <title id="title">Global FLAC UDP and LL-HLS latency over time</title>
    <desc id="description">Five aligned regional plots compare UDP with forward error correction against FLAC LL-HLS availability during one 600-second, eight-track test.</desc>
    ${elements.join("\n")}
  </svg>
  `;
}

const nodeLocations = {
  "contrib-london": [-0.1278, 51.5074],
  "relay-primary-amsterdam": [4.9041, 52.3676],
  "relay-secondary-japan": [139.6503, 35.6762],
  "relay-regional-osaka": [135.5023, 34.6937],
  "relay-regional-australia": [151.2093, -33.8688],
  "edge-london": [-0.1278, 51.5074],
  "edge-tokyo": [139.6503, 35.6762],
  "edge-sydney": [151.2093, -33.8688],
  "edge-australia": [151.2093, -33.8688],
  "edge-japan": [139.6503, 35.6762],
};

const visualOffsets = {
  "contrib-london": [-18, -22],
  "edge-london": [-12, 23],
  "relay-primary-amsterdam": [36, -2],
  "relay-secondary-japan": [-44, -30],
  "relay-regional-osaka": [-48, 8],
  "edge-tokyo": [2, -5],
  "edge-japan": [42, 20],
  "relay-regional-australia": [-50, -28],
  "edge-sydney": [-28, 15],
  "edge-australia": [34, 24],
};

const labels = {
  "contrib-london": "London origin",
  "relay-primary-amsterdam": "Amsterdam backbone",
  "relay-secondary-japan": "Azure Japan backbone",
  "relay-regional-osaka": "Osaka relay",
  "relay-regional-australia": "Azure Australia relay",
  "edge-london": "London edge",
  "edge-tokyo": "Tokyo edge",
  "edge-sydney": "Sydney edge",
  "edge-australia": "Azure Australia edge",
  "edge-japan": "Azure Japan edge",
};

const labelPlacements = {
  "contrib-london": [11, 5, "start"],
  "edge-london": [11, 5, "start"],
  "relay-primary-amsterdam": [11, 5, "start"],
  "relay-secondary-japan": [-11, -8, "end"],
  "relay-regional-osaka": [-11, 5, "end"],
  "edge-tokyo": [11, 5, "start"],
  "edge-japan": [11, 5, "start"],
  "relay-regional-australia": [-11, 5, "end"],
  "edge-sydney": [-11, 5, "end"],
  "edge-australia": [-11, 20, "end"],
};

const providers = Object.fromEntries(
  topologyDocument.topology.nodes.map((node) => [
    node.node_id,
    node.failure_domain.provider,
  ]),
);

function project([longitude, latitude]) {
  const width = 1200;
  const mapLeft = 60;
  const mapRight = 1140;
  const mapTop = 105;
  const mapBottom = 595;
  return [
    mapLeft + ((longitude + 180) / 360) * (mapRight - mapLeft),
    mapTop + ((90 - latitude) / 180) * (mapBottom - mapTop),
  ];
}

function geographicPath(geometry) {
  const polygons =
    geometry.type === "Polygon"
      ? [geometry.coordinates]
      : geometry.type === "MultiPolygon"
        ? geometry.coordinates
        : [];
  const paths = [];
  for (const polygon of polygons) {
    for (const ring of polygon) {
      let segment = [];
      let previousLongitude = null;
      for (const coordinate of ring) {
        if (
          previousLongitude !== null &&
          Math.abs(coordinate[0] - previousLongitude) > 180
        ) {
          if (segment.length > 2) paths.push(segment);
          segment = [];
        }
        segment.push(project(coordinate));
        previousLongitude = coordinate[0];
      }
      if (segment.length > 2) paths.push(segment);
    }
  }
  return paths
    .map(
      (points) =>
        `M${points
          .map(([x, y]) => `${x.toFixed(1)},${y.toFixed(1)}`)
          .join("L")}Z`,
    )
    .join("");
}

function nodePoint(nodeId) {
  const [x, y] = project(nodeLocations[nodeId]);
  const [offsetX, offsetY] = visualOffsets[nodeId] ?? [0, 0];
  return [x + offsetX, y + offsetY];
}

function curvedLink(parentId, childId) {
  const [x1, y1] = nodePoint(parentId);
  const [x2, y2] = nodePoint(childId);
  const bend = Math.max(28, Math.abs(x2 - x1) * 0.18);
  const controlY = Math.min(y1, y2) - bend;
  return `M${x1.toFixed(1)},${y1.toFixed(1)} Q${((x1 + x2) / 2).toFixed(
    1,
  )},${controlY.toFixed(1)} ${x2.toFixed(1)},${y2.toFixed(1)}`;
}

async function renderMeshMap() {
  const response = await fetch(
    "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/geojson/ne_110m_admin_0_countries.geojson",
  );
  if (!response.ok) {
    throw new Error(`Natural Earth download failed: ${response.status}`);
  }
  const world = await response.json();
  const width = 1200;
  const height = 700;
  const elements = [];

  elements.push(`
    <rect width="${width}" height="${height}" fill="#08111f"/>
    <text x="60" y="45" fill="#f7fbff" font-size="28" font-family="Inter,Arial,sans-serif" font-weight="600">Ten-node GCP and Azure qualification mesh</text>
    <text x="60" y="74" fill="#9fb1c7" font-size="16" font-family="Inter,Arial,sans-serif">Two providers · two backbone routes · regional relays · five playback edges</text>
    <rect x="60" y="105" width="1080" height="490" rx="10" fill="#0b1726" stroke="#1d3047"/>
  `);
  for (const feature of world.features) {
    const drawing = geographicPath(feature.geometry);
    if (drawing) {
      elements.push(
        `<path d="${drawing}" fill="#13263a" stroke="#29415b" stroke-width="0.65"/>`,
      );
    }
  }
  for (const longitude of [-120, -60, 0, 60, 120]) {
    const [x] = project([longitude, 0]);
    elements.push(
      `<line x1="${x}" y1="105" x2="${x}" y2="595" stroke="#1a2d43" stroke-width="1"/>`,
    );
  }
  for (const latitude of [-60, -30, 0, 30, 60]) {
    const [, y] = project([0, latitude]);
    elements.push(
      `<line x1="60" y1="${y}" x2="1140" y2="${y}" stroke="#1a2d43" stroke-width="1"/>`,
    );
  }
  for (const link of topologyDocument.topology.parent_links) {
    const secondary = link.role === "secondary";
    elements.push(
      `<path d="${curvedLink(
        link.parent_node_id,
        link.child_node_id,
      )}" fill="none" stroke="${
        secondary ? "#8598ad" : "#5dd3a5"
      }" stroke-width="${secondary ? 1.5 : 2.5}" stroke-dasharray="${
        secondary ? "6 6" : "none"
      }" opacity="${secondary ? 0.58 : 0.88}"/>`,
    );
  }

  for (const node of topologyDocument.topology.nodes) {
    const [x, y] = nodePoint(node.node_id);
    const provider = providers[node.node_id];
    const fill = provider === "azure" ? "#55c2ff" : "#ffb454";
    const radius =
      node.role === "origin" ? 9 : node.role === "playback_edge" ? 6 : 8;
    const [labelOffsetX, labelOffsetY, labelAnchor] =
      labelPlacements[node.node_id];
    const labelX = x + labelOffsetX;
    elements.push(`
      <circle cx="${x}" cy="${y}" r="${radius + 4}" fill="${fill}" opacity="0.16"/>
      <circle cx="${x}" cy="${y}" r="${radius}" fill="${fill}" stroke="#f7fbff" stroke-width="1.2"/>
      <text x="${labelX}" y="${y + labelOffsetY}" text-anchor="${labelAnchor}" fill="#eef5fc" font-size="13" font-family="Inter,Arial,sans-serif">${escapeXml(labels[node.node_id])}</text>
    `);
  }

  const [sourceX, sourceY] = nodePoint("contrib-london");
  elements.push(`
    <path d="M${sourceX - 45},${sourceY - 38} L${sourceX - 7},${sourceY - 7}" stroke="#b98cff" stroke-width="2.5" fill="none"/>
    <rect x="${sourceX - 75}" y="${sourceY - 58}" width="34" height="24" rx="4" fill="#b98cff" stroke="#f7fbff"/>
    <text x="${sourceX - 82}" y="${sourceY - 65}" text-anchor="start" fill="#d9c7ff" font-size="12" font-family="Inter,Arial,sans-serif">16-vCPU test source</text>
    <circle cx="68" cy="630" r="6" fill="#ffb454"/><text x="82" y="635" fill="#d8e3ef" font-size="13" font-family="Inter,Arial,sans-serif">GCP</text>
    <circle cx="142" cy="630" r="6" fill="#55c2ff"/><text x="156" y="635" fill="#d8e3ef" font-size="13" font-family="Inter,Arial,sans-serif">Azure</text>
    <line x1="232" y1="630" x2="266" y2="630" stroke="#5dd3a5" stroke-width="2.5"/><text x="276" y="635" fill="#d8e3ef" font-size="13" font-family="Inter,Arial,sans-serif">primary</text>
    <line x1="354" y1="630" x2="388" y2="630" stroke="#8598ad" stroke-width="1.5" stroke-dasharray="6 6"/><text x="398" y="635" fill="#d8e3ef" font-size="13" font-family="Inter,Arial,sans-serif">warm secondary</text>
    <text x="1140" y="675" text-anchor="end" fill="#71869e" font-size="11" font-family="Inter,Arial,sans-serif">Base geometry: Natural Earth 1:110m · topology captured after run 20260728T113000Z</text>
  `);

  return `<svg xmlns="http://www.w3.org/2000/svg" role="img" aria-labelledby="title description" viewBox="0 0 ${width} ${height}">
    <title id="title">Global Needletail qualification mesh</title>
    <desc id="description">A world map shows one London contributor, two backbone relays, two regional relays, and five playback edges across GCP and Azure.</desc>
    ${elements.join("\n")}
  </svg>
  `;
}

await writeFile(
  path.join(outputDirectory, "2026-07-28-global-flac-latency.svg"),
  normalizeSvg(renderLatencyChart()),
);
await writeFile(
  path.join(outputDirectory, "2026-07-28-global-mesh.svg"),
  normalizeSvg(await renderMeshMap()),
);
