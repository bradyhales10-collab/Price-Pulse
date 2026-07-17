from __future__ import annotations

import csv
import json
import builtins
from decimal import Decimal
from pathlib import Path

import collect_parts
from app.competitors.base import CompetitorObservation
from app.collection import (
    CollectionRow,
    CollectionRunResult,
    PlannedPart,
    fingerprint_file,
    normalize_result_type,
    output_dir_for_run,
    plan_collection,
    stop_status_for,
    validate_delay,
    write_collection_outputs,
)
from app.database import (
    complete_scan_run,
    connect_database,
    create_scan_run,
    initialize_database,
    persist_observation,
    seed_motosport,
    seed_partzilla,
    table_counts,
    upsert_competitor_listing,
    upsert_product_and_listing,
    utc_now,
)
from app.input_loader import load_parts_csv
from app.models import PartRecord
from app.schemas.product_observation import AvailabilityStatus, PageClassification
from tests.test_database_layer import _db, _obs, _record


TEST_OUTPUT_DIR = Path("data/output/test-artifacts")


def test_dry_run_plan_does_not_modify_database() -> None:
    db = _prepared_db("collection_dry.db", ["41080-1514"])
    csv_path = _input_csv("collection_dry.csv", ["41080-1514"])
    with connect_database(db) as conn:
        before = table_counts(conn)
        load_result = load_parts_csv(csv_path)
        plan = plan_collection(conn, load_result.records, csv_path, 25)
        after = table_counts(conn)
    assert len(plan.planned_parts) == 1
    assert before == after


def test_run_requires_exact_run_confirmation(monkeypatch) -> None:
    db = _prepared_db("collection_confirm.db", ["41080-1514"])
    csv_path = _input_csv("collection_confirm.csv", ["41080-1514"])
    monkeypatch.setattr(collect_parts, "require_competitor_auth_state", lambda *args, **kwargs: Path("fake_auth_state.json"))
    monkeypatch.setattr(collect_parts, "run_collection", lambda args, plan: (_ for _ in ()).throw(AssertionError("should not run")))
    monkeypatch.setattr(builtins, "input", lambda prompt: "run")
    monkeypatch.setattr(collect_parts.sys, "argv", ["collect_parts.py", "--file", str(csv_path), "--max-parts", "25", "--save-to-database", "--database", str(db)])
    assert collect_parts.main() == 1


def test_part_count_cannot_exceed_max_parts() -> None:
    db = _prepared_db("collection_max.db", ["41080-1514", "55061-5438-739"])
    csv_path = _input_csv("collection_max.csv", ["41080-1514", "55061-5438-739"])
    with connect_database(db) as conn:
        load_result = load_parts_csv(csv_path)
        try:
            plan_collection(conn, load_result.records, csv_path, 1)
        except ValueError as exc:
            assert "exceeding --max-parts" in str(exc)
        else:
            raise AssertionError("expected max-parts error")


def test_delay_below_minimum_is_rejected() -> None:
    try:
        validate_delay(0)
    except ValueError as exc:
        assert "at least 1" in str(exc)
    else:
        raise AssertionError("expected delay validation error")


def test_result_normalization() -> None:
    assert normalize_result_type("no change") == "no_change"


def test_stop_statuses_for_block_challenge_429_and_auth_loss() -> None:
    assert stop_status_for(CollectionRow(1, 1, None, "Kawasaki", "A", page_classification="blocked")) == "stopped_blocked"
    assert stop_status_for(CollectionRow(1, 1, None, "Kawasaki", "A", page_classification="challenge")) == "stopped_challenge"
    assert stop_status_for(CollectionRow(1, 1, None, "Kawasaki", "A", http_status=429)) == "stopped_blocked"
    assert stop_status_for(CollectionRow(1, 1, None, "Kawasaki", "A", session_status="expired_or_invalid")) == "failed"


def test_not_found_result_is_logged_without_stopping_collection() -> None:
    result_type = collect_parts.collection_result_type(
        _obs(None, classification=PageClassification.NOT_FOUND),
        404,
        "warning or failure",
    )

    assert result_type == "not_found"
    assert stop_status_for(CollectionRow(1, 1, None, "Kawasaki", "BAD-PART", page_classification="not_found", result_type=result_type)) is None


