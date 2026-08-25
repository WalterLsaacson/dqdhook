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
    in_play: "DOM in_play",
    aligned_buy: "买入 ∧",
    wait_af: "等 AF",
    wait_shot: "无射门",
    after_buy: "买后 DOM",
    var_veto: "VAR 否决",
    reversal_risk_skip: "开场跳过",
    stopped: "stopped",
    unclear: "unclear",
    waiting: "等待判定",
    waiting_in_play: "等 DOM",
    pending_judge: "待判定",
    capture_failed: "读数失败",
    reversed: "回撤·未买",
    reversed_after_buy: "回撤·已买",
    reversed_after_in_play: "回撤·已买",
    reversal_observe: "回撤观察",
    flatten_or: "已平仓 ∨",
    hold: "持仓",
    mixed: "mixed",
  };
  return map[s] || s;
}

const REVERSAL_VERDICTS = new Set(["reversal_observe", "flatten_or", "hold"]);

/** True when this row is the post-reversal AF/DOM trail (not a buyable goal). */
export function isReversalObserve(goal) {
  return Boolean(
    goal?.kind === "reversal_observe" ||
      goal?.observe_only ||
      REVERSAL_VERDICTS.has(String(goal?.verdict || ""))
  );
}

/** True when this goal had an aligned buy, then DQD reversed. */
export function reversedAfterBuy(goal) {
  return Boolean(
    goal?.verdict === "reversed_after_buy" ||
      ((goal?.reversed || goal?.verdict === "reversed") &&
        (goal?.had_aligned_buy || goal?.aligned_elapsed_s != null))
  );
}

/** @deprecated use reversedAfterBuy — DOM in_play alone is not a buy. */
export function reversedAfterInPlay(goal) {
  return reversedAfterBuy(goal);
}

/** Badge key for a goal row. */
export function goalVerdictKey(goal) {
  if (isReversalObserve(goal)) {
    const v = String(goal?.verdict || "");
    if (v === "flatten_or" || v === "hold") return v;
    return "reversal_observe";
  }
  if (goal?.reversed || goal?.verdict === "reversed" || goal?.verdict === "reversed_after_buy") {
    return reversedAfterBuy(goal) ? "reversed_after_buy" : "reversed";
  }
  const v = goal?.verdict;
  if (v === "waiting" || v === "unclear" || v === "in_play") return v === "in_play" ? "wait_af" : "waiting_in_play";
  return v;
}

/** Header / pill filters — one bucket per row badge, plus 全部. */
export const GOAL_FILTERS = [
  { id: "all", short: "全部" },
  { id: "aligned_buy", short: "买入" },
  { id: "wait_af", short: "等AF" },
  { id: "wait_shot", short: "无射门" },
  { id: "waiting_in_play", short: "等DOM" },
  { id: "var_veto", short: "VAR" },
  { id: "reversal_risk_skip", short: "开场跳过" },
  { id: "stopped", short: "stopped" },
  { id: "pending_judge", short: "待判定" },
  { id: "capture_failed", short: "读数失败" },
  { id: "mixed", short: "mixed" },
  { id: "reversed", short: "回撤·未买" },
  { id: "reversed_after_buy", short: "回撤·已买" },
  { id: "reversal_observe", short: "回撤观察" },
  { id: "flatten_or", short: "已平仓" },
  { id: "hold", short: "持仓" },
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
      : "暂无进球记录。<br/>等 DQD 进球门控（DOM∧AF∧射门）或回撤观察（AF∨DOM）。";
  }
  const row = GOAL_FILTERS.find((f) => f.id === filter);
  const label = row?.short || stateLabel(filter);
  return `当前筛选下无「${label}」进球。`;
}
