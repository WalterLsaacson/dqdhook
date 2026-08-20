"""Observe-only Dongqiudi live/animation screenshots after DQD goal-up."""

from __future__ import annotations

import logging
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import quote_lib as lib
from observe_timing import SAMPLE_COUNT, SAMPLE_INTERVAL_S

logger = logging.getLogger("pm_quote.dqd_stream_observe")

_active: "DqdStreamObserver | None" = None
_active_lock = threading.Lock()

_DONGQIUDI_SCRIPTS = Path(__file__).resolve().parents[2] / "dongqiudi-match" / "scripts"
if str(_DONGQIUDI_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_DONGQIUDI_SCRIPTS))
import dqd_live  # noqa: E402


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def observe_path(root: Path) -> Path:
    return lib.data_dir(root) / "dqd_stream_observe.jsonl"


def frames_dir(root: Path) -> Path:
    return lib.data_dir(root) / "dqd_stream_frames"


def set_active_observer(observer: "DqdStreamObserver | None") -> None:
    global _active
    with _active_lock:
        _active = observer


def get_active_observer() -> "DqdStreamObserver | None":
    with _active_lock:
        return _active


def _safe_part(raw: Any) -> str:
    text = "".join(ch if ch.isalnum() or ch in ("-", "_", ".") else "_" for ch in str(raw or ""))
    return text.strip("_") or "na"


@dataclass
class _ObserveJob:
    match_id: str
    event_key: str
    dqd_ts: str
    home: str
    away: str
    home_score: Any
    away_score: Any
    t0_mono: float


