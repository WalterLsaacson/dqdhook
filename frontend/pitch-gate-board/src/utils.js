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
  if (
    (goal?.kind === "reversal_observe" || goal?.observe_only) &&
    goal?.score_from &&
    goal?.score_to
  ) {
    return `${goal.score_from}→${goal.score_to}`;
  }
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
    reversal_observe: "回撤观察",
    mixed: "mixed",
  };
  return map[s] || s;
}

/** True when this row is the post-reversal AF/DOM trail (not a buyable goal). */
export function isReversalObserve(goal) {
  return Boolean(
    goal?.kind === "reversal_observe" || goal?.observe_only || goal?.verdict === "reversal_observe"
  );
}

/** True when this goal was judged in_play on a frame, then later reversed. */
export function reversedAfterInPlay(goal) {
  return Boolean(
    (goal?.reversed || goal?.verdict === "reversed") && goal?.in_play_elapsed_s != null
  );
}

/** Badge key for a goal row (splits plain reverse vs reverse-after-in_play). */
export function goalVerdictKey(goal) {
  if (isReversalObserve(goal)) return "reversal_observe";
  if (goal?.reversed || goal?.verdict === "reversed") {
    return reversedAfterInPlay(goal) ? "reversed_after_in_play" : "reversed";
  }
  const v = goal?.verdict;
  if (v === "waiting" || v === "unclear") return "waiting_in_play";
  return v;
}

/** Header / pill filters — one bucket per row badge, plus 全部. */
export const GOAL_FILTERS = [
  { id: "all", short: "全部" },
  { id: "in_play", short: "in_play" },
  { id: "stopped", short: "stopped" },
  { id: "waiting_in_play", short: "等待信号" },
  { id: "pending_judge", short: "待判定" },
  { id: "capture_failed", short: "截帧失败" },
  { id: "mixed", short: "mixed" },
  { id: "reversed", short: "回撤" },
  { id: "reversed_after_in_play", short: "回撤·曾in_play" },
  { id: "reversal_observe", short: "回撤观察" },
];

const FILTER_IDS = new Set(GOAL_FILTERS.map((f) => f.id));

export function isGoalFilter(id) {
  return FILTER_IDS.has(id);
}

export function goalMatchesFilter(goal, filter) {
  if (!filter || filter === "all") return true;
  return goalVerdictKey(goal) === filter;
}

export function countByFilter(goals) {
  const list = goals || [];
  const counts = { all: list.length };
  for (const f of GOAL_FILTERS) {
    if (f.id === "all") continue;
    counts[f.id] = 0;
  }
  for (const g of list) {
    const key = goalVerdictKey(g);
    if (key in counts) counts[key] += 1;
  }
  return counts;
}

export function emptyFilterMessage(filter, { detail = false } = {}) {
  if (!filter || filter === "all") {
    return detail
      ? "选择左侧一场进球查看逐帧判定。"
      : "暂无进球记录。<br/>等 DQD goal / 回撤观察 + pitch-gate 采样。";
  }
  const row = GOAL_FILTERS.find((f) => f.id === filter);
  const label = row?.short || stateLabel(filter);
  return `当前筛选下无「${label}」进球。`;
}
