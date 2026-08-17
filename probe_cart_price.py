from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import time
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

from app.competitors.motosport import MotoSportAdapter
from app.config import DATA_DIR, DEFAULT_VIEWPORT, ProbeSettings, ensure_data_directories
from app.database import utc_now
from app.manufacturer_registry import manufacturer_support_metadata, normalize_manufacturer
from app.models import PartRecord
from app.parsers.money_parser import parse_money

CONFIRMATION_TEXT = "RUN CART PRICE PROBE"
HARD_CART_PROBE_MAX_PARTS = 5
STOP_STATUSES = {401, 403, 429}
STOP_CLASSIFICATIONS = {"blocked", "challenge"}
CART_ACTION_TEXTS = (
    "add to cart",
    "see price in cart",
    "price in cart",
    "view price in cart",
    "add to cart to see price",
    "add to cart for price",
    "add for price",
    "add to basket",
    "add item",
    "buy now",
)
CART_PRICE_PLACEHOLDERS = {Decimal("9999.99")}
CART_ACTION_RESCAN_DELAY_MS = 750
CART_NETWORK_IDLE_CAP_MS = 1500
CART_RESPONSE_SETTLE_MS = 400
CART_CLEANUP_SETTLE_MS = 500
NEVER_CLICK_TEXTS = (
    "checkout",
    "proceed to checkout",
    "pay",
    "payment",
    "place order",
    "submit order",
    "review order",
    "express checkout",
    "paypal",
    "shop pay",
    "apple pay",
    "google pay",
    "klarna",
)
REQUIRED_INPUT_FIELDS = {
    "manufacturer",
    "oem_part_number",
    "product_name",
    "product_url",
    "reference_price",
}
OUTPUT_FIELDS = [
    "run_order",
    "manufacturer",
    "normalized_manufacturer",
    "competitor",
    "manufacturer_supported",
    "lookup_status",
    "status_reason",
    "oem_part_number",
    "product_url",
    "checked_at",
    "product_association_confirmed",
    "reference_price",
    "cart_selling_price",
    "quantity",
    "line_subtotal",
    "cart_price_confidence",
    "cleanup_status",
    "result_type",
    "warnings",
]


@dataclass(frozen=True)
class CartProbeLimits:
    max_run_seconds: int = 120
    max_part_seconds: int = 60
    max_candidate_scans: int = 3
    max_cart_action_attempts: int = 1
    max_cart_navigation_wait_seconds: int = 15
    max_cleanup_attempts: int = 1
    max_empty_cart_checks: int = 2


@dataclass(frozen=True)
class CartProbeInputRow:
    manufacturer: str
    oem_part_number: str
    product_name: str
    product_url: str
    reference_price: str
    prior_probe_timestamp: str = ""
    prior_probe_note: str = ""


@dataclass
class CartProbeResult:
    run_order: int
    row: CartProbeInputRow
    checked_at: str
    product_association_confirmed: bool = False
    reference_price: Decimal | None = None
    cart_selling_price: Decimal | None = None
    quantity: int | None = None
    line_subtotal: Decimal | None = None
    cart_price_confidence: str = "low"
    cleanup_status: str = "not_attempted"
    result_type: str = "error"
    warnings: list[str] = field(default_factory=list)
    raw_result: dict[str, object] = field(default_factory=dict)


@dataclass
class CartProbeRun:
    competitor_key: str
    started_at: str
    completed_at: str | None = None
    rows: list[CartProbeResult] = field(default_factory=list)
    stopped: bool = False
    stop_reason: str | None = None
    blocked: int = 0
    challenges: int = 0
    errors: int = 0


@dataclass
class CartProbeRunContext:
    run_id: str
    run_output_dir: Path
    started_at: str
    started_monotonic: float
    limits: CartProbeLimits
    max_total_output_directories_created: int
    directories_created_count: int = 1
    attempted_count: int = 0
    stop_requested: bool = False
    stop_reason: str | None = None
    product_dirs: dict[int, Path] = field(default_factory=dict)
    product_dir_names: dict[str, int] = field(default_factory=dict)
    candidate_scan_count: int = 0
    cart_action_attempt_count: int = 0
    cleanup_attempt_count: int = 0
    empty_cart_check_count: int = 0
    current_part_started_monotonic: float | None = None

    @classmethod
    def create(
        cls,
        *,
        base_data_dir: Path,
        attempted_parts: int,
        limits: CartProbeLimits | None = None,
        input_file: Path | None = None,
        requested_max_parts: int | None = None,
        mode: str = "unknown",
    ) -> CartProbeRunContext:
        run_id = utc_now().replace(":", "").replace("-", "")
        run_output_dir = base_data_dir / "output" / "competitor_probes" / "motosport_cart" / run_id
        run_output_dir.mkdir(parents=True, exist_ok=False)
        context = cls(
            run_id=run_id,
            run_output_dir=run_output_dir,
            started_at=utc_now(),
            started_monotonic=time.monotonic(),
            limits=limits or CartProbeLimits(),
            max_total_output_directories_created=attempted_parts + 1,
        )
        write_startup_metadata(
            context,
            input_file=input_file,
            requested_max_parts=requested_max_parts,
            mode=mode,
            status="initialized",
        )
        return context

    def begin_part(self, row: CartProbeInputRow, run_order: int) -> Path:
        self.attempted_count += 1
        self.current_part_started_monotonic = time.monotonic()
        self.candidate_scan_count = 0
        self.cart_action_attempt_count = 0
        self.cleanup_attempt_count = 0
        self.empty_cart_check_count = 0
        return self.product_output_dir(row, run_order)

    def product_output_dir(self, row: CartProbeInputRow, run_order: int) -> Path:
        if run_order in self.product_dirs:
            return self.product_dirs[run_order]
        base_name = _safe_name(row.oem_part_number)
        count = self.product_dir_names.get(base_name, 0) + 1
        self.product_dir_names[base_name] = count
        folder_name = base_name if count == 1 else f"{base_name}-{count}"
        if self.directories_created_count + 1 > self.max_total_output_directories_created:
            self.request_stop("loop_guard_triggered", "output_directory_limit_exceeded")
            raise RuntimeError("output_directory_limit_exceeded")
        product_dir = self.run_output_dir / folder_name
        product_dir.mkdir(parents=True, exist_ok=True)
        self.directories_created_count += 1
        self.product_dirs[run_order] = product_dir
        return product_dir

    def request_stop(self, stop_reason: str, warning: str | None = None) -> None:
        self.stop_requested = True
        self.stop_reason = stop_reason if not warning else f"{stop_reason}: {warning}"

    def guard_run_time(self) -> bool:
        if time.monotonic() - self.started_monotonic > self.limits.max_run_seconds:
            self.request_stop("timeout_guard_triggered", "max_run_seconds_exceeded")
            return False
        return True

    def guard_part_time(self) -> bool:
        if self.current_part_started_monotonic is None:
            return True
        if time.monotonic() - self.current_part_started_monotonic > self.limits.max_part_seconds:
            self.request_stop("timeout_guard_triggered", "max_part_seconds_exceeded")
            return False
        return True

    def allow_candidate_scan(self) -> bool:
        if self.candidate_scan_count >= self.limits.max_candidate_scans:
            self.request_stop("loop_guard_triggered", "max_candidate_scans_exceeded")
            return False
        self.candidate_scan_count += 1
        return True

    def allow_cart_action_attempt(self) -> bool:
        if self.cart_action_attempt_count >= self.limits.max_cart_action_attempts:
            self.request_stop("loop_guard_triggered", "max_cart_action_attempts_exceeded")
            return False
        self.cart_action_attempt_count += 1
        return True

    def allow_cleanup_attempt(self) -> bool:
        if self.cleanup_attempt_count >= self.limits.max_cleanup_attempts:
            self.request_stop("loop_guard_triggered", "max_cleanup_attempts_exceeded")
            return False
        self.cleanup_attempt_count += 1
        return True

    def allow_empty_cart_check(self) -> bool:
        if self.empty_cart_check_count >= self.limits.max_empty_cart_checks:
            self.request_stop("loop_guard_triggered", "max_empty_cart_checks_exceeded")
            return False
        self.empty_cart_check_count += 1
        return True


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Experimental, manually gated MotoSport cart-price probe.")
    parser.add_argument("--competitor", required=True)
    parser.add_argument("--file", type=Path, required=True)
    parser.add_argument("--max-parts", type=int, default=HARD_CART_PROBE_MAX_PARTS)
    parser.add_argument("--delay-seconds", type=int, default=5)
    parser.add_argument("--experimental-cart-pricing", action="store_true")
    parser.add_argument("--diagnose-cart-action-only", action="store_true")
    parser.add_argument("--dry-run-structure", action="store_true")
    parser.add_argument("--headless", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        validate_args(args)
    except ValueError as exc:
        print(f"Error: {exc}")
        return 1
    try:
        rows = load_cart_probe_rows(args.file)[: args.max_parts]
    except ValueError as exc:
        print(f"Error: {exc}")
        return 1
    if not rows:
        print("Error: no cart-hidden probe rows found.")
        return 1
    ensure_data_directories()
    mode = cart_probe_mode(args)
    if not args.dry_run_structure and not args.diagnose_cart_action_only and latest_folder_audit_failed(DATA_DIR):
        print("Error: latest MotoSport cart probe folder audit failed. Run dry-run structure, diagnostic-only, and inspect outputs before a real cart click.")
        return 1
    if mode == "real_cart_probe":
        confirmation = input(f"Type {CONFIRMATION_TEXT} to continue: ").strip()
        if confirmation != CONFIRMATION_TEXT:
            print("Cart price probe was not confirmed. Exiting without opening a browser.")
            return 1
    context = CartProbeRunContext.create(
        base_data_dir=DATA_DIR,
        attempted_parts=len(rows),
        input_file=args.file,
        requested_max_parts=args.max_parts,
        mode=mode,
    )
    try:
        if mode == "dry_run_structure":
            run = run_cart_probe_structure_dry_run(rows, context)
        elif mode == "diagnostic_only":
            run = run_cart_action_diagnostics(rows, context=context, headless=bool(args.headless))
        else:
            run = run_cart_probe(rows, context=context, headless=bool(args.headless), delay_seconds=args.delay_seconds)
    except KeyboardInterrupt:
        run = CartProbeRun(competitor_key="motosport", started_at=context.started_at, completed_at=utc_now(), stopped=True, stop_reason="interrupted_by_user", errors=1)
        context.request_stop("interrupted_by_user")
    write_outputs(context.run_output_dir, run, args, context=context)
    print(f"Cart probe output: {context.run_output_dir}")
    return 0 if not run.errors and not run.stopped else 1


def validate_args(args: argparse.Namespace) -> None:
    if args.competitor != "motosport":
        raise ValueError("Cart price probe currently supports MotoSport only.")
    if not args.experimental_cart_pricing:
        raise ValueError("Pass --experimental-cart-pricing to enable this experimental command.")
    if args.max_parts < 1:
        raise ValueError("--max-parts must be at least 1.")
    if args.max_parts > HARD_CART_PROBE_MAX_PARTS:
        raise ValueError(f"--max-parts must be {HARD_CART_PROBE_MAX_PARTS} or fewer for cart-price probes.")
    if args.delay_seconds < 5:
        raise ValueError("--delay-seconds must be at least 5.")


def cart_probe_mode(args: argparse.Namespace) -> str:
    if getattr(args, "dry_run_structure", False):
        return "dry_run_structure"
    if getattr(args, "diagnose_cart_action_only", False):
        return "diagnostic_only"
    return "real_cart_probe"


def load_cart_probe_rows(path: Path) -> list[CartProbeInputRow]:
    if not path.exists():
        raise ValueError(f"Input CSV not found: {path}")
    rows: list[CartProbeInputRow] = []
    with path.open("r", newline="", encoding="utf-8-sig") as file:
        reader = csv.DictReader(file)
        missing = REQUIRED_INPUT_FIELDS.difference(reader.fieldnames or [])
        if missing:
            raise ValueError(f"Input CSV is missing required field(s): {', '.join(sorted(missing))}")
        if "prior_probe_timestamp" not in (reader.fieldnames or []) and "prior_probe_note" not in (reader.fieldnames or []):
            raise ValueError("Input CSV must include prior_probe_timestamp or prior_probe_note.")
        for raw in reader:
            row = {key: (value or "").strip() for key, value in raw.items()}
            if not _row_is_cart_hidden(row):
                raise ValueError(f"Row for {row.get('oem_part_number', '')} was not exported as see_price_in_cart.")
            rows.append(
                CartProbeInputRow(
                    manufacturer=row["manufacturer"],
                    oem_part_number=row["oem_part_number"],
                    product_name=row["product_name"],
                    product_url=row["product_url"],
                    reference_price=row["reference_price"],
                    prior_probe_timestamp=row.get("prior_probe_timestamp", ""),
                    prior_probe_note=row.get("prior_probe_note", ""),
                )
            )
    return rows


def run_cart_probe_structure_dry_run(rows: list[CartProbeInputRow], context: CartProbeRunContext) -> CartProbeRun:
    run = CartProbeRun(competitor_key="motosport", started_at=context.started_at)
    for index, row in enumerate(rows, start=1):
        if not context.guard_run_time():
            run.stopped = True
            run.stop_reason = context.stop_reason
            break
        context.begin_part(row, index)
        run.rows.append(CartProbeResult(run_order=index, row=row, checked_at=utc_now(), result_type="dry_run_structure", warnings=["dry_run_structure_no_browser_opened"]))
    run.completed_at = utc_now()
    return run


def run_cart_action_diagnostics(rows: list[CartProbeInputRow], *, context: CartProbeRunContext, headless: bool) -> CartProbeRun:
    settings = ProbeSettings(headless=headless, timeout=30000)
    run = CartProbeRun(competitor_key="motosport", started_at=context.started_at)
    adapter = MotoSportAdapter()
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=settings.headless)
        browser_context = browser.new_context(viewport=DEFAULT_VIEWPORT)
        page = browser_context.new_page()
        page.set_default_timeout(settings.timeout)
        page.set_default_navigation_timeout(settings.timeout)
        try:
            for index, row in enumerate(rows, start=1):
                if not context.guard_run_time():
                    run.stopped = True
                    run.stop_reason = context.stop_reason
                    break
                support = manufacturer_support_metadata("motosport", row.manufacturer, row.oem_part_number)
                if not support["manufacturer_supported"]:
                    run.rows.append(CartProbeResult(run_order=index, row=row, checked_at=utc_now(), result_type="manufacturer_not_carried", warnings=[str(support["status_reason"])]))
                    continue
                product_output_dir = context.begin_part(row, index)
                write_cart_probe_progress(product_output_dir, row, step="diagnostic_part_started", status="started")
                result = diagnose_one_cart_action(page, adapter, row, index, context=context, product_output_dir=product_output_dir)
                run.rows.append(result)
                if context.stop_requested:
                    run.stopped = True
                    run.stop_reason = context.stop_reason
                    run.errors += 1
                    break
        finally:
            browser_context.close()
            browser.close()
    run.completed_at = utc_now()
    return run


