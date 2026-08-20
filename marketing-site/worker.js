const CONSOLE_HOST = "tail.bitneedle.com";
const LEGACY_CONSOLE_ORIGIN = "https://needletail.bitneedle.com";
const APEX_ORIGIN = "https://bitneedle.com";
const PLAYBACK_EDGES = Object.freeze({
  "edge-canada.bitneedle.com": Object.freeze({
    nodeId: "edge-london",
    origin: "http://needletail-edge-canada-20260820.canadaeast.cloudapp.azure.com",
  }),
  "edge-korea.bitneedle.com": Object.freeze({
    nodeId: "edge-tokyo",
    origin: "http://needletail-edge-korea-20260820.koreacentral.cloudapp.azure.com",
  }),
  "edge-australia.bitneedle.com": Object.freeze({
    nodeId: "edge-sydney",
    origin: "http://needletail-edge-australia-20260820.australiasoutheast.cloudapp.azure.com",
  }),
  "edge-brazil.bitneedle.com": Object.freeze({
    nodeId: "edge-australia",
    origin: "http://needletail-edge-brazil-20260820.brazilsouth.cloudapp.azure.com",
  }),
  "edge-eastasia.bitneedle.com": Object.freeze({
    nodeId: "edge-japan",
    origin: "http://needletail-edge-eastasia-20260820.eastasia.cloudapp.azure.com",
  }),
});

const PLAYBACK_ENDPOINTS = Object.freeze(
  Object.fromEntries(
    Object.entries(PLAYBACK_EDGES).map(([hostname, edge]) => [
      edge.nodeId,
      `https://${hostname}`,
    ]),
  ),
);

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const playbackEdge = PLAYBACK_EDGES[url.hostname];

    if (playbackEdge) {
      return proxyPlaybackEdge(request, url, playbackEdge);
    }

    if (url.hostname === CONSOLE_HOST) {
      return proxyConsole(request, url);
    }

    if (url.hostname === "www.bitneedle.com") {
      const canonical = new URL(url.pathname + url.search, APEX_ORIGIN);
      return Response.redirect(canonical, 308);
    }

    if (url.pathname === "/api/live-mesh") {
      return proxyLiveMesh(request);
    }

    if (url.pathname === "/console" || url.pathname === "/console/") {
      return Response.redirect(`https://${CONSOLE_HOST}/mesh#network`, 302);
    }

    const response = await env.ASSETS.fetch(request);
    return withSiteHeaders(response);
  },
};

async function proxyConsole(request, url) {
  const upstream = new URL(url.pathname + url.search, LEGACY_CONSOLE_ORIGIN);
  const headers = new Headers(request.headers);
  headers.set("host", upstream.hostname);
  headers.set("x-forwarded-host", CONSOLE_HOST);
  const response = await fetch(new Request(upstream, request), { headers });
  if (url.pathname === "/api/mesh" && response.ok) {
    return enrichMeshSnapshot(response);
  }
  const responseHeaders = new Headers(response.headers);
  responseHeaders.set("x-robots-tag", "noindex, nofollow");
  responseHeaders.set("referrer-policy", "same-origin");
  const location = responseHeaders.get("location");
  if (location) {
    const redirected = new URL(location, upstream);
    if (redirected.origin === LEGACY_CONSOLE_ORIGIN) {
      redirected.protocol = "https:";
      redirected.host = CONSOLE_HOST;
      responseHeaders.set("location", redirected.toString());
    }
  }
  return new Response(response.body, {
    status: response.status,
    statusText: response.statusText,
    headers: responseHeaders,
  });
}

async function enrichMeshSnapshot(response) {
  const snapshot = await response.json();
  if (Array.isArray(snapshot.nodes)) {
    for (const node of snapshot.nodes) {
      const endpoint = PLAYBACK_ENDPOINTS[node?.node_id];
      if (endpoint) node.public_endpoint = endpoint;
    }
  }
  const headers = new Headers(response.headers);
  headers.delete("content-length");
  headers.set("content-type", "application/json; charset=utf-8");
  headers.set("cache-control", "no-store");
  headers.set("x-robots-tag", "noindex, nofollow");
  headers.set("x-content-type-options", "nosniff");
  return new Response(JSON.stringify(snapshot), {
    status: response.status,
    statusText: response.statusText,
    headers,
  });
}

async function proxyPlaybackEdge(request, url, edge) {
  if (request.method !== "GET" && request.method !== "HEAD") {
    return new Response("Method not allowed", {
      status: 405,
      headers: { allow: "GET, HEAD" },
    });
  }
  const upstream = new URL(url.pathname + url.search, edge.origin);
  const headers = new Headers(request.headers);
  headers.delete("cookie");
  headers.delete("authorization");
  headers.set("host", upstream.hostname);
  headers.set("x-forwarded-host", url.hostname);
  const response = await fetch(new Request(upstream, request), {
    headers,
    cf: { cacheTtl: 0 },
  });
  const responseHeaders = new Headers(response.headers);
  responseHeaders.set("cache-control", "no-store");
  responseHeaders.set("x-needletail-playback-edge", edge.nodeId);
  responseHeaders.set("x-content-type-options", "nosniff");
  return new Response(response.body, {
    status: response.status,
    statusText: response.statusText,
    headers: responseHeaders,
  });
}

async function proxyLiveMesh(request) {
  if (request.method !== "GET" && request.method !== "HEAD") {
    return new Response("Method not allowed", {
      status: 405,
      headers: { allow: "GET, HEAD" },
    });
  }
  const upstream = new URL("/api/mesh", LEGACY_CONSOLE_ORIGIN);
  const response = await fetch(upstream, {
    headers: { accept: "application/json" },
    cf: { cacheTtl: 0 },
  });
  const headers = new Headers(response.headers);
  headers.set("cache-control", "no-store");
  headers.set("access-control-allow-origin", APEX_ORIGIN);
  headers.set("x-content-type-options", "nosniff");
  return new Response(response.body, {
    status: response.status,
    statusText: response.statusText,
    headers,
  });
}

function withSiteHeaders(response) {
  const headers = new Headers(response.headers);
  headers.set("x-content-type-options", "nosniff");
  headers.set("referrer-policy", "strict-origin-when-cross-origin");
  headers.set("permissions-policy", "camera=(), microphone=(), geolocation=()");
  headers.set(
    "content-security-policy",
    "default-src 'self'; script-src 'self'; style-src 'self' https://fonts.googleapis.com; font-src https://fonts.gstatic.com; img-src 'self' data:; connect-src 'self'; base-uri 'self'; form-action 'none'; frame-ancestors 'none'",
  );
  return new Response(response.body, {
    status: response.status,
    statusText: response.statusText,
    headers,
  });
}
