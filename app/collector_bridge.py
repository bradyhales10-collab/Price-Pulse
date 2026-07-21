from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path

from app.collection import SUCCESS_RESULT_TYPES
from app.competitors.registry import get_competitor
from app.database import complete_scan_run, connect_database, create_scan_run, persist_observation, seed_competitor, upsert_competitor_listing, utc_now
from app.input_loader import FIELDNAMES
from app.manufacturer_registry import normalize_manufacturer
from app.models import PartRecord
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


@dataclass(frozen=True)
class CollectorImportResult:
    scan_run_id: int
    competitor: str
    rows_received: int
    rows_imported: int
    rows_skipped: int
    successful_rows: int


def selected_parts_csv(database: Path, import_batch_id: int) -> str:
    with connect_database(database) as conn:
        rows = conn.execute(
            """
            SELECT p.manufacturer, p.oem_part_number
            FROM internal_product_state ips
            JOIN products p ON p.product_id=ips.product_id
            WHERE ips.is_active=1 AND ips.source_import_batch_id=?
            ORDER BY COALESCE(ips.scan_priority, ''), p.manufacturer, p.oem_part_number
            """,
            (import_batch_id,),
        ).fetchall()
    lines: list[str] = []
    writer = csv.DictWriter(_ListWriter(lines), fieldnames=FIELDNAMES, lineterminator="\n")
    writer.writeheader()
    for index, row in enumerate(rows, start=1):
        writer.writerow(
            {
                "Test_Case_ID": f"CLOUD-{import_batch_id}-{index}",
                "Manufacturer": row["manufacturer"],
                "OEM_Part_Number": row["oem_part_number"],
                "Search_Observed_Product_Name": "",
                "Search_Observed_MSRP": "",
                "Expected_Partzilla_URL": "",
                "Test_Purpose": "Local collector bridge",
                "Verified_Date": "",
                "Source_URL": "",
            }
        )
    return "".join(lines)


def import_collection_summary(database: Path, *, summary_csv: bytes, fallback_competitor: str | None = None) -> CollectorImportResult:
    decoded = summary_csv.decode("utf-8-sig", errors="replace")
    rows = list(csv.DictReader(decoded.splitlines()))
    competitor_key = _competitor_key(rows, fallback_competitor)
    with connect_database(database) as conn:
        competitor_id = seed_competitor(conn, competitor_key)
        scan_run_id = create_scan_run(conn, competitor_id=competitor_id, requested_part_count=len(rows), run_status="running")
        imported = 0
        successful = 0
        for row in rows:
            product = _find_product(conn, row)
            if product is None:
                continue
            listing_id, _ = upsert_competitor_listing(
                conn,
                product_id=int(product["product_id"]),
                competitor_id=competitor_id,
                competitor_part_number=row.get("observed_part_number") or row.get("oem_part_number") or "",
                canonical_url=_canonical_url(row, competitor_key),
            )
            if _is_manufacturer_not_carried(row):
                _insert_unsupported_event(conn, scan_run_id, listing_id, row)
            else:
                observation = _observation_from_summary_row(row, competitor_key)
                result = persist_observation(
                    conn,
                    scan_run_id=scan_run_id,
                    listing_id=listing_id,
                    observation=observation,
                    observation_json_path=row.get("observation_json_path") or None,
                    price_source_category=row.get("price_source_category") or None,
                )
                if _normalized_result(row.get("result_type")) in SUCCESS_RESULT_TYPES or result in {"first_observation", "no change", "price_change", "availability_change", "supersession_change", "multiple_changes"}:
                    successful += 1
            imported += 1
        complete_scan_run(conn, scan_run_id)
    return CollectorImportResult(
        scan_run_id=scan_run_id,
        competitor=competitor_key,
        rows_received=len(rows),
        rows_imported=imported,
        rows_skipped=len(rows) - imported,
        successful_rows=successful,
    )


class _ListWriter:
    def __init__(self, lines: list[str]):
        self.lines = lines

    def write(self, value: str) -> None:
        self.lines.append(value)


def _competitor_key(rows: list[dict[str, str]], fallback: str | None) -> str:
    for row in rows:
        value = (row.get("competitor") or "").strip().lower()
        if value:
            return value
    return (fallback or "partzilla").strip().lower()


def _find_product(conn, row: dict[str, str]):
    manufacturer = normalize_manufacturer(row.get("manufacturer") or row.get("normalized_manufacturer") or "")
    part = (row.get("oem_part_number") or "").strip().upper()
    if not part:
        return None
    product = conn.execute(
        "SELECT * FROM products WHERE manufacturer=? AND normalized_part_number=?",
        (manufacturer, part),
    ).fetchone()
    if product is not None:
        return product
    matches = conn.execute("SELECT * FROM products WHERE normalized_part_number=?", (part,)).fetchall()
    if len(matches) == 1:
        return matches[0]
    return None


def _canonical_url(row: dict[str, str], competitor_key: str) -> str:
    try:
        adapter = get_competitor(competitor_key)
        return adapter.build_product_url(
            PartRecord(
                test_case_id="",
                manufacturer=normalize_manufacturer(row.get("manufacturer") or ""),
                oem_part_number=row.get("oem_part_number") or "",
                search_observed_product_name="",
                search_observed_msrp="",
                expected_partzilla_url="",
                test_purpose="",
                verified_date="",
                source_url="",
            )
        )
    except Exception:
        return ""


