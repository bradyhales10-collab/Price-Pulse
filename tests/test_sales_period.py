"""Sensitivity compares demand against fixed thresholds, so the period behind a
quantity changes the pricing treatment. The import field is named
units_sold_12m and matches a column called simply "Qty Sold", with nothing
checking it really covers twelve months.
"""

from __future__ import annotations

from decimal import Decimal

from app.sales_period import (
    DEFAULT_SALES_PERIOD,
    annualize_quantity,
    annualize_sales,
    detect_period_from_header,
)


def test_the_same_number_means_different_demand_on_different_periods() -> None:
    """400 units over three months is four times the demand of 400 over a year,
    and must not be scored as though they were the same."""
    annual = annualize_quantity(400, "12_months")
    half = annualize_quantity(400, "6_months")
    quarter = annualize_quantity(400, "3_months")

    assert annual.annualized_qty == 400
    assert half.annualized_qty == 800
    assert quarter.annualized_qty == 1600


def test_a_twelve_month_figure_is_left_exactly_as_supplied() -> None:
    result = annualize_quantity(8073, "12_months")

    assert result.annualized_qty == 8073
    assert result.was_scaled is False


def test_a_longer_period_is_scaled_down() -> None:
    result = annualize_quantity(2400, "24_months")

    assert result.annualized_qty == 1200
    assert result.was_scaled is True


def test_scaling_is_recorded_because_it_is_an_estimate() -> None:
    """Projecting a year from three months is an estimate, not a measurement,
    and a recommendation built on it should be readable as such."""
    result = annualize_quantity(400, "3_months")

    assert result.was_scaled is True
    assert "scaled up" in result.note
    assert "3 months" in result.note


def test_a_header_that_states_its_period_is_detected() -> None:
    assert detect_period_from_header("Units Sold 12M") == "12_months"
    assert detect_period_from_header("6 Month Sales") == "6_months"
    assert detect_period_from_header("Qty Sold (3 months)") == "3_months"
    assert detect_period_from_header("Annual Units") == "12_months"
    assert detect_period_from_header("Monthly Qty") == "1_month"


def test_twelve_month_beats_one_month_in_a_heading() -> None:
    """"12 month" contains "1 month" as a substring, so order matters."""
    assert detect_period_from_header("12 Month Qty") == "12_months"


def test_an_ambiguous_heading_is_not_guessed() -> None:
    """"Qty Sold" is exactly what the real Polaris upload uses, and it says
    nothing about the period. YTD depends on when the file was produced. A
    wrong guess is worse than asking, so these return nothing."""
    assert detect_period_from_header("Qty Sold") is None
    assert detect_period_from_header("YTD Qty") is None
    assert detect_period_from_header("") is None
    assert detect_period_from_header(None) is None


def test_a_missing_quantity_does_not_fail() -> None:
    result = annualize_quantity(None, "6_months")

    assert result.annualized_qty is None
    assert result.was_scaled is False


def test_sales_amounts_scale_the_same_way() -> None:
    assert annualize_sales(Decimal("10000"), "6_months") == Decimal("20000.00")
    assert annualize_sales(Decimal("10000"), "12_months") == Decimal("10000")
    assert annualize_sales(None, "6_months") is None


def test_the_default_is_annual_which_matches_the_field_name() -> None:
    assert DEFAULT_SALES_PERIOD == "12_months"
    assert annualize_quantity(500).annualized_qty == 500


def _upload_and_import(tmp_path, period: str, headers: list[str], row: list[str]):
    """Drive a real upload through the web interface with a chosen period."""
    from fastapi.testclient import TestClient

    from app.database import connect_database, initialize_database
    from app.imports import auto_map_headers
    from app.web.app import create_app
    from app.xlsx_utils import write_workbook

    database = tmp_path / "p.db"
    initialize_database(database)
    client = TestClient(create_app(database), raise_server_exceptions=False)

    workbook = tmp_path / "parts.xlsx"
    write_workbook(workbook, {"Sheet1": [headers, row]})
    client.post(
        "/imports/upload",
        content=workbook.read_bytes(),
        headers={"x-filename": "parts.xlsx"},
        follow_redirects=False,
    )
    with connect_database(database) as conn:
        batch_id = conn.execute(
            "SELECT import_batch_id FROM import_batches ORDER BY import_batch_id DESC LIMIT 1"
        ).fetchone()[0]

    form = {"worksheet": "Sheet1", "sales_period": period}
    for header, field in auto_map_headers(headers).items():
        form[f"map_{header}"] = field
    client.post(f"/imports/{batch_id}/preview", data=form)
    client.post(f"/imports/{batch_id}/start-price-check", data={"sales_period": period, "delay_seconds": "1"})

    with connect_database(database) as conn:
        stored = conn.execute("SELECT units_sold_12m, sales_period FROM internal_product_state").fetchone()
    return client, batch_id, dict(stored) if stored else {}


