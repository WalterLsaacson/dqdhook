"""0.999 exit sells list each 0.99 fill once, then only the later increment."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from exit_sell import (
    ensure_exit_eligible,
    exit_unlisted_shares,
    is_dongqiudi_match_id,
    note_exit_fill,
    untracked_fill,
)
from score_reversal import OpenPositionLedger


def _lot(**kwargs: object) -> dict:
    row = {
        "shares": 0.0,
        "usdc": 0.0,
        "exit_listed_shares": 0.0,
    }
    row.update(kwargs)
    return row


def main() -> int:
    dust = _lot(shares=2.17, usdc=2.1483)
    ensure_exit_eligible(dust)
    assert abs(float(dust["exit_eligible_shares"]) - 2.17) < 1e-9
    assert exit_unlisted_shares(dust) == 0.0

    held = _lot(shares=10.0, usdc=9.9)
    ensure_exit_eligible(held)
    assert exit_unlisted_shares(held) == 10.0
    held["exit_listed_shares"] = 10.0
    assert exit_unlisted_shares(held) == 0.0
    note_exit_fill(held, 2.0, 0.99)
    assert exit_unlisted_shares(held) == 0.0
    note_exit_fill(held, 4.0, 0.99)
    assert abs(exit_unlisted_shares(held) - 6.0) < 1e-9

    cheap = _lot(shares=20.0, usdc=18.4)
    ensure_exit_eligible(cheap)
    assert float(cheap["exit_eligible_shares"]) == 0.0
    note_exit_fill(cheap, 6.0, 0.99)
    assert abs(exit_unlisted_shares(cheap) - 6.0) < 1e-9

    assert is_dongqiudi_match_id("54540053")
    assert not is_dongqiudi_match_id("0x7d5e9269")
    known = {"oid-a"}
    assert untracked_fill({"id": "oid-a", "size_matched": 8, "price": 0.99}, known) == (0.0, 0.0)
    filled, cost = untracked_fill(
        {"id": "oid-b", "size_matched": 3, "price": 0.99}, known
    )
    assert abs(filled - 3.0) < 1e-9 and abs(cost - 2.97) < 1e-9

    with tempfile.TemporaryDirectory() as td:
        led = OpenPositionLedger(Path(td) / "open_positions.json")
        led.register_buy(
            match_id="m1",
            token_id="tok",
            market_key="match_total_0.5_over",
            shares=10.0,
            usdc=9.9,
            home_score=0,
            away_score=1,
            live=True,
            event_key="k",
            entry_price=0.99,
        )
        lot = led.all_open()[0]
        assert abs(float(lot["exit_eligible_shares"]) - 10.0) < 1e-9
        lot["exit_listed_shares"] = 10.0
        lot["exit_orders"] = [{"order_id": "a", "shares": 10.0, "price": 0.999}]
        led._save()
        led.register_buy(
            match_id="m1",
            token_id="tok",
            market_key="match_total_0.5_over",
            shares=6.0,
            usdc=5.94,
            home_score=0,
            away_score=1,
            live=True,
            event_key="k",
            entry_price=0.99,
        )
        lot = led.all_open()[0]
        assert abs(float(lot["shares"]) - 16.0) < 1e-9
        assert abs(float(lot["exit_eligible_shares"]) - 16.0) < 1e-9
        assert abs(float(lot["exit_listed_shares"]) - 10.0) < 1e-9
        assert len(lot["exit_orders"]) == 1
        assert abs(exit_unlisted_shares(lot) - 6.0) < 1e-9

    print("ok: exit sell lists 0.99 inventory once, then only the new shares")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
