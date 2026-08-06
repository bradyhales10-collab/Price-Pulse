from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from app.competitors.registry import list_competitors, short_display_name
from app.database import SCHEMA_VERSION, cents_to_money
from app.manufacturer_registry import competitor_supports_manufacturer


class DashboardDatabaseError(RuntimeError):
    pass


@dataclass(frozen=True)
class CatalogFilters:
    search: str = ""
    manufacturer: str = ""
    price_type: str = ""
    availability: str = ""
    superseded: str = ""
    confidence: str = ""
    needs_review: bool = False
    sort: str = "last_checked"
    page: int = 1
    page_size: int = 50


SORT_COLUMNS = {
    "part_number": "p.oem_part_number COLLATE NOCASE",
    "our_price": "ips.our_current_price_cents",
    "lowest_competitor": "lowest_competitor_price_cents",
    "last_checked": "last_checked_at",
}


def connect_readonly(database: Path) -> sqlite3.Connection:
    if not database.exists():
        raise DashboardDatabaseError(f"Database not found: {database}")
    uri = f"file:{database.resolve().as_posix()}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only = ON")
        _validate_schema(conn)
        return conn
    except sqlite3.Error as exc:
        raise DashboardDatabaseError(str(exc)) from exc


def _validate_schema(conn: sqlite3.Connection) -> None:
    row = conn.execute("SELECT MAX(version) version FROM schema_migrations").fetchone()
    version = int(row["version"] or 0)
    if version < SCHEMA_VERSION:
        raise DashboardDatabaseError(f"Unsupported database schema version {version}. Expected {SCHEMA_VERSION}.")


def dashboard_data(database: Path) -> dict[str, Any]:
    with connect_readonly(database) as conn:
        latest_run = _latest_run(conn)
        return {
            "kpis": _kpis(conn),
            "comparison_summary": _comparison_summary(conn),
            "latest_run": latest_run,
            "manufacturers": _manufacturer_counts(conn),
            "oem_snapshots": _oem_snapshots(conn),
            "competitor_snapshots": _competitor_snapshots(conn),
        }


def import_batch_label(database: Path, import_batch_id: int | None) -> dict[str, Any] | None:
    if not import_batch_id:
        return None
    with connect_readonly(database) as conn:
        row = conn.execute(
            """
            SELECT import_batch_id, original_filename, worksheet_name, uploaded_at, valid_rows, inserted_rows, updated_rows
            FROM import_batches
            WHERE import_batch_id=?
            """,
            (import_batch_id,),
        ).fetchone()
    return dict(row) if row else None


def catalog_data(database: Path, filters: CatalogFilters) -> dict[str, Any]:
    filters = _normalized_filters(filters)
    with connect_readonly(database) as conn:
        where, params = _product_catalog_where(filters)
        total = conn.execute(f"""
            SELECT COUNT(*)
            FROM products p
            LEFT JOIN internal_product_state ips ON ips.product_id=p.product_id
            WHERE {where}
        """, params).fetchone()[0]
        rows = _catalog_product_rows(conn, where, params, filters=filters)
        return {
            "products": rows,
            "filters": filters,
            "manufacturers": manufacturers(conn),
            "availability_options": _distinct(conn, "current_listing_state", "availability_status"),
            "total": total,
            "total_pages": max(1, math.ceil(total / filters.page_size)),
        }


def _catalog_competitor_columns_sql() -> str:
    columns: list[str] = []
    for adapter in list_competitors():
        key = adapter.competitor_key
        if not key.replace("_", "").isalnum():
            raise DashboardDatabaseError(f"Unsafe competitor key: {key}")
        columns.extend(
            [
                f"MAX(CASE WHEN c.competitor_code='{key}' THEN s.selling_price_cents END) {key}_selling_price_cents",
                f"MAX(CASE WHEN c.competitor_code='{key}' THEN s.reference_price_cents END) {key}_reference_price_cents",
                f"MAX(CASE WHEN c.competitor_code='{key}' THEN s.savings_percent END) {key}_savings_percent",
                f"MAX(CASE WHEN c.competitor_code='{key}' THEN COALESCE(se.page_classification, s.price_display_type, 'not_checked') END) {key}_status",
                f"MAX(CASE WHEN c.competitor_code='{key}' THEN COALESCE(se.checked_at, s.last_successful_check_at) END) {key}_checked_at",
                f"MAX(CASE WHEN c.competitor_code='{key}' THEN COALESCE(se.parse_warning_count, 0) END) {key}_warning_count",
            ]
        )
    return ",\n               ".join(columns)


