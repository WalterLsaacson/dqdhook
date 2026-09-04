import { state } from "./state.js";
import {
  $,
  escapeHtml,
  groupMatches,
  scoreText,
  isLive,
  isFinished,
  officialClock,
  wallClockNow,
  leagueColor,
  formatBeijingDateTime,
  shortDate,
  visibleMatches,
} from "./utils.js";

export function renderRail(onFilter) {
  const rail = $("leagueRail");
  const shown = visibleMatches();
  const groups = groupMatches(shown, null);
  const chips = [
    `<button class="league-chip ${state.filterLeagueId ? "" : "is-active"}" data-league="" type="button">
      <span class="league-chip__dot" style="--dot:#2dd4bf;background:#2dd4bf"></span>
      <span class="league-chip__name">All</span>
      <span class="league-chip__count">${shown.length}</span>
    </button>`,
  ];
  for (const g of groups) {
    chips.push(`
      <button class="league-chip ${state.filterLeagueId === g.id ? "is-active" : ""}" data-league="${escapeHtml(g.id)}" type="button">
        <span class="league-chip__dot" style="--dot:${g.color};background:${g.color}"></span>
        <span class="league-chip__name">${escapeHtml(g.name)}</span>
        <span class="league-chip__count">${g.matches.length}</span>
      </button>
    `);
  }
  rail.innerHTML = chips.join("");
  rail.querySelectorAll(".league-chip").forEach((btn) => {
    btn.addEventListener("click", () => {
      const id = btn.getAttribute("data-league") || "";
      onFilter(id || null);
    });
  });
}

function teamLogo(url, name) {
  const src = String(url || "").trim();
  const label = escapeHtml(name || "");
  const initial = escapeHtml((name || "?").trim().charAt(0) || "?");
  if (!src) {
    return `<span class="team__logo team__logo--empty" aria-hidden="true">${initial}</span>`;
  }
  return `<img class="team__logo" src="${escapeHtml(src)}" alt="${label}" loading="lazy" referrerpolicy="no-referrer" data-initial="${initial}" onerror="const s=document.createElement('span');s.className='team__logo team__logo--empty';s.textContent=this.dataset.initial;s.setAttribute('aria-hidden','true');this.replaceWith(s)" />`;
}

function rowHtml(row) {
  const dqd = row.dongqiudi || {};
  const pm = row.polymarket || {};
  const live = isLive(dqd);
  const finished = isFinished(dqd, row);
  const color = leagueColor(row);
  const homeName = pm.home || dqd.home || "";
  const awayName = pm.away || dqd.away || "";
  const half =
    dqd.home_half !== "" || dqd.away_half !== ""
      ? `<div class="score__half">HT ${escapeHtml(dqd.home_half)}-${escapeHtml(dqd.away_half)}</div>`
      : "";
  const period = dqd.period ? `<span class="chip chip--period">${escapeHtml(dqd.period)}</span>` : "";
  const injury = Number(dqd.injury_time || 0);
  const injuryChip =
    live && injury > 0
      ? `<span class="chip chip--injury">+${injury}' 伤停</span>`
      : "";
  const ftChip = finished ? `<span class="chip chip--ft">FT 完场</span>` : "";
  const statusText = finished
    ? "Played · FT"
    : dqd.status || dqd.status_raw || "—";

  return `
    <article class="match-card ${live ? "is-live" : ""} ${finished ? "is-finished" : ""}" data-id="${escapeHtml(dqd.id || "")}" data-ts="${escapeHtml(dqd.match_timestamp || "")}" data-status="${escapeHtml(dqd.status_raw || "")}">
      <div class="match-card__top">
        <div class="kickoff">${escapeHtml(row.kickoff_beijing || `${dqd.local_date || ""} ${dqd.time || ""}`)}</div>
        <div class="league-tag" style="--tag:${color}">
          <span class="league-tag__dot"></span>
          <span>${escapeHtml(pm.league || dqd.league || "")}</span>
        </div>
        ${finished ? `<div class="ft-badge">FT</div>` : ""}
        <div class="score-badge">${escapeHtml((row.match_score ?? 0).toFixed(2))}</div>
      </div>

      <div class="match-card__teams">
        <div class="team team--home">
          <div class="team__name">${escapeHtml(homeName)}</div>
          ${teamLogo(dqd.home_logo, homeName)}
        </div>
        <div class="score ${live ? "is-live" : ""} ${finished ? "is-finished" : ""}">
          <div class="score__main">${escapeHtml(scoreText(dqd))}</div>
          ${half}
        </div>
        <div class="team team--away">
          ${teamLogo(dqd.away_logo, awayName)}
          <div class="team__name">${escapeHtml(awayName)}</div>
        </div>
      </div>

      <div class="progress">
        <div class="progress__item">
          <span class="progress__label">伤停补时</span>
          <span class="progress__value official" data-role="official">${escapeHtml(officialClock(dqd))}</span>
          ${period}${injuryChip}${ftChip}
        </div>
        <div class="progress__item">
          <span class="progress__label">墙钟</span>
          <span class="progress__value wall" data-role="wall">${escapeHtml(wallClockNow(dqd))}</span>
        </div>
        <div class="progress__item progress__item--status">
          <span class="progress__label">状态</span>
          <span class="progress__value ${live ? "is-live" : ""} ${finished ? "is-finished" : ""}">${escapeHtml(statusText)}</span>
        </div>
      </div>

      <div class="match-card__foot">
        <a class="pm-link" href="${escapeHtml(pm.url || "#")}" target="_blank" rel="noopener">
          Polymarket · ${escapeHtml(pm.slug || pm.event_id || "event")}
        </a>
        <code class="pm-id" title="Gamma event id">${escapeHtml(pm.event_id || "")}</code>
      </div>
    </article>
  `;
}