def run_cart_probe(rows: list[CartProbeInputRow], *, context: CartProbeRunContext, headless: bool, delay_seconds: int) -> CartProbeRun:
    settings = ProbeSettings(headless=headless, timeout=30000)
    run = CartProbeRun(competitor_key="motosport", started_at=context.started_at)
    adapter = MotoSportAdapter()
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=settings.headless)
        browser_context = browser.new_context(viewport=DEFAULT_VIEWPORT)
        page = browser_context.new_page()
        page.set_default_timeout(settings.timeout)
        page.set_default_navigation_timeout(settings.timeout)
        try:
            if not ensure_cart_empty(page, context=context):
                run.stopped = True
                run.stop_reason = "Cart was not empty or could not be verified empty before starting."
                return run
            for index, row in enumerate(rows, start=1):
                if not context.guard_run_time():
                    run.stopped = True
                    run.stop_reason = context.stop_reason
                    break
                if index > 1:
                    time.sleep(delay_seconds)
                support = manufacturer_support_metadata("motosport", row.manufacturer, row.oem_part_number)
                if not support["manufacturer_supported"]:
                    run.rows.append(CartProbeResult(run_order=index, row=row, checked_at=utc_now(), result_type="manufacturer_not_carried", warnings=[str(support["status_reason"])]))
                    continue
                product_output_dir = context.begin_part(row, index)
                write_cart_probe_progress(product_output_dir, row, step="real_cart_part_started", status="started")
                result = probe_one_cart_price(page, adapter, row, index, context=context, product_output_dir=product_output_dir)
                run.rows.append(result)
                if result.result_type in {"blocked", "challenge", "cleanup_failed", "cart_not_empty", "error", "ambiguous_cart_action"}:
                    run.stopped = True
                    run.stop_reason = context.stop_reason or "; ".join(result.warnings) or result.result_type
                    if result.result_type == "blocked":
                        run.blocked += 1
                    elif result.result_type == "challenge":
                        run.challenges += 1
                    else:
                        run.errors += 1
                    break
        finally:
            try:
                ensure_cart_empty(page)
            finally:
                browser_context.close()
                browser.close()
    run.completed_at = utc_now()
    return run


def diagnose_one_cart_action(page, adapter: MotoSportAdapter, row: CartProbeInputRow, run_order: int, *, context: CartProbeRunContext, product_output_dir: Path) -> CartProbeResult:
    result = CartProbeResult(run_order=run_order, row=row, checked_at=utc_now(), result_type="cart_action_diagnostics")
    result.reference_price = parse_money(row.reference_price).value
    try:
        if not context.guard_part_time():
            result.result_type = "error"
            result.warnings.append(context.stop_reason or "timeout_guard_triggered")
            return result
        write_cart_probe_progress(product_output_dir, row, step="loading_product_page", status="started")
        response = page.goto(row.product_url, wait_until="domcontentloaded", timeout=live_step_timeout_ms(context))
        write_cart_probe_progress(product_output_dir, row, step="loading_product_page", status="completed")
        status = response.status if response is not None else None
        page.wait_for_timeout(2000)
        body_text = page.locator("body").inner_text(timeout=5000) if page.locator("body").count() else ""
        observation = adapter.parse_product_page(page.content(), PartRecord("", row.manufacturer, row.oem_part_number), visible_text=body_text, final_url=page.url, http_status=status)
        result.product_association_confirmed = bool((observation.raw_evidence_summary.get("product_association") or {}).get("confirmed"))
        inventory = bounded_cart_action_inventory(page, row, observation, context=context)
        result.raw_result = {"product_observation": observation.to_json_dict(), "cart_action_inventory": inventory}
        save_cart_action_diagnostics(product_output_dir, row, observation, inventory)
        if status in STOP_STATUSES:
            result.result_type = "blocked"
            result.warnings.append(f"stopped_on_http_{status}")
        elif observation.page_classification in STOP_CLASSIFICATIONS:
            result.result_type = observation.page_classification
            result.warnings.append(f"stopped_on_{observation.page_classification}")
        elif not result.product_association_confirmed or observation.price_visibility != "see_price_in_cart":
            result.result_type = "not_cart_hidden"
            result.warnings.append("product_not_confirmed_as_cart_hidden")
        if context.stop_requested:
            result.result_type = "error"
            result.warnings.append(context.stop_reason or "loop_guard_triggered")
        return result
    except (PlaywrightTimeoutError, PlaywrightError, Exception) as exc:
        result.result_type = "error"
        result.warnings.append(f"error: {exc}")
        write_cart_probe_progress(product_output_dir, row, step="diagnostic_error", status="failed", details={"error": str(exc)})
        return result