def _catalog_product_rows(conn: sqlite3.Connection, where: str, params: list[Any], *, filters: CatalogFilters) -> list[dict[str, Any]]:
    sort_column = SORT_COLUMNS.get(filters.sort, SORT_COLUMNS["last_checked"])
    offset = (filters.page - 1) * filters.page_size
    competitor_columns = _catalog_competitor_columns_sql()
    rows = conn.execute(f"""
        SELECT p.product_id, p.manufacturer, p.oem_part_number,
               COALESCE(ips.internal_sku, p.internal_sku) internal_sku,
               COALESCE(MAX(s.product_name), p.product_name) product_name,
               ips.our_current_price_cents, ips.current_cost_cents,
               ips.units_sold_12m, ips.inventory_qty, ips.scan_priority,
               prd.suggested_new_price_cents,
               MIN(CASE WHEN s.selling_price_cents IS NOT NULL THEN s.selling_price_cents END) lowest_competitor_price_cents,
               (
                   SELECT c2.competitor_name
                   FROM competitor_listings l2
                   JOIN competitors c2 ON c2.competitor_id=l2.competitor_id
                   JOIN current_listing_state s2 ON s2.listing_id=l2.listing_id
                   WHERE l2.product_id=p.product_id AND l2.is_active=1 AND s2.selling_price_cents IS NOT NULL
                   ORDER BY s2.selling_price_cents,
                            CASE c2.competitor_code WHEN 'partzilla' THEN 1 WHEN 'motosport' THEN 2 WHEN 'chaparral' THEN 3 ELSE 99 END,
                            c2.competitor_name COLLATE NOCASE
                   LIMIT 1
               ) lowest_competitor_name,
               MAX(s.last_successful_check_at) last_checked_at,
               {competitor_columns}
        FROM products p
        LEFT JOIN internal_product_state ips ON ips.product_id=p.product_id
        LEFT JOIN competitor_listings l ON l.product_id=p.product_id AND l.is_active=1
        LEFT JOIN competitors c ON c.competitor_id=l.competitor_id
        LEFT JOIN current_listing_state s ON s.listing_id=l.listing_id
        LEFT JOIN pricing_review_decisions prd ON prd.product_id=p.product_id
        LEFT JOIN scan_events se ON se.scan_event_id=(
            SELECT MAX(se2.scan_event_id) FROM scan_events se2 WHERE se2.listing_id=l.listing_id
        )
        WHERE {where}
        GROUP BY p.product_id, p.manufacturer, p.oem_part_number, p.internal_sku, p.product_name,
                 ips.internal_sku, ips.our_current_price_cents, ips.current_cost_cents,
                 ips.units_sold_12m, ips.inventory_qty, ips.scan_priority, prd.suggested_new_price_cents
        ORDER BY {sort_column} DESC NULLS LAST, p.oem_part_number COLLATE NOCASE
        LIMIT ? OFFSET ?
    """, params + [filters.page_size, offset]).fetchall()
    return [_catalog_product_row(row) for row in rows]


def product_detail(database: Path, product_id: int) -> dict[str, Any] | None:
    with connect_readonly(database) as conn:
        rows = conn.execute("""
            SELECT p.product_id, p.manufacturer, p.oem_part_number, p.internal_sku,
                   COALESCE(s.product_name, p.product_name) product_name,
                   ips.our_current_price_cents, ips.current_cost_cents,
                   c.competitor_code, c.competitor_name, l.listing_id, l.canonical_url, l.first_seen_at, l.last_seen_at,
                   s.*,
                   se.checked_at latest_event_at, se.page_classification latest_page_classification,
                   se.session_status latest_session_status, se.parse_warnings latest_parse_warnings
            FROM products p
            JOIN competitor_listings l ON l.product_id=p.product_id
            JOIN competitors c ON c.competitor_id=l.competitor_id
            LEFT JOIN internal_product_state ips ON ips.product_id=p.product_id
            LEFT JOIN current_listing_state s ON s.listing_id=l.listing_id
            LEFT JOIN scan_events se ON se.scan_event_id=(
                SELECT MAX(se2.scan_event_id) FROM scan_events se2 WHERE se2.listing_id=l.listing_id
            )
            WHERE p.product_id=?
            ORDER BY
                CASE
                    WHEN s.selling_price_cents IS NOT NULL THEN 0
                    WHEN se.page_classification='manufacturer_not_carried' THEN 2
                    ELSE 1
                END,
                s.last_successful_check_at DESC NULLS LAST,
                c.competitor_name COLLATE NOCASE
        """, (product_id,)).fetchall()
        if not rows:
            return None
        row = rows[0]
        history = conn.execute("""
            SELECT c.competitor_name, effective_at, change_type, previous_selling_price_cents, new_selling_price_cents,
                   previous_reference_price_cents, new_reference_price_cents, change_details_json
            FROM listing_history h
            JOIN competitor_listings l ON l.listing_id=h.listing_id
            JOIN competitors c ON c.competitor_id=l.competitor_id
            WHERE l.product_id=?
            ORDER BY effective_at DESC
            LIMIT 25
        """, (product_id,)).fetchall()
        events = conn.execute("""
            SELECT c.competitor_name, checked_at, http_status, page_classification, session_status, price_found,
                   price_parse_confidence, parse_warning_count, parse_warnings
            FROM scan_events se
            JOIN competitor_listings l ON l.listing_id=se.listing_id
            JOIN competitors c ON c.competitor_id=l.competitor_id
            WHERE l.product_id=?
            ORDER BY checked_at DESC
            LIMIT 25
        """, (product_id,)).fetchall()
        history_rows = [_history_row(item) for item in history]
        history_competitors, history_grid = _pivot_price_history(history_rows)
        listing_rows = [_catalog_row(item) for item in rows]
        priced = [item for item in rows if item["selling_price_cents"] is not None]
        lowest_row = min(priced, key=lambda item: item["selling_price_cents"]) if priced else None
        lowest_competitor = (
            {
                "competitor_name": lowest_row["competitor_name"],
                "selling_price": cents_to_money(lowest_row["selling_price_cents"]),
                "reference_price": cents_to_money(lowest_row["reference_price_cents"]),
                "selling_price_confidence": lowest_row["selling_price_confidence"],
                "price_display_type": lowest_row["price_display_type"],
            }
            if lowest_row is not None
            else None
        )
        return {
            "product": _catalog_row(row),
            "listing": dict(row),
            "listings": listing_rows,
            "lowest_competitor": lowest_competitor,
            "history": history_rows,
            "history_competitors": history_competitors,
            "history_grid": history_grid,
            "events": [dict(item) for item in events],
        }


