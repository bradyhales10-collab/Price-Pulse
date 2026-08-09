"""Dashboard pages must stay fast as scan history accumulates.

scan_events grows by roughly one row per part per competitor on every run, so a
1000-part check across four competitors adds about 4,000 rows each time. With no
index on listing_id, every dashboard query scanned that whole table, so pages
got slower the more the program was used. Measured at 80,000 scan events, a
catalog page took 14.7 seconds; with the index it takes 0.02.
"""

from __future__ import annotations

import tempfile
import time
from pathlib import Path

from app.comparison import ComparisonFilters, comparison_rows
from app.database import (
    connect_database,
    create_scan_run,
    initialize_database,
    normalize_part_number,
    seed_chaparral,
    seed_motosport,
    seed_partzilla,
    seed_revzilla,
    utc_now,
)
from app.web.queries import CatalogFilters, catalog_data

PRODUCT_COUNT = 400
PREVIOUS_RUNS = 10


def _build_database(path: Path) -> None:
    initialize_database(path)
    now = utc_now()
    with connect_database(path) as conn:
        competitor_ids = [
            seed_partzilla(conn),
            seed_motosport(conn),
            seed_chaparral(conn),
            seed_revzilla(conn),
        ]
        for index in range(1, PRODUCT_COUNT + 1):
            part_number = f"PART-{index:05d}"
            conn.execute(
                "INSERT INTO products(product_id, manufacturer, oem_part_number, normalized_part_number, "
                "product_name, is_active, created_at, updated_at) VALUES (?,?,?,?,?,1,?,?)",
                (index, "Yamaha", part_number, normalize_part_number(part_number), f"Part {index}", now, now),
            )
            conn.execute(
                "INSERT INTO internal_product_state(product_id, internal_sku, our_current_price_cents, "
                "current_cost_cents, is_active, updated_at) VALUES (?,?,?,?,1,?)",
                (index, f"SKU{index}", 5000, 3000, now),
            )
        listing_id = 0
        for product_id in range(1, PRODUCT_COUNT + 1):
            for competitor_id in competitor_ids:
                listing_id += 1
                conn.execute(
                    "INSERT INTO competitor_listings(listing_id, product_id, competitor_id, "
                    "competitor_part_number, canonical_url, is_active, first_seen_at, last_seen_at, "
                    "created_at, updated_at) VALUES (?,?,?,?,?,1,?,?,?,?)",
                    (listing_id, product_id, competitor_id, f"P{product_id}", "https://x", now, now, now, now),
                )
                conn.execute(
                    "INSERT INTO current_listing_state(listing_id, selling_price_cents, price_display_type, "
                    "first_observed_at, last_successful_check_at, last_changed_at, updated_at) "
                    "VALUES (?,?, 'regular', ?,?,?,?)",
                    (listing_id, 4000 + product_id, now, now, now, now),
                )
        for _ in range(PREVIOUS_RUNS):
            for competitor_id in competitor_ids:
                run_id = create_scan_run(
                    conn, competitor_id=competitor_id, requested_part_count=PRODUCT_COUNT
                )
                conn.executemany(
                    "INSERT INTO scan_events(scan_run_id, listing_id, checked_at, page_classification, "
                    "session_status, navigation_succeeded, price_found, parse_warning_count) "
                    "VALUES (?,?,?,?,?,1,1,0)",
                    [
                        (run_id, candidate, now, "normal_product", "public")
                        for candidate in range(1, listing_id + 1, 4)
                    ],
                )


def test_catalog_and_comparison_stay_fast_with_accumulated_scan_history() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        database = Path(tmp) / "history.db"
        _build_database(database)

        with connect_database(database) as conn:
            events = conn.execute("SELECT COUNT(*) count FROM scan_events").fetchone()["count"]
        assert events > 10000, "the test needs enough history to be meaningful"

        started = time.perf_counter()
        catalog_data(database, CatalogFilters(page=1, page_size=50))
        catalog_seconds = time.perf_counter() - started

        started = time.perf_counter()
        comparison_rows(database, ComparisonFilters())
        comparison_seconds = time.perf_counter() - started

        assert catalog_seconds < 2.0, f"catalog page took {catalog_seconds:.2f}s with {events} scan events"
        assert comparison_seconds < 3.0, f"comparison took {comparison_seconds:.2f}s with {events} scan events"


def test_the_indexes_the_dashboard_depends_on_exist() -> None:
    """Named explicitly so removing one fails here rather than silently making
    every page slow again as history builds up."""
    with tempfile.TemporaryDirectory() as tmp:
        database = Path(tmp) / "fresh.db"
        initialize_database(database)
        with connect_database(database) as conn:
            names = {
                row["name"]
                for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")
            }

    for required in ("idx_events_listing", "idx_listing_product", "idx_listing_competitor"):
        assert required in names, f"missing index {required}"
