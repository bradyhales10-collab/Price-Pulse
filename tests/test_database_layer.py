from __future__ import annotations

import csv
import json
from decimal import Decimal
from pathlib import Path

import import_validation_results
from app.database import (
    cents_to_money,
    complete_scan_run,
    connect_database,
    create_scan_run,
    initialize_database,
    money_to_cents,
    persist_observation,
    seed_partzilla,
    table_counts,
    upsert_product_and_listing,
)
from app.models import PartRecord
from app.schemas.product_observation import (
    AccessContext,
    AvailabilityStatus,
    PageClassification,
    ParseConfidence,
    PriceValidationStatus,
    PriceVisibility,
    ProductObservation,
    SessionStatus,
)
from export_current_prices import _rows as current_price_rows
from export_price_changes import _rows as price_change_rows
from import_validation_results import main as import_validation_main

TEST_OUTPUT_DIR = Path("data/output/test-artifacts")


def test_database_initializes_and_is_rerunnable() -> None:
    db = _db("init.db")
    initialize_database(db)
    initialize_database(db)
    with connect_database(db) as conn:
        counts = table_counts(conn)
        migrations = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
        override_table = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='pricing_rule_manufacturer_overrides'").fetchone()
    assert counts["competitors"] == 3
    assert migrations == 9
    assert override_table is not None


def test_partzilla_seed_competitor_not_duplicated() -> None:
    db = _db("seed.db")
    initialize_database(db)
    with connect_database(db) as conn:
        seed_partzilla(conn)
        seed_partzilla(conn)
        assert conn.execute("SELECT COUNT(*) FROM competitors WHERE competitor_code='partzilla'").fetchone()[0] == 1


def test_product_import_and_reimport_no_duplicates_and_listing_unique() -> None:
    db = _db("products.db")
    initialize_database(db)
    with connect_database(db) as conn:
        upsert_product_and_listing(conn, _record("41080-1514"))
        upsert_product_and_listing(conn, _record("41080-1514"))
        assert conn.execute("SELECT COUNT(*) FROM products").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM competitor_listings").fetchone()[0] == 1


def test_complex_and_letter_part_numbers_preserved() -> None:
    db = _db("part_numbers.db")
    initialize_database(db)
    with connect_database(db) as conn:
        for part in ("55061-5438-739", "K53001-240", "KMT4X7-3-4", "46092-S013", "3099"):
            upsert_product_and_listing(conn, _record(part))
        stored = [row[0] for row in conn.execute("SELECT oem_part_number FROM products ORDER BY product_id")]
    assert stored == ["55061-5438-739", "K53001-240", "KMT4X7-3-4", "46092-S013", "3099"]


def test_first_observation_creates_current_state_and_history() -> None:
    db, listing_id, run_id = _prepared("first.db")
    with connect_database(db) as conn:
        result = persist_observation(conn, scan_run_id=run_id, listing_id=listing_id, observation=_obs("282.32"), observation_json_path="obs.json")
        assert result == "first_observation"
        assert conn.execute("SELECT selling_price_cents FROM current_listing_state").fetchone()[0] == 28232
        assert conn.execute("SELECT COUNT(*) FROM listing_history").fetchone()[0] == 1


def test_same_price_creates_event_but_no_history() -> None:
    db, listing_id, run_id = _prepared("same.db")
    with connect_database(db) as conn:
        persist_observation(conn, scan_run_id=run_id, listing_id=listing_id, observation=_obs("282.32"), observation_json_path="1")
        persist_observation(conn, scan_run_id=run_id, listing_id=listing_id, observation=_obs("282.32"), observation_json_path="2")
        assert conn.execute("SELECT COUNT(*) FROM scan_events").fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM listing_history").fetchone()[0] == 1


def test_price_increase_and_decrease_history_and_decimal_details() -> None:
    db, listing_id, run_id = _prepared("price_change.db")
    with connect_database(db) as conn:
        persist_observation(conn, scan_run_id=run_id, listing_id=listing_id, observation=_obs("282.32"), observation_json_path="1")
        persist_observation(conn, scan_run_id=run_id, listing_id=listing_id, observation=_obs("300.00"), observation_json_path="2")
        persist_observation(conn, scan_run_id=run_id, listing_id=listing_id, observation=_obs("269.99"), observation_json_path="3")
        rows = conn.execute("SELECT change_type, change_details_json FROM listing_history ORDER BY history_id").fetchall()
    assert [row["change_type"] for row in rows] == ["first_observation", "price_change", "price_change"]
    details = json.loads(rows[2]["change_details_json"])
    assert details["previous_price"] == "300.00"
    assert details["new_price"] == "269.99"
    assert details["dollar_change"] == "-30.01"
    assert "percent_change" in details