def scan_runs(database: Path) -> list[dict[str, Any]]:
    with connect_readonly(database) as conn:
        rows = conn.execute("""
            SELECT r.*, c.competitor_name
            FROM scan_runs r
            JOIN competitors c ON c.competitor_id=r.competitor_id
            ORDER BY r.started_at DESC
            LIMIT 100
        """).fetchall()
        return [_run_row(row) for row in rows]


def scan_run_detail(database: Path, scan_run_id: int) -> dict[str, Any] | None:
    with connect_readonly(database) as conn:
        run = conn.execute("""
            SELECT r.*, c.competitor_name
            FROM scan_runs r
            JOIN competitors c ON c.competitor_id=r.competitor_id
            WHERE r.scan_run_id=?
        """, (scan_run_id,)).fetchone()
        if run is None:
            return None
        events = conn.execute("""
            SELECT se.*, p.manufacturer, p.oem_part_number, COALESCE(s.product_name, p.product_name) product_name,
                   s.selling_price_cents, s.availability_status
            FROM scan_events se
            JOIN competitor_listings l ON l.listing_id=se.listing_id
            JOIN products p ON p.product_id=l.product_id
            LEFT JOIN current_listing_state s ON s.listing_id=l.listing_id
            WHERE se.scan_run_id=?
            ORDER BY se.scan_event_id
        """, (scan_run_id,)).fetchall()
        return {"run": _run_row(run), "events": [_event_row(row) for row in events]}


def quality_data(database: Path) -> dict[str, Any]:
    filters = CatalogFilters(needs_review=True, page_size=100)
    with connect_readonly(database) as conn:
        missing_product_where = """COALESCE(ips.is_active, p.is_active, 1)=1 AND EXISTS (
            SELECT 1 FROM competitor_listings missing_l
            LEFT JOIN current_listing_state missing_s ON missing_s.listing_id=missing_l.listing_id
            LEFT JOIN scan_events missing_se ON missing_se.scan_event_id=(
                SELECT MAX(missing_se2.scan_event_id) FROM scan_events missing_se2 WHERE missing_se2.listing_id=missing_l.listing_id
            )
            WHERE missing_l.product_id=p.product_id
              AND missing_l.is_active=1
              AND missing_s.selling_price_cents IS NULL
              AND COALESCE(missing_se.page_classification, '') <> 'manufacturer_not_carried'
        )"""
        missing_price_products = _catalog_product_rows(conn, missing_product_where, [], filters=CatalogFilters(page_size=100))
        display_names = {
            adapter.competitor_key: short_display_name(adapter) for adapter in list_competitors()
        }
        for row in missing_price_products:
            row["missing_competitors"] = [
                display_names[key]
                for key in display_names
                if competitor_supports_manufacturer(key, str(row.get("manufacturer") or ""))
                and not row[key].get("price")
                and row[key].get("status") != "manufacturer_not_carried"
            ]
        return {
            "missing_price_products": missing_price_products,
            "missing_prices": _quality_rows(conn, "s.selling_price_cents IS NULL AND COALESCE(se.page_classification, '') <> 'manufacturer_not_carried'"),
            "not_carried": _quality_rows(conn, "se.page_classification='manufacturer_not_carried'"),
            "low_confidence": _quality_rows(conn, "COALESCE(s.selling_price_confidence, s.price_parse_confidence)='low'"),
            "failures": _quality_rows(conn, "s.consecutive_failure_count > 0"),
            "warnings": _quality_rows(conn, "COALESCE(se.parse_warning_count, 0) > 0"),
            "supersession": _quality_rows(conn, "s.supersession_detected=1"),
            "needs_review_count": catalog_data(database, filters)["total"],
            "competitors": [dict(row) for row in conn.execute("""
                SELECT competitor_code, competitor_name, status, legal_review_status, requires_login,
                       supports_public_price, supports_direct_part_url, cart_price_probe_status, notes
                FROM competitors
                ORDER BY competitor_name COLLATE NOCASE
            """).fetchall()],
        }


PAGE_SIZE_OPTIONS = (25, 50, 100, 200)
DEFAULT_PAGE_SIZE = 50


def short_competitor_name(value: Any) -> str:
    """Shorten long competitor names for table cells, matching the column
    headings used on the Price Check screen. Names come from the adapter
    registry, so a new competitor only declares its own short name."""
    if not value:
        return ""
    # Imported here to avoid a circular import at module load.
    from app.competitors.registry import competitor_short_names

    return competitor_short_names().get(str(value).strip(), str(value))


def manufacturer_options(database: Path) -> list[str]:
    with connect_readonly(database) as conn:
        return manufacturers(conn)


def manufacturers(conn: sqlite3.Connection) -> list[str]:
    return [row["manufacturer"] for row in conn.execute("SELECT DISTINCT manufacturer FROM products ORDER BY manufacturer COLLATE NOCASE")]


