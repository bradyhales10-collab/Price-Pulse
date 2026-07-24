from __future__ import annotations

import sqlite3
import json
import zipfile
from pathlib import Path

from fastapi.testclient import TestClient

from app.collection_jobs import PlannedCollectionPart, job_status, plan_import_collection, start_collection_job, start_price_collection_job, validate_collection_request
from app.comparison import ComparisonFilters, comparison_rows
from app.database import connect_database, initialize_database, utc_now
from app.exports.review_export import REVIEW_COLUMNS, export_review
from app.imports import confirm_import, preview_import, save_upload
from app.input_loader import load_parts_csv
import app.manufacturer_registry as manufacturer_registry
from app.manufacturer_registry import competitor_supports_manufacturer, normalize_manufacturer, partzilla_slug_for
from app.reviews import comparison_review_rows, review_rows
from app.web.app import create_app
from app.xlsx_utils import read_rows, write_workbook


TEST_OUTPUT_DIR = Path("data/output/test-artifacts")


def test_xlsx_import_validates_preserves_values_and_confirm_updates_internal_state() -> None:
    db = _empty_db("import_xlsx.db")
    upload_path = TEST_OUTPUT_DIR / "import_upload.xlsx"
    write_workbook(
        upload_path,
        {
            "Notes": [["Ignore this sheet"]],
            "Upload Data": [
                ["Internal_SKU", "Manufacturer", "OEM_Part_Number", "Our_Current_Price", "Product_Name", "Current_Cost", "Units_Sold_12M", "Inventory_Qty", "Scan_Priority"],
                ["SKU-001", "Kawasaki", "00123", "12.34", "Leading Zero Part", "7.00", "3", "4", "high"],
                ["SKU-002", "canam", "07JAZ-001070A", "$8.75", "Can-Am Alias Part", "3.00", "2", "6", "low"],
                ["DUP", "Yamaha", "Y-1", "10.00", "", "", "", "", ""],
                ["DUP", "Yamaha", "Y-2", "11.00", "", "", "", "", ""],
                ["SKU-004", "Honda", "H-100", "not money", "", "", "", "", ""],
            ],
        },
    )

    result = save_upload(db, filename="parts.xlsx", content=upload_path.read_bytes())
    assert result.worksheets == ["Notes", "Upload Data"]
    assert result.default_worksheet == "Upload Data"
    assert result.auto_mapping["OEM_Part_Number"] == "oem_part_number"

    preview = preview_import(db, result.import_batch_id)
    assert preview.rows_read == 5
    assert preview.valid_rows == 2
    assert preview.invalid_rows == 3
    assert preview.unsupported_manufacturer_mappings == 0
    assert _count(db, "products") == 0

    confirm_import(db, result.import_batch_id)
    with connect_database(db) as conn:
        rows = conn.execute(
            """
            SELECT p.manufacturer, p.oem_part_number, ips.internal_sku, ips.our_current_price_cents,
                   ips.current_cost_cents, ips.units_sold_12m, ips.inventory_qty, ips.scan_priority
            FROM products p JOIN internal_product_state ips ON ips.product_id=p.product_id
            ORDER BY ips.internal_sku
            """
        ).fetchall()
    assert [dict(row) for row in rows] == [
        {
            "manufacturer": "Kawasaki",
            "oem_part_number": "00123",
            "internal_sku": "SKU-001",
            "our_current_price_cents": 1234,
            "current_cost_cents": 700,
            "units_sold_12m": 3,
            "inventory_qty": 4,
            "scan_priority": "high",
        },
        {
            "manufacturer": "Can-Am",
            "oem_part_number": "07JAZ-001070A",
            "internal_sku": "SKU-002",
            "our_current_price_cents": 875,
            "current_cost_cents": 300,
            "units_sold_12m": 2,
            "inventory_qty": 6,
            "scan_priority": "low",
        },
    ]

    confirm_import(db, result.import_batch_id)
    assert _count(db, "products") == 2


def test_csv_upload_accepts_plain_csv_and_rejects_unsafe_or_oversized_files() -> None:
    db = _empty_db("import_csv.db")

    result = save_upload(
        db,
        filename="prices.csv",
        content=b"Internal_SKU,Manufacturer,OEM_Part_Number,Our_Current_Price\nSKU-CSV,Honda,H-1,19.99\n",
    )
    assert result.worksheets == ["CSV"]
    assert result.headers[0] == "Internal_SKU"

    for filename in ["macro.xlsm", "tool.exe", "archive.zip"]:
        try:
            save_upload(db, filename=filename, content=b"x")
        except ValueError as exc:
            assert "Unsupported upload type" in str(exc)
        else:
            raise AssertionError(f"{filename} should have been rejected")

    try:
        save_upload(db, filename="huge.csv", content=b"x" * 11, max_bytes=10)
    except ValueError as exc:
        assert "too large" in str(exc)
    else:
            raise AssertionError("oversized upload should have been rejected")


def test_inventory_export_template_maps_to_production_price_check_fields() -> None:
    db = _empty_db("inventory_export_template.db")
    upload_path = TEST_OUTPUT_DIR / "inventory_export_template.xlsx"
    write_workbook(
        upload_path,
        {
            "Sheet1": [
                ["OEM", "Prod No", "Stock Name", "MF ID", "Qty On\nHold Or Transfer", "Qty Sold", "Total Qty\nAvail", "Cost", "Calc Cost", "MSRP", "Price", "MAP", "Discontinued", "Days Not\n Available", "Total Sold"],
                ["KTM", "131307016723", "Sealkit 48mm SKF black | ON DEMAND", "RP10048T", "0", "234", "2387", "34.39", "31.00", "78.53", "66.75", "false", " ", "30", "15619.5"],
                ["CAN AM", "131307016723", "Filter oil", "420956744", "0", "5", "10", "3.00", "2.50", "18.49", "12.99", "false", "10/12/2021 10:32 AM", "30", "64.95"],
            ],
        },
    )

    result = save_upload(db, filename=upload_path.name, content=upload_path.read_bytes())

    assert result.default_worksheet == "Sheet1"
    assert result.auto_mapping == {
        "OEM": "manufacturer",
        "Prod No": "internal_sku",
        "Stock Name": "product_name",
        "MF ID": "oem_part_number",
        "Total Qty\nAvail": "inventory_qty",
        "Calc Cost": "current_cost",
        "Price": "our_current_price",
        "Discontinued": "discontinued",
    }

    preview = preview_import(db, result.import_batch_id)
    assert preview.rows_read == 2
    assert preview.valid_rows == 2
    assert preview.invalid_rows == 0
    assert preview.unsupported_manufacturer_mappings == 1  # KTM is not carried by Partzilla but is supported by MotoSport/Chaparral.

    confirm_import(db, result.import_batch_id)
    with connect_database(db) as conn:
        rows = conn.execute(
            """
            SELECT p.manufacturer, p.oem_part_number, p.product_name, ips.internal_sku,
                   ips.our_current_price_cents, ips.current_cost_cents, ips.inventory_qty, ips.is_active
            FROM products p JOIN internal_product_state ips ON ips.product_id=p.product_id
            ORDER BY ips.internal_sku
            """
        ).fetchall()

    assert len(rows) == 2
    ktm_row = next(dict(row) for row in rows if row["manufacturer"] == "KTM")
    assert ktm_row == {
        "manufacturer": "KTM",
        "oem_part_number": "RP10048T",
        "product_name": "Sealkit 48mm SKF black | ON DEMAND",
        "internal_sku": "131307016723",
        "our_current_price_cents": 6675,
        "current_cost_cents": 3100,
        "inventory_qty": 2387,
        "is_active": 1,
    }
    can_am_row = next(dict(row) for row in rows if row["manufacturer"] == "Can-Am")
    assert can_am_row["internal_sku"] == "131307016723"
    assert can_am_row["current_cost_cents"] == 250
    assert can_am_row["is_active"] == 0

    update_path = TEST_OUTPUT_DIR / "inventory_export_template_update.xlsx"
    write_workbook(
        update_path,
        {
            "Sheet1": [
                ["OEM", "Prod No", "Stock Name", "MF ID", "Total Qty\nAvail", "Cost", "Calc Cost", "Price", "Discontinued"],
                ["CAN AM", "131307016723", "Filter oil", "420956744", "12", "99.00", "4.00", "14.99", "false"],
            ],
        },
    )
    updated = save_upload(db, filename=update_path.name, content=update_path.read_bytes())
    confirm_import(db, updated.import_batch_id)
    updated_row = next(row for row in comparison_rows(db) if row["manufacturer"] == "Can-Am")
    assert updated_row["our_current_price"] == "14.99"
    assert updated_row["current_cost"] == "4.00"
    assert updated_row["our_gross_margin_pct"] == "73.32"


