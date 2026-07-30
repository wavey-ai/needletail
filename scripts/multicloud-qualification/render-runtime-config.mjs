#!/usr/bin/env node

import { lstat, mkdir, readFile, readdir, rm, writeFile } from "node:fs/promises";
import { homedir } from "node:os";
import { resolve } from "node:path";

const root = resolve(import.meta.dirname, "../..");
const defaultRuntimeRoot = resolve(`${root}/target/multicloud-qualification`);
const outputMarkerName = ".needletail-runtime-output";
const outputMarkerContents = "needletail.multicloud-runtime-output.v1\n";
const programTemplatePath = resolve(
  `${root}/deploy/multicloud-qualification/relay-program.json`,
);
const nodeRuntimePath = resolve(
  `${root}/deploy/multicloud-qualification/node-runtime.json`,
);

const usage = () => {
  console.log(`Usage: render-runtime-config.mjs [inventory.json] [--output-root PATH]

Renders the committed multicloud relay topology and all node environment files.
NEEDLETAIL_TLS_SERVER_NAME must name the qualification certificate.

A custom output root must already exist and be empty or contain the renderer's
ownership marker.`);
};

const parseArguments = (arguments_) => {
  let inventoryPath;
  let outputRoot = defaultRuntimeRoot;
  for (let index = 0; index < arguments_.length; index += 1) {
    const argument = arguments_[index];
    if (argument === "--help" || argument === "-h") {
      usage();
      process.exit(0);
    }
    if (argument === "--output-root") {
      index += 1;
      if (!arguments_[index]) {
        throw new Error("--output-root requires a path");
      }
      outputRoot = resolve(arguments_[index]);
      continue;
    }
    if (argument.startsWith("-") || inventoryPath) {
      throw new Error(`unexpected argument: ${argument}`);
    }
    inventoryPath = resolve(argument);
  }
  return {
    inventoryPath:
      inventoryPath ?? resolve(`${defaultRuntimeRoot}/lab-inventory.json`),
    outputRoot,
  };
};

const parseJson = async (path) => {
  try {
    return JSON.parse(await readFile(path, "utf8"));
  } catch (error) {
    throw new Error(`could not read JSON input ${path}: ${error.message}`);
  }
};

const assertObject = (value, description) => {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new Error(`${description} must be an object`);
  }
};

const assertUniqueNodes = (items, description) => {
  if (!Array.isArray(items) || items.length === 0) {
    throw new Error(`${description} must contain nodes`);
  }
  const result = new Map();
  for (const node of items) {
    assertObject(node, `${description} node`);
    if (
      typeof node.node_id !== "string" ||
      !/^[a-z0-9]+(?:-[a-z0-9]+)*$/.test(node.node_id)
    ) {
      throw new Error(`${description} contains an invalid node_id`);
    }
    if (result.has(node.node_id)) {
      throw new Error(`${description} contains duplicate node ${node.node_id}`);
    }
    result.set(node.node_id, node);
  }
  return result;
};

const assertExactNodeSet = (expected, actual, description) => {
  const missing = [...expected.keys()].filter((nodeId) => !actual.has(nodeId));
  const extra = [...actual.keys()].filter((nodeId) => !expected.has(nodeId));
  if (missing.length > 0 || extra.length > 0) {
    throw new Error(
      `${description} node set differs from topology; missing=${missing.join(",") || "none"} extra=${extra.join(",") || "none"}`,
    );
  }
};

const assertIpv4 = (value, description) => {
  if (typeof value !== "string") {
    throw new Error(`${description} must be an IPv4 address`);
  }
  const octets = value.split(".");
  if (
    octets.length !== 4 ||
    octets.some(
      (octet) =>
        !/^(?:0|[1-9][0-9]{0,2})$/.test(octet) ||
        Number.parseInt(octet, 10) > 255,
    )
  ) {
    throw new Error(`${description} must be an IPv4 address`);
  }
};

const assertPort = (value, description) => {
  if (!Number.isInteger(value) || value < 1 || value > 65_535) {
    throw new Error(`${description} must be a valid port`);
  }
};

const assertCoordinate = (value, minimum, maximum, description) => {
  if (typeof value !== "number" || value < minimum || value > maximum) {
    throw new Error(`${description} is outside its valid range`);
  }
};