def probe_one_cart_price(page, adapter: MotoSportAdapter, row: CartProbeInputRow, run_order: int, *, context: CartProbeRunContext, product_output_dir: Path) -> CartProbeResult:
    result = CartProbeResult(run_order=run_order, row=row, checked_at=utc_now())
    result.reference_price = parse_money(row.reference_price).value
    try:
        if not context.guard_part_time():
            result.result_type = "error"
            result.warnings.append(context.stop_reason or "timeout_guard_triggered")
            return result
        write_cart_probe_progress(product_output_dir, row, step="loading_product_page", status="started")
        response = page.goto(row.product_url, wait_until="domcontentloaded", timeout=live_step_timeout_ms(context))
        write_cart_probe_progress(product_output_dir, row, step="loading_product_page", status="completed")
        status = response.status if response is not None else None
        page.wait_for_timeout(2000)
        body_text = page.locator("body").inner_text(timeout=5000) if page.locator("body").count() else ""
        observation = adapter.parse_product_page(page.content(), PartRecord("", row.manufacturer, row.oem_part_number), visible_text=body_text, final_url=page.url, http_status=status)
        result.product_association_confirmed = bool((observation.raw_evidence_summary.get("product_association") or {}).get("confirmed"))
        result.raw_result["product_observation"] = observation.to_json_dict()
        if status in STOP_STATUSES:
            result.result_type = "blocked"
            result.warnings.append(f"stopped_on_http_{status}")
            return result
        if observation.page_classification in STOP_CLASSIFICATIONS:
            result.result_type = observation.page_classification
            result.warnings.append(f"stopped_on_{observation.page_classification}")
            return result
        if not result.product_association_confirmed or observation.price_visibility != "see_price_in_cart":
            result.result_type = "not_cart_hidden"
            result.warnings.append("product_not_confirmed_as_cart_hidden")
            return result
        inventory = bounded_cart_action_inventory(page, row, observation, context=context)
        result.raw_result["cart_action_inventory"] = inventory
        action = select_high_confidence_cart_action(inventory["candidates"])
        if action["status"] != "selected":
            result.result_type = "ambiguous_cart_action" if action["status"] == "ambiguous" else "error"
            result.warnings.append("ambiguous_cart_action" if action["status"] == "ambiguous" else "add_to_cart_button_not_found")
            save_cart_action_diagnostics(product_output_dir, row, observation, inventory)
            return result
        form_validation = validate_cart_action_form(page, action["candidate"], row=row, observation=observation)
        result.raw_result["cart_action_form_validation"] = form_validation
        if not form_validation["valid"]:
            result.result_type = "error"
            result.warnings.append("add_to_cart_form_validation_failed")
            save_cart_action_diagnostics(product_output_dir, row, observation, inventory)
            save_real_cart_probe_diagnostics(product_output_dir, row, action["candidate"], form_validation=form_validation)
            return result
        if not ensure_cart_empty(page, context=context):
            result.result_type = "cart_not_empty"
            result.warnings.append("cart_not_empty_before_add")
            return result
        if not context.allow_cart_action_attempt():
            result.result_type = "error"
            result.warnings.append(context.stop_reason or "loop_guard_triggered")
            return result
        write_cart_probe_progress(product_output_dir, row, step="clicking_add_to_cart", status="started")
        click_result = click_cart_action_with_result(page, action["candidate"], timeout_ms=live_click_timeout_ms(context))
        result.raw_result["cart_action_click"] = click_result
        if not click_result["clicked"]:
            result.result_type = "error"
            result.warnings.append(str(click_result["reason"]))
            save_cart_action_diagnostics(product_output_dir, row, observation, inventory)
            save_real_cart_probe_diagnostics(product_output_dir, row, action["candidate"], form_validation=form_validation)
            save_cart_click_failure_evidence(product_output_dir, row, page, click_result=click_result)
            write_cart_probe_progress(product_output_dir, row, step="clicking_add_to_cart", status="failed", details=click_result)
            return result
        write_cart_probe_progress(product_output_dir, row, step="clicking_add_to_cart", status="completed", details=click_result)
        save_real_cart_probe_diagnostics(product_output_dir, row, action["candidate"], form_validation=form_validation)
        write_cart_probe_progress(product_output_dir, row, step="waiting_for_cart_response", status="started")
        wait_for_cart_response(page, context=context)
        write_cart_probe_progress(product_output_dir, row, step="waiting_for_cart_response", status="completed")
        cart_text = open_cart_text(page)
        result.raw_result["cart_text_excerpt"] = cart_text[:2000]
        supporting_sku = extract_tracking_label(action["candidate"])
        cart_line_records = collect_cart_line_records(page)
        (product_output_dir / "cart_line_records.json").write_text(
            json.dumps(cart_line_records, indent=2, default=str) + "\n",
            encoding="utf-8",
        )
        line_evidence = cart_line_evidence(cart_text, row, supporting_sku=supporting_sku, cart_line_records=cart_line_records)
        result.raw_result["cart_line_evidence"] = line_evidence
        save_cart_line_evidence(product_output_dir, row, line_evidence)
        if not line_evidence["confirmed"]:
            result.result_type = "error"
            result.warnings.append("cart_line_not_confirmed")
            result.cleanup_status = "not_attempted"
            return result
        if line_evidence["quantity"] != 1:
            result.result_type = "error"
            result.warnings.append("unexpected_cart_quantity")
            save_cart_price_evidence(product_output_dir, row, line_evidence)
            if remove_cart_item(page, context=context, line_evidence=line_evidence):
                result.cleanup_status = "success" if ensure_cart_empty(page, context=context) else "failed"
            else:
                result.cleanup_status = "failed"
            save_cleanup_evidence(product_output_dir, row, cleanup_status=result.cleanup_status, page=page)
            return result
        else:
            result.cart_selling_price = line_evidence["accepted_price"]
            result.quantity = line_evidence["quantity"]
            result.line_subtotal = result.cart_selling_price if result.quantity == 1 else None
            result.cart_price_confidence = "medium" if result.cart_selling_price is not None and result.quantity == 1 else "low"
            result.result_type = "cart_price_found" if result.cart_selling_price is not None else "cart_price_not_found"
        save_cart_price_evidence(product_output_dir, row, line_evidence)
        if not remove_cart_item(page, context=context, line_evidence=line_evidence):
            result.cleanup_status = "failed"
            result.result_type = "cleanup_failed"
            result.warnings.append("cart_cleanup_failed")
            save_cleanup_evidence(product_output_dir, row, cleanup_status="failed", page=page)
            return result
        result.cleanup_status = "success" if ensure_cart_empty(page, context=context) else "failed"
        save_cleanup_evidence(product_output_dir, row, cleanup_status=result.cleanup_status, page=page)
        if result.cleanup_status != "success":
            result.result_type = "cleanup_failed"
            result.warnings.append("cart_not_empty_after_cleanup")
        return result
    except (PlaywrightTimeoutError, PlaywrightError, Exception) as exc:
        result.result_type = "error"
        result.warnings.append(f"error: {exc}")
        write_cart_probe_progress(product_output_dir, row, step="real_cart_error", status="failed", details={"error": str(exc)})
        return result


def ensure_cart_empty(page, *, context: CartProbeRunContext | None = None) -> bool:
    # Best-effort safety check. If the cart cannot be verified empty, the run stops.
    if context is not None and not context.allow_empty_cart_check():
        return False
    try:
        text = page.locator("body").inner_text(timeout=2000) if page.locator("body").count() else ""
        if _cart_empty_text(text):
            return True
        if _cart_has_unrelated_items(text):
            return False
        return True
    except Exception:
        return False


def bounded_cart_action_inventory(page, row: CartProbeInputRow, observation, *, context: CartProbeRunContext | None = None) -> dict[str, object]:
    last_inventory: dict[str, object] = {"candidates": [], "candidate_count": 0, "high_confidence_count": 0, "scan_count": 0}
    max_scans = context.limits.max_candidate_scans if context is not None else 3
    for scan_number in range(1, max_scans + 1):
        if context is not None:
            if not context.guard_run_time() or not context.guard_part_time() or not context.allow_candidate_scan():
                last_inventory["scan_count"] = scan_number - 1
                return last_inventory
        inventory = collect_cart_action_inventory(page, row, observation)
        inventory["scan_count"] = scan_number
        last_inventory = inventory
        high = [candidate for candidate in inventory["candidates"] if candidate["confidence"] == "high" and not candidate.get("rejected")]
        if len(high) == 1:
            return inventory
        if scan_number < max_scans:
            page.wait_for_timeout(CART_ACTION_RESCAN_DELAY_MS)
    return last_inventory


def collect_cart_action_inventory(page, row: CartProbeInputRow, observation) -> dict[str, object]:
    raw_controls = collect_visible_controls(page)
    product_region = product_region_text(observation)
    candidates = [score_cart_action_candidate(control, row=row, product_region=product_region) for control in raw_controls]
    return {
        "candidate_count": len(candidates),
        "high_confidence_count": sum(1 for candidate in candidates if candidate["confidence"] == "high"),
        "candidates": candidates,
        "visible_buttons": [candidate for candidate in candidates if candidate["tag_name"] in {"button", "input"}],
        "visible_links": [candidate for candidate in candidates if candidate["tag_name"] == "a"],
        "product_panel_forms": collect_product_panel_forms(page),
    }


def collect_visible_controls(page) -> list[dict[str, object]]:
    try:
        return page.evaluate(
            """
            () => Array.from(document.querySelectorAll('button,a,[role="button"],input[type="button"],input[type="submit"],input[value],input#addtocartbutton,input#add-to-cart')).map((el, index) => {
              const rect = el.getBoundingClientRect();
              const style = window.getComputedStyle(el);
              const form = el.closest('form');
              const text = (el.innerText || el.value || el.textContent || '').trim();
              const stableSelector = el.id ? `#${CSS.escape(el.id)}` : (
                el.getAttribute('data-testid') ? `[data-testid="${CSS.escape(el.getAttribute('data-testid'))}"]` : ''
              );
                const dataTracking = el.getAttribute('data-tracking') || '';
                return {
                index,
                selector_hint: stableSelector || `${el.tagName.toLowerCase()}:nth-of-type(${index + 1})`,
                stable_selector: stableSelector,
                tag_name: el.tagName.toLowerCase(),
                input_type: el.getAttribute('type') || '',
                input_value: el.value || '',
                name: el.getAttribute('name') || '',
                role: el.getAttribute('role') || '',
                text,
                link_text: el.tagName.toLowerCase() === 'a' ? text : '',
                aria_label: el.getAttribute('aria-label') || '',
                title: el.getAttribute('title') || '',
                data_testid: el.getAttribute('data-testid') || '',
                data_test: el.getAttribute('data-test') || '',
                data_cy: el.getAttribute('data-cy') || '',
                data_tracking: dataTracking,
                data_tracking_category: (() => { try { return JSON.parse(dataTracking).category || ''; } catch { return ''; } })(),
                data_tracking_label: (() => { try { return JSON.parse(dataTracking).label || ''; } catch { return ''; } })(),
                data_sku: el.getAttribute('data-sku') || '',
                data_quantity: el.getAttribute('data-quantity') || '',
                id: el.id || '',
                class_summary: String(el.className || '').split(/\\s+/).slice(0, 6).join(' '),
                form_id: form ? (form.id || '') : '',
                form_action: form ? (form.getAttribute('action') || form.action || '') : '',
                form_method: form ? (form.getAttribute('method') || form.method || '') : '',
                disabled: Boolean(el.disabled || el.getAttribute('aria-disabled') === 'true'),
                visible: Boolean(rect.width && rect.height && style.visibility !== 'hidden' && style.display !== 'none'),
                enabled: !Boolean(el.disabled || el.getAttribute('aria-disabled') === 'true'),
                bounding_box: {x: rect.x, y: rect.y, width: rect.width, height: rect.height}
              };
            })
            """
        )
    except Exception:
        return []


def collect_product_panel_forms(page) -> list[dict[str, object]]:
    try:
        return page.evaluate(
            """
            () => Array.from(document.querySelectorAll('form')).slice(0, 10).map((form, index) => {
              const safeValue = (el) => {
                const key = `${el.id || ''} ${el.getAttribute('name') || ''}`.toLowerCase();
                if (key.includes('token') || key.includes('csrf') || key.includes('auth') || key.includes('session')) {
                  return '[redacted]';
                }
                return el.value || '';
              };
              const controls = Array.from(form.querySelectorAll('button,input,a,[role="button"]')).slice(0, 20).map((el) => ({
                tag_name: el.tagName.toLowerCase(),
                id: el.id || '',
                name: el.getAttribute('name') || '',
                type: el.getAttribute('type') || '',
                value: safeValue(el),
                text: (el.innerText || safeValue(el) || el.textContent || '').trim(),
                visible: Boolean(el.getBoundingClientRect().width && el.getBoundingClientRect().height)
              }));
              const quantities = Array.from(form.querySelectorAll('input[name*="qty" i],input[name*="quantity" i],input[id*="qty" i],input[id*="quantity" i]')).map((el) => ({
                id: el.id || '',
                name: el.getAttribute('name') || '',
                value: el.value || ''
              }));
              return {
                index,
                id: form.id || '',
                class_summary: String(form.className || '').split(/\\s+/).slice(0, 6).join(' '),
                text_excerpt: (form.innerText || '').trim().slice(0, 500),
                action: form.getAttribute('action') || form.action || '',
                method: form.getAttribute('method') || form.method || '',
                controls,
                quantities
              };
            })
            """
        )
    except Exception:
        return []


