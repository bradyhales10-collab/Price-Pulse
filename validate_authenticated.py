from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
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
from app.authenticated_validation import update_authenticated_summary, write_authenticated_review
from app.browser_probe import detect_page_signals
from app.config import (
    AUTHENTICATED_DIAGNOSTICS_DIR,
    AUTHENTICATED_VALIDATION_MANIFEST,
    AUTHENTICATED_VALIDATION_REVIEW,
    AUTHENTICATED_VALIDATION_SUMMARY,
    DEFAULT_DATABASE_PATH,
    DEFAULT_VIEWPORT,
    PARTZILLA_AUTH_STATE_PATH,
    ProbeSettings,
    ensure_data_directories,
)
from app.database import (
    complete_scan_run,
    connect_database,
    create_scan_run,
    initialize_database,
    persist_observation,
    seed_partzilla,
    upsert_product_and_listing,
)
from app.parsers.partzilla_product_parser import build_parse_input_from_probe, parse_partzilla_product_page
from app.price_forensics import (
    PriceCandidateRole,
    apply_price_evidence_to_observation,
    build_price_evidence,
    write_price_evidence,
)
from app.raw_price_signals import discover_raw_price_signals, write_raw_price_signals
from app.schemas.product_observation import PageClassification
from app.url_builder import build_partzilla_product_url
from app.validation import ValidationPartNotFoundError, find_validation_part, load_validation_manifest


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run one selected authenticated validation case.")
    parser.add_argument("--part-number", required=True, help="OEM part number from authenticated_validation_parts.csv.")
    parser.add_argument("--slow-mo", type=int, default=0, help="Playwright slow motion in milliseconds.")
    parser.add_argument("--timeout", type=int, default=30000, help="Navigation timeout in milliseconds.")
    parser.add_argument("--manual-price-confirmation", action="store_true", help="Reserved for manual debugging.")
    parser.add_argument("--save-to-database", action="store_true", help="Persist this one validation result to SQLite.")
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE_PATH)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    ensure_data_directories()

    try:
        auth_state_path = require_auth_state(PARTZILLA_AUTH_STATE_PATH)
    except MissingAuthStateError as exc:
        print(f"Error: {exc}")
        print(r"Run .\.venv\Scripts\python.exe auth_bootstrap.py first.")
        return 1

    try:
        manifest_parts = load_validation_manifest(AUTHENTICATED_VALIDATION_MANIFEST)
        manifest_part = find_validation_part(manifest_parts, args.part_number)
    except (FileNotFoundError, KeyError, ValueError, ValidationPartNotFoundError) as exc:
        print(f"Error: {exc}")
        return 1

    print(f"Authenticated validation part {manifest_part.validation_order} of {len(manifest_parts)}")
    print(f"Part: {manifest_part.oem_part_number}")
    print(f"Purpose: {manifest_part.test_purpose}")

    result = inspect_authenticated_validation_part(
        manifest_part.to_part_record(),
        auth_state_path=str(auth_state_path),
        settings=ProbeSettings(headless=False, slow_mo=args.slow_mo, timeout=args.timeout),
    )
    observation, price_evidence, observation_path = result
    update_authenticated_summary(
        AUTHENTICATED_VALIDATION_SUMMARY,
        manifest_part,
        observation,
        price_evidence,
        observation_path,
    )
    write_authenticated_review(AUTHENTICATED_VALIDATION_REVIEW, manifest_parts, AUTHENTICATED_VALIDATION_SUMMARY)
    db_result = None
    if args.save_to_database:
        db_result = save_one_result_to_database(args.database, manifest_part.to_part_record(), observation, price_evidence, observation_path)
    print(f"Page: {observation.page_classification.value}")
    print(f"Session status: {observation.session_status.value}")
    print(f"Product: {observation.product_name or ''}")
    print(f"Selling price: {observation.selling_price_raw or observation.selling_price or ''}")
    print(f"Price confidence: {observation.price_parse_confidence.value}")
    print(f"Warnings: {', '.join(observation.parse_warnings) or 'None'}")
    print(f"Observation JSON: {observation_path}")
    if db_result:
        print(f"Database save result: {db_result}")
    print(f"Validation summary: {AUTHENTICATED_VALIDATION_SUMMARY}")
    print(f"Validation review: {AUTHENTICATED_VALIDATION_REVIEW}")
    return 0 if observation.page_classification != PageClassification.NAVIGATION_ERROR else 1


def inspect_authenticated_validation_part(record, *, auth_state_path: str, settings: ProbeSettings):
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    checked_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    output_dir = AUTHENTICATED_DIAGNOSTICS_DIR / f"{stamp}_{_safe_filename(record.oem_part_number)}"
    observation_path = output_dir / "observation.json"
    diagnostics_path = output_dir / "sanitized_diagnostics.txt"
    price_evidence_path = output_dir / "price_evidence.json"
    raw_price_signals_path = output_dir / "raw_price_signals.json"

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
            context = browser.new_context(storage_state=auth_state_path, viewport=DEFAULT_VIEWPORT)
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
    apply_price_evidence_to_observation(observation, price_evidence)
    write_authenticated_observation(observation_path, observation)
    write_sanitized_authenticated_diagnostics(diagnostics_path, observation, exception_message=exception_message)
    write_price_evidence(price_evidence_path, price_evidence)
    write_raw_price_signals(raw_price_signals_path, observation=observation, signals=raw_price_signals)
    return observation, price_evidence, observation_path


def save_one_result_to_database(database_path, record, observation, price_evidence, observation_path):
    initialize_database(database_path)
    with connect_database(database_path) as conn:
        with conn:
            competitor_id = seed_partzilla(conn)
            _, listing_id, _, _ = upsert_product_and_listing(conn, record)
            scan_run_id = create_scan_run(conn, competitor_id=competitor_id, requested_part_count=1)
            result = persist_observation(
                conn,
                scan_run_id=scan_run_id,
                listing_id=listing_id,
                observation=observation,
                observation_json_path=str(observation_path),
                price_source_category=_price_source_category(price_evidence),
            )
            complete_scan_run(conn, scan_run_id)
            return result


def _price_source_category(price_evidence):
    if price_evidence.selected_selling_price is None:
        return None
    for candidate in price_evidence.price_candidates:
        if candidate.candidate_role == PriceCandidateRole.SELLING_PRICE and candidate.normalized_value == price_evidence.selected_selling_price:
            return candidate.source_type.value
    return None


def _safe_filename(value: str) -> str:
    return "".join(char if char.isalnum() or char in ("-", "_") else "_" for char in value)


if __name__ == "__main__":
    sys.exit(main())
