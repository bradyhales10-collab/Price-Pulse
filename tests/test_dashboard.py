from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from fastapi.testclient import TestClient

from app.database import connect_database, create_scan_run, initialize_database, persist_observation, seed_motosport, seed_partzilla, table_counts, upsert_competitor_listing, upsert_product_and_listing
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
from app.web.app import create_app
from app.web.queries import dashboard_data, quality_data


TEST_OUTPUT_DIR = Path("data/output/test-artifacts")


def test_dashboard_loads_and_kpis_are_correct() -> None:
    db = _dashboard_db("dashboard.db")
    response = _client(db).get("/")

    assert response.status_code == 200
    assert "Part Pulse Intelligence" in response.text
    assert "Management Summary" in response.text
    assert "Ready to export" in response.text
    assert "OEM Margin Snapshot" in response.text
    assert "Competitor Snapshot" in response.text
    assert "Our Avg Margin" in response.text
    assert "Avg Margin Using Our Cost" in response.text
    assert "Monitored Products" in response.text
    assert ">6<" in response.text
    assert "Discounted Products" in response.text
    assert "Needs Review" in response.text
    assert "25 / 25" in response.text


def test_dashboard_counts_products_once_across_multiple_competitors() -> None:
    db = _dashboard_db("dashboard_distinct_products.db")
    with connect_database(db) as conn:
        product_id = conn.execute("SELECT product_id FROM products WHERE oem_part_number='41080-1514'").fetchone()[0]
        upsert_competitor_listing(
            conn,
            product_id=product_id,
            competitor_id=seed_motosport(conn),
            competitor_part_number="41080-1514",
            canonical_url="https://www.motosport.com/oem-parts/part-number/41080-1514",
        )

    data = dashboard_data(db)
    kpis = {item["label"]: item["value"] for item in data["kpis"]}

    assert kpis["Monitored Products"] == 7
    assert sum(item["count"] for item in data["manufacturers"]) == 7


def test_competitor_snapshot_our_margin_uses_only_priced_competitor_rows() -> None:
    db = _empty_db("competitor_snapshot_margin.db")
    with connect_database(db) as conn:
        competitor_id = seed_motosport(conn)
        run_id = create_scan_run(conn, competitor_id=competitor_id, requested_part_count=1)
        priced_id, _, _, _ = upsert_product_and_listing(
            conn,
            PartRecord(test_case_id="", manufacturer="Honda", oem_part_number="PRICED", search_observed_product_name="Priced"),
        )
        unpriced_id, _, _, _ = upsert_product_and_listing(
            conn,
            PartRecord(test_case_id="", manufacturer="Honda", oem_part_number="UNPRICED", search_observed_product_name="Unpriced"),
        )
        for product_id, sku, price, cost in [(priced_id, "SKU-P", 10000, 5000), (unpriced_id, "SKU-U", 10000, 9000)]:
            conn.execute(
                """
                INSERT INTO internal_product_state(product_id, internal_sku, our_current_price_cents, current_cost_cents, is_active, updated_at)
                VALUES (?, ?, ?, ?, 1, '2026-07-09T00:00:00Z')
                """,
                (product_id, sku, price, cost),
            )
        listing_id, _ = upsert_competitor_listing(
            conn,
            product_id=priced_id,
            competitor_id=competitor_id,
            competitor_part_number="PRICED",
            canonical_url="https://www.motosport.com/oem-parts/part-number/PRICED",
        )
        upsert_competitor_listing(
            conn,
            product_id=unpriced_id,
            competitor_id=competitor_id,
            competitor_part_number="UNPRICED",
            canonical_url="https://www.motosport.com/oem-parts/part-number/UNPRICED",
        )
        persist_observation(conn, scan_run_id=run_id, listing_id=listing_id, observation=_obs("Honda", "PRICED", "Priced", "95.00", None, None, PriceDisplayType.REGULAR, AvailabilityStatus.IN_STOCK, ParseConfidence.HIGH, False), observation_json_path="observation.json")

    motosport = next(item for item in dashboard_data(db)["competitor_snapshots"] if item["competitor_name"] == "MotoSport")

    assert motosport["product_count"] == 2
    assert motosport["priced_product_count"] == 1
    assert motosport["avg_our_margin"] == "50.00%"


