import { state } from "./state.js";
import {
  $,
  GOAL_FILTERS,
  countByFilter,
  emptyFilterMessage,
  escapeHtml,
  formatBeijing,
  goalMatchesFilter,
  goalVerdictKey,
  isGoalFilter,
  isReversalObserve,
  reversedAfterInPlay,
  scoreLabel,
  stateBadgeClass,
  stateLabel,
} from "./utils.js";

function playStateOf(frame) {
  if (frame?.ok === false) return "capture_failed";
  return String(frame?.judge?.play_state || "pending_judge");
}

const MARK_LABELS = {
  "possession-rect": "控球",
  attack: "进攻",
  "attack-move": "进攻",
  "dangerous-attack": "危险进攻",
  "dangerous-attack-move": "危险进攻",
  ball: "射门",
  net: "射门",
  "penalty-box": "禁区",
};

const NAMI_ZONE_LABELS = {
  center: "中圈开球",
  box_l: "左禁区",
  box_r: "右禁区",
  third_l: "左三分之一",
  third_r: "右三分之一",
  mid: "中场",
  unknown: "未知",
};

function namiZoneLabel(zone) {
  return NAMI_ZONE_LABELS[zone] || zone || "—";
}

function pitchPct(v) {
  return Math.max(1, Math.min(99, Number(v) * 100));
}

function namiDotClass(f, { isIp = false, last = false } = {}) {
  if (isIp) return "is-inplay";
  if (last) return "is-last";
  if (f.in_box) return "is-box";
  if (f.restart_center) return "is-center";
  return "";
}

function renderNamiTrail(goal) {
  const ip = goal.nami_in_play;
  const allFrames = goal.frames || [];
  const seenSi = new Set();
  const domPts = allFrames.filter((f) => {
    if (f.nami?.ball_x == null) return false;
    const si = f.sample_i;
    if (si == null) return true;
    if (seenSi.has(si)) return false;
    seenSi.add(si);
    return true;
  });
  const used = new Map();
  const numbered = domPts
    .map((f, i, arr) => {
      const n = f.nami;
      let x = pitchPct(n.ball_x);
      let y = pitchPct(n.ball_y);
      const cell = `${Math.round(x)}:${Math.round(y)}`;
      const stack = used.get(cell) || 0;
      used.set(cell, stack + 1);
      if (stack) {
        const ang = stack * 0.95;
        const r = 3.4 + (stack % 4) * 1.5;
        x = Math.max(3, Math.min(97, x + Math.cos(ang) * r));
        y = Math.max(4, Math.min(60, y + Math.sin(ang) * r));
      }
      const ps = playStateOf(f);
      const isIp = ps === "in_play" && Number(f.elapsed_s) === Number(goal.in_play_elapsed_s);
      const last = !isIp && i === arr.length - 1;
      const cls = namiDotClass(n, { isIp, last });
      const seq = f.dom_seq || i + 1;
      const ty = Math.max(5, y - 3.4);
      const stale = n.carried ? " · 沿用上次坐标" : "";
      return `<g class="nami-mark">
        <circle class="nami-dot ${cls}" cx="${x}" cy="${y}" r="2.3">
          <title>#${seq} t+${f.elapsed_s ?? "?"}s ${namiZoneLabel(n.zone)} · ${ps}${stale}</title>
        </circle>
        <text class="nami-num" x="${x}" y="${ty}">${seq}</text>
      </g>`;
    })
    .join("");
  let ipLine = "重启 run_main 之后的新球才会有点";
  if (domPts.length && ip) {
    ipLine = `DOM 共 ${domPts.length} 个采样点 · 买入(#${
      allFrames.find((f) => playStateOf(f) === "in_play")?.dom_seq || "?"
    }) 在「${namiZoneLabel(ip.zone)}」`;
  } else if (domPts.length) {
    ipLine = `DOM 共 ${domPts.length} 个采样点`;
  }
  const body = numbered
    ? `<svg class="nami-pitch" viewBox="0 0 100 64" role="img" aria-label="DOM sample ball positions">
      <rect class="nami-pitch__field" x="1" y="1" width="98" height="62" rx="1.5" />
      <line class="nami-pitch__line" x1="50" y1="1" x2="50" y2="63" />
      <circle class="nami-pitch__line" cx="50" cy="32" r="8" fill="none" />
      <rect class="nami-pitch__line" x="1" y="18" width="16" height="28" fill="none" />
      <rect class="nami-pitch__line" x="83" y="18" width="16" height="28" fill="none" />
      ${numbered}
    </svg>
    <p class="nami-legend">
      <span class="nami-legend__swatch is-inplay"></span>买入（首个 in_play）
      <span class="nami-legend__swatch is-center"></span>中圈
      <span class="nami-legend__swatch is-box"></span>禁区
      <span class="nami-legend__swatch is-last"></span>最后一次 DOM
      · 序号 = 第几次 DOM 采样（约 5s 一拍）
    </p>`
    : `<p class="nami-empty">还没有球位点。重启后每个 DOM 采样会记下当时球位并标序号（不再画全程轨迹）。</p>`;
  return `<section class="nami-trail">
    <div class="af-trail__head">
      <h3>Nami 球位（只观察，不下单）</h3>
      <span>${escapeHtml(ipLine)}</span>
    </div>
    ${body}
  </section>`;
}

