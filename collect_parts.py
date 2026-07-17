from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

from app.auth_session import auth_state_path_for, mark_authenticated_context, require_competitor_auth_state
from app.browser_probe import detect_page_signals
from app.collection import (
    CollectionRow,
    CollectionRunResult,
    fingerprint_file,
    normalize_result_type,
    plan_collection,
    print_plan,
    stop_status_for,
    validate_delay,
    write_collection_outputs,
)
from app.config import DEFAULT_DATABASE_PATH, DEFAULT_VIEWPORT, OUTPUT_DIR, ProbeSettings, ensure_data_directories
from app.database import (
    cents_to_money,
    complete_scan_run,
    connect_database,
    create_scan_run,
    initialize_database,
    persist_observation,
    seed_competitor,
    seed_motosport,
    seed_partzilla,
    upsert_competitor_listing,
    utc_now,
)
from app.input_loader import load_parts_csv
from app.manufacturer_registry import competitor_supports_manufacturer, manufacturer_support_metadata, normalize_manufacturer
from app.models import PartRecord
from app.parsers.partzilla_product_parser import build_parse_input_from_probe, parse_partzilla_product_page
from app.price_forensics import PriceCandidateRole, apply_price_evidence_to_observation, build_price_evidence, write_price_evidence
from app.raw_price_signals import discover_raw_price_signals, write_raw_price_signals
from app.schemas.product_observation import AccessContext, AvailabilityStatus, PageClassification, ParseConfidence, PriceDisplayType, PriceValidationStatus, PriceVisibility, ProductObservation, SessionStatus
from app.url_builder import build_partzilla_product_url
from app.auth_session import write_authenticated_observation, write_sanitized_authenticated_diagnostics
from app.competitors.motosport import MotoSportAdapter
from app.competitors.chaparral import ChaparralAdapter, build_search_url, normalize_part_number_for_match
from app.competitors.registry import get_competitor, select_competitors
from export_current_prices import export_current_prices
from export_price_changes import export_price_changes
from probe_cart_price import (
    bounded_cart_action_inventory,
    cart_line_evidence,
    click_cart_action_with_result,
    collect_cart_line_records,
    CartProbeInputRow,
    ensure_cart_empty,
    extract_tracking_label,
    open_cart_text,
    remove_cart_item,
    select_high_confidence_cart_action,
    validate_cart_action_form,
    wait_for_cart_response,
)


