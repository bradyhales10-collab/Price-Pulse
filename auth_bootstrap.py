from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

from app.auth_session import (
    auth_state_path_for,
    mark_authenticated_context,
    write_authenticated_observation,
    write_sanitized_authenticated_diagnostics,
)
from app.browser_probe import detect_page_signals
from app.competitors.registry import get_competitor
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
    parser.add_argument("--part-number", default="41080-1514", help="One OEM part number to use for manual login.")
    parser.add_argument("--url", help="Optional URL to open instead of building one from the default input CSV.")
    parser.add_argument("--slow-mo", type=int, default=0, help="Playwright slow motion in milliseconds.")
    parser.add_argument("--timeout", type=int, default=30000, help="Navigation timeout in milliseconds.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    ensure_data_directories()
    adapter = get_competitor(args.competitor)

    record = None
    if not args.url:
        try:
            load_result = load_parts_csv(DEFAULT_INPUT_CSV)
            record = find_part_record(load_result.records, args.part_number)
        except (FileNotFoundError, ValueError, PartNotFoundError) as exc:
            print(f"Error: {exc}")
            return 1

    requested_url = args.url or adapter.build_product_url(record)
    checked_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    settings = ProbeSettings(headless=False, slow_mo=args.slow_mo, timeout=args.timeout)
    auth_state_path = auth_state_path_for(adapter.competitor_key)

    print(f"A browser window will open for {adapter.display_name}.")
    print("Sign in manually in that browser. This script will not type, click, store, or display your credentials.")
    if adapter.competitor_key == "partzilla":
        print("After the product page shows a visible main product price, come back here and press Enter.")
    else:
        print("After you are signed in and the page is stable, come back here and press Enter.")
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

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=settings.headless, slow_mo=settings.slow_mo)
            context = browser.new_context(viewport=DEFAULT_VIEWPORT)
            page = context.new_page()
            page.set_default_timeout(settings.timeout)
            page.set_default_navigation_timeout(settings.timeout)

            response = page.goto(requested_url, wait_until="domcontentloaded", timeout=settings.timeout)
            navigation_succeeded = True
            status = response.status if response is not None else None
            page.wait_for_timeout(settings.render_settle_ms)

            input("Press Enter after you have manually signed in and returned to the product page...")
            page.wait_for_timeout(settings.render_settle_ms)

            final_url = page.url
            title = page.title()
            html = page.content()
            text = page.locator("body").inner_text(timeout=5000) if page.locator("body").count() else ""
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
                should_save = observation.session_status == SessionStatus.AUTHENTICATED
            else:
                result = "session_saved"
                should_save = True

            if should_save:
                auth_state_path.parent.mkdir(parents=True, exist_ok=True)
                context.storage_state(path=auth_state_path)
                storage_saved = True

            context.close()
            browser.close()

    except (PlaywrightTimeoutError, PlaywrightError, Exception) as exc:
        exception_message = str(exc)
        result = "navigation_error"

    print(f"Result: {result}")
    print(f"Auth state saved: {'yes' if storage_saved else 'no'}")
    if storage_saved:
        print(f"Auth state path: {auth_state_path}")
        print("Treat this file like a password. Do not share it or commit it.")
    elif observation is not None:
        part_label = record.oem_part_number if record is not None else adapter.competitor_key
        output_dir = AUTHENTICATED_DIAGNOSTICS_DIR / f"{stamp}_bootstrap_{_safe_filename(part_label)}"
        observation_path = output_dir / "observation.json"
        diagnostics_path = output_dir / "sanitized_diagnostics.txt"
        write_authenticated_observation(observation_path, observation)
        write_sanitized_authenticated_diagnostics(diagnostics_path, observation, exception_message=exception_message)
        _print_unconfirmed_summary(observation, observation_path, diagnostics_path)
    elif exception_message:
        print(f"Exception: {exception_message}")
    return 0 if storage_saved else 1


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
