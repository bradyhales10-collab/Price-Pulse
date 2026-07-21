from __future__ import annotations

from app.collector_bridge import import_collection_summary, selected_parts_csv
from app.database import connect_database, initialize_database, seed_motosport, upsert_product_and_listing
from app.models import PartRecord
from local_collector import prepare_local_database


def test_selected_parts_csv_exports_import_batch_parts(tmp_path):
    db = tmp_path / "bridge.db"
    initialize_database(db)
    with connect_database(db) as conn:
        now = "2026-07-21T00:00:00Z"
        conn.execute(
            """
            INSERT INTO import_batches(import_batch_id, original_filename, stored_filename, file_sha256, uploaded_at, status)
            VALUES (101, 'parts.xlsx', 'parts.xlsx', 'abc', ?, 'imported')
            """,
            (now,),
        )
        product_id, _, _, _ = upsert_product_and_listing(
            conn,
            PartRecord(test_case_id="", manufacturer="Honda", oem_part_number="ABC-123", search_observed_product_name="Brake Pad"),
        )
        conn.execute(
            """
            INSERT INTO internal_product_state(product_id, internal_sku, our_current_price_cents, current_cost_cents, is_active, source_import_batch_id, updated_at)
            VALUES (?, 'SKU-1', 1299, 700, 1, 101, ?)
            """,
            (product_id, now),
        )

    csv_text = selected_parts_csv(db, 101)

    assert "Manufacturer,OEM_Part_Number" in csv_text
    assert "Honda,ABC-123" in csv_text


def test_collection_summary_upload_updates_competitor_price(tmp_path):
    db = tmp_path / "bridge_import.db"
    initialize_database(db)
    with connect_database(db) as conn:
        seed_motosport(conn)
        product_id, _, _, _ = upsert_product_and_listing(
            conn,
            PartRecord(test_case_id="", manufacturer="KTM", oem_part_number="79532010033", search_observed_product_name="KTM Part"),
        )
        conn.execute(
            """
            INSERT INTO internal_product_state(product_id, internal_sku, our_current_price_cents, current_cost_cents, is_active, updated_at)
            VALUES (?, 'SKU-KTM', 20000, 10000, 1, '2026-07-21T00:00:00Z')
            """,
            (product_id,),
        )
    summary = b"""run_order,scan_run_id,scan_event_id,manufacturer,normalized_manufacturer,competitor,manufacturer_supported,lookup_status,status_reason,oem_part_number,observed_part_number,product_name,checked_at,http_status,page_classification,session_status,selling_price,reference_price,savings_percent,price_display_type,previous_selling_price,result_type,price_changed,availability_raw,previous_availability_status,availability_status,supersession_detected,superseded_by_raw,price_source_category,price_corroboration_count,price_parse_confidence,parse_confidence,warning_count,warnings,observation_json_path
1,1,,KTM,KTM,motosport,True,found,,79532010033,79532010033,KTM Part,2026-07-21T00:00:00Z,200,normal_product,authenticated,185.99,199.99,,discounted,,first_observation,False,In Stock,,in_stock,False,,visible_text,1,high,high,0,,
"""

    result = import_collection_summary(db, summary_csv=summary)

    assert result.rows_imported == 1
    with connect_database(db) as conn:
        row = conn.execute(
            """
            SELECT s.selling_price_cents
            FROM current_listing_state s
            JOIN competitor_listings l ON l.listing_id=s.listing_id
            JOIN competitors c ON c.competitor_id=l.competitor_id
            WHERE c.competitor_code='motosport'
            """
        ).fetchone()
    assert row["selling_price_cents"] == 18599


def test_unsupported_manufacturer_summary_does_not_create_current_price(tmp_path):
    db = tmp_path / "bridge_unsupported.db"
    initialize_database(db)
    with connect_database(db) as conn:
        product_id, _, _, _ = upsert_product_and_listing(
            conn,
            PartRecord(test_case_id="", manufacturer="KTM", oem_part_number="00050000068", search_observed_product_name="KTM Part"),
        )
        conn.execute(
            """
            INSERT INTO internal_product_state(product_id, internal_sku, our_current_price_cents, is_active, updated_at)
            VALUES (?, 'SKU-KTM-2', 1000, 1, '2026-07-21T00:00:00Z')
            """,
            (product_id,),
        )
    summary = b"""run_order,scan_run_id,scan_event_id,manufacturer,normalized_manufacturer,competitor,manufacturer_supported,lookup_status,status_reason,oem_part_number,observed_part_number,product_name,checked_at,http_status,page_classification,session_status,selling_price,reference_price,savings_percent,price_display_type,previous_selling_price,result_type,price_changed,availability_raw,previous_availability_status,availability_status,supersession_detected,superseded_by_raw,price_source_category,price_corroboration_count,price_parse_confidence,parse_confidence,warning_count,warnings,observation_json_path
1,1,,KTM,KTM,partzilla,False,manufacturer_not_carried,Partzilla does not carry KTM,00050000068,,,,,manufacturer_not_carried,not_applicable,,,,,manufacturer_not_carried,False,,,,False,,,,low,low,0,,
"""

    result = import_collection_summary(db, summary_csv=summary)

    assert result.rows_imported == 1
    with connect_database(db) as conn:
        current_count = conn.execute("SELECT COUNT(*) FROM current_listing_state").fetchone()[0]
        event = conn.execute("SELECT page_classification FROM scan_events").fetchone()
    assert current_count == 0
    assert event["page_classification"] == "manufacturer_not_carried"


def test_local_collector_prepares_temporary_database(tmp_path):
    input_path = tmp_path / "input.csv"
    input_path.write_text(
        "\n".join(
            [
                "Test_Case_ID,Manufacturer,OEM_Part_Number,Search_Observed_Product_Name,Search_Observed_MSRP,Expected_Partzilla_URL,Test_Purpose,Verified_Date,Source_URL",
                "1,Honda,ABC-123,,,,,,",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    db = tmp_path / "local.db"

    prepare_local_database(input_path, db, ["partzilla", "motosport", "chaparral"])

    with connect_database(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM products").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM competitor_listings").fetchone()[0] == 3