HEAVY_RESOURCE_TYPES = {"image", "font", "media"}
COMPETITOR_RENDER_SETTLE_MS = 1000
CHAPARRAL_SEARCH_SETTLE_MS = 500
CHAPARRAL_LOOKUP_POLL_MS = 250


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a controlled authenticated Partzilla collection.")
    parser.add_argument("--file", type=Path, required=True)
    parser.add_argument("--max-parts", type=int, required=True)
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE_PATH)
    parser.add_argument("--delay-seconds", type=int, default=10)
    parser.add_argument("--collection-mode", choices=["full_browser", "lightweight_browser"], default="full_browser")
    parser.add_argument("--competitor", action="append", default=None, help="Production competitor to collect. Defaults to partzilla.")
    parser.add_argument("--allow-experimental-competitors", action="store_true", help="Acknowledge experimental competitors. Production collection still only supports approved adapters.")
    parser.add_argument("--headless", action="store_true", help="Run Chromium without opening a visible browser window.")
    parser.add_argument("--progress-file", type=Path, help="Write live collection progress as JSON.")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--save-to-database", action="store_true")
    parser.add_argument("--yes", action="store_true", help="Skip RUN confirmation. Not recommended for controlled testing.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    ensure_data_directories()
    competitor_keys = args.competitor or ["partzilla"]
    try:
        competitors = select_competitors(competitor_keys, allow_experimental=args.allow_experimental_competitors)
    except ValueError as exc:
        print(f"Error: {exc}")
        return 1
    if len(competitors) != 1:
        print("Error: collect_parts.py runs one competitor per scan run. Use the UI to run multiple competitors together.")
        return 1
    competitor = competitors[0]
    validate_delay(args.delay_seconds)
    load_result = load_parts_csv(args.file)
    initialize_database(args.database)
    with connect_database(args.database) as conn:
        ensure_competitor_listings(conn, load_result.records, competitor.competitor_key)
        plan = plan_collection(conn, load_result.records, args.file, args.max_parts, invalid_rows=len(load_result.invalid_rows), competitor_key=competitor.competitor_key)
    print_plan(plan)
    if args.dry_run:
        return 0
    if not args.save_to_database:
        print("Error: live collection requires --save-to-database for this phase.")
        return 1
    if competitor.requires_login:
        require_competitor_auth_state(competitor.competitor_key, competitor_name=competitor.display_name)
    if not args.yes:
        print(f"This run will inspect {len(plan.planned_parts)} {competitor.display_name} product pages.")
        if input("Type RUN to continue: ").strip() != "RUN":
            print("Confirmation not entered. Exiting without creating a scan run.")
            return 1
    return run_collection(args, plan)


def ensure_competitor_listings(conn, records, competitor_key: str) -> None:
    adapter = get_competitor(competitor_key)
    competitor_id = seed_competitor(conn, competitor_key)
    for record in records:
        product = conn.execute(
            "SELECT product_id FROM products WHERE manufacturer=? AND normalized_part_number=?",
            (record.manufacturer, record.oem_part_number.strip().upper()),
        ).fetchone()
        if product is None:
            continue
        canonical_url = adapter.build_product_url(record) if competitor_supports_manufacturer(competitor_key, record.manufacturer) else ""
        upsert_competitor_listing(
            conn,
            product_id=int(product["product_id"]),
            competitor_id=competitor_id,
            competitor_part_number=record.oem_part_number,
            canonical_url=canonical_url,
        )


def run_collection(args, plan) -> int:
    settings = ProbeSettings(headless=bool(getattr(args, "headless", False)), timeout=30000)
    competitor_key = plan.competitor_key
    with connect_database(args.database) as conn:
        competitor_id = seed_competitor(conn, competitor_key)
        scan_run_id = create_scan_run(conn, competitor_id=competitor_id, requested_part_count=len(plan.planned_parts))
    result = CollectionRunResult(scan_run_id=scan_run_id, started_at=utc_now())
    consecutive_errors = 0
    started_monotonic = time.monotonic()
    print(f"{competitor_key.upper()} COLLECTION RUN")
    print(f"Run ID: {scan_run_id}")
    print(f"Parts planned: {len(plan.planned_parts)}")
    print(f"Delay: {args.delay_seconds} seconds")
    print(f"Collection mode: {args.collection_mode}")
    _write_progress(args, result, plan, status="running", started_monotonic=started_monotonic)
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=settings.headless)
            if get_competitor(competitor_key).requires_login:
                context = browser.new_context(storage_state=str(auth_state_path_for(competitor_key)), viewport=DEFAULT_VIEWPORT)
            else:
                context = browser.new_context(viewport=DEFAULT_VIEWPORT)
            if args.collection_mode == "lightweight_browser":
                context.route("**/*", lambda route: route.abort() if route.request.resource_type in HEAVY_RESOURCE_TYPES else route.continue_())
            page = context.new_page()
            page.set_default_timeout(settings.timeout)
            page.set_default_navigation_timeout(settings.timeout)
            for planned in plan.planned_parts:
                if result.rows:
                    time.sleep(args.delay_seconds)
                if competitor_key == "motosport":
                    row = collect_one_motosport_part(args.database, page, planned, scan_run_id, settings)
                elif competitor_key == "chaparral":
                    row = collect_one_chaparral_part(args.database, page, planned, scan_run_id, settings)
                else:
                    row = collect_one_part(args.database, page, planned, scan_run_id, settings)
                result.rows.append(row)
                result.last_attempted_part = planned.oem_part_number
                print(f"[{planned.run_order}/{len(plan.planned_parts)}] {planned.oem_part_number} | {row.selling_price or ''} | {row.result_type.upper()}")
                _write_progress(args, result, plan, status="running", started_monotonic=started_monotonic)
                stop_status = stop_status_for(row)
                if stop_status:
                    result.run_status = stop_status
                    result.stop_reason = row.result_type
                    break
                if row.result_type in {"navigation_error", "error"}:
                    consecutive_errors += 1
                    if consecutive_errors >= 2:
                        result.run_status = "failed"
                        result.stop_reason = "two_consecutive_operational_errors"
                        break
                else:
                    consecutive_errors = 0
            context.close()
            browser.close()
    finally:
        result.completed_at = utc_now()
        with connect_database(args.database) as conn:
            complete_scan_run(conn, scan_run_id)
            if result.run_status != "running":
                conn.execute("UPDATE scan_runs SET run_status=? WHERE scan_run_id=?", (result.run_status, scan_run_id))
            else:
                result.run_status = conn.execute("SELECT run_status FROM scan_runs WHERE scan_run_id=?", (scan_run_id,)).fetchone()[0]
        try:
            export_current_prices(args.database)
            export_price_changes(args.database)
        except Exception as exc:
            result.export_warning = str(exc)
        write_collection_outputs(result=result, plan=plan, delay_seconds=args.delay_seconds, input_fingerprint=fingerprint_file(args.file))
        _write_progress(args, result, plan, status=result.run_status, started_monotonic=started_monotonic)
    return 0


def _write_progress(args, result: CollectionRunResult, plan, *, status: str, started_monotonic: float) -> None:
    progress_file = getattr(args, "progress_file", None)
    if not progress_file:
        return
    total = len(plan.planned_parts)
    completed = len(result.rows)
    elapsed_seconds = max(0, int(time.monotonic() - started_monotonic))
    average_seconds = elapsed_seconds / completed if completed else None
    remaining = total - completed
    eta_seconds = int(average_seconds * remaining) if average_seconds is not None else None
    payload = {
        "status": status,
        "competitor": plan.competitor_key,
        "scan_run_id": result.scan_run_id,
        "total": total,
        "completed": completed,
        "remaining": remaining,
        "elapsed_seconds": elapsed_seconds,
        "eta_seconds": eta_seconds,
        "last_attempted_part": result.last_attempted_part,
        "started_at": result.started_at,
        "completed_at": result.completed_at,
        "run_status": result.run_status,
        "stop_reason": result.stop_reason,
        "rows": [row.__dict__ for row in result.rows[-50:]],
    }
    path = Path(progress_file)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    tmp.replace(path)


