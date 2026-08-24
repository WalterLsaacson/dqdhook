#!/usr/bin/env python3
"""Smoke: 8-cell reversal score skip/haircut rules."""

from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from reversal_score import ReversalFeatures, fit_lookup, score  # noqa: E402


def _feat(**kwargs: object) -> ReversalFeatures:
    clk = kwargs.get("clock_min")
    clk_i = int(clk) if isinstance(clk, int) else None
    return ReversalFeatures(
        opening=int(kwargs.get("opening") or 0),
        clock_min=clk_i,
        clock_ge_75=int((clk_i or 0) >= 75),
        clock_ge_90=int((clk_i or 0) >= 90),
        prior_same=int(kwargs.get("prior_same") or 0),
        prior_match=int(kwargs.get("prior_match") or 0),
        transition=str(kwargs.get("transition") or "?"),
    )


def main() -> None:
    delfin = score(_feat(opening=1, clock_min=36, prior_same=0))
    assert delfin.action == "full", delfin
    assert delfin.size_mult == 1.0

    saprissa = score(_feat(opening=1, clock_min=78, prior_same=1))
    assert saprissa.action == "skip", saprissa
    assert saprissa.p_rev >= 0.5

    late_open = score(_feat(opening=1, clock_min=90, prior_same=0))
    assert late_open.action == "skip", late_open

    other_prior = score(_feat(opening=0, clock_min=25, prior_same=1))
    assert other_prior.action == "haircut", other_prior

    table = fit_lookup(
        [
            {
                "type": "score_change",
                "ts": "2026-08-24T05:00:00+08:00",
                "match_id": "1",
                "is_goal": True,
                "is_reversal": False,
                "official_clock": "36'",
                "prev": {"home": 0, "away": 0},
                "curr": {"home": 1, "away": 0},
            },
            {
                "type": "score_change",
                "ts": "2026-08-24T05:02:00+08:00",
                "match_id": "1",
                "is_goal": False,
                "is_reversal": True,
                "official_clock": "38'",
                "prev": {"home": 1, "away": 0},
                "curr": {"home": 0, "away": 0},
            },
        ]
    )
    assert table[(1, 0, 0)]["undone"] == 1, table
    print("ok")


if __name__ == "__main__":
    main()
