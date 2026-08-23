"""Shared Chromium + one tracker tab per in-play match.

Playwright's sync API is thread-affine, so a dedicated browser thread owns
the process. Gate sessions ``evaluate`` an already-open tab (``new_page`` only
on first open / URL change). While an open waits for the animation root, the
owner thread drains pending READ jobs so a goal sample is not stuck behind
warm. Lease tokens (not a shared counter) keep FT close from stealing a
later session's tab.
"""

from __future__ import annotations

import itertools
import logging
import os
import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

logger = logging.getLogger("pm_quote.dom_page_pool")

DOM_STATE_JS = """
() => {
  const root = document.querySelector('.football-animate');
  if (!root) return null;
  const cls = (el) => {
    if (!el) return '';
    const c = el.className;
    return String((c && c.baseVal !== undefined) ? c.baseVal : (c || ''));
  };
  const txt = (sel) => {
    const el = root.querySelector(sel);
    return el ? (el.textContent || '').replace(/\\s+/g, ' ').trim().slice(0, 80) : null;
  };
  const pop = root.querySelector('.pop-box');
  const marks = ['possession-rect', 'attack-move', 'dangerous-attack-move',
                 'attack', 'dangerous-attack', 'ball', 'net', 'penalty-box'];
  const present = new Set();
  root.querySelectorAll('*').forEach((el) => {
    cls(el).split(/\\s+/).forEach((c) => { if (marks.includes(c)) present.add(c); });
  });
  return {
    pop_box: txt('.pop-box'),
    pop_class: cls(pop),
    center_box: txt('.center-box'),
    root_class: cls(root),
    marks: Array.from(present).sort(),
  };
}
"""

_PRIO_READ = 0
_PRIO_CLOSE = 1
_PRIO_OPEN = 2
_PRIO_SHUTDOWN = 3


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        return int(float(str(raw).strip()))
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float, *, lo: float, hi: float) -> float:
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        return max(lo, min(hi, float(str(raw).strip())))
    except (TypeError, ValueError):
        return default


def pool_max_pages() -> int:
    return max(1, _env_int("QUOTE_DOM_POOL_MAX", 24))


def warm_open_timeout_s() -> float:
    return _env_float("QUOTE_DOM_WARM_OPEN_TIMEOUT_S", 3.0, lo=1.0, hi=15.0)


class DomBackend(Protocol):
    def start(self) -> None: ...
    def ensure_open(
        self,
        match_id: str,
        url: str,
        *,
        timeout_s: float,
        max_pages: int = 0,
        protect: frozenset[str] | None = None,
    ) -> tuple[bool, str | None, bool]: ...
    def read(self, match_id: str) -> tuple[dict[str, Any] | None, str | None]: ...
    def close_page(self, match_id: str) -> None: ...
    def opened_ids(self) -> set[str]: ...
    def shutdown(self) -> None: ...


def _evict_one(
    pages: dict[str, Any],
    used_at: dict[str, float],
    *,
    max_pages: int,
    protect: set[str],
    on_close: Callable[[str, Any], None] | None = None,
) -> None:
    if max_pages <= 0 or len(pages) < max_pages:
        return
    victims = [
        (float(used_at.get(mid) or 0), mid)
        for mid in list(pages)
        if mid not in protect
    ]
    if not victims:
        return
    victims.sort()
    victim = victims[0][1]
    slot = pages.pop(victim, None)
    used_at.pop(victim, None)
    if on_close is not None:
        on_close(victim, slot)


