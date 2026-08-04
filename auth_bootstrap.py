from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

from app.auth_session import (
    auth_state_path_for,
    mark_authenticated_context,
    save_uploaded_auth_state,
    write_authenticated_observation,
    write_sanitized_authenticated_diagnostics,
)
from app.browser_hygiene import block_tracking_requests, close_popup_pages, disable_popups
from app.browser_probe import detect_page_signals
from app.competitors.registry import get_competitor, login_page_url
from app.config import (
    AUTHENTICATED_DIAGNOSTICS_DIR,
    DEFAULT_INPUT_CSV,
    DEFAULT_VIEWPORT,
    ProbeSettings,
    ensure_data_directories,
)
from app.input_loader import PartNotFoundError, find_part_record, load_parts_csv
from app.parsers.partzilla_product_parser import build_parse_input_from_probe, parse_partzilla_product_page
from app.schemas.product_observation import PageClassification, PriceVisibility, SessionStatus


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Manually create a saved authenticated browser session for a competitor.")
    parser.add_argument("--competitor", default="partzilla", help="Competitor key to save auth for. Defaults to partzilla.")
    parser.add_argument(
        "--part-number",
        default=None,
        help="Optional: sign in from this part's product page instead of the sign-in page.",
    )
    parser.add_argument("--url", help="Optional URL to open instead of building one from the default input CSV.")
    parser.add_argument(
        "--allow-popups",
        action="store_true",
        help="Allow the page to open popup windows. Only needed for a social sign-in that requires one.",
    )
    parser.add_argument("--slow-mo", type=int, default=0, help="Playwright slow motion in milliseconds.")
    parser.add_argument("--timeout", type=int, default=30000, help="Navigation timeout in milliseconds.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    ensure_data_directories()
    adapter = get_competitor(args.competitor)

    # Signing in happens on the sign-in page. Opening a product page instead
    # redirects a signed-out visitor and loads tracking pages, which made the
    # window flicker between tabs and impossible to sign in on. A part record is
    # only needed when a specific product page was explicitly requested.
    record = None
    if args.part_number:
        try:
            load_result = load_parts_csv(DEFAULT_INPUT_CSV)
            record = find_part_record(load_result.records, args.part_number)
        except (FileNotFoundError, ValueError, PartNotFoundError) as exc:
            print(f"Error: {exc}")
            return 1

    requested_url = args.url or (
        adapter.build_product_url(record) if record is not None else login_page_url(adapter)
    )
    checked_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    settings = ProbeSettings(headless=False, slow_mo=args.slow_mo, timeout=args.timeout)
    auth_state_path = auth_state_path_for(adapter.competitor_key)

    print(f"A browser window will open for {adapter.display_name}.")
    print("Sign in manually in that browser. This script will not type, click, store, or display your credentials.")
    print("")
    print("WHEN YOU ARE DONE SIGNING IN, JUST CLOSE THE BROWSER WINDOW.")
    print("Your sign-in will be saved automatically. You do not need to come back to this window.")
    print("")
    print(f"Page: {requested_url}")

    final_url: str | None = None
    status: int | None = None
    title: str | None = None
    html = ""
    text = ""
    navigation_succeeded = False
    exception_message: str | None = None
    signals: list[str] = []
    storage_saved = False
    observation = None
    cookie_count = 0

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=settings.headless, slow_mo=settings.slow_mo)
            context = browser.new_context(viewport=DEFAULT_VIEWPORT)
            # Playwright's Chromium has no ad blocker, so tracking scripts run
            # that a normal desktop browser would stop. Some of them open
            # popups, which take focus mid-typing and then close themselves,
            # making it impossible to sign in.
            block_tracking_requests(context)
            if not args.allow_popups:
                disable_popups(context)
            page = context.new_page()
            close_popup_pages(context, page)
            page.set_default_timeout(settings.timeout)
            page.set_default_navigation_timeout(settings.timeout)

            response = page.goto(requested_url, wait_until="domcontentloaded", timeout=settings.timeout)
            navigation_succeeded = True
            status = response.status if response is not None else None
            page.wait_for_timeout(settings.render_settle_ms)

            snapshot = _wait_for_manual_sign_in(page, context, settle_ms=settings.render_settle_ms)
            final_url = snapshot.final_url
            title = snapshot.title
            html = snapshot.html
            text = snapshot.text
            signals = detect_page_signals(text=text, html=html)

            if adapter.competitor_key == "partzilla" and record is not None:
                observation = parse_partzilla_product_page(
                    build_parse_input_from_probe(
                        record=record,
                        html=html,
                        visible_text=text,
                        final_url=final_url,
                        http_status=status,
                        page_title=title,
                        navigation_succeeded=navigation_succeeded,
                        exception_message=exception_message,
                        detected_signals=signals,
                        checked_at=checked_at,
                    )
                )
                mark_authenticated_context(observation, auth_state_loaded=False)
                result = _bootstrap_result(observation.session_status, observation.page_classification, observation.price_visibility)
                should_save = (
                    observation.session_status == SessionStatus.AUTHENTICATED
                    or snapshot.signed_in_observed
                )
            else:
                result = "session_saved" if snapshot.signed_in_observed else "session_saved_unconfirmed"
                should_save = True

            # The state is written from the snapshot captured while polling, so this
            # still works when the user simply closes the browser to finish.
            cookie_count = len((snapshot.storage_state or {}).get("cookies") or [])
            if should_save and cookie_count:
                save_uploaded_auth_state(
                    adapter.competitor_key,
                    json.dumps(snapshot.storage_state).encode("utf-8"),
                )
                storage_saved = True
            elif should_save:
                # Nothing to save means no cookies were ever captured, which is
                # worth stating plainly rather than reporting a bare failure.
                print("")
                print("No browser session was captured, so there was nothing to save.")
                print("This usually means the browser closed before the sign-in completed.")

            try:
                context.close()
                browser.close()
            except Exception:
                # The user closing the browser is a normal way to finish here.
                pass

    except (PlaywrightTimeoutError, PlaywrightError, Exception) as exc:
        exception_message = str(exc)
        result = "navigation_error"

    print("")
    print("===============================")
    if storage_saved:
        print("  Your sign-in was SAVED.")
        print("===============================")
        print("")
        print("You can close this window and start the price check again.")
        print("")
        print(f"(Technical details: result={result}, {cookie_count} cookies, saved to {auth_state_path})")
        print("Treat that file like a password. Do not share it or commit it.")
    else:
        print("  Your sign-in was NOT saved.")
        print("===============================")
        print("")
        print("Please try again, and this time make sure you are fully signed in")
        print("before closing the browser window.")
        print("")
        print(f"(Technical details: result={result})")
    print("")

    if not storage_saved and observation is not None:
        part_label = record.oem_part_number if record is not None else adapter.competitor_key
        output_dir = AUTHENTICATED_DIAGNOSTICS_DIR / f"{stamp}_bootstrap_{_safe_filename(part_label)}"
        observation_path = output_dir / "observation.json"
        diagnostics_path = output_dir / "sanitized_diagnostics.txt"
        write_authenticated_observation(observation_path, observation)
        write_sanitized_authenticated_diagnostics(diagnostics_path, observation, exception_message=exception_message)
        _print_unconfirmed_summary(observation, observation_path, diagnostics_path)
    elif not storage_saved and exception_message:
        print(f"Exception: {exception_message}")
    return 0 if storage_saved else 1


