import { copyFile, mkdir, readFile, rm, writeFile } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const source = resolve(root, "src");
const destination = process.argv[2] ? resolve(process.argv[2]) : resolve(root, "dist");
const assets = [
  [resolve(source, "index.html"), resolve(destination, "index.html")],
  [resolve(source, "player.css"), resolve(destination, "player.css")],
  [resolve(source, "manifest.webmanifest"), resolve(destination, "manifest.webmanifest")],
  [resolve(root, "node_modules/hls.js/dist/hls.min.js"), resolve(destination, "hls.min.js")],
];

await rm(destination, { recursive: true, force: true });
await mkdir(destination, { recursive: true });
await Promise.all(assets.map(([from, to]) => copyFile(from, to)));

const [playerSource, pcmPlayerSource, adaptiveLatencySource, streamFreshnessSource] = await Promise.all([
  readFile(resolve(source, "player.js"), "utf8"),
  readFile(resolve(source, "pcm-player.js"), "utf8"),
  readFile(resolve(source, "adaptive-latency.js"), "utf8"),
  readFile(resolve(source, "stream-freshness.js"), "utf8"),
]);
const bundledPlayer = [
  adaptiveLatencySource.replaceAll("export class ", "class "),
  streamFreshnessSource.replaceAll("export function ", "function "),
  pcmPlayerSource.replaceAll("export function ", "function ").replace("export class ", "class "),
  playerSource
    .replace('import { PcmLlHlsPlayer } from "/pcm-player.js";\n', "")
    .replace('import { RollingLatencyWindow, StableLatencyController } from "/adaptive-latency.js";\n', "")
    .replace('import { livePlaylistIsStale } from "/stream-freshness.js";\n\n', ""),
].join("\n");
await writeFile(resolve(destination, "player.js"), bundledPlayer);

const hlsBundle = resolve(destination, "hls.min.js");
const hlsSource = await readFile(hlsBundle, "utf8");
await writeFile(
  hlsBundle,
  hlsSource.replace(/\n\/\/# sourceMappingURL=hls\.min\.js\.map\s*$/, "\n"),
);

console.log(`Needletail Player built at ${destination}`);