class DqdStreamObserver:
    def __init__(
        self,
        root: Path,
        *,
        discover_fn: Callable[..., dict[str, Any]] | None = None,
        capture_stream_fn: Callable[[str, Path], tuple[bool, str | None]] | None = None,
        capture_page_fn: Callable[[str, Path], tuple[bool, str | None]] | None = None,
    ) -> None:
        self.root = Path(root)
        self._q: queue.Queue[_ObserveJob | None] = queue.Queue()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._workers: list[threading.Thread] = []
        self._workers_lock = threading.Lock()
        self._discover_fn = discover_fn or dqd_live.discover_live_surface
        self._capture_stream_fn = capture_stream_fn or self._capture_stream_ffmpeg
        self._capture_page_fn = capture_page_fn or self._capture_page_playwright

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="dqd-stream-observe", daemon=True)
        self._thread.start()
        set_active_observer(self)
        logger.info("DQD stream observe on → %s", observe_path(self.root))
        # Load PaddleOCR in the background so the first goal frame is not cold-start.
        threading.Thread(
            target=self._warmup_pitch_state,
            name="pitch-state-ocr-warmup",
            daemon=True,
        ).start()

    def stop(self) -> None:
        self._stop.set()
        self._q.put(None)
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        with self._workers_lock:
            workers = list(self._workers)
        for worker in workers:
            worker.join(timeout=1.0)
        if get_active_observer() is self:
            set_active_observer(None)

    def enqueue_event(self, ev: dict[str, Any], *, event_key: str) -> bool:
        if self._stop.is_set():
            return False
        if str(ev.get("type") or "") != "score_change":
            return False
        match_id = str(ev.get("match_id") or "")
        if not match_id:
            return False
        job = _ObserveJob(
            match_id=match_id,
            event_key=str(event_key or ""),
            dqd_ts=str(ev.get("ts") or lib.now_cn_iso()),
            home=str(ev.get("home") or ""),
            away=str(ev.get("away") or ""),
            home_score=ev.get("home_score"),
            away_score=ev.get("away_score"),
            t0_mono=time.monotonic(),
        )
        row = self._capture_row(job, sample_i=0, elapsed_s=0.0)
        self._write_rows([row])
        self._schedule_judge_frame(row)
        self._q.put(job)
        return True

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                job = self._q.get(timeout=0.5)
            except queue.Empty:
                continue
            if job is None:
                return
            worker = threading.Thread(
                target=self._run_job_safe,
                args=(job,),
                name=f"dqd-stream-job-{job.match_id}",
                daemon=True,
            )
            with self._workers_lock:
                self._workers = [w for w in self._workers if w.is_alive()]
                self._workers.append(worker)
            worker.start()

    def _run_job_safe(self, job: _ObserveJob) -> None:
        try:
            self._run_job(job)
        except Exception:  # noqa: BLE001
            logger.exception("dqd stream observe failed match=%s", job.match_id)

    def _run_job(self, job: _ObserveJob) -> None:
        for sample_i in range(1, SAMPLE_COUNT):
            if self._stop.is_set():
                return
            target = job.t0_mono + sample_i * SAMPLE_INTERVAL_S
            while not self._stop.is_set():
                now = time.monotonic()
                if now >= target:
                    break
                time.sleep(min(0.25, target - now))
            if self._stop.is_set():
                return
            elapsed = round(time.monotonic() - job.t0_mono, 3)
            row = self._capture_row(job, sample_i=sample_i, elapsed_s=elapsed)
            self._write_rows([row])
            # Keep the sample clock on capture timing; OCR runs off-thread.
            self._schedule_judge_frame(row)

    def _capture_row(self, job: _ObserveJob, *, sample_i: int, elapsed_s: float) -> dict[str, Any]:
        discover_timeout_s = float(os.getenv("QUOTE_DQD_STREAM_DISCOVER_TIMEOUT_S", "2.0") or 2.0)
        try:
            info = self._discover_fn(
                job.match_id, root=self.root, timeout=discover_timeout_s
            )
        except TypeError:
            # Backward-compatible for test mocks.
            info = self._discover_fn(job.match_id, root=self.root)
        frame_dir = frames_dir(self.root) / _safe_part(job.match_id) / _safe_part(job.event_key)
        frame_dir.mkdir(parents=True, exist_ok=True)
        frame_path = frame_dir / f"{sample_i:02d}_{int(round(elapsed_s)):02d}s.jpg"
        ok = False
        err: str | None = None
        method = "skipped"
        frame_kind = "page"
        stream_url = str(info.get("stream_url") or "")
        page_url = str(info.get("page_url") or "")
        surface = str(info.get("surface") or "none")
        # Animation surfaces must stay on page/OCR path even if a stream URL leaks in.
        prefer_page = surface == "animation" or (bool(page_url) and not stream_url)

        if stream_url and not prefer_page:
            method = "ffmpeg"
            ok, err = self._capture_stream_fn(stream_url, frame_path)
            if ok:
                frame_kind = "video"
            elif page_url:
                method = "playwright"
                ok, err, frame_kind = self._capture_page(page_url, frame_path)
        elif page_url:
            method = "playwright"
            ok, err, frame_kind = self._capture_page(page_url, frame_path)
        elif stream_url:
            method = "ffmpeg"
            ok, err = self._capture_stream_fn(stream_url, frame_path)
            if ok:
                frame_kind = "video"
        else:
            err = "no_live_surface"

        return {
            "sampled_at": lib.now_cn_iso(),
            "match_id": job.match_id,
            "event_key": job.event_key,
            "dqd_ts": job.dqd_ts,
            "elapsed_s": elapsed_s,
            "sample_i": sample_i,
            "home": job.home,
            "away": job.away,
            "home_score": job.home_score,
            "away_score": job.away_score,
            "score": (
                f"{job.home_score}-{job.away_score}"
                if job.home_score is not None and job.away_score is not None
                else None
            ),
            "surface": surface,
            "page_url": page_url or None,
            "stream_url": stream_url or None,
            "capture_method": method,
            "frame_kind": frame_kind,
            "frame_path": str(frame_path) if ok else None,
            "ok": bool(ok),
            "error": None if ok else (err or "capture_failed"),
            "raw_hint": info.get("raw_hint") if isinstance(info, dict) else None,
        }

    def _write_rows(self, rows: list[dict[str, Any]]) -> None:
        lib.append_jsonl(observe_path(self.root), rows)

    def _capture_page(
        self,
        page_url: str,
        frame_path: Path,
    ) -> tuple[bool, str | None, str]:
        result = self._capture_page_fn(page_url, frame_path)
        if isinstance(result, tuple) and len(result) >= 3:
            ok, err, frame_kind = result[0], result[1], result[2]
            return bool(ok), err, str(frame_kind or "page")
        if isinstance(result, tuple) and len(result) >= 2:
            ok, err = result[0], result[1]
            return bool(ok), err, "page"
        return False, "invalid_capture_result", "page"

    def _schedule_judge_frame(self, row: dict[str, Any]) -> None:
        """Run pitch-state off the capture/event threads so sampling stays on schedule."""
        if not _env_bool("QUOTE_PITCH_STATE", False):
            return
        if row.get("ok") is not True or not row.get("frame_path"):
            return
        sample_i = row.get("sample_i")
        match_id = row.get("match_id")
        threading.Thread(
            target=self._maybe_judge_frame,
            args=(row,),
            name=f"pitch-state-{match_id}-{sample_i}",
            daemon=True,
        ).start()

    def _maybe_judge_frame(self, row: dict[str, Any]) -> None:
        if not _env_bool("QUOTE_PITCH_STATE", False):
            return
        if row.get("ok") is not True or not row.get("frame_path"):
            return
        try:
            pitch_state_scripts = Path(__file__).resolve().parents[2] / "pitch-state" / "scripts"
            if str(pitch_state_scripts) not in sys.path:
                sys.path.insert(0, str(pitch_state_scripts))
            from pipeline import judge_inputs  # type: ignore

            judge_inputs(
                image=Path(str(row["frame_path"])),
                match_id=str(row.get("match_id") or "") or None,
                event_key=str(row.get("event_key") or "") or None,
                frame_meta={
                    "sample_i": row.get("sample_i"),
                    "elapsed_s": row.get("elapsed_s"),
                    "surface": row.get("surface"),
                    "stream_url": row.get("stream_url"),
                    "page_url": row.get("page_url"),
                    "frame_kind": row.get("frame_kind"),
                    "match_id": row.get("match_id"),
                    "event_key": row.get("event_key"),
                },
                append_output=True,
                write_sidecars=True,
            )
        except Exception:  # noqa: BLE001
            logger.exception(
                "pitch-state frame judge failed match=%s sample=%s",
                row.get("match_id"),
                row.get("sample_i"),
            )

    def _warmup_pitch_state(self) -> None:
        if not _env_bool("QUOTE_PITCH_STATE", False):
            return
        try:
            pitch_state_scripts = Path(__file__).resolve().parents[2] / "pitch-state" / "scripts"
            if str(pitch_state_scripts) not in sys.path:
                sys.path.insert(0, str(pitch_state_scripts))
            from animation_ocr import warmup_ocr  # type: ignore

            info = warmup_ocr()
            if info.get("ok"):
                print(f"pitch-state OCR ready ({info.get('latency_ms')}ms)", flush=True)
            else:
                print(f"pitch-state OCR warmup failed: {info.get('error')}", flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"pitch-state OCR warmup failed: {e}", flush=True)
            logger.exception("pitch-state OCR warmup failed")

    @staticmethod
    def _capture_stream_ffmpeg(stream_url: str, frame_path: Path) -> tuple[bool, str | None]:
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            return False, "ffmpeg_not_found"
        try:
            proc = subprocess.run(
                [
                    ffmpeg,
                    "-y",
                    "-loglevel",
                    "error",
                    "-i",
                    stream_url,
                    "-frames:v",
                    "1",
                    str(frame_path),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=15,
                check=False,
            )
        except Exception as e:  # noqa: BLE001
            return False, str(e)
        if proc.returncode != 0 or not frame_path.is_file():
            return False, (proc.stderr or proc.stdout or "ffmpeg_failed").strip()
        return True, None

    @staticmethod
    def _capture_page_playwright(page_url: str, frame_path: Path) -> tuple[bool, str | None, str]:
        try:
            from playwright.sync_api import sync_playwright
        except Exception:
            return (
                False,
                "playwright_not_installed: pip install playwright && python3 -m playwright install chromium",
                "page",
            )
        wait_s = float(os.getenv("QUOTE_DQD_STREAM_PAGE_WAIT_S", "2.0") or 2.0)
        selector_timeout_s = float(
            os.getenv("QUOTE_DQD_STREAM_SELECTOR_TIMEOUT_S", "15") or 15
        )
        try:
            with sync_playwright() as pw:
                try:
                    browser = pw.chromium.launch(headless=True)
                except Exception as launch_err:  # noqa: BLE001
                    msg = str(launch_err)
                    if "Executable doesn't exist" in msg or "playwright install" in msg:
                        return (
                            False,
                            "playwright_browser_missing: python3 -m playwright install chromium",
                            "page",
                        )
                    return False, f"playwright_launch_failed: {msg.splitlines()[0][:160]}", "page"
                context = browser.new_context(
                    viewport={"width": 1280, "height": 900},
                    user_agent=(
                        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/131.0.0.0 Safari/537.36"
                    ),
                    locale="zh-CN",
                )
                page = context.new_page()
                page.goto(page_url, wait_until="networkidle", timeout=30000)
                found_sel: str | None = None
                frame_kind = "page"
                deadline = time.monotonic() + selector_timeout_s
                while time.monotonic() < deadline:
                    iframe = page.locator("iframe.md-anim-iframe")
                    if iframe.count() > 0:
                        src = (iframe.first.get_attribute("src") or "").strip()
                        try:
                            if src and iframe.first.is_visible():
                                found_sel = "iframe.md-anim-iframe"
                                frame_kind = "animation"
                                break
                        except Exception:
                            pass
                    video = page.locator("video")
                    if video.count() > 0:
                        try:
                            if video.first.is_visible():
                                found_sel = "video"
                                frame_kind = "video"
                                break
                        except Exception:
                            pass
                    time.sleep(0.25)
                if wait_s > 0:
                    time.sleep(wait_s)
                if found_sel:
                    page.locator(found_sel).first.screenshot(path=str(frame_path))
                else:
                    page.screenshot(path=str(frame_path), full_page=False)
                context.close()
                browser.close()
        except Exception as e:  # noqa: BLE001
            msg = str(e)
            if "Executable doesn't exist" in msg or "playwright install" in msg:
                return (
                    False,
                    "playwright_browser_missing: python3 -m playwright install chromium",
                    "page",
                )
            return False, msg.splitlines()[0][:200] if msg else "page_capture_failed", "page"
        return (
            frame_path.is_file(),
            None if frame_path.is_file() else "page_capture_failed",
            frame_kind,
        )


def try_create_observer(root: Path) -> DqdStreamObserver | None:
    if not _env_bool("QUOTE_DQD_STREAM_OBSERVE", False):
        return None
    return DqdStreamObserver(root)
