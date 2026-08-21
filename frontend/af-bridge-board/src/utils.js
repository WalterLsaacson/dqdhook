export function $(id) {
  return document.getElementById(id);
}

export function escapeHtml(s) {
  return String(s ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

export function leagueColor(row) {
  let h = 0;
  const key = String(row?.af_league || row?.af_country || "unknown");
  for (let i = 0; i < key.length; i++) h = (h * 31 + key.charCodeAt(i)) >>> 0;
  return `hsl(${h % 360} 58% 46%)`;
}

export function groupMatches(matches, filterLeagueId = null) {
  const map = new Map();
  for (const row of matches) {
    const id = String(row.af_league || "unknown");
    if (filterLeagueId && id !== filterLeagueId) continue;
    if (!map.has(id)) {
      map.set(id, {
        id,
        name: row.af_league || "Unknown",
        color: leagueColor(row),
        matches: [],
      });
    }
    map.get(id).matches.push(row);
  }
  return [...map.values()].sort(
    (a, b) => b.matches.length - a.matches.length || a.name.localeCompare(b.name),
  );
}

export function formatBeijingDateTime(value) {
  if (!value) return "—";
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return String(value);
  return new Intl.DateTimeFormat("zh-CN", {
    timeZone: "Asia/Shanghai",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  }).format(d);
}

export function goalsText(goals) {
  if (!goals || (goals.home == null && goals.away == null)) return "—";
  const h = goals.home == null ? "?" : goals.home;
  const a = goals.away == null ? "?" : goals.away;
  return `${h} - ${a}`;
}

export function statusShort(afStatus) {
  if (!afStatus) return "—";
  if (typeof afStatus === "string") return afStatus;
  return afStatus.short || afStatus.long || "—";
}
