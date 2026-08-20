import { state } from "./state.js";
import { $, escapeHtml, formatBeijing, scoreLabel, stateBadgeClass } from "./utils.js";

function playStateOf(frame) {
  if (frame?.ok === false) return "capture_failed";
  return String(frame?.judge?.play_state || "pending_judge");
}

function scorePair(obj) {
  if (!obj || typeof obj !== "object") return "?-?";
  return `${obj.home ?? "?"}-${obj.away ?? "?"}`;
}

function reversalKey(ev) {
  return `${ev?.match_id || ""}|${ev?.ts || ""}|${ev?.event_key || ""}`;
}

export function renderMeta(snap) {
  state.lastMeta = snap;
  const pill = $("pillStatus");
  pill.textContent = "Watching jsonl";
  pill.className = "pill is-ok";
  $("pillGoals").textContent = `Goals ${snap.goal_count ?? 0}`;
  $("pillGate").textContent = `Gate ${snap.gate_goal_count ?? 0}`;
  $("pillInPlay").textContent = `in_play ${snap.in_play_count ?? 0}`;
  const revN = snap.reversed_count ?? 0;
  const pillRev = $("pillReversed");
  if (pillRev) {
    pillRev.textContent = `回撤 ${revN}`;
    pillRev.classList.toggle("is-rev", revN > 0);
  }
  const latest = snap.goals?.[0]?.dqd_ts;
  $("pillLatest").textContent = latest ? `Latest ${formatBeijing(latest)}` : "No goals yet";
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
    rail.innerHTML = `<div class="empty" style="padding:20px">暂无进球截图。<br/>等 DQD goal + pitch-gate 抓帧。</div>`;
    return;
  }
  rail.innerHTML = goals
    .map((g) => {
      const active = g.event_key === state.selectedKey ? "is-active" : "";
      const revClass = g.reversed ? "is-reversed" : "";
      const title = `${escapeHtml(g.home || "?")} ${escapeHtml(scoreLabel(g))} ${escapeHtml(g.away || "?")}`;
      return `
        <button type="button" class="goal-item ${active} ${revClass}" data-key="${escapeHtml(g.event_key)}">
          <p class="goal-item__title">${title}</p>
          <div class="goal-item__meta">
            <span class="${stateBadgeClass(g.verdict)}">${escapeHtml(g.verdict)}</span>
            <span>${g.frame_count || 0} frames</span>
            <span>${g.gate ? "gate" : "observe"}</span>
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
    board.innerHTML = `<div class="empty">选择左侧一场进球查看截图判定。</div>`;
    return;
  }

  const frames = goal.frames || [];
  const title = `${escapeHtml(goal.home || "?")} ${escapeHtml(scoreLabel(goal))} ${escapeHtml(goal.away || "?")}`;
  const rev = goal.reversal;
  let revLine = "";
  if (goal.reversed && rev) {
    if (rev.source === "dqd_reversal") {
      revLine = `<p class="detail__rev">比分回撤 ${escapeHtml(scorePair(rev.prev))} → ${escapeHtml(scorePair(rev.curr))} · 门控已取消 · ${escapeHtml(formatBeijing(rev.ts))}</p>`;
    } else {
      revLine = `<p class="detail__rev">门控取消 · ${escapeHtml(rev.reason || rev.mode || "canceled")} · ${escapeHtml(formatBeijing(rev.ts))}</p>`;
    }
  }

  const framesHtml = frames.length
    ? `<div class="frames">${frames
        .map((f) => {
          const ps = playStateOf(f);
          const conf =
            f.judge?.confidence != null ? Number(f.judge.confidence).toFixed(2) : "—";
          const evidence = Array.isArray(f.judge?.evidence)
            ? f.judge.evidence.slice(0, 3).join(" · ")
            : "";
          const img = f.thumb_url
            ? `<img src="${escapeHtml(f.thumb_url)}" alt="frame ${f.sample_i}" loading="lazy" />`
            : `<div class="frame__placeholder">${escapeHtml(f.error || "no frame")}</div>`;
          return `
            <article class="frame ${ps === "in_play" ? "is-in_play" : ""}">
              <div class="frame__img-wrap">${img}</div>
              <div class="frame__body">
                <div class="frame__row">
                  <span class="frame__elapsed">t+${escapeHtml(String(f.elapsed_s ?? "?"))}s</span>
                  <span class="${stateBadgeClass(ps)}">${escapeHtml(ps)}</span>
                </div>
                <div class="frame__row">
                  <span>#${escapeHtml(String(f.sample_i ?? "?"))}</span>
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
    : `<div class="empty">该球尚无截帧。</div>`;

  board.innerHTML = `
    <div class="detail__head">
      <div>
        <h2 class="detail__title">${title}</h2>
        <p class="detail__sub">
          match ${escapeHtml(goal.match_id)} · ${escapeHtml(goal.event_key)} ·
          ${goal.gate ? "pitch-gate" : "observe"} ·
          ${frames.length} frames
          ${
            goal.in_play_elapsed_s != null
              ? ` · in_play @ t+${escapeHtml(String(goal.in_play_elapsed_s))}s`
              : ""
          }
        </p>
        ${revLine}
      </div>
      <span class="${stateBadgeClass(goal.verdict)}">${escapeHtml(goal.verdict)}</span>
    </div>
    ${framesHtml}`;
}

export function render(goals) {
  state.goals = goals || [];
  if (!state.selectedKey && state.goals.length) {
    state.selectedKey = state.goals[0].event_key;
  }
  if (state.selectedKey && !state.goals.some((g) => g.event_key === state.selectedKey)) {
    state.selectedKey = state.goals[0]?.event_key || null;
  }
  renderRail(state.goals);
  const selected = state.goals.find((g) => g.event_key === state.selectedKey) || null;
  renderDetail(selected);
}