/** DOM-mode frames have no screenshot; show the text the gate actually read. */
function renderDomCard(f) {
  const pop = f.dom_pop_box;
  if (!pop && !f.dom_center_box) {
    return `<div class="frame__img-wrap"><div class="frame__placeholder">${escapeHtml(
      f.error || "no reading",
    )}</div></div>`;
  }
  const side = String(f.dom_pop_class || "").includes("away")
    ? "away"
    : String(f.dom_pop_class || "").includes("home")
      ? "home"
      : "";
  const marks = [...new Set((f.dom_marks || []).map((m) => MARK_LABELS[m]).filter(Boolean))];
  return `
    <div class="frame__dom">
      <div class="frame__dom-state ${side ? `is-${side}` : ""}">${escapeHtml(pop || "—")}</div>
      <div class="frame__dom-board">${escapeHtml(f.dom_center_box || "—")}</div>
      ${
        marks.length
          ? `<div class="frame__dom-marks">${marks
              .map((m) => `<span>${escapeHtml(m)}</span>`)
              .join("")}</div>`
          : ""
      }
    </div>`;
}

function scorePair(obj) {
  if (!obj || typeof obj !== "object") return "?-?";
  return `${obj.home ?? "?"}-${obj.away ?? "?"}`;
}

function reversalKey(ev) {
  return `${ev?.match_id || ""}|${ev?.ts || ""}|${ev?.event_key || ""}`;
}

export function ensureFilterUi() {
  const bar = $("filterBar");
  if (bar && !bar.dataset.ready) {
    bar.innerHTML = GOAL_FILTERS.map(
      (f) =>
        `<button type="button" class="filter__btn${f.id === "all" ? " is-active" : ""}" data-filter="${escapeHtml(f.id)}">${escapeHtml(f.short)}</button>`,
    ).join("");
    bar.dataset.ready = "1";
  }
  const pills = $("verdictPills");
  if (pills && !pills.dataset.ready) {
    pills.innerHTML = GOAL_FILTERS.filter((f) => f.id !== "all")
      .map(
        (f) =>
          `<button type="button" class="pill pill--muted pill--filter" data-filter="${escapeHtml(f.id)}" id="pillFilter-${escapeHtml(f.id)}">${escapeHtml(f.short)} —</button>`,
      )
      .join("");
    pills.dataset.ready = "1";
  }
}

/** @param {Array} goals */
export function filterGoals(goals) {
  return (goals || []).filter((g) => goalMatchesFilter(g, state.filter));
}

export function syncFilterUi() {
  ensureFilterUi();
  document.querySelectorAll(".filter__btn, .pill--filter").forEach((el) => {
    el.classList.toggle("is-active", el.getAttribute("data-filter") === state.filter);
  });
}

export function setFilter(next, { toggle = false } = {}) {
  if (!isGoalFilter(next)) return;
  if (toggle && state.filter === next && next !== "all") {
    state.filter = "all";
  } else {
    state.filter = next;
  }
  syncFilterUi();
  render(state.goals);
}