def score_cart_action_candidate(control: dict[str, object], *, row: CartProbeInputRow, product_region: str) -> dict[str, object]:
    text_bits = [
        str(control.get("text") or ""),
        str(control.get("link_text") or ""),
        str(control.get("aria_label") or ""),
        str(control.get("title") or ""),
        str(control.get("data_testid") or ""),
        str(control.get("data_test") or ""),
        str(control.get("data_cy") or ""),
        str(control.get("data_tracking_category") or ""),
        str(control.get("id") or ""),
        str(control.get("input_value") or ""),
    ]
    joined = " ".join(text_bits).strip()
    normalized = joined.lower()
    rejected = any(phrase in normalized for phrase in NEVER_CLICK_TEXTS)
    cart_related = any(phrase in normalized for phrase in CART_ACTION_TEXTS)
    outside_product_panel = not _control_associated_with_product(control, row, product_region)
    visible = bool(control.get("visible"))
    enabled = bool(control.get("enabled")) and not bool(control.get("disabled"))
    confidence = "low"
    reasons: list[str] = []
    if rejected:
        reasons.append("rejected_checkout_or_payment_control")
    if not cart_related:
        reasons.append("not_cart_related_text")
    if outside_product_panel:
        reasons.append("outside_product_panel")
    if not visible:
        reasons.append("not_visible")
    if not enabled:
        reasons.append("not_enabled")
    if cart_related and not rejected and visible and enabled and not outside_product_panel:
        confidence = "high"
        reasons.append("inside_product_panel_visible_enabled_cart_action")
        if control.get("id") == "addtocartbutton":
            reasons.append("stable_addtocartbutton_selector")
        if control.get("id") == "add-to-cart":
            reasons.append("stable_chaparral_add_to_cart_selector")
        if control.get("data_tracking_category") == "oem_AddToCartButton":
            reasons.append("oem_add_to_cart_tracking_fallback")
        if _contains("/cart/add", str(control.get("form_action") or "")):
            reasons.append("cart_add_form_action")
        if _contains("cart.php", str(control.get("form_action") or "")):
            reasons.append("chaparral_cart_form_action")
    elif cart_related and not rejected and visible and enabled:
        confidence = "medium"
        reasons.append("cart_action_outside_product_panel")
    return {
        **control,
        "combined_text": joined,
        "outside_product_panel": outside_product_panel,
        "cart_related": cart_related,
        "rejected": rejected,
        "confidence": confidence,
        "distance_to_product_title": 0 if not outside_product_panel else None,
        "distance_to_requested_part_number": 0 if _contains(row.oem_part_number, joined + " " + product_region) else None,
        "distance_to_see_price_in_cart": 0 if _contains("see price in cart", joined + " " + product_region) else None,
        "distance_to_quantity_input": None,
        "distance_to_reference_price": 0 if row.reference_price and _contains(row.reference_price, joined + " " + product_region) else None,
        "reasons": reasons,
    }


def select_high_confidence_cart_action(candidates: list[dict[str, object]]) -> dict[str, object]:
    high = [candidate for candidate in candidates if candidate.get("confidence") == "high" and not candidate.get("rejected")]
    if len(high) == 1:
        return {"status": "selected", "candidate": high[0]}
    if len(high) > 1:
        return {"status": "ambiguous", "candidate": None, "high_confidence_count": len(high)}
    return {"status": "not_found", "candidate": None, "high_confidence_count": 0}


def click_cart_action(page, candidate: dict[str, object] | None) -> bool:
    return bool(click_cart_action_with_result(page, candidate)["clicked"])


def click_cart_action_with_result(page, candidate: dict[str, object] | None, *, timeout_ms: int = 5000) -> dict[str, object]:
    if not candidate or candidate.get("rejected"):
        return {"clicked": False, "reason": "add_to_cart_button_not_found", "selector": ""}
    suppressed_overlays = suppress_known_click_overlays(page)
    stable_selector = str(candidate.get("stable_selector") or "")
    selector_hint = str(candidate.get("selector_hint") or "")
    text = str(candidate.get("text") or candidate.get("aria_label") or "")
    selectors = []
    if candidate.get("id") == "addtocartbutton":
        selectors.append('form#addtocartform input#addtocartbutton[type="submit"]')
        selectors.append("#addtocartbutton")
    if candidate.get("id") == "add-to-cart":
        selectors.append('form#orderform input#add-to-cart[type="submit"]')
        selectors.append("#add-to-cart")
    if candidate.get("data_tracking_category") == "oem_AddToCartButton":
        selectors.append('form#addtocartform input[data-tracking*="oem_AddToCartButton"]')
    if stable_selector:
        selectors.append(stable_selector)
    if selector_hint:
        selectors.append(selector_hint)
    if text:
        selectors.extend(
            [
                f"button:has-text('{_selector_text(text)}')",
                f"a:has-text('{_selector_text(text)}')",
                f"input[value='{_selector_text(text)}']",
                f"[role='button']:has-text('{_selector_text(text)}')",
            ]
        )
    last_error = ""
    timed_out_selector = ""
    for selector in dict.fromkeys(selectors):
        try:
            locator = page.locator(selector)
            if locator.count():
                _click_cart_locator(locator.first, candidate, timeout_ms=timeout_ms)
                return {"clicked": True, "reason": "clicked", "selector": selector, "suppressed_overlays": suppressed_overlays}
        except PlaywrightTimeoutError as exc:
            error = str(exc)
            if _looks_like_overlay_intercept(error):
                suppressed_overlays.extend(suppress_known_click_overlays(page))
                try:
                    _click_cart_locator(locator.first, candidate, timeout_ms=timeout_ms)
                    return {"clicked": True, "reason": "clicked_after_overlay_suppression", "selector": selector, "suppressed_overlays": sorted(set(suppressed_overlays))}
                except PlaywrightTimeoutError as retry_exc:
                    error = str(retry_exc)
                    if candidate.get("id") == "add-to-cart" and _submit_chaparral_cart_form(page):
                        return {"clicked": True, "reason": "submitted_chaparral_form_after_click_timeout", "selector": "form#orderform", "error": error, "suppressed_overlays": sorted(set(suppressed_overlays))}
            # Carry on to the next selector rather than giving up here. The list
            # above exists precisely as a set of alternatives, and returning on
            # the first timeout meant none of the others was ever tried: a
            # 997-part MotoSport run failed to click the cart on 38 parts while
            # a working selector may have been next in line.
            last_error = error
            # Remember which selector timed out: knowing that is what makes the
            # failure diagnosable, and it would otherwise be lost now that the
            # loop carries on to the alternatives.
            timed_out_selector = timed_out_selector or selector
            continue
        except Exception as exc:
            last_error = str(exc)
            continue
    if candidate.get("id") == "add-to-cart" and _submit_chaparral_cart_form(page):
        return {"clicked": True, "reason": "submitted_chaparral_form_fallback", "selector": "form#orderform", "error": last_error, "suppressed_overlays": sorted(set(suppressed_overlays))}
    if candidate.get("id") == "addtocartbutton" and _submit_motosport_cart_form(page):
        return {"clicked": True, "reason": "submitted_motosport_form_fallback", "selector": "form#addtocartform", "error": last_error, "suppressed_overlays": sorted(set(suppressed_overlays))}
    # Say which it was. Reporting a timeout as "button not found" sends anyone
    # investigating after the wrong thing entirely.
    reason = "add_to_cart_click_timeout" if timed_out_selector else "add_to_cart_button_not_found"
    return {"clicked": False, "reason": reason, "selector": timed_out_selector, "error": last_error, "suppressed_overlays": sorted(set(suppressed_overlays))}


def _click_cart_locator(locator, candidate: dict[str, object], *, timeout_ms: int) -> None:
    if candidate.get("id") == "add-to-cart":
        try:
            locator.click(timeout=timeout_ms, no_wait_after=True)
            return
        except TypeError:
            pass
    locator.click(timeout=timeout_ms)


def _submit_motosport_cart_form(page) -> bool:
    """Submit MotoSport's cart form directly.

    A click can time out because something sits over the button or it is still
    animating, even though the form itself is perfectly submittable. Chaparral
    already had this fallback; MotoSport did not, which is why a click timeout
    there produced no price at all.
    """
    try:
        return bool(
            page.evaluate(
                """
                () => {
                  const form = document.querySelector('form#addtocartform');
                  const button = document.querySelector('form#addtocartform input#addtocartbutton[type="submit"]')
                    || document.querySelector('form#addtocartform [type="submit"]');
                  if (!form || !button || button.disabled || button.getAttribute('aria-disabled') === 'true') {
                    return false;
                  }
                  if (typeof form.requestSubmit === 'function') {
                    form.requestSubmit(button);
                  } else {
                    button.click();
                  }
                  return true;
                }
                """
            )
        )
    except Exception:
        return False


def _submit_chaparral_cart_form(page) -> bool:
    try:
        return bool(
            page.evaluate(
                """
                () => {
                  const form = document.querySelector('form#orderform');
                  const button = document.querySelector('form#orderform input#add-to-cart[type="submit"]');
                  if (!form || !button || button.disabled || button.getAttribute('aria-disabled') === 'true') {
                    return false;
                  }
                  if (typeof form.requestSubmit === 'function') {
                    form.requestSubmit(button);
                  } else {
                    button.click();
                  }
                  return true;
                }
                """
            )
        )
    except Exception:
        return False


