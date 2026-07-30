#!/usr/bin/env node

import assert from "node:assert/strict";
import {
  lstat,
  mkdtemp,
  mkdir,
  readFile,
  readdir,
  rm,
  writeFile,
} from "node:fs/promises";
import { tmpdir } from "node:os";
import { resolve } from "node:path";
import { spawnSync } from "node:child_process";
import test from "node:test";

const root = resolve(import.meta.dirname, "../..");
const renderer = resolve(
  root,
  "scripts/multicloud-qualification/render-runtime-config.mjs",
);
const inventory = resolve(
  root,
  "scripts/tests/fixtures/multicloud-runtime-config/lab-inventory.json",
);
const topologyPath = resolve(
  root,
  "deploy/multicloud-qualification/relay-program.json",
);

const runRenderer = (outputRoot, extraEnvironment = {}) =>
  spawnSync(
    process.execPath,
    [renderer, inventory, "--output-root", outputRoot],
    {
      cwd: root,
      encoding: "utf8",
      env: {
        ...process.env,
        NEEDLETAIL_TLS_SERVER_NAME: "qualification.example.test",
        ...extraEnvironment,
      },
    },
  );

const normalizeProgramAddresses = (program) => {
  const normalized = structuredClone(program);
  const normalize = (value) =>
    `192.0.2.1:${value.slice(value.lastIndexOf(":") + 1)}`;
  for (const link of normalized.carrier_links) {
    link.sender_bind = normalize(link.sender_bind);
    link.sender_peer = normalize(link.sender_peer);
    link.receiver_bind = normalize(link.receiver_bind);
    link.receiver_target = normalize(link.receiver_target);
  }
  for (const link of normalized.failover_control_links) {
    link.controller_bind = normalize(link.controller_bind);
    link.controller_peer = normalize(link.controller_peer);
    link.listener_bind = normalize(link.listener_bind);
    link.listener_target = normalize(link.listener_target);
  }
  return normalized;
};

test("renders every environment from committed inputs on an empty output tree", async () => {
  const temporaryRoot = await mkdtemp(
    resolve(tmpdir(), "needletail-runtime-config-"),
  );
  try {
    const completed = runRenderer(temporaryRoot);
    assert.equal(completed.status, 0, completed.stderr);
    assert.equal(
      await readFile(
        resolve(temporaryRoot, ".needletail-runtime-output"),
        "utf8",
      ),
      "needletail.multicloud-runtime-output.v1\n",
    );

    const envFiles = (await readdir(resolve(temporaryRoot, "env"))).sort();
    assert.deepEqual(envFiles, [
      "contrib-london.env",
      "edge-australia.env",
      "edge-japan.env",
      "edge-london.env",
      "edge-sydney.env",
      "edge-tokyo.env",
      "relay-primary-amsterdam.env",
      "relay-regional-australia.env",
      "relay-regional-osaka.env",
      "relay-secondary-japan.env",
    ]);

    assert.equal(
      await readFile(resolve(temporaryRoot, "env/contrib-london.env"), "utf8"),
      `NEEDLETAIL_NODE_ID=contrib-london
NEEDLETAIL_ENABLE_SRT=0
NEEDLETAIL_HTTP_PORT=19443
NEEDLETAIL_PART_MS=250
NEEDLETAIL_FMP4_PART_MS=250
NEEDLETAIL_DAW_MEDIA_PORT=27100
NEEDLETAIL_DAW_HLS_QUEUE_CAPACITY=4096
NEEDLETAIL_DAW_HLS_PACKAGING=fmp4
NEEDLETAIL_DAW_HLS_FORMATS=flac,opus
NEEDLETAIL_RELAY_MIN_REPAIR_SYMBOLS=3
`,
    );

    const edgeJapan = await readFile(
      resolve(temporaryRoot, "env/edge-japan.env"),
      "utf8",
    );
    assert.match(edgeJapan, /^NEEDLETAIL_REGION=japaneast$/m);
    assert.match(edgeJapan, /^NEEDLETAIL_PRIVATE_IP=10\.71\.1\.5$/m);
    assert.match(
      edgeJapan,
      /^NEEDLETAIL_TELEMETRY_DNS_NAME=qualification\.example\.test$/m,
    );
    assert.match(
      edgeJapan,
      /^NEEDLETAIL_TELEMETRY_PEERS=198\.51\.100\.25:27300,203\.0\.113\.30:27300$/m,
    );
    assert.match(edgeJapan, /^NEEDLETAIL_EDGE_WEBTRANSPORT=1$/m);

    const committed = JSON.parse(await readFile(topologyPath, "utf8"));
    const rendered = JSON.parse(
      await readFile(resolve(temporaryRoot, "relay-program.json"), "utf8"),
    );
    assert.deepEqual(normalizeProgramAddresses(rendered), committed);
    assert.equal(
      rendered.carrier_links[0].sender_bind,
      "10.84.10.5:22301",
    );
    assert.equal(
      rendered.carrier_links[0].receiver_target,
      "203.0.113.20:22001",
    );

    await writeFile(resolve(temporaryRoot, "env/stale.env"), "stale\n");
    const repeated = runRenderer(temporaryRoot);
    assert.equal(repeated.status, 0, repeated.stderr);
    assert.equal(
      (await readdir(resolve(temporaryRoot, "env"))).includes("stale.env"),
      false,
    );
  } finally {
    await rm(temporaryRoot, { recursive: true, force: true });
  }
});