def test_exact_cents_are_preserved() -> None:
    assert money_to_cents(Decimal("6.97")) == 697
    assert cents_to_money(697) == "6.97"
    assert cents_to_money(1000) == "10.00"


def test_availability_supersession_and_multiple_changes() -> None:
    db, listing_id, run_id = _prepared("multi.db")
    with connect_database(db) as conn:
        persist_observation(conn, scan_run_id=run_id, listing_id=listing_id, observation=_obs("282.32", availability=AvailabilityStatus.SHIPS_IN), observation_json_path="1")
        assert persist_observation(conn, scan_run_id=run_id, listing_id=listing_id, observation=_obs("282.32", availability=AvailabilityStatus.IN_STOCK), observation_json_path="2") == "availability_change"
        assert persist_observation(conn, scan_run_id=run_id, listing_id=listing_id, observation=_obs("282.32", availability=AvailabilityStatus.IN_STOCK, superseded=True), observation_json_path="3") == "supersession_change"
        assert persist_observation(conn, scan_run_id=run_id, listing_id=listing_id, observation=_obs("200.00", availability=AvailabilityStatus.SHIPS_IN, superseded=False), observation_json_path="4") == "multiple_changes"


def test_failed_and_blocked_scan_do_not_erase_price_and_increment_failures_then_success_resets() -> None:
    db, listing_id, run_id = _prepared("failure.db")
    with connect_database(db) as conn:
        persist_observation(conn, scan_run_id=run_id, listing_id=listing_id, observation=_obs("282.32"), observation_json_path="1")
        persist_observation(conn, scan_run_id=run_id, listing_id=listing_id, observation=_obs(None, classification=PageClassification.NAVIGATION_ERROR), observation_json_path="2")
        persist_observation(conn, scan_run_id=run_id, listing_id=listing_id, observation=_obs(None, classification=PageClassification.BLOCKED), observation_json_path="3")
        state = conn.execute("SELECT selling_price_cents, consecutive_failure_count FROM current_listing_state").fetchone()
        assert state["selling_price_cents"] == 28232
        assert state["consecutive_failure_count"] == 2
        persist_observation(conn, scan_run_id=run_id, listing_id=listing_id, observation=_obs("282.32"), observation_json_path="4")
        assert conn.execute("SELECT consecutive_failure_count FROM current_listing_state").fetchone()[0] == 0


def test_transaction_failure_rolls_back() -> None:
    db, _, run_id = _prepared("rollback.db")
    with connect_database(db) as conn:
        try:
            persist_observation(conn, scan_run_id=run_id, listing_id=9999, observation=_obs("282.32"), observation_json_path="bad")
        except Exception:
            pass
        assert conn.execute("SELECT COUNT(*) FROM scan_events").fetchone()[0] == 0


def test_duplicate_validation_import_is_idempotent(monkeypatch) -> None:
    db = _db("validation_import.db")
    summary = _validation_summary("validation_summary.csv")
    monkeypatch.setattr(import_validation_results.sys, "argv", ["import_validation_results.py", "--file", str(summary), "--database", str(db)])
    assert import_validation_main() == 0
    monkeypatch.setattr(import_validation_results.sys, "argv", ["import_validation_results.py", "--file", str(summary), "--database", str(db)])
    assert import_validation_main() == 0
    with connect_database(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM scan_events").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM listing_history").fetchone()[0] == 1


def test_exports_are_correct() -> None:
    db, listing_id, run_id = _prepared("exports.db")
    with connect_database(db) as conn:
        persist_observation(conn, scan_run_id=run_id, listing_id=listing_id, observation=_obs("282.32"), observation_json_path="1")
        persist_observation(conn, scan_run_id=run_id, listing_id=listing_id, observation=_obs("269.99"), observation_json_path="2")
    current = current_price_rows(db)
    changes = price_change_rows(db)
    assert current[0]["partzilla_price"] == "269.99"
    assert any(row["change_type"] == "price_change" and row["new_price"] == "269.99" for row in changes)


