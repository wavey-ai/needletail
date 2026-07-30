#!/usr/bin/env node

import { copyFile, readFile, writeFile } from "node:fs/promises";
import { resolve } from "node:path";

const root = resolve(import.meta.dirname, "../..");
const inventoryPath = resolve(process.argv[2] ?? `${root}/target/multicloud-qualification/lab-inventory.json`);
const programTemplatePath = resolve(`${root}/deploy/multicloud-qualification/relay-program.json`);
const programPath = resolve(`${root}/target/multicloud-qualification/relay-program.json`);
const envDirectory = resolve(`${root}/target/multicloud-qualification/env`);

const inventory = JSON.parse(await readFile(inventoryPath, "utf8"));
const nodes = new Map(inventory.nodes.map((node) => [node.node_id, node]));
const address = (nodeId, kind) => {
  const value = nodes.get(nodeId)?.[`${kind}_ip`];
  if (!value) {
    throw new Error(`inventory is missing ${kind} address for ${nodeId}`);
  }
  return value;
};
const replaceHost = (socket, host) => {
  const separator = socket.lastIndexOf(":");
  if (separator < 0) {
    throw new Error(`invalid socket address: ${socket}`);
  }
  return `${host}${socket.slice(separator)}`;
};

await copyFile(programTemplatePath, programPath);
const program = JSON.parse(await readFile(programPath, "utf8"));
for (const link of program.carrier_links) {
  link.sender_bind = replaceHost(link.sender_bind, address(link.parent_node_id, "private"));
  link.sender_peer = replaceHost(link.sender_peer, address(link.parent_node_id, "public"));
  link.receiver_bind = replaceHost(link.receiver_bind, "0.0.0.0");
  link.receiver_target = replaceHost(link.receiver_target, address(link.child_node_id, "public"));
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
await writeFile(programPath, `${JSON.stringify(program, null, 2)}\n`);

const telemetryPeers = new Map([
  ["relay-regional-osaka", ["relay-primary-amsterdam", "relay-secondary-japan"]],
  ["relay-secondary-japan", ["relay-regional-osaka", "edge-australia"]],
  ["relay-regional-australia", ["relay-regional-osaka", "relay-secondary-japan"]],
  ["edge-japan", ["relay-regional-australia", "relay-regional-osaka"]],
]);
const defaultPeers = ["relay-regional-osaka", "relay-secondary-japan"];

for (const node of inventory.nodes) {
  if (node.node_id === "contrib-london") {
    continue;
  }
  const envPath = resolve(envDirectory, `${node.node_id}.env`);
  let source = await readFile(envPath, "utf8");
  source = source.replace(
    /^NEEDLETAIL_PRIVATE_IP=.*$/m,
    `NEEDLETAIL_PRIVATE_IP=${node.private_ip}`,
  );
  const peers = telemetryPeers.get(node.node_id) ?? defaultPeers;
  source = source.replace(
    /^NEEDLETAIL_TELEMETRY_PEERS=.*$/m,
    `NEEDLETAIL_TELEMETRY_PEERS=${peers
      .map((peer) => `${address(peer, "public")}:27300`)
      .join(",")}`,
  );
  await writeFile(envPath, source);
}

console.log(`Rendered ${programPath} and ${inventory.nodes.length - 1} node environment files`);