test("rejects a home-like broad root before touching its env directory", async () => {
  const temporaryRoot = await mkdtemp(
    resolve(tmpdir(), "needletail-runtime-config-broad-"),
  );
  try {
    const fakeHome = resolve(temporaryRoot, "operator-home");
    const fakeEnv = resolve(fakeHome, "env");
    await mkdir(fakeEnv, { recursive: true });
    const sentinel = resolve(fakeEnv, "keep.txt");
    await writeFile(sentinel, "keep\n");

    const completed = runRenderer(fakeHome, { HOME: fakeHome });
    assert.notEqual(completed.status, 0);
    assert.match(completed.stderr, /refusing broad output root/);
    assert.equal(await readFile(sentinel, "utf8"), "keep\n");
  } finally {
    await rm(temporaryRoot, { recursive: true, force: true });
  }
});

test("rejects an unowned nonempty custom output root without deletion", async () => {
  const temporaryRoot = await mkdtemp(
    resolve(tmpdir(), "needletail-runtime-config-unowned-"),
  );
  try {
    const outputRoot = resolve(temporaryRoot, "existing");
    const existingEnv = resolve(outputRoot, "env");
    await mkdir(existingEnv, { recursive: true });
    const sentinel = resolve(existingEnv, "keep.txt");
    await writeFile(sentinel, "keep\n");

    const completed = runRenderer(outputRoot);
    assert.notEqual(completed.status, 0);
    assert.match(
      completed.stderr,
      /must be empty or contain a valid ownership marker/,
    );
    assert.equal(await readFile(sentinel, "utf8"), "keep\n");
  } finally {
    await rm(temporaryRoot, { recursive: true, force: true });
  }
});

test("rejects a missing custom output root instead of claiming a typo", async () => {
  const temporaryRoot = await mkdtemp(
    resolve(tmpdir(), "needletail-runtime-config-missing-root-"),
  );
  try {
    const outputRoot = resolve(temporaryRoot, "does-not-exist");
    const completed = runRenderer(outputRoot);
    assert.notEqual(completed.status, 0);
    assert.match(
      completed.stderr,
      /must be an existing dedicated directory/,
    );
    await assert.rejects(lstat(outputRoot));
  } finally {
    await rm(temporaryRoot, { recursive: true, force: true });
  }
});

test("rejects an incomplete inventory instead of rendering partial topology", async () => {
  const temporaryRoot = await mkdtemp(
    resolve(tmpdir(), "needletail-runtime-config-invalid-"),
  );
  try {
    const fixture = JSON.parse(await readFile(inventory, "utf8"));
    fixture.nodes.pop();
    const incompleteInventory = resolve(temporaryRoot, "inventory.json");
    await writeFile(
      incompleteInventory,
      `${JSON.stringify(fixture, null, 2)}\n`,
    );
    const outputRoot = resolve(temporaryRoot, "output");
    await mkdir(outputRoot);
    const completed = spawnSync(
      process.execPath,
      [renderer, incompleteInventory, "--output-root", outputRoot],
      {
        cwd: root,
        encoding: "utf8",
        env: {
          ...process.env,
          NEEDLETAIL_TLS_SERVER_NAME: "qualification.example.test",
        },
      },
    );
    assert.notEqual(completed.status, 0);
    assert.match(completed.stderr, /node set differs from topology/);
    await assert.rejects(readFile(resolve(outputRoot, "relay-program.json")));
  } finally {
    await rm(temporaryRoot, { recursive: true, force: true });
  }
});

test("renders SRT only as an explicit contributor opt-in", async () => {
  const temporaryRoot = await mkdtemp(
    resolve(tmpdir(), "needletail-runtime-config-srt-"),
  );
  try {
    const completed = runRenderer(temporaryRoot, {
      NEEDLETAIL_ENABLE_SRT: "1",
    });
    assert.equal(completed.status, 0, completed.stderr);
    const contributor = await readFile(
      resolve(temporaryRoot, "env/contrib-london.env"),
      "utf8",
    );
    assert.match(contributor, /^NEEDLETAIL_ENABLE_SRT=1$/m);

    const edge = await readFile(
      resolve(temporaryRoot, "env/edge-london.env"),
      "utf8",
    );
    assert.doesNotMatch(edge, /NEEDLETAIL_ENABLE_SRT/);
  } finally {
    await rm(temporaryRoot, { recursive: true, force: true });
  }
});

test("rejects an invalid SRT runtime setting", async () => {
  const temporaryRoot = await mkdtemp(
    resolve(tmpdir(), "needletail-runtime-config-invalid-srt-"),
  );
  try {
    const completed = runRenderer(temporaryRoot, {
      NEEDLETAIL_ENABLE_SRT: "yes",
    });
    assert.notEqual(completed.status, 0);
    assert.match(completed.stderr, /NEEDLETAIL_ENABLE_SRT must be 0 or 1/);
    await assert.rejects(
      readFile(resolve(temporaryRoot, "env/contrib-london.env")),
    );
  } finally {
    await rm(temporaryRoot, { recursive: true, force: true });
  }
});

test("requires an explicit certificate DNS name", async () => {
  const temporaryRoot = await mkdtemp(
    resolve(tmpdir(), "needletail-runtime-config-dns-"),
  );
  try {
    const completed = runRenderer(temporaryRoot, {
      NEEDLETAIL_TLS_SERVER_NAME: "",
    });
    assert.notEqual(completed.status, 0);
    assert.match(completed.stderr, /NEEDLETAIL_TLS_SERVER_NAME/);
  } finally {
    await rm(temporaryRoot, { recursive: true, force: true });
  }
});
