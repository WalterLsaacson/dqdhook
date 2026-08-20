#!/usr/bin/env python3
"""CLI entrypoint for pitch-state judgment."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from pipeline import judge_inputs  # noqa: E402


def _parse_elapsed(raw: str | None) -> list[float]:
    if not raw:
        return []
    out: list[float] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        out.append(float(part))
    return out


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="Judge whether play has resumed from Dongqiudi screenshots.")
    ap.add_argument("--image")
    ap.add_argument("--images", nargs="*")
    ap.add_argument("--elapsed", help="Comma-separated elapsed seconds matching --images order.")
    ap.add_argument("--observe-jsonl")
    ap.add_argument("--match-id")
    ap.add_argument("--event-key")
    ap.add_argument("--output-path")
    ap.add_argument("--no-append", action="store_true")
    ap.add_argument("--json", action="store_true", help="Print JSON result.")
    return ap


def main() -> int:
    args = build_parser().parse_args()
    if not any([args.image, args.images, args.observe_jsonl]):
        print("need --image, --images, or --observe-jsonl", file=sys.stderr)
        return 2
    result = judge_inputs(
        image=Path(args.image) if args.image else None,
        images=[Path(p) for p in args.images] if args.images else None,
        elapsed=_parse_elapsed(args.elapsed),
        observe_jsonl=Path(args.observe_jsonl) if args.observe_jsonl else None,
        match_id=args.match_id,
        event_key=args.event_key,
        append_output=not args.no_append,
        output_path=Path(args.output_path) if args.output_path else None,
    )
    if args.json:
        json.dump(result, sys.stdout, ensure_ascii=False, indent=2)
        print()
    else:
        print(f"play_state={result['play_state']} frame_type={result['frame_type']} confidence={result['confidence']:.2f}")
        if result.get("evidence"):
            print("evidence:")
            for item in result["evidence"]:
                print(f"- {item}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