class MemoryDomBackend:
    """In-process fake for smokes (no Playwright)."""

    def __init__(self) -> None:
        self.pages: dict[str, dict[str, Any]] = {}
        self.open_calls: list[str] = []
        self.close_calls: list[str] = []
        self.started = 0
        self.dom_by_match: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()
        self._used: dict[str, float] = {}
        self.open_delay_s = 0.0
        self.pump_during_open: Callable[[], None] | None = None

    def start(self) -> None:
        if not self.started:
            self.started = 1

    def ensure_open(
        self,
        match_id: str,
        url: str,
        *,
        timeout_s: float = 15.0,
        max_pages: int = 0,
        protect: frozenset[str] | None = None,
    ) -> tuple[bool, str | None, bool]:
        del timeout_s
        mid = str(match_id or "")
        page_url = str(url or "")
        if not mid or not page_url:
            return False, "no_page_url", False
        if self.open_delay_s > 0:
            deadline = time.monotonic() + float(self.open_delay_s)
            while time.monotonic() < deadline:
                pump = self.pump_during_open
                if pump is not None:
                    pump()
                time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
        with self._lock:
            hold = set(protect or ())
            hold.add(mid)
            if mid not in self.pages:
                _evict_one(
                    self.pages,
                    self._used,
                    max_pages=max_pages,
                    protect=hold,
                    on_close=lambda victim, _slot: self.close_calls.append(victim),
                )
            existing = self.pages.get(mid)
            if existing and existing.get("url") == page_url:
                self._used[mid] = time.monotonic()
                return True, None, True
            self.pages[mid] = {"url": page_url}
            self._used[mid] = time.monotonic()
            self.open_calls.append(mid)
            return True, None, False

    def read(self, match_id: str) -> tuple[dict[str, Any] | None, str | None]:
        mid = str(match_id or "")
        with self._lock:
            if mid not in self.pages:
                return None, "not_open"
            self._used[mid] = time.monotonic()
            dom = self.dom_by_match.get(mid) or {
                "pop_box": "进攻",
                "pop_class": "pop-box home",
                "center_box": "10:00 1 : 0",
                "root_class": "football-animate",
                "marks": [],
            }
            return dict(dom), None

    def close_page(self, match_id: str) -> None:
        mid = str(match_id or "")
        with self._lock:
            if mid in self.pages:
                self.pages.pop(mid, None)
                self._used.pop(mid, None)
                self.close_calls.append(mid)

    def opened_ids(self) -> set[str]:
        with self._lock:
            return set(self.pages)

    def shutdown(self) -> None:
        with self._lock:
            self.pages.clear()
            self._used.clear()


def _probe_animation_frame(page: Any) -> Any:
    try:
        if page.locator(".football-animate").count() > 0:
            return page
    except Exception:  # noqa: BLE001
        pass
    try:
        iframe = page.locator("iframe.md-anim-iframe")
        if iframe.count() > 0:
            handle = iframe.first.element_handle()
            frame = handle.content_frame() if handle is not None else None
            if frame is not None and frame.locator(".football-animate").count() > 0:
                return frame
    except Exception:  # noqa: BLE001
        pass
    return None


def _find_animation_frame(
    page: Any,
    *,
    deadline: float,
    pump: Callable[[], None] | None = None,
    cancelled: threading.Event | None = None,
) -> Any:
    while time.monotonic() < deadline:
        if cancelled is not None and cancelled.is_set():
            return _probe_animation_frame(page)
        found = _probe_animation_frame(page)
        if found is not None:
            return found
        if pump is not None:
            pump()
            time.sleep(0.05)
        else:
            time.sleep(0.1)
    return _probe_animation_frame(page)


@dataclass(order=True)
class _Job:
    priority: int
    seq: int
    op: str = field(compare=False)
    match_id: str = field(default="", compare=False)
    url: str = field(default="", compare=False)
    timeout_s: float = field(default=15.0, compare=False)
    max_pages: int = field(default=0, compare=False)
    protect: frozenset[str] = field(default_factory=frozenset, compare=False)
    reply: queue.Queue = field(default_factory=queue.Queue, compare=False)
    cancelled: threading.Event = field(default_factory=threading.Event, compare=False)


