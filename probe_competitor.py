from __future__ import annotations

import argparse
import csv
import json
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

from app.browser_hygiene import block_tracking_requests
from app.collection import jittered_delay
from app.competitors.base import CompetitorObservation
from app.competitors.registry import get_competitor, select_competitors
from app.config import (
    DATA_DIR,
    DEFAULT_DATABASE_PATH,
    DEFAULT_VIEWPORT,
    ProbeSettings,
    ensure_data_directories,
)
from app.database import connect_database, initialize_database, money_to_cents, normalize_part_number, utc_now
from app.input_loader import load_parts_csv
from app.manufacturer_registry import manufacturer_support_metadata, normalize_manufacturer

DEFAULT_PROBE_MAX_PARTS = 10
HARD_PROBE_MAX_PARTS = 25
MIN_PROBE_DELAY_SECONDS = 5
REVZILLA_MIN_PROBE_DELAY_SECONDS = 1
STOP_CLASSIFICATIONS = {"blocked", "challenge"}
STOP_STATUSES = {401, 403, 429}
PLACEHOLDER_PART_RE = re.compile(r"(^12345$|-MISSING$|^[A-Z]-100$)")
OUTPUT_FIELDS = [
    "run_order",
    "manufacturer",
    "normalized_manufacturer",
    "competitor",
    "manufacturer_supported",
    "lookup_status",
    "status_reason",
    "oem_part_number",
    "url",
    "http_status",
    "page_classification",
    "product_name",
    "selling_price",
    "reference_price",
    "savings_percent",
    "savings_amount",
    "price_visibility",
    "price_display_type",
    "result_type",
    "selling_price_found",
    "reference_price_found",
    "cart_hidden_price",
    "see_price_in_cart_detected",
    "availability_raw",
    "availability_status",
    "parse_confidence",
    "product_association_confirmed",
    "warnings",
]


@dataclass
class ProbeRow:
    run_order: int
    manufacturer: str
    oem_part_number: str
    url: str
    checked_at: str
    observation: CompetitorObservation


@dataclass
class ProbeRun:
    competitor_key: str
    started_at: str
    completed_at: str | None = None
    rows: list[ProbeRow] = field(default_factory=list)
    stop_reason: str | None = None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a safe manual competitor feasibility probe.")
    parser.add_argument("--competitor", required=True)
    parser.add_argument("--file", type=Path, required=True)
    parser.add_argument("--max-parts", type=int, default=DEFAULT_PROBE_MAX_PARTS)
    parser.add_argument("--delay-seconds", type=int, default=MIN_PROBE_DELAY_SECONDS)
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE_PATH)
    parser.add_argument("--save-probe-to-database", action="store_true")
    parser.add_argument("--headless", action="store_true")
    return parser.parse_args()


def validate_probe_args(args: argparse.Namespace) -> None:
    if args.max_parts > HARD_PROBE_MAX_PARTS:
        raise ValueError(f"--max-parts must be {HARD_PROBE_MAX_PARTS} or fewer for competitor probes.")
    if args.max_parts < 1:
        raise ValueError("--max-parts must be at least 1.")
    minimum_delay = _minimum_probe_delay(args.competitor)
    if args.delay_seconds < minimum_delay:
        raise ValueError(f"--delay-seconds must be at least {minimum_delay} for {args.competitor}.")
    select_competitors([args.competitor], probe_mode=True)


def _minimum_probe_delay(competitor_key: str) -> int:
    return REVZILLA_MIN_PROBE_DELAY_SECONDS if competitor_key == "revzilla" else MIN_PROBE_DELAY_SECONDS


