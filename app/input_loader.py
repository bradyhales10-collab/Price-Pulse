from __future__ import annotations

import csv
import logging
from pathlib import Path

from app.manufacturer_registry import normalize_manufacturer
from app.models import InvalidRow, LoadResult, PartRecord

LOGGER = logging.getLogger(__name__)

FIELDNAMES = [
    "Test_Case_ID",
    "Manufacturer",
    "OEM_Part_Number",
    "Search_Observed_Product_Name",
    "Search_Observed_MSRP",
    "Expected_Partzilla_URL",
    "Test_Purpose",
    "Verified_Date",
    "Source_URL",
]


class PartNotFoundError(LookupError):
    """Raised when a requested part number is not present in the input file."""


def _clean_row(row: dict[str, str | None]) -> dict[str, str]:
    return {key: (value or "").strip() for key, value in row.items()}


def _record_from_row(row: dict[str, str]) -> PartRecord:
    return PartRecord(
        test_case_id=row.get("Test_Case_ID", ""),
        manufacturer=normalize_manufacturer(row.get("Manufacturer", "")),
        oem_part_number=row.get("OEM_Part_Number", ""),
        search_observed_product_name=row.get("Search_Observed_Product_Name", ""),
        search_observed_msrp=row.get("Search_Observed_MSRP", ""),
        expected_partzilla_url=row.get("Expected_Partzilla_URL", ""),
        test_purpose=row.get("Test_Purpose", ""),
        verified_date=row.get("Verified_Date", ""),
        source_url=row.get("Source_URL", ""),
    )


def load_parts_csv(path: Path) -> LoadResult:
    records: list[PartRecord] = []
    invalid_rows: list[InvalidRow] = []

    if not path.exists():
        raise FileNotFoundError(f"Input CSV not found: {path}")

    with path.open("r", newline="", encoding="utf-8-sig") as csv_file:
        reader = csv.DictReader(csv_file)
        missing = [field for field in FIELDNAMES if field not in (reader.fieldnames or [])]
        if missing:
            raise ValueError(f"Input CSV is missing required field(s): {', '.join(missing)}")

        for row_number, raw_row in enumerate(reader, start=2):
            row = _clean_row(raw_row)
            manufacturer = row.get("Manufacturer", "")
            part_number = row.get("OEM_Part_Number", "")

            if not manufacturer:
                invalid_rows.append(
                    InvalidRow(row_number=row_number, reason="Blank manufacturer", row=row)
                )
                continue

            if not part_number:
                invalid_rows.append(
                    InvalidRow(row_number=row_number, reason="Blank OEM part number", row=row)
                )
                continue

            records.append(_record_from_row(row))

    if invalid_rows:
        LOGGER.warning("Loaded %s valid records with %s invalid rows.", len(records), len(invalid_rows))
    else:
        LOGGER.info("Loaded %s valid records.", len(records))

    return LoadResult(records=records, invalid_rows=invalid_rows)


def find_part_record(records: list[PartRecord], part_number: str) -> PartRecord:
    requested = part_number.strip()
    for record in records:
        if record.oem_part_number == requested:
            return record
    raise PartNotFoundError(f"Part number not found in input CSV: {requested}")
