#!/usr/bin/env python3
"""Smoke: gate_prewarm take_books hit / stale / incomplete."""

from __future__ import annotations

import sys
import time
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import gate_prewarm as gp  # noqa: E402


def main() -> None:
    gp.reset_prewarm_for_tests()
    pw = gp.get_prewarm()
    pw.configure(Path("/tmp/gate_prewarm_smoke"))
    key = "score_change|m1|0-0->1-0|t"
    mid = "m1"
    books = {
        "t1": {"token_id": "t1", "best_ask": 0.99, "best_bid": 0.98},
        "t2": {"token_id": "t2", "best_ask": None, "best_bid": 0.99},
    }
    pw._ready[key] = gp._ReadyBooks(
        match_id=mid,
        event_key=key,
        token_ids=["t1", "t2"],
        books=books,
        books_mono=time.monotonic(),
        catalog_ok=True,
        refreshes=2,
    )
    hit, meta = pw.take_books(["t1", "t2"], event_key=key, max_age_s=4.0)
    assert hit is not None and meta.get("hit") is True, meta
    assert hit["t1"]["best_ask"] == 0.99
    # one-shot consume
    miss, meta2 = pw.take_books(["t1", "t2"], event_key=key, max_age_s=4.0)
    assert miss is None and meta2.get("reason") == "no_books", meta2

    pw._ready[key] = gp._ReadyBooks(
        match_id=mid,
        event_key=key,
        token_ids=["t1"],
        books={"t1": books["t1"]},
        books_mono=time.monotonic() - 5.0,  # > max_age 4, < purge 8
        catalog_ok=True,
        refreshes=1,
    )
    stale, meta3 = pw.take_books(["t1"], event_key=key, max_age_s=4.0)
    assert stale is None and meta3.get("reason") == "stale", meta3

    pw._ready[key] = gp._ReadyBooks(
        match_id=mid,
        event_key=key,
        token_ids=["t1"],
        books={"t1": books["t1"]},
        books_mono=time.monotonic(),
        catalog_ok=True,
        refreshes=1,
    )
    incomplete, meta4 = pw.take_books(["t1", "t2"], event_key=key, max_age_s=4.0)
    assert incomplete is None and meta4.get("reason") == "incomplete", meta4

    print("smoke_gate_prewarm: ok")


if __name__ == "__main__":
    main()