export function renderMeta(snap) {
  state.lastMeta = snap;
  ensureFilterUi();
  const statusPill = $("pillStatus");
  statusPill.textContent = "Watching jsonl";
  statusPill.className = "pill is-ok";
  $("pillGoals").textContent = `Goals ${snap.goal_count ?? 0}`;
  $("pillGate").textContent = `Gate ${snap.gate_goal_count ?? 0}`;
  const counts = countByFilter(snap.goals || []);
  for (const f of GOAL_FILTERS) {
    if (f.id === "all") continue;
    const n = counts[f.id] ?? 0;
    const countPill = $(`pillFilter-${f.id}`);
    if (countPill) {
      countPill.textContent = `${f.short} ${n}`;
      countPill.classList.toggle("is-rev", f.id === "reversed" && n > 0);
      countPill.classList.toggle("is-rev-hard", f.id === "reversed_after_in_play" && n > 0);
      countPill.classList.toggle("is-obs", f.id === "reversal_observe" && n > 0);
    }
    const btn = document.querySelector(`.filter__btn[data-filter="${f.id}"]`);
    if (btn) btn.textContent = `${f.short} ${n}`;
  }
  const allBtn = document.querySelector('.filter__btn[data-filter="all"]');
  if (allBtn) allBtn.textContent = `全部 ${counts.all ?? 0}`;
  const latest = snap.goals?.[0]?.dqd_ts;
  $("pillLatest").textContent = latest ? `Latest ${formatBeijing(latest)}` : "No goals yet";
  syncFilterUi();
}

export function pushReversalToast(ev) {
  if (!ev) return;
  const key = reversalKey(ev);
  if (state.seenReversalKeys.has(key)) return;
  state.seenReversalKeys.add(key);

  const stack = $("toasts");
  if (!stack) return;
  const el = document.createElement("div");
  el.className = "toast";
  const prev = scorePair(ev.prev);
  const curr = scorePair(ev.curr);
  el.innerHTML = `
    <div class="toast__badge">回撤</div>
    <div>
      <div class="toast__title">${escapeHtml(ev.home || "?")} ${escapeHtml(prev)}→${escapeHtml(curr)} ${escapeHtml(ev.away || "?")}</div>
      <div class="toast__meta">
        match ${escapeHtml(ev.match_id || "")}
        ${ev.league ? ` · ${escapeHtml(ev.league)}` : ""}
        ${ev.ts ? ` · ${escapeHtml(formatBeijing(ev.ts))}` : ""}
        · 门控已取消
      </div>
    </div>`;
  stack.prepend(el);
  setTimeout(() => {
    el.classList.add("is-out");
    setTimeout(() => el.remove(), 280);
  }, 7000);
}

export function consumeReversals(list, { toast = true } = {}) {
  for (const ev of list || []) {
    const key = reversalKey(ev);
    if (!state.seededReversals) {
      state.seenReversalKeys.add(key);
      continue;
    }
    if (toast) pushReversalToast(ev);
    else state.seenReversalKeys.add(key);
  }
  if (!state.seededReversals) state.seededReversals = true;
}

export function renderRail(goals) {
  const rail = $("goalRail");
  if (!goals.length) {
    const emptyMsg = emptyFilterMessage(state.filter);
    rail.innerHTML = `<div class="empty" style="padding:20px">${emptyMsg}</div>`;
    return;
  }
  rail.innerHTML = goals
    .map((g) => {
      const active = g.event_key === state.selectedKey ? "is-active" : "";
      const afterIp = reversedAfterInPlay(g);
      const revObs = isReversalObserve(g);
      const revClass = revObs
        ? "is-reversal-observe"
        : g.reversed
          ? afterIp
            ? "is-reversed is-reversed-after-inplay"
            : "is-reversed"
          : "";
      const vKey = goalVerdictKey(g);
      const title = `${escapeHtml(g.home || "?")} ${escapeHtml(scoreLabel(g))} ${escapeHtml(g.away || "?")}`;
      return `
        <button type="button" class="goal-item ${active} ${revClass}" data-key="${escapeHtml(g.event_key)}">
          <p class="goal-item__title">${title}</p>
          <div class="goal-item__meta">
            <span class="${stateBadgeClass(vKey)}">${escapeHtml(stateLabel(vKey))}</span>
            <span>${g.frame_count || 0} frames</span>
            ${
              g.af_frame_count
                ? `<span class="goal-item__af">AF ${g.af_match_count || 0}/${g.af_frame_count}</span>`
                : ""
            }
            ${
              g.nami_in_play?.zone
                ? `<span class="goal-item__nami">${escapeHtml(NAMI_ZONE_LABELS[g.nami_in_play.zone] || g.nami_in_play.zone)}</span>`
                : g.nami_dom_points
                  ? `<span class="goal-item__nami">球位 ${g.nami_dom_points}</span>`
                  : ""
            }
            <span>${g.gate ? (revObs ? "observe" : "gate") : "observe"}</span>
            <span>${escapeHtml(formatBeijing(g.dqd_ts))}</span>
          </div>
        </button>`;
    })
    .join("");

  rail.querySelectorAll(".goal-item").forEach((btn) => {
    btn.addEventListener("click", () => {
      state.selectedKey = btn.getAttribute("data-key");
      render(state.goals);
    });
  });
}