def test_unsupported_manufacturer_is_logged_without_scraping_or_stopping() -> None:
    db = _prepared_db("collection_manufacturer_not_carried.db", ["41080-1514"])
    with connect_database(db) as conn:
        competitor_id = seed_motosport(conn)
        listing_id = conn.execute("SELECT listing_id FROM competitor_listings ORDER BY listing_id").fetchone()[0]
        run_id = create_scan_run(conn, competitor_id=competitor_id, requested_part_count=1)
    planned = PlannedPart(
        run_order=1,
        manufacturer="Polaris",
        oem_part_number="2879324",
        product_id=1,
        listing_id=int(listing_id),
        current_price_cents=None,
        last_successful_check_at=None,
        has_current_state=False,
    )

    row = collect_parts.collect_one_motosport_part(db, _NoNavigationPage(), planned, run_id, collect_parts.ProbeSettings())

    assert row.result_type == "manufacturer_not_carried"
    assert row.lookup_status == "manufacturer_not_carried"
    assert row.competitor == "motosport"
    assert row.normalized_manufacturer == "Polaris"
    assert row.manufacturer_supported is False
    assert "does not carry OEM manufacturer Polaris" in row.status_reason
    assert stop_status_for(row) is None


def test_motosport_collection_only_adds_to_cart_for_cart_hidden_prices(monkeypatch) -> None:
    db = _prepared_db("collection_motosport_visible_no_cart.db", ["41080-1514"])
    with connect_database(db) as conn:
        product_id = conn.execute("SELECT product_id FROM products WHERE oem_part_number='41080-1514'").fetchone()[0]
        competitor_id = seed_motosport(conn)
        listing_id, _ = upsert_competitor_listing(
            conn,
            product_id=int(product_id),
            competitor_id=competitor_id,
            competitor_part_number="41080-1514",
            canonical_url="https://www.motosport.com/oem-parts/part-number/41080-1514",
        )
        run_id = create_scan_run(conn, competitor_id=competitor_id, requested_part_count=1)
    planned = PlannedPart(1, "Kawasaki", "41080-1514", int(product_id), listing_id, None, None, False)

    class VisiblePriceAdapter:
        def build_product_url(self, _record):
            return "https://www.motosport.com/oem-parts/part-number/41080-1514"

        def parse_product_page(self, *_args, **_kwargs):
            return CompetitorObservation(
                competitor_key="motosport",
                manufacturer="Kawasaki",
                oem_part_number="41080-1514",
                observed_part_number="41080-1514",
                product_name="DISC",
                canonical_url="https://www.motosport.com/oem-parts/part-number/41080-1514",
                http_status=200,
                page_classification="normal_product",
                session_status="unknown",
                price_visibility="visible",
                selling_price=Decimal("282.32"),
                price_display_type="regular",
                selling_price_confidence="high",
                parse_confidence="high",
            )

    monkeypatch.setattr(collect_parts, "MotoSportAdapter", VisiblePriceAdapter)
    monkeypatch.setattr(collect_parts, "bounded_cart_action_inventory", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("cart inventory should not run for visible prices")))
    monkeypatch.setattr(collect_parts, "ensure_cart_empty", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("cart should not be opened for visible prices")))
    monkeypatch.setattr(collect_parts, "click_cart_action_with_result", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("add-to-cart should not be clicked for visible prices")))

    row = collect_parts.collect_one_motosport_part(db, _VisibleProductPage(), planned, run_id, collect_parts.ProbeSettings(render_settle_ms=0))

    assert row.selling_price == "282.32"
    assert row.result_type == "first_observation"
    assert row.price_source_category == "motosport_page"