export function renderBoard() {
  const board = $("board");
  const shown = visibleMatches();
  const groups = groupMatches(shown, state.filterLeagueId);
  if (!groups.length) {
    if (state.filterDate) {
      const row = (state.coverageByDate || []).find((r) => r.date === state.filterDate);
      const total = row?.total ?? 0;
      board.innerHTML = `<div class="empty">${escapeHtml(shortDate(state.filterDate))} 暂无配对（Polymarket ${total} 场）。</div>`;
      return;
    }
    board.innerHTML = `<div class="empty">暂无匹配赛程。等待 DQD / Polymarket 轮询，或点 Refresh。</div>`;
    return;
  }
  board.innerHTML = groups
    .map(
      (g, idx) => `
      <section class="league-block" style="--league:${g.color}; animation-delay:${idx * 40}ms">
        <header class="league-head">
          <span class="league-head__bar"></span>
          <div class="league-head__name">${escapeHtml(g.name)}</div>
          <div class="league-head__meta">${g.matches.length} matched</div>
        </header>
        <div class="league-body">
          ${g.matches.map(rowHtml).join("")}
        </div>
      </section>
    `,
    )
    .join("");
}

/** Tick only wall-clock nodes for live rows (1s). */
export function tickWallClocks() {
  document.querySelectorAll(".match-card.is-live").forEach((el) => {
    const ts = Number(el.getAttribute("data-ts") || 0);
    const wall = el.querySelector('[data-role="wall"]');
    if (!wall || !ts) return;
    const elapsed = Math.floor(Date.now() / 1000) - ts;
    const hh = Math.floor(Math.max(0, elapsed) / 3600);
    const mm = Math.floor((Math.max(0, elapsed) % 3600) / 60);
    const ss = Math.max(0, elapsed) % 60;
    wall.textContent =
      hh > 0
        ? `${String(hh).padStart(2, "0")}:${String(mm).padStart(2, "0")}:${String(ss).padStart(2, "0")}`
        : `${String(mm).padStart(2, "0")}:${String(ss).padStart(2, "0")}`;
  });
}

export function eventKey(ev) {
  return `${ev?.type || ""}|${ev?.match_id || ""}|${ev?.ts || ""}`;
}

export function pushToast(ev) {
  if (!ev) return;
  const key = eventKey(ev);
  if (state.seenEventKeys.has(key)) return;

  const isFt = ev.type === "match_finished";
  const isRev = ev.type === "score_change" && !!ev.is_reversal;
  if (!isFt && !isRev) return;
  state.seenEventKeys.add(key);

  const stack = $("toasts");
  if (!stack) return;
  const el = document.createElement("div");
  el.className = isRev ? "toast toast--rev" : "toast toast--ft";
  let score;
  let badge;
  let metaExtra;
  if (isRev) {
    const prev = ev.prev || {};
    const curr = ev.curr || {};
    score = `${prev.home ?? "?"}-${prev.away ?? "?"} → ${curr.home ?? "?"}-${curr.away ?? "?"}`;
    badge = "回撤";
    metaExtra = "比分回撤 · 门控取消";
  } else {
    score =
      ev.home_score != null && ev.away_score != null
        ? `${ev.home_score} - ${ev.away_score}`
        : "FT";
    badge = "FT";
    metaExtra = "完场";
  }
  el.innerHTML = `
    <div class="toast__badge">${badge}</div>
    <div class="toast__body">
      <div class="toast__title">${escapeHtml(ev.home || "")} ${escapeHtml(score)} ${escapeHtml(ev.away || "")}</div>
      <div class="toast__meta">${escapeHtml(ev.league || "")} · ${metaExtra}${ev.ts ? ` · ${escapeHtml(formatBeijingDateTime(ev.ts))}` : ""}</div>
    </div>
  `;
  stack.prepend(el);
  setTimeout(() => {
    el.classList.add("is-out");
    setTimeout(() => el.remove(), 280);
  }, 6500);
}

