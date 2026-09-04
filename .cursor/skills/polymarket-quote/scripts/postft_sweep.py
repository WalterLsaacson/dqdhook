#!/usr/bin/env python3
"""Periodic post-FT locked-WIN ask sweep (24h scan, walk asks ≤0.995).

Scan runs in a **child process** so Gamma/CLOB book I/O cannot stall
pitch-gate. FAK posts are short CLOB worker jobs; they yield if a gate
quote / flatten / rest-cancel is already queued.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_SCRIPTS = Path(__file__).resolve().parent
_LOCKED = _SCRIPTS.parent.parent / "pm-locked-scan" / "scripts"
SCAN_CLI = _LOCKED / "pm_locked_scan.py"
SCAN_TIMEOUT_S = 20 * 60

from rest_ladder import FAK_ZONE_MAX_ASK  # noqa: E402

DEFAULT_ENABLED = True
DEFAULT_HOURS = 24
DEFAULT_INTERVAL_S = 3600.0
DEFAULT_START_DELAY_S = 60.0
DEFAULT_USDC = 1000.0
DEFAULT_MAX_ASK = FAK_ZONE_MAX_ASK

_active_stop: threading.Event | None = None
_active_thread: threading.Thread | None = None
_lock = threading.Lock()


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return bool(default)
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return float(default)
    try:
        return float(raw)
    except (TypeError, ValueError):
        return float(default)


def sweep_enabled() -> bool:
    if not _env_bool("QUOTE_POSTFT_SWEEP", DEFAULT_ENABLED):
        return False
    return postft_usdc() > 1e-12


def postft_usdc() -> float:
    return max(0.0, _env_float("QUOTE_POSTFT_SWEEP_USDC", DEFAULT_USDC))


def sweep_hours() -> int:
    return max(1, int(_env_float("QUOTE_POSTFT_SWEEP_HOURS", float(DEFAULT_HOURS))))


def sweep_interval_s() -> float:
    return max(60.0, _env_float("QUOTE_POSTFT_SWEEP_INTERVAL_S", DEFAULT_INTERVAL_S))


def sweep_start_delay_s() -> float:
    return max(0.0, _env_float("QUOTE_POSTFT_SWEEP_START_DELAY_S", DEFAULT_START_DELAY_S))


def sweep_max_ask() -> float:
    return min(1.0, max(0.01, _env_float("QUOTE_POSTFT_SWEEP_MAX_ASK", DEFAULT_MAX_ASK)))


AF_SCORE_SOURCES = frozenset({"apifootball", "api_football", "score.fulltime"})


def af_regulation_source(score: dict[str, Any] | None) -> bool:
    src = str((score or {}).get("source") or "").strip().lower()
    return src in AF_SCORE_SOURCES


def tradeable_hit(hit: dict[str, Any], *, max_ask: float = DEFAULT_MAX_ASK) -> bool:
    """True when leftover WIN asks exist at or below the FAK cap."""
    try:
        tradable = float(hit.get("tradeable_shares") or 0)
    except (TypeError, ValueError):
        tradable = 0.0
    if tradable > 1e-12:
        return True
    try:
        best = float(hit.get("best_ask"))
    except (TypeError, ValueError):
        return False
    return best <= float(max_ask) + 1e-12


def collect_tradeable_hits(
    payload: dict[str, Any],
    *,
    max_ask: float = DEFAULT_MAX_ASK,
) -> list[dict[str, Any]]:
    """Flatten scan results to tokens with asks ≤ max_ask, cheapest first."""
    cap = float(max_ask)
    rows: list[dict[str, Any]] = []
    for scanned in payload.get("results") or []:
        if not isinstance(scanned, dict):
            continue
        match = scanned.get("match") or {}
        score = scanned.get("score") or {}
        if not af_regulation_source(score):
            continue
        for hit in scanned.get("hits") or []:
            if not isinstance(hit, dict):
                continue
            if not tradeable_hit(hit, max_ask=cap):
                continue
            asks = [
                a
                for a in (hit.get("asks") or [])
                if isinstance(a, dict)
            ]
            cheap = []
            for a in asks:
                try:
                    p = float(a.get("price"))
                    s = float(a.get("size"))
                except (TypeError, ValueError):
                    continue
                if p <= cap + 1e-12 and s > 0:
                    cheap.append({"price": p, "size": s})
            if not cheap:
                continue
            cheap.sort(key=lambda x: x["price"])
            try:
                best = float(hit.get("best_ask") if hit.get("best_ask") is not None else cheap[0]["price"])
            except (TypeError, ValueError):
                best = cheap[0]["price"]
            rows.append(
                {
                    "token_id": str(hit.get("token_id") or ""),
                    "best_ask": best,
                    "asks": cheap,
                    "tick_size": str(hit.get("tick_size") or "") or "0.001",
                    "question": hit.get("question") or "",
                    "family": hit.get("family"),
                    "outcome": hit.get("outcome"),
                    "market_key": hit.get("market_key"),
                    "match": {
                        "id": match.get("id"),
                        "slug": match.get("slug"),
                        "title": match.get("title"),
                        "home": match.get("home"),
                        "away": match.get("away"),
                        "kickoff_beijing": match.get("kickoff_beijing"),
                        "url": match.get("url"),
                        "league": match.get("league"),
                        "league_id": match.get("league_id"),
                    },
                    "score": score,
                }
            )
    rows.sort(
        key=lambda r: (
            float(r.get("best_ask") or 9),
            str(r.get("token_id") or ""),
        )
    )
    return rows


def refresh_quote_book(
    quote: dict[str, Any],
    *,
    proxy: str | None | object = ...,
) -> dict[str, Any]:
    """Replace asks with a live CLOB book. Forces soccer tick + neg_risk=False."""
    import quote_lib as ql

    tid = str(quote.get("token_id") or "")
    out = dict(quote)
    if not tid:
        return out
    books = ql.fetch_books(
        [tid], proxy=proxy, timeout=30.0, top_n=12, sequential_fallback=True
    )
    book = books.get(tid) or {}
    if book.get("book_missing"):
        out["book_missing"] = True
        return out
    out["asks_top"] = book.get("asks_top") or out.get("asks_top")
    out["best_ask"] = book.get("best_ask")
    out["best_ask_size"] = book.get("best_ask_size")
    out["tick_size"] = str(book.get("tick_size") or out.get("tick_size") or "0.001") or "0.001"
    out["neg_risk"] = False
    out["book_missing"] = False
    return out


def hit_to_quote(hit: dict[str, Any]) -> dict[str, Any]:
    asks = hit.get("asks") or []
    best = hit.get("best_ask")
    best_size = None
    if asks:
        try:
            best_size = float(asks[0].get("size"))
        except (TypeError, ValueError, IndexError):
            best_size = None
    return {
        "trade": "buy_win",
        "settlement": "WIN",
        "locked": True,
        "misprice": True,
        "token_id": str(hit.get("token_id") or ""),
        "best_ask": best,
        "best_ask_size": best_size,
        "asks_top": [{"price": str(a.get("price")), "size": str(a.get("size"))} for a in asks],
        "tick_size": str(hit.get("tick_size") or "0.001") or "0.001",
        "neg_risk": False,
        "question": hit.get("question") or "",
        "family": hit.get("family"),
        "outcome": hit.get("outcome"),
        "market_key": hit.get("market_key"),
    }


def match_meta_for_hit(hit: dict[str, Any], *, event_key: str) -> dict[str, Any]:
    m = hit.get("match") or {}
    return {
        "event_type": "postft",
        "event_key": event_key,
        "match_id": str(m.get("id") or m.get("slug") or ""),
        "home": m.get("home"),
        "away": m.get("away"),
        "slug": m.get("slug"),
        "trade_context": {"postft_sweep": True},
    }


def sweep_event_key(cycle_id: str, token_id: str) -> str:
    tid = str(token_id or "").strip()
    return f"postft|{cycle_id}|{tid}"


def scan_cli_cmd(
    *,
    hours: int,
    max_ask: float,
    out_path: Path,
) -> list[str]:
    """Hourly trade sweep: AF regulation only (no Dongqiudi fallback)."""
    return [
        sys.executable,
        str(SCAN_CLI),
        "--hours",
        str(int(hours)),
        "--max-ask",
        str(max_ask),
        "--out",
        str(out_path),
        "--json",
        "--require-af",
    ]


def run_scan_subprocess(
    root: Path,
    *,
    hours: int,
    max_ask: float,
    stop: threading.Event | None = None,
) -> dict[str, Any]:
    """Child-process scan. Does not share this process's CLOB worker or GIL."""
    out_dir = Path(root) / "data" / "pm-locked-scan"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "latest.json"
    cmd = scan_cli_cmd(hours=hours, max_ask=max_ask, out_path=out_path)
    if not SCAN_CLI.is_file():
        raise FileNotFoundError(f"locked-scan CLI missing: {SCAN_CLI}")
    proc = subprocess.Popen(
        cmd,
        cwd=str(root),
        stdout=subprocess.DEVNULL,
        stderr=None,
    )
    deadline = time.monotonic() + SCAN_TIMEOUT_S
    while proc.poll() is None:
        if stop is not None and stop.is_set():
            proc.terminate()
            try:
                proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                proc.kill()
            raise RuntimeError("postft-sweep scan aborted")
        if time.monotonic() > deadline:
            proc.kill()
            raise TimeoutError(f"postft-sweep scan exceeded {SCAN_TIMEOUT_S}s")
        time.sleep(0.4)
    if proc.returncode not in (0, None):
        raise RuntimeError(f"postft-sweep scan exit {proc.returncode}")
    raw = json.loads(out_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise RuntimeError("postft-sweep scan JSON is not an object")
    return raw


def collect_from_scan(
    *,
    root: Path,
    hours: int | None = None,
    max_ask: float | None = None,
    stop: threading.Event | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    h = int(hours if hours is not None else sweep_hours())
    cap = float(max_ask if max_ask is not None else sweep_max_ask())
    payload = run_scan_subprocess(root, hours=h, max_ask=cap, stop=stop)
    return payload, collect_tradeable_hits(payload, max_ask=cap)


def run_once(
    root: Path,
    *,
    executor: Any | None = None,
    worker: Any | None = None,
    proxy: str | None | object = ...,
    progress: Any = None,
    stop: threading.Event | None = None,
) -> dict[str, Any]:
    """Scan 24h locked WIN leftover asks and FAK-walk each token up to 0.995."""
    cycle_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    cap = sweep_max_ask()
    log = progress
    if log:
        print(
            f"postft-sweep → SCAN hours={sweep_hours()} max_ask={cap} "
            f"cycle={cycle_id} (child process)",
            file=log,
            flush=True,
        )
    payload, hits = collect_from_scan(root=root, max_ask=cap, stop=stop)

    submitted = 0
    skipped = 0
    for hit in hits:
        tid = str(hit.get("token_id") or "")
        if not tid:
            skipped += 1
            continue
        key = sweep_event_key(cycle_id, tid)
        quote = hit_to_quote(hit)
        meta = match_meta_for_hit(hit, event_key=key)
        m = hit.get("match") or {}
        if log:
            print(
                f"postft-sweep → BUY {m.get('home')} vs {m.get('away')} "
                f"ask={hit.get('best_ask')} {hit.get('question')}",
                file=log,
                flush=True,
            )
        if worker is not None and hasattr(worker, "submit_postft"):
            try:
                quote = refresh_quote_book(quote, proxy=proxy)
            except Exception as e:  # noqa: BLE001
                skipped += 1
                if log:
                    print(f"postft-sweep → book fail token={tid[:12]}… {e}", file=log, flush=True)
                continue
            if quote.get("book_missing"):
                skipped += 1
                continue
            worker.submit_postft(quote, event_key=key, match_meta=meta)
            submitted += 1
            continue
        if executor is not None and getattr(getattr(executor, "settings", None), "enabled", True):
            try:
                quote = refresh_quote_book(quote, proxy=proxy)
                if quote.get("book_missing"):
                    skipped += 1
                    continue
                executor.maybe_trade(
                    quote,
                    event_key=key,
                    match_meta=meta,
                    event_type="postft",
                )
                submitted += 1
            except Exception as e:  # noqa: BLE001
                skipped += 1
                if log:
                    print(f"postft-sweep → FAIL token={tid[:12]}… {e}", file=log, flush=True)
            continue
        skipped += 1

    summary = {
        "cycle_id": cycle_id,
        "listed": payload.get("listed"),
        "scored": payload.get("scored"),
        "match_hits": payload.get("match_hits"),
        "tradeable_tokens": len(hits),
        "submitted": submitted,
        "skipped": skipped,
        "hours": sweep_hours(),
        "max_ask": cap,
    }
    if log:
        print(
            f"postft-sweep → DONE listed={summary['listed']} scored={summary['scored']} "
            f"tradeable={len(hits)} submitted={submitted}",
            file=log,
            flush=True,
        )
    return summary


def start_scheduler(
    root: Path,
    *,
    executor: Any | None,
    worker: Any | None,
    proxy: str | None | object = ...,
    stop_event: threading.Event | None = None,
) -> threading.Thread | None:
    """Daemon thread: first scan after start delay, then every 1h."""
    global _active_stop, _active_thread
    if not sweep_enabled():
        print("postft-sweep skipped (QUOTE_POSTFT_SWEEP=0 or USDC=0)", flush=True)
        return None
    stop = stop_event or threading.Event()
    delay = sweep_start_delay_s()
    interval = sweep_interval_s()

    def _loop() -> None:
        if delay > 0 and stop.wait(delay):
            return
        while not stop.is_set():
            try:
                run_once(
                    root,
                    executor=executor,
                    worker=worker,
                    proxy=proxy,
                    progress=sys.stderr,
                    stop=stop,
                )
            except Exception as e:  # noqa: BLE001
                print(f"ALERT postft-sweep failed: {e}", flush=True)
            if stop.wait(interval):
                return

    with _lock:
        _active_stop = stop
        th = threading.Thread(target=_loop, name="postft-sweep", daemon=True)
        _active_thread = th
    th.start()
    print(
        f"postft-sweep → every {interval:.0f}s lookback={sweep_hours()}h "
        f"max_ask={sweep_max_ask()} start_delay={delay:.0f}s "
        f"usdc={postft_usdc():g}",
        flush=True,
    )
    return th


def stop_scheduler() -> None:
    global _active_stop, _active_thread
    with _lock:
        stop = _active_stop
        th = _active_thread
        _active_stop = None
        _active_thread = None
    if stop is not None:
        stop.set()
    if th is not None:
        th.join(timeout=4.0)
