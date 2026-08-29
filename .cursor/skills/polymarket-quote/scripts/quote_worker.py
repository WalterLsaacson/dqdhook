"""Off-thread CLOB quote / rest / flatten so the watch tick can start gates immediately.

The watch loop only enqueues a job with the event payload. This worker is the
only thread that should call ``quote_bridge_event`` / rest cancel / flatten
against the CLOB. Reversal still cancels pitch-gate on the emit thread; it
only *queues* rest cancel here (no HTTP on the bridge thread).
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger("pm_quote.quote_worker")

_PRIO_REST_CANCEL = 0
_PRIO_FLATTEN = 1
_PRIO_QUOTE = 2
_PRIO_HOUSEKEEP = 3

_active: "QuoteWorker | None" = None
_active_lock = threading.Lock()


def get_quote_worker() -> "QuoteWorker | None":
    with _active_lock:
        return _active


def start_quote_worker(
    root: Path,
    *,
    proxy: str | None | object = ...,
    include_props: bool = True,
    include_exact: bool = True,
    eps: float = 0.005,
    fee_rate: float = 0.05,
    min_net: float = 0.00475,
    trade_executor: Any | None = None,
    market_cache: Any | None = None,
) -> "QuoteWorker":
    global _active
    with _active_lock:
        if _active is not None:
            return _active
        worker = QuoteWorker(
            root,
            proxy=proxy,
            include_props=include_props,
            include_exact=include_exact,
            eps=eps,
            fee_rate=fee_rate,
            min_net=min_net,
            trade_executor=trade_executor,
            market_cache=market_cache,
        )
        worker.start()
        _active = worker
        return worker


def stop_quote_worker() -> None:
    global _active
    with _active_lock:
        worker = _active
        _active = None
    if worker is not None:
        worker.stop()


def reset_quote_worker_for_tests() -> None:
    stop_quote_worker()


@dataclass(order=True)
class _QItem:
    priority: int
    seq: int
    job: dict[str, Any] = field(compare=False)


@dataclass
class QuoteJobResult:
    bundles: list[dict[str, Any]]
    seen_keys: list[str]
    ft_match_ids: list[str]
    event_key: str
    match_id: str
    kind: str
    skipped: bool = False


class QuoteWorker:
    def __init__(
        self,
        root: Path,
        *,
        proxy: str | None | object,
        include_props: bool,
        include_exact: bool,
        eps: float,
        fee_rate: float,
        min_net: float,
        trade_executor: Any | None,
        market_cache: Any | None,
        idle_housekeep_s: float = 2.0,
    ) -> None:
        self.root = Path(root)
        self.proxy = proxy
        self.include_props = bool(include_props)
        self.include_exact = bool(include_exact)
        self.eps = float(eps)
        self.fee_rate = float(fee_rate)
        self.min_net = float(min_net)
        self.trade_executor = trade_executor
        self.market_cache = market_cache
        self.idle_housekeep_s = max(0.2, float(idle_housekeep_s))
        self._q: queue.PriorityQueue[_QItem] = queue.PriorityQueue()
        self._seq = 0
        self._lock = threading.Lock()
        self._revoked_keys: set[str] = set()
        self._quote_keys_by_match: dict[str, set[str]] = {}
        self._in_flight: set[str] = set()
        self._results: list[QuoteJobResult] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._busy_kind: str | None = None
        self._housekeep_queued = False

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._loop, name="clob-quote-worker", daemon=True
        )
        self._thread.start()
        print("clob-worker → started (quote / rest / flatten off watch tick)", flush=True)

    def stop(self) -> None:
        self._stop.set()
        self._q.put(_QItem(priority=99, seq=self._next_seq(), job={"kind": "stop"}))
        th = self._thread
        if th is not None:
            th.join(timeout=8.0)
        self._thread = None

    def is_in_flight(self, event_key: str) -> bool:
        key = str(event_key or "").strip()
        if not key:
            return False
        with self._lock:
            return key in self._in_flight

    def drain_results(self) -> list[QuoteJobResult]:
        with self._lock:
            out = list(self._results)
            self._results.clear()
            return out

    def wait_idle(self, timeout: float = 4.0) -> None:
        """Test helper: wait until the queue is empty and no job is running."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            idle = self._q.unfinished_tasks == 0 and self._busy_kind is None
            if idle:
                return
            time.sleep(0.02)
        raise TimeoutError("clob-worker did not go idle")

    def revoke_event(self, event_key: str) -> None:
        """Skip this quote job; in-flight quote must self-abort after CLOB."""
        key = str(event_key or "").strip()
        if not key:
            return
        with self._lock:
            self._revoked_keys.add(key)

    def revoke_submitted_quotes(self, match_id: str) -> None:
        """Revoke quote keys already submitted for this match (not a later re-award)."""
        mid = str(match_id or "").strip()
        if not mid:
            return
        with self._lock:
            self._revoked_keys.update(self._quote_keys_by_match.get(mid) or ())

    def submit_quote(
        self,
        ev: dict[str, Any],
        *,
        event_key: str,
        extra: dict[str, Any] | None = None,
    ) -> None:
        key = str(event_key or "").strip()
        mid = str(ev.get("match_id") or "").strip()
        with self._lock:
            if key:
                self._in_flight.add(key)
            if mid and key:
                self._quote_keys_by_match.setdefault(mid, set()).add(key)
        self._put(
            _PRIO_QUOTE,
            {
                "kind": "quote",
                "ev": dict(ev),
                "event_key": key,
                "match_id": mid,
                "extra": dict(extra or {}),
            },
        )

    def submit_flatten(
        self,
        ev: dict[str, Any],
        *,
        event_key: str,
        extra: dict[str, Any] | None = None,
    ) -> None:
        self._put(
            _PRIO_FLATTEN,
            {
                "kind": "flatten",
                "ev": dict(ev),
                "event_key": str(event_key or ""),
                "match_id": str(ev.get("match_id") or ""),
                "extra": dict(extra or {}),
            },
        )

    def submit_rest_cancel(self, match_id: str, *, reason: str) -> None:
        mid = str(match_id or "").strip()
        if not mid:
            return
        self.revoke_submitted_quotes(mid)
        self._put(
            _PRIO_REST_CANCEL,
            {
                "kind": "rest_cancel",
                "match_id": mid,
                "event_key": "",
                "ev": {},
                "reason": str(reason or "cancel"),
            },
        )

    def _next_seq(self) -> int:
        with self._lock:
            self._seq += 1
            return self._seq

    def _put(self, priority: int, job: dict[str, Any]) -> None:
        self._q.put(_QItem(priority=priority, seq=self._next_seq(), job=job))

    def _key_revoked(self, event_key: str) -> bool:
        key = str(event_key or "").strip()
        if not key:
            return False
        with self._lock:
            return key in self._revoked_keys

    def _schedule_housekeep(self) -> None:
        with self._lock:
            if self._housekeep_queued:
                return
            self._housekeep_queued = True
        self._put(_PRIO_HOUSEKEEP, {"kind": "housekeep", "match_id": "", "event_key": "", "ev": {}})

    def _queue_has_work(self) -> bool:
        return not self._q.empty()

    def _finish_flight(self, event_key: str) -> None:
        key = str(event_key or "").strip()
        if not key:
            return
        with self._lock:
            self._in_flight.discard(key)

    def _push_result(self, result: QuoteJobResult) -> None:
        with self._lock:
            self._results.append(result)

    def _loop(self) -> None:
        last_housekeep = 0.0
        while not self._stop.is_set():
            try:
                item = self._q.get(timeout=min(0.5, self.idle_housekeep_s))
            except queue.Empty:
                now = time.monotonic()
                if now - last_housekeep >= self.idle_housekeep_s:
                    last_housekeep = now
                    self._schedule_housekeep()
                continue
            job = item.job
            kind = str(job.get("kind") or "")
            if kind == "stop" or self._stop.is_set():
                break
            try:
                self._busy_kind = kind
                self._run_job(job)
            except Exception:
                logger.exception("clob-worker job failed kind=%s", kind)
                print(
                    f"ALERT clob-worker job failed kind={kind} "
                    f"match={job.get('match_id')} key={job.get('event_key')}",
                    flush=True,
                )
                self._finish_flight(str(job.get("event_key") or ""))
            finally:
                self._busy_kind = None
                self._q.task_done()

    def _run_housekeep(self) -> None:
        with self._lock:
            self._housekeep_queued = False
        if self._queue_has_work():
            self._schedule_housekeep()
            return
        ex = self.trade_executor
        if ex is None:
            return
        try:
            retried = list(ex.retry_pending_flattens() or [])
            if retried:
                self._push_result(
                    QuoteJobResult(
                        bundles=[
                            {
                                "quoted_at": _now(),
                                "trigger": "flatten_retry",
                                "flatten_attempts": retried,
                                "flatten_count": len(retried),
                            }
                        ],
                        seen_keys=[],
                        ft_match_ids=[],
                        event_key="",
                        match_id="",
                        kind="flatten_retry",
                    )
                )
        except Exception as e:  # noqa: BLE001
            print(f"ALERT flatten retry sweep failed: {e}", flush=True)
        if self._queue_has_work():
            self._schedule_housekeep()
            return
        try:
            rested = list(ex.reconcile_rest_orders() or [])
            if rested:
                self._push_result(
                    QuoteJobResult(
                        bundles=[
                            {
                                "quoted_at": _now(),
                                "trigger": "rest_fill",
                                "rest_fills": rested,
                                "rest_fill_count": len(rested),
                            }
                        ],
                        seen_keys=[],
                        ft_match_ids=[],
                        event_key="",
                        match_id="",
                        kind="rest_reconcile",
                    )
                )
        except Exception as e:  # noqa: BLE001
            print(f"ALERT rest reconcile failed: {e}", flush=True)

    def _run_job(self, job: dict[str, Any]) -> None:
        kind = str(job.get("kind") or "")
        if kind == "rest_cancel":
            self._run_rest_cancel(job)
            return
        if kind == "flatten":
            self._run_flatten(job)
            return
        if kind == "quote":
            self._run_quote(job)
            return
        if kind == "housekeep":
            self._run_housekeep()

    def _run_rest_cancel(self, job: dict[str, Any]) -> None:
        mid = str(job.get("match_id") or "")
        reason = str(job.get("reason") or "cancel")
        ex = self.trade_executor
        if not mid or ex is None:
            return
        try:
            ex.cancel_rest_orders_for_match(mid, reason=reason)
        except Exception as e:  # noqa: BLE001
            print(f"ALERT rest cancel on {reason} failed match={mid}: {e}", flush=True)

    def _run_flatten(self, job: dict[str, Any]) -> None:
        import quote_lib as lib

        ev = job.get("ev") if isinstance(job.get("ev"), dict) else {}
        extra = job.get("extra") if isinstance(job.get("extra"), dict) else {}
        key = str(job.get("event_key") or "")
        mid = str(job.get("match_id") or ev.get("match_id") or "")
        flatten_rows: list[dict[str, Any]] = []
        ex = self.trade_executor
        if ex is not None:
            try:
                flatten_rows = list(
                    ex.maybe_flatten_for_event(ev, require_protect_window=False)
                    or []
                )
            except Exception as e:  # noqa: BLE001
                flatten_rows = [
                    {
                        "quoted_at": lib.now_cn_iso(),
                        "status": "flatten_error",
                        "error": str(e),
                        "match_id": mid,
                        "event_key": key,
                    }
                ]
                print(f"ALERT AF flatten failed match={mid}: {e}", flush=True)
        bundle = {
            "quoted_at": lib.now_cn_iso(),
            "trigger": "score_change",
            "mode": "pitch_gate_flatten_or",
            "event_key": key,
            "match_id": mid,
            "home": ev.get("home"),
            "away": ev.get("away"),
            "home_score": ev.get("home_score"),
            "away_score": ev.get("away_score"),
            "count": 0,
            "opportunity_count": 0,
            "flatten_attempts": flatten_rows,
            "flatten_count": len(flatten_rows),
            "pitch_gate": dict(extra.get("pitch_gate") or {}),
        }
        print(
            f"pitch-gate → FLATTEN_OR match_id={mid} key={key} "
            f"reason={(extra.get('pitch_gate') or {}).get('reason')} "
            f"flatten={len(flatten_rows)}",
            flush=True,
        )
        self._push_result(
            QuoteJobResult(
                bundles=[bundle],
                seen_keys=[],
                ft_match_ids=[],
                event_key=key,
                match_id=mid,
                kind="flatten",
            )
        )

    def _run_quote(self, job: dict[str, Any]) -> None:
        import quote_lib as lib

        ev = job.get("ev") if isinstance(job.get("ev"), dict) else {}
        extra = job.get("extra") if isinstance(job.get("extra"), dict) else {}
        key = str(job.get("event_key") or "")
        mid = str(job.get("match_id") or ev.get("match_id") or "")
        if self._key_revoked(key):
            print(
                f"clob-worker → SKIP revoked key={key} match_id={mid}",
                flush=True,
            )
            self._finish_flight(key)
            self._push_result(
                QuoteJobResult(
                    bundles=[],
                    seen_keys=[key] if key else [],
                    ft_match_ids=[],
                    event_key=key,
                    match_id=mid,
                    kind="quote",
                    skipped=True,
                )
            )
            return

        flatten_rows: list[dict[str, Any]] = []
        ex = self.trade_executor
        if extra.get("skip_flatten"):
            flatten_rows = []
        elif ex is not None:
            try:
                flatten_rows = list(ex.maybe_flatten_for_event(ev) or [])
            except Exception as e:  # noqa: BLE001
                flatten_rows = [
                    {
                        "quoted_at": lib.now_cn_iso(),
                        "status": "flatten_error",
                        "error": str(e),
                        "match_id": mid,
                        "event_key": key,
                    }
                ]

        retry_needed = True
        bundles: list[dict[str, Any]] = []
        ft_ids: list[str] = []
        try:
            bundle = lib.quote_bridge_event(
                self.root,
                ev,
                proxy=self.proxy,
                include_props=self.include_props,
                include_exact=self.include_exact,
                eps=self.eps,
                fee_rate=self.fee_rate,
                min_net=self.min_net,
                persist=True,
                trade_executor=self.trade_executor,
                market_cache=self.market_cache,
            )
            if flatten_rows:
                bundle["flatten_attempts"] = flatten_rows
                bundle["flatten_count"] = len(flatten_rows)
            if extra.get("mode"):
                bundle["mode"] = extra["mode"]
            if extra.get("pitch_gate"):
                bundle["pitch_gate"] = extra["pitch_gate"]
            if extra.get("t10"):
                bundle["t10"] = extra["t10"]
            bundles.append(bundle)
            retry_needed = _quote_retry_needed(bundle)
            if str(ev.get("type") or "") == "match_finished" and not retry_needed:
                if mid:
                    ft_ids.append(mid)
                    if self.market_cache is not None:
                        try:
                            self.market_cache.drop(mid)
                        except Exception:  # noqa: BLE001
                            pass
        except Exception as e:  # noqa: BLE001
            err_row: dict[str, Any] = {
                "quoted_at": lib.now_cn_iso(),
                "error": str(e),
                "event_key": key,
                "match_id": mid,
                "trigger": ev.get("type"),
            }
            if flatten_rows:
                err_row["flatten_attempts"] = flatten_rows
            bundles.append(err_row)
            print(f"ALERT quote failed (will retry) key={key}: {e}", flush=True)
            retry_needed = True

        if self._key_revoked(key) and ex is not None and mid:
            try:
                ex.cancel_rest_orders_for_match(mid, reason="dqd_reversal")
            except Exception as e:  # noqa: BLE001
                print(
                    f"ALERT rest cancel after revoked quote failed match={mid}: {e}",
                    flush=True,
                )

        seen_keys = [key] if key and not retry_needed else []
        self._finish_flight(key)
        self._push_result(
            QuoteJobResult(
                bundles=bundles,
                seen_keys=seen_keys,
                ft_match_ids=ft_ids,
                event_key=key,
                match_id=mid,
                kind="quote",
            )
        )


def _quote_retry_needed(bundle: dict[str, Any]) -> bool:
    for q in bundle.get("quotes") or []:
        attempt = q.get("trade_attempt") if isinstance(q, dict) else None
        if not isinstance(attempt, dict):
            continue
        if attempt.get("status") == "error":
            return True
        size_policy = (
            attempt.get("size_policy")
            if isinstance(attempt.get("size_policy"), dict)
            else {}
        )
        plan = attempt.get("plan") if isinstance(attempt.get("plan"), dict) else {}
        try:
            target = float(size_policy.get("target_usdc"))
            before = float(size_policy.get("already_usdc") or 0.0)
            filled = float(plan.get("usdc") or 0.0)
        except (TypeError, ValueError):
            continue
        if attempt.get("success") and before + filled + 1e-9 < target:
            return True
    return False


def _now() -> str:
    import quote_lib as lib

    return lib.now_cn_iso()
