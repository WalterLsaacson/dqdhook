/**
 * @dongqiudi/bridge-board — frontend for match-bridge skill.
 */
import { state } from "./state.js";
import { $, escapeHtml } from "./utils.js";
import { fetchEvents, fetchMatches, fetchStatus, runOnce, startBridge, stopBridge } from "./api.js";
import { consumeEvents, eventKey, render, renderMeta, tickWallClocks } from "./render.js";

function applySnap(snap) {
  state.matches = snap.matches || [];
  render(snap);
}

async function seedSeenEvents() {
  try {
    const data = await fetchEvents(80);
    for (const ev of data.events || []) {
      state.seenEventKeys.add(eventKey(ev));
    }
  } catch {
    /* ignore */
  }
}

function startClockTick() {
  stopClockTick();
  state.clockTimer = setInterval(tickWallClocks, 1000);
}

function stopClockTick() {
  if (state.clockTimer) {
    clearInterval(state.clockTimer);
    state.clockTimer = null;
  }
}

function startPolling() {
  stopPolling();
  state.pollTimer = setInterval(async () => {
    try {
      if (!state.running) return;
      const st = await fetchStatus();
      state.running = !!st.running;
      // Prefer live matches endpoint (refreshes wall clocks server-side too).
      const snap = await fetchMatches();
      applySnap(snap);
      // FT toasts from the latest rematch tick (also embedded in snap.events).
      consumeEvents(st.last_result?.events || st.last_events || []);
      renderMeta({
        ...(snap || {}),
        dqd_count: st.last_result?.dqd_count ?? snap.dqd_count,
        pm_count: st.last_result?.pm_count ?? snap.pm_count,
        finished_count: st.last_result?.finished_count ?? snap.finished_count,
      });
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
      $("board").innerHTML = `<div class="empty">Refreshing match-bridge…</div>`;
      const snap = await runOnce({ offline: false });
      applySnap(snap);
    } catch (e) {
      $("board").innerHTML = `<div class="empty">Failed: ${escapeHtml(e.message)}</div>`;
    }
  });

  $("btnStart").addEventListener("click", async () => {
    try {
      await ensureBridgeRunning();
    } catch (e) {
      $("board").innerHTML = `<div class="empty">Start failed: ${escapeHtml(e.message)}</div>`;
    }
  });

  $("btnStop").addEventListener("click", async () => {
    try {
      await stopBridge();
      state.running = false;
      stopPolling();
      renderMeta(state.lastMeta);
    } catch (e) {
      alert(`Stop failed: ${e.message}`);
    }
  });
}

async function ensureBridgeRunning() {
  $("board").innerHTML = `<div class="empty">Starting match-bridge (DQD + Polymarket)…</div>`;
  const st = await startBridge();
  state.running = true;
  if (st.last_result?.matches) applySnap(st.last_result);
  else applySnap(await fetchMatches());
  startPolling();
  startClockTick();
  return st;
}

async function init() {
  bind();
  startClockTick();
  try {
    await seedSeenEvents();
    const st = await fetchStatus();
    if (st.running) {
      state.running = true;
      applySnap(await fetchMatches());
      startPolling();
      return;
    }
    // Page open = master switch: pull up DQD + PM + rematch loops.
    await ensureBridgeRunning();
  } catch (e) {
    $("board").innerHTML = `<div class="empty">无法启动 bridge-board / match-bridge。<br/>${escapeHtml(e.message)}</div>`;
  }
}

init();
