#!/usr/bin/env python3
"""Empirical reversal-risk score for pitch-gate buys.

Label = DQD ``is_goal`` later undone by an inverse ``is_reversal`` on the same
match. Features are the 8-cell lookup (opening × clock≥75' × prior_same).
Logistic regression on the same bits does not beat this table.

Buy path (``pitch_gate.start_gate``) only **skips** the dirty cells:

- opening + same transition already reversed (``prior_same``) — ~50–71% undone
- opening + clock ≥ 90'

Ordinary opening goals (Delfin / Operário-like 0-0→1-0 at ~35') stay ``full``.
``haircut`` is recorded on the score object but is **not** applied to size.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[3]
DEFAULT_EVENTS = ROOT / "data" / "bridge" / "events.jsonl"
DEFAULT_OUT = ROOT / "data" / "trade-analytics" / "reversal_lookup.json"

OPENING_TRANS = frozenset({"0-0→1-0", "0-0→0-1"})

# Fit 2026-08-24 on 511 goals / 72 undone. Cells omitted from a fresh fit
# still resolve via DEFAULT_LOOKUP so score() works offline.
DEFAULT_LOOKUP: dict[tuple[int, int, int], dict[str, float | int]] = {
    (0, 0, 0): {"n": 226, "undone": 20, "p": 0.0885},
    (0, 0, 1): {"n": 8, "undone": 1, "p": 0.125},
    (0, 1, 0): {"n": 88, "undone": 14, "p": 0.1591},
    (0, 1, 1): {"n": 6, "undone": 1, "p": 0.1667},
    (1, 0, 0): {"n": 151, "undone": 22, "p": 0.1457},
    (1, 0, 1): {"n": 16, "undone": 8, "p": 0.5},
    (1, 1, 0): {"n": 9, "undone": 1, "p": 0.1111},
    (1, 1, 1): {"n": 7, "undone": 5, "p": 0.7143},
}


@dataclass(frozen=True)
class ReversalFeatures:
    opening: int
    clock_min: int | None
    clock_ge_75: int
    clock_ge_90: int
    prior_same: int
    prior_match: int
    transition: str


@dataclass(frozen=True)
class ReversalScore:
    p_rev: float
    cell: tuple[int, int, int]
    action: str
    size_mult: float
    features: ReversalFeatures

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["cell"] = list(self.cell)
        return d


def _ts(row: dict[str, Any]) -> datetime:
    return datetime.fromisoformat(str(row["ts"]).replace("Z", "+00:00"))


def transition(row: dict[str, Any]) -> str:
    prev = row.get("prev") or {}
    curr = row.get("curr") or {}
    return f"{prev.get('home')}-{prev.get('away')}→{curr.get('home')}-{curr.get('away')}"


def inverse_transition(trans: str) -> str:
    a, b = trans.split("→", 1)
    return f"{b}→{a}"


def clock_min_of(row: dict[str, Any]) -> int | None:
    raw = str(row.get("official_clock") or row.get("status") or "")
    m = re.search(r"(\d{1,3})", raw)
    return int(m.group(1)) if m else None


def extract_features(
    row: dict[str, Any],
    *,
    prior_same: bool,
    prior_match: bool,
) -> ReversalFeatures:
    trans = transition(row)
    clk = clock_min_of(row)
    opening = int(trans in OPENING_TRANS)
    return ReversalFeatures(
        opening=opening,
        clock_min=clk,
        clock_ge_75=int((clk or 0) >= 75),
        clock_ge_90=int((clk or 0) >= 90),
        prior_same=int(bool(prior_same)),
        prior_match=int(bool(prior_match)),
        transition=trans,
    )


def cell_of(feat: ReversalFeatures) -> tuple[int, int, int]:
    return (feat.opening, feat.clock_ge_75, feat.prior_same)


def lookup_p(
    feat: ReversalFeatures,
    table: dict[tuple[int, int, int], dict[str, float | int]] | None = None,
) -> float:
    tab = table or DEFAULT_LOOKUP
    row = tab.get(cell_of(feat))
    if row is None:
        return 0.141
    return float(row["p"])


def decide_action(feat: ReversalFeatures, p_rev: float) -> tuple[str, float]:
    """Skip high-p opening re-awards / 90'+ openings; do not shrink the bulk.

    ``haircut`` is advisory only until a size-policy change is explicit.
    """
    if feat.opening and feat.prior_same:
        return "skip", 0.0
    if feat.opening and feat.clock_ge_90:
        return "skip", 0.0
    if feat.prior_same:
        return "haircut", 0.5
    if p_rev >= 0.35:
        return "haircut", 0.5
    return "full", 1.0


def score(
    feat: ReversalFeatures,
    table: dict[tuple[int, int, int], dict[str, float | int]] | None = None,
) -> ReversalScore:
    p = lookup_p(feat, table)
    action, mult = decide_action(feat, p)
    return ReversalScore(
        p_rev=round(p, 4),
        cell=cell_of(feat),
        action=action,
        size_mult=mult,
        features=feat,
    )


def score_event(
    ev: dict[str, Any],
    *,
    prior_same: bool,
    prior_match: bool = False,
) -> ReversalScore:
    """Score a live ``score_change`` goal using event ``prev``/``curr`` + clock."""
    feat = extract_features(ev, prior_same=bool(prior_same), prior_match=bool(prior_match))
    return score(feat)


def _iter_score_changes(path: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    with path.open() as fh:
        for line in fh:
            if not line.strip():
                continue
            row = json.loads(line)
            if row.get("type") == "score_change":
                out.append(row)
    return out


def fit_lookup(events: Iterable[dict[str, Any]]) -> dict[tuple[int, int, int], dict[str, float | int]]:
    ev = list(events)
    goals = [x for x in ev if x.get("is_goal") and not x.get("is_reversal")]
    revs = [x for x in ev if x.get("is_reversal")]
    rev_by: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in revs:
        rev_by[str(r.get("match_id"))].append(r)
    rev_index = [(_ts(r), str(r.get("match_id")), transition(r)) for r in revs]

    cells: dict[tuple[int, int, int], list[int]] = defaultdict(lambda: [0, 0])
    for g in sorted(goals, key=lambda x: x.get("ts") or ""):
        gt = _ts(g)
        mid = str(g.get("match_id"))
        trans = transition(g)
        inv = inverse_transition(trans)
        undone = 0
        for r in rev_by[mid]:
            if transition(r) == inv and _ts(r) >= gt:
                undone = 1
                break
        prior_same = any(m == mid and rr == inv and rt < gt for rt, m, rr in rev_index)
        prior_match = any(m == mid and rt < gt for rt, m, rr in rev_index)
        feat = extract_features(g, prior_same=prior_same, prior_match=prior_match)
        key = cell_of(feat)
        cells[key][0] += 1
        cells[key][1] += undone
    table: dict[tuple[int, int, int], dict[str, float | int]] = {}
    for key, (n, u) in cells.items():
        table[key] = {"n": n, "undone": u, "p": (u / n) if n else 0.0}
    return table


def _table_to_json(table: dict[tuple[int, int, int], dict[str, float | int]]) -> dict[str, Any]:
    return {
        "label": "dqd_goal_later_undone_by_inverse_reversal",
        "features": ["opening", "clock_ge_75", "prior_same"],
        "cells": {
            ",".join(str(x) for x in k): v
            for k, v in sorted(table.items())
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    fit_p = sub.add_parser("fit", help="Rebuild lookup from bridge events.jsonl")
    fit_p.add_argument("--events", type=Path, default=DEFAULT_EVENTS)
    fit_p.add_argument("--out", type=Path, default=DEFAULT_OUT)
    score_p = sub.add_parser("score", help="Score one goal from flags")
    score_p.add_argument("--opening", type=int, required=True)
    score_p.add_argument("--clock", type=int, default=None)
    score_p.add_argument("--prior-same", type=int, default=0)
    score_p.add_argument("--prior-match", type=int, default=0)
    args = parser.parse_args(argv)

    if args.cmd == "fit":
        table = fit_lookup(_iter_score_changes(args.events))
        payload = _table_to_json(table)
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(payload, indent=2) + "\n")
        print(f"wrote {args.out}")
        for key in sorted(table):
            row = table[key]
            print(
                f"  opening={key[0]} c75={key[1]} prior_same={key[2]}  "
                f"n={row['n']:3d} undone={row['undone']:3d} p={float(row['p']):.3f}"
            )
        return 0

    clk = args.clock
    feat = ReversalFeatures(
        opening=int(args.opening),
        clock_min=clk,
        clock_ge_75=int((clk or 0) >= 75),
        clock_ge_90=int((clk or 0) >= 90),
        prior_same=int(args.prior_same),
        prior_match=int(args.prior_match),
        transition="?",
    )
    print(json.dumps(score(feat).to_dict(), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