class PlaywrightDomBackend:
    """One Chromium; all sync Playwright calls run on ``_loop``."""

    def __init__(self) -> None:
        self._q: queue.PriorityQueue[_Job] = queue.PriorityQueue()
        self._seq = itertools.count()
        self._thread: threading.Thread | None = None
        self._started = threading.Event()
        self._dead: str | None = None
        self._closing = False
        self._life = threading.Lock()

    def start(self) -> None:
        with self._life:
            if self._thread is not None and self._thread.is_alive():
                return
            self._dead = None
            self._closing = False
            self._started.clear()
            self._thread = threading.Thread(
                target=self._loop, name="dom-chromium", daemon=True
            )
            self._thread.start()
            thread = self._thread
        if not self._started.wait(timeout=30.0):
            raise RuntimeError(self._dead or "dom chromium start timeout")
        if self._dead:
            raise RuntimeError(self._dead)
        if thread is not None and not thread.is_alive() and self._dead:
            raise RuntimeError(self._dead)

    def _fail_reply(self, job: _Job) -> Any:
        reason = self._dead or "dom_closing"
        if job.op == "read":
            return None, reason
        if job.op == "open":
            return False, reason, False
        if job.op == "ids":
            return set()
        return False

    def _submit(self, job: _Job, *, wait_s: float) -> Any:
        if self._dead or self._closing:
            return self._fail_reply(job)
        self._q.put(job)
        try:
            return job.reply.get(timeout=max(1.0, float(wait_s)))
        except queue.Empty:
            job.cancelled.set()
            return self._fail_reply(job) if job.op != "open" else (
                False,
                "dom_backend_timeout",
                False,
            )

    def ensure_open(
        self,
        match_id: str,
        url: str,
        *,
        timeout_s: float = 15.0,
        max_pages: int = 0,
        protect: frozenset[str] | None = None,
    ) -> tuple[bool, str | None, bool]:
        job = _Job(
            priority=_PRIO_OPEN,
            seq=next(self._seq),
            op="open",
            match_id=str(match_id or ""),
            url=str(url or ""),
            timeout_s=float(timeout_s),
            max_pages=int(max_pages or 0),
            protect=frozenset(protect or ()),
        )
        result = self._submit(job, wait_s=float(timeout_s) + 5.0)
        if isinstance(result, tuple) and len(result) >= 3:
            return bool(result[0]), result[1], bool(result[2])
        return False, "dom_backend_bad_reply", False

    def read(self, match_id: str) -> tuple[dict[str, Any] | None, str | None]:
        job = _Job(
            priority=_PRIO_READ,
            seq=next(self._seq),
            op="read",
            match_id=str(match_id or ""),
        )
        result = self._submit(job, wait_s=8.0)
        if isinstance(result, tuple) and len(result) >= 2:
            return result[0], result[1]
        return None, "dom_backend_bad_reply"

    def close_page(self, match_id: str) -> None:
        job = _Job(
            priority=_PRIO_CLOSE,
            seq=next(self._seq),
            op="close",
            match_id=str(match_id or ""),
        )
        self._submit(job, wait_s=8.0)

    def opened_ids(self) -> set[str]:
        job = _Job(priority=_PRIO_READ, seq=next(self._seq), op="ids")
        result = self._submit(job, wait_s=8.0)
        if isinstance(result, set):
            return result
        return set()

    def shutdown(self) -> None:
        with self._life:
            self._closing = True
            thread = self._thread
        if thread is None or not thread.is_alive():
            self._thread = None
            return
        job = _Job(priority=_PRIO_SHUTDOWN, seq=next(self._seq), op="shutdown")
        self._q.put(job)
        try:
            job.reply.get(timeout=45.0)
        except queue.Empty:
            logger.debug("dom chromium shutdown wait timed out")
        thread.join(timeout=8.0)
        with self._life:
            if thread is not self._thread:
                return
            if not thread.is_alive():
                self._thread = None

    def _pump_urgent(
        self,
        pages: dict[str, dict[str, Any]],
        used_at: dict[str, float],
        *,
        skip_match: str,
    ) -> None:
        deferred: list[_Job] = []
        while True:
            try:
                nxt = self._q.get_nowait()
            except queue.Empty:
                break
            if nxt.op in {"read", "ids"} or (
                nxt.op == "close" and nxt.match_id != skip_match
            ):
                self._dispatch(nxt, pages, used_at, context=None)
            else:
                deferred.append(nxt)
        for nxt in deferred:
            self._q.put(nxt)

    def _dispatch(
        self,
        job: _Job,
        pages: dict[str, dict[str, Any]],
        used_at: dict[str, float],
        context: Any,
    ) -> None:
        if job.op == "ids":
            job.reply.put(set(pages))
            return
        if job.op == "read":
            slot = pages.get(job.match_id)
            if not slot:
                job.reply.put((None, "not_open"))
                return
            used_at[job.match_id] = time.monotonic()
            try:
                dom = slot["frame"].evaluate(DOM_STATE_JS)
            except Exception as e:  # noqa: BLE001
                job.reply.put(
                    (
                        None,
                        str(e).splitlines()[0][:200] if str(e) else "dom_read_failed",
                    )
                )
                return
            if not isinstance(dom, dict):
                job.reply.put((None, "no_animation_root"))
                return
            job.reply.put((dom, None))
            return
        if job.op == "close":
            slot = pages.pop(job.match_id, None)
            used_at.pop(job.match_id, None)
            if slot is not None:
                try:
                    slot["page"].close()
                except Exception:  # noqa: BLE001
                    pass
            job.reply.put(True)
            return
        if job.op != "open":
            job.reply.put(None)
            return
        if context is None:
            self._q.put(job)
            return
        if not job.match_id or not job.url:
            job.reply.put((False, "no_page_url", False))
            return
        existing = pages.get(job.match_id)
        if (
            existing
            and existing.get("url") == job.url
            and existing.get("frame") is not None
        ):
            used_at[job.match_id] = time.monotonic()
            job.reply.put((True, None, True))
            return
        if existing is not None:
            try:
                existing["page"].close()
            except Exception:  # noqa: BLE001
                pass
            pages.pop(job.match_id, None)
            used_at.pop(job.match_id, None)
        hold = set(job.protect)
        hold.add(job.match_id)
        _evict_one(
            pages,
            used_at,
            max_pages=job.max_pages,
            protect=hold,
            on_close=lambda _victim, slot: _close_slot(slot),
        )
        page = context.new_page()
        timeout_ms = int(max(1.0, float(job.timeout_s)) * 1000)
        page.goto(job.url, wait_until="domcontentloaded", timeout=timeout_ms)
        frame = _find_animation_frame(
            page,
            deadline=time.monotonic() + max(1.0, float(job.timeout_s)),
            pump=lambda: self._pump_urgent(
                pages, used_at, skip_match=job.match_id
            ),
            cancelled=job.cancelled,
        )
        if frame is None:
            try:
                page.close()
            except Exception:  # noqa: BLE001
                pass
            job.reply.put((False, "no_animation_frame", False))
            return
        pages[job.match_id] = {"page": page, "frame": frame, "url": job.url}
        used_at[job.match_id] = time.monotonic()
        job.reply.put((True, None, False))

    def _loop(self) -> None:
        try:
            from playwright.sync_api import sync_playwright
        except Exception:  # noqa: BLE001
            self._dead = "playwright_not_installed"
            self._started.set()
            return
        pw = None
        browser = None
        context = None
        pages: dict[str, dict[str, Any]] = {}
        used_at: dict[str, float] = {}
        try:
            pw = sync_playwright().start()
            browser = pw.chromium.launch(headless=True)
            context = browser.new_context(
                viewport={"width": 1280, "height": 900},
                locale="zh-CN",
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/131.0.0.0 Safari/537.36"
                ),
            )
            self._started.set()
            print("dom-pool → chromium up (shared)", flush=True)
            while True:
                job: _Job = self._q.get()
                if job.op == "shutdown":
                    job.reply.put(True)
                    break
                try:
                    self._dispatch(job, pages, used_at, context)
                except Exception as e:  # noqa: BLE001
                    msg = str(e).splitlines()[0][:200] if str(e) else "dom_backend_error"
                    if "Executable doesn't exist" in msg or "playwright install" in msg:
                        msg = "playwright_browser_missing"
                    if job.op == "open":
                        job.reply.put((False, msg, False))
                    elif job.op == "read":
                        job.reply.put((None, msg))
                    elif job.op == "ids":
                        job.reply.put(set())
                    else:
                        job.reply.put(False)
        except Exception as e:  # noqa: BLE001
            msg = str(e).splitlines()[0][:200] if str(e) else "dom_chromium_failed"
            if "Executable doesn't exist" in msg or "playwright install" in msg:
                self._dead = "playwright_browser_missing"
            else:
                self._dead = msg
            self._started.set()
        finally:
            for slot in list(pages.values()):
                _close_slot(slot)
            pages.clear()
            used_at.clear()
            for obj in (context, browser):
                if obj is not None:
                    try:
                        obj.close()
                    except Exception:  # noqa: BLE001
                        pass
            if pw is not None:
                try:
                    pw.stop()
                except Exception:  # noqa: BLE001
                    pass


