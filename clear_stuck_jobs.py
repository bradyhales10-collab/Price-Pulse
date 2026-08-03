"""Clear any price checks left stuck as queued or running.

Called automatically when Part Pulse restarts. At that moment no Browser
Helper is running yet, so any job still marked active is guaranteed to be
stale, not genuinely in progress. Restarting should mean a clean slate rather
than a stuck job silently occupying the screen for up to two hours.
"""

from __future__ import annotations

import sys

from app.collection_jobs import cancel_all_active_jobs


def main() -> int:
    cleared = cancel_all_active_jobs()
    if cleared:
        print(f"Cleared {cleared} stuck price check(s) from before this restart.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
