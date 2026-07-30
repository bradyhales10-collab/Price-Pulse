from __future__ import annotations

import csv
import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

from app.config import OUTPUT_DIR
from app.database import cents_to_money

MIN_DELAY_SECONDS = 1


@dataclass(frozen=True)
class PlannedPart:
    run_order: int
    manufacturer: str
    oem_part_number: str
    product_id: int
    listing_id: int
    current_price_cents: int | None
    last_successful_check_at: str | None
    has_current_state: bool


@dataclass
class CollectionPlan:
    competitor_key: str
    input_file: Path
    valid_rows: int
    invalid_rows: int
    unique_oem_parts: int
    database_products_matched: int
    database_listings_matched: int
    missing_products: list[str]
    missing_listings: list[str]
    maximum_allowed_parts: int
    planned_parts: list[PlannedPart]


@dataclass
class CollectionRow:
    run_order: int
    scan_run_id: int
    scan_event_id: int | None
    manufacturer: str
    oem_part_number: str
    normalized_manufacturer: str | None = None
    competitor: str = "partzilla"
    manufacturer_supported: bool = True
    lookup_status: str = ""
    status_reason: str = ""
    observed_part_number: str | None = None
    product_name: str | None = None
    checked_at: str | None = None
    http_status: int | None = None
    page_classification: str | None = None
    session_status: str | None = None
    selling_price: str | None = None
    reference_price: str | None = None
    savings_percent: int | None = None
    price_display_type: str | None = None
    previous_selling_price: str | None = None
    result_type: str = "error"
    price_changed: bool = False
    availability_raw: str | None = None
    previous_availability_status: str | None = None
    availability_status: str | None = None
    supersession_detected: bool = False
    superseded_by_raw: str | None = None
    price_source_category: str | None = None
    price_corroboration_count: int = 0
    price_parse_confidence: str | None = None
    parse_confidence: str | None = None
    warning_count: int = 0
    warnings: str = ""
    observation_json_path: str | None = None


@dataclass
class CollectionRunResult:
    scan_run_id: int
    started_at: str
    completed_at: str | None = None
    run_status: str = "running"
    rows: list[CollectionRow] = field(default_factory=list)
    stop_reason: str | None = None
    last_attempted_part: str | None = None
    export_warning: str | None = None


def validate_delay(delay_seconds: int) -> None:
    if delay_seconds < MIN_DELAY_SECONDS:
        raise ValueError(f"--delay-seconds must be at least {MIN_DELAY_SECONDS}.")


def fingerprint_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def normalize_result_type(result: str) -> str:
    return result.replace(" ", "_")


def stop_status_for(row: CollectionRow) -> str | None:
    if row.page_classification == "blocked" or row.http_status in {401, 403, 429}:
        return "stopped_blocked"
    if row.page_classification == "challenge":
        return "stopped_challenge"
    if row.session_status in {"expired_or_invalid", "authentication_required"}:
        return "failed"
    return None


def plan_collection(conn, input_records, input_file: Path, max_parts: int, invalid_rows: int = 0, *, competitor_key: str = "partzilla") -> CollectionPlan:
    if len(input_records) > max_parts:
        raise ValueError(f"Input has {len(input_records)} valid parts, exceeding --max-parts {max_parts}.")
    seen: set[str] = set()
    planned: list[PlannedPart] = []
    missing_products: list[str] = []
    missing_listings: list[str] = []
    for record in input_records:
        if record.oem_part_number in seen:
            continue
        seen.add(record.oem_part_number)
        product = conn.execute(
            "SELECT * FROM products WHERE manufacturer=? AND normalized_part_number=?",
            (record.manufacturer, record.oem_part_number.strip().upper()),
        ).fetchone()
        if product is None:
            missing_products.append(record.oem_part_number)
            continue
        listing = conn.execute(
            """
            SELECT l.*, s.selling_price_cents, s.last_successful_check_at
            FROM competitor_listings l
            JOIN competitors c ON c.competitor_id=l.competitor_id AND c.competitor_code=?
            LEFT JOIN current_listing_state s ON s.listing_id=l.listing_id
            WHERE l.product_id=?
            """,
            (competitor_key, product["product_id"]),
        ).fetchone()
        if listing is None:
            missing_listings.append(record.oem_part_number)
            continue
        planned.append(
            PlannedPart(
                run_order=len(planned) + 1,
                manufacturer=record.manufacturer,
                oem_part_number=record.oem_part_number,
                product_id=int(product["product_id"]),
                listing_id=int(listing["listing_id"]),
                current_price_cents=listing["selling_price_cents"],
                last_successful_check_at=listing["last_successful_check_at"],
                has_current_state=listing["selling_price_cents"] is not None,
            )
        )
    return CollectionPlan(
        competitor_key=competitor_key,
        input_file=input_file,
        valid_rows=len(input_records),
        invalid_rows=invalid_rows,
        unique_oem_parts=len(seen),
        database_products_matched=len(planned),
        database_listings_matched=len(planned),
        missing_products=missing_products,
        missing_listings=missing_listings,
        maximum_allowed_parts=max_parts,
        planned_parts=planned,
    )


