from __future__ import annotations

from pathlib import Path

from app.database import connect_database, initialize_database, seed_motosport, utc_now
from app.maintenance import clear_comparison_results, clear_pending_review_queue, clear_scan_runs


def test_cleanup_actions_keep_catalog_and_scope_their_deletions() -> None:
    database = Path("data/output/test-artifacts/maintenance.db")
    database.unlink(missing_ok=True)
    initialize_database(database)
    now = utc_now()
    with connect_database(database) as conn:
        competitor_id = seed_motosport(conn)
        conn.execute(
            """
            INSERT INTO products(product_id, manufacturer, oem_part_number, normalized_part_number,
                internal_sku, is_active, created_at, updated_at)
            VALUES (1, 'Honda', 'H-1', 'H-1', 'SKU-1', 1, ?, ?)
            """,
            (now, now),
        )
        conn.execute(
            """
            INSERT INTO internal_product_state(product_id, internal_sku, our_current_price_cents,
                current_cost_cents, is_active, updated_at)
            VALUES (1, 'SKU-1', 1200, 700, 1, ?)
            """,
            (now,),
        )
        conn.execute(
            """
            INSERT INTO competitor_listings(product_id, competitor_id, competitor_part_number, canonical_url,
                first_seen_at, last_seen_at, created_at, updated_at)
            VALUES (1, ?, 'H-1', 'https://example.test/H-1', ?, ?, ?, ?)
            """,
            (competitor_id, now, now, now, now),
        )
        conn.execute(
            """
            INSERT INTO current_listing_state(listing_id, selling_price_cents, first_observed_at,
                last_successful_check_at, last_changed_at, updated_at)
            VALUES (1, 1000, ?, ?, ?, ?)
            """,
            (now, now, now, now),
        )
        conn.execute(
            """
            INSERT INTO scan_runs(scan_run_id, competitor_id, started_at, requested_part_count, run_status)
            VALUES (1, ?, ?, 1, 'completed')
            """,
            (competitor_id, now),
        )
        conn.execute(
            """
            INSERT INTO scan_events(scan_event_id, scan_run_id, listing_id, checked_at, page_classification,
                session_status, navigation_succeeded, price_found, parse_warning_count)
            VALUES (1, 1, 1, ?, 'normal_product', 'authenticated', 1, 1, 0)
            """,
            (now,),
        )
        conn.execute(
            """
            INSERT INTO listing_history(listing_id, scan_event_id, effective_at, change_type,
                new_selling_price_cents)
            VALUES (1, 1, ?, 'first_observation', 1000)
            """,
            (now,),
        )
        conn.execute(
            """
            INSERT INTO competitor_probe_results(competitor_key, product_id, manufacturer, oem_part_number,
                url, checked_at, page_classification, warnings_json, raw_result_json, created_at)
            VALUES ('motosport', 1, 'Honda', 'H-1', 'https://example.test/H-1', ?, 'normal_product', '[]', '{}', ?)
            """,
            (now, now),
        )
        conn.execute(
            """
            INSERT INTO pricing_review_decisions(product_id, review_status, created_at, updated_at)
            VALUES (1, 'Pending Review', ?, ?)
            """,
            (now, now),
        )

    clear_counts = clear_comparison_results(database)
    assert clear_counts["current_prices"] == 1
    with connect_database(database) as conn:
        assert conn.execute("SELECT COUNT(*) FROM products").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM internal_product_state").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM current_listing_state").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM scan_runs").fetchone()[0] == 1

    assert clear_pending_review_queue(database) == 1
    with connect_database(database) as conn:
        assert conn.execute("SELECT review_status FROM pricing_review_decisions WHERE product_id=1").fetchone()[0] == "Ignored"

    clear_scan_runs(database)
    with connect_database(database) as conn:
        assert conn.execute("SELECT COUNT(*) FROM scan_runs").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM scan_events").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM products").fetchone()[0] == 1