HEADERS = ["OEM", "Prod No", "Stock Name", "MF ID", "Qty Sold", "Calc Cost", "Price"]
ROW = ["POLARIS", "SKU1", "DRIVE BELT", "3211186", "400", "107.19", "163.99"]


def test_the_chosen_period_is_stored_with_the_parts(tmp_path) -> None:
    """Without this the quantity is meaningless: the same 400 could be a year
    or a quarter, and the scoring would differ by four times."""
    _, _, stored = _upload_and_import(tmp_path, "6_months", HEADERS, ROW)

    assert stored["units_sold_12m"] == 400
    assert stored["sales_period"] == "6_months"

    result = annualize_quantity(stored["units_sold_12m"], stored["sales_period"])
    assert result.annualized_qty == 800


def test_choosing_annual_leaves_the_quantity_untouched(tmp_path) -> None:
    _, _, stored = _upload_and_import(tmp_path, "12_months", HEADERS, ROW)

    result = annualize_quantity(stored["units_sold_12m"], stored["sales_period"])
    assert result.annualized_qty == 400
    assert result.was_scaled is False


def test_the_uploaded_figure_itself_is_never_altered(tmp_path) -> None:
    """Only the scoring is adjusted. Someone reading their own data back should
    see exactly what they uploaded."""
    _, _, stored = _upload_and_import(tmp_path, "3_months", HEADERS, ROW)

    assert stored["units_sold_12m"] == 400


def test_the_real_qty_sold_column_is_imported_at_all(tmp_path) -> None:
    """"Qty Sold" is what the real inventory export uses and was missing from
    the aliases, so the sales quantity was silently never imported and every
    part scored as though it had no sales history."""
    from app.imports import auto_map_headers

    assert auto_map_headers(HEADERS).get("Qty Sold") == "units_sold_12m"

    _, _, stored = _upload_and_import(tmp_path, "12_months", HEADERS, ROW)
    assert stored["units_sold_12m"] == 400


def test_the_mapping_screen_offers_the_choice_and_explains_it(tmp_path) -> None:
    from fastapi.testclient import TestClient

    from app.database import connect_database, initialize_database
    from app.web.app import create_app
    from app.xlsx_utils import write_workbook

    database = tmp_path / "p.db"
    initialize_database(database)
    client = TestClient(create_app(database), raise_server_exceptions=False)
    workbook = tmp_path / "parts.xlsx"
    write_workbook(workbook, {"Sheet1": [HEADERS, ROW]})
    client.post(
        "/imports/upload", content=workbook.read_bytes(), headers={"x-filename": "parts.xlsx"}, follow_redirects=False
    )
    with connect_database(database) as conn:
        batch_id = conn.execute(
            "SELECT import_batch_id FROM import_batches ORDER BY import_batch_id DESC LIMIT 1"
        ).fetchone()[0]

    page = client.get(f"/imports/{batch_id}/map").text

    assert 'name="sales_period"' in page
    # It must say why the choice matters, or it will be clicked past.
    assert "four times the demand" in page
    # "Qty Sold" states no period, so it must say it is assuming annual.
    assert "does not say" in page