def _close_slot(slot: dict[str, Any] | None) -> None:
    if not slot:
        return
    try:
        slot["page"].close()
    except Exception:  # noqa: BLE001
        pass


class DomPagePool:
    """Lease-token tabs: gate sessions reuse; warm keeps idle playing pages."""

    def __init__(
        self,
        backend: DomBackend | None = None,
        *,
        max_pages: int | None = None,
    ) -> None:
        self._backend = backend
        self._max = max(1, int(max_pages if max_pages is not None else pool_max_pages()))
        self._lock = threading.Lock()
        self._life = threading.Lock()
        self._leases: dict[str, set[int]] = {}
        self._used: dict[str, float] = {}
        self._close_when_idle: set[str] = set()
        self._lease_seq = itertools.count(1)

    @property
    def max_pages(self) -> int:
        return self._max

    def _be(self) -> DomBackend:
        with self._life:
            if self._backend is None:
                self._backend = PlaywrightDomBackend()
            be = self._backend
        be.start()
        return be

    def start(self) -> None:
        self._be()

    def _leased_ids(self) -> set[str]:
        with self._lock:
            return {mid for mid, toks in self._leases.items() if toks}

    def _grant_lease(self, match_id: str) -> int:
        token = next(self._lease_seq)
        with self._lock:
            self._leases.setdefault(match_id, set()).add(token)
            self._used[match_id] = time.monotonic()
        return token

    def ensure_open(
        self,
        match_id: str,
        url: str,
        *,
        timeout_s: float = 15.0,
        lease: bool = False,
    ) -> tuple[bool, str | None, bool, int]:
        mid = str(match_id or "").strip()
        page_url = str(url or "").strip()
        if not mid or not page_url:
            return False, "no_page_url", False, 0
        token = 0
        if lease:
            token = self._grant_lease(mid)
        protect = frozenset(self._leased_ids() | {mid})
        be = self._be()
        ok, err, reused = be.ensure_open(
            mid,
            page_url,
            timeout_s=timeout_s,
            max_pages=self._max,
            protect=protect,
        )
        if not ok:
            if token:
                self.release_lease(mid, token)
            return False, err, False, 0
        with self._lock:
            self._used[mid] = time.monotonic()
        return True, None, reused, token

    def acquire_lease(self, match_id: str) -> int:
        mid = str(match_id or "").strip()
        if not mid:
            return 0
        return self._grant_lease(mid)

    def release_lease(self, match_id: str, token: int = 0) -> None:
        mid = str(match_id or "").strip()
        if not mid or not token:
            return
        should_close = False
        with self._lock:
            toks = self._leases.get(mid)
            if toks:
                toks.discard(int(token))
                if not toks:
                    self._leases.pop(mid, None)
            should_close = mid not in self._leases and mid in self._close_when_idle
            if should_close:
                self._close_when_idle.discard(mid)
        if should_close:
            self.close_page(mid)

    def read(self, match_id: str) -> tuple[dict[str, Any] | None, str | None]:
        mid = str(match_id or "").strip()
        with self._lock:
            self._used[mid] = time.monotonic()
        return self._be().read(mid)

    def close_page(self, match_id: str, *, force: bool = False) -> bool:
        """Close the tab now, or mark it to close when the last lease drops."""
        mid = str(match_id or "").strip()
        if not mid:
            return False
        with self._lock:
            if not force and self._leases.get(mid):
                self._close_when_idle.add(mid)
                return False
            self._close_when_idle.discard(mid)
            self._used.pop(mid, None)
            if force:
                self._leases.pop(mid, None)
        self._be().close_page(mid)
        return True

    def opened_ids(self) -> set[str]:
        return self._be().opened_ids()

    def lease_count(self, match_id: str) -> int:
        with self._lock:
            return len(self._leases.get(str(match_id or "")) or ())

    def close_absent(self, keep: set[str]) -> list[str]:
        """Drop idle tabs that are no longer in the playing set."""
        closed: list[str] = []
        keep = {str(x) for x in keep}
        for mid in list(self.opened_ids()):
            if mid in keep:
                continue
            if self.close_page(mid):
                closed.append(mid)
        return closed

    def shutdown(self) -> None:
        with self._life:
            be = self._backend
            self._backend = None
        if be is not None:
            try:
                be.shutdown()
            except Exception:  # noqa: BLE001
                logger.debug("dom pool shutdown failed", exc_info=True)
        with self._lock:
            self._leases.clear()
            self._used.clear()
            self._close_when_idle.clear()