def test_xlsx_reader_accepts_excel_relationship_targets_that_include_xl_prefix() -> None:
    db = _empty_db("import_excel_target.db")
    upload_path = TEST_OUTPUT_DIR / "excel_target.xlsx"
    write_workbook(
        upload_path,
        {
            "Upload Data": [
                ["Internal_SKU", "Manufacturer", "OEM_Part_Number", "Our_Current_Price"],
                ["SKU-X", "Honda", "18327-MEN-A30", "44.99"],
            ],
        },
    )
    _rewrite_workbook_relationship_target(upload_path, "Target=\"worksheets/sheet1.xml\"", "Target=\"xl/worksheets/sheet1.xml\"")

    result = save_upload(db, filename="excel-target.xlsx", content=upload_path.read_bytes())
    preview = preview_import(db, result.import_batch_id)

    assert result.default_worksheet == "Upload Data"
    assert preview.valid_rows == 1


def test_manufacturer_registry_supports_configured_coverage_and_aliases() -> None:
    assert normalize_manufacturer("can am") == "Can-Am"
    assert normalize_manufacturer("CAN_AM") == "Can-Am"
    assert normalize_manufacturer("Sea Doo") == "Sea-Doo"
    assert normalize_manufacturer("Ski Doo") == "Ski-Doo"
    assert normalize_manufacturer("Gas Gas") == "GasGas"
    assert normalize_manufacturer("ArcticCat") == "Arctic Cat"
    assert partzilla_slug_for("canam") == "can-am"
    assert partzilla_slug_for("sea doo") == "sea-doo"
    assert partzilla_slug_for("ski doo") == "ski-doo"
    assert partzilla_slug_for("Kawasaki") == "kawasaki"
    assert competitor_supports_manufacturer("partzilla", "Honda") is True
    assert competitor_supports_manufacturer("motosport", "Honda") is True
    assert competitor_supports_manufacturer("partzilla", "Polaris") is True
    assert competitor_supports_manufacturer("motosport", "Polaris") is False
    assert competitor_supports_manufacturer("partzilla", "KTM") is False
    assert competitor_supports_manufacturer("motosport", "KTM") is True
    assert competitor_supports_manufacturer("chaparral", "Honda") is True
    assert competitor_supports_manufacturer("chaparral", "Polaris") is True
    assert competitor_supports_manufacturer("chaparral", "KTM") is True
    assert competitor_supports_manufacturer("chaparral", "Husqvarna") is True
    assert competitor_supports_manufacturer("chaparral", "GasGas") is True
    assert competitor_supports_manufacturer("chaparral", "Triumph") is True
    assert competitor_supports_manufacturer("partzilla", "Triumph") is False
    assert competitor_supports_manufacturer("motosport", "Triumph") is False
    assert partzilla_slug_for("Unknown OEM") is None