def test_saving_parts_without_a_price_check_keeps_competitor_prices(tmp_path) -> None:
    """Re-importing to correct part details must not throw away competitor
    prices that took hours to collect, and must not need them re-collected.
    Until now the only way forward from the preview screen was Start Price
    Check, so updating a sales quantity meant re-checking every competitor."""
    from fastapi.testclient import TestClient

    from app.database import connect_database, initialize_database, utc_now
    from app.imports import auto_map_headers
    from app.web.app import create_app
    from app.xlsx_utils import write_workbook

    database = tmp_path / "p.db"
    initialize_database(database)
    client = TestClient(create_app(database), raise_server_exceptions=False)

    def upload(name: str, row: list[str]) -> int:
        workbook = tmp_path / name
        write_workbook(workbook, {"Sheet1": [HEADERS, row]})
        client.post(
            "/imports/upload",
            content=workbook.read_bytes(),
            headers={"x-filename": name},
            follow_redirects=False,
        )
        with connect_database(database) as conn:
            return conn.execute(
                "SELECT import_batch_id FROM import_batches ORDER BY import_batch_id DESC LIMIT 1"
            ).fetchone()[0]

    form = {"worksheet": "Sheet1", "sales_period": "12_months"}
    for header, field in auto_map_headers(HEADERS).items():
        form[f"map_{header}"] = field

    first = upload("a.xlsx", ["POLARIS", "SKU1", "DRIVE BELT", "3211186", "8073", "107.19", "163.99"])
    client.post(f"/imports/{first}/preview", data=form)
    client.post(f"/imports/{first}/confirm", data=form)

    now = utc_now()
    with connect_database(database) as conn:
        for offset, listing in enumerate(conn.execute("SELECT listing_id FROM competitor_listings").fetchall()):
            conn.execute(
                "INSERT OR REPLACE INTO current_listing_state(listing_id, selling_price_cents, "
                "price_display_type, first_observed_at, last_successful_check_at, last_changed_at, updated_at) "
                "VALUES (?,?, 'regular', ?,?,?,?)",
                (listing["listing_id"], 18999 + offset, now, now, now, now),
            )
        before = conn.execute(
            "SELECT COUNT(*) FROM current_listing_state WHERE selling_price_cents IS NOT NULL"
        ).fetchone()[0]
    assert before > 0

    second = upload("b.xlsx", ["POLARIS", "SKU1", "DRIVE BELT", "3211186", "400", "107.19", "171.99"])
    updated = dict(form, sales_period="6_months")
    client.post(f"/imports/{second}/preview", data=updated)
    response = client.post(f"/imports/{second}/confirm", data=updated)

    assert response.status_code == 200
    with connect_database(database) as conn:
        after = conn.execute(
            "SELECT COUNT(*) FROM current_listing_state WHERE selling_price_cents IS NOT NULL"
        ).fetchone()[0]
        state = conn.execute(
            "SELECT units_sold_12m, sales_period, our_current_price_cents FROM internal_product_state"
        ).fetchone()
        products = conn.execute("SELECT COUNT(*) FROM products").fetchone()[0]

    assert after == before, "competitor prices must survive a re-import"
    assert state["units_sold_12m"] == 400
    assert state["sales_period"] == "6_months"
    assert state["our_current_price_cents"] == 17199
    assert products == 1, "re-importing must update the part rather than duplicate it"


def test_the_preview_screen_offers_saving_without_a_price_check(tmp_path) -> None:
    from fastapi.testclient import TestClient

    from app.database import connect_database, initialize_database
    from app.imports import auto_map_headers
    from app.web.app import create_app
    from app.xlsx_utils import write_workbook

    database = tmp_path / "p.db"
    initialize_database(database)
    client = TestClient(create_app(database), raise_server_exceptions=False)
    workbook = tmp_path / "parts.xlsx"
    write_workbook(workbook, {"Sheet1": [HEADERS, ROW]})
    client.post(
        "/imports/upload", content=workbook.read_bytes(), headers={"x-filename": "parts.xlsx"}, follow_redirects=False
    )
    with connect_database(database) as conn:
        batch_id = conn.execute(
            "SELECT import_batch_id FROM import_batches ORDER BY import_batch_id DESC LIMIT 1"
        ).fetchone()[0]
    form = {"worksheet": "Sheet1", "sales_period": "12_months"}
    for header, field in auto_map_headers(HEADERS).items():
        form[f"map_{header}"] = field
    client.post(f"/imports/{batch_id}/preview", data=form)

    page = client.get(f"/imports?import_batch_id={batch_id}").text

    assert "Save Parts Without Checking Prices" in page
    assert "keeps the competitor prices already collected" in page
