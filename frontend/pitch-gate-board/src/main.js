/**
 * @dongqiudi/pitch-gate-board — per-goal screenshot + play_state judgments.
 */
import { state } from "./state.js";
import { $, escapeHtml } from "./utils.js";
import { fetchGoals } from "./api.js";
import { render, renderMeta } from "./render.js";

async function refresh() {
  const snap = await fetchGoals(100);
  renderMeta(snap);
  render(snap.goals || []);
  return snap;
}

function startPolling() {
  stopPolling();
  state.pollTimer = setInterval(async () => {
    try {
      await refresh();
    } catch (e) {
      console.error(e);
    }
  }, 2500);
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
      $("board").innerHTML = `<div class="empty">Refreshing…</div>`;
      await refresh();
    } catch (e) {
      $("board").innerHTML = `<div class="empty">Failed: ${escapeHtml(e.message)}</div>`;
    }
  });
}

async function init() {
  bind();
  try {
    await refresh();
    startPolling();
  } catch (e) {
    $("board").innerHTML = `<div class="empty">无法连接 pitch-gate-board。<br/>${escapeHtml(e.message)}</div>`;
  }
}

init();
