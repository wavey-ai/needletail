#!/usr/bin/env node
// Run the measured viewer in headless Chrome and write its record as JSON.
//
//   node run.mjs --kind whep --url URL --duration 60 --out viewer.json
//   node run.mjs --kind cf --session PUBLISHER_SESSION ...   (CF_REALTIME_APP_ID / _SECRET in the environment)
//   node run.mjs --kind livekit --url ws://HOST:7880 --token TOKEN ...
//   node run.mjs --kind hls --url https://.../master.m3u8 [--hls-config '{"..."}'] ...
//
// The page is served from this process on localhost. Chrome runs with web
// security off, so that one page can reach every system, and it accepts the
// self-signed certificates of a lab.

import fs from "node:fs";
import http from "node:http";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import puppeteer from "puppeteer-core";

const here = path.dirname(fileURLToPath(import.meta.url));
const args = {};
for (let i = 2; i < process.argv.length; i++) {
  const key = process.argv[i].replace(/^--/, "");
  const next = process.argv[i + 1];
  if (next === undefined || next.startsWith("--")) args[key] = true;
  else { args[key] = next; i++; }
}
const duration = Number(args.duration || 30);
const files = {
  "/": ["player.html", "text/html"],
  "/player.js": ["player.js", "text/javascript"],
  "/lib/hls.js": ["node_modules/hls.js/dist/hls.min.js", "text/javascript"],
  "/lib/livekit-client.js": ["node_modules/livekit-client/dist/livekit-client.umd.js", "text/javascript"],
};
const server = http.createServer((request, response) => {
  const entry = files[request.url.split("?")[0]];
  if (!entry) { response.writeHead(404); response.end(); return; }
  response.writeHead(200, { "Content-Type": entry[1] });
  response.end(fs.readFileSync(path.join(here, entry[0])));
});
await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
const origin = `http://127.0.0.1:${server.address().port}`;

function cpuSeconds(pids) {
  let total = 0;
  for (const pid of pids) {
    try {
      const fields = fs.readFileSync(`/proc/${pid}/stat`, "utf8").split(") ")[1].split(" ");
      total += (Number(fields[11]) + Number(fields[12])) / 100;
    } catch {}
  }
  return total;
}
function chromePids(root) {
  const pids = [];
  for (const name of fs.readdirSync("/proc")) {
    if (!/^\d+$/.test(name)) continue;
    try {
      const command = fs.readFileSync(`/proc/${name}/cmdline`, "utf8");
      if (command.includes(root)) pids.push(Number(name));
    } catch {}
  }
  return pids;
}

const profile = fs.mkdtempSync(path.join(os.tmpdir(), "compare-player-"));
const browser = await puppeteer.launch({
  executablePath: args.chrome || "/usr/bin/google-chrome",
  headless: true,
  userDataDir: profile,
  args: [
    "--no-sandbox", "--autoplay-policy=no-user-gesture-required", "--disable-web-security",
    "--ignore-certificate-errors", "--disable-features=WebRtcHideLocalIpsWithMdns",
    "--disable-background-timer-throttling", "--disable-renderer-backgrounding",
    "--disable-backgrounding-occluded-windows", "--window-size=1280,720",
    ...(args["chrome-arg"] ? String(args["chrome-arg"]).split(" ") : []),
  ],
});
let exit = 0;
try {
  const page = await browser.newPage();
  const consoleLines = [];
  page.on("console", (message) => { if (consoleLines.length < 200) consoleLines.push(message.text().slice(0, 300)); });
  page.on("pageerror", (error) => consoleLines.push("pageerror " + String(error).slice(0, 300)));

  // Cloudflare Realtime signalling stays in this process.
  const cf = { app: process.env.CF_REALTIME_APP_ID, secret: process.env.CF_REALTIME_APP_SECRET, session: null };
  async function cfCall(method, suffix, body) {
    const response = await fetch(`https://rtc.live.cloudflare.com/v1/apps/${cf.app}${suffix}`, {
      method, headers: { Authorization: `Bearer ${cf.secret}`, "Content-Type": "application/json" },
      body: body ? JSON.stringify(body) : undefined,
    });
    const reply = await response.json();
    if (!response.ok || reply.errorCode) throw new Error(`Cloudflare ${suffix.split("/").pop()}: ${response.status} ${reply.errorCode || ""} ${reply.errorDescription || ""}`);
    return reply;
  }
  await page.exposeFunction("cfPull", async () => {
    cf.session = (await cfCall("POST", "/sessions/new")).sessionId;
    const names = args["audio-only"] ? ["audio"] : ["video", "audio"];
    const tracks = names.map((trackName) => ({ location: "remote", sessionId: args.session, trackName }));
    const reply = await cfCall("POST", `/sessions/${cf.session}/tracks/new`, { tracks });
    for (const track of reply.tracks || []) if (track.errorCode) throw new Error(`track ${track.trackName}: ${track.errorCode}`);
    return reply.sessionDescription;
  });
  await page.exposeFunction("cfAnswer", async (sdp) => {
    await cfCall("PUT", `/sessions/${cf.session}/renegotiate`, { sessionDescription: { type: "answer", sdp } });
  });

  await page.goto(origin + "/", { waitUntil: "load" });
  const config = {
    kind: args.kind, url: args.url, token: args.token || process.env.LIVEKIT_TOKEN, audio: Boolean(args.audio),
    audioOnly: Boolean(args["audio-only"]),
    hls: args["hls-config"] ? JSON.parse(args["hls-config"]) : undefined,
    jitterBufferTarget: args["jitter-target"] !== undefined ? Number(args["jitter-target"]) : undefined,
  };
  await page.evaluate((value) => { window.CONFIG = value; }, config);
  const pids = chromePids(profile);
  const cpu0 = cpuSeconds(pids), wall0 = Date.now();
  await page.evaluate(() => window.start());
  await new Promise((resolve) => setTimeout(resolve, duration * 1000));
  const result = await page.evaluate(() => window.result());
  const pidsEnd = chromePids(profile);
  result.chrome_cpu_cores = Math.round((cpuSeconds(pidsEnd) - cpu0) / ((Date.now() - wall0) / 1000) * 100) / 100;
  result.label = args.label || "";
  result.lane = args.lane || args.kind;
  result.host = os.hostname();
  result.cores = os.cpus().length;
  result.duration_seconds = duration;
  result.console = consoleLines;
  result.chrome = await browser.version();
  const text = JSON.stringify(result);
  if (args.out) fs.writeFileSync(args.out, text + "\n"); else process.stdout.write(text + "\n");
  if (result.error) exit = 1;
} catch (error) {
  console.error("player:", error);
  exit = 1;
} finally {
  await browser.close().catch(() => {});
  server.close();
  fs.rmSync(profile, { recursive: true, force: true });
}
process.exit(exit);
