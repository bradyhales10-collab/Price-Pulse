"""Atomically replace a file, tolerating a brief Windows file lock.

Two real runs crashed entirely because of this: collect_parts.py writes its
progress file every part, while the Browser Helper polls that same file
roughly twice a second to forward progress to the dashboard. On Windows,
os.replace can fail with "PermissionError: [WinError 5] Access is denied" if
something else has the destination file open at that exact instant - the
poller reading it here, or something like antivirus scanning it. The lock is
normally held for milliseconds, so a short retry resolves it without a
meaningful delay.

The waits are deliberately brief. This runs on every progress write during a
run and on every job status write the dashboard makes, so a generous retry
would add up across thousands of writes and make the whole application feel
sluggish. Six attempts at 20 milliseconds covers a real lock while costing at
most about a tenth of a second in the worst case.

Two independent places in this project write a temp file and replace it into
place: collect_parts.py's own progress file, and the job status file the
dashboard writes and the Browser Helper polls. Both are read concurrently by
another process on a tight loop, so both are equally exposed to this. This is
used by both rather than duplicating the retry logic in each.
"""

from __future__ import annotations

import time
from pathlib import Path


def replace_with_retry(tmp: Path, path: Path, *, attempts: int = 6, delay_seconds: float = 0.02) -> None:
    last_exc: OSError | None = None
    for attempt in range(attempts):
        try:
            tmp.replace(path)
            return
        except PermissionError as exc:
            last_exc = exc
            if attempt < attempts - 1:
                time.sleep(delay_seconds)
    assert last_exc is not None
    raise last_exc
