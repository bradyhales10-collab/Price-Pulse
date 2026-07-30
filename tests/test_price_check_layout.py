from __future__ import annotations

import re
import sys
from pathlib import Path

from fastapi.testclient import TestClient

from app.comparison import ComparisonFilters, comparison_rows
from app.web.app import create_app
from app.web.queries import short_competitor_name

sys.path.insert(0, str(Path(__file__).parent))

from test_import_comparison_collection import _comparison_db, _product_id  # noqa: E402


def _client(db: Path) -> TestClient:
    return TestClient(create_app(db), raise_server_exceptions=False)


def _row_count(text: str) -> int | None:
    match = re.search(r"<h2>(\d+) Comparison Rows", text)
    return int(match.group(1)) if match else None


def test_two_hundred_rows_per_page_is_offered_and_accepted() -> None:
    client = _client(_comparison_db("rows_200.db"))

    assert '<option value="200"' in client.get("/imports").text

    # 200 must pass the allowlist rather than silently falling back to 50.
    page = client.get("/imports?page_size=200").text
    selected = re.search(r'<option value="(\d+)"\s+selected>\d+ rows</option>', page)
    assert selected is not None
    assert selected.group(1) == "200"


def test_manufacturer_filter_narrows_the_rows() -> None:
    client = _client(_comparison_db("filter_manufacturer.db"))

    everything = _row_count(client.get("/imports").text)
    kawasaki = _row_count(client.get("/imports?manufacturer=Kawasaki").text)

    assert everything is not None and kawasaki is not None
    assert 0 < kawasaki < everything


def test_review_state_filter_splits_reviewed_from_pending() -> None:
    db = _comparison_db("filter_review_state.db")
    everything = len(comparison_rows(db))
    pending = len(comparison_rows(db, ComparisonFilters(review_state="pending")))
    reviewed = len(comparison_rows(db, ComparisonFilters(review_state="reviewed")))

    assert pending + reviewed == everything


def test_a_filter_with_no_matches_still_shows_the_filter_controls() -> None:
    """Otherwise the screen goes blank and there is no way to undo the filter."""
    client = _client(_comparison_db("filter_empty.db"))
    page = client.get("/imports?manufacturer=NoSuchBrand").text

    assert _row_count(page) == 0
    assert 'name="manufacturer"' in page
    assert 'name="review_state"' in page


def test_oem_part_links_to_the_product_page_and_remembers_where_you_were() -> None:
    client = _client(_comparison_db("oem_link.db"))
    page = client.get("/imports?page_size=50&manufacturer=Kawasaki").text

    match = re.search(r'<td class="col-oem"><a href="(/products/\d+\?back=[^"]+)"', page)
    assert match, "OEM part number should link to the product page"
    href = match.group(1)
    assert "page_size" in href and "manufacturer" in href


def test_product_page_shows_a_back_link_only_for_internal_paths() -> None:
    db = _comparison_db("back_link.db")
    client = _client(db)
    product_id = _product_id(db, "K-PRICE")

    good = client.get(f"/products/{product_id}?back=%2Fimports%3Fpage_size%3D50").text
    assert "Back to Price Check" in good
    assert 'href="/imports?page_size=50"' in good

    # An off-site value must never become a link.
    bad = client.get(f"/products/{product_id}?back=https%3A%2F%2Fevil.com").text
    assert "evil.com" not in bad

    protocol_relative = client.get(f"/products/{product_id}?back=%2F%2Fevil.com").text
    assert "evil.com" not in protocol_relative


def test_wide_tables_scroll_with_the_page_instead_of_an_inner_window() -> None:
    client = _client(_comparison_db("single_scroll.db"))

    assert "table-wrap page-scroll" in client.get("/imports").text
    assert "table-wrap page-scroll" in client.get("/products").text


def test_long_competitor_names_are_shortened_for_table_cells() -> None:
    assert short_competitor_name("Chaparral Motorsports") == "Chaparral"
    assert short_competitor_name("Partzilla") == "Partzilla"
    assert short_competitor_name(None) == ""
    assert short_competitor_name("") == ""


def test_catalog_uses_the_short_competitor_name() -> None:
    client = _client(_comparison_db("catalog_short_name.db"))

    assert "Chaparral Motorsports" not in client.get("/products").text


def test_rules_page_keeps_the_rule_list_and_overrides_without_presets() -> None:
    page = _client(_comparison_db("rules_no_presets.db")).get("/rules").text

    assert "Strategy Presets" not in page
    assert "Active Pricing Logic" in page
    assert "oem-rule-" in page
