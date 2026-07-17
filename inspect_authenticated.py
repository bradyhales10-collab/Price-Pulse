from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

from app.auth_session import (
    MissingAuthStateError,
    mark_authenticated_context,
    require_auth_state,
    write_authenticated_observation,
    write_sanitized_authenticated_diagnostics,
)
from app.browser_probe import detect_page_signals
from app.config import (
    AUTHENTICATED_DIAGNOSTICS_DIR,
    DEFAULT_INPUT_CSV,
    DEFAULT_VIEWPORT,
    PARTZILLA_AUTH_STATE_PATH,
    ProbeSettings,
    ensure_data_directories,
)
from app.input_loader import PartNotFoundError, find_part_record, load_parts_csv
from app.parsers.partzilla_product_parser import build_parse_input_from_probe, parse_partzilla_product_page
from app.price_forensics import (
    add_manual_validation,
    apply_price_evidence_to_observation,
    build_price_dom_debug,
    build_price_evidence,
    write_price_dom_debug,
    write_price_evidence,
    write_product_purchase_region_text,
)
from app.raw_price_signals import discover_raw_price_signals, write_raw_price_signals
from app.schemas.product_observation import AvailabilityStatus, PageClassification, PriceVisibility, ProductObservation
from app.url_builder import build_partzilla_product_url


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Inspect one Partzilla product with a saved authenticated session.")
    parser.add_argument("--part-number", required=True, help="One OEM part number to inspect from the CSV.")
    parser.add_argument("--slow-mo", type=int, default=0, help="Playwright slow motion in milliseconds.")
    parser.add_argument("--timeout", type=int, default=30000, help="Navigation timeout in milliseconds.")
    parser.add_argument(
        "--manual-price-confirmation",
        action="store_true",
        help="Ask for visible MSRP and selling-price confirmation after parsing.",
    )
    parser.add_argument(
        "--debug-price-dom",
        action="store_true",
        help="Save bounded, sanitized price-region DOM debug files.",
    )
    parser.add_argument(
        "--debug-raw-price-signals",
        action="store_true",
        help="Save sanitized raw product-associated price signal evidence.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    ensure_data_directories()

    try:
        auth_state_path = require_auth_state(PARTZILLA_AUTH_STATE_PATH)
    except MissingAuthStateError as exc:
        print(f"Error: {exc}")
        print("Run this first:")
        print(r".\.venv\Scripts\python.exe auth_bootstrap.py")
        return 1

    try:
        load_result = load_parts_csv(DEFAULT_INPUT_CSV)
        record = find_part_record(load_result.records, args.part_number)
    except (FileNotFoundError, ValueError, PartNotFoundError) as exc:
        print(f"Error: {exc}")
        return 1

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    checked_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    output_dir = AUTHENTICATED_DIAGNOSTICS_DIR / f"{stamp}_{_safe_filename(record.oem_part_number)}"
    observation_path = output_dir / "observation.json"
    diagnostics_path = output_dir / "sanitized_diagnostics.txt"
    price_evidence_path = output_dir / "price_evidence.json"
    raw_price_signals_path = output_dir / "raw_price_signals.json"
    price_dom_debug_path = output_dir / "price_dom_debug.json"
    product_region_path = output_dir / "product_purchase_region.txt"

    requested_url = build_partzilla_product_url(record.manufacturer, record.oem_part_number)
    settings = ProbeSettings(headless=False, slow_mo=args.slow_mo, timeout=args.timeout)

    final_url: str | None = None
    status: int | None = None
    title: str | None = None
    html = ""
    text = ""
    navigation_succeeded = False
    exception_message: str | None = None
    signals: list[str] = []

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=settings.headless, slow_mo=settings.slow_mo)
            context = browser.new_context(storage_state=str(auth_state_path), viewport=DEFAULT_VIEWPORT)
            page = context.new_page()
            page.set_default_timeout(settings.timeout)
            page.set_default_navigation_timeout(settings.timeout)

            response = page.goto(requested_url, wait_until="domcontentloaded", timeout=settings.timeout)
            navigation_succeeded = True
            status = response.status if response is not None else None
            page.wait_for_timeout(settings.render_settle_ms)

            final_url = page.url
            title = page.title()
            html = page.content()
            text = page.locator("body").inner_text(timeout=5000) if page.locator("body").count() else ""
            signals = detect_page_signals(text=text, html=html)

            context.close()
            browser.close()

    except (PlaywrightTimeoutError, PlaywrightError, Exception) as exc:
        exception_message = str(exc)

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
    mark_authenticated_context(observation, auth_state_loaded=True)
    raw_price_signals = discover_raw_price_signals(html=html, visible_text=text, observation=observation)
    if any(signal.rejection_reason is None for signal in raw_price_signals):
        observation.classification_evidence.append("Product-associated raw price signal detected")
    price_evidence = build_price_evidence(
        html=html,
        visible_text=text,
        observation=observation,
        raw_price_signals=raw_price_signals,
    )
    if args.manual_price_confirmation:
        _print_price_candidates(price_evidence)
        selling_input = input("What selling price do you visibly see on the product page? Enter a dollar amount, none, or unclear: ")
        msrp_input = input("What MSRP do you visibly see? Enter a dollar amount, none, or unclear: ")
        price_evidence = add_manual_validation(
            price_evidence,
            selling_price_input=selling_input,
            msrp_input=msrp_input,
        )
    apply_price_evidence_to_observation(observation, price_evidence)
    write_authenticated_observation(observation_path, observation)
    write_sanitized_authenticated_diagnostics(diagnostics_path, observation, exception_message=exception_message)
    write_price_evidence(price_evidence_path, price_evidence)
    write_raw_price_signals(raw_price_signals_path, observation=observation, signals=raw_price_signals)
    write_debug_outputs = args.debug_price_dom or args.manual_price_confirmation
    if write_debug_outputs:
        debug = build_price_dom_debug(html=html, visible_text=text, observation=observation, evidence=price_evidence)
        write_price_dom_debug(price_dom_debug_path, debug)
        write_product_purchase_region_text(product_region_path, html=html, visible_text=text, observation=observation)
    _print_summary(
        observation,
        observation_path,
        diagnostics_path,
        price_evidence_path,
        raw_price_signals_path,
        price_dom_debug_path if write_debug_outputs else None,
        product_region_path if write_debug_outputs else None,
    )
    return 0 if observation.page_classification != PageClassification.NAVIGATION_ERROR else 1