def collect_one_part(database_path: Path, page, planned, scan_run_id: int, settings: ProbeSettings) -> CollectionRow:
    support = manufacturer_support_metadata("partzilla", planned.manufacturer, planned.oem_part_number)
    if not support["manufacturer_supported"]:
        return manufacturer_not_carried_row(database_path, planned, scan_run_id, support)
    requested_url = build_partzilla_product_url(planned.manufacturer, planned.oem_part_number)
    status = None
    final_url = None
    title = None
    html = ""
    text = ""
    navigation_succeeded = False
    exception_message = None
    checked_at = utc_now()
    try:
        response = page.goto(requested_url, wait_until="domcontentloaded", timeout=settings.timeout)
        navigation_succeeded = True
        status = response.status if response is not None else None
        page.wait_for_timeout(settings.render_settle_ms)
        final_url = page.url
        title = page.title()
        html = page.content()
        text = page.locator("body").inner_text(timeout=5000) if page.locator("body").count() else ""
    except (PlaywrightTimeoutError, PlaywrightError, Exception) as exc:
        exception_message = str(exc)
    from app.models import PartRecord
    record = PartRecord(test_case_id="", manufacturer=planned.manufacturer, oem_part_number=planned.oem_part_number)
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
            detected_signals=detect_page_signals(text=text, html=html),
            checked_at=checked_at,
        )
    )
    mark_authenticated_context(observation, auth_state_loaded=True)
    raw_signals = discover_raw_price_signals(html=html, visible_text=text, observation=observation)
    price_evidence = build_price_evidence(html=html, visible_text=text, observation=observation, raw_price_signals=raw_signals)
    apply_price_evidence_to_observation(observation, price_evidence)
    from app.config import AUTHENTICATED_DIAGNOSTICS_DIR
    output_dir = AUTHENTICATED_DIAGNOSTICS_DIR / f"{checked_at.replace(':','').replace('-','')}_{planned.oem_part_number}"
    observation_path = output_dir / "observation.json"
    write_authenticated_observation(observation_path, observation)
    write_sanitized_authenticated_diagnostics(output_dir / "sanitized_diagnostics.txt", observation, exception_message=exception_message)
    write_price_evidence(output_dir / "price_evidence.json", price_evidence)
    write_raw_price_signals(output_dir / "raw_price_signals.json", observation=observation, signals=raw_signals)
    previous_price = planned.current_price_cents
    with connect_database(database_path) as conn:
        previous = conn.execute("SELECT * FROM current_listing_state WHERE listing_id=?", (planned.listing_id,)).fetchone()
        previous_price = previous["selling_price_cents"] if previous else None
        previous_availability = previous["availability_status"] if previous else None
        persisted_result = persist_observation(
            conn,
            scan_run_id=scan_run_id,
            listing_id=planned.listing_id,
            observation=observation,
            observation_json_path=str(observation_path),
            price_source_category=_price_source_category(price_evidence),
        )
        scan_event_id = conn.execute("SELECT MAX(scan_event_id) FROM scan_events WHERE scan_run_id=? AND listing_id=?", (scan_run_id, planned.listing_id)).fetchone()[0]
    result_type = collection_result_type(observation, status, persisted_result)
    return CollectionRow(
        run_order=planned.run_order,
        scan_run_id=scan_run_id,
        scan_event_id=scan_event_id,
        manufacturer=planned.manufacturer,
        oem_part_number=planned.oem_part_number,
        normalized_manufacturer=normalize_manufacturer(planned.manufacturer),
        competitor="partzilla",
        manufacturer_supported=True,
        lookup_status=result_type,
        status_reason="",
        observed_part_number=observation.observed_part_number,
        product_name=observation.product_name,
        checked_at=observation.checked_at,
        http_status=observation.http_status,
        page_classification=observation.page_classification.value,
        session_status=observation.session_status.value,
        selling_price=str(observation.selling_price) if observation.selling_price is not None else None,
        reference_price=str(observation.reference_price) if observation.reference_price is not None else None,
        savings_percent=observation.savings_percent,
        price_display_type=observation.price_display_type.value,
        previous_selling_price=cents_to_money(previous_price),
        result_type=result_type,
        price_changed=result_type in {"price_change", "multiple_changes"},
        availability_raw=observation.availability_raw,
        previous_availability_status=previous_availability,
        availability_status=observation.availability_status.value,
        supersession_detected=observation.supersession_detected,
        superseded_by_raw=observation.superseded_by_raw,
        price_source_category=_price_source_category(price_evidence),
        price_corroboration_count=_corroboration_count(price_evidence),
        price_parse_confidence=observation.price_parse_confidence.value,
        parse_confidence=observation.parse_confidence.value,
        warning_count=len(observation.parse_warnings),
        warnings="; ".join(observation.parse_warnings),
        observation_json_path=str(observation_path),
    )


