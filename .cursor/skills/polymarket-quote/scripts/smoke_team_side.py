#!/usr/bin/env python3
"""Regression: short home codes must not match inside away names (ofi⊂sofia)."""

from __future__ import annotations

from quote_lib import (
    _parse_total_market,
    _role_for_team_blob,
    classify_moneyline_role,
    moneyline_tokens,
    totals_tokens,
)


def main() -> int:
    home, away = "OFI", "PFK CSKA Sofia"

    assert _role_for_team_blob("OFI", home, away) == "home"
    assert _role_for_team_blob("PFK CSKA Sofia", home, away) == "away"
    assert _role_for_team_blob("CSKA Sofia", home, away) == "away"

    assert (
        classify_moneyline_role({"question": "Will OFI win on 2026-08-20?"}, home, away)
        == "home"
    )
    assert (
        classify_moneyline_role(
            {"question": "Will PFK CSKA Sofia win on 2026-08-20?"}, home, away
        )
        == "away"
    )
    assert (
        classify_moneyline_role(
            {"question": "Will OFI vs. PFK CSKA Sofia end in a draw?"}, home, away
        )
        == "draw"
    )
    assert (
        classify_moneyline_role({"group_item_title": "PFK CSKA Sofia", "question": ""}, home, away)
        == "away"
    )

    assert _parse_total_market(
        "OFI vs. PFK CSKA Sofia: PFK CSKA Sofia O/U 1.5", home, away
    ) == {"line": 1.5, "period": "ft", "side": "away"}
    assert _parse_total_market(
        "OFI vs. PFK CSKA Sofia: OFI O/U 1.5", home, away
    ) == {"line": 1.5, "period": "ft", "side": "home"}

    markets = [
        {
            "question": "Will OFI win on 2026-08-20?",
            "group_item_title": "OFI",
            "sports_market_type": "moneyline",
            "outcomes": ["Yes", "No"],
            "clob_token_ids": ["ofi_yes", "ofi_no"],
            "market_id": "1",
            "condition_id": "c1",
        },
        {
            "question": "Will PFK CSKA Sofia win on 2026-08-20?",
            "group_item_title": "PFK CSKA Sofia",
            "sports_market_type": "moneyline",
            "outcomes": ["Yes", "No"],
            "clob_token_ids": ["cska_yes", "cska_no"],
            "market_id": "2",
            "condition_id": "c2",
        },
    ]
    ml = moneyline_tokens(markets, home=home, away=away, home_score=3, away_score=0)
    by_key = {r["market_key"]: r for r in ml}
    assert by_key["home_yes"]["token_id"] == "ofi_yes"
    assert by_key["home_yes"]["settlement"] == "WIN"
    assert by_key["away_yes"]["token_id"] == "cska_yes"
    assert by_key["away_yes"]["settlement"] == "LOSE"

    tot_markets = [
        {
            "question": "OFI vs. PFK CSKA Sofia: PFK CSKA Sofia O/U 1.5",
            "sports_market_type": "totals",
            "outcomes": ["Over", "Under"],
            "clob_token_ids": ["cska_o15", "cska_u15"],
            "market_id": "3",
            "condition_id": "c3",
        }
    ]
    tots = totals_tokens(
        tot_markets, home=home, away=away, home_score=3, away_score=0
    )
    assert tots, tots
    assert tots[0]["market_key"] == "away_total_1.5_over"
    assert tots[0]["settlement"] == "LOSE"  # CSKA scored 0

    print("ok: ofi/cska team-side mapping")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