def _kpis(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    latest_warnings = """
        SELECT listing_id, parse_warning_count
        FROM scan_events se
        WHERE se.scan_event_id=(SELECT MAX(se2.scan_event_id) FROM scan_events se2 WHERE se2.listing_id=se.listing_id)
    """
    values = {
        "Monitored Products": conn.execute("SELECT COUNT(DISTINCT product_id) FROM competitor_listings WHERE is_active=1").fetchone()[0],
        "Products With Our Price": conn.execute("SELECT COUNT(*) FROM internal_product_state WHERE our_current_price_cents IS NOT NULL").fetchone()[0],
        "Products With Competitor Price": conn.execute("""
            SELECT COUNT(DISTINCT l.product_id)
            FROM current_listing_state s
            JOIN competitor_listings l ON l.listing_id=s.listing_id
            WHERE s.selling_price_cents IS NOT NULL AND l.is_active=1
        """).fetchone()[0],
        "Discounted Products": conn.execute("""
            SELECT COUNT(DISTINCT l.product_id)
            FROM current_listing_state s
            JOIN competitor_listings l ON l.listing_id=s.listing_id
            WHERE s.price_display_type='discounted' AND l.is_active=1
        """).fetchone()[0],
        "Needs Review": conn.execute(f"""
            SELECT COUNT(DISTINCT l.product_id)
            FROM competitor_listings l
            LEFT JOIN internal_product_state ips ON ips.product_id=(SELECT product_id FROM competitor_listings WHERE listing_id=l.listing_id)
            LEFT JOIN current_listing_state s ON s.listing_id=l.listing_id
            LEFT JOIN ({latest_warnings}) lw ON lw.listing_id=l.listing_id
            WHERE s.selling_price_cents IS NULL
               OR ips.our_current_price_cents IS NULL
               OR COALESCE(s.selling_price_confidence, s.price_parse_confidence)='low'
               OR COALESCE(s.consecutive_failure_count, 0) > 0
               OR COALESCE(lw.parse_warning_count, 0) > 0
        """).fetchone()[0],
        "Manufacturers": conn.execute("SELECT COUNT(DISTINCT manufacturer) FROM products").fetchone()[0],
    }
    links = {
        "Discounted Products": "/products?price_type=discounted",
        "Needs Review": "/products?needs_review=1",
        "Products With Competitor Price": "/products",
        "Products With Our Price": "/comparison",
        "Monitored Products": "/products",
        "Manufacturers": "/products",
    }
    return [{"label": key, "value": value, "href": links[key]} for key, value in values.items()]


def _latest_run(conn: sqlite3.Connection) -> dict[str, Any] | None:
    row = conn.execute("""
        SELECT r.*, c.competitor_name
        FROM scan_runs r
        JOIN competitors c ON c.competitor_id=r.competitor_id
        WHERE r.completed_at IS NOT NULL
        ORDER BY r.completed_at DESC
        LIMIT 1
    """).fetchone()
    return _run_row(row) if row else None


def _composition(conn: sqlite3.Connection) -> dict[str, int]:
    return {
        "regular": conn.execute("SELECT COUNT(*) FROM current_listing_state WHERE price_display_type='regular'").fetchone()[0],
        "discounted": conn.execute("SELECT COUNT(*) FROM current_listing_state WHERE price_display_type='discounted'").fetchone()[0],
        "in_stock": conn.execute("SELECT COUNT(*) FROM current_listing_state WHERE availability_status='in_stock'").fetchone()[0],
        "superseded": conn.execute("SELECT COUNT(*) FROM current_listing_state WHERE supersession_detected=1").fetchone()[0],
    }


def _manufacturer_counts(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return [dict(row) for row in conn.execute("""
        SELECT p.manufacturer, COUNT(DISTINCT p.product_id) count
        FROM products p
        JOIN competitor_listings l ON l.product_id=p.product_id
        WHERE l.is_active=1
        GROUP BY p.manufacturer
        ORDER BY count DESC, p.manufacturer COLLATE NOCASE
    """)]


def _oem_snapshots(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute("""
        WITH product_prices AS (
            SELECT p.product_id, p.manufacturer, ips.our_current_price_cents our_price, ips.current_cost_cents cost,
                   MIN(s.selling_price_cents) lowest_competitor_price,
                   COUNT(DISTINCT l.competitor_id) competitor_count,
                   COUNT(DISTINCT CASE WHEN s.selling_price_cents IS NOT NULL THEN l.competitor_id END) priced_competitor_count
            FROM products p
            LEFT JOIN internal_product_state ips ON ips.product_id=p.product_id
            LEFT JOIN competitor_listings l ON l.product_id=p.product_id AND l.is_active=1
            LEFT JOIN current_listing_state s ON s.listing_id=l.listing_id
            WHERE COALESCE(ips.is_active, p.is_active, 1)=1
            GROUP BY p.product_id, p.manufacturer, ips.our_current_price_cents, ips.current_cost_cents
        )
        SELECT manufacturer,
               COUNT(*) product_count,
               SUM(CASE WHEN lowest_competitor_price IS NOT NULL THEN 1 ELSE 0 END) products_with_competitor_price,
               AVG(CASE WHEN our_price IS NOT NULL AND our_price > 0 AND cost IS NOT NULL THEN (our_price - cost) * 1.0 / our_price END) avg_our_margin,
               AVG(CASE WHEN lowest_competitor_price IS NOT NULL AND lowest_competitor_price > 0 AND cost IS NOT NULL THEN (lowest_competitor_price - cost) * 1.0 / lowest_competitor_price END) avg_lowest_competitor_margin,
               AVG(CASE WHEN our_price IS NOT NULL AND lowest_competitor_price IS NOT NULL THEN (our_price - lowest_competitor_price) / 100.0 END) avg_gap_vs_lowest,
               AVG(CASE WHEN competitor_count > 0 THEN priced_competitor_count * 1.0 / competitor_count END) avg_price_coverage,
               SUM(CASE WHEN our_price IS NOT NULL AND lowest_competitor_price IS NOT NULL AND our_price > lowest_competitor_price THEN 1 ELSE 0 END) our_higher_count,
               SUM(CASE WHEN our_price IS NOT NULL AND lowest_competitor_price IS NOT NULL AND our_price < lowest_competitor_price THEN 1 ELSE 0 END) our_lower_count
        FROM product_prices
        GROUP BY manufacturer
        ORDER BY product_count DESC, manufacturer COLLATE NOCASE
    """).fetchall()
    return [
        {
            "manufacturer": row["manufacturer"],
            "product_count": row["product_count"],
            "products_with_competitor_price": row["products_with_competitor_price"],
            "avg_our_margin": _ratio_percent(row["avg_our_margin"]),
            "avg_lowest_competitor_margin": _ratio_percent(row["avg_lowest_competitor_margin"]),
            "avg_gap_vs_lowest": _decimal_money(row["avg_gap_vs_lowest"]),
            "avg_price_coverage": _ratio_percent(row["avg_price_coverage"]),
            "our_higher_count": row["our_higher_count"],
            "our_lower_count": row["our_lower_count"],
        }
        for row in rows
    ]


def _competitor_snapshots(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute("""
        SELECT c.competitor_code, c.competitor_name,
               COUNT(DISTINCT l.product_id) product_count,
               COUNT(DISTINCT CASE WHEN s.selling_price_cents IS NOT NULL THEN l.product_id END) priced_product_count,
               AVG(CASE WHEN s.selling_price_cents IS NOT NULL AND ips.our_current_price_cents IS NOT NULL AND ips.our_current_price_cents > 0 AND ips.current_cost_cents IS NOT NULL THEN (ips.our_current_price_cents - ips.current_cost_cents) * 1.0 / ips.our_current_price_cents END) avg_our_margin,
               AVG(CASE WHEN s.selling_price_cents IS NOT NULL AND s.selling_price_cents > 0 AND ips.current_cost_cents IS NOT NULL THEN (s.selling_price_cents - ips.current_cost_cents) * 1.0 / s.selling_price_cents END) avg_competitor_margin,
               AVG(CASE WHEN ips.our_current_price_cents IS NOT NULL AND s.selling_price_cents IS NOT NULL THEN (ips.our_current_price_cents - s.selling_price_cents) / 100.0 END) avg_gap,
               SUM(CASE WHEN ips.our_current_price_cents IS NOT NULL AND s.selling_price_cents IS NOT NULL AND ips.our_current_price_cents > s.selling_price_cents THEN 1 ELSE 0 END) our_higher_count,
               SUM(CASE WHEN ips.our_current_price_cents IS NOT NULL AND s.selling_price_cents IS NOT NULL AND ips.our_current_price_cents < s.selling_price_cents THEN 1 ELSE 0 END) our_lower_count
        FROM competitors c
        LEFT JOIN competitor_listings l ON l.competitor_id=c.competitor_id AND l.is_active=1
        LEFT JOIN products p ON p.product_id=l.product_id
        LEFT JOIN internal_product_state ips ON ips.product_id=p.product_id
        LEFT JOIN current_listing_state s ON s.listing_id=l.listing_id
        GROUP BY c.competitor_code, c.competitor_name
        ORDER BY c.competitor_name COLLATE NOCASE
    """).fetchall()
    return [
        {
            "competitor_code": row["competitor_code"],
            "competitor_name": row["competitor_name"],
            "product_count": row["product_count"],
            "priced_product_count": row["priced_product_count"],
            "price_coverage": _ratio_percent((row["priced_product_count"] / row["product_count"]) if row["product_count"] else None),
            "avg_our_margin": _ratio_percent(row["avg_our_margin"]),
            "avg_competitor_margin": _ratio_percent(row["avg_competitor_margin"]),
            "avg_gap": _decimal_money(row["avg_gap"]),
            "our_higher_count": row["our_higher_count"],
            "our_lower_count": row["our_lower_count"],
        }
        for row in rows
    ]


def _comparison_summary(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute("""
        SELECT ips.product_id, ips.our_current_price_cents our_price,
               MIN(s.selling_price_cents) competitor_price
        FROM internal_product_state ips
        LEFT JOIN competitor_listings l ON l.product_id=ips.product_id AND l.is_active=1
        LEFT JOIN current_listing_state s ON s.listing_id=l.listing_id
        WHERE ips.is_active=1
        GROUP BY ips.product_id, ips.our_current_price_cents
    """).fetchall()
    return {
        "our_price_higher": sum(1 for row in rows if row["our_price"] is not None and row["competitor_price"] is not None and row["our_price"] > row["competitor_price"]),
        "our_price_lower": sum(1 for row in rows if row["our_price"] is not None and row["competitor_price"] is not None and row["our_price"] < row["competitor_price"]),
        "same_price": sum(1 for row in rows if row["our_price"] is not None and row["competitor_price"] is not None and row["our_price"] == row["competitor_price"]),
        "missing_competitor_price": sum(1 for row in rows if row["competitor_price"] is None),
    }


def _top_savings(conn: sqlite3.Connection, limit: int) -> list[dict[str, Any]]:
    return [_catalog_row(row) for row in conn.execute("""
        SELECT p.product_id, p.manufacturer, p.oem_part_number, p.internal_sku,
               COALESCE(s.product_name, p.product_name) product_name, s.*
        FROM current_listing_state s
        JOIN competitor_listings l ON l.listing_id=s.listing_id
        JOIN products p ON p.product_id=l.product_id
        WHERE s.price_display_type='discounted'
        ORDER BY s.savings_percent DESC NULLS LAST, s.selling_price_cents DESC
        LIMIT ?
    """, (limit,)).fetchall()]


def _recent_changes(conn: sqlite3.Connection, limit: int) -> list[dict[str, Any]]:
    rows = conn.execute("""
        SELECT h.*, p.manufacturer, p.oem_part_number, COALESCE(s.product_name, p.product_name) product_name
        FROM listing_history h
        JOIN competitor_listings l ON l.listing_id=h.listing_id
        JOIN products p ON p.product_id=l.product_id
        LEFT JOIN current_listing_state s ON s.listing_id=l.listing_id
        WHERE h.change_type <> 'first_observation'
        ORDER BY h.effective_at DESC
        LIMIT ?
    """, (limit,)).fetchall()
    return [_history_row(row) for row in rows]


def _quality_rows(conn: sqlite3.Connection, predicate: str) -> list[dict[str, Any]]:
    rows = conn.execute(f"""
        SELECT p.product_id, p.manufacturer, p.oem_part_number, p.internal_sku,
               c.competitor_code, c.competitor_name,
               COALESCE(s.product_name, p.product_name) product_name, s.*,
               COALESCE(se.parse_warning_count, 0) latest_warning_count, se.parse_warnings,
               se.page_classification latest_page_classification,
               se.session_status latest_session_status,
               se.checked_at latest_event_at
        FROM competitor_listings l
        JOIN competitors c ON c.competitor_id=l.competitor_id
        JOIN products p ON p.product_id=l.product_id
        LEFT JOIN current_listing_state s ON s.listing_id=l.listing_id
        LEFT JOIN scan_events se ON se.scan_event_id=(
            SELECT MAX(se2.scan_event_id) FROM scan_events se2 WHERE se2.listing_id=l.listing_id
        )
        WHERE l.is_active=1 AND {predicate}
        ORDER BY s.last_successful_check_at DESC NULLS LAST, p.oem_part_number COLLATE NOCASE
        LIMIT 50
    """).fetchall()
    return [_catalog_row(row) | {"latest_warning_count": row["latest_warning_count"], "parse_warnings": row["parse_warnings"] if "parse_warnings" in row.keys() else ""} for row in rows]


def _product_catalog_where(filters: CatalogFilters) -> tuple[str, list[Any]]:
    clauses = ["COALESCE(ips.is_active, p.is_active, 1)=1"]
    params: list[Any] = []
    if filters.search:
        clauses.append("""(
            p.oem_part_number LIKE ?
            OR COALESCE(ips.internal_sku, p.internal_sku, '') LIKE ?
            OR COALESCE(p.product_name, '') LIKE ?
            OR EXISTS (
                SELECT 1 FROM competitor_listings search_l
                LEFT JOIN current_listing_state search_s ON search_s.listing_id=search_l.listing_id
                WHERE search_l.product_id=p.product_id
                  AND COALESCE(search_s.product_name, '') LIKE ?
            )
        )""")
        like = f"%{filters.search}%"
        params.extend([like, like, like, like])
    if filters.manufacturer:
        clauses.append("p.manufacturer = ?")
        params.append(filters.manufacturer)
    if filters.price_type:
        clauses.append("""EXISTS (
            SELECT 1 FROM competitor_listings price_l
            LEFT JOIN current_listing_state price_s ON price_s.listing_id=price_l.listing_id
            WHERE price_l.product_id=p.product_id
              AND COALESCE(price_s.price_display_type, 'unknown') = ?
        )""")
        params.append(filters.price_type)
    if filters.availability:
        clauses.append("""EXISTS (
            SELECT 1 FROM competitor_listings availability_l
            JOIN current_listing_state availability_s ON availability_s.listing_id=availability_l.listing_id
            WHERE availability_l.product_id=p.product_id
              AND availability_s.availability_status = ?
        )""")
        params.append(filters.availability)
    if filters.superseded == "true":
        clauses.append("""EXISTS (
            SELECT 1 FROM competitor_listings superseded_l
            JOIN current_listing_state superseded_s ON superseded_s.listing_id=superseded_l.listing_id
            WHERE superseded_l.product_id=p.product_id
              AND superseded_s.supersession_detected = 1
        )""")
    elif filters.superseded == "false":
        clauses.append("""NOT EXISTS (
            SELECT 1 FROM competitor_listings superseded_l
            JOIN current_listing_state superseded_s ON superseded_s.listing_id=superseded_l.listing_id
            WHERE superseded_l.product_id=p.product_id
              AND superseded_s.supersession_detected = 1
        )""")
    if filters.confidence:
        clauses.append("""EXISTS (
            SELECT 1 FROM competitor_listings confidence_l
            LEFT JOIN current_listing_state confidence_s ON confidence_s.listing_id=confidence_l.listing_id
            WHERE confidence_l.product_id=p.product_id
              AND COALESCE(confidence_s.selling_price_confidence, confidence_s.price_parse_confidence, 'low') = ?
        )""")
        params.append(filters.confidence)
    if filters.needs_review:
        clauses.append("""
            NOT EXISTS (
                SELECT 1 FROM pricing_review_decisions review_prd
                WHERE review_prd.product_id=p.product_id
                  AND review_prd.suggested_new_price_cents=ips.our_current_price_cents
            )
        """)
    return " AND ".join(clauses), params


def _catalog_where(filters: CatalogFilters) -> tuple[str, list[Any]]:
    clauses = ["l.is_active=1"]
    params: list[Any] = []
    if filters.search:
        clauses.append("(p.oem_part_number LIKE ? OR COALESCE(p.internal_sku, '') LIKE ? OR COALESCE(s.product_name, p.product_name, '') LIKE ?)")
        like = f"%{filters.search}%"
        params.extend([like, like, like])
    if filters.manufacturer:
        clauses.append("p.manufacturer = ?")
        params.append(filters.manufacturer)
    if filters.price_type:
        clauses.append("COALESCE(s.price_display_type, 'unknown') = ?")
        params.append(filters.price_type)
    if filters.availability:
        clauses.append("s.availability_status = ?")
        params.append(filters.availability)
    if filters.superseded == "true":
        clauses.append("s.supersession_detected = 1")
    elif filters.superseded == "false":
        clauses.append("COALESCE(s.supersession_detected, 0) = 0")
    if filters.confidence:
        clauses.append("COALESCE(s.selling_price_confidence, s.price_parse_confidence, 'low') = ?")
        params.append(filters.confidence)
    if filters.needs_review:
        clauses.append("""(
            s.selling_price_cents IS NULL
            OR COALESCE(s.selling_price_confidence, s.price_parse_confidence)='low'
            OR COALESCE(s.consecutive_failure_count, 0) > 0
            OR COALESCE(se.parse_warning_count, 0) > 0
        )""")
    return " AND ".join(clauses), params


def _normalized_filters(filters: CatalogFilters) -> CatalogFilters:
    page_size = filters.page_size if filters.page_size in PAGE_SIZE_OPTIONS else DEFAULT_PAGE_SIZE
    return CatalogFilters(
        search=filters.search.strip(),
        manufacturer=filters.manufacturer.strip(),
        price_type=filters.price_type.strip(),
        availability=filters.availability.strip(),
        superseded=filters.superseded.strip(),
        confidence=filters.confidence.strip(),
        needs_review=filters.needs_review,
        sort=filters.sort if filters.sort in SORT_COLUMNS else "last_checked",
        page=max(1, filters.page),
        page_size=page_size,
    )


def _catalog_product_row(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    our_cents = data.get("our_current_price_cents")
    cost_cents = data.get("current_cost_cents")
    lowest_cents = data.get("lowest_competitor_price_cents")
    data["our_current_price"] = cents_to_money(our_cents)
    data["current_cost"] = cents_to_money(cost_cents)
    data["lowest_competitor_price"] = cents_to_money(lowest_cents)
    data["difference_vs_lowest_competitor"] = _fixed_money(our_cents - lowest_cents) if our_cents is not None and lowest_cents is not None else ""
    data["our_margin_pct"] = _percent_cents(our_cents - cost_cents, our_cents) if our_cents not in (None, 0) and cost_cents is not None else ""
    data["price_difference_class"] = _price_difference_class(our_cents, lowest_cents)
    data["gap_price_class"] = _price_difference_class(our_cents, lowest_cents)
    competitor_prices = [
        data.get(f"{adapter.competitor_key}_selling_price_cents")
        for adapter in list_competitors()
    ]
    data["our_price_class"] = _our_price_class(our_cents, competitor_prices)
    data["competitors"] = []
    for adapter in list_competitors():
        key = adapter.competitor_key
        cell = _competitor_cell(data, key)
        cell.update(
            {
                "key": key,
                "display_name": adapter.display_name,
                "short_name": short_display_name(adapter),
                "is_lowest": bool(
                    cell.get("price_cents") is not None
                    and lowest_cents is not None
                    and cell["price_cents"] == lowest_cents
                ),
            }
        )
        data[key] = cell
        data["competitors"].append(cell)
    saved_to_catalog = data.get("suggested_new_price_cents") is not None and data.get("suggested_new_price_cents") == our_cents
    data["needs_review"] = not saved_to_catalog
    return data


def _competitor_cell(data: dict[str, Any], key: str) -> dict[str, Any]:
    price_cents = data.get(f"{key}_selling_price_cents")
    status = data.get(f"{key}_status") or "not_checked"
    warning_count = data.get(f"{key}_warning_count") or 0
    return {
        "price_cents": price_cents,
        "price": cents_to_money(price_cents),
        "reference_price": cents_to_money(data.get(f"{key}_reference_price_cents")),
        "savings_percent": data.get(f"{key}_savings_percent"),
        "status": status,
        "checked_at": data.get(f"{key}_checked_at"),
        "needs_review": price_cents is None and status != "manufacturer_not_carried" or warning_count > 0,
    }


def _fixed_money(cents: int) -> str:
    return format((Decimal(cents) / Decimal("100")).quantize(Decimal("0.01")), "f")


def _catalog_row(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    our_cents = data.get("our_current_price_cents")
    cost_cents = data.get("current_cost_cents")
    data["our_current_price"] = cents_to_money(our_cents)
    data["current_cost"] = cents_to_money(cost_cents)
    data["selling_price"] = cents_to_money(data.get("selling_price_cents"))
    data["reference_price"] = cents_to_money(data.get("reference_price_cents"))
    data["supersession_detected"] = bool(data.get("supersession_detected") or False)
    if our_cents is not None and cost_cents is not None:
        data["our_margin"] = _fixed_money(our_cents - cost_cents)
        data["our_margin_pct"] = _percent_cents(our_cents - cost_cents, our_cents) if our_cents else ""
    else:
        data["our_margin"] = ""
        data["our_margin_pct"] = ""
    data["needs_review"] = (
        data.get("selling_price_cents") is None
        or data.get("selling_price_confidence") == "low"
        or (data.get("consecutive_failure_count") or 0) > 0
        or (data.get("latest_warning_count") or 0) > 0
    )
    return data


def _percent_cents(numerator: int, denominator: int) -> str:
    return f"{(Decimal(numerator) / Decimal(denominator) * Decimal('100')).quantize(Decimal('0.01'))}"


def _price_difference_class(our_cents: int | None, competitor_cents: int | None) -> str:
    if our_cents is None or competitor_cents is None:
        return ""
    if our_cents > competitor_cents:
        return "price-difference-higher"
    if our_cents < competitor_cents:
        return "price-difference-lower"
    return ""


def _our_price_class(our_cents: int | None, competitor_prices: list[int | None]) -> str:
    available = [price for price in competitor_prices if price is not None]
    if our_cents is None or not available:
        return ""
    if any(our_cents > price for price in available):
        return "price-above-competitor"
    if all(our_cents < price for price in available):
        return "price-below-competitors"
    return ""


def _ratio_percent(value: Any) -> str:
    if value is None:
        return ""
    return f"{(Decimal(str(value)) * Decimal('100')).quantize(Decimal('0.01'))}%"


def _decimal_money(value: Any) -> str:
    if value is None:
        return ""
    return format(Decimal(str(value)).quantize(Decimal("0.01")), "f")


def _run_row(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    data["duration"] = _duration(data.get("started_at"), data.get("completed_at"))
    attempted = data.get("attempted_part_count") or 0
    successful = data.get("successful_part_count") or 0
    data["success_percent"] = round(successful / attempted * 100, 1) if attempted else 0
    return data


def _event_row(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    data["selling_price"] = cents_to_money(data.get("selling_price_cents"))
    return data


PRICE_HISTORY_GROUP_MINUTES = 45


def _pivot_price_history(
    rows: list[dict[str, Any]],
    *,
    window_minutes: int = PRICE_HISTORY_GROUP_MINUTES,
) -> tuple[list[str], list[dict[str, Any]]]:
    """Turn one-row-per-change history into a date x competitor grid.

    Each competitor is checked in its own scan run, usually a minute or two
    apart, so entries close together in time belong to the same price check and
    should share a row. A repeat of the same competitor always starts a new row.
    Rows are expected newest-first.
    """
    competitors = sorted(
        {str(row.get("competitor_name") or "") for row in rows if row.get("competitor_name")}
    )
    groups: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None

    for row in rows:
        name = str(row.get("competitor_name") or "")
        if not name:
            continue
        stamp = str(row.get("effective_at") or "")
        start_new = current is None
        if current is not None:
            if name in current["prices"]:
                start_new = True
            else:
                try:
                    gap = (_parse_time(current["oldest_at"]) - _parse_time(stamp)).total_seconds()
                except ValueError:
                    gap = 0
                if abs(gap) > window_minutes * 60:
                    start_new = True
        if start_new:
            current = {"checked_at": stamp, "oldest_at": stamp, "prices": {}}
            groups.append(current)
        current["prices"][name] = {
            "price": row.get("new_price"),
            "previous_price": row.get("previous_price"),
            "change_type": row.get("change_type"),
            "percent_change": row.get("percent_change"),
        }
        current["oldest_at"] = stamp

    return competitors, groups


def _history_row(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    data["previous_price"] = cents_to_money(data.get("previous_selling_price_cents"))
    data["new_price"] = cents_to_money(data.get("new_selling_price_cents"))
    previous = data.get("previous_selling_price_cents")
    new = data.get("new_selling_price_cents")
    data["percent_change"] = ""
    if previous and new:
        data["percent_change"] = f"{((Decimal(new) - Decimal(previous)) / Decimal(previous) * Decimal('100')).quantize(Decimal('0.1'))}%"
    return data


def _duration(started: str | None, completed: str | None) -> str:
    if not started or not completed:
        return ""
    try:
        start = _parse_time(started)
        end = _parse_time(completed)
    except ValueError:
        return ""
    seconds = max(0, int((end - start).total_seconds()))
    minutes, rem = divmod(seconds, 60)
    return f"{minutes}m {rem}s" if minutes else f"{rem}s"


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def _distinct(conn: sqlite3.Connection, table: str, column: str) -> list[str]:
    return [row[0] for row in conn.execute(f"SELECT DISTINCT {column} FROM {table} WHERE {column} IS NOT NULL ORDER BY {column}")]
