from __future__ import annotations

import csv
import hashlib
import json
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any

from app.config import DATA_DIR
from app.database import connect_database, money_to_cents, normalize_part_number, seed_partzilla, upsert_product_and_listing, utc_now
from app.manufacturer_registry import normalize_manufacturer, partzilla_slug_for
from app.models import PartRecord
from app.xlsx_utils import read_rows as read_xlsx_rows
from app.xlsx_utils import sheet_names as xlsx_sheet_names
from app.xlsx_utils import write_workbook


IMPORT_DIR = DATA_DIR / "imports"
MAX_UPLOAD_BYTES = 20 * 1024 * 1024
ALLOWED_EXTENSIONS = {".xlsx", ".csv"}
REJECTED_EXTENSIONS = {".xlsm", ".exe", ".zip"}
DEFAULT_SHEETS = ("Upload Data", "Price Data", "Products", "Pricing", "Sheet1")
REQUIRED_FIELDS = ("internal_sku", "manufacturer", "oem_part_number", "our_current_price")
OPTIONAL_FIELDS = ("product_name", "current_cost", "product_category", "units_sold_12m", "inventory_qty", "scan_priority", "is_active", "discontinued")
ALL_FIELDS = REQUIRED_FIELDS + OPTIONAL_FIELDS
HEADER_ALIASES = {
    "internal_sku": ("internal sku", "sku", "item no", "item number", "prod no", "product no", "product number", "internal_sku"),
    "manufacturer": ("manufacturer", "make", "brand", "oem"),
    "oem_part_number": ("oem part number", "part number", "oem part", "part #", "mf id", "mfr id", "manufacturer id", "manufacturer part number", "oem_part_number"),
    "our_current_price": ("our current price", "current price", "selling price", "retail price", "price", "our price", "our_current_price"),
    "product_name": ("product name", "description", "name", "stock name", "item description", "product_name"),
    # Prefer Calc Cost when both source columns are present. Cost remains a
    # supported fallback for older files that do not include Calc Cost.
    "current_cost": ("current cost", "calc cost", "calculated cost", "cost", "current_cost"),
    "product_category": ("product category", "category", "product_category"),
    "units_sold_12m": ("units sold 12m", "units sold", "12 month sales", "units_sold_12m"),
    "inventory_qty": ("inventory qty", "inventory", "qty on hand", "total qty avail", "total quantity available", "qty available", "available quantity", "inventory_qty"),
    "scan_priority": ("scan priority", "priority", "scan_priority"),
    "is_active": ("is active", "active", "is_active"),
    "discontinued": ("discontinued", "discontinued flag", "is discontinued"),
}


@dataclass(frozen=True)
class UploadResult:
    import_batch_id: int
    original_filename: str
    stored_filename: str
    extension: str
    worksheets: list[str]
    default_worksheet: str | None
    headers: list[str]
    auto_mapping: dict[str, str]


@dataclass(frozen=True)
class ImportPreviewRow:
    source_row_number: int
    values: dict[str, str]
    status: str
    messages: list[str]
    action: str


@dataclass(frozen=True)
class ImportPreview:
    import_batch_id: int
    worksheet_name: str | None
    mapping: dict[str, str]
    rows_read: int
    valid_rows: int
    invalid_rows: int
    new_products: int
    existing_products: int
    new_listings: int
    existing_listings: int
    duplicate_rows: int
    unsupported_manufacturer_mappings: int
    rows: list[ImportPreviewRow]