def _is_manufacturer_not_carried(row: dict[str, str]) -> bool:
    return _normalized_result(row.get("result_type")) == "manufacturer_not_carried" or _normalized_result(row.get("lookup_status")) == "manufacturer_not_carried"


def _insert_unsupported_event(conn, scan_run_id: int, listing_id: int, row: dict[str, str]) -> None:
    conn.execute(
        """
        INSERT INTO scan_events(scan_run_id, listing_id, checked_at, http_status, page_classification, session_status,
            navigation_succeeded, price_found, price_parse_confidence, parse_warning_count, parse_warnings,
            observation_json_path, error_message)
        VALUES (?, ?, ?, NULL, 'manufacturer_not_carried', 'not_applicable', 0, 0, 'low', 0, ?, NULL, NULL)
        """,
        (scan_run_id, listing_id, row.get("checked_at") or utc_now(), row.get("status_reason") or "Manufacturer is not carried by this competitor."),
    )


def _observation_from_summary_row(row: dict[str, str], competitor_key: str) -> ProductObservation:
    page_classification = _enum(PageClassification, row.get("page_classification"), PageClassification.UNKNOWN)
    selling_price = _decimal(row.get("selling_price"))
    reference_price = _decimal(row.get("reference_price"))
    return ProductObservation(
        test_case_id=None,
        manufacturer=normalize_manufacturer(row.get("manufacturer") or ""),
        oem_part_number=row.get("oem_part_number") or "",
        observed_part_number=row.get("observed_part_number") or None,
        requested_url=_canonical_url(row, competitor_key),
        final_url=None,
        canonical_url=_canonical_url(row, competitor_key),
        http_status=_int(row.get("http_status")),
        page_title=None,
        page_classification=page_classification,
        price_visibility=PriceVisibility.VISIBLE if selling_price is not None else PriceVisibility.UNKNOWN,
        classification_confidence=_enum(ParseConfidence, row.get("parse_confidence"), ParseConfidence.LOW),
        classification_evidence=[],
        product_name=row.get("product_name") or None,
        manufacturer_display=row.get("manufacturer") or None,
        msrp_raw=row.get("reference_price") or None,
        msrp=reference_price,
        selling_price_raw=row.get("selling_price") or None,
        selling_price=selling_price,
        availability_raw=row.get("availability_raw") or None,
        availability_status=_enum(AvailabilityStatus, row.get("availability_status"), AvailabilityStatus.UNKNOWN),
        shipping_estimate=None,
        access_context=AccessContext.AUTHENTICATED_SESSION if row.get("session_status") == "authenticated" else AccessContext.PUBLIC,
        session_status=_session_status(row.get("session_status")),
        superseded_by_raw=row.get("superseded_by_raw") or None,
        supersession_detected=_bool(row.get("supersession_detected")),
        price_parse_confidence=_enum(ParseConfidence, row.get("price_parse_confidence"), ParseConfidence.LOW),
        price_validation_status=PriceValidationStatus.NOT_MANUALLY_VALIDATED,
        parse_confidence=_enum(ParseConfidence, row.get("parse_confidence"), ParseConfidence.LOW),
        parse_warnings=_warnings(row),
        checked_at=row.get("checked_at") or utc_now(),
        reference_price_raw=row.get("reference_price") or None,
        reference_price=reference_price,
        savings_percent=_int(row.get("savings_percent")),
        savings_amount=None,
        price_display_type=_enum(PriceDisplayType, row.get("price_display_type"), PriceDisplayType.UNKNOWN),
        selling_price_confidence=_enum(ParseConfidence, row.get("price_parse_confidence"), ParseConfidence.LOW),
        reference_price_confidence=_enum(ParseConfidence, row.get("price_parse_confidence"), ParseConfidence.LOW),
    )


def _enum(enum_type, value: str | None, default):
    try:
        return enum_type((value or "").strip())
    except ValueError:
        return default


def _session_status(value: str | None) -> SessionStatus:
    normalized = (value or "").strip()
    if normalized in {"", "public", "not_applicable"}:
        return SessionStatus.UNKNOWN
    return _enum(SessionStatus, normalized, SessionStatus.UNKNOWN)


def _decimal(value: str | None) -> Decimal | None:
    cleaned = (value or "").strip().replace("$", "").replace(",", "")
    if not cleaned:
        return None
    try:
        return Decimal(cleaned)
    except InvalidOperation:
        return None


def _int(value: str | None) -> int | None:
    try:
        return int(str(value or "").strip())
    except ValueError:
        return None


def _bool(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes"}


def _warnings(row: dict[str, str]) -> list[str]:
    raw = row.get("warnings") or ""
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return [item.strip() for item in raw.split(";") if item.strip()]
    if isinstance(parsed, list):
        return [str(item) for item in parsed]
    return [str(parsed)]


def _normalized_result(value: str | None) -> str:
    return (value or "").strip().lower().replace(" ", "_")