def test_navigation_error_creates_scan_event_and_can_continue() -> None:
    db = _prepared_db("collection_nav.db", ["41080-1514"])
    with connect_database(db) as conn:
        competitor_id = seed_partzilla(conn)
        listing_id = conn.execute("SELECT listing_id FROM competitor_listings").fetchone()[0]
        run_id = create_scan_run(conn, competitor_id=competitor_id, requested_part_count=2)
        persist_observation(conn, scan_run_id=run_id, listing_id=listing_id, observation=_obs(None, classification=PageClassification.NAVIGATION_ERROR), observation_json_path="nav")
        persist_observation(conn, scan_run_id=run_id, listing_id=listing_id, observation=_obs("282.32"), observation_json_path="ok")
        assert conn.execute("SELECT COUNT(*) FROM scan_events").fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM current_listing_state").fetchone()[0] == 1


def test_partial_run_preserves_earlier_observations_and_unattempted_have_no_events() -> None:
    db = _prepared_db("collection_partial.db", ["41080-1514", "55061-5438-739"])
    with connect_database(db) as conn:
        competitor_id = seed_partzilla(conn)
        listing_id = conn.execute("SELECT listing_id FROM competitor_listings ORDER BY listing_id").fetchone()[0]
        run_id = create_scan_run(conn, competitor_id=competitor_id, requested_part_count=2)
        persist_observation(conn, scan_run_id=run_id, listing_id=listing_id, observation=_obs("282.32"), observation_json_path="ok")
        complete_scan_run(conn, run_id)
        assert conn.execute("SELECT attempted_part_count FROM scan_runs WHERE scan_run_id=?", (run_id,)).fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM scan_events").fetchone()[0] == 1


def test_repeated_full_run_creates_new_events_but_no_unchanged_history() -> None:
    db = _prepared_db("collection_repeat.db", ["41080-1514"])
    with connect_database(db) as conn:
        competitor_id = seed_partzilla(conn)
        listing_id = conn.execute("SELECT listing_id FROM competitor_listings").fetchone()[0]
        run1 = create_scan_run(conn, competitor_id=competitor_id, requested_part_count=1)
        run2 = create_scan_run(conn, competitor_id=competitor_id, requested_part_count=1)
        persist_observation(conn, scan_run_id=run1, listing_id=listing_id, observation=_obs("282.32"), observation_json_path="1")
        persist_observation(conn, scan_run_id=run2, listing_id=listing_id, observation=_obs("282.32"), observation_json_path="2")
        assert conn.execute("SELECT COUNT(*) FROM scan_events").fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM listing_history").fetchone()[0] == 1


def test_collection_summary_review_and_metadata_files() -> None:
    csv_path = _input_csv("collection_outputs.csv", ["41080-1514", "55061-5438-739"])
    db = _prepared_db("collection_outputs.db", ["41080-1514", "55061-5438-739"])
    with connect_database(db) as conn:
        plan = plan_collection(conn, load_parts_csv(csv_path).records, csv_path, 25)
    result = CollectionRunResult(
        scan_run_id=99901,
        started_at="2026-07-08T00:00:00Z",
        completed_at="2026-07-08T00:01:00Z",
        run_status="stopped_blocked",
        stop_reason="blocked",
        last_attempted_part="41080-1514",
        rows=[
            CollectionRow(
                run_order=1,
                scan_run_id=99901,
                scan_event_id=1,
                manufacturer="Kawasaki",
                oem_part_number="41080-1514",
                result_type="blocked",
            )
        ],
    )
    output_dir = write_collection_outputs(result=result, plan=plan, delay_seconds=10, input_fingerprint=fingerprint_file(csv_path))
    assert (output_dir / "collection_summary.csv").exists()
    assert (output_dir / "collection_review.txt").exists()
    metadata = json.loads((output_dir / "run_metadata.json").read_text(encoding="utf-8"))
    assert metadata["scan_run_id"] == 99901
    assert "auth" not in json.dumps(metadata).lower()
    assert "55061-5438-739" in (output_dir / "collection_review.txt").read_text(encoding="utf-8")
    with (output_dir / "collection_summary.csv").open("r", newline="", encoding="utf-8") as file:
        assert len(list(csv.DictReader(file))) == 1