def print_plan(plan: CollectionPlan) -> None:
    print("COLLECTION PLAN")
    print(f"Input file: {plan.input_file}")
    print(f"Valid rows: {plan.valid_rows}")
    print(f"Invalid rows: {plan.invalid_rows}")
    print(f"Unique OEM parts: {plan.unique_oem_parts}")
    print(f"Database products matched: {plan.database_products_matched}")
    print(f"Database listings matched: {plan.database_listings_matched}")
    print(f"Missing products: {', '.join(plan.missing_products) or 'None'}")
    print(f"Missing listings: {', '.join(plan.missing_listings) or 'None'}")
    print(f"Maximum allowed parts: {plan.maximum_allowed_parts}")
    print(f"Actual planned parts: {len(plan.planned_parts)}")
    for part in plan.planned_parts:
        print(
            f"{part.run_order} | {part.manufacturer} | {part.oem_part_number} | "
            f"Current: {cents_to_money(part.current_price_cents) or 'None'} | "
            f"Last check: {part.last_successful_check_at or 'None'} | "
            f"Has current state: {'Yes' if part.has_current_state else 'No'}"
        )


def output_dir_for_run(scan_run_id: int) -> Path:
    return OUTPUT_DIR / "collection_runs" / str(scan_run_id)


def write_collection_outputs(
    *,
    result: CollectionRunResult,
    plan: CollectionPlan,
    delay_seconds: int,
    input_fingerprint: str,
) -> Path:
    output_dir = output_dir_for_run(result.scan_run_id)
    output_dir.mkdir(parents=True, exist_ok=True)
    _write_summary_csv(output_dir / "collection_summary.csv", result.rows)
    _write_review(output_dir / "collection_review.txt", result, plan)
    metadata = {
        "scan_run_id": result.scan_run_id,
        "competitor": plan.competitor_key,
        "input_file": str(plan.input_file),
        "input_file_sha256": input_fingerprint,
        "started_at": result.started_at,
        "completed_at": result.completed_at,
        "run_status": result.run_status,
        "requested_part_count": len(plan.planned_parts),
        "attempted_part_count": len(result.rows),
        "successful_part_count": sum(1 for row in result.rows if row.result_type in SUCCESS_RESULT_TYPES),
        "changed_part_count": sum(1 for row in result.rows if row.result_type in CHANGE_RESULT_TYPES),
        "warning_count": sum(1 for row in result.rows if row.warning_count),
        "blocked_count": sum(1 for row in result.rows if row.result_type == "blocked"),
        "challenge_count": sum(1 for row in result.rows if row.result_type == "challenge"),
        "error_count": sum(1 for row in result.rows if row.result_type in {"navigation_error", "error"}),
        "delay_seconds": delay_seconds,
        "stop_reason": result.stop_reason,
        "last_attempted_part": result.last_attempted_part,
        "export_warning": result.export_warning,
    }
    (output_dir / "run_metadata.json").write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
    return output_dir


SUCCESS_RESULT_TYPES = {"first_observation", "no_change", "price_change", "availability_change", "supersession_change", "multiple_changes"}
CHANGE_RESULT_TYPES = {"first_observation", "price_change", "availability_change", "supersession_change", "multiple_changes"}