def save_upload(database: Path, *, filename: str, content: bytes, max_bytes: int = MAX_UPLOAD_BYTES) -> UploadResult:
    extension = Path(filename).suffix.lower()
    if extension in REJECTED_EXTENSIONS or extension not in ALLOWED_EXTENSIONS:
        raise ValueError("Unsupported upload type. Use .xlsx or .csv.")
    if len(content) > max_bytes:
        raise ValueError("Uploaded file is too large.")
    IMPORT_DIR.mkdir(parents=True, exist_ok=True)
    stored_filename = f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}_{secrets.token_hex(8)}{extension}"
    path = IMPORT_DIR / stored_filename
    path.write_bytes(content)
    digest = hashlib.sha256(content).hexdigest()
    uploaded_at = utc_now()
    worksheets = workbook_sheets(path)
    worksheet = default_sheet(worksheets)
    headers = read_headers(path, worksheet)
    mapping = auto_map_headers(headers)
    with connect_database(database) as conn:
        cur = conn.execute(
            """
            INSERT INTO import_batches(original_filename, stored_filename, file_sha256, worksheet_name, uploaded_at, status)
            VALUES (?, ?, ?, ?, ?, 'uploaded')
            """,
            (Path(filename).name, stored_filename, digest, worksheet, uploaded_at),
        )
        batch_id = int(cur.lastrowid)
    return UploadResult(batch_id, Path(filename).name, stored_filename, extension, worksheets, worksheet, headers, mapping)


def workbook_sheets(path: Path) -> list[str]:
    if path.suffix.lower() == ".csv":
        return ["CSV"]
    return xlsx_sheet_names(path)


def default_sheet(sheets: list[str]) -> str | None:
    for preferred in DEFAULT_SHEETS:
        if preferred in sheets:
            return preferred
    return sheets[0] if sheets else None


def read_headers(path: Path, worksheet: str | None) -> list[str]:
    rows = _read_rows(path, worksheet, limit=1)
    return [str(value).strip() if value is not None else "" for value in (rows[0] if rows else [])]


def auto_map_headers(headers: list[str]) -> dict[str, str]:
    mapping: dict[str, str] = {}
    normalized = {_normalize_header(header): header for header in headers if header}
    for field, aliases in HEADER_ALIASES.items():
        for alias in aliases:
            if _normalize_header(alias) in normalized:
                mapping[normalized[_normalize_header(alias)]] = field
                break
    return mapping


def preview_import(database: Path, import_batch_id: int, *, worksheet: str | None = None, mapping: dict[str, str] | None = None) -> ImportPreview:
    batch = _batch(database, import_batch_id)
    path = IMPORT_DIR / batch["stored_filename"]
    worksheet = worksheet or batch["worksheet_name"] or default_sheet(workbook_sheets(path))
    headers = read_headers(path, worksheet)
    mapping = mapping or auto_map_headers(headers)
    rows = _mapped_rows(path, worksheet, mapping)
    duplicate_skus = _duplicates([_sku_key(row) for row in rows])
    duplicate_parts = _duplicates([f"{normalize_manufacturer(row.get('manufacturer', ''))}|{normalize_part_number(row.get('oem_part_number', ''))}" for row in rows])
    preview_rows: list[ImportPreviewRow] = []
    with connect_database(database) as conn:
        for index, row in enumerate(rows, start=2):
            messages = _validate_row(row, duplicate_skus, duplicate_parts)
            manufacturer = normalize_manufacturer(row.get("manufacturer", ""))
            part = row.get("oem_part_number", "")
            existing = conn.execute(
                "SELECT product_id FROM products WHERE manufacturer=? AND normalized_part_number=?",
                (manufacturer, normalize_part_number(part)),
            ).fetchone() if manufacturer and part else None
            action = "update" if existing else "insert"
            preview_rows.append(ImportPreviewRow(index, row, "valid" if not messages else "invalid", messages, action))
    valid_rows = [row for row in preview_rows if row.status == "valid"]
    invalid_rows = [row for row in preview_rows if row.status == "invalid"]
    unsupported = sum(1 for row in valid_rows if partzilla_slug_for(row.values.get("manufacturer", "")) is None)
    with connect_database(database) as conn:
        conn.execute(
            """
            UPDATE import_batches SET worksheet_name=?, validated_at=?, status='validated',
                rows_read=?, valid_rows=?, invalid_rows=?
            WHERE import_batch_id=?
            """,
            (worksheet, utc_now(), len(preview_rows), len(valid_rows), len(invalid_rows), import_batch_id),
        )
    new_listings = 0
    existing_listings = 0
    with connect_database(database) as conn:
        for row in valid_rows:
            manufacturer = normalize_manufacturer(row.values.get("manufacturer", ""))
            part = row.values.get("oem_part_number", "")
            if partzilla_slug_for(manufacturer) is None:
                continue
            listing = conn.execute(
                """
                SELECT l.listing_id FROM competitor_listings l
                JOIN products p ON p.product_id=l.product_id
                WHERE p.manufacturer=? AND p.normalized_part_number=?
                """,
                (manufacturer, normalize_part_number(part)),
            ).fetchone()
            if listing:
                existing_listings += 1
            else:
                new_listings += 1
    return ImportPreview(
        import_batch_id=import_batch_id,
        worksheet_name=worksheet,
        mapping=mapping,
        rows_read=len(preview_rows),
        valid_rows=len(valid_rows),
        invalid_rows=len(invalid_rows),
        new_products=sum(1 for row in valid_rows if row.action == "insert"),
        existing_products=sum(1 for row in valid_rows if row.action == "update"),
        new_listings=new_listings,
        existing_listings=existing_listings,
        duplicate_rows=len(duplicate_skus) + len(duplicate_parts),
        unsupported_manufacturer_mappings=unsupported,
        rows=preview_rows,
    )