def _print_summary(
    observation: ProductObservation,
    observation_path: Path,
    diagnostics_path: Path,
    price_evidence_path: Path,
    raw_price_signals_path: Path,
    price_dom_debug_path: Path | None,
    product_region_path: Path | None,
) -> None:
    availability = observation.availability_raw or observation.availability_status.value
    if observation.availability_status == AvailabilityStatus.SHIPS_IN and observation.shipping_estimate:
        availability = f"Ships in {observation.shipping_estimate}"

    selling = observation.selling_price_raw or ""
    if observation.price_visibility == PriceVisibility.SIGN_IN_REQUIRED:
        selling = "Sign in required"
    elif observation.price_visibility != PriceVisibility.VISIBLE:
        selling = "Not visible"

    print(f"Part: {observation.oem_part_number}")
    print(f"Access context: {observation.access_context.value}")
    print(f"Session status: {observation.session_status.value}")
    print(f"Page: {observation.page_classification.value}")
    print(f"Price visibility: {observation.price_visibility.value}")
    print(f"Product: {observation.product_name or ''}")
    print(f"MSRP: {observation.msrp_raw or ''}")
    print(f"Partzilla selling price: {selling}")
    print(f"Availability: {availability}")
    print(f"Confidence: {observation.parse_confidence.value}")
    print(f"Price confidence: {observation.price_parse_confidence.value}")
    print(f"Price validation: {observation.price_validation_status.value}")
    print(f"Warnings: {', '.join(observation.parse_warnings) or 'None'}")
    print(f"Observation JSON path: {observation_path}")
    print(f"Sanitized diagnostics path: {diagnostics_path}")
    print(f"Price evidence path: {price_evidence_path}")
    print(f"Raw price signals path: {raw_price_signals_path}")
    if price_dom_debug_path:
        print(f"Price DOM debug path: {price_dom_debug_path}")
    if product_region_path:
        print(f"Product purchase region path: {product_region_path}")
    print("Observation JSON:")
    print(json.dumps(observation.to_json_dict(), indent=2))


def _print_price_candidates(price_evidence) -> None:
    print("Detected main-product price candidates:")
    if not price_evidence.price_candidates:
        print("  None")
        return
    for index, candidate in enumerate(price_evidence.price_candidates, start=1):
        print(
            f"  {index}. {candidate.raw_text} | role={candidate.candidate_role.value} | "
            f"confidence={candidate.candidate_confidence.value} | label={candidate.nearby_label or ''}"
        )
        print(f"     context: {candidate.visible_text_context}")


def _safe_filename(value: str) -> str:
    return "".join(char if char.isalnum() or char in ("-", "_") else "_" for char in value)


if __name__ == "__main__":
    sys.exit(main())
