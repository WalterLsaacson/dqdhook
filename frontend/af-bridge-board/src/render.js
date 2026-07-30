import { state } from "./state.js";
import {
  $,
  escapeHtml,
  groupMatches,
  leagueColor,
  formatBeijingDateTime,
  goalsText,
  statusShort,
} from "./utils.js";

export function renderRail(onFilter) {
  const rail = $("leagueRail");
  const groups = groupMatches(state.matches, null);
  const chips = [
    `<button class="league-chip ${!state.filterLeagueId && !state.showUnresolved ? "is-active" : ""}" data-league="" type="button">
      <span class="league-chip__dot" style="--dot:#38bdf8;background:#38bdf8"></span>
      <span class="league-chip__name">All mapped</span>
      <span class="league-chip__count">${state.matches.length}</span>
    </button>`,
    `<button class="league-chip ${state.showUnresolved ? "is-active" : ""}" data-unresolved="1" type="button">
      <span class="league-chip__dot" style="--dot:#fb7185;background:#fb7185"></span>
      <span class="league-chip__name">Unresolved</span>
      <span class="league-chip__count">${state.unresolved.length}</span>
    </button>`,
  ];
  for (const g of groups) {
    chips.push(`
      <button class="league-chip ${!state.showUnresolved && state.filterLeagueId === g.id ? "is-active" : ""}" data-league="${escapeHtml(g.id)}" type="button">
        <span class="league-chip__dot" style="--dot:${g.color};background:${g.color}"></span>
        <span class="league-chip__name">${escapeHtml(g.name)}</span>
        <span class="league-chip__count">${g.matches.length}</span>
      </button>
    `);
  }
  rail.innerHTML = chips.join("");
  rail.querySelectorAll(".league-chip").forEach((btn) => {
    btn.addEventListener("click", () => {
      if (btn.getAttribute("data-unresolved") === "1") {
        onFilter({ unresolved: true });
        return;
      }
      const id = btn.getAttribute("data-league") || "";
      onFilter({ leagueId: id || null });
    });
  });
}

function rowHtml(row) {
  const color = leagueColor(row);
  const dqdHome = row.dqd_home || "";
  const dqdAway = row.dqd_away || "";
  const afHome = row.af_home || "";
  const afAway = row.af_away || "";
  const score = Number(row.name_score);
  const scoreLabel = Number.isFinite(score) ? score.toFixed(2) : "—";
  const skew = row.skew_min != null ? `${row.skew_min}m` : "—";
  const goals = goalsText(row.af_goals);
  const st = statusShort(row.af_status);
  const pm = row.bridge?.polymarket || {};
  const kickoff =
    row.kickoff_beijing ||
    row.bridge?.kickoff_beijing ||
    "";

  return `
    <article class="match-card" data-id="${escapeHtml(row.dqd_match_id || "")}">
      <div class="match-card__top">
        <div class="kickoff">${escapeHtml(kickoff || "—")}</div>
        <div class="league-tag" style="--tag:${color}">
          <span class="league-tag__dot"></span>
          <span>${escapeHtml(row.af_league || "")}</span>
        </div>
        <div class="score-badge" title="name similarity">map ${escapeHtml(scoreLabel)}</div>
      </div>

      <div class="match-card__teams">
        <div class="team team--home">
          <div class="team__name">${escapeHtml(dqdHome)}</div>
        </div>
        <div class="score score--map">
          <div class="score__main">${escapeHtml(goals)}</div>
          <div class="score__half">映射快照 · ${escapeHtml(st)}</div>
        </div>
        <div class="team team--away">
          <div class="team__name">${escapeHtml(dqdAway)}</div>
        </div>
      </div>

      <div class="af-map">
        <div class="af-map__row">
          <span class="af-map__label">AF</span>
          <span class="af-map__teams">${escapeHtml(afHome)} · ${escapeHtml(afAway)}</span>
        </div>
        <div class="af-map__row">
          <span class="af-map__label">IDs</span>
          <span class="af-map__ids">
            DQD <code>${escapeHtml(row.dqd_match_id || "")}</code>
            · AF <code>${escapeHtml(String(row.af_fixture_id || ""))}</code>
            · skew ${escapeHtml(skew)}
          </span>
        </div>
      </div>

      <div class="match-card__foot">
        ${
          pm.url
            ? `<a class="pm-link" href="${escapeHtml(pm.url)}" target="_blank" rel="noopener">Polymarket · ${escapeHtml(pm.slug || pm.event_id || "event")}</a>`
            : `<span class="pm-id">no bridge PM link</span>`
        }
        <code class="pm-id" title="matched_at">${escapeHtml(row.matched_at || "")}</code>
      </div>
    </article>
  `;
}

