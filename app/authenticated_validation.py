from __future__ import annotations

import csv
from pathlib import Path

from app.price_forensics import PriceCandidateRole, PriceEvidence
from app.schemas.product_observation import (
    PageClassification,
    ParseConfidence,
    ProductObservation,
    SessionStatus,
)
from app.validation import ValidationPart

AUTHENTICATED_SUMMARY_FIELDNAMES = [
    "validation_order",
    "test_case_id",
    "manufacturer",
    "oem_part_number",
    "test_purpose",
    "checked_at",
    "http_status",
    "page_classification",
    "session_status",
    "observed_part_number",
    "product_name",
    "selling_price_raw",
    "selling_price",
    "price_visibility",
    "availability_raw",
    "availability_status",
    "supersession_detected",
    "superseded_by_raw",
    "price_source_category",
    "price_source_locations",
    "price_corroboration_count",
    "price_parse_confidence",
    "parse_confidence",
    "parse_warning_count",
    "parse_warnings",
    "observation_json_path",
]


def update_authenticated_summary(
    summary_path: Path,
    manifest_part: ValidationPart,
    observation: ProductObservation,
    price_evidence: PriceEvidence,
    observation_json_path: Path,
) -> None:
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    rows = _read_rows(summary_path)
    rows = [row for row in rows if row.get("oem_part_number") != manifest_part.oem_part_number]
    rows.append(_summary_row(manifest_part, observation, price_evidence, observation_json_path))
    rows.sort(key=lambda row: int(row["validation_order"]))
    with summary_path.open("w", newline="", encoding="utf-8") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=AUTHENTICATED_SUMMARY_FIELDNAMES)
        writer.writeheader()
        writer.writerows(rows)


def write_authenticated_review(
    review_path: Path,
    manifest_parts: list[ValidationPart],
    summary_path: Path,
) -> None:
    rows_by_part = {row["oem_part_number"]: row for row in _read_rows(summary_path)}
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
                f"Session status: {row['session_status']}",
                f"Product: {row['product_name']}",
                f"Observed part number: {row['observed_part_number']}",
                f"Selling price: {row['selling_price_raw'] or row['selling_price']}",
                f"Price source: {row['price_source_category']}",
                f"Corroborating sources: {row['price_source_locations']}",
                f"Availability: {row['availability_raw']}",
                f"Superseded: {row['supersession_detected']} {row['superseded_by_raw']}",
                f"Price confidence: {row['price_parse_confidence']}",
                f"Overall confidence: {row['parse_confidence']}",
                f"Warnings: {row['parse_warnings'] or 'None'}",
                "",
            ]
        )

    stats = _review_stats(list(rows_by_part.values()), total=len(manifest_parts))
    lines.extend(
        [
            f"Total cases: {stats['total']}",
            f"Completed: {stats['completed']}",
            f"Authenticated: {stats['authenticated']}",
            f"Normal products: {stats['normal_products']}",
            f"Prices found: {stats['prices_found']}",
            f"High price confidence: {stats['high_price_confidence']}",
            f"Medium price confidence: {stats['medium_price_confidence']}",
            f"Low price confidence: {stats['low_price_confidence']}",
            f"Conflicting prices: {stats['conflicting_prices']}",
            f"Cases with warnings: {stats['cases_with_warnings']}",
            f"Blocked: {stats['blocked']}",
            f"Challenges: {stats['challenges']}",
            f"Navigation errors: {stats['navigation_errors']}",
        ]
    )
    review_path.parent.mkdir(parents=True, exist_ok=True)
    review_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _summary_row(
    manifest_part: ValidationPart,
    observation: ProductObservation,
    price_evidence: PriceEvidence,
    observation_json_path: Path,
) -> dict[str, str]:
    data = observation.to_json_dict()
    selected = _selected_selling_candidate(price_evidence)
    locations = selected.source_locations if selected else []
    return {
        "validation_order": str(manifest_part.validation_order),
        "test_case_id": manifest_part.test_case_id,
        "manufacturer": manifest_part.manufacturer,
        "oem_part_number": manifest_part.oem_part_number,
        "test_purpose": manifest_part.test_purpose,
        "checked_at": data["checked_at"] or "",
        "http_status": "" if data["http_status"] is None else str(data["http_status"]),
        "page_classification": data["page_classification"],
        "session_status": data["session_status"],
        "observed_part_number": data["observed_part_number"] or "",
        "product_name": data["product_name"] or "",
        "selling_price_raw": data["selling_price_raw"] or "",
        "selling_price": data["selling_price"] or "",
        "price_visibility": data["price_visibility"],
        "availability_raw": data["availability_raw"] or "",
        "availability_status": data["availability_status"],
        "supersession_detected": str(data["supersession_detected"]),
        "superseded_by_raw": data["superseded_by_raw"] or "",
        "price_source_category": selected.source_type.value if selected else "",
        "price_source_locations": "; ".join(locations),
        "price_corroboration_count": str(selected.corroboration_count if selected else 0),
        "price_parse_confidence": data["price_parse_confidence"],
        "parse_confidence": data["parse_confidence"],
        "parse_warning_count": str(len(data["parse_warnings"])),
        "parse_warnings": "; ".join(data["parse_warnings"]),
        "observation_json_path": str(observation_json_path),
    }


def _selected_selling_candidate(price_evidence: PriceEvidence):
    if price_evidence.selected_selling_price is None:
        return None
    for candidate in price_evidence.price_candidates:
        if (
            candidate.candidate_role == PriceCandidateRole.SELLING_PRICE
            and candidate.normalized_value == price_evidence.selected_selling_price
        ):
            return candidate
    return None


def _read_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists() or path.stat().st_size == 0:
        return []
    with path.open("r", newline="", encoding="utf-8-sig") as csv_file:
        return list(csv.DictReader(csv_file))


def _review_stats(rows: list[dict[str, str]], total: int) -> dict[str, int]:
    return {
        "total": total,
        "completed": len(rows),
        "authenticated": sum(1 for row in rows if row.get("session_status") == SessionStatus.AUTHENTICATED.value),
        "normal_products": sum(1 for row in rows if row.get("page_classification") == PageClassification.NORMAL_PRODUCT.value),
        "prices_found": sum(1 for row in rows if row.get("selling_price")),
        "high_price_confidence": sum(1 for row in rows if row.get("price_parse_confidence") == ParseConfidence.HIGH.value),
        "medium_price_confidence": sum(1 for row in rows if row.get("price_parse_confidence") == ParseConfidence.MEDIUM.value),
        "low_price_confidence": sum(1 for row in rows if row.get("price_parse_confidence") == ParseConfidence.LOW.value),
        "conflicting_prices": sum(1 for row in rows if "conflicting_selling_price_signals" in row.get("parse_warnings", "")),
        "cases_with_warnings": sum(1 for row in rows if int(row.get("parse_warning_count") or "0") > 0),
        "blocked": sum(1 for row in rows if row.get("page_classification") == PageClassification.BLOCKED.value),
        "challenges": sum(1 for row in rows if row.get("page_classification") == PageClassification.CHALLENGE.value),
        "navigation_errors": sum(
            1 for row in rows if row.get("page_classification") == PageClassification.NAVIGATION_ERROR.value
        ),
    }