const replaceHost = (socket, host) => {
  if (
    typeof socket !== "string" ||
    !/^192\.0\.2\.1:[1-9][0-9]{0,4}$/.test(socket)
  ) {
    throw new Error(`invalid committed placeholder socket: ${socket}`);
  }
  return `${host}${socket.slice(socket.lastIndexOf(":"))}`;
};

const environmentValue = (value, description) => {
  const rendered = String(value);
  if (
    rendered.length === 0 ||
    rendered.includes("\n") ||
    rendered.includes("\r")
  ) {
    throw new Error(`${description} is not a safe environment value`);
  }
  return rendered;
};

const renderEnvironment = (entries) =>
  `${entries
    .map(([name, value]) => {
      if (!/^[A-Z][A-Z0-9_]*$/.test(name)) {
        throw new Error(`invalid environment variable name: ${name}`);
      }
      return `${name}=${environmentValue(value, `environment value ${name}`)}`;
    })
    .join("\n")}\n`;

const missingOkLstat = async (path) => {
  try {
    return await lstat(path);
  } catch (error) {
    if (error.code === "ENOENT") {
      return undefined;
    }
    throw error;
  }
};

const prepareOutputRoot = async (outputRoot) => {
  const isDefault = outputRoot === defaultRuntimeRoot;
  const forbiddenRoots = new Set([
    "/",
    resolve(homedir()),
    root,
    resolve(root, ".."),
  ]);
  if (forbiddenRoots.has(outputRoot)) {
    throw new Error(`refusing broad output root: ${outputRoot}`);
  }

  let outputRootStat = await missingOkLstat(outputRoot);
  if (!outputRootStat) {
    if (!isDefault) {
      throw new Error(
        "custom --output-root must be an existing dedicated directory",
      );
    }
    await mkdir(outputRoot, { recursive: true, mode: 0o700 });
    outputRootStat = await lstat(outputRoot);
  }
  if (outputRootStat.isSymbolicLink() || !outputRootStat.isDirectory()) {
    throw new Error("--output-root must be a regular directory, not a symlink");
  }

  const markerPath = resolve(outputRoot, outputMarkerName);
  const markerStat = await missingOkLstat(markerPath);
  if (markerStat) {
    if (markerStat.isSymbolicLink() || !markerStat.isFile()) {
      throw new Error("runtime output ownership marker must be a regular file");
    }
    if ((await readFile(markerPath, "utf8")) !== outputMarkerContents) {
      throw new Error("runtime output ownership marker is invalid");
    }
    return;
  }

  if (!isDefault && (await readdir(outputRoot)).length !== 0) {
    throw new Error(
      "custom --output-root must be empty or contain a valid ownership marker",
    );
  }
  await writeFile(markerPath, outputMarkerContents, {
    encoding: "utf8",
    flag: "wx",
    mode: 0o600,
  });
};

const { inventoryPath, outputRoot } = parseArguments(process.argv.slice(2));
const tlsServerName = process.env.NEEDLETAIL_TLS_SERVER_NAME;
const enableSrt = process.env.NEEDLETAIL_ENABLE_SRT ?? "0";
const tlsServerLabels =
  typeof tlsServerName === "string" ? tlsServerName.split(".") : [];
