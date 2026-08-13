#!/usr/bin/env python3
"""AF Bridge Board server: runs apifootball-bridge sync/watch and serves the UI."""

from __future__ import annotations

import json
import mimetypes
import sys
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

MODULE_DIR = Path(__file__).resolve().parents[1]
FRONTEND_DIR = MODULE_DIR.parent
ROOT = FRONTEND_DIR.parent
PUBLIC = MODULE_DIR / "public"
SRC = MODULE_DIR / "src"
AF_SCRIPTS = ROOT / ".cursor" / "skills" / "apifootball-bridge" / "scripts"

sys.path.insert(0, str(AF_SCRIPTS))
import af_bridge_lib as aflib  # noqa: E402

HOST = "127.0.0.1"
PORT = 8791
MODULE_ID = "af-bridge-board"
WATCH_INTERVAL_S = 15.0


class AfBridgeRuntime:
    """In-process sync/watch over fixture_cache.json (same as af_bridge CLI)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sync_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.running = False
        self.last_error: str | None = None
        self.sync_ticks = 0
        self.started_at: str | None = None
        self._af: aflib.AFClient | None = None
        self._last_fp: str | None = None

    def _client(self) -> aflib.AFClient:
        if self._af is None:
            key = aflib.load_af_key()
            self._af = aflib.AFClient(key, min_interval_s=aflib.FREE_PLAN_MIN_INTERVAL_S)
        return self._af

    def status(self) -> dict[str, Any]:
        cache = aflib.load_cache(aflib.DEFAULT_CACHE_PATH)
        bridge = aflib.load_bridge_snapshot(aflib.DEFAULT_BRIDGE_MATCHES)
        entries = cache.get("entries") or {}
        unresolved = cache.get("unresolved") or {}
        bridge_ids = {
            str((m.get("dongqiudi") or {}).get("id") or "")
            for m in (bridge.get("matches") or [])
            if isinstance(m, dict)
        }
        bridge_ids.discard("")
        bridge_mapped = sum(
            1 for mid in bridge_ids if mid in entries and (entries[mid] or {}).get("af_fixture_id")
        )
        bridge_unresolved = sum(1 for mid in bridge_ids if mid in unresolved)
        bridge_count = len(bridge_ids)
        with self._lock:
            running = self.running
            started_at = self.started_at
            sync_ticks = self.sync_ticks
            last_error = self.last_error
        return {
            "running": running,
            "started_at": started_at,
            "sync_ticks": sync_ticks,
            "last_error": last_error,
            "last_sync_at": cache.get("last_sync_at"),
            "last_sync_stats": cache.get("last_sync_stats"),
            # Prefer bridge-scoped coverage for hub pills (historical cache piles up).
            "entry_count": bridge_mapped,
            "unresolved_count": bridge_unresolved,
            "cache_entry_count": len(entries),
            "cache_unresolved_count": len(unresolved),
            "bridge_mapped": bridge_mapped,
            "bridge_unresolved": bridge_unresolved,
            "bridge_mapped_rate": round(bridge_mapped / bridge_count, 4) if bridge_count else None,
            "bridge_matched_at": bridge.get("matched_at"),
            "bridge_count": bridge_count,
            "cache_path": str(aflib.DEFAULT_CACHE_PATH),
            "interval_s": WATCH_INTERVAL_S,
        }

    def build_matches_payload(self) -> dict[str, Any]:
        cache = aflib.load_cache(aflib.DEFAULT_CACHE_PATH)
        bridge = aflib.load_bridge_snapshot(aflib.DEFAULT_BRIDGE_MATCHES)
        by_id: dict[str, dict[str, Any]] = {}
        for row in bridge.get("matches") or []:
            if not isinstance(row, dict):
                continue
            mid = str((row.get("dongqiudi") or {}).get("id") or "")
            if mid:
                by_id[mid] = row

        matches: list[dict[str, Any]] = []
        for mid, ent in (cache.get("entries") or {}).items():
            if not isinstance(ent, dict) or not ent.get("af_fixture_id"):
                continue
            if mid not in by_id:
                continue
            item = dict(ent)
            item["dqd_match_id"] = str(ent.get("dqd_match_id") or mid)
            br = by_id.get(str(mid))
            if br:
                item["bridge"] = {
                    "dongqiudi": br.get("dongqiudi"),
                    "polymarket": br.get("polymarket"),
                    "kickoff_beijing": br.get("kickoff_beijing"),
                    "match_score": br.get("match_score"),
                }
            matches.append(item)

        def _sort_key(m: dict[str, Any]) -> tuple:
            return (
                str(m.get("kickoff_beijing") or ""),
                str(m.get("af_league") or ""),
                str(m.get("dqd_home") or ""),
            )

        matches.sort(key=_sort_key)

        unresolved = []
        for mid, u in (cache.get("unresolved") or {}).items():
            if not isinstance(u, dict):
                continue
            # Only surface unresolved rows still on the current bridge.
            if mid not in by_id:
                continue
            unresolved.append({"dqd_match_id": str(mid), **u})

        bridge_count = len(by_id)
        return {
            "updated_at": cache.get("updated_at"),
            "last_sync_at": cache.get("last_sync_at"),
            "matched_at": cache.get("last_sync_at") or cache.get("updated_at"),
            "matches": matches,
            "unresolved": unresolved,
            "entry_count": len(matches),
            "unresolved_count": len(unresolved),
            "stats": cache.get("last_sync_stats"),
            "bridge_matched_at": bridge.get("matched_at"),
            "bridge_count": bridge_count,
            "bridge_mapped": sum(1 for mid in by_id if mid in (cache.get("entries") or {}) and (cache.get("entries") or {}).get(mid, {}).get("af_fixture_id")),
            "bridge_unresolved": len(unresolved),
        }

    def sync_once(self) -> dict[str, Any]:
        with self._sync_lock:
            af = self._client()
            cache = aflib.load_cache(aflib.DEFAULT_CACHE_PATH)
            bridge_snap = aflib.load_bridge_snapshot(aflib.DEFAULT_BRIDGE_MATCHES)
            cache = aflib.sync_fixture_cache(af, cache=cache, bridge_snap=bridge_snap)
            aflib.save_cache(aflib.DEFAULT_CACHE_PATH, cache)
            with self._lock:
                self.sync_ticks += 1
                self.last_error = None
                self._last_fp = aflib.bridge_fingerprint(bridge_snap)
            payload = self.build_matches_payload()
            payload["stats"] = cache.get("last_sync_stats")
            return payload

    def _watch_loop(self) -> None:
        force_every = 40
        tick = 0
        while not self._stop.is_set():
            tick += 1
            try:
                bridge_snap = aflib.load_bridge_snapshot(aflib.DEFAULT_BRIDGE_MATCHES)
                fp = aflib.bridge_fingerprint(bridge_snap)
                with self._lock:
                    last_fp = self._last_fp
                should = last_fp is None or fp != last_fp or (tick % force_every == 0)
                if should:
                    self.sync_once()
            except Exception as e:  # noqa: BLE001
                with self._lock:
                    self.last_error = str(e)
                traceback.print_exc()
            self._stop.wait(WATCH_INTERVAL_S)

    def start(self) -> dict[str, Any]:
        with self._lock:
            if self.running and self._thread and self._thread.is_alive():
                already = True
            else:
                already = False
                self._stop.clear()
                self.running = True
                self.started_at = aflib.iso_now()
                # Reset so the watch loop does one sync on first tick (no parallel first-sync thread).
                self._last_fp = None
                self._thread = threading.Thread(
                    target=self._watch_loop, name="af-bridge-watch", daemon=True
                )
                self._thread.start()
        st = self.status()
        return {"ok": True, "already": already, "running": True, **st}

    def stop(self) -> dict[str, Any]:
        self._stop.set()
        with self._lock:
            self.running = False
            t = self._thread
            self._thread = None
        if t and t.is_alive():
            t.join(timeout=2.0)
        return {"ok": True, "running": False, **self.status()}


RUNTIME = AfBridgeRuntime()


def json_response(handler: BaseHTTPRequestHandler, code: int, payload: Any) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(code)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.end_headers()
    handler.wfile.write(body)


def read_body(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    length = int(handler.headers.get("Content-Length") or 0)
    if length <= 0:
        return {}
    raw = handler.rfile.read(length)
    try:
        data = json.loads(raw.decode("utf-8"))
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        return {}


def _safe_file(root: Path, rel: str) -> Path | None:
    path = (root / rel).resolve()
    if not str(path).startswith(str(root.resolve())) or not path.is_file():
        return None
    return path


def serve_file(handler: BaseHTTPRequestHandler, path: Path) -> None:
    ctype = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
    if path.suffix == ".js":
        ctype = "text/javascript; charset=utf-8"
    elif path.suffix == ".css":
        ctype = "text/css; charset=utf-8"
    elif path.suffix == ".html":
        ctype = "text/html; charset=utf-8"
    data = path.read_bytes()
    handler.send_response(200)
    handler.send_header("Content-Type", ctype)
    handler.send_header("Content-Length", str(len(data)))
    handler.send_header("Cache-Control", "no-cache")
    handler.end_headers()
    handler.wfile.write(data)


class Handler(BaseHTTPRequestHandler):
    server_version = "AfBridgeBoard/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        from urllib.parse import urlparse

        parsed = urlparse(self.path)
        path = parsed.path

        if path in ("/", "/index.html"):
            file_path = PUBLIC / "index.html"
            if file_path.is_file():
                serve_file(self, file_path)
            else:
                self.send_error(404)
            return
        if path.startswith("/src/"):
            file_path = _safe_file(SRC, path[len("/src/") :])
            if file_path:
                serve_file(self, file_path)
            else:
                self.send_error(404)
            return
        if path == "/api/health":
            json_response(
                self,
                200,
                {
                    "ok": True,
                    "module": MODULE_ID,
                    "skill": "apifootball-bridge",
                    "version": "1.0.0",
                },
            )
            return
        if path == "/api/module":
            meta = MODULE_DIR / "module.json"
            if meta.is_file():
                json_response(self, 200, json.loads(meta.read_text(encoding="utf-8")))
            else:
                json_response(self, 404, {"error": "module.json missing"})
            return
        if path == "/api/status":
            json_response(self, 200, RUNTIME.status())
            return
        if path == "/api/matches":
            try:
                json_response(self, 200, RUNTIME.build_matches_payload())
            except Exception as e:  # noqa: BLE001
                traceback.print_exc()
                json_response(self, 502, {"error": str(e)})
            return

        self.send_error(404)

    def do_POST(self) -> None:  # noqa: N802
        from urllib.parse import urlparse

        path = urlparse(self.path).path
        _ = read_body(self)

        if path == "/api/af/once":
            try:
                payload = RUNTIME.sync_once()
                json_response(self, 200, payload)
            except Exception as e:  # noqa: BLE001
                traceback.print_exc()
                json_response(self, 502, {"error": str(e)})
            return
        if path == "/api/af/start":
            try:
                json_response(self, 200, RUNTIME.start())
            except Exception as e:  # noqa: BLE001
                traceback.print_exc()
                json_response(self, 502, {"error": str(e)})
            return
        if path == "/api/af/stop":
            json_response(self, 200, RUNTIME.stop())
            return

        self.send_error(404)


def main() -> int:
    PUBLIC.mkdir(parents=True, exist_ok=True)
    SRC.mkdir(parents=True, exist_ok=True)
    (ROOT / "data" / "apifootball").mkdir(parents=True, exist_ok=True)
    httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"AF Bridge Board → http://{HOST}:{PORT}/", flush=True)
    print(f"Module path     → {MODULE_DIR}", flush=True)
    print(
        "Skill           → apifootball-bridge (read cache; Start watch / Sync once to hit AF)",
        flush=True,
    )
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nbye", flush=True)
        RUNTIME.stop()
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