def collect_one_motosport_part(database_path: Path, page, planned, scan_run_id: int, settings: ProbeSettings) -> CollectionRow:
    support = manufacturer_support_metadata("motosport", planned.manufacturer, planned.oem_part_number)
    if not support["manufacturer_supported"]:
        return manufacturer_not_carried_row(database_path, planned, scan_run_id, support)
    adapter = MotoSportAdapter()
    record = PartRecord(test_case_id="", manufacturer=planned.manufacturer, oem_part_number=planned.oem_part_number)
    requested_url = adapter.build_product_url(record)
    status = None
    final_url = None
    html = ""
    text = ""
    exception_message = None
    checked_at = utc_now()
    cart_price_collected = False
    cleanup_status = "not_attempted"
    try:
        response = page.goto(requested_url, wait_until="domcontentloaded", timeout=settings.timeout)
        status = response.status if response is not None else None
        page.wait_for_timeout(min(settings.render_settle_ms, COMPETITOR_RENDER_SETTLE_MS))
        final_url = page.url
        html = page.content()
        text = page.locator("body").inner_text(timeout=5000) if page.locator("body").count() else ""
    except (PlaywrightTimeoutError, PlaywrightError, Exception) as exc:
        exception_message = str(exc)

    observation = adapter.parse_product_page(html, record, visible_text=text, final_url=final_url, http_status=status)
    if exception_message:
        observation.page_classification = "navigation_error"
        observation.warnings.append(exception_message)

    if observation.page_classification == "normal_product" and observation.price_visibility == "see_price_in_cart":
        cart_row = CartProbeInputRow(
            manufacturer=planned.manufacturer,
            oem_part_number=planned.oem_part_number,
            product_name=observation.product_name or "",
            product_url=requested_url,
            reference_price=str(observation.reference_price or ""),
            prior_probe_note="see_price_in_cart",
        )
        inventory = bounded_cart_action_inventory(page, cart_row, observation)
        action = select_high_confidence_cart_action(inventory["candidates"])
        if action["status"] == "selected":
            form_validation = validate_cart_action_form(page, action["candidate"], row=cart_row, observation=observation)
            if form_validation["valid"] and ensure_cart_empty(page):
                click_result = click_cart_action_with_result(page, action["candidate"], timeout_ms=5000)
                if click_result["clicked"]:
                    wait_for_cart_response(page)
                    cart_text = open_cart_text(page)
                    line_evidence = cart_line_evidence(
                        cart_text,
                        cart_row,
                        supporting_sku=extract_tracking_label(action["candidate"]),
                        cart_line_records=collect_cart_line_records(page),
                    )
                    if line_evidence["confirmed"] and line_evidence["quantity"] == 1 and line_evidence["accepted_price"] is not None:
                        observation.selling_price = line_evidence["accepted_price"]
                        observation.price_visibility = "visible"
                        observation.price_display_type = "discounted" if observation.reference_price and observation.reference_price > observation.selling_price else "regular"
                        observation.selling_price_confidence = "medium"
                        observation.parse_confidence = "medium"
                        observation.warnings = [warning for warning in observation.warnings if warning != "selling_price_hidden_in_cart"]
                        observation.raw_evidence_summary["cart_price_evidence"] = line_evidence
                        cart_price_collected = True
                    cleanup_status = "success" if remove_cart_item(page, line_evidence=line_evidence) and ensure_cart_empty(page) else "failed"
                else:
                    observation.warnings.append(str(click_result["reason"]))
            else:
                observation.warnings.append("add_to_cart_form_validation_failed")
        else:
            observation.warnings.append("add_to_cart_button_not_found" if action["status"] == "not_found" else "ambiguous_cart_action")

    if cart_price_collected and cleanup_status != "success":
        observation.warnings.append("cart_cleanup_failed")
    product_observation = _product_observation_from_competitor(observation, requested_url=requested_url, checked_at=checked_at)
    output_dir = OUTPUT_DIR / "motosport_collection_diagnostics" / f"{checked_at.replace(':','').replace('-','')}_{planned.oem_part_number}"
    output_dir.mkdir(parents=True, exist_ok=True)
    observation_path = output_dir / "observation.json"
    observation_path.write_text(json.dumps(observation.to_json_dict(), indent=2, default=str) + "\n", encoding="utf-8")
    previous_price = planned.current_price_cents
    previous_availability = None
    with connect_database(database_path) as conn:
        previous = conn.execute("SELECT * FROM current_listing_state WHERE listing_id=?", (planned.listing_id,)).fetchone()
        previous_price = previous["selling_price_cents"] if previous else None
        previous_availability = previous["availability_status"] if previous else None
        persisted_result = persist_observation(
            conn,
            scan_run_id=scan_run_id,
            listing_id=planned.listing_id,
            observation=product_observation,
            observation_json_path=str(observation_path),
            price_source_category="motosport_cart" if cart_price_collected else "motosport_page",
        )
        scan_event_id = conn.execute("SELECT MAX(scan_event_id) FROM scan_events WHERE scan_run_id=? AND listing_id=?", (scan_run_id, planned.listing_id)).fetchone()[0]
    result_type = collection_result_type(product_observation, status, persisted_result)
    if cart_price_collected and cleanup_status != "success":
        result_type = "cleanup_failed"
    return CollectionRow(
        run_order=planned.run_order,
        scan_run_id=scan_run_id,
        scan_event_id=scan_event_id,
        manufacturer=planned.manufacturer,
        oem_part_number=planned.oem_part_number,
        normalized_manufacturer=normalize_manufacturer(planned.manufacturer),
        competitor="motosport",
        manufacturer_supported=True,
        lookup_status=result_type,
        status_reason="",
        observed_part_number=product_observation.observed_part_number,
        product_name=product_observation.product_name,
        checked_at=product_observation.checked_at,
        http_status=product_observation.http_status,
        page_classification=product_observation.page_classification.value,
        session_status=product_observation.session_status.value,
        selling_price=str(product_observation.selling_price) if product_observation.selling_price is not None else None,
        reference_price=str(product_observation.reference_price) if product_observation.reference_price is not None else None,
        savings_percent=product_observation.savings_percent,
        price_display_type=product_observation.price_display_type.value,
        previous_selling_price=cents_to_money(previous_price),
        result_type=result_type,
        price_changed=result_type in {"price_change", "multiple_changes"},
        availability_raw=product_observation.availability_raw,
        previous_availability_status=previous_availability,
        availability_status=product_observation.availability_status.value,
        supersession_detected=product_observation.supersession_detected,
        superseded_by_raw=product_observation.superseded_by_raw,
        price_source_category="motosport_cart" if cart_price_collected else "motosport_page",
        price_corroboration_count=1 if product_observation.selling_price is not None else 0,
        price_parse_confidence=product_observation.price_parse_confidence.value,
        parse_confidence=product_observation.parse_confidence.value,
        warning_count=len(product_observation.parse_warnings),
        warnings="; ".join(product_observation.parse_warnings),
        observation_json_path=str(observation_path),
    )


