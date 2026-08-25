"""Warm Gamma catalog + CLOB books while pitch-gate waits for aligned buy.

Kick on ``start_gate`` (buy path only). Refresh books on a short interval so
``quote_bridge_event`` can skip the ~0.5–1s ``POST /books`` RTT when the
snapshot is still fresh. Does not shorten DOM/AF/shot wait — only quote-path
latency after BUY.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger("pm_quote.gate_prewarm")

DEFAULT_BOOK_INTERVAL_S = 3.0
DEFAULT_MAX_AGE_S = 4.0


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return float(default)
    try:
        return float(raw)
    except (TypeError, ValueError):
        return float(default)


def _env_bool(name: str, default: bool = True) -> bool:
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def prewarm_enabled() -> bool:
    return _env_bool("QUOTE_GATE_PREWARM", True)


def book_interval_s() -> float:
    return max(0.5, _env_float("QUOTE_GATE_PREWARM_INTERVAL_S", DEFAULT_BOOK_INTERVAL_S))


def max_book_age_s() -> float:
    return max(0.5, _env_float("QUOTE_GATE_PREWARM_MAX_AGE_S", DEFAULT_MAX_AGE_S))


@dataclass
class _Slot:
    match_id: str
    event_key: str
    ev: dict[str, Any]
    cancel: threading.Event = field(default_factory=threading.Event)
    thread: threading.Thread | None = None
    token_ids: list[str] = field(default_factory=list)
    books: dict[str, dict[str, Any]] = field(default_factory=dict)
    books_mono: float = 0.0
    catalog_ok: bool = False
    refreshes: int = 0
    last_error: str = ""


@dataclass
class _ReadyBooks:
    match_id: str
    event_key: str
    token_ids: list[str]
    books: dict[str, dict[str, Any]]
    books_mono: float
    catalog_ok: bool
    refreshes: int


class GatePrewarm:
    """Process-wide prewarm registry (one slot per event_key)."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._slots: dict[str, _Slot] = {}
        # Last snapshot kept after refresh loop stops so BUY quote can still hit.
        self._ready: dict[str, _ReadyBooks] = {}
        self.root: Path | None = None
        self.market_cache: Any | None = None
        self.proxy: str | None | object = ...
        self.include_props: bool = True
        self.include_exact: bool = True

    def configure(
        self,
        root: Path,
        *,
        market_cache: Any | None = None,
        proxy: str | None | object = ...,
        include_props: bool = True,
        include_exact: bool = True,
    ) -> None:
        with self._lock:
            self.root = Path(root)
            self.market_cache = market_cache
            self.proxy = proxy
            self.include_props = bool(include_props)
            self.include_exact = bool(include_exact)

    def kick(self, ev: dict[str, Any], *, event_key: str) -> bool:
        """Start / replace prewarm for a pitch-gate buy session."""
        if not prewarm_enabled():
            return False
        if self.root is None:
            return False
        mid = str(ev.get("match_id") or "").strip()
        key = str(event_key or "").strip()
        if not mid or not key:
            return False
        with self._lock:
            old = self._slots.get(key)
            if old is not None and not old.cancel.is_set():
                return True
            slot = _Slot(match_id=mid, event_key=key, ev=dict(ev))
            self._slots[key] = slot
            self._ready.pop(key, None)
        thread = threading.Thread(
            target=self._run_slot,
            args=(slot,),
            name=f"gate-prewarm-{mid}",
            daemon=True,
        )
        slot.thread = thread
        thread.start()
        print(
            f"gate-prewarm → START match_id={mid} key={key} "
            f"interval={book_interval_s():g}s max_age={max_book_age_s():g}s",
            flush=True,
        )
        return True

    def stop(self, *, event_key: str | None = None, match_id: str | None = None) -> int:
        """Cancel refresh loops; keep last books for take_books until max_age."""
        n = 0
        with self._lock:
            keys: list[str] = []
            if event_key:
                keys.append(str(event_key))
            mid = str(match_id or "").strip()
            if mid:
                keys.extend(k for k, s in self._slots.items() if s.match_id == mid)
            for key in dict.fromkeys(keys):
                slot = self._slots.get(key)
                if slot is not None and not slot.cancel.is_set():
                    slot.cancel.set()
                    n += 1
        return n

    def take_books(
        self,
        token_ids: list[str],
        *,
        event_key: str = "",
        match_id: str = "",
        max_age_s: float | None = None,
    ) -> tuple[dict[str, dict[str, Any]] | None, dict[str, Any]]:
        """Return a fresh prewarmed book map covering ``token_ids``, or None."""
        meta: dict[str, Any] = {"hit": False, "reason": "no_slot"}
        ids = [str(t) for t in token_ids if t]
        if not ids:
            meta["reason"] = "empty_ids"
            return None, meta
        max_age = float(max_book_age_s() if max_age_s is None else max_age_s)
        now = time.monotonic()
        with self._lock:
            self._purge_stale_ready_locked(now, max_age)
            ready = self._find_ready_locked(event_key=event_key, match_id=match_id)
            if ready is None:
                meta["reason"] = "no_books"
                return None, meta
            age = now - float(ready.books_mono or 0.0)
            meta.update(
                {
                    "age_s": round(age, 3),
                    "max_age_s": max_age,
                    "refreshes": ready.refreshes,
                    "catalog_ok": ready.catalog_ok,
                    "token_n": len(ready.token_ids),
                    "event_key": ready.event_key,
                }
            )
            if age < 0 or age > max_age:
                meta["reason"] = "stale"
                return None, meta
            missing = [tid for tid in ids if tid not in ready.books]
            if missing:
                meta["reason"] = "incomplete"
                meta["missing_n"] = len(missing)
                return None, meta
            out = {tid: dict(ready.books[tid]) for tid in ids}
            meta["hit"] = True
            meta["reason"] = "ok"
            # One-shot: drop so we do not reuse a snapshot after the BUY quote.
            self._ready.pop(ready.event_key, None)
            return out, meta

    def _find_ready_locked(
        self, *, event_key: str, match_id: str
    ) -> _ReadyBooks | None:
        key = str(event_key or "").strip()
        if key:
            ready = self._ready.get(key)
            if ready is not None:
                return ready
            slot = self._slots.get(key)
            if slot is not None and slot.books:
                return self._snapshot_slot(slot)
        mid = str(match_id or "").strip()
        if mid:
            for ready in self._ready.values():
                if ready.match_id == mid and ready.books:
                    return ready
            for slot in self._slots.values():
                if slot.match_id == mid and slot.books:
                    return self._snapshot_slot(slot)
        return None

    @staticmethod
    def _snapshot_slot(slot: _Slot) -> _ReadyBooks:
        return _ReadyBooks(
            match_id=slot.match_id,
            event_key=slot.event_key,
            token_ids=list(slot.token_ids),
            books=dict(slot.books),
            books_mono=float(slot.books_mono or 0.0),
            catalog_ok=bool(slot.catalog_ok),
            refreshes=int(slot.refreshes),
        )

    def _purge_stale_ready_locked(self, now: float, max_age: float) -> None:
        dead = [
            k
            for k, r in self._ready.items()
            if now - float(r.books_mono or 0.0) > max_age * 2
        ]
        for k in dead:
            self._ready.pop(k, None)

    def _publish_ready(self, slot: _Slot) -> None:
        with self._lock:
            if not slot.books:
                return
            self._ready[slot.event_key] = self._snapshot_slot(slot)

    def _run_slot(self, slot: _Slot) -> None:
        import quote_lib as lib

        assert self.root is not None
        interval = book_interval_s()
        try:
            self._warm_catalog(slot)
            while not slot.cancel.is_set():
                try:
                    self._refresh_books(slot, lib)
                    self._publish_ready(slot)
                except Exception as e:  # noqa: BLE001
                    slot.last_error = str(e)
                    logger.warning(
                        "gate-prewarm books failed match=%s: %s",
                        slot.match_id,
                        e,
                    )
                if slot.cancel.wait(timeout=interval):
                    break
        finally:
            self._publish_ready(slot)
            with self._lock:
                cur = self._slots.get(slot.event_key)
                if cur is slot:
                    self._slots.pop(slot.event_key, None)
            print(
                f"gate-prewarm → STOP match_id={slot.match_id} key={slot.event_key} "
                f"refreshes={slot.refreshes} catalog_ok={slot.catalog_ok}",
                flush=True,
            )

    def _warm_catalog(self, slot: _Slot) -> None:
        import market_cache as mcache

        cache = self.market_cache
        if cache is None:
            return
        ev = slot.ev
        pm = dict(ev.get("polymarket") or {})
        row = {
            "match_id": slot.match_id,
            "dongqiudi": {
                "id": slot.match_id,
                "home": ev.get("home"),
                "away": ev.get("away"),
            },
            "polymarket": pm,
        }
        try:
            hit = cache.warm_match(row, proxy=self.proxy, force=False)
            slot.catalog_ok = bool(mcache.catalog_complete(hit))
        except Exception as e:  # noqa: BLE001
            slot.last_error = str(e)
            logger.warning("gate-prewarm catalog failed match=%s: %s", slot.match_id, e)

    def _refresh_books(self, slot: _Slot, lib: Any) -> None:
        assert self.root is not None
        ctx = lib.join_ft_context(self.root, slot.ev)
        tokens, _meta = lib.collect_target_tokens(
            ctx,
            proxy=self.proxy,
            include_props=self.include_props,
            include_exact=self.include_exact,
            mode="live",
            market_cache=self.market_cache,
        )
        tokens = lib.tradeable_token_rows(tokens)
        ids = [str(t["token_id"]) for t in tokens if t.get("token_id")]
        if not ids:
            return
        books = lib.fetch_books(ids, proxy=self.proxy)
        with self._lock:
            slot.token_ids = ids
            slot.books = books
            slot.books_mono = time.monotonic()
            slot.refreshes += 1
            if slot.refreshes == 1:
                print(
                    f"gate-prewarm → BOOKS match_id={slot.match_id} "
                    f"key={slot.event_key} tokens={len(ids)} "
                    f"catalog_ok={slot.catalog_ok}",
                    flush=True,
                )


_prewarm = GatePrewarm()
_prewarm_lock = threading.Lock()


def get_prewarm() -> GatePrewarm:
    return _prewarm


def configure_prewarm(
    root: Path,
    *,
    market_cache: Any | None = None,
    proxy: str | None | object = ...,
    include_props: bool = True,
    include_exact: bool = True,
) -> GatePrewarm:
    with _prewarm_lock:
        _prewarm.configure(
            root,
            market_cache=market_cache,
            proxy=proxy,
            include_props=include_props,
            include_exact=include_exact,
        )
        return _prewarm


def reset_prewarm_for_tests() -> None:
    with _prewarm_lock:
        for slot in list(_prewarm._slots.values()):
            slot.cancel.set()
        _prewarm._slots.clear()
        _prewarm._ready.clear()
        _prewarm.root = None
        _prewarm.market_cache = None
