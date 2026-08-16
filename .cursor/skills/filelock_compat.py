#!/usr/bin/env python3
"""Windows exclusive lock for sidecar ``{file}.lock`` files (msvcrt)."""

from __future__ import annotations

import msvcrt
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


@contextmanager
def exclusive_file_lock(lock_path: Path) -> Iterator[None]:
    """Hold an exclusive lock on ``lock_path`` for the duration of the block."""
    lock_path = Path(lock_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("ab+") as lf:
        if lock_path.stat().st_size == 0:
            lf.write(b"\0")
            lf.flush()
        lf.seek(0)
        msvcrt.locking(lf.fileno(), msvcrt.LK_LOCK, 1)
        try:
            yield
        finally:
            lf.seek(0)
            msvcrt.locking(lf.fileno(), msvcrt.LK_UNLCK, 1)
