const $ = (id) => document.getElementById(id);

async function fetchStatus() {
  const r = await fetch("/api/status", { cache: "no-store" });
  if (!r.ok) throw new Error(`status ${r.status}`);
  return r.json();
}

function render(st) {
  const quoteUp = !!st.quote?.running;
  $("pillQuote").textContent = quoteUp ? "Quote skill up" : "Quote skill down";
  $("pillQuote").className = `pill ${quoteUp ? "ok" : "bad"}`;

  const trade = st.quote?.trade || {};
  const mode = trade.mode || "off";
  const depth = trade.take_depth || "top";
  if (mode === "off") {
    $("pillTrade").textContent = "Trade off";
    $("pillTrade").className = "pill muted";
  } else if (mode === "live") {
    $("pillTrade").textContent = `Trade LIVE · ${depth} · $${trade.max_usdc ?? "?"}`;
    $("pillTrade").className = "pill ok";
  } else {
    $("pillTrade").textContent = `Trade dry-run · ${depth} · $${trade.max_usdc ?? "?"}`;
    $("pillTrade").className = "pill ok";
  }

  const afBoard = (st.boards || []).find((b) => b.id === "af-bridge-board");
  const afRef = st.quote?.af_referee !== false;
  if (!afBoard?.up) {
    $("pillAf").textContent = "AF board down";
    $("pillAf").className = "pill bad";
  } else if (afBoard.skill_running) {
    $("pillAf").textContent = afRef
      ? `AF watch · referee on · ${afBoard.entry_count ?? "?"} mapped`
      : `AF watch · referee off · ${afBoard.entry_count ?? "?"} mapped`;
    $("pillAf").className = "pill ok";
  } else {
    $("pillAf").textContent = "AF watch idle";
    $("pillAf").className = "pill bad";
  }

  const boards = st.boards || [];
  const upN = boards.filter((b) => b.up).length;
  $("pillBoards").textContent = `Boards ${upN}/${boards.length}`;
  $("pillBoards").className = `pill ${upN === boards.length ? "ok" : "bad"}`;
  $("pillStarted").textContent = st.started_at ? `Since ${st.started_at}` : "Starting…";

  const hub = {
    id: "main",
    name: "System Main",
    skill: "orchestrator",
    url: st.hub,
    up: true,
  };

  const items = [hub, ...boards];
  $("grid").innerHTML = items
    .map((b) => {
      const detail =
        b.id === "bridge-board" && b.skill_running != null
          ? ` · bridge skill ${b.skill_running ? "running" : "idle"}` +
            (b.dqd_ticks != null ? ` · DQD ${b.dqd_ticks}` : "") +
            (b.pm_ticks != null ? ` · PM ${b.pm_ticks}` : "")
          : b.id === "af-bridge-board" && b.skill_running != null
            ? ` · af watch ${b.skill_running ? "running" : "idle"}` +
              (b.entry_count != null ? ` · ${b.entry_count} mapped` : "") +
              (b.unresolved_count != null ? ` · ${b.unresolved_count} unresolved` : "")
            : "";
      return `
      <a class="card ${b.up ? "" : "is-down"}" href="${b.url}" target="_blank" rel="noreferrer">
        <div>
          <h2>${b.name || b.id}</h2>
          <p class="skill">${b.skill || ""}${detail}</p>
        </div>
        <div class="go">${b.up ? "OPEN →" : "DOWN"}</div>
      </a>`;
    })
    .join("");
}

async function tick() {
  try {
    render(await fetchStatus());
  } catch {
    $("pillQuote").textContent = "Hub unreachable";
    $("pillQuote").className = "pill bad";
  }
}

tick();
setInterval(tick, 2000);