def collect_one_chaparral_part(database_path: Path, page, planned, scan_run_id: int, settings: ProbeSettings) -> CollectionRow:
    support = manufacturer_support_metadata("chaparral", planned.manufacturer, planned.oem_part_number)
    if not support["manufacturer_supported"]:
        return manufacturer_not_carried_row(database_path, planned, scan_run_id, support)
    adapter = ChaparralAdapter()
    record = PartRecord(test_case_id="", manufacturer=planned.manufacturer, oem_part_number=planned.oem_part_number)
    requested_url = adapter.build_product_url(record)
    cached_url = _chaparral_cached_url(database_path, planned.manufacturer, planned.oem_part_number)
    status = None
    final_url = None
    html = ""
    text = ""
    exception_message = None
    checked_at = utc_now()
    cache_used = False
    try:
        if cached_url:
            response = page.goto(cached_url, wait_until="domcontentloaded", timeout=settings.timeout)
            status = response.status if response is not None else None
            page.wait_for_timeout(min(settings.render_settle_ms, COMPETITOR_RENDER_SETTLE_MS))
            final_url = page.url
            html = page.content()
            text = page.locator("body").inner_text(timeout=5000) if page.locator("body").count() else ""
            cache_observation = adapter.parse_product_page(html, record, visible_text=text, final_url=final_url, http_status=status)
            if _chaparral_observation_matches(cache_observation, planned.oem_part_number):
                cache_used = True
            else:
                _invalidate_chaparral_cache(database_path, planned.manufacturer, planned.oem_part_number)
                html = ""
                text = ""
                final_url = None
        if not cache_used:
            status, final_url, html, text = _run_chaparral_search_lookup(page, planned.oem_part_number, settings)
    except (PlaywrightTimeoutError, PlaywrightError, Exception) as exc:
        exception_message = str(exc)

    observation = adapter.parse_product_page(html, record, visible_text=text, final_url=final_url, http_status=status)
    if exception_message:
        observation.page_classification = "navigation_error"
        observation.warnings.append(exception_message)
        observation.raw_evidence_summary["lookup_status"] = "lookup_failed"

    cart_price_collected = False
    cleanup_status = "not_attempted"
    if observation.page_classification == "normal_product" and observation.price_visibility == "see_price_in_cart":
        cart_row = CartProbeInputRow(
            manufacturer=planned.manufacturer,
            oem_part_number=planned.oem_part_number,
            product_name=observation.product_name or "",
            product_url=observation.canonical_url or final_url or requested_url,
            reference_price=str(observation.reference_price or ""),
            prior_probe_note="chaparral_add_to_view_price",
        )
        if ensure_cart_empty(page):
            inventory = bounded_cart_action_inventory(page, cart_row, observation)
            action = select_high_confidence_cart_action(inventory["candidates"])
            observation.raw_evidence_summary["cart_action_selection"] = {
                "status": action.get("status"),
                "high_confidence_count": action.get("high_confidence_count"),
                "candidate": action.get("candidate"),
                "candidate_count": inventory.get("candidate_count"),
            }
            if action["status"] == "selected":
                form_validation = validate_cart_action_form(page, action["candidate"], row=cart_row, observation=observation)
                if form_validation["valid"]:
                    click_result = click_cart_action_with_result(page, action["candidate"], timeout_ms=5000)
                    observation.raw_evidence_summary["cart_action_click"] = click_result
                    if click_result["clicked"]:
                        wait_for_cart_response(page)
                        cart_text = open_cart_text(page)
                        line_evidence = cart_line_evidence(
                            cart_text,
                            cart_row,
                            supporting_sku=extract_tracking_label(action["candidate"]),
                            cart_line_records=collect_cart_line_records(page),
                        )
                        observation.raw_evidence_summary["cart_price_evidence"] = line_evidence
                        if line_evidence.get("rejected_placeholder_price_candidates"):
                            observation.warnings.append("cart_price_placeholder_ignored")
                        if line_evidence["confirmed"] and line_evidence["quantity"] == 1 and line_evidence["accepted_price"] is not None:
                            observation.selling_price = line_evidence["accepted_price"]
                            observation.price_visibility = "visible"
                            observation.price_display_type = "regular"
                            observation.selling_price_confidence = "medium"
                            observation.parse_confidence = "medium"
                            observation.raw_evidence_summary["price_source"] = "cart"
                            observation.raw_evidence_summary["lookup_status"] = "price_found"
                            cart_price_collected = True
                        elif line_evidence["confirmed"]:
                            observation.warnings.append("cart_price_not_found")
                            observation.raw_evidence_summary["lookup_status"] = "cart_price_not_found"
                        cleanup_status = "success" if remove_cart_item(page, line_evidence=line_evidence) and ensure_cart_empty(page) else "failed"
                        if cleanup_status != "success" and _reset_chaparral_cart_session(page, settings):
                            cleanup_status = "session_reset"
                            observation.warnings.append("cart_cleanup_session_reset")
                    else:
                        observation.warnings.append(str(click_result["reason"]))
                else:
                    observation.warnings.append("add_to_cart_form_validation_failed")
            else:
                observation.warnings.append("add_to_cart_button_not_found" if action["status"] == "not_found" else "ambiguous_cart_action")
        else:
            observation.warnings.append("cart_not_empty_before_chaparral_check")
    if cart_price_collected and cleanup_status not in {"success", "session_reset"}:
        observation.warnings.append("cart_cleanup_failed")
        observation.raw_evidence_summary["lookup_status"] = "cart_cleanup_failed"

    if _chaparral_observation_matches(observation, planned.oem_part_number) and observation.canonical_url and not _chaparral_is_search_url(observation.canonical_url):
        _save_chaparral_cache(database_path, planned.manufacturer, planned.oem_part_number, observation.canonical_url, observation.observed_part_number)

    product_observation = _product_observation_from_competitor(observation, requested_url=requested_url, checked_at=checked_at)
    output_dir = OUTPUT_DIR / "chaparral_collection_diagnostics" / f"{checked_at.replace(':','').replace('-','')}_{planned.oem_part_number}"
    output_dir.mkdir(parents=True, exist_ok=True)
    observation_path = output_dir / "observation.json"
    observation_path.write_text(json.dumps(observation.to_json_dict(), indent=2, default=str) + "\n", encoding="utf-8")
    previous_price = planned.current_price_cents
    previous_availability = None
    with connect_database(database_path) as conn:
        previous = conn.execute("SELECT * FROM current_listing_state WHERE listing_id=?", (planned.listing_id,)).fetchone()
        previous_price = previous["selling_price_cents"] if previous else None
        previous_availability = previous["availability_status"] if previous else None
        persisted_result = persist_observation(
            conn,
            scan_run_id=scan_run_id,
            listing_id=planned.listing_id,
            observation=product_observation,
            observation_json_path=str(observation_path),
            price_source_category="chaparral_cart" if cart_price_collected else str(observation.raw_evidence_summary.get("price_source") or "chaparral_lookup"),
        )
        scan_event_id = conn.execute("SELECT MAX(scan_event_id) FROM scan_events WHERE scan_run_id=? AND listing_id=?", (scan_run_id, planned.listing_id)).fetchone()[0]
    result_type = _chaparral_result_type(observation, status, persisted_result)
    if cart_price_collected and cleanup_status != "success":
        result_type = "cart_cleanup_failed"
    return CollectionRow(
        run_order=planned.run_order,
        scan_run_id=scan_run_id,
        scan_event_id=scan_event_id,
        manufacturer=planned.manufacturer,
        oem_part_number=planned.oem_part_number,
        normalized_manufacturer=normalize_manufacturer(planned.manufacturer),
        competitor="chaparral",
        manufacturer_supported=True,
        lookup_status=str(observation.raw_evidence_summary.get("lookup_status") or result_type),
        status_reason="; ".join(observation.warnings),
        observed_part_number=product_observation.observed_part_number,
        product_name=product_observation.product_name,
        checked_at=product_observation.checked_at,
        http_status=product_observation.http_status,
        page_classification=product_observation.page_classification.value,
        session_status=product_observation.session_status.value,
        selling_price=str(product_observation.selling_price) if product_observation.selling_price is not None else None,
        reference_price=str(product_observation.reference_price) if product_observation.reference_price is not None else None,
        savings_percent=product_observation.savings_percent,
        price_display_type=product_observation.price_display_type.value,
        previous_selling_price=cents_to_money(previous_price),
        result_type=result_type,
        price_changed=result_type in {"price_change", "multiple_changes"},
        availability_raw=product_observation.availability_raw,
        previous_availability_status=previous_availability,
        availability_status=product_observation.availability_status.value,
        supersession_detected=product_observation.supersession_detected,
        superseded_by_raw=product_observation.superseded_by_raw,
        price_source_category="chaparral_cart" if cart_price_collected else str(observation.raw_evidence_summary.get("price_source") or "chaparral_lookup"),
        price_corroboration_count=1 if product_observation.selling_price is not None else 0,
        price_parse_confidence=product_observation.price_parse_confidence.value,
        parse_confidence=product_observation.parse_confidence.value,
        warning_count=len(product_observation.parse_warnings),
        warnings="; ".join(product_observation.parse_warnings),
        observation_json_path=str(observation_path),
    )


