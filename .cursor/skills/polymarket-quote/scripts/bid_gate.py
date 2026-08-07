#!/usr/bin/env python3
"""Post-AF market bid gate: require best_bid ≥ threshold before buy.

After AF confirms a goal, poll CLOB books until a WIN candidate token shows
``best_bid >= min_bid`` (default 0.9), then allow quote/trade. Abort on DQD
reversal / cancel. Async ThreadPool — never block the watch loop.
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable

import quote_lib as lib

DEFAULT_MIN_BID = 0.9
DEFAULT_POLL_S = 2.0
DEFAULT_TIMEOUT_S = 120.0
DEFAULT_WORKERS = 4


def iso_now() -> str:
    return lib.now_cn_iso()


def _sleep_abortable(
    abort: threading.Event,
    seconds: float,
    *,
    chunk_s: float = 0.25,
) -> bool:
    """Sleep up to ``seconds``; return True if abort was set."""
    deadline = time.monotonic() + max(0.0, float(seconds))
    while time.monotonic() < deadline:
        if abort.is_set():
            return True
        time.sleep(min(chunk_s, max(0.0, deadline - time.monotonic())))
    return abort.is_set()


def pick_win_candidates(
    win_rows: list[dict[str, Any]],
    book_map: dict[str, dict[str, Any]],
    *,
    eps: float,
    fee_rate: float,
    min_net: float,
) -> list[tuple[dict[str, Any], float | None, float | None]]:
    """WIN rows that look buyable (misprice) or at least have an ask."""
    mispriced: list[tuple[dict[str, Any], float | None, float | None]] = []
    with_ask: list[tuple[dict[str, Any], float | None, float | None]] = []
    for row in win_rows:
        tid = str(row.get("token_id") or "")
        if not tid:
            continue
        book = book_map.get(tid) or {
            "book_missing": True,
            "best_bid": None,
            "best_ask": None,
        }
        bid = book.get("best_bid")
        ask = book.get("best_ask")
        try:
            bid_f = float(bid) if bid is not None else None
        except (TypeError, ValueError):
            bid_f = None
        try:
            ask_f = float(ask) if ask is not None else None
        except (TypeError, ValueError):
            ask_f = None
        mis, _reason, _econ = lib.flag_misprice(
            "WIN",
            book,
            eps=eps,
            fee_rate=fee_rate,
            min_net=min_net,
        )
        item = (row, bid_f, ask_f)
        if mis:
            mispriced.append(item)
        elif ask_f is not None:
            with_ask.append(item)
    return mispriced if mispriced else with_ask


class BidGate:
    """Async market-bid confirmation after AF gate success."""

    def __init__(
        self,
        root: Path,
        *,
        min_bid: float = DEFAULT_MIN_BID,
        poll_s: float = DEFAULT_POLL_S,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        max_workers: int = DEFAULT_WORKERS,
        proxy: str | None | object = ...,
        include_props: bool = True,
        include_exact: bool = True,
        eps: float = lib.DEFAULT_EPS,
        fee_rate: float = lib.SPORTS_TAKER_FEE_RATE,
        min_net: float = lib.DEFAULT_MIN_NET,
        market_cache: Any | None = None,
        fetch_books_fn: Callable[..., dict[str, dict[str, Any]]] | None = None,
    ) -> None:
        self.root = Path(root)
        self.min_bid = float(min_bid)
        self.poll_s = max(0.05, float(poll_s))
        self.timeout_s = max(0.05, float(timeout_s))
        self.proxy = proxy
        self.include_props = bool(include_props)
        self.include_exact = bool(include_exact)
        self.eps = float(eps)
        self.fee_rate = float(fee_rate)
        self.min_net = float(min_net)
        self.market_cache = market_cache
        self._fetch_books_fn = fetch_books_fn
        self._exec = ThreadPoolExecutor(
            max_workers=max(1, int(max_workers)),
            thread_name_prefix="bid-gate",
        )
        self._lock = threading.Lock()
        self._pending: dict[str, Future] = {}
        self._meta: dict[str, dict[str, Any]] = {}

    def pending_event_keys(self) -> set[str]:
        with self._lock:
            return set(self._pending.keys())

    def pending_match_ids(self) -> set[str]:
        with self._lock:
            return {
                str(m.get("match_id") or "")
                for m in self._meta.values()
                if m.get("match_id")
            }

    def _fetch_books(self, token_ids: list[str]) -> dict[str, dict[str, Any]]:
        if self._fetch_books_fn is not None:
            return self._fetch_books_fn(token_ids, proxy=self.proxy)
        return lib.fetch_books(token_ids, proxy=self.proxy)

    def await_bid(
        self,
        work_ev: dict[str, Any],
        *,
        abort: threading.Event,
        abort_reason_holder: dict[str, str],
    ) -> dict[str, Any]:
        """Block until bid ≥ min_bid / timeout / abort. Call via submit."""
        t0 = time.monotonic()
        mid = str(work_ev.get("match_id") or "")
        polls = 0
        first_bid: float | None = None
        last_max_bid: float | None = None

        def _aborted() -> dict[str, Any]:
            return {
                "ok": False,
                "confirmed": False,
                "match_id": mid,
                "polls": polls,
                "elapsed_ms": int((time.monotonic() - t0) * 1000),
                "min_bid": self.min_bid,
                "first_bid": first_bid,
                "last_max_bid": last_max_bid,
                "error": "aborted",
                "reason": abort_reason_holder.get("reason") or "cancelled",
                "via": "bid_gate",
            }

        if abort.is_set():
            return _aborted()

        try:
            ctx = lib.join_ft_context(self.root, work_ev)
            if ctx.get("home_score") is None or ctx.get("away_score") is None:
                return {
                    "ok": False,
                    "confirmed": False,
                    "match_id": mid,
                    "polls": 0,
                    "elapsed_ms": int((time.monotonic() - t0) * 1000),
                    "min_bid": self.min_bid,
                    "error": "missing_score",
                    "via": "bid_gate",
                }
            tokens, _disc = lib.collect_target_tokens(
                ctx,
                proxy=self.proxy,
                include_props=self.include_props,
                include_exact=self.include_exact,
                mode="live",
                market_cache=self.market_cache,
            )
            tokens = lib.tradeable_token_rows(tokens)
            win_rows = [
                r
                for r in tokens
                if str(r.get("settlement") or "").upper() == "WIN" and r.get("token_id")
            ]
        except Exception as e:  # noqa: BLE001
            return {
                "ok": False,
                "confirmed": False,
                "match_id": mid,
                "polls": 0,
                "elapsed_ms": int((time.monotonic() - t0) * 1000),
                "min_bid": self.min_bid,
                "error": f"discover_failed:{e}",
                "via": "bid_gate",
            }

        if not win_rows:
            return {
                "ok": False,
                "confirmed": False,
                "match_id": mid,
                "polls": 0,
                "elapsed_ms": int((time.monotonic() - t0) * 1000),
                "min_bid": self.min_bid,
                "error": "no_win_candidates",
                "via": "bid_gate",
            }

        ids = [str(r["token_id"]) for r in win_rows]
        while True:
            if abort.is_set():
                return _aborted()
            elapsed = time.monotonic() - t0
            if polls > 0 and elapsed >= self.timeout_s:
                break

            polls += 1
            try:
                book_map = self._fetch_books(ids)
            except Exception as e:  # noqa: BLE001
                book_map = {}
                if polls == 1:
                    # Keep trying — transient CLOB blip.
                    pass
                _ = e

            candidates = pick_win_candidates(
                win_rows,
                book_map,
                eps=self.eps,
                fee_rate=self.fee_rate,
                min_net=self.min_net,
            )
            max_bid: float | None = None
            for _row, bid_f, _ask_f in candidates:
                if bid_f is None:
                    continue
                if first_bid is None:
                    first_bid = bid_f
                if max_bid is None or bid_f > max_bid:
                    max_bid = bid_f
            if max_bid is not None:
                last_max_bid = max_bid

            for row, bid_f, _ask_f in candidates:
                if bid_f is not None and bid_f + 1e-12 >= self.min_bid:
                    return {
                        "ok": True,
                        "confirmed": True,
                        "match_id": mid,
                        "polls": polls,
                        "elapsed_ms": int((time.monotonic() - t0) * 1000),
                        "min_bid": self.min_bid,
                        "first_bid": first_bid,
                        "pass_bid": bid_f,
                        "token_id": str(row.get("token_id") or ""),
                        "market_key": row.get("market_key"),
                        "via": "bid_gate",
                    }

            # Immediate t0 check done; wait then poll again until timeout.
            elapsed = time.monotonic() - t0
            remain = self.timeout_s - elapsed
            if remain <= 0.05:
                break
            wait = min(self.poll_s, remain)
            if _sleep_abortable(abort, wait):
                return _aborted()

        return {
            "ok": False,
            "confirmed": False,
            "match_id": mid,
            "polls": polls,
            "elapsed_ms": int((time.monotonic() - t0) * 1000),
            "min_bid": self.min_bid,
            "first_bid": first_bid,
            "last_max_bid": last_max_bid,
            "error": "market_bid_timeout",
            "via": "bid_gate",
        }

    def submit(
        self,
        event_key: str,
        work_ev: dict[str, Any],
        *,
        af_gate: dict[str, Any] | None = None,
    ) -> bool:
        """Enqueue non-blocking bid confirm. Returns False if already pending."""
        mid = str(work_ev.get("match_id") or "")
        if not mid or not event_key:
            return False
        abort = threading.Event()
        reason_holder: dict[str, str] = {"reason": ""}
        with self._lock:
            if event_key in self._pending:
                return False
            fut = self._exec.submit(
                self.await_bid,
                dict(work_ev),
                abort=abort,
                abort_reason_holder=reason_holder,
            )
            self._pending[event_key] = fut
            self._meta[event_key] = {
                "work_ev": dict(work_ev),
                "af_gate": dict(af_gate or {}),
                "match_id": mid,
                "submitted_at": iso_now(),
                "abort": abort,
                "abort_reason_holder": reason_holder,
            }
        print(
            f"bid-gate → queued {mid} min_bid={self.min_bid} "
            f"poll={self.poll_s}s timeout={self.timeout_s}s key={event_key}",
            flush=True,
        )
        return True

    def cancel_key(self, event_key: str, reason: str = "cancelled") -> bool:
        with self._lock:
            fut = self._pending.pop(event_key, None)
            meta = self._meta.pop(event_key, None)
        if meta is None and fut is None:
            return False
        if meta is not None:
            holder = meta.get("abort_reason_holder")
            if isinstance(holder, dict):
                holder["reason"] = str(reason or "cancelled")
            abort = meta.get("abort")
            if isinstance(abort, threading.Event):
                abort.set()
        if fut is not None and not fut.done():
            fut.cancel()
        print(
            f"bid-gate → cancelled key={event_key} reason={reason}",
            flush=True,
        )
        return True

    def cancel_match(self, match_id: str, reason: str = "cancelled") -> int:
        mid = str(match_id or "")
        if not mid:
            return 0
        with self._lock:
            keys = [
                k
                for k, m in self._meta.items()
                if str(m.get("match_id") or "") == mid
            ]
        n = 0
        for k in keys:
            if self.cancel_key(k, reason=reason):
                n += 1
        return n

    def drain_done(self) -> list[dict[str, Any]]:
        with self._lock:
            done_keys = [k for k, fut in self._pending.items() if fut.done()]
        out: list[dict[str, Any]] = []
        for key in done_keys:
            with self._lock:
                fut = self._pending.pop(key, None)
                meta = self._meta.pop(key, None)
            if fut is None or meta is None:
                continue
            try:
                gate = fut.result()
            except Exception as e:  # noqa: BLE001
                gate = {
                    "ok": False,
                    "confirmed": False,
                    "match_id": meta.get("match_id"),
                    "error": str(e),
                    "via": "bid_gate",
                }
            out.append(
                {
                    "event_key": key,
                    "work_ev": meta["work_ev"],
                    "af_gate": meta.get("af_gate") or {},
                    "match_id": meta.get("match_id"),
                    "gate": gate,
                }
            )
        return out
