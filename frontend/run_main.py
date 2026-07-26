#!/usr/bin/env python3
"""Launch System Main (boards + quote watch with in-process trading).

Examples:
  python3 frontend/run_main.py
  python3 frontend/run_main.py --take-depth walk --max-usdc 5
  python3 frontend/run_main.py --live --max-usdc 2
  python3 frontend/run_main.py --goals-mode dry --ft-mode live --max-usdc 1
  python3 frontend/run_main.py --goals-mode live --ft-mode dry --max-usdc 1
  python3 frontend/run_main.py --no-trade
"""

from __future__ import annotations

import runpy
from pathlib import Path

APP = Path(__file__).resolve().parent / "main" / "server" / "app.py"


def main() -> int:
    # Preserve CLI flags for app.main() argparse (runpy keeps sys.argv).
    runpy.run_path(str(APP), run_name="__main__")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