def _run_chaparral_search_lookup(page, part_number: str, settings: ProbeSettings) -> tuple[int | None, str | None, str, str]:
    response = page.goto(build_search_url(part_number), wait_until="domcontentloaded", timeout=settings.timeout)
    status = response.status if response is not None else None
    page.wait_for_timeout(min(settings.render_settle_ms, CHAPARRAL_SEARCH_SETTLE_MS))
    _wait_for_chaparral_lookup_result(page, part_number, settings.timeout)
    page.wait_for_timeout(min(settings.render_settle_ms, CHAPARRAL_SEARCH_SETTLE_MS))
    html = page.content()
    text = page.locator("body").inner_text(timeout=5000) if page.locator("body").count() else ""
    return status, page.url, html, text


def _reset_chaparral_cart_session(page, settings: ProbeSettings) -> bool:
    try:
        page.context.clear_cookies()
    except Exception:
        pass
    try:
        page.evaluate("() => { localStorage.clear(); sessionStorage.clear(); }")
    except Exception:
        pass
    try:
        page.reload(wait_until="domcontentloaded", timeout=settings.timeout)
        page.wait_for_timeout(min(settings.render_settle_ms, COMPETITOR_RENDER_SETTLE_MS))
        return True
    except Exception:
        return False


def _chaparral_is_search_url(url: str) -> bool:
    return "chapmoto.com/search/" in url


