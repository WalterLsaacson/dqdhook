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

/** Human label for aggregate / frame badges. */
export function stateLabel(state) {
  const s = String(state || "unclear");
  const map = {
    in_play: "in_play",
    stopped: "stopped",
    unclear: "unclear",
    waiting: "等待判定",
    waiting_in_play: "等待进行中信号",
    pending_judge: "待判定",
    capture_failed: "截帧失败",
    reversed: "回撤",
    reversed_after_in_play: "回撤·曾in_play",
    mixed: "mixed",
  };
  return map[s] || s;
}

/** True when this goal was judged in_play on a frame, then later reversed. */
export function reversedAfterInPlay(goal) {
  return Boolean(
    (goal?.reversed || goal?.verdict === "reversed") && goal?.in_play_elapsed_s != null
  );
}

/** Badge key for a goal row (splits plain reverse vs reverse-after-in_play). */
export function goalVerdictKey(goal) {
  if (goal?.reversed || goal?.verdict === "reversed") {
    return reversedAfterInPlay(goal) ? "reversed_after_in_play" : "reversed";
  }
  return goal?.verdict;
}
