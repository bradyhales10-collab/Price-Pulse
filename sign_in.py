"""Sign in to one competitor, with nothing else running.

Signing in during or alongside a price check does not work: the collectors
open their own browsers and steal focus, and queued sign-in requests can each
spawn another window. This does one competitor at a time, on its own:

  1. stops the Browser Helper so nothing else can open a window
  2. clears any queued sign-in requests so none of them fire later
  3. opens one browser on that competitor's sign-in page
  4. waits for the sign-in, then saves it
  5. confirms it against the live site before saying it worked

Run it from the Price-Pulse folder:
    .venv\\Scripts\\python.exe sign_in.py --competitor partzilla
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

from app.auth_session import auth_state_path_for, delete_auth_state
from app.competitors.registry import get_competitor, list_competitors, login_page_url
from app.session_check import first_probe_part, verify_saved_session

ROOT = Path(__file__).resolve().parent
LOGIN_REQUEST_DIR = ROOT / "data" / "output" / "ui_collection_jobs" / "local_login_requests"
DEFAULT_INPUT_DIR = ROOT / "data" / "input"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Sign in to one competitor, with nothing else running.")
    parser.add_argument("--competitor", required=True)
    parser.add_argument(
        "--keep-helper-running",
        action="store_true",
        help="Do not stop the Browser Helper first. Not recommended.",
    )
    return parser.parse_args()


def stop_browser_helper() -> None:
    """Stop the Browser Helper so it cannot open a window while we sign in."""
    if os.name != "nt":
        return
    for title in ("Part Pulse Browser Helper", "Part Pulse Dashboard"):
        subprocess.run(
            ["taskkill", "/F", "/FI", f"WINDOWTITLE eq {title}*"],
            capture_output=True,
            text=True,
            timeout=15,
        )


def clear_queued_sign_in_requests() -> int:
    """Discard queued sign-in requests.

    Each queued request opens its own browser when the Browser Helper picks it
    up. Several clicks of the Sign In button therefore produce several windows,
    which appear and disappear while you are trying to type.
    """
    if not LOGIN_REQUEST_DIR.exists():
        return 0
    cleared = 0
    for path in LOGIN_REQUEST_DIR.glob("*.json"):
        try:
            path.unlink()
            cleared += 1
        except OSError:
            pass
    return cleared


def _probe_part(competitor_key: str):
    """A real part to confirm the sign-in with, from any available input file."""
    for candidate in sorted(DEFAULT_INPUT_DIR.glob("*.csv")):
        part = first_probe_part(candidate, competitor_key)
        if part is not None:
            return part
    return None


def main() -> int:
    args = parse_args()
    try:
        adapter = get_competitor(args.competitor)
    except ValueError:
        known = ", ".join(a.competitor_key for a in list_competitors())
        print(f"Unknown competitor '{args.competitor}'. Choose one of: {known}")
        return 1

    name = adapter.display_name
    print("=" * 60)
    print(f"  Sign in to {name}")
    print("=" * 60)
    print("")

    if not adapter.requires_login:
        print(f"{name} does not need a sign-in. Nothing to do.")
        return 0

    if not args.keep_helper_running:
        print("[1 of 5] Stopping Part Pulse so nothing else opens a window...")
        stop_browser_helper()
        print("         Done.")
    else:
        print("[1 of 5] Leaving Part Pulse running, as asked.")

    print("[2 of 5] Clearing any queued sign-in requests...")
    cleared = clear_queued_sign_in_requests()
    print(f"         Cleared {cleared}.")

    print("[3 of 5] Removing the old saved sign-in...")
    delete_auth_state(adapter.competitor_key)
    print("         Done.")

    print("[4 of 5] Opening the sign-in page...")
    print(f"         {login_page_url(adapter)}")
    print("")
    print("         Sign in in that window, then CLOSE it.")
    print("         Nothing else is running, so nothing will interrupt you.")
    print("")
    result = subprocess.run(
        [sys.executable, str(ROOT / "auth_bootstrap.py"), "--competitor", adapter.competitor_key],
        cwd=ROOT,
    )
    if result.returncode != 0:
        print("")
        print("The sign-in was not saved. Run this again and make sure you are fully")
        print(f"signed in to {name} before closing the window.")
        return 1

    if not auth_state_path_for(adapter.competitor_key).exists():
        print("")
        print("No sign-in was saved. Run this again.")
        return 1

    print("")
    print("[5 of 5] Confirming the sign-in against the live site...")
    part = _probe_part(adapter.competitor_key)
    if part is None:
        print("         Skipped: no parts file available to check against.")
        print("")
        print(f"The {name} sign-in was saved but could not be confirmed.")
        print("Upload a parts file in Part Pulse, then run this again to confirm it.")
        return 0

    confirmed, reason = verify_saved_session(adapter.competitor_key, part, headless=True)
    print(f"         {reason}")
    print("")
    print("=" * 60)
    if confirmed:
        print(f"  {name} sign-in CONFIRMED.")
        print("=" * 60)
        print("")
        print('Now double-click "Start Part Pulse.cmd" and run your price check.')
        return 0

    print(f"  {name} sign-in did NOT work.")
    print("=" * 60)
    print("")
    print("The sign-in was saved but the site still does not treat us as signed in.")
    print("Run this again, and check that you can see a price on the site in that")
    print("same window before closing it.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
