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

export function formatBeijing(value) {
  if (!value) return "—";
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return String(value);
  return new Intl.DateTimeFormat("zh-CN", {
    timeZone: "Asia/Shanghai",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  }).format(d);
}

export function scoreLabel(goal) {
  if (goal?.score) return String(goal.score);
  if (goal?.home_score != null && goal?.away_score != null) {
    return `${goal.home_score}-${goal.away_score}`;
  }
  return "?—?";
}

export function stateBadgeClass(state) {
  const s = String(state || "unclear");
  return `badge badge--${s.replace(/[^a-z0-9_]/gi, "_")}`;
}