SIGNED_OUT_MARKERS = ("sign in to see price", "login to see price", "sign in for price")
SIGNED_IN_MARKERS = ("sign out", "log out", "logout", "my account", "account dashboard")


@dataclass
class SignInSnapshot:
    """Last readable state of the sign-in page, captured while polling."""

    final_url: str | None = None
    title: str | None = None
    html: str = ""
    text: str = ""
    storage_state: dict | None = None
    signed_in_observed: bool = False
    closed_by_user: bool = False
    timed_out: bool = False
    detected_signals: list[str] = field(default_factory=list)


def _looks_signed_in(*, text: str, html: str) -> bool:
    visible = text.lower()
    if any(marker in visible for marker in SIGNED_OUT_MARKERS):
        return False
    return any(marker in visible for marker in SIGNED_IN_MARKERS)


def _wait_for_manual_sign_in(
    page,
    context,
    *,
    settle_ms: int,
    timeout_seconds: int = 900,
    poll_seconds: float = 2.0,
) -> SignInSnapshot:
    """Wait for the user to sign in, then capture the browser session.

    Finishes as soon as the page looks signed in, or when the user closes the
    browser window. Capturing the state during polling (rather than after)
    means closing the browser is a valid way to finish, which is what people
    naturally do.
    """
    snapshot = SignInSnapshot()
    deadline = time.monotonic() + timeout_seconds
    consecutive_read_errors = 0

    while time.monotonic() < deadline:
        if page.is_closed():
            snapshot.closed_by_user = True
            return snapshot

        # Cookies come from the context, not the page, so this keeps working
        # while the page is mid-navigation. Capture it first and on its own,
        # because it is the only thing actually needed to save the sign-in.
        try:
            state = context.storage_state()
            if state:
                snapshot.storage_state = state
        except Exception:
            pass

        html = ""
        text = ""
        try:
            html = page.content()
            text = page.locator("body").inner_text(timeout=5000) if page.locator("body").count() else ""
            snapshot.final_url = page.url
            snapshot.title = page.title()
            snapshot.html = html
            snapshot.text = text
            consecutive_read_errors = 0
        except Exception:
            # Signing in submits a form, which navigates, and reading a page
            # mid-navigation raises. Treating that as a closed browser used to
            # abandon the wait and discard the sign-in. Keep polling instead,
            # and only give up if the window is really gone.
            consecutive_read_errors += 1
            if page.is_closed():
                snapshot.closed_by_user = True
                return snapshot
            if consecutive_read_errors >= 30:
                snapshot.closed_by_user = True
                return snapshot
            time.sleep(poll_seconds)
            continue

        if _looks_signed_in(text=text, html=html):
            try:
                page.wait_for_timeout(settle_ms)
                snapshot.storage_state = context.storage_state() or snapshot.storage_state
                snapshot.html = page.content()
                snapshot.text = page.locator("body").inner_text(timeout=5000)
                snapshot.final_url = page.url
                snapshot.title = page.title()
            except Exception:
                pass
            snapshot.signed_in_observed = True
            return snapshot

        time.sleep(poll_seconds)

    snapshot.timed_out = True
    return snapshot


