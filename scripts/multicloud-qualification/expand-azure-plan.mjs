#!/usr/bin/env node

import { readFile, writeFile } from "node:fs/promises";
import { resolve } from "node:path";

const [programArgument, japanEdgePublicIp, australiaRelayPublicIp] = process.argv.slice(2);
if (!programArgument || !japanEdgePublicIp || !australiaRelayPublicIp) {
  throw new Error(
    "usage: expand-azure-plan.mjs PROGRAM JAPAN_EDGE_PUBLIC_IP AUSTRALIA_RELAY_PUBLIC_IP",
  );
}

const programPath = resolve(programArgument);
const program = JSON.parse(await readFile(programPath, "utf8"));
const topology = program.topology;
const newNodeIds = new Set(["edge-japan", "relay-regional-australia"]);
const osakaSourceLink = program.carrier_links.find(
  ({ parent_node_id: parent }) => parent === "relay-regional-osaka",
);
if (!osakaSourceLink) {
  throw new Error("program does not contain a relay-regional-osaka carrier link");
}
const [osakaPrivateIp] = osakaSourceLink.sender_bind.split(":");

topology.generation = 2026072801;
topology.nodes = topology.nodes.filter(({ node_id: nodeId }) => !newNodeIds.has(nodeId));
topology.nodes.push(
  {
    node_id: "relay-regional-australia",
    level: 3,
    role: "regional_relay",
    failure_domain: {
      provider: "azure",
      region: "australiaeast",
      asn: 8075,
      zone: "australiaeast-unzoned",
    },
  },
  {
    node_id: "edge-japan",
    level: 4,
    role: "playback_edge",
    failure_domain: {
      provider: "azure",
      region: "japaneast",
      asn: 8075,
      zone: "japaneast-unzoned",
    },
  },
);

topology.parent_links = topology.parent_links.filter(
  ({ parent_node_id: parent, child_node_id: child }) => (
    !newNodeIds.has(parent)
    && !newNodeIds.has(child)
  ),
);
topology.parent_links.push(
  {
    parent_node_id: "relay-regional-osaka",
    child_node_id: "relay-regional-australia",
    role: "primary",
  },
  {
    parent_node_id: "relay-secondary-japan",
    child_node_id: "relay-regional-australia",
    role: "secondary",
  },
  {
    parent_node_id: "relay-regional-australia",
    child_node_id: "edge-japan",
    role: "primary",
  },
  {
    parent_node_id: "relay-regional-osaka",
    child_node_id: "edge-japan",
    role: "secondary",
  },
);

program.carrier_links = program.carrier_links.filter(
  ({ parent_node_id: parent, child_node_id: child }) => (
    !newNodeIds.has(parent)
    && !newNodeIds.has(child)
  ),
);
program.carrier_links.push(
  {
    parent_node_id: "relay-regional-osaka",
    child_node_id: "relay-regional-australia",
    role: "primary",
    lane: "source",
    sender_bind: `${osakaPrivateIp}:22450`,
    sender_peer: "34.97.104.29:22450",
    receiver_bind: "0.0.0.0:22250",
    receiver_target: `${australiaRelayPublicIp}:22250`,
  },
  {
    parent_node_id: "relay-secondary-japan",
    child_node_id: "relay-regional-australia",
    role: "secondary",
    lane: "repair",
    sender_bind: "10.71.1.4:22451",
    sender_peer: "20.48.56.214:22451",
    receiver_bind: "0.0.0.0:22251",
    receiver_target: `${australiaRelayPublicIp}:22251`,
  },
  {
    parent_node_id: "relay-regional-australia",
    child_node_id: "edge-japan",
    role: "primary",
    lane: "source",
    sender_bind: "10.74.1.5:22460",
    sender_peer: `${australiaRelayPublicIp}:22460`,
    receiver_bind: "0.0.0.0:22260",
    receiver_target: `${japanEdgePublicIp}:22260`,
  },
  {
    parent_node_id: "relay-regional-osaka",
    child_node_id: "edge-japan",
    role: "secondary",
    lane: "repair",
    sender_bind: `${osakaPrivateIp}:22461`,
    sender_peer: "34.97.104.29:22461",
    receiver_bind: "0.0.0.0:22261",
    receiver_target: `${japanEdgePublicIp}:22261`,
  },
);

program.failover_control_links = program.failover_control_links.filter(
  ({ controller_node_id: controller }) => !newNodeIds.has(controller),
);
program.failover_control_links.push(
  {
    forwarder_node_id: "relay-secondary-japan",
    controller_node_id: "relay-regional-australia",
    controller_bind: "10.74.1.5:22550",
    controller_peer: `${australiaRelayPublicIp}:22550`,
    listener_bind: "10.71.1.4:22650",
    listener_target: "20.48.56.214:22650",
  },
  {
    forwarder_node_id: "relay-regional-osaka",
    controller_node_id: "edge-japan",
    controller_bind: "10.71.1.5:22560",
    controller_peer: `${japanEdgePublicIp}:22560`,
    listener_bind: `${osakaPrivateIp}:22660`,
    listener_target: "34.97.104.29:22660",
  },
);

await writeFile(programPath, `${JSON.stringify(program, null, 2)}\n`);
