"""Atomically replace a file, tolerating a brief Windows file lock.

Two real runs crashed entirely because of this: collect_parts.py writes its
progress file every part, while the Browser Helper polls that same file
roughly twice a second to forward progress to the dashboard. On Windows,
os.replace can fail with "PermissionError: [WinError 5] Access is denied" if
something else has the destination file open at that exact instant - the
poller reading it here, or something like antivirus scanning it. The lock is
normally held for milliseconds, so a short retry resolves it without a
meaningful delay.

The wait doubles each attempt rather than staying flat, because the two
requirements pull in opposite directions and a single fixed wait cannot serve
both. This runs on every progress write during a run and every job status
write the dashboard makes, so it has to be nearly free when there is no
contention; but a lock that genuinely lasts a moment has to be waited out, or
the run crashes.

A flat 100ms across 8 attempts was patient enough but added up across
thousands of writes. Cutting it to a flat 20ms across 6 made the application
responsive again and then crashed a run at part 884 with PermissionError,
because 0.12 seconds of total patience was not enough for a real lock.

Doubling from 10ms gives both: the common case costs nothing, since the first
attempt succeeds, while a stubborn lock is waited out for about 2.5 seconds
in total before giving up.

Two independent places in this project write a temp file and replace it into
place: collect_parts.py's own progress file, and the job status file the
dashboard writes and the Browser Helper polls. Both are read concurrently by
another process on a tight loop, so both are equally exposed to this. This is
used by both rather than duplicating the retry logic in each.
"""

from __future__ import annotations

import time
from pathlib import Path


def replace_with_retry(tmp: Path, path: Path, *, attempts: int = 8, first_delay_seconds: float = 0.01) -> None:
    last_exc: OSError | None = None
    delay = first_delay_seconds
    for attempt in range(attempts):
        try:
            tmp.replace(path)
            return
        except PermissionError as exc:
            last_exc = exc
            if attempt < attempts - 1:
                time.sleep(delay)
                delay *= 2
    assert last_exc is not None
    raise last_exc
