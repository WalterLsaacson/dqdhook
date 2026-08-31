#!/usr/bin/env python3
"""1H totals must not settle from the live 2H score (Portuguesa 1-2)."""

from __future__ import annotations

from quote_lib import (
    infer_clock_period,
    resolve_regulation_halves,
    totals_tokens,
)


def _ou(question: str, over_id: str, under_id: str) -> dict:
    return {
        "question": question,
        "sports_market_type": "totals",
        "outcomes": ["Over", "Under"],
        "clob_token_ids": [over_id, under_id],
        "market_id": over_id,
        "condition_id": "c",
    }


def main() -> int:
    assert infer_clock_period({"official_clock": "12'"}) == "1H"
    assert infer_clock_period({"status": "Playing 47'"}) == "2H"
    assert infer_clock_period({"period": "2H"}, {"minute": "67"}) == "2H"
    assert infer_clock_period({"period": "FT"}) == "FT"
    assert infer_clock_period({"period": "HT"}) == "1H"

    # Event HT 1-1 beats snapshot hts that copied live 1-2.
    hh, ah = resolve_regulation_halves(
        home_score=1,
        away_score=2,
        candidates=[(1, 1), (1, 2)],
        period="2H",
    )
    assert (hh, ah) == (1, 1), (hh, ah)

    # 2H with no HT: never use current score as 1H.
    miss_h, miss_a = resolve_regulation_halves(
        home_score=1,
        away_score=2,
        candidates=[("", ""), (None, None)],
        period="2H",
    )
    assert miss_h is None and miss_a is None, (miss_h, miss_a)

    # 1H: empty hts → current score is the 1H score.
    live_h, live_a = resolve_regulation_halves(
        home_score=1,
        away_score=0,
        candidates=[("", "")],
        period="1H",
    )
    assert (live_h, live_a) == (1, 0), (live_h, live_a)

    # FT: event 1-2 vs snapshot 1-1 → keep true HT.
    ft_h, ft_a = resolve_regulation_halves(
        home_score=2,
        away_score=2,
        candidates=[(1, 2), (1, 1)],
        period="FT",
    )
    assert (ft_h, ft_a) == (1, 1), (ft_h, ft_a)

    markets = [
        _ou("Portuguesa FC vs. Metropolitanos FC: 1st Half O/U 2.5", "o25", "u25"),
        _ou(
            "Portuguesa FC vs. Metropolitanos FC: Metropolitanos FC 1st Half O/U 1.5",
            "ao15",
            "au15",
        ),
        _ou("Portuguesa FC vs. Metropolitanos FC: 1st Half O/U 1.5", "o15", "u15"),
    ]
    live = totals_tokens(
        markets,
        home="Portuguesa FC",
        away="Metropolitanos FC",
        home_score=1,
        away_score=2,
        home_half=1,
        away_half=1,
        mode="live",
    )
    keys = {r["market_key"] for r in live}
    assert "match_1h_total_2.5_over" not in keys, live
    assert "away_1h_total_1.5_over" not in keys, live
    assert "match_1h_total_1.5_over" in keys, live

    print("ok: 1H totals ignore 2H score")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
