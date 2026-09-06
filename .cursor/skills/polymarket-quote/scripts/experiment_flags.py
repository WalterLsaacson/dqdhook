"""Branch-level hard-stops for quote strategies.

Code constants — not ``.env``. Leftover ``QUOTE_T10_USDC=100`` / ``QUOTE_LOCKED_SWEEP=1``
must not re-enable a path this experiment branch has turned off.
Tests that still exercise the old implementation call ``disable_hard_stops_for_tests``.
"""

from __future__ import annotations

HARD_STOP_T10 = True
HARD_STOP_FT_SCAN = True
HARD_STOP_POSTFT_SWEEP = True
HARD_STOP_LOCKED_SWEEP = True


def disable_hard_stops_for_tests() -> None:
    """Let existing T10 / FT / postft / locked-sweep smokes hit the real code."""
    global HARD_STOP_T10, HARD_STOP_FT_SCAN, HARD_STOP_POSTFT_SWEEP, HARD_STOP_LOCKED_SWEEP
    HARD_STOP_T10 = False
    HARD_STOP_FT_SCAN = False
    HARD_STOP_POSTFT_SWEEP = False
    HARD_STOP_LOCKED_SWEEP = False


def restore_hard_stops() -> None:
    global HARD_STOP_T10, HARD_STOP_FT_SCAN, HARD_STOP_POSTFT_SWEEP, HARD_STOP_LOCKED_SWEEP
    HARD_STOP_T10 = True
    HARD_STOP_FT_SCAN = True
    HARD_STOP_POSTFT_SWEEP = True
    HARD_STOP_LOCKED_SWEEP = True
