#!/usr/bin/env python3
"""1H totals must not settle from the live 2H score (Portuguesa 1-2)."""

from __future__ import annotations

from quote_lib import (
    infer_clock_period,
    join_ft_context,
    promote_clock_period,
    resolve_regulation_halves,
    stated_match_period,
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

    assert infer_clock_period({"official_clock": "45'", "period": "2H"}) == "2H"

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

    # Lincoln: frozen 1-0 beats DQD hts that copied 2H (1-1 / live 1-2).
    lin_h, lin_a = resolve_regulation_halves(
        home_score=1,
        away_score=2,
        candidates=[(1, 0), (1, 1), (1, 2)],
        period="2H",
    )
    assert (lin_h, lin_a) == (1, 0), (lin_h, lin_a)

    # 2H with only the live score as "HT": drop it, do not settle 1H from 2H.
    live_only_h, live_only_a = resolve_regulation_halves(
        home_score=1,
        away_score=2,
        candidates=[(1, 2)],
        period="2H",
    )
    assert live_only_h is None and live_only_a is None, (live_only_h, live_only_a)

    # Frozen HT still equal to live (no 2H goals yet) is real HT, not a copy.
    same_h, same_a = resolve_regulation_halves(
        home_score=1,
        away_score=0,
        candidates=[(1, 0), (1, 0)],
        period="2H",
        frozen=(1, 0),
    )
    assert (same_h, same_a) == (1, 0), (same_h, same_a)
    ft_same_h, ft_same_a = resolve_regulation_halves(
        home_score=1,
        away_score=0,
        candidates=[(1, 0)],
        period="FT",
        frozen=(1, 0),
    )
    assert (ft_same_h, ft_same_a) == (1, 0), (ft_same_h, ft_same_a)

    assert stated_match_period({"period": "1H"}, {"period": "2H"}) == "1H"
    assert stated_match_period({"official_clock": "40'"}) == ""
    assert (
        promote_clock_period(
            "1H", frozen=(0, 0), home_score=1, away_score=0, stated_period="1H"
        )
        == "1H"
    )
    assert (
        promote_clock_period(
            "1H", frozen=(0, 0), home_score=1, away_score=0, stated_period="HT"
        )
        == "1H"
    )
    assert (
        promote_clock_period(
            "1H", frozen=(0, 0), home_score=1, away_score=0, stated_period=""
        )
        == "2H"
    )
    assert (
        promote_clock_period(
            "1H", frozen=(1, 0), home_score=1, away_score=0, stated_period=""
        )
        == "1H"
    )

    import json
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        bdir = root / "data" / "bridge"
        bdir.mkdir(parents=True)
        (bdir / "half_scores.json").write_text(
            json.dumps({"m1": {"home": 0, "away": 0}}), encoding="utf-8"
        )
        explicit = join_ft_context(
            root,
            {
                "match_id": "m1",
                "home_score": 1,
                "away_score": 0,
                "period": "1H",
                "home": "H",
                "away": "A",
            },
        )
        assert explicit["clock_period"] == "1H", explicit
        assert (explicit["home_half"], explicit["away_half"]) == (1, 0), explicit
        clock_only = join_ft_context(
            root,
            {
                "match_id": "m1",
                "home_score": 1,
                "away_score": 0,
                "official_clock": "40'",
                "home": "H",
                "away": "A",
            },
        )
        assert clock_only["clock_period"] == "2H", clock_only
        assert (clock_only["home_half"], clock_only["away_half"]) == (0, 0), clock_only
        (bdir / "half_scores.json").write_text(
            json.dumps({"m1": {"home": 1, "away": 0}}), encoding="utf-8"
        )
        early_2h = join_ft_context(
            root,
            {
                "match_id": "m1",
                "home_score": 1,
                "away_score": 0,
                "period": "2H",
                "home_half": 1,
                "away_half": 0,
                "home": "H",
                "away": "A",
            },
        )
        assert early_2h["clock_period"] == "2H", early_2h
        assert (early_2h["home_half"], early_2h["away_half"]) == (1, 0), early_2h

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

    lincoln = totals_tokens(
        [
            _ou("Lincoln City FC vs. Swansea City AFC: 1st Half O/U 0.5", "o05", "u05"),
            _ou("Lincoln City FC vs. Swansea City AFC: 1st Half O/U 1.5", "o15b", "u15b"),
            _ou("Lincoln City FC vs. Swansea City AFC: 1st Half O/U 2.5", "o25b", "u25b"),
            _ou(
                "Lincoln City FC vs. Swansea City AFC: Swansea City AFC 1st Half O/U 0.5",
                "ao05",
                "au05",
            ),
        ],
        home="Lincoln City FC",
        away="Swansea City AFC",
        home_score=1,
        away_score=2,
        home_half=1,
        away_half=0,
        mode="live",
    )
    lin_keys = {r["market_key"] for r in lincoln}
    assert "match_1h_total_0.5_over" in lin_keys, lincoln
    assert "match_1h_total_1.5_over" not in lin_keys, lincoln
    assert "match_1h_total_2.5_over" not in lin_keys, lincoln
    assert "away_1h_total_0.5_over" not in lin_keys, lincoln

    print("ok: 1H totals ignore 2H score")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