def confirm_import(database: Path, import_batch_id: int, *, worksheet: str | None = None, mapping: dict[str, str] | None = None) -> ImportPreview:
    preview = preview_import(database, import_batch_id, worksheet=worksheet, mapping=mapping)
    batch = _batch(database, import_batch_id)
    source_name = str(batch["original_filename"])
    source_type = "csv_upload" if source_name.lower().endswith(".csv") else "excel_upload"
    inserted = 0
    updated = 0
    with connect_database(database) as conn:
        for row in preview.rows:
            if row.status != "valid":
                continue
            values = row.values
            manufacturer = normalize_manufacturer(values["manufacturer"])
            record = PartRecord(
                test_case_id="",
                manufacturer=manufacturer,
                oem_part_number=values["oem_part_number"],
                search_observed_product_name=values.get("product_name") or "",
            )
            product_id, listing_id, product_inserted, _ = upsert_product_and_listing(conn, record)
            if product_inserted:
                inserted += 1
            else:
                updated += 1
            conn.execute(
                """
                UPDATE products SET internal_sku=?, product_name=COALESCE(NULLIF(?, ''), product_name),
                    product_category=COALESCE(NULLIF(?, ''), product_category), updated_at=?
                WHERE product_id=?
                """,
                (values["internal_sku"], values.get("product_name", ""), values.get("product_category", ""), utc_now(), product_id),
            )
            existing_state = conn.execute("SELECT * FROM internal_product_state WHERE product_id=?", (product_id,)).fetchone()
            merged = _merge_internal(existing_state, values)
            conn.execute(
                """
                INSERT INTO internal_product_state(product_id, internal_sku, our_current_price_cents, current_cost_cents,
                    product_category, units_sold_12m, inventory_qty, scan_priority, is_active, source_import_batch_id,
                    source_type, source_name, last_source_sync_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(product_id) DO UPDATE SET
                    internal_sku=excluded.internal_sku,
                    our_current_price_cents=excluded.our_current_price_cents,
                    current_cost_cents=COALESCE(excluded.current_cost_cents, internal_product_state.current_cost_cents),
                    product_category=COALESCE(NULLIF(excluded.product_category, ''), internal_product_state.product_category),
                    units_sold_12m=COALESCE(excluded.units_sold_12m, internal_product_state.units_sold_12m),
                    inventory_qty=COALESCE(excluded.inventory_qty, internal_product_state.inventory_qty),
                    scan_priority=COALESCE(NULLIF(excluded.scan_priority, ''), internal_product_state.scan_priority),
                    is_active=excluded.is_active,
                    source_import_batch_id=excluded.source_import_batch_id,
                    source_type=excluded.source_type,
                    source_name=excluded.source_name,
                    last_source_sync_at=excluded.last_source_sync_at,
                    updated_at=excluded.updated_at
                """,
                (
                    product_id,
                    values["internal_sku"],
                    merged["price"],
                    merged["cost"],
                    merged["category"],
                    merged["units"],
                    merged["inventory"],
                    merged["priority"],
                    merged["active"],
                    import_batch_id,
                    source_type,
                    source_name,
                    utc_now(),
                    utc_now(),
                ),
            )
            conn.execute(
                """
                INSERT INTO import_batch_rows(import_batch_id, source_row_number, product_id, row_status, action, validation_errors_json, source_values_json)
                VALUES (?, ?, ?, 'valid', ?, '[]', ?)
                """,
                (import_batch_id, row.source_row_number, product_id, row.action, json.dumps(values)),
            )
        for row in preview.rows:
            if row.status == "invalid":
                conn.execute(
                    """
                    INSERT INTO import_batch_rows(import_batch_id, source_row_number, row_status, action, validation_errors_json, source_values_json)
                    VALUES (?, ?, 'invalid', 'skip', ?, ?)
                    """,
                    (import_batch_id, row.source_row_number, json.dumps(row.messages), json.dumps(row.values)),
                )
        conn.execute(
            """
            UPDATE import_batches SET imported_at=?, status='imported', inserted_rows=?, updated_rows=?
            WHERE import_batch_id=?
            """,
            (utc_now(), inserted, updated, import_batch_id),
        )
    return preview