function unresolvedHtml(u) {
  return `
    <article class="match-card is-unresolved">
      <div class="match-card__top">
        <div class="kickoff">${escapeHtml(u.tried_at || "—")}</div>
        <div class="league-tag" style="--tag:#fb7185">
          <span class="league-tag__dot"></span>
          <span>${escapeHtml(u.dqd_league || "unresolved")}</span>
        </div>
        <div class="score-badge">${escapeHtml(u.reason || "no_af_fixture")}</div>
      </div>
      <div class="match-card__teams">
        <div class="team team--home"><div class="team__name">${escapeHtml(u.dqd_home || "")}</div></div>
        <div class="score"><div class="score__main">vs</div></div>
        <div class="team team--away"><div class="team__name">${escapeHtml(u.dqd_away || "")}</div></div>
      </div>
      <div class="match-card__foot">
        <code class="pm-id">DQD ${escapeHtml(u.dqd_match_id || "")}</code>
      </div>
    </article>
  `;
}

export function renderBoard() {
  const board = $("board");
  if (state.showUnresolved) {
    if (!state.unresolved.length) {
      board.innerHTML = `<div class="empty">暂无 unresolved（6h TTL 内未找到 AF fixture）。</div>`;
      return;
    }
    board.innerHTML = `
      <section class="league-block" style="--league:#fb7185">
        <header class="league-head">
          <span class="league-head__bar"></span>
          <div class="league-head__name">Unresolved</div>
          <div class="league-head__meta">${state.unresolved.length} pending</div>
        </header>
        <div class="league-body">
          ${state.unresolved.map(unresolvedHtml).join("")}
        </div>
      </section>
    `;
    return;
  }

  const groups = groupMatches(state.matches, state.filterLeagueId);
  if (!groups.length) {
    board.innerHTML = `<div class="empty">暂无 AF 映射。确认 bridge 有场次，或点 Sync once / Start watch。</div>`;
    return;
  }
  board.innerHTML = groups
    .map(
      (g, idx) => `
      <section class="league-block" style="--league:${g.color}; animation-delay:${idx * 40}ms">
        <header class="league-head">
          <span class="league-head__bar"></span>
          <div class="league-head__name">${escapeHtml(g.name)}</div>
          <div class="league-head__meta">${g.matches.length} mapped</div>
        </header>
        <div class="league-body">
          ${g.matches.map(rowHtml).join("")}
        </div>
      </section>
    `,
    )
    .join("");
}

export function renderMeta(snap) {
  const stats = snap?.stats || {};
  $("pillCount").textContent = `${state.matches.length} mapped`;
  $("pillUnresolved").textContent = `Unresolved ${state.unresolved.length}`;
  $("pillUnresolved").classList.toggle("is-warn", state.unresolved.length > 0);
  $("pillBridge").textContent = `Bridge ${snap?.bridge_count ?? "—"}`;
  const parts = [];
  if (stats.cache_hits != null) parts.push(`hit ${stats.cache_hits}`);
  if (stats.resolved != null) parts.push(`new ${stats.resolved}`);
  if (stats.unresolved_new != null) parts.push(`miss ${stats.unresolved_new}`);
  if (stats.skipped_ttl != null) parts.push(`ttl ${stats.skipped_ttl}`);
  $("pillStats").textContent = parts.length ? parts.join(" · ") : "—";
  $("pillFetched").textContent = snap?.last_sync_at || snap?.matched_at
    ? `同步 ${formatBeijingDateTime(snap.last_sync_at || snap.matched_at)}`
    : "—";
  $("pillStatus").textContent = state.running ? "Watch running" : "Idle";
  $("pillStatus").classList.toggle("is-live", state.running);
  $("btnStart").disabled = state.running;
  $("btnStop").disabled = !state.running;
}

export function render(snap) {
  state.lastMeta = snap || state.lastMeta;
  if (snap) {
    state.matches = snap.matches || [];
    state.unresolved = snap.unresolved || [];
  }
  renderRail((sel) => {
    if (sel?.unresolved) {
      state.showUnresolved = true;
      state.filterLeagueId = null;
    } else {
      state.showUnresolved = false;
      state.filterLeagueId = sel?.leagueId ?? null;
    }
    render(state.lastMeta);
  });
  renderBoard();
  renderMeta(snap || state.lastMeta);
}
