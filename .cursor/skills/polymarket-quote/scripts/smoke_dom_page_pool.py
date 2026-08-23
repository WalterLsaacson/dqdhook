#!/usr/bin/env python3
"""Smoke: shared Chromium pool reuses tabs across goals and warms playing matches."""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import dqd_stream_observe as dso  # noqa: E402
from dqd_stream_observe import DomReader  # noqa: E402
from dom_page_pool import (  # noqa: E402
    DomPagePool,
    MemoryDomBackend,
    _find_animation_frame,
)


class _EmptyLoc:
    def count(self) -> int:
        return 0

    @property
    def first(self) -> "_EmptyLoc":
        return self

    def element_handle(self) -> None:
        return None


class _FakePage:
    def locator(self, _sel: str) -> _EmptyLoc:
        return _EmptyLoc()


def _url(mid: str) -> str:
    return f"https://tracker.example/{mid}"


def main() -> int:
    be = MemoryDomBackend()
    pool = DomPagePool(backend=be, max_pages=2)
    pool.start()
    assert be.started == 1, be.started

    ok, err, reused, token = pool.ensure_open("m1", _url("m1"), lease=False)
    assert ok and err is None and reused is False and token == 0, (ok, err, reused, token)
    ok, err, reused, token = pool.ensure_open("m1", _url("m1"), lease=True)
    assert ok and reused is True and token > 0, (ok, reused, token)
    assert be.open_calls == ["m1"], be.open_calls
    assert pool.lease_count("m1") == 1
    pool.release_lease("m1", token)
    assert pool.lease_count("m1") == 0
    assert "m1" in pool.opened_ids()

    ok, err, reused, token = pool.ensure_open("m1", _url("m1"), lease=True)
    assert reused is True
    pool.release_lease("m1", token)
    assert be.open_calls == ["m1"], be.open_calls

    ok, _, reused, _ = pool.ensure_open("m2", _url("m2"), lease=False)
    assert ok and reused is False
    # Cap 2: opening m3 evicts idle m1 (no lease).
    ok, _, _, _ = pool.ensure_open("m3", _url("m3"), lease=False)
    assert ok
    opened = pool.opened_ids()
    assert "m3" in opened and "m2" in opened
    assert "m1" not in opened, opened
    assert "m1" in be.close_calls

    # Token close is idempotent and does not steal another session's lease.
    r1 = DomReader(_url("m2"), match_id="m2", pool=pool)
    r2 = DomReader(_url("m2"), match_id="m2", pool=pool)
    assert r1.open()[0] and r2.open()[0]
    assert pool.lease_count("m2") == 2
    r1.close()
    r1.close()
    assert pool.lease_count("m2") == 1
    assert "m2" in pool.opened_ids()
    r2.close()
    assert pool.lease_count("m2") == 0

    # FT close_page while leased is deferred until the last token drops.
    r3 = DomReader(_url("m3"), match_id="m3", pool=pool)
    assert r3.open()[0]
    assert pool.close_page("m3") is False
    assert "m3" in pool.opened_ids()
    r3.close()
    assert "m3" not in pool.opened_ids()
    assert "m3" in be.close_calls

    # Concurrent opens at cap stay at cap (evict+open is one backend critical section).
    be4 = MemoryDomBackend()
    be4.open_delay_s = 0.05
    pool4 = DomPagePool(backend=be4, max_pages=2)
    pool4.start()
    pool4.ensure_open("a", _url("a"), lease=False)
    pool4.ensure_open("b", _url("b"), lease=False)
    barrier = threading.Barrier(2)

    def _open_extra(mid: str) -> None:
        barrier.wait()
        pool4.ensure_open(mid, _url(mid), lease=False)

    threads = [
        threading.Thread(target=_open_extra, args=("c",)),
        threading.Thread(target=_open_extra, args=("d",)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(pool4.opened_ids()) <= 2, pool4.opened_ids()

    # Delayed open pumps reads of an already-open tab.
    be5 = MemoryDomBackend()
    pool5 = DomPagePool(backend=be5, max_pages=8)
    pool5.start()
    pool5.ensure_open("keep", _url("keep"), lease=False)
    reads: list[bool] = []

    def _pump() -> None:
        dom, err = pool5.read("keep")
        reads.append(dom is not None and err is None)

    be5.pump_during_open = _pump
    be5.open_delay_s = 0.2
    t0 = time.monotonic()
    ok, _, _, _ = pool5.ensure_open("slow", _url("slow"), lease=False)
    assert ok
    assert reads, "open wait should drain reads"
    assert time.monotonic() - t0 >= 0.15

    pumped = {"n": 0}

    def _count_pump() -> None:
        pumped["n"] += 1

    _find_animation_frame(
        _FakePage(),
        deadline=time.monotonic() + 0.2,
        pump=_count_pump,
    )
    assert pumped["n"] >= 1, pumped

    tmp = Path(tempfile.mkdtemp(prefix="dom-pool-"))
    (tmp / "data" / "bridge").mkdir(parents=True)
    (tmp / "data" / "bridge" / "matches.json").write_text(
        json.dumps(
            {
                "matches": [
                    {
                        "dongqiudi": {
                            "id": "live1",
                            "status": "playing",
                            "status_raw": "playing",
                        },
                        "polymarket": {"event_id": "e1", "slug": "s1"},
                    },
                    {
                        "dongqiudi": {
                            "id": "done1",
                            "status": "played",
                            "status_raw": "played",
                            "is_finished": True,
                        },
                        "polymarket": {"event_id": "e2"},
                    },
                    {
                        "dongqiudi": {
                            "id": "unpaired",
                            "status": "playing",
                            "status_raw": "playing",
                        },
                        "polymarket": {},
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    be2 = MemoryDomBackend()

    def _discover(match_id: str, root=None, timeout=None, **_kwargs):
        return {
            "match_id": match_id,
            "page_url": f"https://tracker.example/{match_id}",
            "surface": "animation",
        }

    obs = dso.DqdStreamObserver(
        tmp,
        discover_fn=_discover,
        page_pool=DomPagePool(backend=be2, max_pages=8),
    )
    obs.page_pool.start()
    stats = obs.sync_playing_pages()
    assert stats["warmed"] == 1, stats
    assert "live1" in be2.open_calls
    assert "done1" not in be2.open_calls
    assert "unpaired" not in be2.open_calls

    reader, err, _info = obs.acquire_dom_reader(
        "live1", "https://tracker.example/live1", {"page_url": "https://tracker.example/live1"}
    )
    assert err is None and reader is not None
    assert reader.reused is True, "in-play warm should already hold the tab"
    reader.close()
    assert "live1" in obs.page_pool.opened_ids()

    obs.release_match("live1", reason="match_finished")
    assert "live1" not in obs.page_pool.opened_ids()

    print("ok: shared chromium pool reuses tabs and warms playing matches")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