class DomReader:
    """Handle to a pooled tracker tab. ``close()`` drops this handle's token."""

    def __init__(
        self,
        page_url: str,
        *,
        match_id: str = "",
        pool: DomPagePool | None = None,
        open_timeout_s: float | None = None,
    ) -> None:
        self.page_url = str(page_url or "")
        self.match_id = str(match_id or "")
        self.pool = pool
        self.reused = False
        self.open_timeout_s = float(
            open_timeout_s
            if open_timeout_s is not None
            else os.getenv("QUOTE_DOM_OPEN_TIMEOUT_S", "15") or 15
        )
        self._legacy: Any = None
        self._lease_token = 0
        self._closed = False

    def open(self) -> tuple[bool, str | None]:
        if self.pool is not None:
            ok, err, reused, token = self.pool.ensure_open(
                self.match_id,
                self.page_url,
                timeout_s=self.open_timeout_s,
                lease=True,
            )
            self.reused = bool(reused)
            self._lease_token = int(token or 0) if ok else 0
            self._closed = not ok
            return ok, err
        self._legacy = _LegacyDomBrowser(
            self.page_url, open_timeout_s=self.open_timeout_s
        )
        return self._legacy.open()

    def read(self) -> tuple[dict[str, Any] | None, str | None]:
        if self.pool is not None:
            return self.pool.read(self.match_id)
        if self._legacy is not None:
            return self._legacy.read()
        return None, "not_open"

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self.pool is not None:
            token = self._lease_token
            self._lease_token = 0
            if token:
                self.pool.release_lease(self.match_id, token)
            return
        if self._legacy is not None:
            self._legacy.close()
            self._legacy = None