def suppress_known_click_overlays(page) -> list[str]:
    try:
        removed = page.evaluate(
            """
            () => {
              const selectors = [
                '#attentive_overlay',
                '#attentive_creative',
                'iframe#attentive_creative',
                '[id^="attentive_"]',
                '[id*="attentive"]',
                '[class*="attentive"]',
                '[data-kl-scroll-locking-modal="true"]',
                '[aria-label="POPUP Form"]',
                '[class*="klaviyo"]',
                '[class*="kl-private-reset"]',
                '[data-testid="form-component"]'
              ];
              const removed = [];
              const safeRemove = (el, selector) => {
                if (!el || el === document.documentElement || el === document.body) {
                  return;
                }
                const target = el.closest('[role="dialog"],[aria-modal="true"],[data-kl-scroll-locking-modal="true"],[aria-label="POPUP Form"]') || el;
                if (!target || target === document.documentElement || target === document.body) {
                  return;
                }
                const style = window.getComputedStyle(target);
                const isDialog = target.getAttribute('role') === 'dialog' || target.getAttribute('aria-modal') === 'true';
                const isKnownModal = target.matches('[data-kl-scroll-locking-modal="true"],[aria-label="POPUP Form"],#attentive_overlay,#attentive_creative,iframe#attentive_creative,[id^="attentive_"],[id*="attentive"],[class*="attentive"]');
                const isFloating = ['fixed', 'sticky', 'absolute'].includes(style.position);
                if (!isDialog && !isKnownModal && !isFloating) {
                  return;
                }
                removed.push(selector);
                target.remove();
              };
              for (const selector of selectors) {
                for (const el of Array.from(document.querySelectorAll(selector))) {
                  safeRemove(el, selector);
                }
              }
              document.documentElement.style.overflow = '';
              document.body.style.overflow = '';
              return Array.from(new Set(removed));
            }
            """
        )
        return [str(item) for item in (removed or [])]
    except Exception:
        return []


def _looks_like_overlay_intercept(error: str) -> bool:
    lowered = error.lower()
    return (
        "intercepts pointer events" in lowered
        or "attentive_overlay" in lowered
        or "attentive_creative" in lowered
        or "klaviyo" in lowered
        or "kl-private" in lowered
        or "popup form" in lowered
    )


def click_add_to_cart(page) -> bool:
    for selector in ["button:has-text('Add to Cart')", "button:has-text('Add To Cart')", "[role='button']:has-text('Add to Cart')"]:
        try:
            locator = page.locator(selector)
            if locator.count():
                locator.first.click()
                return True
        except Exception:
            continue
    return False


def validate_cart_action_form(page, candidate: dict[str, object], *, row: CartProbeInputRow, observation) -> dict[str, object]:
    evidence = {
        "valid": False,
        "checks": {},
        "warnings": [],
        "form": {},
        "candidate": _sanitized_candidate(candidate),
    }
    selected_region = product_region_text(observation)
    checks = evidence["checks"]
    checks["product_association_confirmed"] = product_association_confirmed(observation, row)
    checks["selected_region_contains_part"] = _contains(row.oem_part_number, selected_region)
    try:
        form = page.evaluate(
            """
            () => {
              const form = document.querySelector('#addtocartform, form#orderform');
              if (!form) {
                return {exists: false};
              }
              const action = form.getAttribute('action') || form.action || '';
              const method = form.getAttribute('method') || form.method || '';
              const button = form.querySelector('#addtocartbutton, #add-to-cart');
              const quantities = Array.from(form.querySelectorAll('input[name*="qty" i],input[name*="quantity" i],input[id*="qty" i],input[id*="quantity" i]')).map((el) => ({
                id: el.id || '',
                name: el.getAttribute('name') || '',
                value: el.value || ''
              }));
              return {
                exists: true,
                id: form.id || '',
                action,
                method,
                has_addtocartbutton: Boolean(button),
                addtocartbutton_type: button ? (button.getAttribute('type') || '') : '',
                addtocartbutton_value: button ? (button.value || '') : '',
                addtocartbutton_visible: button ? Boolean(button.getBoundingClientRect().width && button.getBoundingClientRect().height) : false,
                addtocartbutton_enabled: button ? !Boolean(button.disabled || button.getAttribute('aria-disabled') === 'true') : false,
                quantities
              };
            }
            """
        )
    except Exception as exc:
        form = {"exists": False, "error": str(exc)}
    evidence["form"] = form
    quantities = list(form.get("quantities") or [])
    quantity_values = [str(item.get("value") or "").strip() for item in quantities]
    quantity_valid = all(value in {"", "1"} for value in quantity_values) if quantities else True
    checks["form_exists"] = bool(form.get("exists"))
    checks["form_method_post"] = str(form.get("method") or "").lower() == "post"
    checks["form_action_cart_add"] = _is_cart_add_action(str(form.get("action") or ""))
    checks["action_inside_form"] = bool(form.get("has_addtocartbutton")) if candidate.get("id") in {"addtocartbutton", "add-to-cart"} else _is_cart_add_form(str(candidate.get("form_id") or ""), str(candidate.get("form_action") or ""))
    checks["action_visible_enabled"] = bool(candidate.get("visible")) and bool(candidate.get("enabled")) and not bool(candidate.get("disabled"))
    checks["addtocartbutton_type_submit"] = str(form.get("addtocartbutton_type") or "").lower() == "submit"
    checks["addtocartbutton_value_add_to_cart"] = str(form.get("addtocartbutton_value") or "").strip().lower() == "add to cart"
    checks["quantity_one_or_blank"] = quantity_valid
    checks["candidate_high_confidence"] = candidate.get("confidence") == "high" and not candidate.get("rejected")
    for check_name, passed in checks.items():
        if not passed:
            evidence["warnings"].append(check_name)
    evidence["valid"] = all(bool(value) for value in checks.values())
    return evidence


def wait_for_cart_response(page, *, context: CartProbeRunContext | None = None) -> None:
    timeout_ms = (context.limits.max_cart_navigation_wait_seconds if context is not None else 15) * 1000
    cart_rendered = False
    try:
        page.wait_for_function(
            """
            () => Boolean(document.querySelector(
              '[data-sku], .cart-item, .cart-row, a.cart-remove-item, .removeCartItem, [class*="cart" i] [class*="price" i]'
            ))
            """,
            timeout=min(4000, timeout_ms),
        )
        cart_rendered = True
    except Exception:
        pass
    try:
        if not cart_rendered:
            page.wait_for_load_state("networkidle", timeout=min(CART_NETWORK_IDLE_CAP_MS, timeout_ms))
    except Exception:
        pass
    try:
        page.wait_for_timeout(min(CART_RESPONSE_SETTLE_MS, timeout_ms))
    except Exception:
        pass


def live_step_timeout_ms(context: CartProbeRunContext) -> int:
    return min(context.limits.max_cart_navigation_wait_seconds * 1000, context.limits.max_part_seconds * 1000)


def live_click_timeout_ms(context: CartProbeRunContext) -> int:
    return min(5000, live_step_timeout_ms(context))


