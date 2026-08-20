const formatRate = (bps) => {
  const value = Number(bps || 0);
  if (value >= 1e9) return `${(value / 1e9).toFixed(2)} Gb/s`;
  if (value >= 1e6) return `${(value / 1e6).toFixed(1)} Mb/s`;
  if (value >= 1e3) return `${(value / 1e3).toFixed(1)} kb/s`;
  return `${value.toFixed(0)} b/s`;
};

const setText = (id, value) => {
  const node = document.getElementById(id);
  if (node) node.textContent = value;
};

async function refreshMesh() {
  try {
    const response = await fetch('/api/live-mesh', { cache: 'no-store' });
    if (!response.ok) throw new Error(`mesh status ${response.status}`);
    const snapshot = await response.json();
    const links = Array.isArray(snapshot.topology_links) ? snapshot.topology_links : [];
    const reported = links.filter((link) => link.reporting === 'reported' && Number.isFinite(link.rtt_ms));
    const aggregate = links.reduce((sum, link) => sum + Number(link.throughput_bps || 0), 0);
    const worstLoss = reported.reduce((max, link) => Math.max(max, Number(link.loss_percent || 0)), 0);
    const sample = reported.find((link) => Number(link.throughput_bps || 0) > 0) || reported[0];

    setText('metric-nodes', String(Array.isArray(snapshot.nodes) ? snapshot.nodes.length : 0));
    setText('metric-links', `${reported.length}/${links.length}`);
    setText('metric-throughput', formatRate(aggregate));
    setText('metric-loss', `${worstLoss.toFixed(2)}%`);
    setText('snapshot-age', 'LIVE / REPORTED');

    if (sample) {
      setText('terminal-rtt', `${Number(sample.rtt_ms).toFixed(1)} ms`);
      setText('terminal-jitter', `${Number(sample.jitter_ms || 0).toFixed(1)} ms`);
      setText('terminal-loss', `${Number(sample.loss_percent || 0).toFixed(2)}%`);
      setText('terminal-rate', formatRate(sample.throughput_bps));
    }
  } catch (_) {
    setText('snapshot-age', 'TELEMETRY RETRY');
  }
}

refreshMesh();
setInterval(refreshMesh, 15000);