def _wait_for_chaparral_lookup_result(page, part_number: str, timeout_ms: int) -> str:
    deadline = time.monotonic() + (timeout_ms / 1000)
    normalized = normalize_part_number_for_match(part_number)
    last_state = "unknown"
    while time.monotonic() < deadline:
        try:
            text = page.locator("body").inner_text(timeout=2000) if page.locator("body").count() else ""
        except Exception:
            text = ""
        if normalized and normalized in normalize_part_number_for_match(text):
            return "part_number_rendered"
        upper = text.upper()
        if "LOADING" not in upper and "0 % COMPLETE" not in upper and "0% COMPLETE" not in upper:
            return "loading_cleared"
        last_state = "loading"
        page.wait_for_timeout(CHAPARRAL_LOOKUP_POLL_MS)
    return f"timeout_{last_state}"


def _chaparral_observation_matches(observation, part_number: str) -> bool:
    return bool(observation.observed_part_number) and normalize_part_number_for_match(str(observation.observed_part_number)) == normalize_part_number_for_match(part_number)


def _chaparral_result_type(observation, http_status: int | None, persisted_result: str) -> str:
    lookup_status = str(observation.raw_evidence_summary.get("lookup_status") or "")
    if lookup_status in {"part_found_price_hidden", "msrp_only", "superseded", "discontinued", "out_of_stock", "available_to_order", "multiple_exact_matches", "cart_add_failed", "cart_price_not_found", "cart_cleanup_failed"}:
        return lookup_status
    if lookup_status in {"lookup_error", "lookup_failed"}:
        return lookup_status
    if lookup_status == "blocked_or_rate_limited" or observation.page_classification == "blocked" or http_status in {401, 403, 429}:
        return "blocked"
    if lookup_status == "captcha_detected" or observation.page_classification == "challenge":
        return "challenge"
    if lookup_status == "part_not_found" or observation.page_classification == "not_found":
        return "not_found"
    if observation.page_classification == "navigation_error":
        return "navigation_error"
    if observation.selling_price is None and observation.page_classification == "normal_product":
        return "no_price"
    return normalize_result_type(persisted_result)


def _chaparral_cached_url(database_path: Path, manufacturer: str, part_number: str) -> str | None:
    with connect_database(database_path) as conn:
        row = conn.execute(
            """
            SELECT resolved_url
            FROM chaparral_resolution_cache
            WHERE manufacturer=? AND normalized_part_number=? AND is_valid=1
            """,
            (normalize_manufacturer(manufacturer), normalize_part_number_for_match(part_number)),
        ).fetchone()
    return str(row["resolved_url"]) if row else None


def _save_chaparral_cache(database_path: Path, manufacturer: str, part_number: str, resolved_url: str, product_identifier: str | None) -> None:
    now = utc_now()
    with connect_database(database_path) as conn:
        conn.execute(
            """
            INSERT INTO chaparral_resolution_cache(manufacturer, part_number, normalized_part_number, resolved_url,
                product_identifier, resolved_at, last_verified_at, is_valid, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
            ON CONFLICT(manufacturer, normalized_part_number) DO UPDATE SET
                part_number=excluded.part_number,
                resolved_url=excluded.resolved_url,
                product_identifier=excluded.product_identifier,
                last_verified_at=excluded.last_verified_at,
                is_valid=1,
                updated_at=excluded.updated_at
            """,
            (normalize_manufacturer(manufacturer), part_number, normalize_part_number_for_match(part_number), resolved_url, product_identifier, now, now, now, now),
        )


def _invalidate_chaparral_cache(database_path: Path, manufacturer: str, part_number: str) -> None:
    with connect_database(database_path) as conn:
        conn.execute(
            """
            UPDATE chaparral_resolution_cache
            SET is_valid=0, updated_at=?
            WHERE manufacturer=? AND normalized_part_number=?
            """,
            (utc_now(), normalize_manufacturer(manufacturer), normalize_part_number_for_match(part_number)),
        )