export function consumeEvents(events) {
  for (const ev of events || []) pushToast(ev);
}

function coverageTone(matched, total) {
  if (!total) return "empty";
  if (matched >= total) return "full";
  if (matched <= 0) return "empty";
  return "partial";
}

export function renderDateCoverage(snap) {
  const el = $("dateCoverage");
  if (!el) return;
  const rows = Array.isArray(snap?.coverage_by_date)
    ? snap.coverage_by_date
    : state.coverageByDate || [];
  state.coverageByDate = rows;
  if (!rows.length) {
    el.innerHTML = "";
    el.hidden = true;
    return;
  }
  el.hidden = false;
  const sumM = rows.reduce((n, r) => n + (Number(r.matched) || 0), 0);
  const sumT = rows.reduce((n, r) => n + (Number(r.total) || 0), 0);
  const chips = [
    `<button type="button" class="date-chip ${state.filterDate ? "" : "is-active"}" data-date="">
      <span class="date-chip__day">全部</span>
      <span class="date-chip__frac">${sumM} / ${sumT}</span>
      <span class="date-chip__hint">PM 场次</span>
    </button>`,
  ];
  for (const r of rows) {
    const date = String(r.date || "");
    const matched = Number(r.matched) || 0;
    const total = Number(r.total) || 0;
    const tone = coverageTone(matched, total);
    const pct = total ? Math.round((100 * matched) / total) : 0;
    chips.push(`
      <button type="button" class="date-chip date-chip--${tone} ${state.filterDate === date ? "is-active" : ""}" data-date="${escapeHtml(date)}">
        <span class="date-chip__day">${escapeHtml(shortDate(date))}</span>
        <span class="date-chip__frac">${matched} / ${total}</span>
        <span class="date-chip__hint">${pct}%</span>
      </button>
    `);
  }
  el.innerHTML = chips.join("");
  el.querySelectorAll(".date-chip").forEach((btn) => {
    btn.addEventListener("click", () => {
      const id = btn.getAttribute("data-date") || "";
      state.filterDate = id || null;
      render(state.lastMeta);
    });
  });
}

export function renderMeta(snap) {
  const shown = visibleMatches();
  const liveN = shown.filter((r) => isLive(r.dongqiudi)).length;
  const ftN = state.filterDate
    ? shown.filter((r) => isFinished(r.dongqiudi, r)).length
    : snap?.finished_count ??
      state.matches.filter((r) => isFinished(r.dongqiudi, r)).length;
  $("pillCount").textContent = state.filterDate
    ? `${visibleMatches().length} / ${(state.coverageByDate || []).find((r) => r.date === state.filterDate)?.total ?? "—"} · ${shortDate(state.filterDate)}`
    : `${state.matches.length} matched`;
  $("pillLive").textContent = `Live ${liveN}`;
  $("pillLive").classList.toggle("is-live", liveN > 0);
  const pillFt = $("pillFt");
  if (pillFt) {
    pillFt.textContent = `FT ${ftN}`;
    pillFt.classList.toggle("is-ft", ftN > 0);
  }
  $("pillUpstream").textContent = `DQD ${snap?.dqd_count ?? "—"} · PM ${snap?.pm_count ?? "—"}`;
  $("pillFetched").textContent = snap?.matched_at
    ? `更新 ${formatBeijingDateTime(snap.matched_at)}`
    : "—";
  if (state.quoteOwned) {
    $("pillStatus").textContent = "Quote-owned (read-only)";
    $("pillStatus").classList.toggle("is-live", true);
    $("btnStart").disabled = true;
    $("btnStop").disabled = true;
  } else {
    $("pillStatus").textContent = state.running ? "Bridge running" : "Idle";
    $("pillStatus").classList.toggle("is-live", state.running);
    $("btnStart").disabled = state.running;
    $("btnStop").disabled = !state.running;
  }
}

export function render(snap) {
  state.lastMeta = snap || state.lastMeta;
  if (Array.isArray((snap || state.lastMeta)?.coverage_by_date)) {
    state.coverageByDate = (snap || state.lastMeta).coverage_by_date;
  }
  if (snap?.events?.length) consumeEvents(snap.events);
  renderDateCoverage(snap || state.lastMeta);
  renderRail((id) => {
    state.filterLeagueId = id;
    render(state.lastMeta);
  });
  renderBoard();
  renderMeta(snap || state.lastMeta);
}