def test_one_part_database_save_creates_completed_scan_run() -> None:
    db, listing_id, run_id = _prepared("scan_run.db")
    with connect_database(db) as conn:
        persist_observation(conn, scan_run_id=run_id, listing_id=listing_id, observation=_obs("282.32"), observation_json_path="1")
        complete_scan_run(conn, run_id)
        row = conn.execute("SELECT run_status, attempted_part_count, successful_part_count FROM scan_runs").fetchone()
    assert row["run_status"] == "completed"
    assert row["attempted_part_count"] == 1
    assert row["successful_part_count"] == 1


def _db(name: str) -> Path:
    TEST_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    path = TEST_OUTPUT_DIR / name
    if path.exists():
        path.unlink()
    return path


def _record(part_number: str) -> PartRecord:
    return PartRecord(test_case_id="T", manufacturer="Kawasaki", oem_part_number=part_number, search_observed_product_name="DISC")


def _prepared(name: str):
    db = _db(name)
    initialize_database(db)
    with connect_database(db) as conn:
        competitor_id = seed_partzilla(conn)
        _, listing_id, _, _ = upsert_product_and_listing(conn, _record("41080-1514"))
        run_id = create_scan_run(conn, competitor_id=competitor_id, requested_part_count=1)
    return db, listing_id, run_id


def _obs(price: str | None, classification=PageClassification.NORMAL_PRODUCT, availability=AvailabilityStatus.SHIPS_IN, superseded=False) -> ProductObservation:
    return ProductObservation(
        test_case_id="T",
        manufacturer="Kawasaki",
        oem_part_number="41080-1514",
        observed_part_number="41080-1514",
        requested_url="https://www.partzilla.com/product/kawasaki/41080-1514",
        final_url="https://www.partzilla.com/product/kawasaki/41080-1514",
        canonical_url="https://www.partzilla.com/product/kawasaki/41080-1514",
        http_status=200,
        page_title="",
        page_classification=classification,
        price_visibility=PriceVisibility.VISIBLE if price else PriceVisibility.UNKNOWN,
        classification_confidence=ParseConfidence.HIGH,
        classification_evidence=[],
        product_name="DISC",
        manufacturer_display="KAWASAKI",
        msrp_raw=None,
        msrp=None,
        selling_price_raw=f"${price}" if price else None,
        selling_price=Decimal(price) if price else None,
        availability_raw=availability.value,
        availability_status=availability,
        shipping_estimate=None,
        access_context=AccessContext.AUTHENTICATED_SESSION,
        session_status=SessionStatus.AUTHENTICATED,
        superseded_by_raw="14081007" if superseded else None,
        supersession_detected=superseded,
        price_parse_confidence=ParseConfidence.HIGH,
        price_validation_status=PriceValidationStatus.NOT_MANUALLY_VALIDATED,
        parse_confidence=ParseConfidence.HIGH,
        parse_warnings=[],
        checked_at="2026-07-08T00:00:00Z",
    )


def _validation_summary(name: str) -> Path:
    path = TEST_OUTPUT_DIR / name
    fieldnames = [
        "validation_order","test_case_id","manufacturer","oem_part_number","test_purpose","checked_at","http_status",
        "page_classification","session_status","observed_part_number","product_name","selling_price_raw","selling_price",
        "price_visibility","availability_raw","availability_status","supersession_detected","superseded_by_raw",
        "price_source_category","price_source_locations","price_corroboration_count","price_parse_confidence",
        "parse_confidence","parse_warning_count","parse_warnings","observation_json_path",
    ]
    with path.open("w", newline="", encoding="utf-8") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerow({
            "validation_order":"1","test_case_id":"T","manufacturer":"Kawasaki","oem_part_number":"41080-1514",
            "test_purpose":"test","checked_at":"2026-07-08T00:00:00Z","http_status":"200",
            "page_classification":"normal_product","session_status":"authenticated","observed_part_number":"41080-1514",
            "product_name":"DISC","selling_price_raw":"$282.32","selling_price":"282.32","price_visibility":"visible",
            "availability_raw":"Ships in 3 to 4 days","availability_status":"ships_in","supersession_detected":"False",
            "superseded_by_raw":"","price_source_category":"structured_product_data","price_source_locations":"x;y",
            "price_corroboration_count":"2","price_parse_confidence":"high","parse_confidence":"high",
            "parse_warning_count":"0","parse_warnings":"","observation_json_path":"same-observation.json",
        })
    return path
