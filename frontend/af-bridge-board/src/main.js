/**
 * @dongqiudi/af-bridge-board — frontend for apifootball-bridge skill.
 */
import { state } from "./state.js";
import { $, escapeHtml } from "./utils.js";
import { fetchMatches, fetchStatus, runOnce, startWatch, stopWatch } from "./api.js";
import { render, renderMeta } from "./render.js";

function applySnap(snap) {
  render(snap);
}

function startPolling() {
  stopPolling();
  state.pollTimer = setInterval(async () => {
    try {
      const st = await fetchStatus();
      state.running = !!st.running;
      const snap = await fetchMatches();
      applySnap({
        ...snap,
        stats: st.last_sync_stats || snap.stats,
        last_sync_at: st.last_sync_at || snap.last_sync_at,
        bridge_count: st.bridge_count ?? snap.bridge_count,
      });
      if (st.last_error) {
        $("pillStats").textContent = `err: ${st.last_error}`.slice(0, 80);
        $("pillStats").title = st.last_error;
      }
      if (!state.running) stopPolling();
    } catch (e) {
      console.error(e);
    }
  }, 4000);
}

function stopPolling() {
  if (state.pollTimer) {
    clearInterval(state.pollTimer);
    state.pollTimer = null;
  }
}

function bind() {
  $("btnRefresh").addEventListener("click", async () => {
    try {
      $("board").innerHTML = `<div class="empty">Syncing AF fixtures…</div>`;
      const snap = await runOnce();
      applySnap(snap);
      const st = await fetchStatus();
      state.running = !!st.running;
      renderMeta({ ...snap, ...st });
    } catch (e) {
      $("board").innerHTML = `<div class="empty">Failed: ${escapeHtml(e.message)}</div>`;
    }
  });

  $("btnStart").addEventListener("click", async () => {
    try {
      await ensureRunning();
    } catch (e) {
      $("board").innerHTML = `<div class="empty">Start failed: ${escapeHtml(e.message)}</div>`;
    }
  });

  $("btnStop").addEventListener("click", async () => {
    try {
      await stopWatch();
      state.running = false;
      stopPolling();
      renderMeta(state.lastMeta);
    } catch (e) {
      alert(`Stop failed: ${e.message}`);
    }
  });
}

async function ensureRunning() {
  $("board").innerHTML = `<div class="empty">Starting apifootball-bridge watch…</div>`;
  const st = await startWatch();
  state.running = true;
  applySnap(await fetchMatches());
  renderMeta({ ...(state.lastMeta || {}), ...st });
  startPolling();
  return st;
}

async function init() {
  bind();
  try {
    // Read-only by default: show fixture_cache without burning AF quota.
    const st = await fetchStatus();
    state.running = !!st.running;
    applySnap(await fetchMatches());
    renderMeta({ ...(state.lastMeta || {}), ...st });
    if (state.running) startPolling();
  } catch (e) {
    $("board").innerHTML = `<div class="empty">无法连接 af-bridge-board。<br/>${escapeHtml(e.message)}</div>`;
  }
}

init();