def _write_summary_csv(path: Path, rows: list[CollectionRow]) -> None:
    fieldnames = [
        "run_order","scan_run_id","scan_event_id","manufacturer","normalized_manufacturer","competitor",
        "manufacturer_supported","lookup_status","status_reason","oem_part_number","observed_part_number",
        "product_name","checked_at","http_status","page_classification","session_status","selling_price",
        "reference_price","savings_percent","price_display_type",
        "previous_selling_price","result_type","price_changed","availability_raw","previous_availability_status",
        "availability_status","supersession_detected","superseded_by_raw","price_source_category",
        "price_corroboration_count","price_parse_confidence","parse_confidence","warning_count","warnings",
        "observation_json_path",
    ]
    with path.open("w", newline="", encoding="utf-8") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows([row.__dict__ for row in rows])


def _write_review(path: Path, result: CollectionRunResult, plan: CollectionPlan) -> None:
    lines = [
        f"{plan.competitor_key.upper()} COLLECTION RUN",
        f"Scan run ID: {result.scan_run_id}",
        f"Started: {result.started_at}",
        f"Completed: {result.completed_at or ''}",
        f"Run status: {result.run_status}",
        f"Parts requested: {len(plan.planned_parts)}",
        f"Parts attempted: {len(result.rows)}",
        f"Successful: {sum(1 for row in result.rows if row.result_type in SUCCESS_RESULT_TYPES)}",
        f"First observations: {sum(1 for row in result.rows if row.result_type == 'first_observation')}",
        f"No changes: {sum(1 for row in result.rows if row.result_type == 'no_change')}",
        f"Price changes: {sum(1 for row in result.rows if row.result_type == 'price_change')}",
        f"Availability changes: {sum(1 for row in result.rows if row.result_type == 'availability_change')}",
        f"Supersession changes: {sum(1 for row in result.rows if row.result_type == 'supersession_change')}",
        f"Multiple changes: {sum(1 for row in result.rows if row.result_type == 'multiple_changes')}",
        f"No price: {sum(1 for row in result.rows if row.result_type == 'no_price')}",
        f"Not found: {sum(1 for row in result.rows if row.result_type == 'not_found')}",
        f"Manufacturer not carried: {sum(1 for row in result.rows if row.result_type == 'manufacturer_not_carried')}",
        f"Warnings: {sum(1 for row in result.rows if row.warning_count)}",
        f"Blocked: {sum(1 for row in result.rows if row.result_type == 'blocked')}",
        f"Challenges: {sum(1 for row in result.rows if row.result_type == 'challenge')}",
        f"Navigation errors: {sum(1 for row in result.rows if row.result_type == 'navigation_error')}",
        f"Authentication lost: {sum(1 for row in result.rows if row.result_type == 'authentication_lost')}",
        f"Errors: {sum(1 for row in result.rows if row.result_type == 'error')}",
        "",
    ]
    for row in result.rows:
        lines.extend(
            [
                f"PART {row.run_order} OF {len(plan.planned_parts)}",
                f"OEM part number: {row.oem_part_number}",
                f"Product: {row.product_name or ''}",
                f"HTTP status: {row.http_status or ''}",
                f"Session: {row.session_status or ''}",
                f"Selling price: {row.selling_price or ''}",
                f"Reference price: {row.reference_price or ''}",
                f"Savings percent: {row.savings_percent if row.savings_percent is not None else ''}",
                f"Price display: {row.price_display_type or ''}",
                f"Previous price: {row.previous_selling_price or ''}",
                f"Availability: {row.availability_raw or row.availability_status or ''}",
                f"Result: {row.result_type}",
                f"Status reason: {row.status_reason or ''}",
                f"Price confidence: {row.price_parse_confidence or ''}",
                f"Warnings: {row.warnings or 'None'}",
                "",
            ]
        )
    attempted = {row.oem_part_number for row in result.rows}
    unattempted = [part.oem_part_number for part in plan.planned_parts if part.oem_part_number not in attempted]
    if unattempted:
        lines.extend(["UNATTEMPTED PARTS", *unattempted])
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
