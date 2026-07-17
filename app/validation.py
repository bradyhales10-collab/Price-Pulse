from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path

from app.models import PartRecord
from app.schemas.product_observation import PageClassification, ParseConfidence, ProductObservation


SUMMARY_FIELDNAMES = [
    "validation_order",
    "test_case_id",
    "manufacturer",
    "oem_part_number",
    "test_purpose",
    "checked_at",
    "http_status",
    "page_classification",
    "price_visibility",
    "observed_part_number",
    "product_name",
    "manufacturer_display",
    "msrp_raw",
    "msrp",
    "selling_price_raw",
    "selling_price",
    "availability_raw",
    "availability_status",
    "shipping_estimate",
    "parse_confidence",
    "parse_warning_count",
    "parse_warnings",
    "canonical_url",
    "final_url",
    "observation_json_path",
]


@dataclass(frozen=True)
class ValidationPart:
    validation_order: int
    test_case_id: str
    manufacturer: str
    oem_part_number: str
    test_purpose: str

    def to_part_record(self) -> PartRecord:
        return PartRecord(
            test_case_id=self.test_case_id,
            manufacturer=self.manufacturer,
            oem_part_number=self.oem_part_number,
            test_purpose=self.test_purpose,
        )


class ValidationPartNotFoundError(LookupError):
    """Raised when a requested validation part is not in the step manifest."""


def load_validation_manifest(path: Path) -> list[ValidationPart]:
    with path.open("r", newline="", encoding="utf-8-sig") as csv_file:
        reader = csv.DictReader(csv_file)
        parts = [
            ValidationPart(
                validation_order=int(row["validation_order"].strip()),
                test_case_id=row["test_case_id"].strip(),
                manufacturer=row["manufacturer"].strip(),
                oem_part_number=row["oem_part_number"].strip(),
                test_purpose=row["test_purpose"].strip(),
            )
            for row in reader
        ]
    return sorted(parts, key=lambda part: part.validation_order)


def find_validation_part(parts: list[ValidationPart], part_number: str) -> ValidationPart:
    requested = part_number.strip()
    for part in parts:
        if part.oem_part_number == requested:
            return part
    raise ValidationPartNotFoundError(f"Part number is not in step3 validation manifest: {requested}")


def update_validation_summary(
    summary_path: Path,
    manifest_part: ValidationPart,
    observation: ProductObservation,
    observation_json_path: Path,
) -> None:
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    rows = _read_summary_rows(summary_path)
    rows = [row for row in rows if row.get("oem_part_number") != manifest_part.oem_part_number]
    rows.append(_summary_row(manifest_part, observation, observation_json_path))
    rows.sort(key=lambda row: int(row["validation_order"]))

    with summary_path.open("w", newline="", encoding="utf-8") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=SUMMARY_FIELDNAMES)
        writer.writeheader()
        writer.writerows(rows)