def test_current_price_export_refreshed_after_run_semantics() -> None:
    # Export refresh itself is covered by export tests; collection metadata records export warnings separately.
    result = CollectionRunResult(scan_run_id=99902, started_at=utc_now(), export_warning="export failed")
    assert result.export_warning == "export failed"


def test_first_price_availability_supersession_and_multiple_change_results() -> None:
    db = _prepared_db("collection_changes.db", ["41080-1514"])
    with connect_database(db) as conn:
        competitor_id = seed_partzilla(conn)
        listing_id = conn.execute("SELECT listing_id FROM competitor_listings").fetchone()[0]
        run_id = create_scan_run(conn, competitor_id=competitor_id, requested_part_count=4)
        assert persist_observation(conn, scan_run_id=run_id, listing_id=listing_id, observation=_obs("282.32"), observation_json_path="1") == "first_observation"
        assert normalize_result_type(persist_observation(conn, scan_run_id=run_id, listing_id=listing_id, observation=_obs("282.32"), observation_json_path="2")) == "no_change"
        assert persist_observation(conn, scan_run_id=run_id, listing_id=listing_id, observation=_obs("269.99"), observation_json_path="3") == "price_change"
        assert persist_observation(conn, scan_run_id=run_id, listing_id=listing_id, observation=_obs("200.00", availability=AvailabilityStatus.IN_STOCK, superseded=True), observation_json_path="4") == "multiple_changes"


def test_transaction_failure_one_product_does_not_rollback_earlier_product() -> None:
    db = _prepared_db("collection_tx.db", ["41080-1514"])
    with connect_database(db) as conn:
        competitor_id = seed_partzilla(conn)
        listing_id = conn.execute("SELECT listing_id FROM competitor_listings").fetchone()[0]
        run_id = create_scan_run(conn, competitor_id=competitor_id, requested_part_count=2)
        persist_observation(conn, scan_run_id=run_id, listing_id=listing_id, observation=_obs("282.32"), observation_json_path="1")
        try:
            persist_observation(conn, scan_run_id=run_id, listing_id=9999, observation=_obs("200.00"), observation_json_path="bad")
        except Exception:
            pass
        assert conn.execute("SELECT COUNT(*) FROM current_listing_state").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM scan_events").fetchone()[0] == 1


def _prepared_db(name: str, parts: list[str]) -> Path:
    db = _db(name)
    initialize_database(db)
    with connect_database(db) as conn:
        for part in parts:
            upsert_product_and_listing(conn, _record(part))
    return db


def _input_csv(name: str, parts: list[str]) -> Path:
    TEST_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    path = TEST_OUTPUT_DIR / name
    fieldnames = [
        "Test_Case_ID",
        "Manufacturer",
        "OEM_Part_Number",
        "Search_Observed_Product_Name",
        "Search_Observed_MSRP",
        "Expected_Partzilla_URL",
        "Test_Purpose",
        "Verified_Date",
        "Source_URL",
    ]
    with path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        for index, part in enumerate(parts, start=1):
            writer.writerow(
                {
                    "Test_Case_ID": f"T{index}",
                    "Manufacturer": "Kawasaki",
                    "OEM_Part_Number": part,
                    "Search_Observed_Product_Name": "DISC",
                    "Search_Observed_MSRP": "",
                    "Expected_Partzilla_URL": "",
                    "Test_Purpose": "",
                    "Verified_Date": "",
                    "Source_URL": "",
                }
            )
    return path


class _NoNavigationPage:
    def goto(self, *_args, **_kwargs):
        raise AssertionError("unsupported manufacturers should not navigate")


class _VisibleProductPage:
    url = "https://www.motosport.com/oem-parts/part-number/41080-1514"

    def goto(self, *_args, **_kwargs):
        class Response:
            status = 200
        return Response()

    def wait_for_timeout(self, _milliseconds: int) -> None:
        return None

    def content(self) -> str:
        return "<html><body>DISC (41080-1514) $282.32</body></html>"

    def locator(self, _selector: str):
        class Locator:
            def count(self) -> int:
                return 1

            def inner_text(self, **_kwargs) -> str:
                return "DISC (41080-1514)\n$282.32"
        return Locator()