def main() -> int:
    args = parse_args()
    try:
        validate_probe_args(args)
    except ValueError as exc:
        print(f"Error: {exc}")
        return 1
    ensure_data_directories()
    if args.save_probe_to_database:
        initialize_database(args.database)
    adapter = get_competitor(args.competitor)
    load_result = load_parts_csv(args.file)
    records = load_result.records[: args.max_parts]
    if not records:
        print("Error: no valid probe parts found.")
        return 1
    output_dir = DATA_DIR / "output" / "competitor_probes" / adapter.competitor_key / utc_now().replace(":", "").replace("-", "")
    output_dir.mkdir(parents=True, exist_ok=True)
    run = ProbeRun(competitor_key=adapter.competitor_key, started_at=utc_now())
    settings = ProbeSettings(headless=bool(args.headless), timeout=30000)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=settings.headless)
        context = browser.new_context(viewport=DEFAULT_VIEWPORT)
        block_tracking_requests(context)
        page = context.new_page()
        page.set_default_timeout(settings.timeout)
        page.set_default_navigation_timeout(settings.timeout)
        for index, record in enumerate(records, start=1):
            if run.rows:
                time.sleep(args.delay_seconds)
            support = manufacturer_support_metadata(adapter.competitor_key, record.manufacturer, record.oem_part_number)
            if not support["manufacturer_supported"]:
                observation = _unsupported_observation(adapter.competitor_key, record, support)
                row = ProbeRow(index, record.manufacturer, record.oem_part_number, "", utc_now(), observation)
                run.rows.append(row)
                print(f"[{index}/{len(records)}] {record.oem_part_number} |  | MANUFACTURER_NOT_CARRIED")
                continue
            url = adapter.build_product_url(record)
            checked_at = utc_now()
            status = None
            final_url = url
            html = ""
            text = ""
            followed_url = None
            try:
                response = page.goto(url, wait_until="domcontentloaded", timeout=settings.timeout)
                status = response.status if response is not None else None
                page.wait_for_timeout(settings.render_settle_ms)
                final_url = page.url
                html = page.content()
                text = page.locator("body").inner_text(timeout=5000) if page.locator("body").count() else ""

                # Some competitors cannot build a product URL from a part number
                # and have to search first, then open the matching result. That
                # costs one extra page load, so the same delay is applied again.
                resolver = getattr(adapter, "search_result_product_url", None)
                if resolver is not None and status not in STOP_STATUSES:
                    followed_url = resolver(html, record)
                    if followed_url and followed_url != final_url:
                        # A search-based lookup is two requests, so the gap is
                        # applied between them as well. Without this, each part
                        # produces a back-to-back burst, which is the pattern a
                        # rate limiter notices first.
                        time.sleep(jittered_delay(args.delay_seconds))
                        response = page.goto(followed_url, wait_until="domcontentloaded", timeout=settings.timeout)
                        status = response.status if response is not None else status
                        page.wait_for_timeout(settings.render_settle_ms)
                        final_url = page.url
                        html = page.content()
                        text = page.locator("body").inner_text(timeout=5000) if page.locator("body").count() else ""
            except (PlaywrightTimeoutError, PlaywrightError, Exception) as exc:
                text = f"Navigation error: {exc}"
            observation = adapter.parse_product_page(html, record, visible_text=text, final_url=final_url, http_status=status)
            row = ProbeRow(index, record.manufacturer, record.oem_part_number, followed_url or url, checked_at, observation)
            run.rows.append(row)
            print(f"[{index}/{len(records)}] {record.oem_part_number} | {observation.selling_price or ''} | {observation.page_classification}")
            if status in STOP_STATUSES or observation.page_classification in STOP_CLASSIFICATIONS:
                run.stop_reason = f"Stopped on {status or observation.page_classification}"
                break
        context.close()
        browser.close()
    run.completed_at = utc_now()
    _write_outputs(output_dir, run, args)
    if args.save_probe_to_database:
        _save_probe_results(args.database, run)
    print(f"Probe output: {output_dir}")
    return 0