class _LegacyDomBrowser:
    """Own Chromium per handle — only when no pool is attached."""

    def __init__(self, page_url: str, *, open_timeout_s: float = 15.0) -> None:
        self.page_url = str(page_url or "")
        self.open_timeout_s = float(open_timeout_s)
        self._pw: Any = None
        self._browser: Any = None
        self._context: Any = None
        self._page: Any = None
        self._frame: Any = None

    def open(self) -> tuple[bool, str | None]:
        if not self.page_url:
            return False, "no_page_url"
        try:
            from playwright.sync_api import sync_playwright
        except Exception:  # noqa: BLE001
            return False, "playwright_not_installed"
        try:
            self._pw = sync_playwright().start()
            self._browser = self._pw.chromium.launch(headless=True)
            self._context = self._browser.new_context(
                viewport={"width": 1280, "height": 900},
                locale="zh-CN",
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/131.0.0.0 Safari/537.36"
                ),
            )
            timeout_ms = int(max(1.0, self.open_timeout_s) * 1000)
            self._page = self._context.new_page()
            self._page.goto(
                self.page_url, wait_until="domcontentloaded", timeout=timeout_ms
            )
            self._frame = _find_animation_frame(
                self._page, deadline=time.monotonic() + self.open_timeout_s
            )
            if self._frame is None:
                self.close()
                return False, "no_animation_frame"
            return True, None
        except Exception as e:  # noqa: BLE001
            msg = str(e).splitlines()[0][:200] if str(e) else "dom_open_failed"
            self.close()
            if "Executable doesn't exist" in msg or "playwright install" in msg:
                return False, "playwright_browser_missing"
            return False, msg

    def read(self) -> tuple[dict[str, Any] | None, str | None]:
        if self._frame is None:
            return None, "not_open"
        try:
            dom = self._frame.evaluate(DOM_STATE_JS)
        except Exception as e:  # noqa: BLE001
            return None, str(e).splitlines()[0][:200] if str(e) else "dom_read_failed"
        if not isinstance(dom, dict):
            return None, "no_animation_root"
        return dom, None

    def close(self) -> None:
        for attr in ("_context", "_browser"):
            obj = getattr(self, attr, None)
            if obj is not None:
                try:
                    obj.close()
                except Exception:  # noqa: BLE001
                    pass
            setattr(self, attr, None)
        if self._pw is not None:
            try:
                self._pw.stop()
            except Exception:  # noqa: BLE001
                pass
            self._pw = None
        self._page = None
        self._frame = None
