/** HTTP client for the af-bridge-board server. */

export async function api(path, options = {}) {
  const res = await fetch(path, {
    headers: {
      Accept: "application/json",
      ...(options.body ? { "Content-Type": "application/json" } : {}),
    },
    ...options,
  });
  const data = await res.json();
  if (!res.ok) throw new Error(data.error || res.statusText);
  return data;
}

export function fetchMatches() {
  return api("/api/matches");
}

export function fetchStatus() {
  return api("/api/status");
}

export function runOnce() {
  return api("/api/af/once", { method: "POST", body: "{}" });
}

export function startWatch() {
  return api("/api/af/start", { method: "POST", body: "{}" });
}

export function stopWatch() {
  return api("/api/af/stop", { method: "POST", body: "{}" });
}
