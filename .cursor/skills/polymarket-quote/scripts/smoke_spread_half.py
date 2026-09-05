#!/usr/bin/env python3
"""1H/2H spreads must settle from half scores, not full-time (Genoa dust bug)."""

from __future__ import annotations

from quote_lib import _spread_period, spread_tokens, token_is_win_at_score


def _spread(question: str, fav: str, dog: str, tok_fav: str, tok_dog: str) -> dict:
    return {
        "question": question,
        "sports_market_type": "spreads",
        "outcomes": [fav, dog],
        "clob_token_ids": [tok_fav, tok_dog],
        "market_id": tok_fav,
        "condition_id": "c",
    }


def main() -> int:
    assert _spread_period("Spread: Como 1907 (-1.5)") == "ft"
    assert _spread_period("1st Half Spread: Como 1907 (-1.5)") == "1h"
    assert _spread_period("2nd Half Spread: Liverpool FC (-1.5)") == "2h"

    home, away = "Genoa CFC", "Como 1907"
    # FT 1-4, HT 1-0 → 2H was 0-4. Como FT -1.5 covers; 1H Como -1.5 does not.
    markets = [
        _spread("Spread: Como 1907 (-1.5)", "Como 1907", "Genoa CFC", "ft_c", "ft_g"),
        _spread(
            "1st Half Spread: Como 1907 (-1.5)",
            "Como 1907",
            "Genoa CFC",
            "1h_c",
            "1h_g",
        ),
        _spread(
            "2nd Half Spread: Como 1907 (-1.5)",
            "Como 1907",
            "Genoa CFC",
            "2h_c",
            "2h_g",
        ),
    ]

    # Without HT: half markets skipped; FT still settles.
    bare = spread_tokens(
        markets, home=home, away=away, home_score=1, away_score=4
    )
    keys = {r["market_key"] for r in bare}
    assert "spread_como1907_-1.5_0" in keys, keys
    assert not any("1h" in k or "2h" in k for k in keys), keys

    rows = spread_tokens(
        markets,
        home=home,
        away=away,
        home_score=1,
        away_score=4,
        home_half=1,
        away_half=0,
    )
    by_tok = {r["token_id"]: r for r in rows}
    assert by_tok["ft_c"]["settlement"] == "WIN", by_tok["ft_c"]
    assert by_tok["ft_g"]["settlement"] == "LOSE", by_tok["ft_g"]
    # 1H 1-0: Como (away) margin = -1, -1 + (-1.5) < 0 → does not cover
    assert by_tok["1h_c"]["settlement"] == "LOSE", by_tok["1h_c"]
    assert by_tok["1h_g"]["settlement"] == "WIN", by_tok["1h_g"]
    # 2H 0-4: Como margin = 4, covers -1.5
    assert by_tok["2h_c"]["settlement"] == "WIN", by_tok["2h_c"]
    assert by_tok["2h_g"]["settlement"] == "LOSE", by_tok["2h_g"]

    # token_is_win_at_score follows period
    assert token_is_win_at_score(
        by_tok["1h_g"],
        home_score=1,
        away_score=4,
        home_half=1,
        away_half=0,
    )
    assert not token_is_win_at_score(
        by_tok["1h_c"],
        home_score=1,
        away_score=4,
        home_half=1,
        away_half=0,
    )

    # PSG 1-2, HT 0-0 → 2H 1-2. Monaco -1.5 does not cover on 2H (margin 1).
    psg = spread_tokens(
        [
            _spread(
                "2nd Half Spread: AS Monaco FC (-1.5)",
                "AS Monaco FC",
                "Paris Saint-Germain FC",
                "mon",
                "psg",
            )
        ],
        home="Paris Saint-Germain FC",
        away="AS Monaco FC",
        home_score=1,
        away_score=2,
        home_half=0,
        away_half=0,
    )
    by = {r["token_id"]: r for r in psg}
    assert by["mon"]["settlement"] == "LOSE", by["mon"]
    assert by["psg"]["settlement"] == "WIN", by["psg"]

    print("ok: half spreads settle from half scores")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