def _bootstrap_result(
    session_status: SessionStatus,
    page_classification: PageClassification,
    price_visibility: PriceVisibility,
) -> str:
    if session_status == SessionStatus.AUTHENTICATED:
        return "authentication_confirmed"
    if page_classification == PageClassification.BLOCKED:
        return "blocked"
    if page_classification == PageClassification.CHALLENGE:
        return "challenge"
    if page_classification == PageClassification.NAVIGATION_ERROR:
        return "navigation_error"
    if price_visibility == PriceVisibility.SIGN_IN_REQUIRED:
        return "authentication_not_confirmed"
    return "unknown"


def _print_unconfirmed_summary(observation, observation_path, diagnostics_path) -> None:
    print(f"Final URL: {observation.final_url or ''}")
    print(f"Page: {observation.page_classification.value}")
    print(f"Price visibility: {observation.price_visibility.value}")
    print(f"Session status: {observation.session_status.value}")
    print(f"Product: {observation.product_name or ''}")
    print(f"MSRP: {observation.msrp_raw or ''}")
    print(f"Partzilla selling price: {observation.selling_price_raw or ''}")
    print(f"Warnings: {', '.join(observation.parse_warnings) or 'None'}")
    print(f"Bootstrap observation JSON: {observation_path}")
    print(f"Bootstrap sanitized diagnostics: {diagnostics_path}")


def _safe_filename(value: str) -> str:
    return "".join(char if char.isalnum() or char in ("-", "_") else "_" for char in value)


if __name__ == "__main__":
    sys.exit(main())
