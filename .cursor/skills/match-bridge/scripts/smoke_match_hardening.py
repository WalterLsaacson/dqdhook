#!/usr/bin/env python3
"""Smoke checks for match-bridge hardening (wrong-pair rejects + true pairs)."""

from __future__ import annotations

import json
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

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

    # League aliases: LEC / Faroe / UWCL / ASEAN
    _assert(bl.normalize_league("北美联杯") == "lec", "北美联杯→lec")
    _assert(bl.normalize_league("LEC", "lec") == "lec", "LEC→lec")
    _assert(bl.normalize_league("法罗超") == "fro1", "法罗超→fro1")
    _assert(bl.normalize_league("女足欧冠") == "uwcl", "女足欧冠→uwcl")
    _assert(bl.normalize_league("东南锦") == "asean", "东南锦→asean")
    _assert(bl.normalize_league("ASEAN") == "asean", "ASEAN→asean")
    # 2026-08-09: ERE/POR/BEL1/TUR2/GEO1/ITC/NED2/GRC
    _assert(bl.normalize_league("荷甲") == "ere", "荷甲→ere")
    _assert(bl.normalize_league("ERE", "ere") == "ere", "ERE→ere")
    _assert(bl.normalize_league("葡超") == "por", "葡超→por")
    _assert(bl.normalize_league("POR", "por") == "por", "POR→por")
    _assert(bl.normalize_league("比甲") == "bel1", "比甲→bel1")
    _assert(bl.normalize_league("BEL1", "bel1") == "bel1", "BEL1→bel1")
    _assert(bl.normalize_league("土甲") == "tur2", "土甲→tur2")
    _assert(bl.normalize_league("TUR2", "tur2") == "tur2", "TUR2→tur2")
    _assert(bl.normalize_league("格鲁甲") == "geo1", "格鲁甲→geo1")
    _assert(bl.normalize_league("荷乙") == "ned2", "荷乙→ned2")
    _assert(bl.normalize_league("意大利杯") == "itc", "意大利杯→itc")
    _assert(bl.normalize_league("希腊杯") == "grc", "希腊杯→grc")
    # 2026-08-15: Community Shield / French Supercup / Saudi King Cup
    _assert(bl.normalize_league("社区盾杯") == "ecs", "社区盾杯→ecs")
    _assert(bl.normalize_league("ECS", "ecs") == "ecs", "ECS→ecs")
    _assert(bl.normalize_league("法超杯") == "frtc", "法超杯→frtc")
    _assert(bl.normalize_league("FRTC", "frtc") == "frtc", "FRTC→frtc")
    _assert(bl.normalize_league("沙特国王杯") == "skc", "沙特国王杯→skc")
    _assert(bl.normalize_league("SKC", "skc") == "skc", "SKC→skc")

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
    _assert(
        bl.normalize_team("Inter Milano")
        == bl.normalize_team("Internazionale"),
        "Inter Milano",
    )
    _assert(
        bl.normalize_team("Heart of Midlothian LFC")
        == bl.normalize_team("Hearts (w)"),
        "Hearts W",
    )
    _assert(
        bl.normalize_team("ZHFK Sisters")
        == bl.normalize_team("SeaSters Odessa Women"),
        "SeaSters",
    )
    _assert(
        bl.normalize_team("GV CD San José")
        == bl.normalize_team("Gualberto Villarroel Deportivo San José"),
        "GV San José",
    )
    _assert(
        bl.normalize_team("CD La Equidad Seguros")
        == bl.normalize_team("Internacional de Bogota"),
        "La Equidad",
    )
    # 2026-08-05 coverage gaps (UEL/COL + UWCL AF "… W" shorts)
    _assert(bl.normalize_team("Kuopion PS") == bl.normalize_team("KuPS"), "Kuopion PS↔KuPS")
    _assert(
        bl.normalize_team("KF Víkingur")
        == bl.normalize_team("Víkingur Reykjavík"),
        "Víkingur",
    )
    _assert(
        bl.normalize_team("FK Rīgas Futbola Skola")
        == bl.normalize_team("Rīgas FS"),
        "Rīgas Futbola Skola↔RFS",
    )
    _assert(
        bl.normalize_team("KF Shkëndija 79")
        == bl.normalize_team("Shkendija Tetovo"),
        "Shkendija",
    )
    _assert(
        bl.normalize_team("Gualberto Villarroel SJ")
        == bl.normalize_team("GV CD San José"),
        "Villarroel SJ",
    )
    _assert(
        bl.normalize_team("Ajax Amsterdam Women") == bl.normalize_team("Ajax W"),
        "Ajax W",
    )
    _assert(
        bl.normalize_team("TSC W") == bl.normalize_team("FK TSC Bačka Topola"),
        "TSC W",
    )
    # 2026-08-09 side-name gaps
    _assert(
        bl.normalize_team("Royal Charleroi SC")
        == bl.normalize_team("RC Sporting Charleroi"),
        "Charleroi",
    )
    _assert(
        bl.normalize_team("Jong AZ Alkmaar") == bl.normalize_team("AZ II"),
        "Jong AZ↔AZ II",
    )
    _assert(
        bl.normalize_team("Jong PSV Eindhoven") == bl.normalize_team("PSV II"),
        "Jong PSV↔PSV II",
    )
    _assert(
        bl.normalize_team("Jong FC Utrecht") == bl.normalize_team("Utrecht II"),
        "Jong Utrecht↔Utrecht II",
    )
    _assert(
        bl.normalize_team("AC Goianiense") == bl.normalize_team("Atlético GO"),
        "Goianiense↔Atlético GO",
    )
    _assert(
        bl.normalize_team("FC Corvinul 1921 Hunedoara")
        == bl.normalize_team("CS Hunedoara"),
        "Corvinul↔Hunedoara",
    )
    _assert(
        bl.normalize_team("NK Varaždin")
        == bl.normalize_team("NK Varteks Varazdin"),
        "Varazdin↔Varteks",
    )
    _assert(
        bl.normalize_team("AE Lárisas 1964") == bl.normalize_team("Larissa"),
        "Larisas↔Larissa",
    )
    _assert(
        bl.normalize_team("Aluminij Kidricevo") == bl.normalize_team("NK Aluminij"),
        "Aluminij",
    )
    _assert(
        bl.normalize_team("Bravo Ljubljana") == bl.normalize_team("NK Bravo"),
        "Bravo",
    )
    _assert(
        bl.normalize_team("Racing W")
        == bl.normalize_team("Racing FC Union Luxembourg"),
        "Racing W",
    )
    # 2026-08-15 unmatched PM near-misses
    _assert(
        bl.normalize_team("Go Ahead Eagles")
        == bl.normalize_team("DVV Go Ahead"),
        "Go Ahead",
    )
    _assert(
        bl.normalize_team("Celta Fortuna")
        == bl.normalize_team("Celta Vigo B"),
        "Celta Fortuna↔B",
    )
    _assert(
        bl.normalize_team("SCR Altach")
        == bl.normalize_team("SC Rheindorf Altach"),
        "Altach",
    )
    _assert(
        bl.normalize_team("Real Racing Club")
        == bl.normalize_team("Racing Santander"),
        "Racing Santander",
    )
    _assert(
        bl.normalize_team("Rams Başakşehir FK")
        == bl.normalize_team("Başakşehir Futbol Kulübü"),
        "Başakşehir",
    )
    _assert(
        bl.normalize_team("RSC Anderlecht Futures")
        == bl.normalize_team("Anderlecht II"),
        "Anderlecht Futures",
    )
    _assert(
        bl.normalize_team("Inter Toronto FC")
        == bl.normalize_team("York United"),
        "Inter Toronto↔York",
    )
    _assert(
        bl.normalize_team("Jeugd KAA Gent B")
        == bl.normalize_team("Gent reserves"),
        "Gent B",
    )
    _assert(
        bl.normalize_team("Inverness Caledonian Thistle FC")
        == bl.normalize_team("Inverness CT"),
        "Inverness CT",
    )
    # 2026-08-21 league gaps (Serie A / ISR / GRE1 / GSC)
    _assert(bl.normalize_league("意甲", "") == "ita", "意甲")
    _assert(bl.normalize_league("Serie A", "") == "ita", "Serie A")
    _assert(bl.normalize_league("以超", "") == "isr", "以超")
    _assert(bl.normalize_league("GRE1", "gre1") == "gre1", "GRE1")
    _assert(bl.normalize_league("德超级杯", "") == "gsc", "德超级杯")
    _assert(
        bl.normalize_team("Qingdao Xihaian FC")
        == bl.normalize_team("Qingdao West Coast"),
        "Qingdao Xihaian",
    )
    _assert(
        bl.normalize_team("Club NXT")
        == bl.normalize_team("Club Brugge Ⅱ"),
        "Club NXT",
    )
    _assert(
        bl.normalize_team("FK Obolon Kyiv")
        == bl.normalize_team("Obolon'-Brovar"),
        "Obolon",
    )
    _assert(
        bl.normalize_team("Metalist 1925 W") == bl.normalize_team("Zhytlobud-1"),
        "Metalist W↔Zhytlobud",
    )
    _assert(bl.team_similarity("HJK", "HJK W") >= 0.92, "HJK W")
    _assert(bl.team_similarity("OH Leuven", "OH Leuven W") >= 0.92, "OH Leuven W")

    # PM lists Villa first; DQD/AF list Pathum as home with 1-3 → emit Villa 3-1
    _assert(
        bl.sides_are_swapped(
            "BG Pathum United",
            "Aston Villa",
            "Aston Villa",
            "BG Pathum United",
        ),
        "Villa/Pathum sides swapped",
    )
    oh, oa = bl.orient_scores(
        "BG Pathum United",
        "Aston Villa",
        1,
        3,
        "Aston Villa",
        "BG Pathum United",
    )
    _assert((oh, oa) == (3, 1), f"orient Pathum-home 1-3 → Villa-home got {oh}-{oa}")

    prev_scores: dict = {"m_flip": {"home": 1, "away": 2}}
    paired = [
        {
            "dongqiudi": {
                "id": "m_flip",
                "home": "BG Pathum United",
                "away": "Aston Villa",
                "home_score": 1,
                "away_score": 3,
                "status": "Playing",
                "status_raw": "Playing",
                "official_clock": "90'",
            },
            "polymarket": {
                "home": "Aston Villa",
                "away": "BG Pathum United",
                "event_id": "1",
                "slug": "x",
            },
            "kickoff_beijing": "2026-08-04 21:00",
        }
    ]
    evs = bl.detect_score_changes(paired, prev_scores)
    _assert(len(evs) == 1, f"expected 1 score_change, got {len(evs)}")
    _assert(evs[0]["home"] == "Aston Villa", "event home is PM")
    _assert(
        (evs[0]["home_score"], evs[0]["away_score"]) == (3, 1),
        f"PM-oriented score got {evs[0]['home_score']}-{evs[0]['away_score']}",
    )
    _assert(
        evs[0]["prev"] == {"home": 2, "away": 1},
        f"prev also oriented, got {evs[0]['prev']}",
    )
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

    _assert(bl.DEFAULT_PM_INTERVAL == 10800, "PM snapshot cadence is 3h")
    tmp = Path(tempfile.mkdtemp())
    snap_dir = tmp / "data" / "polymarket"
    snap_dir.mkdir(parents=True)
    snap = {
        "fetched_at": "2026-08-14T00:00:00+08:00",
        "count": 1,
        "matches": [{"home": "A", "away": "B"}],
    }
    (snap_dir / "snapshot.json").write_text(json.dumps(snap), encoding="utf-8")
    rt = bl.BridgeRuntime(tmp)
    _assert(rt.pm_interval == 10800, f"runtime default pm_interval, got {rt.pm_interval}")
    fake_load = MagicMock(side_effect=AssertionError("bridge must not call load_matches"))
    with patch.dict("sys.modules", {"pm_lib": MagicMock(load_matches=fake_load)}):
        got = rt.refresh_pm_once()
    _assert(got.get("count") == 1, f"reuse snapshot count, got {got.get('count')}")
    _assert(fake_load.call_count == 0, "Gamma load_matches must not run")

    print("smoke_match_hardening: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