def _code_version() -> str:
    """Short git revision, so a probe report identifies the code that produced it."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            cwd=Path(__file__).resolve().parent,
        )
        return result.stdout.strip() or "unknown"
    except Exception:
        return "unknown"


def _write_outputs(output_dir: Path, run: ProbeRun, args: argparse.Namespace) -> None:
    diagnostics_dir = output_dir / "diagnostics"
    diagnostics_dir.mkdir(parents=True, exist_ok=True)
    with (output_dir / "probe_summary.csv").open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=OUTPUT_FIELDS)
        writer.writeheader()
        for row in run.rows:
            writer.writerow(_summary_row(row))
            _write_row_diagnostics(diagnostics_dir, row)
    (output_dir / "probe_metadata.json").write_text(
        json.dumps(
            {
                "competitor_key": run.competitor_key,
                "input_file": str(args.file),
                "started_at": run.started_at,
                "completed_at": run.completed_at,
                "requested_max_parts": args.max_parts,
                "attempted_parts": len(run.rows),
                "delay_seconds": args.delay_seconds,
                "saved_to_database": bool(args.save_probe_to_database),
                "stop_reason": run.stop_reason,
                "code_version": _code_version(),
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    (output_dir / "probe_review.txt").write_text(_review_text(run), encoding="utf-8")


def _summary_row(row: ProbeRow) -> dict[str, object]:
    observation = row.observation
    return {
        "run_order": row.run_order,
        "manufacturer": row.manufacturer,
        "normalized_manufacturer": normalize_manufacturer(row.manufacturer),
        "competitor": observation.competitor_key,
        "manufacturer_supported": observation.page_classification != "manufacturer_not_carried",
        "lookup_status": _result_type(observation),
        "status_reason": "; ".join(observation.warnings) if observation.page_classification == "manufacturer_not_carried" else "",
        "oem_part_number": row.oem_part_number,
        "url": row.url,
        "http_status": observation.http_status or "",
        "page_classification": observation.page_classification,
        "product_name": observation.product_name or "",
        "selling_price": observation.selling_price or "",
        "reference_price": observation.reference_price or "",
        "savings_percent": observation.savings_percent if observation.savings_percent is not None else "",
        "savings_amount": observation.savings_amount or "",
        "price_visibility": observation.price_visibility,
        "price_display_type": observation.price_display_type,
        "result_type": _result_type(observation),
        "selling_price_found": observation.selling_price is not None,
        "reference_price_found": observation.reference_price is not None,
        "cart_hidden_price": _cart_hidden_price(observation),
        "see_price_in_cart_detected": _see_price_in_cart_detected(observation),
        "availability_raw": observation.availability_raw or "",
        "availability_status": observation.availability_status,
        "parse_confidence": observation.parse_confidence,
        "product_association_confirmed": _association_confirmed(observation),
        "warnings": "; ".join(observation.warnings),
    }


def _review_text(run: ProbeRun) -> str:
    rows = run.rows
    warnings = sum(1 for row in rows if row.observation.warnings)
    blocked = sum(1 for row in rows if row.observation.page_classification == "blocked")
    challenges = sum(1 for row in rows if row.observation.page_classification == "challenge")
    errors = sum(1 for row in rows if row.observation.page_classification in {"unknown", "navigation_error"})
    not_found = sum(1 for row in rows if row.observation.page_classification == "not_found")
    http_200_product_pages = sum(1 for row in rows if row.observation.http_status == 200 and row.observation.page_classification == "normal_product")
    association_confirmed = sum(1 for row in rows if _association_confirmed(row.observation))
    successful = sum(1 for row in rows if _valid_price_row(row))
    selling_prices_found = sum(1 for row in rows if row.observation.selling_price is not None)
    reference_prices_found = sum(1 for row in rows if row.observation.reference_price is not None)
    cart_hidden_prices = sum(1 for row in rows if _cart_hidden_price(row.observation))
    any_price_or_reference_found = sum(1 for row in rows if row.observation.selling_price is not None or row.observation.reference_price is not None)
    high_confidence_prices = sum(1 for row in rows if _valid_price_row(row) and row.observation.parse_confidence == "high")
    ambiguous_prices = sum(1 for row in rows if _ambiguous_price_row(row))
    global_promo_leak_suspected = _global_promo_leak_suspected(rows)
    placeholder_parts = placeholder_part_numbers(rows)
    lines = [
        f"{run.competitor_key.upper()} COMPETITOR PROBE",
        "",
        f"Code version: {_code_version()}",
        f"Started: {run.started_at}",
        f"Completed: {run.completed_at or ''}",
        f"Parts requested: {len(rows)}",
        f"Parts attempted: {len(rows)}",
        f"HTTP 200 product pages: {http_200_product_pages}",
        f"Successful selling price observations: {successful}",
        f"Product association confirmed: {association_confirmed}",
        f"Not found: {not_found}",
        f"Visible selling prices found: {selling_prices_found}",
        f"Reference prices found: {reference_prices_found}",
        f"Cart-hidden price pages: {cart_hidden_prices}",
        f"Any price/reference found: {any_price_or_reference_found}",
        f"High-confidence prices: {high_confidence_prices}",
        f"Ambiguous prices: {ambiguous_prices}",
        f"Discounted products: {sum(1 for row in rows if row.observation.price_display_type == 'discounted')}",
        f"Availability found: {sum(1 for row in rows if row.observation.availability_raw)}",
        f"Warnings: {warnings}",
        f"Blocked: {blocked}",
        f"Challenges: {challenges}",
        f"Errors: {errors}",
        f"Global promo leak suspected: {'yes' if global_promo_leak_suspected else 'no'}",
        "",
    ]
    for row in rows:
        obs = row.observation
        lines.extend(
            [
                f"OEM part number: {row.oem_part_number}",
                f"URL: {row.url}",
                f"HTTP status: {obs.http_status or ''}",
                f"Product: {obs.product_name or ''}",
                f"Product association confirmed: {_association_confirmed(obs)}",
                f"Price visibility: {obs.price_visibility}",
                f"Result type: {_result_type(obs)}",
                f"Selling price: {obs.selling_price or ''}",
                f"Reference price: {obs.reference_price or ''}",
                f"Savings percent: {obs.savings_percent if obs.savings_percent is not None else ''}",
                f"Savings amount: {obs.savings_amount or ''}",
                f"Availability: {obs.availability_raw or ''}",
                f"Parse confidence: {obs.parse_confidence}",
                f"Warnings: {'; '.join(obs.warnings) or 'None'}",
                "",
            ]
        )
    # A competitor can be read perfectly and still yield no prices, because
    # every tested listing was discontinued or out of stock. That is an
    # inventory finding, not a parser failure, and must not be reported as one.
    supported_rows = [row for row in rows if row.observation.page_classification != "manufacturer_not_carried"]
    gated_unavailable = [
        row
        for row in supported_rows
        if any(warning.startswith("price_ignored_") for warning in row.observation.warnings)
    ]
    all_unavailable = bool(supported_rows) and len(gated_unavailable) == len(supported_rows)

    recommendation = "Continue fixture review before any production consideration."
    if rows and successful == len(rows) and not warnings and not blocked and not challenges:
        recommendation = "Controlled probe is clean; next step is a larger manual validation set, not production activation."
    if all_unavailable and not global_promo_leak_suspected:
        recommendation = (
            "Pages were read correctly and part numbers matched, but every tested part was "
            "discontinued or out of stock, so no comparable price exists. Re-run against parts "
            "that are currently in stock before judging this competitor. If most listings are "
            "unavailable, this competitor has little pricing value regardless of parser quality."
        )
    access_feasibility = "promising" if rows and not blocked and not challenges and not errors else "needs_review"
    if not rows:
        access_feasibility = "not_evaluated"
    url_predictability = "partial" if not_found else "promising"
    parser_quality = "failed_pending_fix" if global_promo_leak_suspected or (rows and successful == 0) else "needs_validation"
    if rows and successful == len(rows) and not warnings:
        parser_quality = "promising"
    if all_unavailable and not global_promo_leak_suspected:
        parser_quality = "working_no_sellable_inventory"
    lines.extend(
        [
            f"{run.competitor_key.upper()} COVERAGE SUMMARY",
            "",
            "Input quality: placeholder/test file detected" if placeholder_parts else "Input quality: clean realistic test file",
            "WARNING: This probe input contains placeholder part numbers and should not be used for competitor coverage measurement." if placeholder_parts else "",
            f"Placeholder parts detected: {', '.join(placeholder_parts)}" if placeholder_parts else "",
            f"Direct URL success rate: {_rate(http_200_product_pages, len(rows))}",
            f"Visible selling price rate: {_rate(selling_prices_found, len(rows))}",
            f"Cart-hidden rate: {_rate(cart_hidden_prices, len(rows))}",
            f"Not-found rate: {_rate(not_found, len(rows))}",
            f"Ambiguous price rate: {_rate(ambiguous_prices, len(rows))}",
            f"High-confidence visible price rate: {_rate(high_confidence_prices, len(rows))}",
            f"Parser warning rate: {_rate(warnings, len(rows))}",
            f"Availability coverage rate: {_rate(sum(1 for row in rows if row.observation.availability_raw), len(rows))}",
            f"Unavailable-listing rate: {_rate(len(gated_unavailable), len(supported_rows))}",
            "",
            "Manufacturer-level summary:",
            "Manufacturer | Attempted | HTTP 200 product pages | Visible prices | Cart-hidden prices | Not found | Ambiguous | Warnings",
            *_manufacturer_summary_lines(rows),
            "",
            f"Coverage should be evaluated before promoting {run.competitor_key} from a probe to a production collector.",
            "",
            "FEASIBILITY ASSESSMENT",
            "",
            f"Access feasibility: {access_feasibility}.",
            "URL predictability: not all tested parts resolved to a product page. Additional URL or search fallback testing is needed."
            if url_predictability == "partial"
            else "URL predictability: direct part-number URLs worked for the rows attempted in this controlled probe.",
            "Price visibility: evaluated from public page content only.",
            f"Parser quality: {parser_quality}. See per-part confidence and diagnostics above.",
            "Discounted-price handling: uses current/reference price roles plus % off and Save $ evidence.",
            "Availability handling: prices are only accepted for listings that are positively in stock.",
            f"Observed access problems: {run.stop_reason or 'None recorded by this probe.'}",
            f"Recommended next step: {recommendation}",
            "",
            f"{run.competitor_key} is experimental_probe and is not production-ready.",
        ]
    )
    return "\n".join(lines) + "\n"


def _write_row_diagnostics(diagnostics_dir: Path, row: ProbeRow) -> None:
    observation = row.observation
    evidence = observation.raw_evidence_summary or {}
    row_dir = diagnostics_dir / f"{row.run_order:03d}_{_safe_name(row.oem_part_number)}"
    row_dir.mkdir(parents=True, exist_ok=True)
    (row_dir / "page_classification.txt").write_text(observation.page_classification + "\n", encoding="utf-8")
    (row_dir / "selected_product_region.txt").write_text(str(evidence.get("selected_product_region") or "") + "\n", encoding="utf-8")
    (row_dir / "product_association.json").write_text(
        json.dumps(evidence.get("product_association") or {}, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    (row_dir / "price_evidence.json").write_text(
        json.dumps(evidence.get("price_evidence") or {}, indent=2, default=str) + "\n",
        encoding="utf-8",
    )


def _safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value.strip())[:80] or "part"


def _association_confirmed(observation: CompetitorObservation) -> bool:
    association = (observation.raw_evidence_summary or {}).get("product_association") or {}
    return bool(association.get("confirmed"))


def _rate(numerator: int, denominator: int) -> str:
    if denominator <= 0:
        return "0.0% (0/0)"
    return f"{(numerator / denominator) * 100:.1f}% ({numerator}/{denominator})"


def placeholder_part_numbers(rows: list[ProbeRow]) -> list[str]:
    return sorted({row.oem_part_number for row in rows if PLACEHOLDER_PART_RE.search(row.oem_part_number.strip().upper())})


def _manufacturer_summary_lines(rows: list[ProbeRow]) -> list[str]:
    manufacturers = sorted({row.manufacturer for row in rows})
    lines: list[str] = []
    for manufacturer in manufacturers:
        items = [row for row in rows if row.manufacturer == manufacturer]
        lines.append(
            " | ".join(
                [
                    manufacturer,
                    str(len(items)),
                    str(sum(1 for row in items if row.observation.http_status == 200 and row.observation.page_classification == "normal_product")),
                    str(sum(1 for row in items if row.observation.selling_price is not None and row.observation.price_visibility == "visible")),
                    str(sum(1 for row in items if _cart_hidden_price(row.observation))),
                    str(sum(1 for row in items if row.observation.page_classification == "not_found")),
                    str(sum(1 for row in items if _ambiguous_price_row(row))),
                    str(sum(1 for row in items if row.observation.warnings)),
                ]
            )
        )
    return lines


def _result_type(observation: CompetitorObservation) -> str:
    if observation.page_classification == "manufacturer_not_carried":
        return "manufacturer_not_carried"
    if observation.page_classification == "not_found":
        return "not_found"
    if observation.page_classification in {"blocked", "challenge", "navigation_error", "unknown"}:
        return observation.page_classification
    if _cart_hidden_price(observation):
        return "price_hidden_in_cart"
    if observation.selling_price is not None:
        return "selling_price_found"
    if "ambiguous_price_candidates" in observation.warnings:
        return "ambiguous_price"
    if observation.reference_price is not None:
        return "reference_only"
    return "no_price_found"


def _unsupported_observation(competitor_key: str, record, support: dict[str, object]) -> CompetitorObservation:
    return CompetitorObservation(
        competitor_key=competitor_key,
        manufacturer=record.manufacturer,
        oem_part_number=record.oem_part_number,
        page_classification="manufacturer_not_carried",
        session_status="not_applicable",
        price_visibility="not_applicable",
        price_display_type="not_applicable",
        warnings=[str(support["status_reason"])],
        parser_version="coverage-gate-v1",
        raw_evidence_summary=dict(support),
        parse_confidence="high",
    )


def _cart_hidden_price(observation: CompetitorObservation) -> bool:
    return observation.price_visibility == "see_price_in_cart" or observation.price_display_type == "cart_price_hidden"


def _see_price_in_cart_detected(observation: CompetitorObservation) -> bool:
    price_evidence = (observation.raw_evidence_summary or {}).get("price_evidence") or {}
    return bool(price_evidence.get("see_price_in_cart_detected") or observation.price_visibility == "see_price_in_cart")


def _valid_price_row(row: ProbeRow) -> bool:
    observation = row.observation
    critical_warnings = {"product_association_not_confirmed", "selling_price_not_found", "ambiguous_price_candidates", "not_found", "blocked", "challenge", "selling_price_hidden_in_cart"}
    return (
        observation.page_classification == "normal_product"
        and _association_confirmed(observation)
        and observation.selling_price is not None
        and observation.price_visibility == "visible"
        and observation.product_name not in {None, "", "Skip to content"}
        and observation.parse_confidence in {"medium", "high"}
        and not critical_warnings.intersection(observation.warnings)
    )


def _ambiguous_price_row(row: ProbeRow) -> bool:
    observation = row.observation
    return (
        observation.page_classification == "normal_product"
        and _association_confirmed(observation)
        and observation.selling_price is None
        and "ambiguous_price_candidates" in observation.warnings
    )


def _global_promo_leak_suspected(rows: list[ProbeRow]) -> bool:
    if len(rows) < 2:
        return False
    observed_percents = [row.observation.savings_percent for row in rows if row.observation.savings_percent is not None]
    if len(observed_percents) != len(rows) or len(set(observed_percents)) != 1:
        return False
    return any(not _association_confirmed(row.observation) or row.observation.page_classification != "normal_product" for row in rows)


def _save_probe_results(database: Path, run: ProbeRun) -> None:
    with connect_database(database) as conn:
        for row in run.rows:
            obs = row.observation
            product = conn.execute(
                "SELECT product_id FROM products WHERE manufacturer=? AND normalized_part_number=?",
                (row.manufacturer, normalize_part_number(row.oem_part_number)),
            ).fetchone()
            conn.execute(
                """
                INSERT INTO competitor_probe_results(
                    competitor_key, product_id, manufacturer, oem_part_number, url, checked_at, http_status,
                    page_classification, selling_price_cents, reference_price_cents, savings_percent,
                    price_visibility, price_display_type, result_type,
                    availability_raw, availability_status, parse_confidence, warnings_json, raw_result_json, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run.competitor_key,
                    int(product["product_id"]) if product else None,
                    row.manufacturer,
                    row.oem_part_number,
                    row.url,
                    row.checked_at,
                    obs.http_status,
                    obs.page_classification,
                    money_to_cents(obs.selling_price),
                    money_to_cents(obs.reference_price),
                    obs.savings_percent,
                    obs.price_visibility,
                    obs.price_display_type,
                    _result_type(obs),
                    obs.availability_raw,
                    obs.availability_status,
                    obs.parse_confidence,
                    json.dumps(obs.warnings),
                    json.dumps(obs.to_json_dict()),
                    utc_now(),
                ),
            )


if __name__ == "__main__":
    sys.exit(main())
