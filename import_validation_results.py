from __future__ import annotations

import argparse
import csv
import sys
from decimal import Decimal
from pathlib import Path

from app.config import DEFAULT_DATABASE_PATH
from app.database import (
    complete_scan_run,
    connect_database,
    create_scan_run,
    initialize_database,
    normalize_part_number,
    persist_observation,
    seed_partzilla,
    upsert_product_and_listing,
)
from app.models import PartRecord
from app.schemas.product_observation import (
    AccessContext,
    AvailabilityStatus,
    PageClassification,
    ParseConfidence,
    PriceValidationStatus,
    PriceVisibility,
    ProductObservation,
    SessionStatus,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Import authenticated validation summary into SQLite.")
    parser.add_argument("--file", type=Path, required=True)
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE_PATH)
    args = parser.parse_args()
    initialize_database(args.database)
    rows = _read_rows(args.file)
    imported = skipped = 0
    with connect_database(args.database) as conn:
        with conn:
            competitor_id = seed_partzilla(conn)
            scan_run_id = create_scan_run(conn, competitor_id=competitor_id, requested_part_count=len(rows), run_status="running")
            for row in rows:
                identity = f"validation-import:{Path(row['observation_json_path']).as_posix()}"
                if conn.execute("SELECT 1 FROM scan_events WHERE observation_json_path=?", (identity,)).fetchone():
                    skipped += 1
                    continue
                record = PartRecord(
                    test_case_id=row["test_case_id"],
                    manufacturer=row["manufacturer"],
                    oem_part_number=row["oem_part_number"],
                    search_observed_product_name=row.get("product_name", ""),
                )
                _, listing_id, _, _ = upsert_product_and_listing(conn, record)
                observation = _observation_from_row(row)
                persist_observation(
                    conn,
                    scan_run_id=scan_run_id,
                    listing_id=listing_id,
                    observation=observation,
                    observation_json_path=identity,
                    price_source_category=row.get("price_source_category") or None,
                )
                imported += 1
            complete_scan_run(conn, scan_run_id)
    print(f"Rows read: {len(rows)}")
    print(f"Rows imported: {imported}")
    print(f"Rows skipped as duplicates: {skipped}")
    print(f"Database path: {args.database}")
    return 0


def _read_rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", newline="", encoding="utf-8-sig") as csv_file:
        return list(csv.DictReader(csv_file))


def _observation_from_row(row: dict[str, str]) -> ProductObservation:
    price = Decimal(row["selling_price"]) if row.get("selling_price") else None
    return ProductObservation(
        test_case_id=row.get("test_case_id") or None,
        manufacturer=row["manufacturer"],
        oem_part_number=row["oem_part_number"],
        observed_part_number=row.get("observed_part_number") or None,
        requested_url=f"https://www.partzilla.com/product/kawasaki/{row['oem_part_number']}",
        final_url=f"https://www.partzilla.com/product/kawasaki/{row['oem_part_number']}",
        canonical_url=f"https://www.partzilla.com/product/kawasaki/{row['oem_part_number']}",
        http_status=int(row["http_status"]) if row.get("http_status") else None,
        page_title=None,
        page_classification=PageClassification(row["page_classification"]),
        price_visibility=PriceVisibility(row["price_visibility"]),
        classification_confidence=ParseConfidence.HIGH,
        classification_evidence=[],
        product_name=row.get("product_name") or None,
        manufacturer_display=row["manufacturer"].upper(),
        msrp_raw=None,
        msrp=None,
        selling_price_raw=row.get("selling_price_raw") or (f"${row['selling_price']}" if row.get("selling_price") else None),
        selling_price=price,
        availability_raw=row.get("availability_raw") or None,
        availability_status=AvailabilityStatus(row.get("availability_status") or "unknown"),
        shipping_estimate=None,
        access_context=AccessContext.AUTHENTICATED_SESSION,
        session_status=SessionStatus(row["session_status"]),
        superseded_by_raw=row.get("superseded_by_raw") or None,
        supersession_detected=row.get("supersession_detected") == "True",
        price_parse_confidence=ParseConfidence(row["price_parse_confidence"]),
        price_validation_status=PriceValidationStatus.NOT_MANUALLY_VALIDATED,
        parse_confidence=ParseConfidence(row["parse_confidence"]),
        parse_warnings=[value for value in row.get("parse_warnings", "").split("; ") if value],
        checked_at=row["checked_at"],
    )


if __name__ == "__main__":
    sys.exit(main())