def import_history(database: Path) -> list[dict[str, Any]]:
    with connect_database(database) as conn:
        return [dict(row) for row in conn.execute("SELECT * FROM import_batches ORDER BY uploaded_at DESC LIMIT 50")]


def clear_import_history(database: Path) -> int:
    with connect_database(database) as conn:
        rows = conn.execute("SELECT import_batch_id, stored_filename FROM import_batches").fetchall()
        batch_ids = [int(row["import_batch_id"]) for row in rows]
        if batch_ids:
            placeholders = ",".join("?" for _ in batch_ids)
            conn.execute(f"UPDATE internal_product_state SET source_import_batch_id=NULL WHERE source_import_batch_id IN ({placeholders})", batch_ids)
            conn.execute(f"DELETE FROM import_batch_rows WHERE import_batch_id IN ({placeholders})", batch_ids)
            conn.execute(f"DELETE FROM import_batches WHERE import_batch_id IN ({placeholders})", batch_ids)
    for row in rows:
        stored = IMPORT_DIR / str(row["stored_filename"])
        if stored.exists():
            stored.unlink()
    return len(rows)


def write_import_template(path: Path) -> None:
    headers = ["Internal_SKU", "Manufacturer", "OEM_Part_Number", "Our_Current_Price", "Product_Name", "Calc_Cost", "Product_Category", "Units_Sold_12M", "Inventory_Qty", "Scan_Priority", "Is_Active"]
    examples = [
        ("EX-KAW-001", "Kawasaki", "41080-1514", "282.32", "Example Kawasaki Part", "200.00", "Brake", 12, 4, "high", "TRUE"),
        ("EX-HON-001", "Honda", "06115-MCA-000", "19.99", "Example Honda Part", "10.00", "Engine", 9, 2, "medium", "TRUE"),
        ("EX-YAM-001", "Yamaha", "1MC-2835V-00-P4", "44.99", "Example Yamaha Part", "25.00", "Body", 5, 8, "medium", "TRUE"),
        ("EX-SUZ-001", "Suzuki", "S83625RCA000JF", "12.49", "Example Suzuki Part", "6.00", "Service", 3, 10, "low", "TRUE"),
        ("EX-POL-001", "Polaris", "3099", "120.02", "Example Polaris Part", "80.00", "Accessory", 4, 1, "high", "TRUE"),
        ("EX-CAN-001", "Can-Am", "07JAZ-001070A", "8.75", "Example Can-Am Part", "3.00", "Tool", 7, 12, "low", "TRUE"),
    ]
    write_workbook(
        path,
        {
            "Upload Data": [headers, *[list(row) for row in examples]],
            "Instructions": [
                ["Part Pulse Import Template"],
                ["Rows shown on Upload Data are examples only. Replace them with real upload data before importing."],
                ["Required fields: Internal_SKU, Manufacturer, OEM_Part_Number, Our_Current_Price."],
            ],
        },
    )


def validation_errors_csv(preview: ImportPreview) -> str:
    lines = ["source_row_number,validation_errors"]
    for row in preview.rows:
        if row.status == "invalid":
            lines.append(f"{row.source_row_number},\"{'; '.join(row.messages)}\"")
    return "\n".join(lines) + "\n"