def write_validation_review(
    review_path: Path,
    manifest_parts: list[ValidationPart],
    summary_path: Path,
) -> None:
    rows_by_part = {row["oem_part_number"]: row for row in _read_summary_rows(summary_path)}
    lines: list[str] = []

    for part in manifest_parts:
        row = rows_by_part.get(part.oem_part_number)
        lines.extend([f"PART {part.validation_order} OF {len(manifest_parts)}"])
        lines.extend([f"OEM part number: {part.oem_part_number}", f"Purpose: {part.test_purpose}"])
        if row is None:
            lines.extend(["Status: NOT YET RUN", ""])
            continue
        lines.extend(
            [
                f"HTTP status: {row['http_status']}",
                f"Page classification: {row['page_classification']}",
                f"Price visibility: {row['price_visibility']}",
                f"Product: {row['product_name']}",
                f"Observed part number: {row['observed_part_number']}",
                f"MSRP: {row['msrp_raw']}",
                f"Selling price: {row['selling_price_raw'] or 'Not publicly visible'}",
                f"Availability: {row['availability_raw']}",
                f"Parse confidence: {row['parse_confidence']}",
                f"Warnings: {row['parse_warnings'] or 'None'}",
                f"Observation JSON path: {row['observation_json_path']}",
                "",
            ]
        )

    stats = _review_stats(list(rows_by_part.values()), total=len(manifest_parts))
    lines.extend(
        [
            f"Total validation cases: {stats['total']}",
            f"Completed: {stats['completed']}",
            f"Normal products: {stats['normal_products']}",
            f"Blocked: {stats['blocked']}",
            f"Challenges: {stats['challenges']}",
            f"Not found: {stats['not_found']}",
            f"Navigation errors: {stats['navigation_errors']}",
            f"High confidence: {stats['high_confidence']}",
            f"Medium confidence: {stats['medium_confidence']}",
            f"Low confidence: {stats['low_confidence']}",
            f"Cases with warnings: {stats['cases_with_warnings']}",
        ]
    )

    review_path.parent.mkdir(parents=True, exist_ok=True)
    review_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _read_summary_rows(summary_path: Path) -> list[dict[str, str]]:
    if not summary_path.exists() or summary_path.stat().st_size == 0:
        return []
    with summary_path.open("r", newline="", encoding="utf-8-sig") as csv_file:
        return list(csv.DictReader(csv_file))


def _summary_row(
    manifest_part: ValidationPart,
    observation: ProductObservation,
    observation_json_path: Path,
) -> dict[str, str]:
    data = observation.to_json_dict()
    return {
        "validation_order": str(manifest_part.validation_order),
        "test_case_id": manifest_part.test_case_id,
        "manufacturer": manifest_part.manufacturer,
        "oem_part_number": manifest_part.oem_part_number,
        "test_purpose": manifest_part.test_purpose,
        "checked_at": data["checked_at"] or "",
        "http_status": "" if data["http_status"] is None else str(data["http_status"]),
        "page_classification": data["page_classification"],
        "price_visibility": data["price_visibility"],
        "observed_part_number": data["observed_part_number"] or "",
        "product_name": data["product_name"] or "",
        "manufacturer_display": data["manufacturer_display"] or "",
        "msrp_raw": data["msrp_raw"] or "",
        "msrp": data["msrp"] or "",
        "selling_price_raw": data["selling_price_raw"] or "",
        "selling_price": data["selling_price"] or "",
        "availability_raw": data["availability_raw"] or "",
        "availability_status": data["availability_status"],
        "shipping_estimate": data["shipping_estimate"] or "",
        "parse_confidence": data["parse_confidence"],
        "parse_warning_count": str(len(data["parse_warnings"])),
        "parse_warnings": "; ".join(data["parse_warnings"]),
        "canonical_url": data["canonical_url"] or "",
        "final_url": data["final_url"] or "",
        "observation_json_path": str(observation_json_path),
    }


def _review_stats(rows: list[dict[str, str]], total: int) -> dict[str, int]:
    return {
        "total": total,
        "completed": len(rows),
        "normal_products": _count_page(rows, PageClassification.NORMAL_PRODUCT.value),
        "blocked": _count_page(rows, PageClassification.BLOCKED.value),
        "challenges": _count_page(rows, PageClassification.CHALLENGE.value),
        "not_found": _count_page(rows, PageClassification.NOT_FOUND.value),
        "navigation_errors": _count_page(rows, PageClassification.NAVIGATION_ERROR.value),
        "high_confidence": _count_confidence(rows, ParseConfidence.HIGH.value),
        "medium_confidence": _count_confidence(rows, ParseConfidence.MEDIUM.value),
        "low_confidence": _count_confidence(rows, ParseConfidence.LOW.value),
        "cases_with_warnings": sum(1 for row in rows if int(row.get("parse_warning_count") or "0") > 0),
    }


def _count_page(rows: list[dict[str, str]], value: str) -> int:
    return sum(1 for row in rows if row.get("page_classification") == value)


def _count_confidence(rows: list[dict[str, str]], value: str) -> int:
    return sum(1 for row in rows if row.get("parse_confidence") == value)