def write_cart_probe_progress(product_output_dir: Path, row: CartProbeInputRow, *, step: str, status: str, details: dict[str, object] | None = None) -> None:
    product_output_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "updated_at": utc_now(),
        "oem_part_number": row.oem_part_number,
        "manufacturer": row.manufacturer,
        "step": step,
        "status": status,
        "details": details or {},
    }
    (product_output_dir / "cart_probe_progress.json").write_text(json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8")


def collect_cart_line_records(page) -> list[dict[str, object]]:
    try:
        return page.evaluate(
            """
            () => {
              const rows = Array.from(document.querySelectorAll('[data-sku], tr, li, .cart-item, .cart-row'));
              const removeControls = Array.from(document.querySelectorAll('a,button,[role="button"]')).filter((el) =>
                (el.innerText || el.textContent || '').trim().toLowerCase() === 'remove' &&
                Boolean(el.getBoundingClientRect().width && el.getBoundingClientRect().height)
              );
              removeControls.forEach((remove, index) => {
                let container = remove.parentElement;
                let depth = 0;
                while (container && container !== document.body && depth < 20) {
                  const text = (container.innerText || container.textContent || '').trim();
                  if (/\\$[\\d,]+(?:\\.\\d{2})?/.test(text) && /\\b(?:quantity|current price)\\b/i.test(text)) {
                    remove.setAttribute('data-part-pulse-cart-remove', String(index));
                    container.setAttribute('data-part-pulse-cart-line', String(index));
                    rows.push(container);
                    break;
                  }
                  container = container.parentElement;
                  depth += 1;
                }
              });
              const uniqueRows = Array.from(new Set(rows)).sort((left, right) =>
                Number(right.hasAttribute('data-part-pulse-cart-line')) - Number(left.hasAttribute('data-part-pulse-cart-line'))
              );
              return uniqueRows.slice(0, 100).map((el, index) => {
              const remove = el.matches('a.cart-remove-item,.removeCartItem,[data-part-pulse-cart-remove]') ? el : el.querySelector('a.cart-remove-item[title="Remove item from cart."],.removeCartItem,[data-part-pulse-cart-remove]');
              const qtyInput = el.querySelector('input.quantity-selector-input,input[id^="amount_"]');
              const dataSku = el.getAttribute('data-sku') || (remove ? remove.getAttribute('data-sku') : '') || '';
              const dataQuantity = el.getAttribute('data-quantity') || (remove ? remove.getAttribute('data-quantity') : '') || (qtyInput ? qtyInput.value : '') || '';
              const removeSelector = (() => {
                if (remove && dataSku) {
                  return `a.cart-remove-item[title="Remove item from cart."][data-sku="${CSS.escape(dataSku)}"]`;
                }
                if (remove && remove.hasAttribute('data-part-pulse-cart-remove')) {
                  return `[data-part-pulse-cart-remove="${remove.getAttribute('data-part-pulse-cart-remove')}"]`;
                }
                if (remove && remove.classList.contains('removeCartItem') && qtyInput && qtyInput.id) {
                  return `tr:has(input#${CSS.escape(qtyInput.id)}) span.cursor:has-text("X")`;
                }
                return '';
              })();
              return {
                index,
                text: (el.innerText || el.textContent || '').trim(),
                data_sku: dataSku,
                data_quantity: dataQuantity,
                remove_selector: removeSelector,
                remove_title: remove ? (remove.getAttribute('title') || '') : '',
                remove_href_present: Boolean(remove && remove.getAttribute('href')),
                remove_href_used: false
              };
            }).filter((row) => row.text || row.data_sku || row.remove_selector);
            }
            """
        )
    except Exception:
        return []


def cart_line_evidence(
    cart_text: str,
    row: CartProbeInputRow,
    *,
    supporting_sku: str | None = None,
    cart_line_records: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    line = matching_cart_line(cart_text, row)
    matching_records: list[dict[str, object]] = []
    for record in cart_line_records or []:
        record_text = str(record.get("text") or "")
        record_sku = str(record.get("data_sku") or "")
        if _contains(row.oem_part_number, record_text) or _contains(row.product_name, record_text) or (supporting_sku and record_sku == supporting_sku):
            matching_records.append(record)
    matching_record = max(
        matching_records,
        key=lambda record: (
            bool(record.get("remove_selector")),
            bool(supporting_sku and record.get("data_sku") == supporting_sku),
            -len(str(record.get("text") or "")),
        ),
        default=None,
    )
    if matching_record:
        line = str(matching_record.get("text") or "") or line
    raw_price_candidates = cart_price_candidates(line or "")
    placeholder_price_candidates = [price for price in raw_price_candidates if price in CART_PRICE_PLACEHOLDERS]
    price_candidates = [price for price in raw_price_candidates if price not in CART_PRICE_PLACEHOLDERS]
    quantity = extract_quantity(line or "") if line else None
    if matching_record and str(matching_record.get("data_quantity") or "").isdigit():
        quantity = int(str(matching_record["data_quantity"]))
    confirmed = bool(
        line
        and (
            _contains(row.oem_part_number, line)
            or _contains(row.product_name, line)
            or bool(supporting_sku and matching_record and matching_record.get("data_sku") == supporting_sku)
        )
    )
    accepted_price = price_candidates[0] if confirmed and quantity == 1 and price_candidates else None
    return {
        "confirmed": confirmed,
        "raw_cart_line_text": line or "",
        "accepted_price": accepted_price,
        "quantity": quantity,
        "line_subtotal": accepted_price if quantity == 1 else None,
        "rejected_price_candidates": price_candidates[1:] if accepted_price else price_candidates,
        "rejected_placeholder_price_candidates": placeholder_price_candidates,
        "reason_accepted": "matched_cart_line_product_price" if confirmed and accepted_price is not None else "",
        "supporting_sku": supporting_sku or "",
        "matched_data_sku": str((matching_record or {}).get("data_sku") or ""),
        "remove_selector": str((matching_record or {}).get("remove_selector") or ""),
        "remove_href_present": bool((matching_record or {}).get("remove_href_present")),
        "remove_href_used": False,
        "matching_evidence": {
            "part_number": _contains(row.oem_part_number, line or ""),
            "product_name": _contains(row.product_name, line or ""),
            "product_url": _contains(row.product_url, line or ""),
            "data_sku": bool(supporting_sku and matching_record and matching_record.get("data_sku") == supporting_sku),
        },
    }


def save_cart_action_diagnostics(product_output_dir: Path, row: CartProbeInputRow, observation, inventory: dict[str, object]) -> None:
    product_output_dir.mkdir(parents=True, exist_ok=True)
    safe_inventory = _redact_sensitive_values(inventory)
    diagnostics = {
        "oem_part_number": row.oem_part_number,
        "manufacturer": row.manufacturer,
        "product_url": row.product_url,
        "product_association_confirmed": product_association_confirmed(observation, row),
        "page_classification": observation.page_classification,
        "price_visibility": observation.price_visibility,
        "scan_count": safe_inventory.get("scan_count", 1),
        "candidate_count": safe_inventory.get("candidate_count", 0),
        "high_confidence_count": safe_inventory.get("high_confidence_count", 0),
        "selected_action": select_high_confidence_cart_action(list(safe_inventory.get("candidates", []))),
        "cart_action_inventory": safe_inventory,
    }
    (product_output_dir / "cart_action_diagnostics.json").write_text(json.dumps(diagnostics, indent=2, default=str) + "\n", encoding="utf-8")
    (product_output_dir / "cart_action_candidates.txt").write_text(_candidate_lines(list(safe_inventory.get("candidates", []))), encoding="utf-8")
    selected_region = product_region_text(observation)
    (product_output_dir / "selected_product_region.txt").write_text((selected_region or "No selected product region captured.") + "\n", encoding="utf-8")
    (product_output_dir / "visible_buttons.txt").write_text(_control_lines(list(safe_inventory.get("visible_buttons", []))), encoding="utf-8")
    (product_output_dir / "visible_links.txt").write_text(_control_lines(list(safe_inventory.get("visible_links", []))), encoding="utf-8")
    (product_output_dir / "product_panel_forms.txt").write_text(json.dumps(safe_inventory.get("product_panel_forms", []), indent=2, default=str) + "\n", encoding="utf-8")
    classification = [
        f"page_classification: {observation.page_classification}",
        f"price_visibility: {observation.price_visibility}",
        f"product_association_confirmed: {diagnostics['product_association_confirmed']}",
        f"candidate_selection_status: {diagnostics['selected_action'].get('status')}",
    ]
    (product_output_dir / "cart_probe_page_classification.txt").write_text("\n".join(classification) + "\n", encoding="utf-8")


def save_real_cart_probe_diagnostics(product_output_dir: Path, row: CartProbeInputRow, candidate: dict[str, object] | None, *, form_validation: dict[str, object]) -> None:
    payload = {
        "oem_part_number": row.oem_part_number,
        "manufacturer": row.manufacturer,
        "cart_action": _sanitized_candidate(candidate or {}),
        "form_validation": form_validation,
        "browser_ui_click_only": True,
        "direct_http_post_used": False,
    }
    (product_output_dir / "cart_action_used.json").write_text(json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8")


def save_cart_line_evidence(product_output_dir: Path, row: CartProbeInputRow, evidence: dict[str, object]) -> None:
    lines = [
        f"OEM part number: {row.oem_part_number}",
        f"Product name: {row.product_name}",
        f"Cart line confirmed: {evidence.get('confirmed')}",
        f"Accepted price: {evidence.get('accepted_price') or ''}",
        f"Quantity: {evidence.get('quantity') or ''}",
        "",
        str(evidence.get("raw_cart_line_text") or "No confirmed cart line."),
        "",
    ]
    (product_output_dir / "cart_line_evidence.txt").write_text("\n".join(lines), encoding="utf-8")


def save_cart_price_evidence(product_output_dir: Path, row: CartProbeInputRow, evidence: dict[str, object]) -> None:
    payload = {
        "oem_part_number": row.oem_part_number,
        "reference_price_from_product_page": row.reference_price,
        "accepted_price": evidence.get("accepted_price"),
        "rejected_price_candidates": evidence.get("rejected_price_candidates", []),
        "reason_accepted": evidence.get("reason_accepted", ""),
        "matching_evidence": evidence.get("matching_evidence", {}),
        "cart_line_confirmed": evidence.get("confirmed", False),
    }
    (product_output_dir / "cart_price_evidence.json").write_text(json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8")


def save_cleanup_evidence(product_output_dir: Path, row: CartProbeInputRow, *, cleanup_status: str, page) -> None:
    try:
        text = page.locator("body").inner_text(timeout=2000) if page.locator("body").count() else ""
    except Exception:
        text = ""
    lines = [
        f"OEM part number: {row.oem_part_number}",
        f"cleanup_status: {cleanup_status}",
        f"cart_empty_verified: {_cart_empty_text(text)}",
        "",
        (text[:1000] if text else "No cleanup page text captured."),
        "",
    ]
    (product_output_dir / "cleanup_evidence.txt").write_text("\n".join(lines), encoding="utf-8")


def save_cart_click_failure_evidence(product_output_dir: Path, row: CartProbeInputRow, page, *, click_result: dict[str, object]) -> None:
    try:
        text = page.locator("body").inner_text(timeout=2000) if page.locator("body").count() else ""
    except Exception as exc:
        text = f"Unable to capture page text: {exc}"
    lines = [
        f"OEM part number: {row.oem_part_number}",
        f"manufacturer: {row.manufacturer}",
        f"current_url: {getattr(page, 'url', '')}",
        f"click_result: {json.dumps(click_result, default=str)}",
        "",
        text[:2000] if text else "No page text captured.",
        "",
    ]
    (product_output_dir / "cart_click_failure_evidence.txt").write_text("\n".join(lines), encoding="utf-8")


def _control_associated_with_product(control: dict[str, object], row: CartProbeInputRow, product_region: str) -> bool:
    combined = " ".join(
        [
            str(control.get("combined_text") or ""),
            str(control.get("text") or ""),
            str(control.get("aria_label") or ""),
            str(control.get("title") or ""),
            str(control.get("data_testid") or ""),
            str(control.get("id") or ""),
            str(control.get("class_summary") or ""),
            str(control.get("form_id") or ""),
            str(control.get("form_action") or ""),
        ]
    )
    if _is_cart_add_form(str(control.get("form_id") or ""), str(control.get("form_action") or "")):
        return True
    if control.get("id") == "addtocartbutton" and _contains("/cart/add", str(control.get("form_action") or "")):
        return True
    if control.get("id") == "add-to-cart" and _contains("cart.php", str(control.get("form_action") or "")):
        return True
    if _contains(row.oem_part_number, combined) or _contains(row.product_name, combined):
        return True
    if not product_region:
        return False
    if (_contains(row.oem_part_number, product_region) or _contains(row.product_name, product_region)) and (
        _contains("addtocartbutton", combined) or _contains("add to cart", combined) or _contains("see price in cart", combined)
    ):
        return True
    return False


def product_region_text(observation) -> str:
    raw = observation.raw_evidence_summary or {}
    return str(raw.get("selected_product_region") or raw.get("region_text") or "")


def product_association_confirmed(observation, row: CartProbeInputRow) -> bool:
    raw = observation.raw_evidence_summary or {}
    association = raw.get("product_association") or {}
    if isinstance(association, dict) and association.get("confirmed"):
        return True
    matched_part = str(raw.get("matched_part_number") or "")
    if matched_part and _contains(row.oem_part_number, matched_part):
        return True
    region = product_region_text(observation)
    return _contains(row.oem_part_number, region) or _contains(row.product_name, region)


def _is_cart_add_action(action: str) -> bool:
    return _contains("/cart/add", action) or _contains("cart.php", action)


def _is_cart_add_form(form_id: str, form_action: str) -> bool:
    return (
        _contains("addtocartform", form_id)
        and _contains("/cart/add", form_action)
    ) or (
        _contains("orderform", form_id)
        and _contains("cart.php", form_action)
    )


def _redact_sensitive_values(value: Any, parent_key: str = "") -> Any:
    sensitive = ("token", "csrf", "auth", "session", "cookie", "password")
    if isinstance(value, dict):
        sensitive_record = any(
            key in value and isinstance(value.get(key), str) and any(word in str(value.get(key)).lower() for word in sensitive)
            for key in ("name", "id")
        )
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key).lower()
            if any(word in key_text for word in sensitive):
                redacted[key] = "[redacted]"
            elif key_text in {"name", "id"} and isinstance(item, str) and any(word in item.lower() for word in sensitive):
                redacted[key] = "[redacted]"
            elif key in {"value", "text"} and (sensitive_record or any(word in parent_key.lower() for word in sensitive)):
                redacted[key] = "[redacted]"
            else:
                redacted[key] = _redact_sensitive_values(item, key_text)
        return redacted
    if isinstance(value, list):
        return [_redact_sensitive_values(item, parent_key) for item in value]
    return value


def _sanitized_candidate(candidate: dict[str, object]) -> dict[str, object]:
    allowed = {
        "index",
        "selector_hint",
        "stable_selector",
        "tag_name",
        "input_type",
        "input_value",
        "name",
        "role",
        "text",
        "aria_label",
        "title",
        "data_testid",
        "data_test",
        "data_cy",
        "data_tracking_category",
        "data_tracking_label",
        "data_sku",
        "data_quantity",
        "id",
        "class_summary",
        "form_id",
        "form_action",
        "form_method",
        "disabled",
        "visible",
        "enabled",
        "bounding_box",
        "outside_product_panel",
        "cart_related",
        "rejected",
        "confidence",
        "reasons",
    }
    return {key: value for key, value in candidate.items() if key in allowed}


def extract_tracking_label(candidate: dict[str, object] | None) -> str:
    if not candidate:
        return ""
    return str(candidate.get("data_tracking_label") or "")


def _real_part_dir(output_dir: Path, row: CartProbeInputRow) -> Path:
    part_dir = output_dir / _safe_name(row.oem_part_number)
    part_dir.mkdir(parents=True, exist_ok=True)
    return part_dir


def _write_part_and_single_run_file(output_dir: Path, part_dir: Path, filename: str, text: str) -> None:
    (part_dir / filename).write_text(text, encoding="utf-8")
    (output_dir / filename).write_text(text, encoding="utf-8")


def _contains(needle: str | None, haystack: str | None) -> bool:
    return bool(needle and haystack and needle.lower() in haystack.lower())


def _selector_text(text: str) -> str:
    return text.replace("\\", "\\\\").replace("'", "\\'")


def _safe_name(value: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", value.strip())
    return safe or "unknown_part"


def _candidate_lines(candidates: list[dict[str, object]]) -> str:
    if not candidates:
        return "No visible cart-action candidates found.\n"
    lines = []
    for candidate in candidates:
        lines.append(
            " | ".join(
                [
                    f"#{candidate.get('index', '')}",
                    f"confidence={candidate.get('confidence', '')}",
                    f"rejected={candidate.get('rejected', '')}",
                    f"outside_product_panel={candidate.get('outside_product_panel', '')}",
                    f"tag={candidate.get('tag_name', '')}",
                    f"text={candidate.get('combined_text', '')}",
                    f"selector={candidate.get('selector_hint', '')}",
                    f"reasons={','.join(str(reason) for reason in candidate.get('reasons', []))}",
                ]
            )
        )
    return "\n".join(lines) + "\n"


def _control_lines(controls: list[dict[str, object]]) -> str:
    if not controls:
        return "No visible controls found.\n"
    lines = []
    for control in controls:
        lines.append(
            " | ".join(
                [
                    f"#{control.get('index', '')}",
                    f"tag={control.get('tag_name', '')}",
                    f"text={control.get('combined_text') or control.get('text') or ''}",
                    f"aria={control.get('aria_label', '')}",
                    f"data-testid={control.get('data_testid', '')}",
                    f"id={control.get('id', '')}",
                    f"class={control.get('class_summary', '')}",
                    f"visible={control.get('visible', '')}",
                    f"enabled={control.get('enabled', '')}",
                    f"box={control.get('bounding_box', '')}",
                ]
            )
        )
    return "\n".join(lines) + "\n"


def open_cart_text(page) -> str:
    # Never clicks checkout. It only reads the current page/cart drawer text.
    return page.locator("body").inner_text(timeout=5000) if page.locator("body").count() else ""


def matching_cart_line(cart_text: str, row: CartProbeInputRow) -> str | None:
    lines = [line.strip() for line in cart_text.splitlines() if line.strip()]
    part = row.oem_part_number.lower()
    product = row.product_name.lower()
    for index, line in enumerate(lines):
        lowered = line.lower()
        if part in lowered or (product and product in lowered):
            return " ".join(lines[index : index + 14])
    return None


def cart_price_candidates(text: str) -> list[Decimal]:
    current_price = re.search(r"Current Price:\s*(\$[\d,]+(?:\.\d{2})?)", text, re.IGNORECASE)
    if current_price:
        return [parse_money(current_price.group(1)).value]
    return [parse_money(match.group(0)).value for match in re.finditer(r"\$[\d,]+(?:\.\d{2})?", text)]


def extract_first_money(text: str) -> Decimal | None:
    match = re.search(r"\$[\d,]+(?:\.\d{2})?", text)
    return parse_money(match.group(0)).value if match else None


def extract_quantity(text: str) -> int | None:
    match = re.search(r"\b(?:qty|quantity)\s*[:#]?\s*(\d+)\b", text, re.IGNORECASE)
    if match:
        return int(match.group(1))
    return 1 if text else None


# MotoSport's cart removes a line with a trash-can icon, not a link reading
# "Remove". Every removal path here keyed on that literal word, so on a real
# cart it found nothing, reported no_remove_control_found, and deleted nothing.
# These cover a labelled control however it is presented: an accessible name, a
# title, a test hook, or a class naming the action.
REMOVE_CONTROL_SELECTORS = (
    "button[aria-label*='Remove' i]",
    "a[aria-label*='Remove' i]",
    "button[title*='Remove' i]",
    "a[title*='Remove' i]",
    "button[aria-label*='Delete' i]",
    "button[title*='Delete' i]",
    "[data-testid*='remove' i]",
    "[data-test*='remove' i]",
    "button[class*='remove' i]",
    "a[class*='remove' i]",
    "button[class*='trash' i]",
    "[class*='cart-item'] button[class*='delete' i]",
    "form[action*='remove'] button",
    "form[action*='delete'] button",
    "a:has-text('Remove')",
    "button:has-text('Remove')",
)

# Counting lines rather than the word "Remove" is what makes removal verifiable
# regardless of how the control is labelled.
CART_LINE_SELECTORS = (
    "[data-testid*='cart-item' i]",
    "[class*='cart-item' i]",
    "[class*='cartItem' i]",
    "[class*='line-item' i]",
    "tr[class*='item' i]",
)


def count_cart_lines(page) -> int:
    """How many lines the cart appears to hold.

    Used to confirm a removal actually happened. Falls back to counting
    "Save For Later" controls, which MotoSport renders once per line, when no
    structural selector matches.
    """
    for selector in CART_LINE_SELECTORS:
        try:
            count = page.locator(selector).count()
        except Exception:
            continue
        if count:
            return count
    try:
        text = page.locator("body").inner_text(timeout=2000) if page.locator("body").count() else ""
        return len(re.findall(r"Save\s+For\s+Later", text, flags=re.IGNORECASE))
    except Exception:
        return 0


def find_remove_controls(page) -> tuple[str, int]:
    """The first selector that matches a removal control, and how many it found."""
    for selector in REMOVE_CONTROL_SELECTORS:
        try:
            count = page.locator(selector).count()
        except Exception:
            continue
        if count:
            return selector, count
    return "", 0


def _await_cart_change(page, *, lines_before: int, timeout_ms: int = 10000, interval_ms: int = 400) -> str:
    """Wait until the cart empties or loses a line.

    Returns "empty", "removed", or "unchanged". Polling for the change the click
    was supposed to cause is what makes this reliable on a slow re-render,
    without waiting the full timeout when it is quick.
    """
    waited = 0
    while waited < timeout_ms:
        try:
            text = page.locator("body").inner_text(timeout=2000) if page.locator("body").count() else ""
        except Exception:
            text = ""
        if _cart_empty_text(text):
            return "empty"
        if lines_before and count_cart_lines(page) < lines_before:
            return "removed"
        page.wait_for_timeout(interval_ms)
        waited += interval_ms
    return "unchanged"


def clear_whole_cart(page, *, max_items: int = 25) -> dict[str, object]:
    """Remove every line from the cart.

    Reading a MotoSport price means adding the item to the cart, so the cart has
    to be emptied afterwards or items accumulate. Each removal is confirmed by
    the number of cart lines falling, rather than by the presence of any
    particular word, so this works whether the control is a link or an icon and
    stops rather than looping when a click has no effect.
    """
    removed = 0
    diagnostics: list[str] = []
    for _attempt in range(max_items):
        try:
            text = page.locator("body").inner_text(timeout=2000) if page.locator("body").count() else ""
        except Exception:
            return {"cleared": False, "removed": removed, "reason": "cart_not_readable", "detail": "; ".join(diagnostics)}
        if _cart_empty_text(text):
            return {"cleared": True, "removed": removed, "reason": "cart_empty", "detail": "; ".join(diagnostics)}

        lines_before = count_cart_lines(page)
        selector, control_count = find_remove_controls(page)
        if not control_count:
            # Report what was actually there rather than guessing at controls,
            # so the next attempt can be aimed at something real.
            diagnostics.append(f"no removal control matched; {lines_before} line(s) visible")
            return {
                "cleared": False,
                "removed": removed,
                "reason": "no_remove_control_found",
                "detail": "; ".join(diagnostics),
            }

        try:
            page.locator(selector).first.click(timeout=5000)
        except Exception as exc:
            diagnostics.append(f"click failed on {selector}: {type(exc).__name__}")
            return {"cleared": False, "removed": removed, "reason": "remove_click_failed", "detail": "; ".join(diagnostics)}

        # Wait for the cart to actually change rather than checking once after a
        # fixed pause. Removing a line re-renders the cart, and how long that
        # takes varies with its size and the network. A single check 1.2 seconds
        # later read a slow re-render as "no effect", which is why a few items
        # were removed and then clearing gave up with items still in the cart.
        outcome = _await_cart_change(page, lines_before=lines_before)
        if outcome == "empty":
            return {"cleared": True, "removed": removed + 1, "reason": "cart_empty", "detail": "; ".join(diagnostics)}
        if outcome == "unchanged":
            diagnostics.append(
                f"clicking {selector} left {count_cart_lines(page)} line(s) after waiting, unchanged"
            )
            return {"cleared": False, "removed": removed, "reason": "remove_had_no_effect", "detail": "; ".join(diagnostics)}
        removed += 1
    return {"cleared": False, "removed": removed, "reason": "too_many_items", "detail": "; ".join(diagnostics)}


def remove_cart_item(page, *, context: CartProbeRunContext | None = None, line_evidence: dict[str, object] | None = None) -> bool:
    if context is not None and not context.allow_cleanup_attempt():
        return False
    if not line_evidence or not line_evidence.get("confirmed"):
        return False
    remove_selector = str(line_evidence.get("remove_selector") or "")
    candidates = [remove_selector] if remove_selector else safe_remove_fallback_selectors(page, line_evidence=line_evidence)
    if not candidates:
        return False
    for selector in candidates:
        try:
            locator = page.locator(selector)
            if locator.count():
                try:
                    locator.first.click(timeout=5000, no_wait_after=True)
                except Exception:
                    locator.first.evaluate("element => element.click()")
                page.wait_for_timeout(CART_CLEANUP_SETTLE_MS)
                return True
        except Exception:
            continue
    return False


def safe_remove_fallback_selectors(page, *, line_evidence: dict[str, object]) -> list[str]:
    try:
        text = page.locator("body").inner_text(timeout=2000) if page.locator("body").count() else ""
    except Exception:
        return []
    raw_line = str(line_evidence.get("raw_cart_line_text") or "")
    if not raw_line or not text:
        return []
    if _squash_space(raw_line) not in _squash_space(text):
        return []
    # Only safe when there is exactly one removal control, so the one found can
    # only belong to this line. Clicking the first of several could remove the
    # wrong item, which is worse than not removing anything.
    #
    # Both checks are needed. The literal word count is what catches a cart
    # showing two "Remove" links, and previously it was the only check, which is
    # why a cart using icon controls matched nothing at all.
    if len(re.findall(r"\bRemove\b", text, flags=re.IGNORECASE)) > 1:
        return []
    selector, control_count = find_remove_controls(page)
    if control_count != 1:
        return []
    return [selector]


def _squash_space(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


def write_outputs(output_dir: Path, run: CartProbeRun, args: argparse.Namespace, *, context: CartProbeRunContext | None = None) -> None:
    folder_audit = write_folder_audit(output_dir, run, context)
    if not folder_audit["folder_guard_passed"]:
        run.stopped = True
        run.stop_reason = "output_directory_guard_failed"
        if run.rows:
            run.rows[-1].warnings.append("unexpected_output_directories_created")
    with (output_dir / "cart_probe_summary.csv").open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=OUTPUT_FIELDS)
        writer.writeheader()
        for result in run.rows:
            writer.writerow(summary_row(result))
    (output_dir / "cart_probe_metadata.json").write_text(
        json.dumps(
            {
                "competitor_key": run.competitor_key,
                "input_file": str(args.file),
                "started_at": run.started_at,
                "completed_at": run.completed_at,
                "requested_max_parts": args.max_parts,
                "attempted_parts": len(run.rows),
                "mode": cart_probe_mode(args),
                "initialized": True,
                "status": "completed" if not run.stopped else "stopped",
                "experimental_cart_pricing": True,
                "diagnose_cart_action_only": bool(getattr(args, "diagnose_cart_action_only", False)),
                "dry_run_structure": bool(getattr(args, "dry_run_structure", False)),
                "production_current_state_written": False,
                "stop_reason": run.stop_reason,
                "run_output_dir": str(output_dir),
                "folder_audit": folder_audit,
                "limits": context.limits.__dict__ if context is not None else {},
                "directories_created_count": context.directories_created_count if context is not None else None,
                "max_total_output_directories_created": context.max_total_output_directories_created if context is not None else None,
                "candidate_scan_count": context.candidate_scan_count if context is not None else None,
                "cart_action_attempt_count": context.cart_action_attempt_count if context is not None else None,
                "cleanup_attempt_count": context.cleanup_attempt_count if context is not None else None,
                "empty_cart_check_count": context.empty_cart_check_count if context is not None else None,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    (output_dir / "cart_probe_review.txt").write_text(review_text(run, args=args, folder_audit=folder_audit), encoding="utf-8")


def write_startup_metadata(
    context: CartProbeRunContext,
    *,
    input_file: Path | None,
    requested_max_parts: int | None,
    mode: str,
    status: str,
    stop_reason: str | None = None,
) -> None:
    payload = {
        "started_at": context.started_at,
        "competitor_key": "motosport",
        "input_file": str(input_file) if input_file is not None else "",
        "requested_max_parts": requested_max_parts,
        "mode": mode,
        "initialized": True,
        "status": status,
        "stop_reason": stop_reason,
        "attempted_parts": 0,
        "experimental_cart_pricing": True,
        "production_current_state_written": False,
        "run_output_dir": str(context.run_output_dir),
        "directories_created_count": context.directories_created_count,
        "max_total_output_directories_created": context.max_total_output_directories_created,
    }
    (context.run_output_dir / "cart_probe_metadata.json").write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def write_startup_abort_metadata(
    context: CartProbeRunContext,
    *,
    input_file: Path | None = None,
    requested_max_parts: int | None = None,
    mode: str = "unknown",
    stop_reason: str = "startup_validation_failed",
) -> None:
    write_startup_metadata(
        context,
        input_file=input_file,
        requested_max_parts=requested_max_parts,
        mode=mode,
        status="aborted",
        stop_reason=stop_reason,
    )


def write_folder_audit(output_dir: Path, run: CartProbeRun, context: CartProbeRunContext | None) -> dict[str, object]:
    expected_directories = 1 + (len(context.product_dirs) if context is not None else len(run.rows))
    immediate_dirs = [path for path in output_dir.iterdir() if path.is_dir()] if output_dir.exists() else []
    product_dirs = [str(path) for path in (context.product_dirs.values() if context is not None else immediate_dirs)]
    expected_product_dirs = {Path(path) for path in product_dirs}
    unexpected_dirs = [str(path) for path in immediate_dirs if path not in expected_product_dirs]
    actual_directories_created = 1 + len(immediate_dirs)
    folder_guard_passed = actual_directories_created <= expected_directories and not unexpected_dirs
    audit = {
        "run_output_dir": str(output_dir),
        "expected_directories": expected_directories,
        "actual_directories_created": actual_directories_created,
        "product_output_dirs": product_dirs,
        "unexpected_dirs": unexpected_dirs,
        "folder_guard_passed": folder_guard_passed,
        "warning": "" if folder_guard_passed else "unexpected_output_directories_created",
    }
    (output_dir / "folder_audit.json").write_text(json.dumps(audit, indent=2, default=str) + "\n", encoding="utf-8")
    return audit


def latest_folder_audit_failed(base_data_dir: Path) -> bool:
    base_dir = base_data_dir / "output" / "competitor_probes" / "motosport_cart"
    if not base_dir.exists():
        return False
    folders = [path for path in base_dir.iterdir() if path.is_dir()]
    if not folders:
        return False
    latest = max(folders, key=lambda path: path.stat().st_mtime)
    audit_path = latest / "folder_audit.json"
    if not audit_path.exists():
        metadata_path = latest / "cart_probe_metadata.json"
        if not metadata_path.exists():
            return True
        metadata = _read_json_file(metadata_path)
        return metadata.get("status") in {"initialized", "aborted"} or metadata.get("mode") == "real_cart_probe"
    try:
        audit = json.loads(audit_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return True
    return audit.get("folder_guard_passed") is False


def _read_json_file(path: Path) -> dict[str, object]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def summary_row(result: CartProbeResult) -> dict[str, object]:
    support = manufacturer_support_metadata("motosport", result.row.manufacturer, result.row.oem_part_number)
    return {
        "run_order": result.run_order,
        "manufacturer": result.row.manufacturer,
        "normalized_manufacturer": normalize_manufacturer(result.row.manufacturer),
        "competitor": "motosport",
        "manufacturer_supported": support["manufacturer_supported"],
        "lookup_status": result.result_type,
        "status_reason": "; ".join(result.warnings) if result.result_type == "manufacturer_not_carried" else "",
        "oem_part_number": result.row.oem_part_number,
        "product_url": result.row.product_url,
        "checked_at": result.checked_at,
        "product_association_confirmed": result.product_association_confirmed,
        "reference_price": result.reference_price or "",
        "cart_selling_price": result.cart_selling_price or "",
        "quantity": result.quantity or "",
        "line_subtotal": result.line_subtotal or "",
        "cart_price_confidence": result.cart_price_confidence,
        "cleanup_status": result.cleanup_status,
        "result_type": result.result_type,
        "warnings": "; ".join(result.warnings),
    }


def review_text(run: CartProbeRun, *, args: argparse.Namespace | None = None, folder_audit: dict[str, object] | None = None) -> str:
    diagnostic_only = bool(getattr(args, "diagnose_cart_action_only", False)) if args is not None else False
    dry_run_structure = bool(getattr(args, "dry_run_structure", False)) if args is not None else False
    mode = "diagnostic_only" if diagnostic_only else "dry_run_structure" if dry_run_structure else "real_cart_probe"
    action_clicked = "No" if diagnostic_only or dry_run_structure else "Possible"
    cart_modified = "No" if diagnostic_only or dry_run_structure else "Possible"
    expected_folders = folder_audit.get("expected_directories") if folder_audit else ""
    actual_folders = folder_audit.get("actual_directories_created") if folder_audit else ""
    folder_guard = "Passed" if not folder_audit or folder_audit.get("folder_guard_passed") else "Failed"
    lines = [
        "MOTOSPORT CART PRICE PROBE",
        "",
        f"Mode: {mode}",
        f"Cart action clicked: {action_clicked}",
        f"Cart modified: {cart_modified}",
        f"Expected folders: {expected_folders}",
        f"Actual folders created: {actual_folders}",
        f"Folder guard: {folder_guard}",
        "",
        f"Started: {run.started_at}",
        f"Completed: {run.completed_at or ''}",
        f"Parts requested: {len(run.rows)}",
        f"Parts attempted: {len(run.rows)}",
        f"Cart prices found: {sum(1 for row in run.rows if row.cart_selling_price is not None)}",
        f"Cleanup successful: {sum(1 for row in run.rows if row.cleanup_status == 'success')}",
        f"Cleanup failed: {sum(1 for row in run.rows if row.cleanup_status == 'failed')}",
        f"Stopped: {run.stopped}",
        f"Stop reason: {run.stop_reason or ''}",
        f"Blocked: {run.blocked}",
        f"Challenges: {run.challenges}",
        f"Errors: {run.errors}",
        "",
    ]
    for result in run.rows:
        lines.extend(
            [
                f"OEM part number: {result.row.oem_part_number}",
                f"Product URL: {result.row.product_url}",
                f"Reference price from product page: {result.reference_price or ''}",
                f"Cart price: {result.cart_selling_price or ''}",
                f"Quantity: {result.quantity or ''}",
                f"Line subtotal: {result.line_subtotal or ''}",
                f"Product association confirmed: {result.product_association_confirmed}",
                f"Cleanup status: {result.cleanup_status}",
                f"Warnings: {'; '.join(result.warnings) or 'None'}",
                "",
            ]
        )
    lines.extend(
        [
            "EXPERIMENTAL STATUS",
            "",
            "Cart price results are experimental and have not been promoted to production competitor pricing.",
        ]
    )
    return "\n".join(lines) + "\n"


def _row_is_cart_hidden(row: dict[str, str]) -> bool:
    if row.get("price_visibility") and row.get("price_visibility") != "see_price_in_cart":
        return False
    if row.get("result_type") and row.get("result_type") != "price_hidden_in_cart":
        return False
    prior_note = row.get("prior_probe_note", "").lower()
    has_prior_evidence = bool(row.get("prior_probe_timestamp")) or "see_price_in_cart" in prior_note
    return bool(row.get("product_url") and has_prior_evidence)


def _cart_empty_text(text: str) -> bool:
    lowered = text.lower()
    return "cart is empty" in lowered or "your cart is empty" in lowered or "0 items" in lowered


def _cart_has_unrelated_items(text: str) -> bool:
    lowered = text.lower()
    return "checkout" in lowered and not _cart_empty_text(text)


if __name__ == "__main__":
    sys.exit(main())