def _batch(database: Path, import_batch_id: int) -> dict[str, Any]:
    with connect_database(database) as conn:
        row = conn.execute("SELECT * FROM import_batches WHERE import_batch_id=?", (import_batch_id,)).fetchone()
    if row is None:
        raise ValueError("Import batch not found.")
    return dict(row)


def _mapped_rows(path: Path, worksheet: str | None, mapping: dict[str, str]) -> list[dict[str, str]]:
    raw_rows = _read_rows(path, worksheet)
    if not raw_rows:
        return []
    headers = [str(value).strip() if value is not None else "" for value in raw_rows[0]]
    mapped: list[dict[str, str]] = []
    for source in raw_rows[1:]:
        if not any(value not in (None, "") for value in source):
            continue
        row: dict[str, str] = {}
        for index, header in enumerate(headers):
            field = mapping.get(header)
            if not field:
                continue
            value = source[index] if index < len(source) else ""
            row[field] = "" if value is None else str(value).strip()
        mapped.append(row)
    return mapped


def _read_rows(path: Path, worksheet: str | None, limit: int | None = None) -> list[list[Any]]:
    if path.suffix.lower() == ".csv":
        with path.open(newline="", encoding="utf-8-sig") as file:
            rows = list(csv.reader(file))
            return rows[:limit] if limit else rows
    return read_xlsx_rows(path, worksheet, limit=limit)


def _validate_row(row: dict[str, str], duplicate_skus: set[str], duplicate_parts: set[str]) -> list[str]:
    errors: list[str] = []
    for field in REQUIRED_FIELDS:
        if not row.get(field):
            errors.append(f"{field} is required")
    manufacturer = normalize_manufacturer(row.get("manufacturer", ""))
    part_key = f"{manufacturer}|{normalize_part_number(row.get('oem_part_number', ''))}"
    if _sku_key(row) in duplicate_skus:
        errors.append("duplicate internal SKU")
    if part_key in duplicate_parts:
        errors.append("duplicate manufacturer + OEM part number")
    for field in ("our_current_price", "current_cost"):
        if row.get(field):
            value = _decimal(row[field])
            if value is None or value < 0:
                errors.append(f"{field} must be a non-negative money value")
    for field in ("units_sold_12m", "inventory_qty"):
        if row.get(field) and not re.fullmatch(r"\d+", row[field]):
            errors.append(f"{field} must be a non-negative integer")
    return errors


def _merge_internal(existing: Any, values: dict[str, str]) -> dict[str, Any]:
    return {
        "price": money_to_cents(_decimal(values["our_current_price"])),
        "cost": money_to_cents(_decimal(values.get("current_cost", ""))) if values.get("current_cost") else None,
        "category": values.get("product_category", ""),
        "units": int(values["units_sold_12m"]) if values.get("units_sold_12m") else None,
        "inventory": int(values["inventory_qty"]) if values.get("inventory_qty") else None,
        "priority": values.get("scan_priority", ""),
        "active": _active_value(existing, values),
    }


def _active_value(existing: Any, values: dict[str, str]) -> int:
    if values.get("discontinued"):
        return 1 if values["discontinued"].lower() in {"false", "0", "no", "n"} else 0
    if values.get("is_active"):
        return 0 if values["is_active"].lower() in {"false", "0", "no", "n"} else 1
    if existing is not None:
        try:
            return int(existing["is_active"])
        except (KeyError, TypeError, ValueError):
            pass
    return 1


def _duplicates(values: list[str]) -> set[str]:
    cleaned = [value for value in values if value]
    return {value for value in cleaned if cleaned.count(value) > 1}


def _sku_key(row: dict[str, str]) -> str:
    sku = row.get("internal_sku", "").strip()
    if not sku:
        return ""
    return f"{normalize_manufacturer(row.get('manufacturer', ''))}|{sku}"


def _decimal(value: str) -> Decimal | None:
    try:
        return Decimal(str(value).replace("$", "").replace(",", "").strip()).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError):
        return None


def _normalize_header(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.strip().lower()).strip()