export function renderDetail(goal) {
  const board = $("board");
  if (!goal) {
    const empty = emptyFilterMessage(state.filter, { detail: true });
    board.innerHTML = `<div class="empty">${empty}</div>`;
    return;
  }

  const frames = goal.frames || [];
  const afFrames = goal.af_frames || [];
  const title = `${escapeHtml(goal.home || "?")} ${escapeHtml(scoreLabel(goal))} ${escapeHtml(goal.away || "?")}`;
  const rev = goal.reversal;
  const afterIp = reversedAfterInPlay(goal);
  const revObs = isReversalObserve(goal);
  const vKey = goalVerdictKey(goal);
  let revLine = "";
  if (revObs) {
    const from = goal.score_from || "?-?";
    const to = goal.score_to || scoreLabel(goal);
    revLine = `<p class="detail__rev detail__rev--observe">回撤观察 · 期望比分 ${escapeHtml(String(to))}（懂球帝 ${escapeHtml(String(from))}→${escapeHtml(String(to))}）· AF/DOM score_match 对照该比分</p>`;
  } else if (goal.reversed && rev) {
    const revCls = afterIp ? "detail__rev detail__rev--after-inplay" : "detail__rev";
    if (rev.source === "dqd_reversal") {
      revLine = `<p class="${revCls}">比分回撤 ${escapeHtml(scorePair(rev.prev))} → ${escapeHtml(scorePair(rev.curr))} · 门控已取消 · ${escapeHtml(formatBeijing(rev.ts))}${
        afterIp ? " · 曾判定 in_play" : ""
      }</p>`;
    } else {
      revLine = `<p class="${revCls}">门控取消 · ${escapeHtml(rev.reason || rev.mode || "canceled")} · ${escapeHtml(formatBeijing(rev.ts))}${
        afterIp ? " · 曾判定 in_play" : ""
      }</p>`;
    }
  }
  if (goal.linked_event_key) {
    const linkLabel = revObs ? "对照进球卡" : "对照回撤观察";
    revLine += `<p class="detail__link-wrap"><button type="button" class="detail__link" data-link-key="${escapeHtml(goal.linked_event_key)}">${escapeHtml(linkLabel)}</button></p>`;
  }

  const afHtml = afFrames.length
    ? `<section class="af-trail">
        <div class="af-trail__head">
          <h3>API-Football observe</h3>
          <span>${afFrames.length} samples · ≤90s @ 5s
          ${
            goal.af_first_match_elapsed_s != null
              ? ` · match @ t+${escapeHtml(String(goal.af_first_match_elapsed_s))}s`
              : ""
          }</span>
        </div>
        <div class="af-trail__row">
          ${afFrames
            .map((f) => {
              const matchCls =
                f.score_match === true
                  ? "is-match"
                  : f.score_match === false
                    ? "is-miss"
                    : f.ok
                      ? ""
                      : "is-err";
              const score = f.af_score || (f.ok ? "?-?" : "—");
              return `<div class="af-chip ${matchCls}" title="${escapeHtml(
                f.error || f.af_home || "",
              )}">
                <span class="af-chip__t">t+${escapeHtml(String(f.elapsed_s ?? "?"))}</span>
                <span class="af-chip__s">${escapeHtml(String(score))}</span>
                <span class="af-chip__m">${
                  f.score_match === true
                    ? "OK"
                    : f.score_match === false
                      ? "≠"
                      : f.ok
                        ? "…"
                        : "err"
                }</span>
              </div>`;
            })
            .join("")}
        </div>
      </section>`
    : "";

  const framesHtml = frames.length
    ? `<div class="frames">${frames
        .map((f) => {
          const ps = playStateOf(f);
          const conf =
            f.judge?.confidence != null ? Number(f.judge.confidence).toFixed(2) : "—";
          const evidence = Array.isArray(f.judge?.evidence)
            ? f.judge.evidence.slice(0, 3).join(" · ")
            : "";
          const visual = [
            f.dom_pop_box || f.dom_center_box ? renderDomCard(f) : "",
            f.thumb_url
              ? `<div class="frame__img-wrap"><img src="${escapeHtml(f.thumb_url)}" alt="frame ${f.sample_i}" loading="lazy" /></div>`
              : "",
          ]
            .filter(Boolean)
            .join("") ||
            `<div class="frame__img-wrap"><div class="frame__placeholder">${escapeHtml(
              f.error || "no reading",
            )}</div></div>`;
          return `
            <article class="frame ${ps === "in_play" ? "is-in_play" : ""}">
              ${visual}
              <div class="frame__body">
                <div class="frame__row">
                  <span class="frame__elapsed">t+${escapeHtml(String(f.elapsed_s ?? "?"))}s</span>
                  <span class="${stateBadgeClass(ps)}">${escapeHtml(stateLabel(ps))}</span>
                </div>
                <div class="frame__row">
                  <span>#${escapeHtml(String(f.dom_seq ?? f.sample_i ?? "?"))}${
                    f.nami?.zone
                      ? ` · ${escapeHtml(namiZoneLabel(f.nami.zone))}`
                      : ""
                  }</span>
                  <span>conf ${escapeHtml(conf)}</span>
                </div>
                ${
                  evidence
                    ? `<p class="frame__evidence">${escapeHtml(evidence)}</p>`
                    : f.judge?.stopped_reason
                      ? `<p class="frame__evidence">${escapeHtml(f.judge.stopped_reason)}</p>`
                      : ""
                }
              </div>
            </article>`;
        })
        .join("")}</div>`
    : `<div class="empty">该球尚无采样。</div>`;

  board.innerHTML = `
    <div class="detail__head">
      <div>
        <h2 class="detail__title">${title}</h2>
        <p class="detail__sub">
          match ${escapeHtml(goal.match_id)} · ${escapeHtml(goal.event_key)} ·
          ${revObs ? "回撤观察" : goal.gate ? "pitch-gate" : "observe"} ·
          ${frames.length} frames
          ${
            afFrames.length
              ? ` · AF ${goal.af_match_count || 0}/${afFrames.length}`
              : ""
          }
          ${
            goal.in_play_elapsed_s != null
              ? ` · in_play @ t+${escapeHtml(String(goal.in_play_elapsed_s))}s`
              : ""
          }
        </p>
        ${revLine}
      </div>
      <span class="${stateBadgeClass(vKey)}">${escapeHtml(stateLabel(vKey))}</span>
    </div>
    ${afHtml}
    ${renderNamiTrail(goal)}
    ${framesHtml}`;

  board.querySelectorAll("[data-link-key]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const key = btn.getAttribute("data-link-key");
      if (!key) return;
      const target = (state.goals || []).find((g) => g.event_key === key);
      if (!target) return;
      if (!goalMatchesFilter(target, state.filter)) {
        state.filter = "all";
      }
      state.selectedKey = key;
      render(state.goals);
    });
  });
}

export function render(goals) {
  state.goals = goals || [];
  const visible = filterGoals(state.goals);
  if (state.selectedKey && !visible.some((g) => g.event_key === state.selectedKey)) {
    state.selectedKey = visible[0]?.event_key || null;
  }
  if (!state.selectedKey && visible.length) {
    state.selectedKey = visible[0].event_key;
  }
  syncFilterUi();
  renderRail(visible);
  const selected = visible.find((g) => g.event_key === state.selectedKey) || null;
  renderDetail(selected);
}
