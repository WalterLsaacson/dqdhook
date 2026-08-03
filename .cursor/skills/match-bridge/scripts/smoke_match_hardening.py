#!/usr/bin/env python3
"""Smoke checks for match-bridge hardening (wrong-pair rejects + true pairs)."""

from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import bridge_lib as bl  # noqa: E402


def _assert(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


def main() -> int:
    # Digits kept as standalone tokens
    _assert("2028" in bl.normalize_team("Shenzhen 2028"), "keep digit token 2028")
    _assert(bl.normalize_team("shenzhen2028") == "shenzhen2028", "keep mid-token digits")
    _assert(bl.team_similarity("Schalke 04", "Schalke 04") == 1.0, "Schalke 04 self")
    _assert(bl.team_similarity("U23 China", "China U23") >= 0.99, "U23 order-insensitive")

    # Case 1: Jiangxi/Shenzhen2028 must NOT pair with Shaanxi/Juniors
    bad_jiangxi = {
        "home": "Jiangxi Lushan",
        "away": "Shenzhen 2028",
        "league": "中乙",
        "local_date": "2026-07-25",
        "time": "19:30",
    }
    pm_shaanxi = {
        "home": "Shaanxi Union",
        "away": "Shenzhen Juniors",
        "league": "CHI2",
        "league_id": "chi2",
        "kickoff_beijing": "2026-07-25 19:30",
        "local_date": "2026-07-25",
        "time": "19:30",
    }
    s_bad = bl.score_pair(bad_jiangxi, pm_shaanxi)
    _assert(s_bad == 0.0, f"Jiangxi/2028 must not match Shaanxi/Juniors, got {s_bad}")

    # Correct China League One pair still matches (same kickoff)
    good_shaanxi = {
        "home": "Shaanxi Union",
        "away": "Shenzhen Juniors",
        "league": "中甲",
        "local_date": "2026-07-25",
        "time": "19:30",
    }
    s_good = bl.score_pair(good_shaanxi, pm_shaanxi)
    _assert(s_good >= bl.DEFAULT_MIN_SCORE, f"Shaanxi true pair should match, got {s_good}")

    # Delayed kickoff within 90 min still ok
    delayed = dict(good_shaanxi)
    delayed["time"] = "20:30"
    delayed["kickoff_beijing"] = "2026-07-25 20:30"
    s_delay = bl.score_pair(delayed, pm_shaanxi)
    _assert(s_delay >= bl.DEFAULT_MIN_SCORE, f"60min delay should still match, got {s_delay}")

    # Case 2: Defensor–Liverpool must NOT pair with Defensor–Cerro
    dqd_def = {
        "home": "Defensor Sporting",
        "away": "Liverpool FC Montevideo",
        "league": "乌拉甲",
        "local_date": "2026-07-26",
        "time": "02:00",
        "kickoff_beijing": "2026-07-26 02:00",
    }
    pm_cerro = {
        "home": "Defensor Sporting",
        "away": "CA Cerro",
        "league": "URU1",
        "league_id": "uru1",
        "local_date": "2026-07-26",
        "time": "02:00",
        "kickoff_beijing": "2026-07-26 02:00",
    }
    s_cerro = bl.score_pair(dqd_def, pm_cerro)
    _assert(s_cerro == 0.0, f"Defensor–Liverpool must not match Cerro, got {s_cerro}")

    pm_liv = {
        "home": "Defensor Sporting",
        "away": "Liverpool Montevideo",
        "league": "URU1",
        "league_id": "uru1",
        "local_date": "2026-07-26",
        "time": "02:00",
        "kickoff_beijing": "2026-07-26 02:00",
    }
    s_liv = bl.score_pair(dqd_def, pm_liv)
    _assert(s_liv >= bl.DEFAULT_MIN_SCORE, f"Defensor–Liverpool true pair should match, got {s_liv}")

    # League gate: 中乙 vs chi2 (中甲) rejects even with identical team names
    mid_tier = {
        "home": "Same FC",
        "away": "Other FC",
        "league": "中乙",
        "local_date": "2026-07-25",
        "time": "19:30",
    }
    pm_l1 = {
        "home": "Same FC",
        "away": "Other FC",
        "league_id": "chi2",
        "league": "CHI2",
        "local_date": "2026-07-25",
        "time": "19:30",
        "kickoff_beijing": "2026-07-25 19:30",
    }
    s_league = bl.score_pair(mid_tier, pm_l1)
    _assert(s_league == 0.0, f"中乙 vs chi2 must reject, got {s_league}")

    # Stale PM filter
    now = datetime.now(bl.TZ_CN)
    stale = {
        "home": "A",
        "away": "B",
        "kickoff_beijing": (now - timedelta(hours=8)).strftime("%Y-%m-%d %H:%M"),
    }
    fresh = {
        "home": "C",
        "away": "D",
        "kickoff_beijing": (now - timedelta(hours=2)).strftime("%Y-%m-%d %H:%M"),
    }
    kept = bl.filter_fresh_pm_matches([stale, fresh], stale_hours=6, now=now)
    _assert(len(kept) == 1 and kept[0]["home"] == "C", f"stale filter failed: {kept}")

    # League aliases: LEC / Faroe / UWCL
    _assert(bl.normalize_league("北美联杯") == "lec", "北美联杯→lec")
    _assert(bl.normalize_league("LEC", "lec") == "lec", "LEC→lec")
    _assert(bl.normalize_league("法罗超") == "fro1", "法罗超→fro1")
    _assert(bl.normalize_league("女足欧冠") == "uwcl", "女足欧冠→uwcl")

    # Team aliases that were blocking PM↔DQD
    _assert(bl.normalize_team("OB") == bl.normalize_team("Odense BK"), "OB↔Odense")
    _assert(bl.normalize_team("Seinajoen JK") == bl.normalize_team("SJK Seinäjoki"), "SJK")
    _assert(
        bl.normalize_team("LDU Quito")
        == bl.normalize_team("Liga Dep Universitaria Quito"),
        "LDU",
    )
    _assert(
        bl.normalize_team("Qairat FK") == bl.normalize_team("FC Kairat Almaty"),
        "Qairat↔Kairat",
    )
    _assert(bl.team_similarity("HB Torshavn", "HB") >= 0.92, "HB Torshavn")
    _assert(bl.team_similarity("NSI Runavik", "NSÍ") >= 0.92, "NSI")

    lec_dqd = {
        "home": "Cincinnati",
        "away": "Pachuca",
        "league": "北美联杯",
        "local_date": "2026-08-05",
        "time": "07:45",
    }
    lec_pm = {
        "home": "FC Cincinnati",
        "away": "CF Pachuca",
        "league": "LEC",
        "league_id": "lec",
        "kickoff_beijing": "2026-08-05 07:30",
        "local_date": "2026-08-05",
        "time": "07:30",
    }
    s_lec = bl.score_pair(lec_dqd, lec_pm)
    _assert(s_lec >= bl.DEFAULT_MIN_SCORE, f"LEC Cincinnati should match, got {s_lec}")

    den_dqd = {
        "home": "OB",
        "away": "Sönderjyske",
        "league": "丹超",
        "local_date": "2026-08-04",
        "time": "01:00",
    }
    den_pm = {
        "home": "Odense BK",
        "away": "Sønderjyske Fodbold",
        "league": "DEN",
        "league_id": "den",
        "kickoff_beijing": "2026-08-04 01:00",
        "local_date": "2026-08-04",
        "time": "01:00",
    }
    s_den = bl.score_pair(den_dqd, den_pm)
    _assert(s_den >= bl.DEFAULT_MIN_SCORE, f"OB/Odense should match, got {s_den}")

    print("smoke_match_hardening: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