def test_start_fresh_removes_test_data_but_preserves_configuration() -> None:
    db = _dashboard_db("start_fresh.db")
    client = _client(db)

    response = client.post("/data/reset", data={"confirmation": "CLEAR DATA"}, follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/settings?message=All%20pricing%20data%20was%20cleared."
    with connect_database(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM products").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM internal_product_state").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM scan_runs").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM competitors").fetchone()[0] >= 3
        assert conn.execute("SELECT COUNT(*) FROM pricing_rules").fetchone()[0] >= 1


def test_clear_data_requires_typed_confirmation() -> None:
    db = _dashboard_db("clear_data_confirmation.db")
    client = _client(db)

    response = client.post("/data/reset", data={"confirmation": "clear data"}, follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"].startswith("/settings?message=Data%20was%20not%20cleared")
    with connect_database(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM products").fetchone()[0] > 0


def test_dashboard_empty_recent_changes_state() -> None:
    db = _empty_priced_db("empty_changes.db")
    response = _client(db).get("/")

    assert response.status_code == 200
    assert "OEM Margin Snapshot" in response.text
    assert "Competitor Snapshot" in response.text


def test_product_search_and_filters() -> None:
    db = _dashboard_db("filters.db")
    client = _client(db)

    assert "41080-1514" in client.get("/products?search=41080").text
    catalog = client.get("/products").text
    assert catalog.index("Product") < catalog.index("Partzilla") < catalog.index("MotoSport") < catalog.index("Chaparral") < catalog.index("Lowest Competitor") < catalog.index("Gap vs Lowest") < catalog.index("Our Price") < catalog.index("Calc Cost") < catalog.index("Our Margin")
    manufacturer_page = client.get("/products?manufacturer=Honda").text
    assert "Honda" in manufacturer_page
    assert "Y-100" not in manufacturer_page
    discounted_page = client.get("/products?price_type=discounted").text
    assert "Save 13%" not in discounted_page
    assert "41080-1514" not in discounted_page
    availability_page = client.get("/products?availability=in_stock").text
    assert "34028-0327" in availability_page


def test_catalog_pagination() -> None:
    db = _dashboard_db("pagination.db")
    response = _client(db).get("/products?page_size=25&page=1")

    assert response.status_code == 200
    assert "Page 1 of 1" in response.text


def test_product_detail_history_and_scan_timeline_load() -> None:
    db = _dashboard_db("detail.db")
    product_id = _product_id(db, "41080-1514")
    response = _client(db).get(f"/products/{product_id}")

    assert response.status_code == 200
    assert "Our Pricing" in response.text
    assert "Selected Competitor Price" in response.text
    assert "Price History" in response.text
    assert "Scan Event Timeline" in response.text
    assert "Price Change" in response.text


def test_product_detail_prefers_supported_competitor_price_over_not_carried_listing() -> None:
    db = _empty_db("detail_competitor_choice.db")
    with connect_database(db) as conn:
        partzilla_id = seed_partzilla(conn)
        product_id, partzilla_listing_id, _, _ = upsert_product_and_listing(
            conn,
            PartRecord(test_case_id="", manufacturer="KTM", oem_part_number="00050000068", search_observed_product_name="KTM TEST"),
        )
        partzilla_run_id = create_scan_run(conn, competitor_id=partzilla_id, requested_part_count=1)
        conn.execute(
            """
            INSERT INTO scan_events(scan_run_id, listing_id, checked_at, page_classification, session_status,
                navigation_succeeded, price_found, price_parse_confidence, parse_warning_count, parse_warnings)
            VALUES (?, ?, '2026-07-09T00:10:00Z', 'manufacturer_not_carried', 'not_applicable',
                0, 0, 'low', 0, 'partzilla does not carry OEM manufacturer KTM.')
            """,
            (partzilla_run_id, partzilla_listing_id),
        )
        motosport_id = seed_motosport(conn)
        motosport_listing_id, _ = upsert_competitor_listing(
            conn,
            product_id=product_id,
            competitor_id=motosport_id,
            competitor_part_number="00050000068",
            canonical_url="https://www.motosport.com/oem-parts/part-number/00050000068",
        )
        motosport_run_id = create_scan_run(conn, competitor_id=motosport_id, requested_part_count=1)
        persist_observation(
            conn,
            scan_run_id=motosport_run_id,
            listing_id=motosport_listing_id,
            observation=_obs("KTM", "00050000068", "KTM TEST", "77.97", None, None, PriceDisplayType.REGULAR, AvailabilityStatus.SHIPS_IN, ParseConfidence.HIGH, False),
            observation_json_path="motosport-observation.json",
        )

    response = _client(db).get(f"/products/{product_id}")

    assert response.status_code == 200
    assert "MotoSport" in response.text
    assert "Partzilla" in response.text
    assert "$77.97" in response.text
    assert "Manufacturer Not Carried" in response.text
    assert "Open MotoSport" in response.text


def test_scan_runs_and_run_detail_load() -> None:
    db = _dashboard_db("runs.db")
    client = _client(db)

    runs = client.get("/runs")
    assert runs.status_code == 200
    assert "Scan Runs" in runs.text
    assert "Jul 9, 2026" in runs.text
    assert "2026-07-09T00:05:00Z" not in runs.text
    detail = client.get("/runs/1")
    assert detail.status_code == 200
    assert "Scan Run 1" in detail.text
    assert "Scan Events" in detail.text


def test_data_quality_page() -> None:
    db = _dashboard_db("quality.db")
    with connect_database(db) as conn:
        competitor_id = seed_motosport(conn)
        product_id = _product_id(db, "P-100")
        listing_id, _ = upsert_competitor_listing(
            conn,
            product_id=product_id,
            competitor_id=competitor_id,
            competitor_part_number="P-100",
            canonical_url="",
        )
        run_id = create_scan_run(conn, competitor_id=competitor_id, requested_part_count=1)
        conn.execute(
            """
            INSERT INTO scan_events(scan_run_id, listing_id, checked_at, page_classification, session_status,
                navigation_succeeded, price_found, price_parse_confidence, parse_warning_count, parse_warnings)
            VALUES (?, ?, '2026-07-09T00:10:00Z', 'manufacturer_not_carried', 'not_applicable',
                0, 0, 'low', 0, 'motosport does not carry OEM manufacturer Polaris.')
            """,
            (run_id, listing_id),
        )
    response = _client(db).get("/quality")
    data = quality_data(db)

    assert response.status_code == 200
    assert "Not Carried By Competitor" in response.text
    assert "Missing Prices" in response.text
    assert "Low Confidence" in response.text
    assert "Supersession Review" in response.text
    assert "MotoSport" in response.text
    assert "Manufacturer Not Carried" in response.text
    assert [row["oem_part_number"] for row in data["not_carried"]] == ["P-100"]
    assert "P-100" not in [row["oem_part_number"] for row in data["missing_prices"]]
    assert "Run database audit from the command line" not in response.text
    assert "Competitor Review Status" not in response.text


def test_empty_database_handling() -> None:
    db = _empty_db("empty.db")
    response = _client(db).get("/")

    assert response.status_code == 200
    assert "No completed scan runs are available yet." in response.text


def test_missing_database_handling() -> None:
    db = TEST_OUTPUT_DIR / "missing-dashboard.db"
    if db.exists():
        db.unlink()
    response = _client(db).get("/")

    assert response.status_code == 503
    assert "Database not found" in response.text


def test_no_auth_secrets_rendered() -> None:
    db = _dashboard_db("secrets.db")
    text = _client(db).get("/").text.lower()

    assert "cookie" not in text
    assert "token" not in text
    assert "password" not in text
    assert "partzilla_auth_state" not in text


def test_login_sessions_page_shows_competitor_readiness(monkeypatch) -> None:
    db = _dashboard_db("sessions.db")
    monkeypatch.setattr("app.web.app.auth_state_exists", lambda _competitor_key: False)
    monkeypatch.setattr("app.web.app.auth_state_status", lambda _competitor_key: {"exists": False, "updated_at": None, "path": ""})

    response = _client(db).get("/sessions")

    assert response.status_code == 200
    assert "Login Sessions" in response.text
    assert "Partzilla" in response.text
    assert "Needs Saved Login" in response.text
    assert "MotoSport" in response.text
    assert "Not required" in response.text
    assert "cookie" not in response.text.lower()
    assert "token" not in response.text.lower()


def test_login_session_upload_saves_valid_state(monkeypatch) -> None:
    db = _dashboard_db("session-upload.db")
    saved: dict[str, object] = {}

    def fake_save(competitor_key, content):
        saved["competitor_key"] = competitor_key
        saved["content"] = content
        return Path("data/private/partzilla_auth_state.json")

    monkeypatch.setattr("app.web.app.save_uploaded_auth_state", fake_save)

    response = _client(db).post(
        "/sessions/partzilla/upload?filename=session.json",
        content=b'{"cookies":[{"name":"session","domain":".partzilla.com"}],"origins":[]}',
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert saved["competitor_key"] == "partzilla"
    assert "Partzilla%20login%20session%20saved" in response.headers["location"]


def test_login_session_page_queues_desktop_login_refresh(monkeypatch, tmp_path) -> None:
    db = _dashboard_db("session-refresh.db")
    monkeypatch.setattr("app.collection_jobs.LOCAL_LOGIN_REQUEST_DIR", tmp_path / "login_requests")
    client = _client(db)

    page = client.get("/sessions")
    assert page.status_code == 200
    assert "Refresh Login On This Computer" in page.text

    queued = client.post("/sessions/partzilla/refresh-local", follow_redirects=False)
    assert queued.status_code == 303
    assert "Desktop%20Collector%20will%20open%20Partzilla%20login%20refresh%20shortly" in queued.headers["location"]

    claimed = client.post("/collector/agent/login/next?agent_id=test-desktop")
    assert claimed.status_code == 200
    assert claimed.json()["competitor_key"] == "partzilla"
    assert client.post("/collector/agent/login/next?agent_id=test-desktop").status_code == 204


def test_price_check_queues_login_refresh_without_leaving_workflow(monkeypatch, tmp_path) -> None:
    db = _dashboard_db("price-check-login-refresh.db")
    monkeypatch.setattr("app.collection_jobs.LOCAL_LOGIN_REQUEST_DIR", tmp_path / "login_requests")
    client = _client(db)

    response = client.post(
        "/imports/partzilla/refresh-login",
        data={"import_batch_id": "7"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"].startswith("/imports?import_batch_id=7&message=")
    claimed = client.post("/collector/agent/login/next?agent_id=test-desktop")
    assert claimed.status_code == 200
    assert claimed.json()["competitor_key"] == "partzilla"


def test_login_maintenance_is_under_settings_instead_of_main_navigation() -> None:
    db = _dashboard_db("login-settings-navigation.db")
    client = _client(db)

    price_check = client.get("/imports")
    settings = client.get("/settings")

    assert '<a class="" href="/sessions">Login Sessions</a>' not in price_check.text
    assert "Manage Logins" in settings.text
    assert 'href="/sessions"' in settings.text


def test_login_refresh_replaces_duplicate_requests(monkeypatch, tmp_path) -> None:
    db = _dashboard_db("session-refresh-dedupe.db")
    monkeypatch.setattr("app.collection_jobs.LOCAL_LOGIN_REQUEST_DIR", tmp_path / "login_requests")
    client = _client(db)

    first = client.post("/sessions/partzilla/refresh-local", follow_redirects=False)
    second = client.post("/sessions/partzilla/refresh-local", follow_redirects=False)
    assert first.status_code == 303
    assert second.status_code == 303

    claimed = client.post("/collector/agent/login/next?agent_id=test-desktop")
    assert claimed.status_code == 200
    assert client.post("/collector/agent/login/next?agent_id=test-desktop").status_code == 204


def test_desktop_collector_has_visible_start_helper() -> None:
    helper = Path("Start Part Pulse Collector.cmd").read_text(encoding="utf-8")
    setup = Path("setup_local_collector_agent.py").read_text(encoding="utf-8")
    agent = Path("local_collector_agent.py").read_text(encoding="utf-8")

    assert "local_collector_agent.py" in helper
    assert "--config" in helper
    assert "pythonw.exe" not in setup
    assert '"cmd.exe", "/k", "call"' in agent
    assert "CREATE_NEW_CONSOLE" in agent


def test_multi_oem_manufacturer_rendering() -> None:
    db = _dashboard_db("multi_oem.db")
    text = _client(db).get("/").text

    for manufacturer in ["Kawasaki", "Honda", "Yamaha", "Suzuki", "Polaris", "Can-Am"]:
        assert manufacturer in text


def test_read_only_interface_does_not_modify_database() -> None:
    db = _dashboard_db("readonly.db")
    before = _counts(db)
    client = _client(db)
    for path in ["/", "/products", f"/products/{_product_id(db, '41080-1514')}", "/runs", "/runs/1", "/quality"]:
        assert client.get(path).status_code == 200
    assert _counts(db) == before


def _client(db: Path) -> TestClient:
    return TestClient(create_app(db), raise_server_exceptions=False)


def _dashboard_db(name: str) -> Path:
    db = _empty_db(name)
    rows = [
        ("Kawasaki", "41080-1514", "DISC", "282.32", None, None, PriceDisplayType.REGULAR, AvailabilityStatus.SHIPS_IN, ParseConfidence.HIGH, False),
        ("Kawasaki", "34028-0327", "STEP", "32.69", "37.30", 13, PriceDisplayType.DISCOUNTED, AvailabilityStatus.IN_STOCK, ParseConfidence.HIGH, False),
        ("Honda", "H-100", "HONDA TEST", "10.00", None, None, PriceDisplayType.REGULAR, AvailabilityStatus.IN_STOCK, ParseConfidence.HIGH, False),
        ("Yamaha", "Y-100", "YAMAHA TEST", None, None, None, PriceDisplayType.UNKNOWN, AvailabilityStatus.UNKNOWN, ParseConfidence.LOW, False),
        ("Suzuki", "S-100", "SUZUKI TEST", "11.00", None, None, PriceDisplayType.REGULAR, AvailabilityStatus.SHIPS_IN, ParseConfidence.HIGH, True),
        ("Polaris", "P-100", "POLARIS TEST", "12.00", None, None, PriceDisplayType.REGULAR, AvailabilityStatus.SHIPS_IN, ParseConfidence.HIGH, False),
        ("Can-Am", "C-100", "CAN-AM TEST", "13.00", None, None, PriceDisplayType.REGULAR, AvailabilityStatus.SHIPS_IN, ParseConfidence.HIGH, False),
    ]
    with connect_database(db) as conn:
        competitor_id = seed_partzilla(conn)
        run_id = create_scan_run(conn, competitor_id=competitor_id, requested_part_count=25)
        for manufacturer, part, name, price, reference, savings, display, availability, confidence, superseded in rows:
            _, listing_id, _, _ = upsert_product_and_listing(
                conn,
                PartRecord(test_case_id="", manufacturer=manufacturer, oem_part_number=part, search_observed_product_name=name),
            )
            persist_observation(
                conn,
                scan_run_id=run_id,
                listing_id=listing_id,
                observation=_obs(manufacturer, part, name, price, reference, savings, display, availability, confidence, superseded),
                observation_json_path="observation.json",
            )
        first_listing = conn.execute("""
            SELECT l.listing_id FROM competitor_listings l
            JOIN products p ON p.product_id=l.product_id
            WHERE p.oem_part_number='41080-1514'
        """).fetchone()[0]
        persist_observation(
            conn,
            scan_run_id=run_id,
            listing_id=first_listing,
            observation=_obs("Kawasaki", "41080-1514", "DISC", "269.99", None, None, PriceDisplayType.REGULAR, AvailabilityStatus.SHIPS_IN, ParseConfidence.HIGH, False),
            observation_json_path="observation.json",
        )
        conn.execute("""
            UPDATE scan_runs
            SET completed_at='2026-07-09T00:05:00Z', attempted_part_count=25, successful_part_count=25,
                changed_part_count=1, warning_count=0, blocked_count=0, challenge_count=0, error_count=0,
                run_status='completed'
            WHERE scan_run_id=?
        """, (run_id,))
    return db


def _empty_priced_db(name: str) -> Path:
    db = _empty_db(name)
    with connect_database(db) as conn:
        competitor_id = seed_partzilla(conn)
        run_id = create_scan_run(conn, competitor_id=competitor_id, requested_part_count=1)
        _, listing_id, _, _ = upsert_product_and_listing(conn, PartRecord(test_case_id="", manufacturer="Kawasaki", oem_part_number="41080-1514", search_observed_product_name="DISC"))
        persist_observation(conn, scan_run_id=run_id, listing_id=listing_id, observation=_obs("Kawasaki", "41080-1514", "DISC", "282.32", None, None, PriceDisplayType.REGULAR, AvailabilityStatus.SHIPS_IN, ParseConfidence.HIGH, False), observation_json_path="observation.json")
        conn.execute("UPDATE scan_runs SET completed_at='2026-07-09T00:01:00Z', attempted_part_count=1, successful_part_count=1, run_status='completed' WHERE scan_run_id=?", (run_id,))
    return db


def _empty_db(name: str) -> Path:
    TEST_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    db = TEST_OUTPUT_DIR / name
    if db.exists():
        db.unlink()
    initialize_database(db)
    return db


def _obs(
    manufacturer: str,
    part: str,
    name: str,
    price: str | None,
    reference: str | None,
    savings: int | None,
    display: PriceDisplayType,
    availability: AvailabilityStatus,
    confidence: ParseConfidence,
    superseded: bool,
) -> ProductObservation:
    return ProductObservation(
        test_case_id="",
        manufacturer=manufacturer,
        oem_part_number=part,
        observed_part_number=part,
        requested_url="",
        final_url="",
        canonical_url="",
        http_status=200,
        page_title="",
        page_classification=PageClassification.NORMAL_PRODUCT,
        price_visibility=PriceVisibility.VISIBLE if price else PriceVisibility.UNKNOWN,
        classification_confidence=ParseConfidence.HIGH,
        classification_evidence=[],
        product_name=name,
        manufacturer_display=manufacturer.upper(),
        msrp_raw=None,
        msrp=None,
        selling_price_raw=f"${price}" if price else None,
        selling_price=Decimal(price) if price else None,
        availability_raw=availability.value,
        availability_status=availability,
        shipping_estimate=None,
        access_context=AccessContext.AUTHENTICATED_SESSION,
        session_status=SessionStatus.AUTHENTICATED,
        superseded_by_raw="NEXT-100" if superseded else None,
        supersession_detected=superseded,
        price_parse_confidence=confidence,
        price_validation_status=PriceValidationStatus.NOT_MANUALLY_VALIDATED,
        parse_confidence=confidence,
        parse_warnings=[],
        checked_at="2026-07-09T00:00:00Z",
        reference_price_raw=f"${reference}" if reference else None,
        reference_price=Decimal(reference) if reference else None,
        savings_percent=savings,
        price_display_type=display,
        selling_price_confidence=confidence,
        reference_price_confidence=ParseConfidence.HIGH if reference else ParseConfidence.LOW,
    )


def _product_id(db: Path, part: str) -> int:
    with connect_database(db) as conn:
        return int(conn.execute("SELECT product_id FROM products WHERE oem_part_number=?", (part,)).fetchone()[0])


def _counts(db: Path) -> dict[str, int]:
    with connect_database(db) as conn:
        return table_counts(conn)
