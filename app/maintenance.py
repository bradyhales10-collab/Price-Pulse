from __future__ import annotations

from pathlib import Path

from app.config import DATA_DIR
from app.database import connect_database, utc_now


IMPORT_DIR = DATA_DIR / "imports"


def clear_comparison_results(database: Path) -> dict[str, int]:
    """Remove stored competitor results while preserving the product catalog."""
    with connect_database(database) as conn:
        counts = {
            "current_prices": _count(conn, "current_listing_state"),
            "price_history": _count(conn, "listing_history"),
            "probe_results": _count(conn, "competitor_probe_results"),
            "cart_probe_results": _count(conn, "competitor_cart_probe_results"),
            "chaparral_cache": _count(conn, "chaparral_resolution_cache"),
        }
        conn.execute("DELETE FROM listing_history")
        conn.execute("DELETE FROM current_listing_state")
        conn.execute("DELETE FROM competitor_probe_results")
        conn.execute("DELETE FROM competitor_cart_probe_results")
        conn.execute("DELETE FROM chaparral_resolution_cache")
    return counts


def clear_pending_review_queue(database: Path) -> int:
    """Resolve all currently pending rows as Ignored without deleting products."""
    now = utc_now()
    with connect_database(database) as conn:
        rows = conn.execute(
            """
            SELECT p.product_id
            FROM products p
            JOIN internal_product_state ips ON ips.product_id=p.product_id
            LEFT JOIN pricing_review_decisions prd ON prd.product_id=p.product_id
            WHERE COALESCE(prd.review_status, 'Pending Review')='Pending Review'
            """
        ).fetchall()
        for row in rows:
            conn.execute(
                """
                INSERT INTO pricing_review_decisions(
                    product_id, review_status, suggested_new_price_cents, applied_rule_codes_json,
                    notes, reviewer, reviewed_at, created_at, updated_at
                )
                VALUES (?, 'Ignored', NULL, '[]', ?, '', ?, ?, ?)
                ON CONFLICT(product_id) DO UPDATE SET
                    review_status='Ignored',
                    notes=excluded.notes,
                    reviewer='',
                    reviewed_at=excluded.reviewed_at,
                    updated_at=excluded.updated_at
                """,
                (int(row["product_id"]), "Cleared from review queue.", now, now, now),
            )
    return len(rows)


def clear_scan_runs(database: Path) -> dict[str, int]:
    """Delete scan history while preserving current competitor prices and products."""
    with connect_database(database) as conn:
        counts = {
            "scan_runs": _count(conn, "scan_runs"),
            "scan_events": _count(conn, "scan_events"),
            "price_history": _count(conn, "listing_history"),
        }
        conn.execute("DELETE FROM listing_history")
        conn.execute("DELETE FROM scan_events")
        conn.execute("DELETE FROM scan_runs")
    return counts


def reset_all_test_data(database: Path) -> dict[str, int]:
    """Remove all uploaded/test data while keeping schema and configuration."""
    with connect_database(database) as conn:
        tables = (
            "competitor_cart_probe_results",
            "competitor_probe_results",
            "pricing_review_decisions",
            "listing_history",
            "current_listing_state",
            "scan_events",
            "scan_runs",
            "chaparral_resolution_cache",
            "import_batch_rows",
            "internal_product_state",
            "competitor_listings",
            "import_batches",
            "products",
        )
        counts = {table: _count(conn, table) for table in tables}
        for table in tables:
            conn.execute(f"DELETE FROM {table}")

    uploaded_files = 0
    if IMPORT_DIR.exists():
        for path in IMPORT_DIR.iterdir():
            if path.is_file():
                path.unlink()
                uploaded_files += 1
    counts["uploaded_files"] = uploaded_files
    return counts


def _count(conn, table: str) -> int:
    return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
