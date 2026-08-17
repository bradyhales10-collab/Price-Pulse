from __future__ import annotations

import argparse
import json
import re
import sys
import time
import traceback
from collections.abc import Callable
from pathlib import Path

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

from app.atomic_write import replace_with_retry
from app.auth_session import (
    auth_state_path_for,
    mark_authenticated_context,
    require_competitor_auth_state,
    save_uploaded_auth_state,
    write_authenticated_observation,
    write_sanitized_authenticated_diagnostics,
)
from app.browser_probe import detect_page_signals
from app.browser_profile import (
    launch_persistent_competitor_context,
    primary_page,
    save_persistent_session,
)
from app.collection import (
    CollectionRow,
    CollectionRunResult,
    consecutive_error_limit,
    effective_delay_seconds,
    fingerprint_file,
    jittered_delay,
    normalize_result_type,
    plan_collection,
    print_plan,
    stop_status_for,
    validate_delay,
    write_collection_outputs,
)
from app.competitors.base import CompetitorObservation
from app.competitors.chaparral import ChaparralAdapter, build_search_url, normalize_part_number_for_match
from app.competitors.motosport import MotoSportAdapter
from app.competitors.registry import get_competitor, select_competitors
from app.config import (
    DEFAULT_DATABASE_PATH,
    DEFAULT_VIEWPORT,
    OUTPUT_DIR,
    ProbeSettings,
    ensure_data_directories,
)
from app.database import (
    cents_to_money,
    complete_scan_run,
    connect_database,
    create_scan_run,
    initialize_database,
    persist_observation,
    seed_competitor,
    upsert_competitor_listing,
    utc_now,
)
from app.input_loader import load_parts_csv
from app.manufacturer_registry import (
    competitor_supports_manufacturer,
    manufacturer_support_metadata,
    normalize_manufacturer,
)
from app.models import PartRecord
from app.parsers.partzilla_product_parser import build_parse_input_from_probe, parse_partzilla_product_page
from app.price_forensics import (
    PriceCandidateRole,
    apply_price_evidence_to_observation,
    build_price_evidence,
    write_price_evidence,
)
from app.raw_price_signals import discover_raw_price_signals, write_raw_price_signals
from app.resolution_cache import cached_product_url, invalidate_product_url, save_product_url
from app.schemas.product_observation import (
    AccessContext,
    AvailabilityStatus,
    PageClassification,
    ParseConfidence,
    PriceDisplayType,
    PriceValidationStatus,
    PriceVisibility,
    ProductObservation,
    SessionStatus,
)
from app.url_builder import build_partzilla_product_url
from export_current_prices import export_current_prices
from export_price_changes import export_price_changes
from probe_cart_price import (
    clear_whole_cart,
    CartProbeInputRow,
    bounded_cart_action_inventory,
    cart_line_evidence,
    click_cart_action_with_result,
    collect_cart_line_records,
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
# Reading a page's text failed with 'Timeout 5000ms exceeded' on a slow page,
# which counted as an operational error. Five seconds is tight for a page that
# has loaded but is still settling. This was briefly raised to 15 seconds,
# which was too far: combined with a run no longer stopping after two errors,
# every slow page stalled three times as long and those stalls repeated
# instead of ending the run, making a large run feel unusable. Eight seconds
# covers a settling page without making a bad patch grind.
BODY_TEXT_TIMEOUT_MS = 8000

# Playwright reports this when the browser it was driving no longer exists,
# whether it crashed, ran out of memory, or was closed by hand. Nothing after
# that point can succeed: every remaining part fails identically. It is not a
# page error and must not be counted as one, because doing so wastes parts
# producing meaningless failures and then blames the pages.
BROWSER_GONE_MARKERS = (
    "target page, context or browser has been closed",
    "browser has been closed",
    "target closed",
    "connection closed",
    "browser closed",
)


def browser_is_gone(message: str | None) -> bool:
    text = str(message or "").lower()
    return any(marker in text for marker in BROWSER_GONE_MARKERS)
PARTZILLA_RENDER_SETTLE_MS = 1000
PARTZILLA_PRICE_POLL_MS = 250
PARTZILLA_PRICE_POLL_ATTEMPTS = 16
CHAPARRAL_SEARCH_SETTLE_MS = 500
CHAPARRAL_LOOKUP_POLL_MS = 250
MOTOSPORT_NAVIGATION_TIMEOUT_MS = 15000
MOTOSPORT_CART_CLICK_TIMEOUT_MS = 3000


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


def _search_based_collector(competitor_key: str) -> Callable[..., CollectionRow]:
    def collect(database_path, page, planned, scan_run_id, settings, delay_seconds: int = 3):
        return collect_one_search_based_part(
            database_path,
            page,
            planned,
            scan_run_id,
            settings,
            adapter=get_competitor(competitor_key),
            delay_seconds=delay_seconds,
        )

    return collect


PRODUCTION_COLLECTORS: dict[str, Callable[..., CollectionRow]] = {
    "partzilla": lambda *a, **k: collect_one_part(*a),
    "motosport": lambda *a, **k: collect_one_motosport_part(*a),
    # Chaparral keeps its own collector because it also has to add an item to the
    # cart to reveal some prices, which the generic path does not do.
    "chaparral": lambda *a, **k: collect_one_chaparral_part(*a),
    "revzilla": _search_based_collector("revzilla"),
}


def assert_production_collector_exists(competitor_key: str) -> None:
    """Refuse a production run for a competitor with no collector of its own.

    Without this, an unrecognised competitor fell through to the Partzilla
    collector, which builds partzilla.com URLs. The run would have scraped the
    wrong site and stored the results under the new competitor's name.
    """
    if competitor_key not in PRODUCTION_COLLECTORS:
        raise ValueError(
            f"{competitor_key} has no production collector yet, so it cannot be used for a real "
            f"price check. Use probe_competitor.py for it instead."
        )


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
            browser = None
            if get_competitor(competitor_key).requires_login:
                # A saved sign-in that is missing or unreadable fails here,
                # before any part is attempted. Record why, because this used to
                # surface as a run that simply reported completed with no rows.
                state_path = auth_state_path_for(competitor_key)
                try:
                    context = launch_persistent_competitor_context(
                        playwright,
                        competitor_key,
                        headless=settings.headless,
                    )
                except Exception as exc:
                    # An already-saved file can contain a cookie with no value,
                    # which Playwright refuses outright. Rewriting it through the
                    # validator drops those and keeps the rest, so an existing
                    # sign-in is repaired rather than needing to be redone.
                    try:
                        repaired = save_uploaded_auth_state(
                            competitor_key, auth_state_path_for(competitor_key).read_bytes()
                        )
                        context = launch_persistent_competitor_context(
                            playwright,
                            competitor_key,
                            headless=settings.headless,
                        )
                        print(f"Repaired the saved {competitor_key} sign-in and continued.")
                    except Exception:
                        result.run_status = "failed"
                        result.stop_reason = "saved_sign_in_unusable"
                        detail = f"Could not start the browser with the saved {competitor_key} sign-in: {exc}"
                        print(detail)
                        crash_dir = OUTPUT_DIR / "collection_crashes"
                        crash_dir.mkdir(parents=True, exist_ok=True)
                        (crash_dir / f"{competitor_key}-setup.txt").write_text(
                            f"{detail}\n\nsign-in file: {state_path}\n"
                            f"exists: {state_path.exists()}\n\n{traceback.format_exc()}",
                            encoding="utf-8",
                        )
                        raise
            else:
                browser = playwright.chromium.launch(headless=settings.headless)
                context = browser.new_context(viewport=DEFAULT_VIEWPORT)
            if args.collection_mode == "lightweight_browser":
                context.route("**/*", lambda route: route.abort() if route.request.resource_type in HEAVY_RESOURCE_TYPES else route.continue_())
            page = primary_page(context) if get_competitor(competitor_key).requires_login else context.new_page()
            page.set_default_timeout(settings.timeout)
            page.set_default_navigation_timeout(settings.timeout)
            assert_production_collector_exists(competitor_key)
            run_delay_seconds = effective_delay_seconds(args.delay_seconds, len(plan.planned_parts))
            error_limit = consecutive_error_limit(len(plan.planned_parts))
            if run_delay_seconds != args.delay_seconds:
                print(
                    f"Using a {run_delay_seconds}s gap between parts instead of "
                    f"{args.delay_seconds}s, because this run covers {len(plan.planned_parts)} parts."
                )
            for planned in plan.planned_parts:
                try:
                    if result.rows and competitor_supports_manufacturer(competitor_key, planned.manufacturer):
                        time.sleep(jittered_delay(run_delay_seconds))
                    try:
                        collector = PRODUCTION_COLLECTORS[competitor_key]
                        row = collector(
                            args.database, page, planned, scan_run_id, settings,
                            delay_seconds=run_delay_seconds,
                        )
                    except Exception as exc:
                        # str(exc) alone loses the traceback, which is what
                        # identifies the file and line that actually raised, and
                        # therefore which copy of the code was running. Write it
                        # out so a failure can be traced rather than guessed at.
                        try:
                            crash_dir = OUTPUT_DIR / "collection_crashes"
                            crash_dir.mkdir(parents=True, exist_ok=True)
                            crash_path = crash_dir / f"{competitor_key}-{planned.oem_part_number}.txt".replace("/", "_")
                            crash_path.write_text(
                                f"competitor: {competitor_key}\n"
                                f"part: {planned.oem_part_number}\n"
                                f"collect_parts.py: {Path(__file__).resolve()}\n"
                                f"python: {sys.executable}\n"
                                f"working directory: {Path.cwd()}\n\n"
                                + traceback.format_exc(),
                                encoding="utf-8",
                            )
                            print(f"  Full error details written to: {crash_path}")
                        except Exception:
                            pass
                        row = collection_error_row(args.database, planned, scan_run_id, competitor_key, exc)
                    result.rows.append(row)
                    result.last_attempted_part = planned.oem_part_number
                    print(f"[{planned.run_order}/{len(plan.planned_parts)}] {planned.oem_part_number} | {row.selling_price or ''} | {row.result_type.upper()}")
                    _write_progress(args, result, plan, status="running", started_monotonic=started_monotonic)
                    stop_status = stop_status_for(row)
                    if stop_status:
                        result.run_status = stop_status
                        result.stop_reason = row.result_type
                        break
                    if browser_is_gone(row.status_reason):
                        # Unrecoverable: the browser is gone, so continuing only
                        # produces identical failures on every remaining part.
                        result.run_status = "failed"
                        result.stop_reason = (
                            f"the browser closed unexpectedly at part {len(result.rows)} of "
                            f"{len(plan.planned_parts)}, so the run could not continue"
                        )
                        print(f"  The browser closed unexpectedly. Stopping after {len(result.rows)} parts.")
                        break
                    if row.result_type in {"navigation_error", "error"}:
                        consecutive_errors += 1
                        if consecutive_errors >= error_limit:
                            result.run_status = "failed"
                            # Spelled out because the previous wording,
                            # "two consecutive operational errors", stated a
                            # fixed number that no longer matched the actual
                            # limit and did not say how far the run had got.
                            result.stop_reason = (
                                f"stopped after {consecutive_errors} page errors in a row "
                                f"at part {len(result.rows)} of {len(plan.planned_parts)}"
                            )
                            break
                    else:
                        consecutive_errors = 0
                except Exception as exc:
                    # Everything above this point, other than the collector call
                    # itself, was unprotected: a failure in recording progress, in
                    # the stop-condition check, or anywhere else in this loop
                    # would end the run silently. A real run stopped at 95 of 994
                    # parts with no stop_reason recorded anywhere and no crash
                    # file, which can only mean something escaped from exactly
                    # this gap. This is the backstop: whatever it turns out to be,
                    # it is now written down with a full traceback instead of
                    # disappearing.
                    try:
                        crash_dir = OUTPUT_DIR / "collection_crashes"
                        crash_dir.mkdir(parents=True, exist_ok=True)
                        crash_path = crash_dir / f"{competitor_key}-loop-{planned.oem_part_number}.txt".replace("/", "_")
                        crash_path.write_text(
                            f"competitor: {competitor_key}\n"
                            f"part: {planned.oem_part_number}\n"
                            f"parts completed before this: {len(result.rows)} of {len(plan.planned_parts)}\n"
                            f"collect_parts.py: {Path(__file__).resolve()}\n\n"
                            + traceback.format_exc(),
                            encoding="utf-8",
                        )
                        print(f"  Full error details written to: {crash_path}")
                    except Exception:
                        pass
                    result.run_status = "failed"
                    result.stop_reason = f"unexpected_error_in_collection_loop: {type(exc).__name__}"
                    break
            if get_competitor(competitor_key).requires_login and result.stop_reason != "authentication_lost":
                # Keep renewed cookies and local storage from successful runs.
                # Sites often rotate session cookies while pages are checked;
                # discarding those rotations made a saved login expire sooner
                # than the browser session the user had just established.
                try:
                    save_persistent_session(context, competitor_key)
                except Exception as exc:
                    print(f"Could not refresh the saved {competitor_key} sign-in: {exc}")
            context.close()
            if browser is not None:
                browser.close()
    finally:
        result.completed_at = utc_now()
        with connect_database(args.database) as conn:
            complete_scan_run(conn, scan_run_id)
            if result.run_status != "running":
                conn.execute("UPDATE scan_runs SET run_status=? WHERE scan_run_id=?", (result.run_status, scan_run_id))
            else:
                database_status = conn.execute("SELECT run_status FROM scan_runs WHERE scan_run_id=?", (scan_run_id,)).fetchone()[0]
                result.run_status = _normalized_completed_run_status(
                    database_status,
                    completed=len(result.rows),
                    total=len(plan.planned_parts),
                )
                if result.run_status != database_status:
                    conn.execute("UPDATE scan_runs SET run_status=? WHERE scan_run_id=?", (result.run_status, scan_run_id))
        try:
            export_current_prices(args.database)
            export_price_changes(args.database)
        except Exception as exc:
            result.export_warning = str(exc)
        write_collection_outputs(result=result, plan=plan, delay_seconds=args.delay_seconds, input_fingerprint=fingerprint_file(args.file))
        _write_progress(args, result, plan, status=result.run_status, started_monotonic=started_monotonic)
    return 0


def _normalized_completed_run_status(database_status: str, *, completed: int, total: int) -> str:
    if database_status == "failed" and total > 0 and completed == total:
        return "completed_with_warnings"
    # Reaching this branch at all means result.run_status was still "running"
    # when the run ended: nothing in the loop ever recorded an explicit reason
    # to stop. That can only happen if something escaped the per-part error
    # handling and ended the run outside the normal loop. Attempting fewer
    # than the full plan is therefore never a normal outcome here, whether
    # that is 0 parts or most of them: a run of 994 that stops at 95 with no
    # recorded reason is exactly as wrong as one that never starts, and
    # reporting it as "completed with warnings" hid that something crashed.
    if total > 0 and completed < total and database_status not in {"failed", "stopped_blocked", "stopped_challenge"}:
        return "failed"
    return database_status


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
    replace_with_retry(tmp, path)


def _wait_for_partzilla_product_price(page, initial_settle_ms: int) -> str:
    page.wait_for_timeout(initial_settle_ms)
    region_text = _partzilla_product_region_text(page)
    for _ in range(PARTZILLA_PRICE_POLL_ATTEMPTS):
        if _partzilla_product_price_text(page):
            region_text = _partzilla_product_region_text(page)
            break
        page.wait_for_timeout(PARTZILLA_PRICE_POLL_MS)
        region_text = _partzilla_product_region_text(page)
    return region_text


def _partzilla_product_region_text(page) -> str:
    try:
        heading = page.locator('[data-testid="productHeadingWrapper"]')
        if heading.count() != 1:
            return ""
        return heading.locator("xpath=ancestor::main[1]").inner_text(timeout=2000)
    except (PlaywrightTimeoutError, PlaywrightError, Exception):
        return ""


def _partzilla_product_price_text(page) -> str:
    return _partzilla_product_field_text(page, "productPrice", r"\$[\d,]+(?:\.\d{2})?")


def _partzilla_product_reference_price_text(page) -> str:
    return _partzilla_product_field_text(page, "productPriceValue", r"\$[\d,]+(?:\.\d{2})?")


def _partzilla_product_savings_text(page) -> str:
    return _partzilla_product_field_text(page, "productSavePercent", r"SAVE\s+\d{1,3}%")


def _partzilla_product_field_text(page, testid: str, pattern: str) -> str:
    try:
        heading = page.locator('[data-testid="productHeadingWrapper"]')
        if heading.count() != 1:
            return ""
        field = heading.locator("..").locator(f'[data-testid="{testid}"]')
        if field.count() != 1 or not field.is_visible():
            return ""
        text = field.inner_text(timeout=2000).strip()
        return text if re.fullmatch(pattern, text, re.IGNORECASE) else ""
    except (PlaywrightTimeoutError, PlaywrightError, Exception):
        return ""


def _has_partzilla_purchase_price(region_text: str) -> bool:
    relevant_lines = [
        line.strip()
        for line in region_text.splitlines()
        if line.strip()
        and "msrp" not in line.lower()
        and "free shipping" not in line.lower()
        and "orders over" not in line.lower()
    ]
    relevant_text = "\n".join(relevant_lines)
    return re.search(r"\$[\d,]+(?:\.\d{2})?", relevant_text) is not None


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
    product_region_text = ""
    visible_selling_price_raw = ""
    visible_reference_price_raw = ""
    visible_savings_text = ""
    navigation_succeeded = False
    exception_message = None
    checked_at = utc_now()
    try:
        response = page.goto(requested_url, wait_until="domcontentloaded", timeout=settings.timeout)
        navigation_succeeded = True
        status = response.status if response is not None else None
        product_region_text = _wait_for_partzilla_product_price(
            page,
            min(settings.render_settle_ms, PARTZILLA_RENDER_SETTLE_MS),
        )
        visible_selling_price_raw = _partzilla_product_price_text(page)
        visible_reference_price_raw = _partzilla_product_reference_price_text(page)
        visible_savings_text = _partzilla_product_savings_text(page)
        final_url = page.url
        title = page.title()
        html = page.content()
        body_text = page.locator("body").inner_text(timeout=BODY_TEXT_TIMEOUT_MS) if page.locator("body").count() else ""
        text = product_region_text or body_text
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
    price_evidence = build_price_evidence(
        html=html,
        visible_text=text,
        observation=observation,
        raw_price_signals=raw_signals,
        verified_visible_selling_price_raw=visible_selling_price_raw or None,
        verified_visible_reference_price_raw=visible_reference_price_raw or None,
        verified_visible_savings_text=visible_savings_text or None,
    )
    apply_price_evidence_to_observation(observation, price_evidence)
    from app.config import AUTHENTICATED_DIAGNOSTICS_DIR
    output_dir = AUTHENTICATED_DIAGNOSTICS_DIR / f"{checked_at.replace(':','').replace('-','')}_{planned.oem_part_number}"
    observation_path = output_dir / "observation.json"
    write_authenticated_observation(observation_path, observation)
    write_sanitized_authenticated_diagnostics(output_dir / "sanitized_diagnostics.txt", observation, exception_message=exception_message)
    write_price_evidence(output_dir / "price_evidence.json", price_evidence)
    write_raw_price_signals(output_dir / "raw_price_signals.json", observation=observation, signals=raw_signals)
    if observation.session_status.value in {"expired_or_invalid", "authentication_required"}:
        return authentication_required_row_from_observation(
            database_path,
            planned,
            scan_run_id,
            observation,
            observation_path,
        )
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


def _cleanup_added_cart_item(
    page,
    row: CartProbeInputRow,
    *,
    supporting_sku: str,
    initial_evidence: dict[str, object] | None = None,
) -> str:
    evidence = initial_evidence or {}
    for _attempt in range(2):
        if not evidence.get("confirmed"):
            try:
                evidence = cart_line_evidence(
                    open_cart_text(page),
                    row,
                    supporting_sku=supporting_sku,
                    cart_line_records=collect_cart_line_records(page),
                )
            except Exception:
                evidence = {}
        if evidence.get("confirmed") and remove_cart_item(page, line_evidence=evidence) and ensure_cart_empty(page):
            return "success"
        evidence = {}
    # Removing the specific line did not work. Clearing the cart outright is the
    # last chance to leave it empty, because anything left behind is inherited by
    # every part after this one.
    if clear_whole_cart(page)["cleared"]:
        return "success_after_full_clear"
    return "failed"


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
        response = page.goto(
            requested_url,
            wait_until="domcontentloaded",
            timeout=min(settings.timeout, MOTOSPORT_NAVIGATION_TIMEOUT_MS),
        )
        status = response.status if response is not None else None
    except PlaywrightTimeoutError as exc:
        # MotoSport can keep loading marketing assets after the product panel is usable.
        # Parse the rendered DOM instead of discarding the part after the bounded wait.
        exception_message = str(exc)
    except (PlaywrightError, Exception) as exc:
        exception_message = str(exc)
    try:
        page.wait_for_timeout(min(settings.render_settle_ms, COMPETITOR_RENDER_SETTLE_MS))
        final_url = page.url
        html = page.content()
        text = page.locator("body").inner_text(timeout=BODY_TEXT_TIMEOUT_MS) if page.locator("body").count() else ""
    except (PlaywrightTimeoutError, PlaywrightError, Exception) as exc:
        exception_message = exception_message or str(exc)

    observation = adapter.parse_product_page(html, record, visible_text=text, final_url=final_url, http_status=status)
    rendered_product = observation.page_classification in {"normal_product", "not_found", "superseded"}
    if exception_message and not rendered_product:
        observation.page_classification = "navigation_error"
        observation.warnings.append(exception_message)
    elif exception_message:
        observation.warnings.append("navigation_timeout_after_product_rendered")

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
                click_result = click_cart_action_with_result(
                    page,
                    action["candidate"],
                    timeout_ms=MOTOSPORT_CART_CLICK_TIMEOUT_MS,
                )
                if click_result["clicked"]:
                    supporting_sku = extract_tracking_label(action["candidate"])
                    line_evidence: dict[str, object] = {}
                    try:
                        wait_for_cart_response(page)
                        cart_text = open_cart_text(page)
                        line_evidence = cart_line_evidence(
                            cart_text,
                            cart_row,
                            supporting_sku=supporting_sku,
                            cart_line_records=collect_cart_line_records(page),
                        )
                        observation.raw_evidence_summary["cart_price_evidence"] = line_evidence
                        if line_evidence["confirmed"] and line_evidence["quantity"] == 1 and line_evidence["accepted_price"] is not None:
                            observation.selling_price = line_evidence["accepted_price"]
                            observation.price_visibility = "visible"
                            observation.price_display_type = "discounted" if observation.reference_price and observation.reference_price > observation.selling_price else "regular"
                            observation.selling_price_confidence = "medium"
                            observation.parse_confidence = "medium"
                            observation.warnings = [warning for warning in observation.warnings if warning != "selling_price_hidden_in_cart"]
                            cart_price_collected = True
                    except Exception as exc:
                        observation.warnings.append(f"cart_price_read_failed: {exc}")
                    finally:
                        cleanup_status = _cleanup_added_cart_item(
                            page,
                            cart_row,
                            supporting_sku=supporting_sku,
                            initial_evidence=line_evidence,
                        )
                else:
                    observation.warnings.append(str(click_result["reason"]))
            elif not form_validation["valid"]:
                observation.warnings.append("add_to_cart_form_validation_failed")
            else:
                # The cart already had something in it. Emptying it and carrying
                # on is far better than skipping: this part needs the cart to
                # read a price at all, and refusing meant a single earlier
                # cleanup failure silently cost every later part its price.
                observation.warnings.append("cart_not_empty_before_add")
                clear_result = clear_whole_cart(page)
                if clear_result["cleared"]:
                    observation.warnings.append(f"cart_cleared_before_add: removed {clear_result['removed']}")
                else:
                    observation.warnings.append(f"cart_clear_failed: {clear_result['reason']}")
        else:
            observation.warnings.append("add_to_cart_button_not_found" if action["status"] == "not_found" else "ambiguous_cart_action")

    if cleanup_status == "failed":
        observation.warnings.append("cart_cleanup_failed")
    elif cleanup_status == "success_after_full_clear":
        observation.warnings.append("cart_cleared_by_emptying_whole_cart")
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
    if cleanup_status == "failed":
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
            text = page.locator("body").inner_text(timeout=BODY_TEXT_TIMEOUT_MS) if page.locator("body").count() else ""
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
                        supporting_sku = extract_tracking_label(action["candidate"])
                        line_evidence: dict[str, object] = {}
                        try:
                            wait_for_cart_response(page)
                            cart_text = open_cart_text(page)
                            line_evidence = cart_line_evidence(
                                cart_text,
                                cart_row,
                                supporting_sku=supporting_sku,
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
                        except Exception as exc:
                            observation.warnings.append(f"cart_price_read_failed: {exc}")
                        finally:
                            cleanup_status = _cleanup_added_cart_item(
                                page,
                                cart_row,
                                supporting_sku=supporting_sku,
                                initial_evidence=line_evidence,
                            )
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
    if cleanup_status == "failed":
        observation.warnings.append("cart_cleanup_failed")
    elif cleanup_status == "success_after_full_clear":
        observation.warnings.append("cart_cleared_by_emptying_whole_cart")
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
    if cleanup_status == "failed":
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
    text = page.locator("body").inner_text(timeout=BODY_TEXT_TIMEOUT_MS) if page.locator("body").count() else ""
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


def collection_error_row(database_path: Path, planned, scan_run_id: int, competitor_key: str, exc: Exception) -> CollectionRow:
    checked_at = utc_now()
    reason = f"{type(exc).__name__}: {exc}"
    with connect_database(database_path) as conn:
        cur = conn.execute(
            """
            INSERT INTO scan_events(scan_run_id, listing_id, checked_at, http_status, page_classification, session_status,
                navigation_succeeded, price_found, price_parse_confidence, parse_warning_count, parse_warnings,
                observation_json_path, error_message)
            VALUES (?, ?, ?, NULL, 'navigation_error', 'unknown', 0, 0, 'low', 1, ?, NULL, ?)
            """,
            (scan_run_id, planned.listing_id, checked_at, reason, reason),
        )
        scan_event_id = int(cur.lastrowid)
    return CollectionRow(
        run_order=planned.run_order,
        scan_run_id=scan_run_id,
        scan_event_id=scan_event_id,
        manufacturer=planned.manufacturer,
        normalized_manufacturer=normalize_manufacturer(planned.manufacturer),
        competitor=competitor_key,
        manufacturer_supported=competitor_supports_manufacturer(competitor_key, planned.manufacturer),
        lookup_status="collection_error",
        status_reason=reason,
        oem_part_number=planned.oem_part_number,
        checked_at=checked_at,
        page_classification="navigation_error",
        session_status="unknown",
        result_type="error",
        warning_count=1,
        warnings=reason,
    )


def authentication_required_row_from_observation(database_path: Path, planned, scan_run_id: int, observation, observation_path: Path) -> CollectionRow:
    checked_at = observation.checked_at or utc_now()
    reason = "Saved Partzilla login is expired or missing. Refresh the desktop login session before using Partzilla prices."
    with connect_database(database_path) as conn:
        cur = conn.execute(
            """
            INSERT INTO scan_events(scan_run_id, listing_id, checked_at, http_status, page_classification, session_status,
                navigation_succeeded, price_found, price_parse_confidence, parse_warning_count, parse_warnings,
                observation_json_path, error_message)
            VALUES (?, ?, ?, ?, ?, ?, 1, 0, ?, 1, ?, ?, ?)
            """,
            (
                scan_run_id,
                planned.listing_id,
                checked_at,
                observation.http_status,
                observation.page_classification.value,
                observation.session_status.value,
                observation.price_parse_confidence.value,
                reason,
                str(observation_path),
                reason,
            ),
        )
        scan_event_id = int(cur.lastrowid)
    return CollectionRow(
        run_order=planned.run_order,
        scan_run_id=scan_run_id,
        scan_event_id=scan_event_id,
        manufacturer=planned.manufacturer,
        normalized_manufacturer=normalize_manufacturer(planned.manufacturer),
        competitor="partzilla",
        manufacturer_supported=True,
        lookup_status="authentication_lost",
        status_reason=reason,
        oem_part_number=planned.oem_part_number,
        observed_part_number=observation.observed_part_number,
        product_name=observation.product_name,
        checked_at=checked_at,
        http_status=observation.http_status,
        page_classification=observation.page_classification.value,
        session_status=observation.session_status.value,
        result_type="authentication_lost",
        price_parse_confidence=observation.price_parse_confidence.value,
        parse_confidence=observation.parse_confidence.value,
        warning_count=1,
        warnings=reason,
        observation_json_path=str(observation_path),
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


def _observation_matches_part(observation, part_number: str) -> bool:
    """Whether the page we landed on really is the part we asked for."""
    observed = normalize_part_number_for_match(observation.observed_part_number or "")
    return bool(observed) and observed == normalize_part_number_for_match(part_number)


def _open_search_based_product_page(page, adapter, record, planned, settings, delay_seconds: int):
    """Search for a part, then open the matching result.

    Returns the status, url, html and text of whichever page we ended on, plus
    the resolved product URL when one was followed.
    """
    search_url = adapter.build_product_url(record)
    response = page.goto(search_url, wait_until="domcontentloaded", timeout=settings.timeout)
    status = response.status if response is not None else None
    page.wait_for_timeout(min(settings.render_settle_ms, COMPETITOR_RENDER_SETTLE_MS))
    final_url = page.url
    html = page.content()
    text = page.locator("body").inner_text(timeout=BODY_TEXT_TIMEOUT_MS) if page.locator("body").count() else ""

    if status in {401, 403, 429}:
        return status, final_url, html, text, None

    resolver = getattr(adapter, "search_result_product_url", None)
    if resolver is None:
        return status, final_url, html, text, None

    product_url = resolver(html, record)
    if not product_url or product_url == final_url:
        return status, final_url, html, text, None

    # This is the second request for one part, so the gap applies here too.
    time.sleep(jittered_delay(delay_seconds))
    response = page.goto(product_url, wait_until="domcontentloaded", timeout=settings.timeout)
    status = response.status if response is not None else status
    page.wait_for_timeout(min(settings.render_settle_ms, COMPETITOR_RENDER_SETTLE_MS))
    final_url = page.url
    html = page.content()
    text = page.locator("body").inner_text(timeout=BODY_TEXT_TIMEOUT_MS) if page.locator("body").count() else ""
    return status, final_url, html, text, product_url


def collect_one_search_based_part(
    database_path: Path,
    page,
    planned,
    scan_run_id: int,
    settings: ProbeSettings,
    *,
    adapter,
    delay_seconds: int = 3,
) -> CollectionRow:
    """Collect one part from a competitor that has to be searched.

    A cached product URL is tried first, which turns the usual two requests back
    into one. If the cached page no longer shows the requested part, the cache
    entry is dropped and the search runs again.
    """
    competitor_key = adapter.competitor_key
    support = manufacturer_support_metadata(competitor_key, planned.manufacturer, planned.oem_part_number)
    if not support["manufacturer_supported"]:
        return manufacturer_not_carried_row(database_path, planned, scan_run_id, support)

    record = PartRecord(test_case_id="", manufacturer=planned.manufacturer, oem_part_number=planned.oem_part_number)
    requested_url = adapter.build_product_url(record)
    status = None
    final_url = None
    html = ""
    text = ""
    exception_message = None
    checked_at = utc_now()
    resolved_url = None
    used_cache = False
    observation = None

    cached_url = cached_product_url(database_path, competitor_key, planned.manufacturer, planned.oem_part_number)
    try:
        if cached_url:
            response = page.goto(cached_url, wait_until="domcontentloaded", timeout=settings.timeout)
            status = response.status if response is not None else None
            page.wait_for_timeout(min(settings.render_settle_ms, COMPETITOR_RENDER_SETTLE_MS))
            final_url = page.url
            html = page.content()
            text = page.locator("body").inner_text(timeout=BODY_TEXT_TIMEOUT_MS) if page.locator("body").count() else ""
            cached_observation = adapter.parse_product_page(
                html, record, visible_text=text, final_url=final_url, http_status=status
            )
            if _observation_matches_part(cached_observation, planned.oem_part_number):
                used_cache = True
                resolved_url = cached_url
            else:
                # The competitor moved or replaced the page, so search again.
                invalidate_product_url(database_path, competitor_key, planned.manufacturer, planned.oem_part_number)
                status, final_url, html, text = None, None, "", ""
        if not used_cache:
            status, final_url, html, text, resolved_url = _open_search_based_product_page(
                page, adapter, record, planned, settings, delay_seconds
            )

        observation = adapter.parse_product_page(
            html, record, visible_text=text, final_url=final_url, http_status=status
        )

        # Only remember a URL that actually showed the requested part.
        if resolved_url and _observation_matches_part(observation, planned.oem_part_number):
            save_product_url(
                database_path,
                competitor_key,
                planned.manufacturer,
                planned.oem_part_number,
                resolved_url,
                observation.observed_part_number,
            )
    except (PlaywrightTimeoutError, PlaywrightError, Exception) as exc:
        # Anything unexpected here, including a page structure this parser has
        # not seen before, becomes a clear recorded row rather than an
        # unhandled exception. Two consecutive unhandled exceptions stop the
        # whole run; a page this parser could not read should not do that on
        # its own, since the next part is very likely fine.
        exception_message = str(exc)
        observation = None

    if observation is None:
        # Built directly rather than calling the parser again: if it just
        # raised once, calling it a second time is not guaranteed to succeed.
        observation = CompetitorObservation(
            competitor_key=competitor_key,
            manufacturer=normalize_manufacturer(planned.manufacturer),
            oem_part_number=planned.oem_part_number,
        )
    if exception_message:
        observation.page_classification = "navigation_error"
        observation.warnings.append(exception_message)
        observation.raw_evidence_summary["lookup_status"] = "lookup_failed"

    product_observation = _product_observation_from_competitor(
        observation, requested_url=requested_url, checked_at=checked_at
    )
    output_dir = (
        OUTPUT_DIR
        / f"{competitor_key}_collection_diagnostics"
        / f"{checked_at.replace(':','').replace('-','')}_{planned.oem_part_number}"
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    observation_path = output_dir / "observation.json"
    observation_path.write_text(
        json.dumps(observation.to_json_dict(), indent=2, default=str) + "\n", encoding="utf-8"
    )

    price_source = f"{competitor_key}_cached_url" if used_cache else f"{competitor_key}_search"
    with connect_database(database_path) as conn:
        previous = conn.execute(
            "SELECT * FROM current_listing_state WHERE listing_id=?", (planned.listing_id,)
        ).fetchone()
        previous_price = previous["selling_price_cents"] if previous else None
        previous_availability = previous["availability_status"] if previous else None
        persisted_result = persist_observation(
            conn,
            scan_run_id=scan_run_id,
            listing_id=planned.listing_id,
            observation=product_observation,
            observation_json_path=str(observation_path),
            price_source_category=price_source,
        )
        scan_event_id = conn.execute(
            "SELECT MAX(scan_event_id) FROM scan_events WHERE scan_run_id=? AND listing_id=?",
            (scan_run_id, planned.listing_id),
        ).fetchone()[0]

    result_type = collection_result_type(product_observation, status, persisted_result)
    return CollectionRow(
        run_order=planned.run_order,
        scan_run_id=scan_run_id,
        scan_event_id=scan_event_id,
        manufacturer=planned.manufacturer,
        oem_part_number=planned.oem_part_number,
        normalized_manufacturer=normalize_manufacturer(planned.manufacturer),
        competitor=competitor_key,
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
        price_source_category=price_source,
        price_corroboration_count=1 if product_observation.selling_price is not None else 0,
        price_parse_confidence=product_observation.price_parse_confidence.value,
        parse_confidence=product_observation.parse_confidence.value,
        warning_count=len(product_observation.parse_warnings),
        warnings="; ".join(product_observation.parse_warnings),
        observation_json_path=str(observation_path),
    )


if __name__ == "__main__":
    sys.exit(main())