def test_settings_page_edits_competitor_oem_coverage(monkeypatch) -> None:
    config_path = TEST_OUTPUT_DIR / "manufacturer_coverage_settings_test.json"
    TEST_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        json.dumps(
            {
                "manufacturers": [
                    {"display_name": "Honda", "aliases": ["honda"], "partzilla_slug": "honda"},
                    {"display_name": "KTM", "aliases": ["ktm"], "partzilla_slug": None},
                ],
                "competitors": {
                    "partzilla": ["Honda"],
                    "motosport": ["Honda", "KTM"],
                    "chaparral": ["Honda"],
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(manufacturer_registry, "MANUFACTURER_COVERAGE_CONFIG", config_path)
    client = TestClient(create_app(_empty_db("settings_coverage.db")), raise_server_exceptions=False)

    page = client.get("/settings")
    assert page.status_code == 200
    assert "Competitor OEM Coverage" in page.text
    assert "1 of 2 OEMs selected" in page.text
    assert 'class="coverage-check selected"' in page.text
    assert 'name="coverage_partzilla" value="Honda" checked' in page.text
    assert 'name="coverage_partzilla" value="KTM"' in page.text

    response = client.post(
        "/settings/coverage",
        content="coverage_partzilla=Honda&coverage_partzilla=KTM&coverage_motosport=Honda&coverage_chaparral=Honda",
        headers={"content-type": "application/x-www-form-urlencoded"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    saved = json.loads(config_path.read_text(encoding="utf-8"))
    assert saved["competitors"]["partzilla"] == ["Honda", "KTM"]


def test_comparison_formulas_filters_and_review_export() -> None:
    db = _comparison_db()

    rows = comparison_rows(db)
    assert len(rows) == 2
    kawasaki = next(row for row in rows if row["oem_part_number"] == "K-PRICE")
    assert kawasaki["price_difference_dollars"] == "2.34"
    assert kawasaki["price_difference_pct"] == "23.40"
    assert kawasaki["our_gross_margin_pct"] == "43.27"
    assert kawasaki["margin_at_partzilla_price"] == "30.00"
    assert kawasaki["our_price_class"] == "price-above-competitor"

    assert [row["oem_part_number"] for row in comparison_rows(db, ComparisonFilters(price_position="above"))] == ["K-PRICE"]
    assert [row["oem_part_number"] for row in comparison_rows(db, ComparisonFilters(missing_competitor_price=True))] == ["H-MISSING"]
    assert [row["oem_part_number"] for row in comparison_rows(db, ComparisonFilters(competitor_discounted=True))] == ["K-PRICE"]

    export_path = export_review(rows, TEST_OUTPUT_DIR / "exports")
    exported = read_rows(export_path, "Pricing Review")
    assert exported[0] == REVIEW_COLUMNS
    assert "Chaparral_Price" in exported[0]
    assert "Original_Price" in exported[0]
    assert exported[0].index("Updated_Price") > exported[0].index("Suggested_Price")
    assert exported[0][-1] == "New_Margin_Pct"
    assert exported[1][exported[0].index("Updated_Price")] == ""
    with zipfile.ZipFile(export_path) as archive:
        sheet_xml = archive.read("xl/worksheets/sheet1.xml").decode("utf-8")
    for money_cell in ["I2", "J2", "K2", "L2", "M2", "P2"]:
        assert f'<c r="{money_cell}" s="2"' in sheet_xml
    for percent_cell in ["N2", "Q2"]:
        assert f'<c r="{percent_cell}" s="3"' in sheet_xml


def test_comparison_excludes_motosport_cart_hidden_reference_from_lowest_price() -> None:
    db = _comparison_db("comparison_cart_hidden.db")
    with connect_database(db) as conn:
        product_id = conn.execute("SELECT product_id FROM products WHERE oem_part_number='K-PRICE'").fetchone()["product_id"]
        conn.execute(
            """
            INSERT INTO competitor_probe_results(competitor_key, product_id, manufacturer, oem_part_number, url, checked_at,
                http_status, page_classification, selling_price_cents, reference_price_cents, savings_percent,
                price_visibility, price_display_type, result_type,
                availability_raw, availability_status, parse_confidence, warnings_json, raw_result_json, created_at)
            VALUES ('motosport', ?, 'Kawasaki', 'K-PRICE', 'https://www.motosport.com/oem-parts/part-number/K-PRICE',
                ?, 200, 'normal_product', NULL, 500, NULL, 'see_price_in_cart', 'cart_price_hidden', 'price_hidden_in_cart',
                'Expected to Ship in 4-9 Days', 'ships_in', 'high', '["selling_price_hidden_in_cart"]', '{}', ?)
            """,
            (product_id, utc_now(), utc_now()),
        )

    row = next(item for item in comparison_rows(db) if item["oem_part_number"] == "K-PRICE")
    page = TestClient(create_app(db), raise_server_exceptions=False).get("/comparison?hidden_competitor_price=1")

    assert row["motosport_hidden_price"] is True
    assert row["motosport_reference_price"] == "5.00"
    assert row["lowest_competitor_name"] == "Partzilla"
    assert row["lowest_competitor_price"] == "10.00"
    assert page.status_code == 200
    assert "Price in cart" in page.text
    assert "Reference:" not in page.text


def test_comparison_layout_is_compact_and_highlights_lowest_our_price() -> None:
    db = _comparison_db("comparison_compact_layout.db")
    with connect_database(db) as conn:
        conn.execute(
            "UPDATE internal_product_state SET our_current_price_cents=900 WHERE product_id=(SELECT product_id FROM products WHERE oem_part_number='K-PRICE')"
        )

    row = next(item for item in comparison_rows(db) if item["oem_part_number"] == "K-PRICE")
    page = TestClient(create_app(db), raise_server_exceptions=False).get("/comparison")

    assert row["our_price_class"] == "price-below-competitors"
    assert 'class="price-below-competitors"' in page.text
    assert "Show Lower Prices" in page.text
    assert "Show All Prices" in page.text
    assert "Show Hidden Prices" not in page.text
    assert page.text.index("<th>Product</th>") < page.text.index("<th>Partzilla</th>") < page.text.index("<th>MotoSport</th>") < page.text.index("<th>Chaparral</th>") < page.text.index("<th>Lowest Competitor</th>") < page.text.index("<th>Gap vs Lowest</th>") < page.text.index("<th>Our Price</th>") < page.text.index("<th>Calc Cost</th>") < page.text.index("<th>Margin %</th>") < page.text.index("<th>Suggested Price</th>") < page.text.index("<th>Updated Price</th>") < page.text.index("<th>New Margin</th>")
    assert "<th>Original Price</th>" not in page.text
    assert "Save Selected" in page.text
    assert "Needs Review" in page.text
    assert "Decision" not in page.text
    assert "data-toggle-visible-selection" in page.text
    assert "data-save-selected-prices" in page.text
    for removed_heading in ["<th>Reference</th>", "<th>Savings</th>", "<th>Units</th>", "<th>Margin at Partzilla</th>", "<th>Priority</th>"]:
        assert removed_heading not in page.text


def test_comparison_quick_filters_work_without_selected_import_file() -> None:
    db = _comparison_db("comparison_no_import_filter.db")
    client = TestClient(create_app(db), raise_server_exceptions=False)

    page = client.get("/comparison")

    assert page.status_code == 200
    assert "/comparison?import_batch_id=" not in page.text
    assert client.get("/comparison?import_batch_id=&page_size=50").status_code == 200
    assert client.get("/comparison?page_size=50").status_code == 200
    assert client.get("/comparison?price_position=above&page_size=50").status_code == 200


def test_product_catalog_order_and_highlighting_matches_comparison() -> None:
    db = _comparison_db("product_catalog_highlighting.db")
    catalog = TestClient(create_app(db), raise_server_exceptions=False).get("/products").text

    assert catalog.index("<th>Product</th>") < catalog.index("<th>Partzilla</th>") < catalog.index("<th>MotoSport</th>") < catalog.index("<th>Chaparral</th>") < catalog.index("<th>Lowest Competitor</th>") < catalog.index("<th>Gap vs Lowest</th>") < catalog.index("<th>Our Price</th>") < catalog.index("<th>Calc Cost</th>") < catalog.index("<th>Our Margin</th>")
    assert 'class="price-above-competitor"' in catalog
    assert 'class="price-difference-higher"' in catalog


def test_comparison_export_route_can_export_selected_rows_without_multipart() -> None:
    db = _comparison_db("comparison_web.db")
    client = TestClient(create_app(db), raise_server_exceptions=False)
    product_id = _product_id(db, "K-PRICE")

    response = client.post("/comparison/export", data={"scope": "selected", "selected": str(product_id)})

    assert response.status_code == 200
    assert response.headers["content-disposition"].endswith(".xlsx\"")


def test_uploaded_file_opens_filtered_price_comparison() -> None:
    db = _empty_db("uploaded_file_comparison.db")
    first = _upload_simple_batch(db, "older.xlsx", "SKU-FIRST", "Honda", "H-FIRST")
    second = _upload_simple_batch(db, "newer.xlsx", "SKU-SECOND", "Yamaha", "Y-SECOND")
    confirm_import(db, first.import_batch_id)
    confirm_import(db, second.import_batch_id)
    client = TestClient(create_app(db), raise_server_exceptions=False)

    imports = client.get("/imports")
    comparison = client.get(f"/comparison?import_batch_id={second.import_batch_id}")

    assert imports.status_code == 200
    assert imports.text.index("newer.xlsx") < imports.text.index("older.xlsx")
    assert f"/imports?import_batch_id={second.import_batch_id}" in imports.text
    assert comparison.status_code == 200
    assert "Y-SECOND" in comparison.text
    assert "H-FIRST" not in comparison.text
    assert "From Selected File" in comparison.text
    assert "Viewing uploaded file:" in comparison.text
    assert "newer.xlsx" in comparison.text


def test_comparison_page_saves_updated_price_decision() -> None:
    db = _comparison_db("comparison_save_decision.db")
    client = TestClient(create_app(db), raise_server_exceptions=False)
    product_id = _product_id(db, "K-PRICE")

    page = client.get("/comparison")
    assert page.status_code == 200
    assert "Suggested Price" in page.text
    assert "Updated Price" in page.text

    response = client.post(
        f"/comparison/{product_id}/review",
        data={
            "review_status": "Approved",
            "suggested_new_price": "11.49",
            "notes": "Approved directly from comparison.",
            "return_query": "page_size=50",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    row = next(item for item in comparison_rows(db) if item["oem_part_number"] == "K-PRICE")
    assert row["review_status"] == "Approved"
    assert row["suggested_new_price"] == "11.49"
    assert row["our_current_price"] == "12.34"
    assert row["saved_to_catalog"] is True
    assert row["notes"] == "Approved directly from comparison."


def test_comparison_row_save_defaults_to_reviewed() -> None:
    db = _comparison_db("comparison_save_defaults_reviewed.db")
    client = TestClient(create_app(db), raise_server_exceptions=False)
    product_id = _product_id(db, "K-PRICE")

    response = client.post(
        f"/comparison/{product_id}/review",
        data={"suggested_new_price": "11.49", "return_query": "page_size=50"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    row = next(item for item in comparison_rows(db) if item["oem_part_number"] == "K-PRICE")
    assert row["review_status"] == "Approved"
    assert row["saved_to_catalog"] is True
    page = client.get("/imports?page_size=50")
    assert "Pending review <b>1</b>" in page.text


def test_comparison_bulk_save_visible_updates_catalog_and_marks_saved() -> None:
    db = _comparison_db("comparison_bulk_save.db")
    client = TestClient(create_app(db), raise_server_exceptions=False)
    product_id = _product_id(db, "K-PRICE")

    response = client.post(
        "/comparison/bulk-save",
        json={"rows": [{"product_id": product_id, "suggested_new_price": "10.99", "review_status": "Approved"}]},
    )

    assert response.status_code == 200
    assert response.json()["saved"] == 1
    row = next(item for item in comparison_rows(db) if item["oem_part_number"] == "K-PRICE")
    assert row["our_current_price"] == "12.34"
    assert row["saved_to_catalog"] is True
    catalog = client.get("/products?search=K-PRICE").text
    assert "OK" in catalog
    assert "Needs Review" not in catalog


def test_comparison_undo_restores_catalog_price_and_reopens_review() -> None:
    db = _comparison_db("comparison_undo_save.db")
    client = TestClient(create_app(db), raise_server_exceptions=False)
    product_id = _product_id(db, "K-PRICE")

    client.post(
        "/comparison/bulk-save",
        json={"rows": [{"product_id": product_id, "suggested_new_price": "10.99", "review_status": "Approved"}]},
    )
    response = client.post(f"/comparison/{product_id}/undo", data={"return_query": "page_size=50"}, follow_redirects=False)

    assert response.status_code == 303
    row = next(item for item in comparison_rows(db) if item["oem_part_number"] == "K-PRICE")
    assert row["our_current_price"] == "12.34"
    assert row["saved_to_catalog"] is False


def test_comparison_bulk_save_all_matching_rows_crosses_pages() -> None:
    db = _comparison_db("comparison_bulk_save_all_matching.db")
    client = TestClient(create_app(db), raise_server_exceptions=False)
    eligible = [row["product_id"] for row in comparison_review_rows(db) if row.get("suggested_new_price")]

    response = client.post(
        "/comparison/bulk-save",
        json={"rows": [], "all_matching": True, "query": "?page_size=25"},
    )

    assert response.status_code == 200
    assert response.json()["saved"] == len(eligible)
    saved = {row["product_id"] for row in comparison_rows(db) if row["saved_to_catalog"]}
    assert saved == set(eligible)


def test_comparison_selected_export_can_use_all_matching_filter() -> None:
    db = _comparison_db("comparison_export_all_matching.db")
    client = TestClient(create_app(db), raise_server_exceptions=False)

    response = client.post(
        "/comparison/export",
        data={
            "scope": "selected",
            "selected": "",
            "all_matching": "1",
            "query": "?missing_competitor_price=1&page_size=25",
        },
    )

    assert response.status_code == 200
    export_path = TEST_OUTPUT_DIR / "comparison_export_all_matching.xlsx"
    export_path.write_bytes(response.content)
    exported = read_rows(export_path, "Pricing Review")
    assert [row[2] for row in exported[1:]] == ["H-MISSING"]


def test_sortable_table_headers_keep_sticky_position() -> None:
    css = Path("app/web/static/styles.css").read_text(encoding="utf-8")

    assert "thead th.sortable-heading { position: sticky; top: 0; }" in css


def test_review_queue_saves_decisions_and_updates_exports() -> None:
    db = _comparison_db("review_queue.db")
    client = TestClient(create_app(db), raise_server_exceptions=False)
    product_id = _product_id(db, "K-PRICE")
    missing_product_id = _product_id(db, "H-MISSING")

    pending = client.get("/reviews")
    assert pending.status_code == 200
    assert "2 Rows" in pending.text
    assert "Ready to export" in pending.text
    assert "Recommended Action" in pending.text
    assert "Current Margin" in pending.text
    assert 'class="sortable-table"' in pending.text
    assert 'data-sort="number">Our Price' in pending.text
    assert 'class="price-difference-higher"' in pending.text
    assert "$12.34" in pending.text
    assert "K-PRICE" in pending.text
    assert "Our Price Higher" in pending.text

    missing_bucket = client.get("/reviews?bucket=Missing%20Competitor%20Price")
    assert missing_bucket.status_code == 200
    assert "H-MISSING" in missing_bucket.text
    assert "K-PRICE" not in missing_bucket.text

    response = client.post(
        f"/reviews/{product_id}",
        data={
            "review_status": "Needs Price Change",
            "suggested_new_price": "11.49",
            "notes": "Lower to stay close to lowest competitor.",
            "return_status": "Pending Review",
            "return_bucket": "All",
            "page": "1",
            "page_size": "50",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303

    rows = comparison_rows(db)
    reviewed = next(row for row in rows if row["oem_part_number"] == "K-PRICE")
    assert reviewed["review_status"] == "Needs Price Change"
    assert reviewed["suggested_new_price"] == "11.49"
    assert reviewed["notes"] == "Lower to stay close to lowest competitor."
    with connect_database(db) as conn:
        saved_price = conn.execute(
            "SELECT our_current_price_cents FROM internal_product_state WHERE product_id=?",
            (product_id,),
        ).fetchone()[0]
    assert saved_price == 1149
    assert [row["oem_part_number"] for row in comparison_rows(db, ComparisonFilters(needs_review=True))] == ["H-MISSING"]

    page = client.get("/reviews?status=Needs%20Price%20Change")
    assert page.status_code == 200
    assert "K-PRICE" in page.text
    assert "Lower to stay close" in page.text

    bulk = client.post(
        "/reviews/bulk",
        content=f"selected={missing_product_id}&review_status=Ignored&notes=No%20competitor%20match&return_status=Pending%20Review&return_bucket=All&page_size=50",
        headers={"content-type": "application/x-www-form-urlencoded"},
        follow_redirects=False,
    )
    assert bulk.status_code == 303
    ignored = client.get("/reviews?status=Ignored")
    assert "H-MISSING" in ignored.text
    assert "No competitor match" in ignored.text

    export_path = export_review(rows, TEST_OUTPUT_DIR / "exports")
    exported = read_rows(export_path, "Pricing Review")
    headers = exported[0]
    exported_row = next(row for row in exported if row[2] == "K-PRICE")
    assert exported_row[headers.index("Updated_Price")] == "11.49"
    assert "Review_Status" not in headers
    assert "Notes" not in headers


def test_manufacturer_specific_pricing_rule_override_changes_suggestion() -> None:
    db = _comparison_db("pricing_rule_oem_override.db")
    client = TestClient(create_app(db), raise_server_exceptions=False)

    rules = client.get("/rules")
    assert rules.status_code == 200
    assert "OEM-Specific Overrides" in rules.text
    assert "Kawasaki" in rules.text

    updated = client.post(
        "/rules/manufacturers/Kawasaki",
        data={
            "is_enabled": "1",
            "adjustment_cents": "-50",
            "ending_cents": "49",
            "minimum_margin_pct": "20",
        },
        follow_redirects=False,
    )

    assert updated.status_code == 303
    review = client.get("/reviews")
    assert review.status_code == 200
    assert "Rules suggest $9.49" in review.text
    with connect_database(db) as conn:
        row = conn.execute(
            """
            SELECT is_enabled, adjustment_cents, ending_cents, minimum_margin_pct
            FROM pricing_rule_manufacturer_overrides
            WHERE manufacturer='Kawasaki'
            """
        ).fetchone()
    assert row["is_enabled"] == 1
    assert row["adjustment_cents"] == -50
    assert row["ending_cents"] == 49
    assert row["minimum_margin_pct"] == 20


def test_pricing_rules_page_and_review_queue_rule_selection() -> None:
    db = _comparison_db("pricing_rules.db")
    client = TestClient(create_app(db), raise_server_exceptions=False)
    product_id = _product_id(db, "K-PRICE")

    rules = client.get("/rules")
    assert rules.status_code == 200
    assert "Strategy Presets" in rules.text
    assert "Match Market" in rules.text
    assert "Use Lowest Competitor" in rules.text
    assert "Protect Minimum Margin" in rules.text

    preset = client.post("/rules/presets/aggressive", follow_redirects=False)
    assert preset.status_code == 303
    with connect_database(db) as conn:
        anchor_settings = json.loads(conn.execute("SELECT settings_json FROM pricing_rules WHERE rule_code='use_lowest_competitor'").fetchone()[0])
        margin_settings = json.loads(conn.execute("SELECT settings_json FROM pricing_rules WHERE rule_code='protect_minimum_margin'").fetchone()[0])
    assert anchor_settings["adjustment_cents"] == -50
    assert margin_settings["minimum_margin_pct"] == 20

    reset_preset = client.post("/rules/presets/match_market", follow_redirects=False)
    assert reset_preset.status_code == 303

    updated = client.post(
        "/rules/protect_minimum_margin",
        data={"is_enabled": "1", "setting_value": "25"},
        follow_redirects=False,
    )
    assert updated.status_code == 303

    review = client.get("/reviews")
    assert review.status_code == 200
    assert "Rules suggest $9.99" in review.text
    assert "Round To .99" in review.text

    save = client.post(
        f"/reviews/{product_id}",
        content=(
            "review_status=Needs%20Price%20Change"
            "&use_rule_suggestion=1"
            "&rule_code=skip_unsafe_competitor_data"
            "&rule_code=use_lowest_competitor"
            "&rule_code=protect_minimum_margin"
            "&notes=Saved%20without%20rounding"
            "&return_status=Pending%20Review"
            "&return_bucket=All"
            "&page=1"
            "&page_size=50"
        ),
        headers={"content-type": "application/x-www-form-urlencoded"},
        follow_redirects=False,
    )
    assert save.status_code == 303

    reviewed = next(row for row in comparison_rows(db) if row["oem_part_number"] == "K-PRICE")
    assert reviewed["suggested_new_price"] == "10.00"
    assert "use_lowest_competitor" in reviewed["applied_rule_codes_json"]
    assert "round_to_99" not in reviewed["applied_rule_codes_json"]

    override = client.post(
        f"/reviews/{product_id}",
        content=(
            "review_status=Needs%20Price%20Change"
            "&use_rule_suggestion=1"
            "&suggested_new_price=10.49"
            "&displayed_suggested_new_price=10"
            "&rule_code=skip_unsafe_competitor_data"
            "&rule_code=use_lowest_competitor"
            "&rule_code=protect_minimum_margin"
            "&notes=Manual%20override"
            "&return_status=Pending%20Review"
            "&return_bucket=All"
            "&page=1"
            "&page_size=50"
        ),
        headers={"content-type": "application/x-www-form-urlencoded"},
        follow_redirects=False,
    )
    assert override.status_code == 303

    overridden = next(row for row in comparison_rows(db) if row["oem_part_number"] == "K-PRICE")
    assert overridden["suggested_new_price"] == "10.49"
    assert overridden["notes"] == "Manual override"

    export_path = export_review(review_rows(db, status="Needs Price Change"), TEST_OUTPUT_DIR / "exports")
    exported = read_rows(export_path, "Pricing Review")
    headers = exported[0]
    exported_row = next(row for row in exported if row[2] == "K-PRICE")
    assert exported_row[headers.index("Updated_Price")] == "10.49"
    for removed_column in ["Applied_Rule_Names", "Rule_Skip_Unsafe_Competitor_Data", "Rule_Use_Lowest_Competitor", "Rule_Round_To_99", "Rule_Protect_Minimum_Margin"]:
        assert removed_column not in headers


def test_approved_review_price_updates_catalog_and_export() -> None:
    db = _comparison_db("approved_price_updates.db")
    client = TestClient(create_app(db), raise_server_exceptions=False)
    product_id = _product_id(db, "K-PRICE")

    response = client.post(
        f"/reviews/{product_id}",
        data={
            "review_status": "Approved",
            "suggested_new_price": "11.49",
            "notes": "Approved new price.",
            "return_status": "Pending Review",
            "return_bucket": "All",
            "page": "1",
            "page_size": "50",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    with connect_database(db) as conn:
        price_cents = conn.execute(
            "SELECT our_current_price_cents FROM internal_product_state WHERE product_id=?",
            (product_id,),
        ).fetchone()[0]
    assert price_cents == 1149

    catalog = client.get("/products?search=K-PRICE")
    assert catalog.status_code == 200
    assert "$11.49" in catalog.text

    export_path = export_review(review_rows(db, status="Approved"), TEST_OUTPUT_DIR / "exports")
    exported = read_rows(export_path, "Pricing Review")
    headers = exported[0]
    exported_row = next(row for row in exported if row[2] == "K-PRICE")
    assert exported_row[headers.index("Updated_Price")] == "11.49"
    assert "Review_Status" not in headers


def test_review_export_route_exports_filtered_view() -> None:
    db = _comparison_db("review_filtered_export.db")
    client = TestClient(create_app(db), raise_server_exceptions=False)

    response = client.get("/reviews/export?status=Pending%20Review&bucket=Missing%20Competitor%20Price")

    assert response.status_code == 200
    assert response.headers["content-disposition"].endswith(".xlsx\"")


def test_comparison_page_is_paginated() -> None:
    db = _empty_db("comparison_pagination.db")
    upload_path = TEST_OUTPUT_DIR / "comparison_pagination.xlsx"
    rows = [["Internal_SKU", "Manufacturer", "OEM_Part_Number", "Our_Current_Price"]]
    rows.extend([f"SKU-{index:03}", "Honda", f"H-PAGE-{index:03}", "10.00"] for index in range(55))
    write_workbook(upload_path, {"Upload Data": rows})
    result = save_upload(db, filename="comparison-pagination.xlsx", content=upload_path.read_bytes())
    confirm_import(db, result.import_batch_id)

    response = TestClient(create_app(db), raise_server_exceptions=False).get("/comparison?page_size=25&page=1")

    assert response.status_code == 200
    assert "55 Comparison Rows" in response.text
    assert "Missing competitor price" in response.text
    assert "Pending review" in response.text
    assert "Page 1 of 3" in response.text
    assert response.text.count('class="row-select"') == 25
    assert "Next" in response.text


def test_price_check_page_combines_upload_summary_and_start_action() -> None:
    db = _empty_db("price_check_page.db")
    upload_path = TEST_OUTPUT_DIR / "price_check_page.xlsx"
    write_workbook(
        upload_path,
        {
            "Upload Data": [
                ["Internal_SKU", "Manufacturer", "OEM_Part_Number", "Our_Current_Price"],
                ["SKU-ONE", "Honda", "18327-MEN-A30", "44.99"],
            ],
        },
    )
    client = TestClient(create_app(db), raise_server_exceptions=False)

    upload = client.post("/imports/upload?filename=price_check_page.xlsx", content=upload_path.read_bytes())
    assert upload.status_code == 200
    assert "File Read Summary" in upload.text
    assert "Valid parts" in upload.text
    assert "Start Checking Prices" in upload.text
    assert 'min="1"' in upload.text
    assert "Show row preview (first 25 rows)" in upload.text
    assert upload.text.index("Upload Parts File") < upload.text.index("Uploaded Files")
    assert upload.text.index("Start Checking Prices") < upload.text.index("Show row preview (first 25 rows)")
    assert "Show Missing Prices" not in upload.text


def test_price_check_page_preselects_active_local_competitors(monkeypatch) -> None:
    db = _empty_db("price_check_runnable_competitors.db")
    upload_path = TEST_OUTPUT_DIR / "price_check_runnable_competitors.xlsx"
    write_workbook(
        upload_path,
        {
            "Upload Data": [
                ["Internal_SKU", "Manufacturer", "OEM_Part_Number", "Our_Current_Price"],
                ["SKU-ONE", "Honda", "18327-MEN-A30", "44.99"],
            ],
        },
    )
    monkeypatch.setattr("app.web.app.auth_state_exists", lambda _competitor_key: False)
    client = TestClient(create_app(db), raise_server_exceptions=False)

    upload = client.post("/imports/upload?filename=price_check_runnable_competitors.xlsx", content=upload_path.read_bytes())

    assert upload.status_code == 200
    assert 'value="partzilla" checked' in upload.text
    assert "Needs Server Login" not in upload.text
    assert 'value="motosport" checked' in upload.text
    assert 'value="chaparral" checked' in upload.text


def test_web_price_check_queues_local_agent_and_reports_live_progress(monkeypatch, tmp_path) -> None:
    import app.collection_jobs as collection_jobs

    monkeypatch.setattr(collection_jobs, "JOB_DIR", tmp_path / "jobs")
    monkeypatch.setattr(collection_jobs, "LOCAL_AGENT_STATUS_FILE", tmp_path / "jobs" / "local_agent_status.json")
    db = _empty_db("local_agent_web_queue.db")
    result = _upload_simple_batch(db, "local-agent.xlsx", "SKU-AGENT", "Honda", "H-AGENT")
    client = TestClient(create_app(db), raise_server_exceptions=False)

    queued = client.post(
        f"/imports/{result.import_batch_id}/start-price-check",
        data={"competitor": ["partzilla", "motosport", "chaparral"], "delay_seconds": "1"},
        follow_redirects=False,
    )

    assert queued.status_code == 303
    job_id = queued.headers["location"].split("job_id=")[-1]
    claimed = client.post("/collector/agent/jobs/next?agent_id=test-desktop")
    assert claimed.status_code == 200
    assert claimed.json()["job_id"] == job_id
    assert claimed.json()["competitors"] == ["partzilla", "motosport", "chaparral"]

    progress = {
        "status": "running",
        "run_status": "running",
        "total": 1,
        "completed": 1,
        "remaining": 0,
        "eta_seconds": 0,
        "last_attempted_part": "H-AGENT",
        "rows": [{"run_order": 1, "manufacturer": "Honda", "oem_part_number": "H-AGENT", "result_type": "first_observation"}],
    }
    updated = client.post(
        f"/collector/agent/jobs/{job_id}/progress/partzilla?agent_id=test-desktop",
        json=progress,
    )
    assert updated.status_code == 200
    status = client.get(f"/collections/jobs/{job_id}/status").json()
    assert status["progress_by_competitor"]["partzilla"]["completed"] == 1
    assert status["progress"]["last_attempted_part"] == "H-AGENT"

    completed = client.post(
        f"/collector/agent/jobs/{job_id}/complete?agent_id=test-desktop",
        json={"status": "completed", "message": "Finished."},
    )
    assert completed.status_code == 200
    assert client.get(f"/collections/jobs/{job_id}/status").json()["status"] == "completed"


def test_price_check_start_validation_error_renders_combined_page() -> None:
    db = _empty_db("price_check_validation_error.db")
    result = _upload_simple_batch(db, "price-check-validation.xlsx", "SKU-ERR", "Honda", "H-ERR")
    client = TestClient(create_app(db), raise_server_exceptions=False)

    response = client.post(
        f"/imports/{result.import_batch_id}/start-price-check",
        data={"competitor": "partzilla", "delay_seconds": "0"},
    )

    assert response.status_code == 400
    assert "Delay must be at least 1 second." in response.text
    assert "Price Comparison" in response.text
    assert "price-check-validation.xlsx" in response.text


def test_price_check_start_requires_a_selected_competitor() -> None:
    db = _empty_db("price_check_requires_competitor.db")
    result = _upload_simple_batch(db, "price-check-no-competitor.xlsx", "SKU-NONE", "Honda", "H-NONE")
    client = TestClient(create_app(db), raise_server_exceptions=False)

    response = client.post(
        f"/imports/{result.import_batch_id}/start-price-check",
        data={"delay_seconds": "1"},
    )

    assert response.status_code == 400
    assert "Select at least one competitor to check." in response.text


def test_recent_files_can_be_cleared_without_deleting_imported_products() -> None:
    db = _empty_db("clear_recent_files.db")
    result = _upload_simple_batch(db, "clear-recent.xlsx", "SKU-CLEAR", "Honda", "18327-MEN-A30")
    confirm_import(db, result.import_batch_id)
    client = TestClient(create_app(db), raise_server_exceptions=False)

    assert "clear-recent.xlsx" in client.get("/imports").text
    response = client.post("/imports/history/clear", follow_redirects=False)

    assert response.status_code == 303
    page = client.get("/imports")
    assert "clear-recent.xlsx" not in page.text
    with connect_database(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM products").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM import_batches").fetchone()[0] == 0
        assert conn.execute("SELECT source_import_batch_id FROM internal_product_state").fetchone()[0] is None


def test_individual_uploaded_file_delete_removes_its_current_comparison_rows() -> None:
    db = _empty_db("delete_single_upload.db")
    first = _upload_simple_batch(db, "delete-first.xlsx", "SKU-FIRST", "Honda", "H-DELETE-1")
    second = _upload_simple_batch(db, "delete-second.xlsx", "SKU-SECOND", "Yamaha", "Y-KEEP-1")
    confirm_import(db, first.import_batch_id)
    confirm_import(db, second.import_batch_id)
    client = TestClient(create_app(db), raise_server_exceptions=False)

    response = client.post(f"/imports/{first.import_batch_id}/delete", follow_redirects=False)

    assert response.status_code == 303
    page = client.get("/imports")
    assert "delete-first.xlsx" not in page.text
    assert "delete-second.xlsx" in page.text
    with connect_database(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM products WHERE oem_part_number='H-DELETE-1'").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM products WHERE oem_part_number='Y-KEEP-1'").fetchone()[0] == 1


def test_collection_plan_uses_current_import_batch() -> None:
    db = _empty_db("batch_collection_plan.db")
    first = _upload_simple_batch(db, "first.xlsx", "SKU-FIRST", "Honda", "18327-MEN-A30")
    second = _upload_simple_batch(db, "second.xlsx", "SKU-SECOND", "Yamaha", "1MC-2835V-00-P4")
    confirm_import(db, first.import_batch_id)
    confirm_import(db, second.import_batch_id)

    parts = plan_import_collection(db, import_batch_id=second.import_batch_id)

    assert [(part.manufacturer, part.oem_part_number) for part in parts] == [("Yamaha", "1MC-2835V-00-P4")]


def test_local_collector_download_confirms_valid_import() -> None:
    db = _empty_db("collector_download_confirms.db")
    upload = _upload_simple_batch(db, "collector.xlsx", "SKU-L", "Honda", "LOCAL-1")

    response = TestClient(create_app(db)).get(f"/collector/imports/{upload.import_batch_id}/input.csv")

    assert response.status_code == 200
    assert "LOCAL-1" in response.text
    with connect_database(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM internal_product_state").fetchone()[0] == 1


def test_import_collection_plan_can_include_more_than_test_collection_limit() -> None:
    db = _empty_db("large_import_collection_plan.db")
    upload_path = TEST_OUTPUT_DIR / "large_import_collection_plan.xlsx"
    rows = [["Internal_SKU", "Manufacturer", "OEM_Part_Number", "Our_Current_Price"]]
    rows.extend([f"SKU-{index:03}", "Honda", f"H-LARGE-{index:03}", "10.00"] for index in range(55))
    write_workbook(upload_path, {"Upload Data": rows})

    result = save_upload(db, filename="large-import.xlsx", content=upload_path.read_bytes())
    confirm_import(db, result.import_batch_id)
    parts = plan_import_collection(db, import_batch_id=result.import_batch_id)

    assert len(parts) == 55


def test_collection_validation_and_launcher_use_existing_collector_safely(monkeypatch) -> None:
    db = _comparison_db("collection_launcher.db")
    monkeypatch.setattr("app.collection_jobs.auth_state_exists", lambda _competitor_key: True)
    monkeypatch.setattr("app.collection_jobs.active_job_exists", lambda: False)
    monkeypatch.setattr("app.collection_jobs.utc_now", lambda: "2026-07-09T00:00:00Z")

    parts = [PlannedCollectionPart("Kawasaki", "K-PRICE", "12.34", "10.00", None)]
    assert validate_collection_request(db, parts, confirmation="run", delay_seconds=5) == ["Type RUN to confirm the test collection."]
    assert validate_collection_request(db, parts, confirmation="RUN", delay_seconds=0) == ["Delay must be at least 1 second."]
    unsupported = [PlannedCollectionPart("Unknown OEM", "X;Remove-Item *", "1.00", "", None)]
    assert validate_collection_request(db, unsupported, confirmation="RUN", delay_seconds=5) == []

    job_id = start_collection_job(db, parts, delay_seconds=5)

    assert job_id == "20260709T000000Z"
    generated_csv = next(Path("data/output/ui_collection_jobs/20260709T000000Z").glob("selected_parts.csv"))
    load_result = load_parts_csv(generated_csv)
    assert load_result.records[0].manufacturer == "Kawasaki"
    assert load_result.records[0].oem_part_number == "K-PRICE"
    job = Path("data/output/ui_collection_jobs/20260709T000000Z/job.json").read_text(encoding="utf-8")
    assert '"status": "prepared"' in job
    assert "collect_parts.py" in job
    assert "--collection-mode full_browser" in job


def test_dashboard_price_check_starts_headless_lightweight_browser(monkeypatch) -> None:
    db = _comparison_db("headless_lightweight_price_check.db")
    monkeypatch.setattr("app.collection_jobs.utc_now", lambda: "2026-07-09T00:10:00Z")
    captured: dict[str, object] = {}

    class FakeThread:
        def __init__(self, *, target, kwargs, daemon):
            captured["target"] = target
            captured["kwargs"] = kwargs
            captured["daemon"] = daemon

        def start(self) -> None:
            captured["started"] = True

    monkeypatch.setattr("app.collection_jobs.threading.Thread", FakeThread)
    parts = [PlannedCollectionPart("Kawasaki", "K-PRICE", "12.34", "10.00", None)]

    job_id = start_price_collection_job(db, parts, delay_seconds=1)
    job = job_status(job_id)

    assert job["mode"] == "headless"
    assert job["collection_mode"] == "lightweight_browser"
    assert "--collection-mode lightweight_browser" in job["manual_command"]
    assert captured["started"] is True
    assert captured["kwargs"]["headless"] is True
    assert captured["kwargs"]["collection_mode"] == "lightweight_browser"


def test_parallel_competitor_job_status_aggregates_progress(monkeypatch) -> None:
    db = _comparison_db("parallel_progress.db")
    monkeypatch.setattr("app.collection_jobs.utc_now", lambda: "2026-07-09T00:20:00Z")
    monkeypatch.setattr("app.collection_jobs.active_job_exists", lambda: False)

    class FakeThread:
        def __init__(self, *, target, kwargs, daemon):
            self.kwargs = kwargs

        def start(self) -> None:
            job_path = self.kwargs["job_path"]
            (job_path / "progress_partzilla.json").write_text(
                json.dumps({"status": "running", "competitor": "partzilla", "total": 2, "completed": 1, "remaining": 1, "eta_seconds": 5, "rows": [{"run_order": 1, "oem_part_number": "K-PRICE"}]}),
                encoding="utf-8",
            )
            (job_path / "progress_motosport.json").write_text(
                json.dumps({"status": "running", "competitor": "motosport", "total": 2, "completed": 2, "remaining": 0, "eta_seconds": 0, "rows": [{"run_order": 1, "oem_part_number": "K-PRICE"}]}),
                encoding="utf-8",
            )

    monkeypatch.setattr("app.collection_jobs.threading.Thread", FakeThread)
    parts = [PlannedCollectionPart("Kawasaki", "K-PRICE", "12.34", "10.00", None), PlannedCollectionPart("Honda", "H-MISSING", "20.00", "", None)]

    job_id = start_price_collection_job(db, parts, delay_seconds=1, competitor_keys=["partzilla", "motosport"])
    job = job_status(job_id)

    assert job["parallel_competitors"] is True
    assert set(job["progress_by_competitor"]) == {"partzilla", "motosport"}
    assert job["progress"]["competitor"] == "multiple"
    assert job["progress"]["total"] == 4
    assert job["progress"]["completed"] == 3
    assert job["progress"]["remaining"] == 1
    assert all("competitor" in row for row in job["progress"]["rows"])


def test_parallel_status_keeps_polling_when_one_competitor_failed_and_another_runs(monkeypatch) -> None:
    db = _comparison_db("parallel_progress_mixed.db")
    monkeypatch.setattr("app.collection_jobs.utc_now", lambda: "2026-07-09T00:20:00Z")
    monkeypatch.setattr("app.collection_jobs.active_job_exists", lambda: False)

    class FakeThread:
        def __init__(self, *, target, kwargs, daemon):
            self.kwargs = kwargs

        def start(self) -> None:
            job_path = self.kwargs["job_path"]
            (job_path / "progress_partzilla.json").write_text(
                json.dumps({"status": "failed", "run_status": "failed", "competitor": "partzilla", "total": 2, "completed": 1, "remaining": 1}),
                encoding="utf-8",
            )
            (job_path / "progress_motosport.json").write_text(
                json.dumps({"status": "running", "run_status": "running", "competitor": "motosport", "total": 2, "completed": 1, "remaining": 1}),
                encoding="utf-8",
            )

    monkeypatch.setattr("app.collection_jobs.threading.Thread", FakeThread)
    parts = [PlannedCollectionPart("Kawasaki", "K-PRICE", "12.34", "10.00", None), PlannedCollectionPart("Honda", "H-MISSING", "20.00", "", None)]

    job_id = start_price_collection_job(db, parts, delay_seconds=1, competitor_keys=["partzilla", "motosport"])
    job = job_status(job_id)

    assert job["progress"]["status"] == "running"
    assert job["progress"]["completed"] == 2


def test_visible_retry_requires_a_display(monkeypatch) -> None:
    import app.collection_jobs as collection_jobs

    monkeypatch.setattr(collection_jobs.os, "name", "posix")
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)

    assert collection_jobs._can_launch_visible_browser() is False


def test_branding_theme_and_navigation_are_present() -> None:
    css = Path("app/web/static/styles.css").read_text(encoding="utf-8")
    base = Path("app/web/templates/base.html").read_text(encoding="utf-8")

    assert "--rm-red: #D71920" in css
    assert "--rm-black: #111111" in css
    for label in [
        "Dashboard",
        "Product Catalog",
        "Price Check",
        "Scan Runs",
        "Data Quality",
        "Pricing Rules",
        "Settings",
    ]:
        assert label in base
    assert "Review Queue" not in base


def test_price_check_keeps_collector_and_live_status_above_large_tables() -> None:
    imports = Path("app/web/templates/imports.html").read_text(encoding="utf-8")
    status = Path("app/web/templates/collector_status.html").read_text(encoding="utf-8")

    assert imports.index('{% include "collector_status.html" %}') < imports.index("{% if history %}")
    assert imports.index('{% include "collector_status.html" %}') < imports.index('{% include "comparison_section.html" %}')
    assert "Desktop collector connected" in status
    assert "Live Price Check" in status


def _comparison_db(name: str = "comparison.db") -> Path:
    db = _empty_db(name)
    upload_path = TEST_OUTPUT_DIR / f"{name}.xlsx"
    write_workbook(
        upload_path,
        {
            "Upload Data": [
                ["Internal_SKU", "Manufacturer", "OEM_Part_Number", "Our_Current_Price", "Product_Name", "Current_Cost", "Units_Sold_12M", "Inventory_Qty", "Scan_Priority"],
                ["SKU-K", "Kawasaki", "K-PRICE", "12.34", "Kawasaki Price Part", "7.00", "9", "2", "high"],
                ["SKU-H", "Honda", "H-MISSING", "20.00", "Honda Missing Price", "15.00", "1", "0", "low"],
            ],
        },
    )
    result = save_upload(db, filename=f"{name}.xlsx", content=upload_path.read_bytes())
    confirm_import(db, result.import_batch_id)
    _set_partzilla_state(db, "K-PRICE", selling=1000, reference=1500, savings=33, display_type="discounted")
    return db


def _upload_simple_batch(db: Path, filename: str, sku: str, manufacturer: str, part: str):
    upload_path = TEST_OUTPUT_DIR / filename
    write_workbook(
        upload_path,
        {
            "Upload Data": [
                ["Internal_SKU", "Manufacturer", "OEM_Part_Number", "Our_Current_Price"],
                [sku, manufacturer, part, "44.99"],
            ],
        },
    )
    return save_upload(db, filename=filename, content=upload_path.read_bytes())


def _set_partzilla_state(db: Path, part_number: str, *, selling: int, reference: int, savings: int, display_type: str) -> None:
    with connect_database(db) as conn:
        listing_id = conn.execute(
            """
            SELECT l.listing_id FROM competitor_listings l
            JOIN products p ON p.product_id=l.product_id
            WHERE p.oem_part_number=?
            """,
            (part_number,),
        ).fetchone()["listing_id"]
        now = utc_now()
        conn.execute(
            """
            INSERT INTO current_listing_state(listing_id, selling_price_cents, reference_price_cents, savings_percent,
                price_display_type, selling_price_confidence, reference_price_confidence, currency_code,
                availability_raw, availability_status, product_name, observed_part_number, supersession_detected,
                price_parse_confidence, first_observed_at, last_successful_check_at, last_changed_at,
                consecutive_failure_count, updated_at)
            VALUES (?, ?, ?, ?, ?, 'high', 'high', 'USD', 'In Stock', 'in_stock', 'Partzilla Part', ?, 0,
                'high', ?, ?, ?, 0, ?)
            """,
            (listing_id, selling, reference, savings, display_type, part_number, now, now, now, now),
        )


def _empty_db(name: str) -> Path:
    TEST_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    db = TEST_OUTPUT_DIR / name
    if db.exists():
        db.unlink()
    initialize_database(db)
    return db


def _count(db: Path, table: str) -> int:
    with connect_database(db) as conn:
        return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def _product_id(db: Path, part_number: str) -> int:
    with connect_database(db) as conn:
        return int(conn.execute("SELECT product_id FROM products WHERE oem_part_number=?", (part_number,)).fetchone()[0])


def _rewrite_workbook_relationship_target(path: Path, old: str, new: str) -> None:
    temp_path = path.with_suffix(".tmp.xlsx")
    with zipfile.ZipFile(path, "r") as source, zipfile.ZipFile(temp_path, "w", compression=zipfile.ZIP_DEFLATED) as target:
        for item in source.infolist():
            content = source.read(item.filename)
            if item.filename == "xl/_rels/workbook.xml.rels":
                content = content.decode("utf-8").replace(old, new).encode("utf-8")
            target.writestr(item, content)
    temp_path.replace(path)