def manufacturer_not_carried_row(database_path: Path, planned, scan_run_id: int, support: dict[str, object]) -> CollectionRow:
    checked_at = utc_now()
    reason = str(support["status_reason"])
    with connect_database(database_path) as conn:
        cur = conn.execute(
            """
            INSERT INTO scan_events(scan_run_id, listing_id, checked_at, http_status, page_classification, session_status,
                navigation_succeeded, price_found, price_parse_confidence, parse_warning_count, parse_warnings,
                observation_json_path, error_message)
            VALUES (?, ?, ?, NULL, 'manufacturer_not_carried', 'not_applicable', 0, 0, 'low', 0, ?, NULL, NULL)
            """,
            (scan_run_id, planned.listing_id, checked_at, reason),
        )
        scan_event_id = int(cur.lastrowid)
    return CollectionRow(
        run_order=planned.run_order,
        scan_run_id=scan_run_id,
        scan_event_id=scan_event_id,
        manufacturer=planned.manufacturer,
        normalized_manufacturer=str(support["normalized_manufacturer"]),
        competitor=str(support["competitor"]),
        manufacturer_supported=False,
        lookup_status="manufacturer_not_carried",
        status_reason=reason,
        oem_part_number=planned.oem_part_number,
        checked_at=checked_at,
        page_classification="manufacturer_not_carried",
        session_status="not_applicable",
        result_type="manufacturer_not_carried",
        warning_count=0,
        warnings="",
    )


def _product_observation_from_competitor(observation, *, requested_url: str, checked_at: str) -> ProductObservation:
    price_visibility = PriceVisibility.VISIBLE if observation.selling_price is not None else PriceVisibility.UNKNOWN
    if observation.price_visibility == "not_present":
        price_visibility = PriceVisibility.NOT_PRESENT
    return ProductObservation(
        test_case_id=None,
        manufacturer=observation.manufacturer,
        oem_part_number=observation.oem_part_number,
        observed_part_number=observation.observed_part_number,
        requested_url=requested_url,
        final_url=observation.canonical_url,
        canonical_url=observation.canonical_url,
        http_status=observation.http_status,
        page_title=None,
        page_classification=_page_classification(observation.page_classification),
        price_visibility=price_visibility,
        classification_confidence=_parse_confidence(observation.parse_confidence),
        classification_evidence=[],
        product_name=observation.product_name,
        manufacturer_display=observation.manufacturer,
        msrp_raw=None,
        msrp=observation.reference_price,
        selling_price_raw=str(observation.selling_price) if observation.selling_price is not None else None,
        selling_price=observation.selling_price,
        availability_raw=observation.availability_raw,
        availability_status=_availability_status(observation.availability_status),
        shipping_estimate=None,
        access_context=AccessContext.PUBLIC,
        session_status=_session_status(observation.session_status),
        superseded_by_raw=observation.superseded_by_raw,
        supersession_detected=observation.supersession_detected,
        price_parse_confidence=_parse_confidence(observation.selling_price_confidence),
        price_validation_status=PriceValidationStatus.NOT_MANUALLY_VALIDATED,
        parse_confidence=_parse_confidence(observation.parse_confidence),
        parse_warnings=list(observation.warnings),
        checked_at=checked_at,
        reference_price_raw=str(observation.reference_price) if observation.reference_price is not None else None,
        reference_price=observation.reference_price,
        savings_percent=observation.savings_percent,
        savings_amount=observation.savings_amount,
        price_display_type=_price_display_type(observation.price_display_type),
        selling_price_confidence=_parse_confidence(observation.selling_price_confidence),
        reference_price_confidence=_parse_confidence(observation.reference_price_confidence),
    )


def _page_classification(value: str) -> PageClassification:
    try:
        return PageClassification(value)
    except ValueError:
        return PageClassification.UNKNOWN


def _parse_confidence(value: str) -> ParseConfidence:
    try:
        return ParseConfidence(value)
    except ValueError:
        return ParseConfidence.LOW


def _availability_status(value: str) -> AvailabilityStatus:
    try:
        return AvailabilityStatus(value)
    except ValueError:
        return AvailabilityStatus.UNKNOWN


def _session_status(value: str) -> SessionStatus:
    if value == "public":
        return SessionStatus.UNKNOWN
    try:
        return SessionStatus(value)
    except ValueError:
        return SessionStatus.UNKNOWN


def _price_display_type(value: str) -> PriceDisplayType:
    try:
        return PriceDisplayType(value)
    except ValueError:
        return PriceDisplayType.UNKNOWN


def collection_result_type(observation, http_status: int | None, persisted_result: str) -> str:
    page_classification = observation.page_classification.value
    if page_classification == "blocked" or http_status in {401, 403, 429}:
        return "blocked"
    if page_classification == "challenge":
        return "challenge"
    if page_classification == "not_found":
        return "not_found"
    if page_classification == "navigation_error":
        return "navigation_error"
    if observation.session_status.value in {"expired_or_invalid", "authentication_required"}:
        return "authentication_lost"
    if observation.selling_price is None and page_classification == "normal_product":
        return "no_price"
    return normalize_result_type(persisted_result)


def _price_source_category(price_evidence):
    if price_evidence.selected_selling_price is None:
        return None
    for candidate in price_evidence.price_candidates:
        if candidate.candidate_role == PriceCandidateRole.SELLING_PRICE and candidate.normalized_value == price_evidence.selected_selling_price:
            return candidate.source_type.value
    return None


def _corroboration_count(price_evidence):
    if price_evidence.selected_selling_price is None:
        return 0
    for candidate in price_evidence.price_candidates:
        if candidate.candidate_role == PriceCandidateRole.SELLING_PRICE and candidate.normalized_value == price_evidence.selected_selling_price:
            return candidate.corroboration_count
    return 0


if __name__ == "__main__":
    sys.exit(main())
