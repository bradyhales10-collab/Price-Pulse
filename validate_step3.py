from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

from app.browser_probe import detect_page_signals
from app.config import (
    DEFAULT_VIEWPORT,
    DIAGNOSTICS_DIR,
    STEP3_VALIDATION_MANIFEST,
    STEP3_VALIDATION_REVIEW,
    STEP3_VALIDATION_SUMMARY,
    ProbeSettings,
    ensure_data_directories,
)
from app.logging_setup import setup_logging
from app.parsers.partzilla_product_parser import build_parse_input_from_probe, parse_partzilla_product_page
from app.schemas.product_observation import (
    AvailabilityStatus,
    PageClassification,
    PriceVisibility,
    ProductObservation,
)
from app.url_builder import build_partzilla_product_url
from app.validation import (
    ValidationPartNotFoundError,
    find_validation_part,
    load_validation_manifest,
    update_validation_summary,
    write_validation_review,
)

LOGGER = logging.getLogger(__name__)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run one selected step 3 validation case.")
    parser.add_argument("--part-number", required=True, help="OEM part number from step3_validation_parts.csv.")
    parser.add_argument("--headless", action="store_true", help="Run Chromium without a visible window.")
    parser.add_argument("--slow-mo", type=int, default=0, help="Playwright slow motion in milliseconds.")
    parser.add_argument("--timeout", type=int, default=30000, help="Navigation timeout in milliseconds.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    ensure_data_directories()
    setup_logging()

    try:
        manifest_parts = load_validation_manifest(STEP3_VALIDATION_MANIFEST)
        manifest_part = find_validation_part(manifest_parts, args.part_number)
    except (FileNotFoundError, KeyError, ValueError, ValidationPartNotFoundError) as exc:
        print(f"Error: {exc}")
        return 1

    print(f"Validation part {manifest_part.validation_order} of {len(manifest_parts)}")
    print(f"Part: {manifest_part.oem_part_number}")
    print(f"Purpose: {manifest_part.test_purpose}")

    observation, observation_path = inspect_validation_part(
        manifest_part.to_part_record(),
        ProbeSettings(headless=args.headless, slow_mo=args.slow_mo, timeout=args.timeout),
    )
    update_validation_summary(STEP3_VALIDATION_SUMMARY, manifest_part, observation, observation_path)
    write_validation_review(STEP3_VALIDATION_REVIEW, manifest_parts, STEP3_VALIDATION_SUMMARY)
    _print_summary(observation, observation_path)
    print(f"Validation summary: {STEP3_VALIDATION_SUMMARY}")
    print(f"Validation review: {STEP3_VALIDATION_REVIEW}")
    return 0 if observation.page_classification != PageClassification.NAVIGATION_ERROR else 1


def inspect_validation_part(record, settings: ProbeSettings) -> tuple[ProductObservation, Path]:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    checked_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    output_dir = DIAGNOSTICS_DIR / f"{stamp}_{_safe_filename(record.oem_part_number)}"
    output_dir.mkdir(parents=True, exist_ok=True)

    requested_url = build_partzilla_product_url(record.manufacturer, record.oem_part_number)
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
            context = browser.new_context(viewport=DEFAULT_VIEWPORT)
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
            page.screenshot(path=output_dir / "screenshot.png", full_page=True)
            context.close()
            browser.close()
    except (PlaywrightTimeoutError, PlaywrightError, Exception) as exc:
        exception_message = str(exc)
        LOGGER.exception("Step 3 validation failed for %s", record.oem_part_number)

    (output_dir / "rendered.html").write_text(html, encoding="utf-8")
    parse_input = build_parse_input_from_probe(
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
    observation = parse_partzilla_product_page(parse_input)
    observation_path = output_dir / "observation.json"
    observation_path.write_text(json.dumps(observation.to_json_dict(), indent=2) + "\n", encoding="utf-8")
    _write_diagnostics(output_dir / "diagnostics.txt", observation, exception_message)
    return observation, observation_path


def _write_diagnostics(path: Path, observation: ProductObservation, exception_message: str | None) -> None:
    lines = [
        f"checked_at: {observation.checked_at}",
        f"test_case_id: {observation.test_case_id or ''}",
        f"manufacturer: {observation.manufacturer}",
        f"oem_part_number: {observation.oem_part_number}",
        f"observed_part_number: {observation.observed_part_number or ''}",
        f"requested_url: {observation.requested_url}",
        f"final_url: {observation.final_url or ''}",
        f"canonical_url: {observation.canonical_url or ''}",
        f"http_status: {observation.http_status if observation.http_status is not None else ''}",
        f"page_title: {observation.page_title or ''}",
        f"page_classification: {observation.page_classification.value}",
        f"price_visibility: {observation.price_visibility.value}",
        f"parse_confidence: {observation.parse_confidence.value}",
        f"parse_warnings: {', '.join(observation.parse_warnings) or 'None'}",
        f"exception_message: {exception_message or ''}",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _print_summary(observation: ProductObservation, observation_path: Path) -> None:
    selling = "Not publicly visible"
    if observation.price_visibility == PriceVisibility.VISIBLE and observation.selling_price_raw:
        selling = observation.selling_price_raw

    availability = observation.availability_raw or observation.availability_status.value
    if observation.availability_status == AvailabilityStatus.SHIPS_IN and observation.shipping_estimate:
        availability = f"Ships in {observation.shipping_estimate}"

    print(f"Page: {observation.page_classification.value}")
    print(f"Price visibility: {observation.price_visibility.value}")
    print(f"Product: {observation.product_name or ''}")
    print(f"MSRP: {observation.msrp_raw or ''}")
    print(f"Selling price: {selling}")
    print(f"Availability: {availability}")
    print(f"Confidence: {observation.parse_confidence.value}")
    print(f"Observation JSON: {observation_path}")


def _safe_filename(value: str) -> str:
    return "".join(char if char.isalnum() or char in ("-", "_") else "_" for char in value)


if __name__ == "__main__":
    sys.exit(main())
