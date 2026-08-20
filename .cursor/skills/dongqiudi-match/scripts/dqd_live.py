#!/usr/bin/env python3
"""Best-effort Dongqiudi live/animation surface discovery with local cache."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
import json

import dqd_lib as lib

TZ_CN = timezone(timedelta(hours=8))
CACHE_TTL_S = 30 * 60.0

FetchJsonFn = Callable[[str, dict[str, Any], float], dict[str, Any]]

PAGE_URL_PATTERNS = (
    "https://www.dongqiudi.com/match/{match_id}",
    "https://www.dongqiudi.com/live/{match_id}",
)

DISCOVERY_ENDPOINTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("/magicball/v1/live/detail", ("match_id",)),
    ("/magicball/v1/live/info", ("match_id",)),
    ("/magicball/v1/live/match_detail", ("match_id",)),
    ("/magicball/v1/live/match_info", ("match_id",)),
    ("/magicball/v1/match/detail", ("match_id",)),
    ("/magicball/v1/match/live", ("match_id",)),
)


def cache_path(root: Path) -> Path:
    return Path(root) / "data" / "dqd_live_cache.json"


def now_cn_iso() -> str:
    return datetime.now(TZ_CN).isoformat(timespec="seconds")


def _load_cache(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_cache(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _walk_candidates(obj: Any) -> tuple[str | None, str | None]:
    stream_url: str | None = None
    surface: str | None = None
    stack = [obj]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            for key, val in cur.items():
                lk = str(key).lower()
                if isinstance(val, str):
                    sv = val.strip()
                    lsv = sv.lower()
                    if not stream_url and (
                        lsv.startswith("http")
                        and any(x in lsv for x in (".m3u8", ".flv", "/m3u8", "/flv"))
                    ):
                        stream_url = sv
                    if surface is None and (
                        ("animation" in lk)
                        or ("ani" == lk)
                        or ("animation" in lsv)
                    ):
                        if lsv in {"1", "true", "yes", "animation"}:
                            surface = "animation"
                    if surface is None and ("video" in lk or "stream" in lk or "live_url" in lk):
                        if sv:
                            surface = "video"
                elif isinstance(val, bool):
                    if val and surface is None and ("animation" in lk or "animate" in lk):
                        surface = "animation"
                elif isinstance(val, (dict, list)):
                    stack.append(val)
        elif isinstance(cur, list):
            stack.extend(cur)
    return stream_url, surface


def _page_url(match_id: str) -> str:
    for pat in PAGE_URL_PATTERNS:
        return pat.format(match_id=match_id)
    return ""


def discover_live_surface(
    match_id: str,
    *,
    root: Path | None = None,
    timeout: float = 10.0,
    fetch_json_fn: FetchJsonFn | None = None,
    force_refresh: bool = False,
) -> dict[str, Any]:
    mid = str(match_id or "").strip()
    out = {
        "match_id": mid,
        "page_url": _page_url(mid) if mid else "",
        "stream_url": None,
        "surface": "none",
        "discovered_at": now_cn_iso(),
        "raw_hint": {},
    }
    if not mid:
        out["raw_hint"] = {"error": "missing_match_id"}
        return out

    cpath = cache_path(root) if root is not None else None
    if cpath is not None and not force_refresh:
        cache = _load_cache(cpath)
        hit = cache.get(mid)
        if isinstance(hit, dict):
            try:
                ts = datetime.fromisoformat(str(hit.get("discovered_at") or ""))
                age = (datetime.now(TZ_CN) - ts).total_seconds()
            except Exception:
                age = CACHE_TTL_S + 1
            if age <= CACHE_TTL_S:
                refreshed = dict(hit)
                refreshed["page_url"] = _page_url(mid)
                return refreshed

    fetch = fetch_json_fn or (lambda path, params, timeout_s: lib.fetch_json(path, params, timeout=timeout_s))
    raw_hint: dict[str, Any] = {}
    for path, param_names in DISCOVERY_ENDPOINTS:
        params = {name: mid for name in param_names}
        try:
            payload = fetch(path, params, timeout)
        except Exception as e:  # noqa: BLE001
            raw_hint.setdefault("errors", []).append(f"{path}: {e}")
            continue
        stream_url, surface = _walk_candidates(payload)
        if stream_url or surface:
            out["stream_url"] = stream_url
            out["surface"] = surface or ("video" if stream_url else "page_only")
            data = payload.get("data") if isinstance(payload, dict) else None
            keys = sorted((data or payload).keys())[:12] if isinstance((data or payload), dict) else []
            out["raw_hint"] = {"endpoint": path, "keys": keys}
            break
        raw_hint.setdefault("checked", []).append(path)

    if out["surface"] == "none":
        out["surface"] = "page_only" if out["page_url"] else "none"
        out["raw_hint"] = raw_hint

    if cpath is not None:
        cache = _load_cache(cpath)
        cache[mid] = out
        _write_cache(cpath, cache)
    return out
