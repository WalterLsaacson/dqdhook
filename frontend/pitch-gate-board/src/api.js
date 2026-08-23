export async function api(path, options = {}) {
  const res = await fetch(path, {
    headers: { Accept: "application/json" },
    cache: "no-store",
    ...options,
  });
  const data = await res.json();
  if (!res.ok) throw new Error(data.error || res.statusText);
  return data;
}

export function fetchGoals(limit = 5000) {
  return api(`/api/goals?limit=${limit}`);
}

export function fetchStatus() {
  return api("/api/status");
}