if (
  typeof tlsServerName !== "string" ||
  tlsServerName.length === 0 ||
  tlsServerName.length > 253 ||
  tlsServerLabels.some(
    (label) =>
      label.length === 0 ||
      label.length > 63 ||
      !/^[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?$/.test(label),
  )
) {
  throw new Error("NEEDLETAIL_TLS_SERVER_NAME must be a DNS name");
}
if (enableSrt !== "0" && enableSrt !== "1") {
  throw new Error("NEEDLETAIL_ENABLE_SRT must be 0 or 1");
}

const [inventory, programTemplate, nodeRuntime] = await Promise.all([
  parseJson(inventoryPath),
  parseJson(programTemplatePath),
  parseJson(nodeRuntimePath),
]);
assertObject(inventory, "lab inventory");
assertObject(programTemplate, "relay program");
assertObject(programTemplate.topology, "relay topology");
assertObject(nodeRuntime, "node runtime");
if (inventory.schema !== "needletail.multicloud-lab.v1") {
  throw new Error(`unsupported inventory schema: ${inventory.schema}`);
}
if (nodeRuntime.schema !== "needletail.multicloud-node-runtime.v1") {
  throw new Error(`unsupported node runtime schema: ${nodeRuntime.schema}`);
}

const topologyNodes = assertUniqueNodes(
  programTemplate.topology.nodes,
  "relay topology",
);
const inventoryNodes = assertUniqueNodes(inventory.nodes, "lab inventory");
const runtimeNodes = assertUniqueNodes(nodeRuntime.nodes, "node runtime");
assertExactNodeSet(topologyNodes, inventoryNodes, "lab inventory");
assertExactNodeSet(topologyNodes, runtimeNodes, "node runtime");

for (const [nodeId, topologyNode] of topologyNodes) {
  const inventoryNode = inventoryNodes.get(nodeId);
  if (inventoryNode.provider !== topologyNode.failure_domain?.provider) {
    throw new Error(`${nodeId} provider differs from the committed topology`);
  }
  if (
    inventoryNode.provider === "gcp" &&
    inventoryNode.zone !== topologyNode.failure_domain.zone
  ) {
    throw new Error(`${nodeId} zone differs from the committed topology`);
  }
  if (
    inventoryNode.provider === "azure" &&
    inventoryNode.region !== topologyNode.failure_domain.region
  ) {
    throw new Error(`${nodeId} region differs from the committed topology`);
  }
  assertIpv4(inventoryNode.private_ip, `${nodeId} private_ip`);
  assertIpv4(inventoryNode.public_ip, `${nodeId} public_ip`);
}

const address = (nodeId, kind) => {
  const node = inventoryNodes.get(nodeId);
  if (!node) {
    throw new Error(`topology endpoint references unknown node ${nodeId}`);
  }
  return node[`${kind}_ip`];
};

const program = structuredClone(programTemplate);
for (const link of program.carrier_links) {
  link.sender_bind = replaceHost(
    link.sender_bind,
    address(link.parent_node_id, "private"),
  );
  link.sender_peer = replaceHost(
    link.sender_peer,
    address(link.parent_node_id, "public"),
  );
  link.receiver_bind = replaceHost(link.receiver_bind, "0.0.0.0");
  link.receiver_target = replaceHost(
    link.receiver_target,
    address(link.child_node_id, "public"),
  );
}
for (const link of program.failover_control_links) {
  link.controller_bind = replaceHost(
    link.controller_bind,
    address(link.controller_node_id, "private"),
  );
  link.controller_peer = replaceHost(
    link.controller_peer,
    address(link.controller_node_id, "public"),
  );
  link.listener_bind = replaceHost(
    link.listener_bind,
    address(link.forwarder_node_id, "private"),
  );
  link.listener_target = replaceHost(
    link.listener_target,
    address(link.forwarder_node_id, "public"),
  );
}

const defaults = nodeRuntime.mesh_defaults;
assertObject(defaults, "mesh defaults");
for (const [name, value] of [
  ["telemetry_port", defaults.telemetry_port],
  ["part_ms", defaults.part_ms],
  ["segment_ms", defaults.segment_ms],
  ["parts_per_segment", defaults.parts_per_segment],
  ["window_parts", defaults.window_parts],
]) {
  assertPort(value, `mesh default ${name}`);
}
if (
  !Array.isArray(nodeRuntime.default_telemetry_peers) ||
  nodeRuntime.default_telemetry_peers.length === 0
) {
  throw new Error("node runtime must define default telemetry peers");
}

const operations = nodeRuntime.operations;
assertObject(operations, "Operations runtime");
if (
  operations.enabled !== true ||
  operations.authority !== "needletail-controller"
) {
  throw new Error("Operations runtime must enable the Needletail controller");
}
if (
  !Array.isArray(operations.voter_nodes) ||
  operations.voter_nodes.length < 3 ||
  operations.voter_nodes.length % 2 === 0 ||
  new Set(operations.voter_nodes).size !== operations.voter_nodes.length
) {
  throw new Error("Operations voters must be a unique odd set of at least three nodes");
}
assertObject(operations.public_endpoints, "Operations public endpoints");
if (runtimeNodes.get(operations.global_entrypoint_node)?.kind !== "mesh") {
  throw new Error("Operations global entry point must be a mesh node");
}
assertPort(
  operations.global_entrypoint_port,
  "Operations global entry point port",
);
for (const voterNodeId of operations.voter_nodes) {
  if (runtimeNodes.get(voterNodeId)?.kind !== "mesh") {
    throw new Error(`Operations voter ${voterNodeId} must be a mesh node`);
  }
  const endpoint = operations.public_endpoints[voterNodeId];
  if (
    typeof endpoint !== "string" ||
    !/^https:\/\/[A-Za-z0-9.-]+(?::[1-9][0-9]{0,4})?\/mesh$/.test(endpoint)
  ) {
    throw new Error(`Operations voter ${voterNodeId} has an invalid public endpoint`);
  }
}
for (const [name, value] of [
  ["etcd_client_port", operations.etcd_client_port],
  ["etcd_peer_port", operations.etcd_peer_port],
]) {
  assertPort(value, `Operations ${name}`);
}
for (const [name, value] of [
  ["heartbeat_interval_ms", operations.heartbeat_interval_ms],
  ["election_timeout_ms", operations.election_timeout_ms],
  ["lease_ttl_seconds", operations.lease_ttl_seconds],
  ["renew_interval_ms", operations.renew_interval_ms],
  ["lease_safety_margin_ms", operations.lease_safety_margin_ms],
  ["max_clock_skew_ms", operations.max_clock_skew_ms],
  ["stale_after_ms", operations.stale_after_ms],
]) {
  if (!Number.isInteger(value) || value <= 0) {
    throw new Error(`Operations ${name} must be a positive integer`);
  }
}
if (
  operations.election_timeout_ms < operations.heartbeat_interval_ms * 10 ||
  operations.renew_interval_ms * 2 >= operations.lease_ttl_seconds * 1000 ||
  operations.lease_safety_margin_ms >= operations.lease_ttl_seconds * 1000 ||
  operations.lease_safety_margin_ms < operations.max_clock_skew_ms
) {
  throw new Error("Operations election and lease timing is unsafe");
}
const operationsAllowedEndpoints = operations.voter_nodes.map(
  (nodeId) => operations.public_endpoints[nodeId],
);
const etcdEndpoints = operations.voter_nodes.map(
  (nodeId) =>
    `https://${address(nodeId, "public")}:${operations.etcd_client_port}`,
);
const etcdInitialCluster = operations.voter_nodes
  .map(
    (nodeId) =>
      `${nodeId}=https://${address(nodeId, "public")}:${operations.etcd_peer_port}`,
  )
  .join(",");

const renderedEnvironments = new Map();
for (const [nodeId, topologyNode] of topologyNodes) {
  const runtimeNode = runtimeNodes.get(nodeId);
  if (runtimeNode.kind === "contributor") {
    if (topologyNode.role !== "origin") {
      throw new Error(`${nodeId} contributor runtime is not a topology origin`);
    }
    assertCoordinate(runtimeNode.latitude, -90, 90, `${nodeId} latitude`);
    assertCoordinate(runtimeNode.longitude, -180, 180, `${nodeId} longitude`);
    if (!/^[a-z][a-z0-9-]*$/.test(runtimeNode.continent ?? "")) {
      throw new Error(`${nodeId} has an invalid continent`);
    }
    assertObject(runtimeNode.environment, `${nodeId} environment`);
    renderedEnvironments.set(
      nodeId,
      renderEnvironment([
        ["NEEDLETAIL_NODE_ID", nodeId],
        ["NEEDLETAIL_ENABLE_SRT", enableSrt],
        ...Object.entries(runtimeNode.environment),
      ]),
    );
    continue;
  }
  if (runtimeNode.kind !== "mesh" || topologyNode.role === "origin") {
    throw new Error(`${nodeId} has an invalid runtime kind`);
  }
  assertCoordinate(runtimeNode.latitude, -90, 90, `${nodeId} latitude`);
  assertCoordinate(runtimeNode.longitude, -180, 180, `${nodeId} longitude`);
  for (const field of [
    "mesh_port",
    "http_port",
    "fec_port",
    "media_fec_port",
  ]) {
    assertPort(runtimeNode[field], `${nodeId} ${field}`);
  }
  if (!/^[a-z][a-z0-9-]*$/.test(runtimeNode.continent ?? "")) {
    throw new Error(`${nodeId} has an invalid continent`);
  }
  const peerIds =
    runtimeNode.telemetry_peers ?? nodeRuntime.default_telemetry_peers;
  if (
    !Array.isArray(peerIds) ||
    peerIds.length === 0 ||
    new Set(peerIds).size !== peerIds.length
  ) {
    throw new Error(`${nodeId} must have unique telemetry peers`);
  }
  const telemetryPeers = peerIds.map((peerId) => {
    if (peerId === nodeId || runtimeNodes.get(peerId)?.kind !== "mesh") {
      throw new Error(`${nodeId} has invalid telemetry peer ${peerId}`);
    }
    return `${address(peerId, "public")}:${defaults.telemetry_port}`;
  });
  const entries = [
    ["NEEDLETAIL_NODE_ID", nodeId],
    ["NEEDLETAIL_REGION", topologyNode.failure_domain.region],
    ["NEEDLETAIL_CONTINENT", runtimeNode.continent],
    ["NEEDLETAIL_LATITUDE", runtimeNode.latitude],
    ["NEEDLETAIL_LONGITUDE", runtimeNode.longitude],
    ["NEEDLETAIL_PRIVATE_IP", address(nodeId, "private")],
    ["NEEDLETAIL_MESH_PORT", runtimeNode.mesh_port],
    ["NEEDLETAIL_HTTP_PORT", runtimeNode.http_port],
    ["NEEDLETAIL_FEC_PORT", runtimeNode.fec_port],
    ["NEEDLETAIL_MEDIA_FEC_PORT", runtimeNode.media_fec_port],
    ["NEEDLETAIL_TELEMETRY_PORT", defaults.telemetry_port],
    ["NEEDLETAIL_TELEMETRY_DNS_NAME", tlsServerName],
    ["NEEDLETAIL_TELEMETRY_PEERS", telemetryPeers.join(",")],
    ["NEEDLETAIL_OPERATIONS_ELECTED", 1],
    [
      "NEEDLETAIL_OPERATIONS_SNAPSHOT_FILE",
      "/run/needletail/operations-snapshot.json",
    ],
    [
      "NEEDLETAIL_OPERATIONS_SOURCES_FILE",
      "/etc/needletail/operations-sources.json",
    ],
    [
      "NEEDLETAIL_CONTROLLER_STATE_FILE",
      "/run/needletail/operations-controller-state.json",
    ],
    [
      "NEEDLETAIL_OPS_ASSIGNMENT_FILE",
      "/run/needletail/operations-collector.json",
    ],
    ["NEEDLETAIL_OPS_ENTRYPOINT_HOST", operations.global_entrypoint_host],
    [
      "NEEDLETAIL_OPS_ALLOWED_ENDPOINTS",
      operationsAllowedEndpoints.join(","),
    ],
    [
      "NEEDLETAIL_OPS_LEASE_SAFETY_MARGIN_MS",
      operations.lease_safety_margin_ms,
    ],
    [
      "NEEDLETAIL_OPS_MAX_CLOCK_SKEW_MS",
      operations.max_clock_skew_ms,
    ],
    ["NEEDLETAIL_CONTROLLER_ETCD_ENDPOINTS", etcdEndpoints.join(",")],
    [
      "NEEDLETAIL_CONTROLLER_ETCD_CA_CERT",
      "/etc/needletail/operations-pki/ca.pem",
    ],
    [
      "NEEDLETAIL_CONTROLLER_ETCD_CLIENT_CERT",
      "/etc/needletail/operations-pki/client.pem",
    ],
    [
      "NEEDLETAIL_CONTROLLER_ETCD_CLIENT_KEY",
      "/etc/needletail/operations-pki/client-key.pem",
    ],
    [
      "NEEDLETAIL_CONTROLLER_CANDIDATE",
      operations.voter_nodes.includes(nodeId) ? "true" : "false",
    ],
    ["NEEDLETAIL_CONTROLLER_LEASE_TTL_SECONDS", operations.lease_ttl_seconds],
    ["NEEDLETAIL_CONTROLLER_RENEW_INTERVAL_MS", operations.renew_interval_ms],
    [
      "NEEDLETAIL_OPS_PUBLIC_ENDPOINT",
      operations.public_endpoints[nodeId] ?? operations.public_endpoints[operations.voter_nodes[0]],
    ],
    [
      "NEEDLETAIL_OPS_SNAPSHOT_ENDPOINT",
      `https://${tlsServerName}:${runtimeNode.http_port}/api/mesh`,
    ],
    [
      "NEEDLETAIL_OPS_SNAPSHOT_ADDRESS",
      `${address(nodeId, "public")}:${runtimeNode.http_port}`,
    ],
    ["NEEDLETAIL_PART_MS", defaults.part_ms],
    ["NEEDLETAIL_SEGMENT_MS", defaults.segment_ms],
    ["NEEDLETAIL_PARTS_PER_SEGMENT", defaults.parts_per_segment],
    ["NEEDLETAIL_WINDOW_PARTS", defaults.window_parts],
  ];
  if (topologyNode.role === "playback_edge") {
    entries.push(["NEEDLETAIL_EDGE_WEBTRANSPORT", 1]);
  }
  renderedEnvironments.set(nodeId, renderEnvironment(entries));
}

const programPath = resolve(outputRoot, "relay-program.json");
const operationsSourcesPath = resolve(outputRoot, "operations-sources.json");
const envDirectory = resolve(outputRoot, "env");
const etcdEnvDirectory = resolve(outputRoot, "etcd-env");
const operationsProxyDirectory = resolve(outputRoot, "operations-proxy");
await prepareOutputRoot(outputRoot);
await rm(envDirectory, { recursive: true, force: true });
await rm(etcdEnvDirectory, { recursive: true, force: true });
await rm(operationsProxyDirectory, { recursive: true, force: true });
await mkdir(envDirectory, { mode: 0o700 });
await mkdir(etcdEnvDirectory, { mode: 0o700 });
await mkdir(operationsProxyDirectory, { mode: 0o700 });
await writeFile(programPath, `${JSON.stringify(program, null, 2)}\n`, {
  mode: 0o600,
});
const operationsSources = {
  schema: "needletail.operations-sources.v1",
  stale_after_ms: operations.stale_after_ms,
  nodes: [...runtimeNodes]
    .filter(([, runtimeNode]) => runtimeNode.kind === "mesh")
    .map(([nodeId, runtimeNode]) => ({
      node_id: nodeId,
      endpoint: `https://${tlsServerName}:${runtimeNode.http_port}/api/mesh/local`,
      address: `${address(nodeId, "public")}:${runtimeNode.http_port}`,
    })),
  contributor: (() => {
    const runtimeNode = runtimeNodes.get("contrib-london");
    return runtimeNode
      ? {
          node_id: "contrib-london",
          endpoint: `https://${tlsServerName}:${runtimeNode.environment.NEEDLETAIL_HTTP_PORT}/api/status`,
          address: `${address("contrib-london", "public")}:${runtimeNode.environment.NEEDLETAIL_HTTP_PORT}`,
        }
      : null;
  })(),
  contributor_node: (() => {
    const nodeId = "contrib-london";
    const runtimeNode = runtimeNodes.get(nodeId);
    const topologyNode = topologyNodes.get(nodeId);
    return runtimeNode && topologyNode
      ? {
          node_id: nodeId,
          provider: topologyNode.failure_domain.provider,
          region: topologyNode.failure_domain.region,
          zone: topologyNode.failure_domain.zone,
          role: topologyNode.role,
          continent: runtimeNode.continent,
          latitude: runtimeNode.latitude,
          longitude: runtimeNode.longitude,
          public_endpoint: `https://${tlsServerName}:${runtimeNode.environment.NEEDLETAIL_HTTP_PORT}`,
          total_storage_bytes: 0,
          used_storage_bytes: 0,
          egress_capacity_bps: 0,
          contributor_streams: 0,
          active_streams: 0,
          draining: false,
        }
      : null;
  })(),
  topology_links: program.carrier_links.map((link) => ({
    from_node_id: link.parent_node_id,
    to_node_id: link.child_node_id,
    role: link.role,
    state: "configured",
    path: link.lane,
    generation: program.topology.generation,
  })),
};
await writeFile(
  operationsSourcesPath,
  `${JSON.stringify(operationsSources, null, 2)}\n`,
  { mode: 0o600 },
);
for (const [nodeId, contents] of renderedEnvironments) {
  await writeFile(resolve(envDirectory, `${nodeId}.env`), contents, {
    mode: 0o600,
  });
}
for (const nodeId of operations.voter_nodes) {
  await writeFile(
    resolve(etcdEnvDirectory, `${nodeId}.env`),
    renderEnvironment([
      ["ETCD_NAME", nodeId],
      ["ETCD_DATA_DIR", "/var/lib/needletail-etcd"],
      [
        "ETCD_LISTEN_PEER_URLS",
        `https://0.0.0.0:${operations.etcd_peer_port}`,
      ],
      [
        "ETCD_INITIAL_ADVERTISE_PEER_URLS",
        `https://${address(nodeId, "public")}:${operations.etcd_peer_port}`,
      ],
      [
        "ETCD_LISTEN_CLIENT_URLS",
        `https://0.0.0.0:${operations.etcd_client_port}`,
      ],
      [
        "ETCD_ADVERTISE_CLIENT_URLS",
        `https://${address(nodeId, "public")}:${operations.etcd_client_port}`,
      ],
      ["ETCD_INITIAL_CLUSTER", etcdInitialCluster],
      ["ETCD_INITIAL_CLUSTER_STATE", "new"],
      ["ETCD_INITIAL_CLUSTER_TOKEN", "needletail-operations-20260730"],
      ["ETCD_HEARTBEAT_INTERVAL", operations.heartbeat_interval_ms],
      ["ETCD_ELECTION_TIMEOUT", operations.election_timeout_ms],
      ["ETCD_AUTO_COMPACTION_MODE", "periodic"],
      ["ETCD_AUTO_COMPACTION_RETENTION", "1h"],
      ["ETCD_SNAPSHOT_COUNT", 10000],
      ["ETCD_CERT_FILE", "/etc/needletail/operations-pki/server.pem"],
      ["ETCD_KEY_FILE", "/etc/needletail/operations-pki/server-key.pem"],
      ["ETCD_TRUSTED_CA_FILE", "/etc/needletail/operations-pki/ca.pem"],
      ["ETCD_CLIENT_CERT_AUTH", "true"],
      ["ETCD_PEER_CERT_FILE", "/etc/needletail/operations-pki/server.pem"],
      ["ETCD_PEER_KEY_FILE", "/etc/needletail/operations-pki/server-key.pem"],
      ["ETCD_PEER_TRUSTED_CA_FILE", "/etc/needletail/operations-pki/ca.pem"],
      ["ETCD_PEER_CLIENT_CERT_AUTH", "true"],
    ]),
    { mode: 0o600 },
  );
}
const proxyPorts = new Set([String(operations.global_entrypoint_port)]);
const entrypointRuntimeNode = runtimeNodes.get(
  operations.global_entrypoint_node,
);
await writeFile(
  resolve(
    operationsProxyDirectory,
    `${operations.global_entrypoint_port}.env`,
  ),
  renderEnvironment([
    [
      "NEEDLETAIL_OPERATIONS_PROXY_TARGET",
      `127.0.0.1:${entrypointRuntimeNode.http_port}`,
    ],
  ]),
  { mode: 0o600 },
);
for (const nodeId of operations.voter_nodes) {
  if (nodeId === "edge-london") {
    continue;
  }
  const publicUrl = new URL(operations.public_endpoints[nodeId]);
  const proxyPort = publicUrl.port;
  if (
    !/^[1-9][0-9]{0,4}$/.test(proxyPort) ||
    Number.parseInt(proxyPort, 10) > 65_535 ||
    proxyPorts.has(proxyPort)
  ) {
    throw new Error(`Operations voter ${nodeId} has an invalid proxy port`);
  }
  proxyPorts.add(proxyPort);
  const runtimeNode = runtimeNodes.get(nodeId);
  await writeFile(
    resolve(operationsProxyDirectory, `${proxyPort}.env`),
    renderEnvironment([
      [
        "NEEDLETAIL_OPERATIONS_PROXY_TARGET",
        `${address(nodeId, "public")}:${runtimeNode.http_port}`,
      ],
    ]),
    { mode: 0o600 },
  );
}

console.log(
  `Rendered ${programPath}, ${operationsSourcesPath}, ${renderedEnvironments.size} node environments, and ${operations.voter_nodes.length} etcd voter environments`,
);
