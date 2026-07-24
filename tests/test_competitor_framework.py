from __future__ import annotations

import argparse
import csv
import json
import uuid
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

import collect_parts
import export_cart_hidden_probe_input
import inspect_cart_probe_outputs
import probe_cart_price
import probe_competitor
from app.competitors.base import CompetitorObservation
from app.competitors.chaparral import ChaparralAdapter, build_search_url, normalize_availability, normalize_part_number_for_match, select_exact_match
from app.competitors.motosport import MotoSportAdapter
from app.competitors.registry import get_competitor, select_competitors
from app.database import connect_database, create_scan_run, initialize_database, money_to_cents, seed_competitor, seed_partzilla, utc_now
from app.internal_sources.api_source import ApiInternalProductSource, ApiSourceConfig
from app.internal_sources.base import InternalProductSource
from app.internal_sources.csv_source import CsvInternalProductSource
from app.internal_sources.excel_source import ExcelInternalProductSource
from app.models import PartRecord
from app.web.app import create_app
from app.xlsx_utils import write_workbook
from tests.test_import_comparison_collection import TEST_OUTPUT_DIR, _empty_db, _upload_simple_batch
from app.imports import confirm_import, save_upload
from app.input_loader import load_parts_csv
from app.manufacturer_registry import manufacturer_support_metadata
from fastapi.testclient import TestClient


REGULAR_HTML = """
<main>
  <h1>plate (13270-1800)</h1>
  <div class="price">$1.40</div>
  <div>In Stock</div>
</main>
"""

DISCOUNTED_HTML = """
<main>
  <h1>disc,pressure (41080-1483)</h1>
  <div class="price">$16.51 <span class="was">$17.95</span></div>
  <div>8% off - Save $1.44</div>
  <div>Expected to Ship in 4-9 Days</div>
</main>
"""

GLOBAL_PROMO_HTML = """
Skip to content
Summer Deals
Tires from $79
Up to 60% off
plate (13270-1800)
$1.40
In Stock
Related Products
$99.99
"""

NOT_FOUND_HTML = """
Skip to content
Page Not Found
Tires from $79
Up to 60% off
"""

SAVED_13270_1800_REGION = """
PLATE (13270-1800)
$1.59
or 4 payments of $0.40 with  for orders over $35
Quantity
Expected to Ship in 4-9 Days
Report an Error
This part fits these bikes:
KAWASAKI KLX110
Year: 2019 Part: PLATE Part Group: ENGINE COVER(S)
"""

SAVED_41080_1483_REGION = """
DISC,PRESSURE (41080-1483)
$17.61 $19.14
7% off - Save $1.53
or 4 payments of $4.40 with  for orders over $35
Quantity
Expected to Ship in 4-9 Days
Report an Error
This part fits these bikes:
KAWASAKI Brute Force 750 4x4i EPS
Year: 2020 Part: DISC,PRESSURE Part Group: DRIVE SHAFT-REAR
"""

CART_HIDDEN_REGION = """
STEP,FR,LH (34028-0327)
See Price in Cart
$37.30
or 4 payments of $9.33 with  for orders over $35
Quantity
Expected to Ship in 4-9 Days
Report an Error
This part fits these bikes:
KAWASAKI TEST
"""


def test_competitor_registry_and_selection_rules() -> None:
    assert get_competitor("partzilla").capabilities.status == "active"
    assert get_competitor("motosport").capabilities.status == "active"
    assert get_competitor("chaparral").display_name == "Chaparral Motorsports"
    with pytest.raises(ValueError):
        get_competitor("missing")
    selected = select_competitors(["partzilla", "motosport", "chaparral"])
    assert [item.competitor_key for item in selected] == ["partzilla", "motosport", "chaparral"]


def test_partzilla_adapter_url_generation_still_uses_existing_slug_map() -> None:
    adapter = get_competitor("partzilla")
    product = PartRecord("", "Can-Am", "07JAZ-001070A")

    assert adapter.build_product_url(product) == "https://www.partzilla.com/product/can-am/07JAZ-001070A"


def test_chaparral_adapter_uses_search_lookup_and_exact_normalized_matching() -> None:
    adapter = ChaparralAdapter()
    product = PartRecord("", "Honda", "15410-MFJ-D02")
    html = """
    <main>
      <a href="/oem/honda/diagram/15410-MFJ-D02">Honda Oil Filter 15410-MFJ-D02</a>
      <div>MSRP $12.99</div>
      <div>Your Price $9.49</div>
      <div>Available to Order</div>
    </main>
    """

    observation = adapter.parse_product_page(html, product, visible_text=html, final_url=adapter.lookup_url, http_status=200)

    assert adapter.build_product_url(product) == "https://www.chapmoto.com/search/?q=15410-MFJ-D02&type=oem"
    assert build_search_url("41080-1514") == "https://www.chapmoto.com/search/?q=41080-1514&type=oem"
    assert normalize_part_number_for_match("15410-MFJ-D02") == "15410MFJD02"
    assert observation.observed_part_number == "15410-MFJ-D02"
    assert observation.selling_price == Decimal("9.49")
    assert observation.reference_price == Decimal("12.99")
    assert observation.availability_status == "available_to_order"
    assert observation.raw_evidence_summary["lookup_status"] == "available_to_order"
    assert observation.raw_evidence_summary["currency"] == "USD"


def test_chaparral_rejects_partial_part_matches() -> None:
    match = select_exact_match(
        text="Honda Oil Filter 15410-MFJ-D021\nYour Price $9.49",
        requested_part_number="15410-MFJ-D02",
    )

    assert match is None


def test_chaparral_hidden_price_msrp_supersession_and_conflicts() -> None:
    hidden = ChaparralAdapter().parse_product_page(
        "15410-MFJ-D02\nAdd to View Price\nMSRP $12.99\nIn Stock",
        PartRecord("", "Honda", "15410-MFJ-D02"),
        visible_text="15410-MFJ-D02\nAdd to View Price\nMSRP $12.99\nIn Stock",
        http_status=200,
    )
    msrp_only = ChaparralAdapter().parse_product_page(
        "15410-MFJ-D02\nMSRP $12.99\nIn Stock",
        PartRecord("", "Honda", "15410-MFJ-D02"),
        visible_text="15410-MFJ-D02\nMSRP $12.99\nIn Stock",
        http_status=200,
    )
    superseded = ChaparralAdapter().parse_product_page(
        "15410-MFJ-D02\nSuperseded by 15410-MFJ-D03\n$10.49\nIn Stock",
        PartRecord("", "Honda", "15410-MFJ-D02"),
        visible_text="15410-MFJ-D02\nSuperseded by 15410-MFJ-D03\n$10.49\nIn Stock",
        http_status=200,
    )
    conflicting = ChaparralAdapter().parse_product_page(
        "15410-MFJ-D02\n$9.49\nIn Stock\nOther diagram\n15410-MFJ-D02\n$10.49\nIn Stock",
        PartRecord("", "Honda", "15410-MFJ-D02"),
        visible_text="15410-MFJ-D02\n$9.49\nIn Stock\nOther diagram\n15410-MFJ-D02\n$10.49\nIn Stock",
        http_status=200,
    )

    assert hidden.raw_evidence_summary["lookup_status"] == "part_found_price_hidden"
    assert hidden.selling_price is None
    assert "selling_price_hidden_in_cart" in hidden.warnings
    assert msrp_only.raw_evidence_summary["lookup_status"] == "msrp_only"
    assert msrp_only.reference_price == Decimal("12.99")
    assert msrp_only.selling_price is None
    assert superseded.raw_evidence_summary["lookup_status"] == "superseded"
    assert superseded.superseded_by_raw == "15410-MFJ-D03"
    assert conflicting.raw_evidence_summary["lookup_status"] == "multiple_exact_matches"
    assert conflicting.selling_price is None


def test_chaparral_preserves_price_for_multiple_supersession_results() -> None:
    text = """
    SUBSTITUTED BY 2878068 | SUPERCEDE TO 2877606
    $14.99
    OEM
    ---
    SUBSTITUTED BY 2878068 | SUPERCEDE TO 2872346
    $12.99
    OEM
    """

    observation = ChaparralAdapter().parse_product_page(
        text,
        PartRecord("", "Polaris", "2878068"),
        visible_text=text,
        http_status=200,
    )

    assert observation.selling_price == Decimal("14.99")
    assert observation.superseded_by_raw == "2877606"
    assert observation.raw_evidence_summary["lookup_status"] == "superseded"
    assert "multiple_supersession_options" in observation.warnings


def test_chaparral_uses_original_oem_card_price_before_substituted_cards() -> None:
    text = """
    OEM
    BELT DRIVE 3211186
    $199.99
    OEM
    SUBSTITUTED BY 3211165 | SUPERCEDE TO 3211165 3211166
    $209.99
    OEM
    SUBSTITUTED BY 3211164 | SUPERCEDE TO 3211164 3211108
    $59.99
    """

    observation = ChaparralAdapter().parse_product_page(
        text,
        PartRecord("", "Polaris", "3211186"),
        visible_text=text,
        http_status=200,
    )

    assert observation.selling_price == Decimal("199.99")
    assert observation.superseded_by_raw is None
    assert observation.raw_evidence_summary["lookup_status"] == "price_found"


def test_chaparral_uses_exact_matching_structured_product_price() -> None:
    html = """
    <main>
      <h1>Kawasaki DISC,RR</h1>
      <div>MSRP $235.50</div>
      <div>Add to Cart to View Price</div>
      <div>OEM Part Number: 41080-1514</div>
    </main>
    <script type="application/ld+json">
    {
      "@context": "https://schema.org/",
      "@type": "Product",
      "sku": "41080-1514",
      "name": "Kawasaki DISC,RR",
      "offers": {
        "priceCurrency": "USD",
        "price": 199.01,
        "url": "https://www.chapmoto.com/disc-rr--524045.html",
        "availability": "http://schema.org/InStock"
      }
    }
    </script>
    """

    observation = ChaparralAdapter().parse_product_page(
        html,
        PartRecord("", "Kawasaki", "41080-1514"),
        visible_text="Kawasaki DISC,RR\nMSRP $235.50\nAdd to Cart to View Price\nOEM Part Number: 41080-1514",
        final_url="https://www.chapmoto.com/disc-rr--524045.html",
        http_status=200,
    )

    assert observation.selling_price == Decimal("199.01")
    assert observation.reference_price == Decimal("235.50")
    assert observation.product_name == "Kawasaki DISC,RR"
    assert observation.availability_status == "in_stock"
    assert observation.price_visibility == "visible"
    assert observation.raw_evidence_summary["price_source"] == "structured_data"
    assert observation.raw_evidence_summary["lookup_status"] == "price_found"


def test_chaparral_ignores_placeholder_structured_price_for_cart_hidden_product() -> None:
    html = """
    <main>
      <h1>Can-Am FILTER OIL</h1>
      <div>MSRP $18.49</div>
      <div>Add to Cart to View Price</div>
      <div>OEM Part Number: 420956744</div>
    </main>
    <script type="application/ld+json">
    {
      "@context": "https://schema.org/",
      "@type": "Product",
      "sku": "420956744",
      "name": "Can-Am FILTER OIL",
      "offers": {
        "priceCurrency": "USD",
        "price": 9999.99,
        "url": "https://www.chapmoto.com/filter-oil-420956744.html",
        "availability": "http://schema.org/InStock"
      }
    }
    </script>
    """

    observation = ChaparralAdapter().parse_product_page(
        html,
        PartRecord("", "Can-Am", "420956744"),
        visible_text="Can-Am FILTER OIL\nMSRP $18.49\nAdd to Cart to View Price\nOEM Part Number: 420956744",
        final_url="https://www.chapmoto.com/filter-oil-420956744.html",
        http_status=200,
    )

    assert observation.selling_price is None
    assert observation.reference_price == Decimal("18.49")
    assert observation.price_visibility == "see_price_in_cart"
    assert observation.raw_evidence_summary["lookup_status"] == "part_found_price_hidden"
    assert observation.raw_evidence_summary["structured_data_used"] is False
    assert observation.raw_evidence_summary["structured_price_rejected_reason"] == "placeholder_price"
    assert "structured_price_placeholder_ignored" in observation.warnings


def test_chaparral_availability_and_blocking_statuses() -> None:
    adapter = ChaparralAdapter()
    product = PartRecord("", "Honda", "15410-MFJ-D02")

    assert normalize_availability("Available to Order") == "available_to_order"
    assert normalize_availability("Back Ordered") == "backordered"
    assert normalize_availability("Discontinued") == "discontinued"
    assert adapter.parse_product_page("captcha verify you are human", product, visible_text="captcha verify you are human").raw_evidence_summary["lookup_status"] == "captcha_detected"
    assert adapter.parse_product_page("Access denied", product, visible_text="Access denied", http_status=403).raw_evidence_summary["lookup_status"] == "blocked_or_rate_limited"
    assert adapter.parse_product_page("No results found", product, visible_text="No results found").raw_evidence_summary["lookup_status"] == "part_not_found"
    api_error = adapter.parse_product_page(
        '{"message": "There was an error pulling part data."}',
        product,
        visible_text="message: There was an error pulling part data.",
        http_status=400,
    )
    assert api_error.raw_evidence_summary["lookup_status"] == "lookup_error"
    assert api_error.page_classification == "normal_product"


def test_chaparral_database_seed_and_resolution_cache_table() -> None:
    db = _empty_db("chaparral_seed_cache.db")
    with connect_database(db) as conn:
        competitor = conn.execute("SELECT competitor_name, supports_direct_part_url FROM competitors WHERE competitor_code='chaparral'").fetchone()
        conn.execute(
            """
            INSERT INTO chaparral_resolution_cache(manufacturer, part_number, normalized_part_number, resolved_url,
                product_identifier, resolved_at, last_verified_at, is_valid, created_at, updated_at)
            VALUES ('Honda', '15410-MFJ-D02', '15410MFJD02', 'https://www.chapmoto.com/oem/test',
                '15410-MFJ-D02', ?, ?, 1, ?, ?)
            """,
            (utc_now(), utc_now(), utc_now(), utc_now()),
        )
        cached = conn.execute("SELECT resolved_url FROM chaparral_resolution_cache WHERE normalized_part_number='15410MFJD02'").fetchone()

    assert competitor["competitor_name"] == "Chaparral Motorsports"
    assert competitor["supports_direct_part_url"] == 0
    assert cached["resolved_url"] == "https://www.chapmoto.com/oem/test"


def test_chaparral_unsupported_manufacturer_does_not_open_page() -> None:
    db = _empty_db("chaparral_unsupported_no_page.db")
    result = _upload_simple_batch(db, "chaparral-unsupported.xlsx", "SKU-AC", "Arctic Cat", "AC-1")
    confirm_import(db, result.import_batch_id)
    record = PartRecord("", "Arctic Cat", "AC-1")
    with connect_database(db) as conn:
        collect_parts.ensure_competitor_listings(conn, [record], "chaparral")
        plan = collect_parts.plan_collection(conn, [record], Path("input.csv"), 1, competitor_key="chaparral")
        scan_run_id = create_scan_run(conn, competitor_id=seed_competitor(conn, "chaparral"), requested_part_count=1)

    class FailingPage:
        def __getattr__(self, name):
            raise AssertionError("Unsupported manufacturer should not open Chaparral.")

    row = collect_parts.collect_one_chaparral_part(db, FailingPage(), plan.planned_parts[0], scan_run_id, SimpleNamespace(timeout=1000, render_settle_ms=0))

    assert row.manufacturer_supported is False
    assert row.lookup_status == "manufacturer_not_carried"
    assert row.result_type == "manufacturer_not_carried"
    assert "does not carry OEM manufacturer Arctic Cat" in row.status_reason


def test_probe_summary_reports_unsupported_manufacturer_without_scrape_error() -> None:
    record = PartRecord("", "Polaris", "2879324")
    support = manufacturer_support_metadata("motosport", record.manufacturer, record.oem_part_number)
    observation = probe_competitor._unsupported_observation("motosport", record, support)
    summary = probe_competitor._summary_row(probe_competitor.ProbeRow(1, record.manufacturer, record.oem_part_number, "", utc_now(), observation))

    assert summary["competitor"] == "motosport"
    assert summary["manufacturer"] == "Polaris"
    assert summary["normalized_manufacturer"] == "Polaris"
    assert summary["manufacturer_supported"] is False
    assert summary["lookup_status"] == "manufacturer_not_carried"
    assert summary["result_type"] == "manufacturer_not_carried"
    assert "does not carry OEM manufacturer Polaris" in summary["status_reason"]
    assert summary["http_status"] == ""
    assert summary["url"] == ""


def test_collect_parts_accepts_partzilla_and_motosport_competitors(monkeypatch) -> None:
    db = _prepared_collect_db("collect_competitor.db")
    csv_path = _input_csv("collect_competitor.csv")
    monkeypatch.setattr(collect_parts.sys, "argv", ["collect_parts.py", "--competitor", "partzilla", "--file", str(csv_path), "--max-parts", "1", "--dry-run", "--database", str(db)])
    assert collect_parts.main() == 0

    monkeypatch.setattr(collect_parts.sys, "argv", ["collect_parts.py", "--competitor", "motosport", "--file", str(csv_path), "--max-parts", "1", "--dry-run", "--database", str(db)])
    assert collect_parts.main() == 0


def test_motosport_url_generation_preserves_part_numbers() -> None:
    adapter = MotoSportAdapter()

    assert adapter.build_product_url(PartRecord("", "Kawasaki", "41080-1514")).endswith("/41080-1514")
    assert adapter.build_product_url(PartRecord("", "Kawasaki", "K53001-240")).endswith("/K53001-240")
    assert adapter.build_product_url(PartRecord("", "Honda", "00123")).endswith("/00123")


def test_motosport_regular_price_fixture_parses() -> None:
    obs = MotoSportAdapter().parse_product_page(REGULAR_HTML, PartRecord("", "Kawasaki", "13270-1800"), visible_text=REGULAR_HTML, http_status=200)

    assert str(obs.selling_price) == "1.40"
    assert obs.reference_price is None
    assert obs.price_visibility == "visible"
    assert obs.price_display_type == "regular"
    assert obs.availability_raw == "In Stock"
    assert obs.availability_status == "in_stock"


def test_motosport_discounted_fixture_parses_roles_and_availability() -> None:
    obs = MotoSportAdapter().parse_product_page(DISCOUNTED_HTML, PartRecord("", "Kawasaki", "41080-1483"), visible_text=DISCOUNTED_HTML, http_status=200)

    assert str(obs.selling_price) == "16.51"
    assert str(obs.reference_price) == "17.95"
    assert obs.savings_percent == 8
    assert str(obs.savings_amount) == "1.44"
    assert obs.price_visibility == "visible"
    assert obs.price_display_type == "discounted"
    assert obs.availability_status == "ships_in"


def test_motosport_ambiguous_or_missing_price_warnings() -> None:
    adapter = MotoSportAdapter()
    ambiguous_text = "part (ABC)\n$1.00\n$2.00\n$3.00\nIn Stock"
    missing_text = "part (ABC)\nIn Stock"
    ambiguous = adapter.parse_product_page(ambiguous_text, PartRecord("", "Kawasaki", "ABC"), visible_text=ambiguous_text, http_status=200)
    missing = adapter.parse_product_page(missing_text, PartRecord("", "Kawasaki", "ABC"), visible_text=missing_text, http_status=200)

    assert "ambiguous_price_candidates" in ambiguous.warnings
    assert ambiguous.selling_price is None
    assert ambiguous.parse_confidence == "low"
    assert "selling_price_not_found" in missing.warnings


def test_motosport_rejects_global_promos_and_requires_product_association() -> None:
    obs = MotoSportAdapter().parse_product_page(GLOBAL_PROMO_HTML, PartRecord("", "Kawasaki", "13270-1800"), visible_text=GLOBAL_PROMO_HTML, http_status=200)

    assert obs.product_name == "plate"
    assert str(obs.selling_price) == "1.40"
    assert obs.savings_percent is None
    assert obs.raw_evidence_summary["product_association"]["confirmed"] is True
    rejected_global = [item for item in obs.raw_evidence_summary["price_evidence"]["candidates"] if item["raw_value"] == "$79"]
    assert rejected_global
    assert rejected_global[0]["rejection_reason"] == "outside_selected_product_region"


def test_motosport_404_and_unassociated_pages_never_emit_prices() -> None:
    not_found = MotoSportAdapter().parse_product_page(NOT_FOUND_HTML, PartRecord("", "Can-Am", "707003505"), visible_text=NOT_FOUND_HTML, http_status=404)
    unassociated = MotoSportAdapter().parse_product_page("Skip to content\n$79\n60% off\nIn Stock", PartRecord("", "Can-Am", "707003505"), visible_text="Skip to content\n$79\n60% off\nIn Stock", http_status=200)

    assert not_found.page_classification == "not_found"
    assert not_found.product_name is None
    assert not_found.selling_price is None
    assert not_found.savings_percent is None
    assert not_found.raw_evidence_summary["product_association"]["confirmed"] is False
    assert unassociated.selling_price is None
    assert unassociated.savings_percent is None
    assert "product_association_not_confirmed" in unassociated.warnings


def test_motosport_see_price_in_cart_reference_only_detection() -> None:
    obs = MotoSportAdapter().parse_product_page(CART_HIDDEN_REGION, PartRecord("", "Kawasaki", "34028-0327"), visible_text=CART_HIDDEN_REGION, http_status=200)

    assert obs.price_visibility == "see_price_in_cart"
    assert obs.selling_price is None
    assert str(obs.reference_price) == "37.30"
    assert obs.price_display_type == "cart_price_hidden"
    assert obs.selling_price_confidence == "low"
    assert obs.reference_price_confidence == "high"
    assert obs.parse_confidence == "high"
    assert "selling_price_hidden_in_cart" in obs.warnings
    assert "selling_price_not_found" not in obs.warnings
    assert obs.raw_evidence_summary["price_evidence"]["see_price_in_cart_detected"] is True
    assert obs.raw_evidence_summary["price_evidence"]["price_cluster"]["rule"] == "see_price_in_cart_reference_only"


def test_motosport_global_see_price_in_cart_outside_panel_is_ignored() -> None:
    text = """
See Price in Cart
Promo Banner
PLATE (13270-1800)
$1.40
In Stock
"""
    obs = MotoSportAdapter().parse_product_page(text, PartRecord("", "Kawasaki", "13270-1800"), visible_text=text, http_status=200)

    assert obs.price_visibility == "visible"
    assert str(obs.selling_price) == "1.40"
    assert obs.reference_price is None
    assert "selling_price_hidden_in_cart" not in obs.warnings


def test_motosport_saved_13270_1800_fixture_rejects_financing_and_resolves_price() -> None:
    obs = MotoSportAdapter().parse_product_page(SAVED_13270_1800_REGION, PartRecord("", "Kawasaki", "13270-1800"), visible_text=SAVED_13270_1800_REGION, http_status=200)
    candidates = obs.raw_evidence_summary["price_evidence"]["candidates"]

    assert str(obs.selling_price) == "1.59"
    assert obs.reference_price is None
    assert obs.price_visibility == "visible"
    assert obs.price_display_type == "regular"
    assert obs.parse_confidence == "high"
    assert "ambiguous_price_candidates" not in obs.warnings
    assert [item for item in candidates if item["raw_value"] == "$0.40"][0]["role"] == "financing_payment"
    assert [item for item in candidates if item["raw_value"] == "$35"][0]["role"] == "order_threshold"


def test_motosport_monthly_payment_text_does_not_create_ambiguous_price() -> None:
    text = """
    DISC PACKAGE (79532010033)
    $209.15 $246.06
    15% off - Save $36.91
    or monthly payments as low as $25.42 with information
    Quantity
    In Stock
    """

    observation = MotoSportAdapter().parse_product_page(
        text,
        PartRecord("", "KTM", "79532010033"),
        visible_text=text,
        http_status=200,
    )
    candidates = observation.raw_evidence_summary["price_evidence"]["candidates"]

    assert observation.selling_price == Decimal("209.15")
    assert observation.reference_price == Decimal("246.06")
    assert observation.savings_percent == 15
    assert observation.savings_amount == Decimal("36.91")
    assert observation.price_visibility == "visible"
    assert "ambiguous_price_candidates" not in observation.warnings
    assert [item for item in candidates if item["raw_value"] == "$25.42"][0]["role"] == "financing_payment"


def test_motosport_saved_41080_1483_fixture_extracts_discounted_cluster() -> None:
    obs = MotoSportAdapter().parse_product_page(SAVED_41080_1483_REGION, PartRecord("", "Kawasaki", "41080-1483"), visible_text=SAVED_41080_1483_REGION, http_status=200)
    cluster = obs.raw_evidence_summary["price_evidence"]["price_cluster"]

    assert str(obs.selling_price) == "17.61"
    assert str(obs.reference_price) == "19.14"
    assert obs.savings_percent == 7
    assert str(obs.savings_amount) == "1.53"
    assert obs.price_visibility == "visible"
    assert obs.price_display_type == "discounted"
    assert obs.parse_confidence == "high"
    assert cluster["savings_math_valid"] is True
    assert [item for item in obs.raw_evidence_summary["price_evidence"]["candidates"] if item["raw_value"] == "$17.61"][0]["accepted_role"] == "selling_price"
    assert [item for item in obs.raw_evidence_summary["price_evidence"]["candidates"] if item["raw_value"] == "$19.14"][0]["accepted_role"] == "reference_price"


def test_motosport_savings_evidence_must_be_near_price_cluster() -> None:
    text = """
PART (ABC)
$10.00 $12.00
Quantity
Expected to Ship in 4-9 Days
This part fits these bikes:
KAWASAKI TEST
7% off - Save $2.00
"""
    obs = MotoSportAdapter().parse_product_page(text, PartRecord("", "Kawasaki", "ABC"), visible_text=text, http_status=200)

    assert obs.selling_price is None
    assert obs.reference_price is None
    assert obs.savings_percent is None
    assert "ambiguous_price_candidates" in obs.warnings


def test_motosport_multiple_unmarked_panel_prices_remain_ambiguous() -> None:
    text = """
PART (ABC)
$10.00
$12.00
$14.00
Expected to Ship in 4-9 Days
"""
    obs = MotoSportAdapter().parse_product_page(text, PartRecord("", "Kawasaki", "ABC"), visible_text=text, http_status=200)

    assert obs.selling_price is None
    assert obs.parse_confidence == "low"
    assert "ambiguous_price_candidates" in obs.warnings


def test_probe_safety_limits_and_stop_rules() -> None:
    args = argparse.Namespace(competitor="motosport", max_parts=10, delay_seconds=5)
    probe_competitor.validate_probe_args(args)
    assert probe_competitor.DEFAULT_PROBE_MAX_PARTS == 10
    assert probe_competitor.HARD_PROBE_MAX_PARTS == 25
    with pytest.raises(ValueError):
        probe_competitor.validate_probe_args(argparse.Namespace(competitor="motosport", max_parts=26, delay_seconds=5))
    with pytest.raises(ValueError):
        probe_competitor.validate_probe_args(argparse.Namespace(competitor="motosport", max_parts=1, delay_seconds=4))
    assert 403 in probe_competitor.STOP_STATUSES
    assert 429 in probe_competitor.STOP_STATUSES
    assert "challenge" in probe_competitor.STOP_CLASSIFICATIONS


def test_motosport_25_part_probe_input_is_multi_oem_and_probe_limit_allows_25() -> None:
    result = load_parts_csv(Path("data/input/MotoSport_25_Part_Probe.csv"))
    counts: dict[str, int] = {}
    for record in result.records:
        counts[record.manufacturer] = counts.get(record.manufacturer, 0) + 1
    parts = [record.oem_part_number for record in result.records]

    assert len(result.records) == 25
    assert counts == {"Honda": 5, "Yamaha": 5, "Suzuki": 5, "Polaris": 5, "Can-Am": 5}
    assert "Kawasaki" not in counts
    assert parts == [
        "18327-MEN-A30",
        "06115-MCA-000",
        "50620-HN1-000",
        "07JAZ-001070A",
        "83500-HN7-000ZA",
        "1MC-2835V-00-P4",
        "1S3-23391-20-00",
        "B4B-2172A-00-00",
        "94227-19292-00",
        "F3H-U410M-00-00",
        "13101-38H00",
        "37101-20820",
        "09363-02002",
        "99500-37L00-03E",
        "17418-33400",
        "2879324",
        "2638684-728",
        "5452743",
        "4010818",
        "1541864-067",
        "705002244",
        "S83625RCA000JF",
        "707003505",
        "708300053",
        "705014896",
    ]
    assert not {"12345", "H-100", "H-MISSING", "Y-1", "Y-2", "Y-100", "S-100", "P-100", "C-100"}.intersection(parts)
    probe_competitor.validate_probe_args(argparse.Namespace(competitor="motosport", max_parts=25, delay_seconds=5))


def test_probe_outputs_exclude_secrets_and_probe_db_save_is_separate() -> None:
    db = _empty_db("probe_db_save.db")
    initialize_database(db)
    output_dir = TEST_OUTPUT_DIR / "probe-output-safe"
    output_dir.mkdir(parents=True, exist_ok=True)
    run = probe_competitor.ProbeRun("motosport", "2026-07-11T00:00:00Z")
    obs = MotoSportAdapter().parse_product_page(REGULAR_HTML, PartRecord("", "Kawasaki", "13270-1800"), visible_text=REGULAR_HTML, http_status=200)
    run.rows.append(probe_competitor.ProbeRow(1, "Kawasaki", "13270-1800", "https://www.motosport.com/oem-parts/part-number/13270-1800", utc_now(), obs))
    probe_competitor._write_outputs(output_dir, run, argparse.Namespace(file=Path("probe.csv"), max_parts=1, delay_seconds=5, save_probe_to_database=False))
    text = (output_dir / "probe_metadata.json").read_text(encoding="utf-8") + (output_dir / "probe_review.txt").read_text(encoding="utf-8")
    assert "cookie" not in text.lower()
    assert "token" not in text.lower()
    assert "password" not in text.lower()
    diagnostics_dir = output_dir / "diagnostics" / "001_13270-1800"
    assert (diagnostics_dir / "price_evidence.json").exists()
    assert (diagnostics_dir / "selected_product_region.txt").exists()
    assert (diagnostics_dir / "page_classification.txt").exists()
    assert (diagnostics_dir / "product_association.json").exists()

    before = _count(db, "current_listing_state")
    probe_competitor._save_probe_results(db, run)
    assert _count(db, "competitor_probe_results") == 1
    assert _count(db, "current_listing_state") == before
    with connect_database(db) as conn:
        saved = conn.execute("SELECT price_visibility, price_display_type, result_type FROM competitor_probe_results").fetchone()
    assert saved["price_visibility"] == "visible"
    assert saved["result_type"] == "selling_price_found"


def test_probe_review_does_not_count_404_prices_and_flags_global_leaks() -> None:
    run = probe_competitor.ProbeRun("motosport", "2026-07-11T00:00:00Z", completed_at="2026-07-11T00:01:00Z")
    row_404 = MotoSportAdapter().parse_product_page(NOT_FOUND_HTML, PartRecord("", "Can-Am", "707003505"), visible_text=NOT_FOUND_HTML, http_status=404)
    run.rows.append(probe_competitor.ProbeRow(1, "Can-Am", "707003505", "https://www.motosport.com/oem-parts/part-number/707003505", utc_now(), row_404))

    review = probe_competitor._review_text(run)

    assert "Successful selling price observations: 0" in review
    assert "Visible selling prices found: 0" in review
    assert "Cart-hidden price pages: 0" in review
    assert "Any price/reference found: 0" in review
    assert "HTTP 200 product pages: 0" in review
    assert "High-confidence prices: 0" in review
    assert "Ambiguous prices: 0" in review
    assert "Not found: 1" in review
    assert "URL predictability: direct part-number URLs appear to work for some MotoSport parts" in review

    leaked = probe_competitor.ProbeRun("motosport", "2026-07-11T00:00:00Z", completed_at="2026-07-11T00:01:00Z")
    for index, part in enumerate(("A", "B"), start=1):
        leaked.rows.append(
            probe_competitor.ProbeRow(
                index,
                "Kawasaki",
                part,
                f"https://example.test/{part}",
                utc_now(),
                CompetitorObservation(
                    competitor_key="motosport",
                    manufacturer="Kawasaki",
                    oem_part_number=part,
                    product_name="Skip to content",
                    page_classification="normal_product",
                    savings_percent=60,
                    raw_evidence_summary={"product_association": {"confirmed": False}},
                ),
            )
        )
    leaked_review = probe_competitor._review_text(leaked)
    assert "Global promo leak suspected: yes" in leaked_review
    assert "Parser quality: failed_pending_fix" in leaked_review


def test_probe_review_counts_cart_hidden_pages_separately() -> None:
    run = probe_competitor.ProbeRun("motosport", "2026-07-11T00:00:00Z", completed_at="2026-07-11T00:01:00Z")
    obs = MotoSportAdapter().parse_product_page(CART_HIDDEN_REGION, PartRecord("", "Kawasaki", "34028-0327"), visible_text=CART_HIDDEN_REGION, http_status=200)
    run.rows.append(probe_competitor.ProbeRow(1, "Kawasaki", "34028-0327", "https://www.motosport.com/oem-parts/part-number/34028-0327", utc_now(), obs))

    summary = probe_competitor._summary_row(run.rows[0])
    review = probe_competitor._review_text(run)

    assert summary["price_visibility"] == "see_price_in_cart"
    assert summary["result_type"] == "price_hidden_in_cart"
    assert summary["selling_price_found"] is False
    assert summary["reference_price_found"] is True
    assert summary["cart_hidden_price"] is True
    assert summary["see_price_in_cart_detected"] is True
    assert "Successful selling price observations: 0" in review
    assert "Reference prices found: 1" in review
    assert "Cart-hidden price pages: 1" in review


def test_probe_review_includes_motosport_coverage_summary() -> None:
    run = probe_competitor.ProbeRun("motosport", "2026-07-11T00:00:00Z", completed_at="2026-07-11T00:01:00Z")
    visible = MotoSportAdapter().parse_product_page(REGULAR_HTML, PartRecord("", "Kawasaki", "13270-1800"), visible_text=REGULAR_HTML, http_status=200)
    hidden = MotoSportAdapter().parse_product_page(CART_HIDDEN_REGION, PartRecord("", "Kawasaki", "34028-0327"), visible_text=CART_HIDDEN_REGION, http_status=200)
    not_found = MotoSportAdapter().parse_product_page(NOT_FOUND_HTML, PartRecord("", "Can-Am", "707003505"), visible_text=NOT_FOUND_HTML, http_status=404)
    run.rows.extend(
        [
            probe_competitor.ProbeRow(1, "Kawasaki", "13270-1800", "https://example.test/13270-1800", utc_now(), visible),
            probe_competitor.ProbeRow(2, "Kawasaki", "34028-0327", "https://example.test/34028-0327", utc_now(), hidden),
            probe_competitor.ProbeRow(3, "Can-Am", "707003505", "https://example.test/707003505", utc_now(), not_found),
        ]
    )

    review = probe_competitor._review_text(run)

    assert "MOTOSPORT COVERAGE SUMMARY" in review
    assert "Direct URL success rate: 66.7% (2/3)" in review
    assert "Visible selling price rate: 33.3% (1/3)" in review
    assert "Cart-hidden rate: 33.3% (1/3)" in review
    assert "Not-found rate: 33.3% (1/3)" in review
    assert "Ambiguous price rate: 0.0% (0/3)" in review
    assert "High-confidence visible price rate: 33.3% (1/3)" in review
    assert "Manufacturer-level summary:" in review
    assert "Kawasaki | 2 | 2 | 1 | 1 | 0 | 0 | 1" in review
    assert "Can-Am | 1 | 0 | 0 | 0 | 1 | 0 | 1" in review
    assert "Coverage should be evaluated before promoting this competitor." in review


def test_probe_review_flags_placeholder_test_inputs() -> None:
    run = probe_competitor.ProbeRun("motosport", "2026-07-11T00:00:00Z", completed_at="2026-07-11T00:01:00Z")
    obs = MotoSportAdapter().parse_product_page(NOT_FOUND_HTML, PartRecord("", "Honda", "H-MISSING"), visible_text=NOT_FOUND_HTML, http_status=404)
    run.rows.append(probe_competitor.ProbeRow(1, "Honda", "H-MISSING", "https://example.test/H-MISSING", utc_now(), obs))

    review = probe_competitor._review_text(run)

    assert probe_competitor.placeholder_part_numbers(run.rows) == ["H-MISSING"]
    assert "Input quality: placeholder/test file detected" in review
    assert "WARNING: This probe input contains placeholder part numbers" in review
    assert "Placeholder parts detected: H-MISSING" in review


def test_export_cart_hidden_probe_input_from_prior_probe_summary() -> None:
    probe_dir = TEST_OUTPUT_DIR / "motosport-probe-export" / "20260711T000000Z"
    probe_dir.mkdir(parents=True, exist_ok=True)
    (probe_dir / "probe_summary.csv").write_text(
        "manufacturer,oem_part_number,product_name,url,reference_price,price_visibility,result_type,cart_hidden_price\n"
        "Kawasaki,34028-0327,STEP,https://example.test/34028-0327,37.30,see_price_in_cart,price_hidden_in_cart,True\n"
        "Kawasaki,41080-1514,DISC,https://example.test/41080-1514,,visible,selling_price_found,False\n",
        encoding="utf-8",
    )
    output = TEST_OUTPUT_DIR / "MotoSport_Cart_Hidden_Probe.csv"

    rows = export_cart_hidden_probe_input.export_cart_hidden_rows(probe_dir, output)
    with output.open("r", newline="", encoding="utf-8") as file:
        loaded = list(csv.DictReader(file))

    assert len(rows) == 1
    assert loaded[0]["oem_part_number"] == "34028-0327"
    assert loaded[0]["prior_probe_timestamp"] == "20260711T000000Z"
    assert loaded[0]["price_visibility"] == "see_price_in_cart"


def test_cart_price_probe_gating_and_input_validation(monkeypatch) -> None:
    valid_args = argparse.Namespace(competitor="motosport", max_parts=5, delay_seconds=5, experimental_cart_pricing=True)
    probe_cart_price.validate_args(valid_args)
    probe_cart_price.validate_args(argparse.Namespace(competitor="motosport", max_parts=2, delay_seconds=5, experimental_cart_pricing=True))
    with pytest.raises(ValueError):
        probe_cart_price.validate_args(argparse.Namespace(competitor="motosport", max_parts=5, delay_seconds=5, experimental_cart_pricing=False))
    with pytest.raises(ValueError):
        probe_cart_price.validate_args(argparse.Namespace(competitor="motosport", max_parts=6, delay_seconds=5, experimental_cart_pricing=True))
    with pytest.raises(ValueError):
        probe_cart_price.validate_args(argparse.Namespace(competitor="motosport", max_parts=1, delay_seconds=4, experimental_cart_pricing=True))

    input_path = TEST_OUTPUT_DIR / "cart-hidden-input.csv"
    input_path.write_text(
        "manufacturer,oem_part_number,product_name,product_url,reference_price,prior_probe_timestamp,price_visibility,result_type\n"
        "Kawasaki,34028-0327,STEP,https://example.test/34028-0327,37.30,20260711T000000Z,see_price_in_cart,price_hidden_in_cart\n",
        encoding="utf-8",
    )
    rows = probe_cart_price.load_cart_probe_rows(input_path)
    assert rows[0].oem_part_number == "34028-0327"

    non_hidden = TEST_OUTPUT_DIR / "cart-visible-input.csv"
    non_hidden.write_text(
        "manufacturer,oem_part_number,product_name,product_url,reference_price,prior_probe_timestamp,price_visibility,result_type\n"
        "Kawasaki,41080-1514,DISC,https://example.test/41080-1514,,20260711T000000Z,visible,selling_price_found\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError):
        probe_cart_price.load_cart_probe_rows(non_hidden)

    monkeypatch.setattr(probe_cart_price.sys, "argv", ["probe_cart_price.py", "--competitor", "motosport", "--file", str(input_path), "--experimental-cart-pricing"])
    monkeypatch.setattr("builtins.input", lambda prompt: "RUN")
    assert probe_cart_price.main() == 1


def test_known_cart_hidden_probe_input_contains_two_prior_observations() -> None:
    rows = probe_cart_price.load_cart_probe_rows(Path("data/input/MotoSport_Known_Cart_Hidden_Probe.csv"))

    assert [row.oem_part_number for row in rows] == ["41080-1514", "34028-0327"]
    assert [row.product_name for row in rows] == ["DISC,RR", "STEP,FR,LH"]
    assert [row.reference_price for row in rows] == ["282.32", "37.30"]
    assert all("see_price_in_cart" in row.prior_probe_note for row in rows)


def test_cart_price_probe_safe_cart_helpers_do_not_checkout() -> None:
    row = probe_cart_price.CartProbeInputRow("Kawasaki", "34028-0327", "STEP", "https://example.test/34028-0327", "37.30", "20260711T000000Z")
    cart_text = "Cart\nSTEP\n34028-0327\nQty: 1\n$32.69\nSubtotal $32.69"
    line = probe_cart_price.matching_cart_line(cart_text, row)

    assert line is not None
    assert probe_cart_price.extract_first_money(line) == Decimal("32.69")
    assert probe_cart_price.extract_quantity(line) == 1
    assert probe_cart_price._cart_has_unrelated_items("Checkout\nUnrelated Product\n$9.99") is True
    assert probe_cart_price._cart_empty_text("Your cart is empty") is True

    page = _FakePage()
    assert probe_cart_price.click_add_to_cart(page) is True
    line_evidence = {"confirmed": True, "remove_selector": 'a.cart-remove-item[title="Remove item from cart."][data-sku="KAW186Z-X001-Y001"]'}
    assert probe_cart_price.remove_cart_item(page, line_evidence=line_evidence) is True
    assert page.clicked == ["button:has-text('Add to Cart')", 'a.cart-remove-item[title="Remove item from cart."][data-sku="KAW186Z-X001-Y001"]']
    assert not any("checkout" in selector.lower() for selector in page.requested_selectors)


def test_cart_action_candidate_scoring_rejects_checkout_and_requires_single_high_confidence() -> None:
    row = probe_cart_price.CartProbeInputRow("Kawasaki", "41080-1514", "DISC,RR", "https://example.test/41080-1514", "282.32", prior_probe_note="see_price_in_cart")
    product_region = "KAWASAKI OEM DISC | 41080-1514 | See Price in Cart | Quantity"

    add_to_cart = probe_cart_price.score_cart_action_candidate(
        _control(1, "button", "Add to Cart", data_testid="add-to-cart"),
        row=row,
        product_region=product_region,
    )
    see_price = probe_cart_price.score_cart_action_candidate(
        _control(2, "button", "", aria_label="See Price in Cart"),
        row=row,
        product_region=product_region,
    )
    checkout = probe_cart_price.score_cart_action_candidate(
        _control(3, "button", "Proceed to Checkout"),
        row=row,
        product_region=product_region,
    )
    outside_panel = probe_cart_price.score_cart_action_candidate(
        _control(4, "button", "Add to Cart"),
        row=row,
        product_region="",
    )

    assert add_to_cart["confidence"] == "high"
    assert add_to_cart["data_testid"] == "add-to-cart"
    assert see_price["confidence"] == "high"
    assert see_price["aria_label"] == "See Price in Cart"
    assert checkout["rejected"] is True
    assert checkout["confidence"] == "low"
    assert "rejected_checkout_or_payment_control" in checkout["reasons"]
    assert outside_panel["confidence"] == "medium"
    assert outside_panel["outside_product_panel"] is True

    assert probe_cart_price.select_high_confidence_cart_action([add_to_cart])["status"] == "selected"
    assert probe_cart_price.select_high_confidence_cart_action([add_to_cart, see_price])["status"] == "ambiguous"
    assert probe_cart_price.select_high_confidence_cart_action([outside_panel])["status"] == "not_found"
    assert probe_cart_price.click_cart_action(_FakePage(), checkout) is False


def test_cart_action_inventory_collects_visible_controls_and_forms() -> None:
    row = probe_cart_price.CartProbeInputRow("Kawasaki", "41080-1514", "DISC,RR", "https://example.test/41080-1514", "282.32", prior_probe_note="see_price_in_cart")
    observation = SimpleNamespace(raw_evidence_summary={"selected_product_region": "KAWASAKI DISC 41080-1514 See Price in Cart"})
    page = _EvaluatePage()

    inventory = probe_cart_price.collect_cart_action_inventory(page, row, observation)

    assert inventory["candidate_count"] == 3
    assert inventory["high_confidence_count"] == 1
    assert inventory["visible_buttons"][0]["data_testid"] == "add-to-cart"
    assert inventory["visible_links"][0]["text"] == "Return Policy"
    assert inventory["product_panel_forms"][0]["id"] == "product-form"


def test_input_addtocartbutton_is_high_confidence_and_prefers_stable_selector() -> None:
    row = probe_cart_price.CartProbeInputRow("Kawasaki", "41080-1514", "DISC,RR", "https://example.test/41080-1514", "282.32", prior_probe_note="see_price_in_cart")
    candidate = probe_cart_price.score_cart_action_candidate(
        _control(
            1088,
            "input",
            "Add to cart",
            id="addtocartbutton",
            input_value="Add to cart",
            form_id="addtocartform",
            form_action="https://www.motosport.com/cart/add",
            form_method="post",
            selector_hint="input:nth-of-type(1088)",
            stable_selector="#addtocartbutton",
            input_type="submit",
            data_tracking_category="oem_AddToCartButton",
            data_tracking_label="KAW186Z-X001-Y001",
        ),
        row=row,
        product_region="KAWASAKI DISC,RR 41080-1514 See Price in Cart Quantity",
    )
    page = _SelectorPage(available={'form#addtocartform input#addtocartbutton[type="submit"]', "#addtocartbutton"})

    assert candidate["confidence"] == "high"
    assert candidate["cart_related"] is True
    assert "stable_addtocartbutton_selector" in candidate["reasons"]
    assert "oem_add_to_cart_tracking_fallback" in candidate["reasons"]
    assert probe_cart_price.extract_tracking_label(candidate) == "KAW186Z-X001-Y001"
    assert probe_cart_price.select_high_confidence_cart_action([candidate])["status"] == "selected"
    assert probe_cart_price.click_cart_action(page, candidate) is True
    assert page.clicked == ['form#addtocartform input#addtocartbutton[type="submit"]']
    assert "input:nth-of-type(1088)" not in page.clicked


def test_chaparral_add_to_cart_input_is_high_confidence_and_prefers_stable_selector() -> None:
    row = probe_cart_price.CartProbeInputRow("Can-Am", "420956744", "Can-Am FILTER OIL", "https://www.chapmoto.com/filter-oil-420956744.html", "18.49", prior_probe_note="chaparral_add_to_view_price")
    candidate = probe_cart_price.score_cart_action_candidate(
        _control(
            12,
            "input",
            "Add to Cart",
            id="add-to-cart",
            input_value="Add to Cart",
            form_id="orderform",
            form_action="https://www.chapmoto.com/cart.php",
            form_method="post",
            selector_hint="input:nth-of-type(12)",
            stable_selector="#add-to-cart",
            input_type="submit",
        ),
        row=row,
        product_region="OEM Part Number: 420956744\nPart: Can-Am FILTER OIL\nAdd to Cart to View Price",
    )
    page = _SelectorPage(available={'form#orderform input#add-to-cart[type="submit"]', "#add-to-cart"})

    assert candidate["confidence"] == "high"
    assert candidate["cart_related"] is True
    assert "stable_chaparral_add_to_cart_selector" in candidate["reasons"]
    assert "chaparral_cart_form_action" in candidate["reasons"]
    assert probe_cart_price.select_high_confidence_cart_action([candidate])["status"] == "selected"
    assert probe_cart_price.click_cart_action(page, candidate) is True
    assert page.clicked == ['form#orderform input#add-to-cart[type="submit"]']
    assert "input:nth-of-type(12)" not in page.clicked


def test_cart_action_click_timeout_returns_diagnostic_result() -> None:
    row = probe_cart_price.CartProbeInputRow("Kawasaki", "41080-1514", "DISC,RR", "https://example.test/41080-1514", "282.32", prior_probe_note="see_price_in_cart")
    candidate = probe_cart_price.score_cart_action_candidate(
        _control(
            1088,
            "input",
            "Add to cart",
            id="addtocartbutton",
            input_value="Add to cart",
            form_id="addtocartform",
            form_action="https://www.motosport.com/cart/add",
            form_method="post",
            stable_selector="#addtocartbutton",
            input_type="submit",
        ),
        row=row,
        product_region="KAWASAKI DISC,RR 41080-1514 See Price in Cart Quantity",
    )
    page = _SelectorPage(available={'form#addtocartform input#addtocartbutton[type="submit"]'})
    page.timeout_on_click = True

    result = probe_cart_price.click_cart_action_with_result(page, candidate, timeout_ms=123)

    assert result["clicked"] is False
    assert result["reason"] == "add_to_cart_click_timeout"
    assert result["selector"] == 'form#addtocartform input#addtocartbutton[type="submit"]'
    assert page.click_timeouts == [123]


def test_cart_action_suppresses_attentive_overlay_and_retries_click() -> None:
    row = probe_cart_price.CartProbeInputRow("Kawasaki", "41080-1514", "DISC,RR", "https://example.test/41080-1514", "282.32", prior_probe_note="see_price_in_cart")
    candidate = probe_cart_price.score_cart_action_candidate(
        _control(
            1088,
            "input",
            "Add to cart",
            id="addtocartbutton",
            input_value="Add to cart",
            form_id="addtocartform",
            form_action="https://www.motosport.com/cart/add",
            form_method="post",
            stable_selector="#addtocartbutton",
            input_type="submit",
        ),
        row=row,
        product_region="KAWASAKI DISC,RR 41080-1514 See Price in Cart Quantity",
    )
    page = _SelectorPage(available={'form#addtocartform input#addtocartbutton[type="submit"]'})
    page.click_timeouts_remaining = 1

    result = probe_cart_price.click_cart_action_with_result(page, candidate, timeout_ms=456)

    assert result["clicked"] is True
    assert result["reason"] == "clicked_after_overlay_suppression"
    assert result["selector"] == 'form#addtocartform input#addtocartbutton[type="submit"]'
    assert "#attentive_overlay" in result["suppressed_overlays"]
    assert page.clicked == ['form#addtocartform input#addtocartbutton[type="submit"]']
    assert page.click_timeouts == [456]


def test_addtocart_data_tracking_category_is_fallback_but_label_is_not_stable() -> None:
    row = probe_cart_price.CartProbeInputRow("Kawasaki", "41080-1514", "DISC,RR", "https://example.test/41080-1514", "282.32", prior_probe_note="see_price_in_cart")
    candidate = probe_cart_price.score_cart_action_candidate(
        _control(
            1,
            "input",
            "",
            input_value="Add to cart",
            form_id="addtocartform",
            form_action="https://www.motosport.com/cart/add",
            data_tracking_category="oem_AddToCartButton",
            data_tracking_label="PRODUCT-SPECIFIC-SKU",
        ),
        row=row,
        product_region="KAWASAKI DISC,RR 41080-1514 See Price in Cart",
    )

    assert candidate["confidence"] == "high"
    assert "oem_add_to_cart_tracking_fallback" in candidate["reasons"]
    assert candidate["data_tracking_label"] == "PRODUCT-SPECIFIC-SKU"
    assert "PRODUCT-SPECIFIC-SKU" not in candidate["reasons"]


def test_cart_action_must_be_visible_enabled_and_associated_with_form() -> None:
    row = probe_cart_price.CartProbeInputRow("Kawasaki", "41080-1514", "DISC,RR", "https://example.test/41080-1514", "282.32", prior_probe_note="see_price_in_cart")
    product_region = "KAWASAKI DISC,RR 41080-1514 See Price in Cart"

    hidden = probe_cart_price.score_cart_action_candidate(
        _control(1, "input", "Add to cart", id="addtocartbutton", visible=False, form_id="addtocartform", form_action="https://www.motosport.com/cart/add"),
        row=row,
        product_region=product_region,
    )
    disabled = probe_cart_price.score_cart_action_candidate(
        _control(2, "input", "Add to cart", id="addtocartbutton", enabled=False, disabled=True, form_id="addtocartform", form_action="https://www.motosport.com/cart/add"),
        row=row,
        product_region=product_region,
    )
    unassociated = probe_cart_price.score_cart_action_candidate(
        _control(3, "input", "Add to cart", id="unrelated-add"),
        row=row,
        product_region="",
    )

    assert hidden["confidence"] == "low"
    assert disabled["confidence"] == "low"
    assert unassociated["confidence"] == "medium"
    assert probe_cart_price.select_high_confidence_cart_action([hidden, disabled, unassociated])["status"] == "not_found"


def test_addtocartform_validation_accepts_only_product_cart_add_form() -> None:
    row = probe_cart_price.CartProbeInputRow("Kawasaki", "41080-1514", "DISC,RR", "https://example.test/41080-1514", "282.32", prior_probe_note="see_price_in_cart")
    observation = SimpleNamespace(raw_evidence_summary={"product_association": {"confirmed": True}, "selected_product_region": "KAWASAKI DISC,RR 41080-1514 See Price in Cart"})
    candidate = _control(
        1,
        "input",
        "Add to cart",
        id="addtocartbutton",
        stable_selector="#addtocartbutton",
        form_id="addtocartform",
        form_action="https://www.motosport.com/cart/add",
        form_method="post",
    )
    candidate.update({"confidence": "high", "rejected": False})

    valid = probe_cart_price.validate_cart_action_form(_FormValidationPage(), candidate, row=row, observation=observation)
    missing = probe_cart_price.validate_cart_action_form(_FormValidationPage(exists=False), candidate, row=row, observation=observation)
    invalid_action = probe_cart_price.validate_cart_action_form(_FormValidationPage(action="https://www.motosport.com/checkout"), candidate, row=row, observation=observation)
    invalid_quantity = probe_cart_price.validate_cart_action_form(_FormValidationPage(quantity="2"), candidate, row=row, observation=observation)

    assert valid["valid"] is True
    assert missing["valid"] is False
    assert "form_exists" in missing["warnings"]
    assert invalid_action["valid"] is False
    assert "form_action_cart_add" in invalid_action["warnings"]
    assert invalid_quantity["valid"] is False
    assert "quantity_one_or_blank" in invalid_quantity["warnings"]


def test_chaparral_orderform_validation_accepts_product_cart_form() -> None:
    row = probe_cart_price.CartProbeInputRow("Can-Am", "420956744", "Can-Am FILTER OIL", "https://www.chapmoto.com/filter-oil-420956744.html", "18.49", prior_probe_note="chaparral_add_to_view_price")
    observation = SimpleNamespace(raw_evidence_summary={"matched_part_number": "420956744", "region_text": "OEM Part Number: 420956744\nPart: Can-Am FILTER OIL"})
    candidate = _control(
        1,
        "input",
        "Add to Cart",
        id="add-to-cart",
        stable_selector="#add-to-cart",
        form_id="orderform",
        form_action="https://www.chapmoto.com/cart.php",
        form_method="post",
    )
    candidate.update({"confidence": "high", "rejected": False})

    valid = probe_cart_price.validate_cart_action_form(_ChaparralFormValidationPage(), candidate, row=row, observation=observation)
    invalid_action = probe_cart_price.validate_cart_action_form(_ChaparralFormValidationPage(action="https://www.chapmoto.com/checkout"), candidate, row=row, observation=observation)
    invalid_quantity = probe_cart_price.validate_cart_action_form(_ChaparralFormValidationPage(quantity="2"), candidate, row=row, observation=observation)

    assert valid["valid"] is True
    assert valid["checks"]["product_association_confirmed"] is True
    assert invalid_action["valid"] is False
    assert "form_action_cart_add" in invalid_action["warnings"]
    assert invalid_quantity["valid"] is False
    assert "quantity_one_or_blank" in invalid_quantity["warnings"]


def test_cart_line_evidence_requires_product_match_quantity_one_and_supports_data_sku() -> None:
    row = probe_cart_price.CartProbeInputRow("Kawasaki", "41080-1514", "DISC,RR", "https://example.test/41080-1514", "282.32", prior_probe_note="see_price_in_cart")
    chaparral_row = probe_cart_price.CartProbeInputRow("Can-Am", "420956744", "FILTER OIL", "https://www.chapmoto.com/filter-oil-420956744.html", "18.49", prior_probe_note="chaparral_add_to_view_price")

    unrelated = probe_cart_price.cart_line_evidence("Cart\nUnrelated Product\nQty: 1\n$282.32", row)
    matched = probe_cart_price.cart_line_evidence("Cart\nDISC,RR\n41080-1514\nQty: 1\n$282.32\nSubtotal $282.32", row)
    motosport_cart = probe_cart_price.cart_line_evidence(
        "Your Cart\nKawasaki OEM Parts DISC,RR\n41080-1514\nCall for availability\nQuantity\nUpdate\n"
        "Save For Later\nRemove\nCurrent Price:\n$259.73\nOriginal Price:\n$282.32\n$259.73",
        row,
    )
    quantity_two = probe_cart_price.cart_line_evidence(
        "Cart\nDISC,RR\n41080-1514\nQty: 2\n$282.32\nSubtotal $564.64",
        row,
        supporting_sku="KAW186Z-X001-Y001",
        cart_line_records=[
            {
                "text": "DISC,RR 41080-1514 $282.32",
                "data_sku": "KAW186Z-X001-Y001",
                "data_quantity": "2",
                "remove_selector": 'a.cart-remove-item[title="Remove item from cart."][data-sku="KAW186Z-X001-Y001"]',
                "remove_href_present": True,
            }
        ],
    )
    chaparral_placeholder = probe_cart_price.cart_line_evidence(
        "Your Cart\nFILTER OIL\n420956744\n$9,999.99 x 1 = $9,999.99\tX\nItems (1): $9,999.99",
        chaparral_row,
        cart_line_records=[
            {
                "text": "FILTER OIL\n420956744\n$9,999.99 x 1 = $9,999.99\tX",
                "data_sku": "",
                "data_quantity": "1",
                "remove_selector": "tr:has(input#amount_1) span.cursor:has-text(\"X\")",
                "remove_href_present": False,
            }
        ],
    )

    assert unrelated["confirmed"] is False
    assert unrelated["accepted_price"] is None
    assert matched["confirmed"] is True
    assert matched["accepted_price"] == Decimal("282.32")
    assert matched["reason_accepted"] == "matched_cart_line_product_price"
    assert motosport_cart["confirmed"] is True
    assert motosport_cart["accepted_price"] == Decimal("259.73")
    assert motosport_cart["rejected_price_candidates"] == []
    assert quantity_two["confirmed"] is True
    assert quantity_two["quantity"] == 2
    assert quantity_two["accepted_price"] is None
    assert quantity_two["matching_evidence"]["data_sku"] is True
    assert quantity_two["remove_href_present"] is True
    assert quantity_two["remove_href_used"] is False
    assert chaparral_placeholder["confirmed"] is True
    assert chaparral_placeholder["quantity"] == 1
    assert chaparral_placeholder["accepted_price"] is None
    assert chaparral_placeholder["rejected_placeholder_price_candidates"] == [Decimal("9999.99"), Decimal("9999.99")]
    assert chaparral_placeholder["remove_selector"] == "tr:has(input#amount_1) span.cursor:has-text(\"X\")"


def test_remove_link_is_scoped_to_matching_cart_line_and_href_is_not_called() -> None:
    page = _SelectorPage(available={'a.cart-remove-item[title="Remove item from cart."][data-sku="KAW186Z-X001-Y001"]'})
    line_evidence = {
        "confirmed": True,
        "remove_selector": 'a.cart-remove-item[title="Remove item from cart."][data-sku="KAW186Z-X001-Y001"]',
        "remove_href_present": True,
        "remove_href_used": False,
    }

    assert probe_cart_price.remove_cart_item(page, line_evidence=line_evidence) is True
    assert page.clicked == ['a.cart-remove-item[title="Remove item from cart."][data-sku="KAW186Z-X001-Y001"]']
    assert not any("cartAction=" in selector for selector in page.requested_selectors)
    assert probe_cart_price.remove_cart_item(_SelectorPage(available={"button:has-text('Remove')"}), line_evidence={"confirmed": False}) is False

    fallback_text = "Your Cart\nKawasaki OEM Parts DISC,RR\n41080-1514\nQuantity\nRemove\nCurrent Price:\n$259.73"
    fallback_page = _SelectorPage(available={"a:has-text('Remove')"}, body_text=fallback_text)
    fallback_evidence = {"confirmed": True, "raw_cart_line_text": "Kawasaki OEM Parts DISC,RR 41080-1514 Quantity Remove Current Price: $259.73"}
    assert probe_cart_price.remove_cart_item(fallback_page, line_evidence=fallback_evidence) is True
    assert fallback_page.clicked == ["a:has-text('Remove')"]

    ambiguous_page = _SelectorPage(available={"a:has-text('Remove')"}, body_text=fallback_text + "\nRemove")
    assert probe_cart_price.remove_cart_item(ambiguous_page, line_evidence=fallback_evidence) is False

    chaparral_page = _SelectorPage(available={"tr:has(input#amount_1) span.cursor:has-text(\"X\")"})
    chaparral_evidence = {"confirmed": True, "remove_selector": "tr:has(input#amount_1) span.cursor:has-text(\"X\")"}
    assert probe_cart_price.remove_cart_item(chaparral_page, line_evidence=chaparral_evidence) is True
    assert chaparral_page.clicked == ["tr:has(input#amount_1) span.cursor:has-text(\"X\")"]


def test_bounded_cart_action_inventory_rescans_after_hydration(monkeypatch) -> None:
    row = probe_cart_price.CartProbeInputRow("Kawasaki", "41080-1514", "DISC,RR", "https://example.test/41080-1514", "282.32", prior_probe_note="see_price_in_cart")
    observation = SimpleNamespace(raw_evidence_summary={"selected_product_region": "KAWASAKI DISC 41080-1514 See Price in Cart"})
    page = _WaitPage()
    calls = []

    def fake_collect(_page, _row, _observation):
        calls.append(len(calls) + 1)
        if len(calls) == 1:
            return {"candidates": [], "candidate_count": 0, "high_confidence_count": 0}
        return {"candidates": [{"confidence": "high", "rejected": False}], "candidate_count": 1, "high_confidence_count": 1}

    monkeypatch.setattr(probe_cart_price, "collect_cart_action_inventory", fake_collect)

    inventory = probe_cart_price.bounded_cart_action_inventory(page, row, observation)

    assert calls == [1, 2]
    assert page.waits == [probe_cart_price.CART_ACTION_RESCAN_DELAY_MS]
    assert inventory["scan_count"] == 2


def test_cart_probe_context_creates_one_run_folder_and_one_part_folder_per_product() -> None:
    row = probe_cart_price.CartProbeInputRow("Kawasaki", "41080-1514", "DISC,RR", "https://example.test/41080-1514", "282.32", prior_probe_note="see_price_in_cart")
    base_dir = TEST_OUTPUT_DIR / f"cart-context-one-run-{uuid.uuid4().hex}"

    context = probe_cart_price.CartProbeRunContext.create(base_data_dir=base_dir, attempted_parts=1)
    run = probe_cart_price.run_cart_probe_structure_dry_run([row], context)

    run_base = base_dir / "output" / "competitor_probes" / "motosport_cart"
    assert len([path for path in run_base.iterdir() if path.is_dir()]) == 1
    assert len([path for path in context.run_output_dir.iterdir() if path.is_dir()]) == 1
    assert context.directories_created_count == 2
    assert run.rows[0].result_type == "dry_run_structure"


def test_cart_probe_startup_validation_failures_create_no_timestamp_folder(monkeypatch) -> None:
    base_dir = TEST_OUTPUT_DIR / f"cart-no-folder-validation-{uuid.uuid4().hex}"
    valid_input = TEST_OUTPUT_DIR / f"valid-cart-hidden-{uuid.uuid4().hex}.csv"
    valid_input.write_text(
        "manufacturer,oem_part_number,product_name,product_url,reference_price,prior_probe_note\n"
        "Kawasaki,41080-1514,\"DISC,RR\",https://example.test/41080-1514,282.32,see_price_in_cart\n",
        encoding="utf-8",
    )
    run_base = base_dir / "output" / "competitor_probes" / "motosport_cart"

    monkeypatch.setattr(probe_cart_price, "DATA_DIR", base_dir)
    monkeypatch.setattr(probe_cart_price.sys, "argv", ["probe_cart_price.py", "--competitor", "bad", "--file", str(valid_input), "--experimental-cart-pricing"])
    assert probe_cart_price.main() == 1
    assert not run_base.exists()

    monkeypatch.setattr(probe_cart_price.sys, "argv", ["probe_cart_price.py", "--competitor", "motosport", "--file", str(TEST_OUTPUT_DIR / "missing.csv"), "--experimental-cart-pricing"])
    assert probe_cart_price.main() == 1
    assert not run_base.exists()

    monkeypatch.setattr(probe_cart_price.sys, "argv", ["probe_cart_price.py", "--competitor", "motosport", "--file", str(valid_input)])
    assert probe_cart_price.main() == 1
    assert not run_base.exists()

    monkeypatch.setattr(probe_cart_price.sys, "argv", ["probe_cart_price.py", "--competitor", "motosport", "--file", str(valid_input), "--experimental-cart-pricing"])
    monkeypatch.setattr("builtins.input", lambda _prompt: "NO")
    assert probe_cart_price.main() == 1
    assert not run_base.exists()


def test_cart_probe_run_folder_gets_initialized_metadata_immediately() -> None:
    base_dir = TEST_OUTPUT_DIR / f"cart-initialized-metadata-{uuid.uuid4().hex}"
    context = probe_cart_price.CartProbeRunContext.create(
        base_data_dir=base_dir,
        attempted_parts=1,
        input_file=Path("cart.csv"),
        requested_max_parts=1,
        mode="diagnostic_only",
    )

    metadata_path = context.run_output_dir / "cart_probe_metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    assert metadata["initialized"] is True
    assert metadata["status"] == "initialized"
    assert metadata["mode"] == "diagnostic_only"
    assert metadata["attempted_parts"] == 0


def test_cart_probe_startup_abort_metadata_marks_folder_aborted() -> None:
    context = probe_cart_price.CartProbeRunContext.create(
        base_data_dir=TEST_OUTPUT_DIR / f"cart-startup-abort-{uuid.uuid4().hex}",
        attempted_parts=1,
        input_file=Path("cart.csv"),
        requested_max_parts=1,
        mode="diagnostic_only",
    )

    probe_cart_price.write_startup_abort_metadata(context, input_file=Path("cart.csv"), requested_max_parts=1, mode="diagnostic_only", stop_reason="invalid_input")

    metadata = json.loads((context.run_output_dir / "cart_probe_metadata.json").read_text(encoding="utf-8"))
    assert metadata["status"] == "aborted"
    assert metadata["stop_reason"] == "invalid_input"
    assert metadata["attempted_parts"] == 0
    assert metadata["production_current_state_written"] is False


def test_cart_probe_output_directory_count_guard_prevents_runaway_folders() -> None:
    row = probe_cart_price.CartProbeInputRow("Kawasaki", "41080-1514", "DISC,RR", "https://example.test/41080-1514", "282.32", prior_probe_note="see_price_in_cart")
    context = probe_cart_price.CartProbeRunContext.create(base_data_dir=TEST_OUTPUT_DIR / "cart-context-guard", attempted_parts=1)

    context.begin_part(row, 1)
    with pytest.raises(RuntimeError):
        context.product_output_dir(row, 2)

    assert context.stop_requested is True
    assert "output_directory_limit_exceeded" in (context.stop_reason or "")
    assert context.directories_created_count == 2


def test_candidate_scan_cleanup_click_and_empty_cart_limits(monkeypatch) -> None:
    row = probe_cart_price.CartProbeInputRow("Kawasaki", "41080-1514", "DISC,RR", "https://example.test/41080-1514", "282.32", prior_probe_note="see_price_in_cart")
    observation = SimpleNamespace(raw_evidence_summary={"selected_product_region": "KAWASAKI DISC 41080-1514 See Price in Cart"})
    page = _WaitPage()
    context = probe_cart_price.CartProbeRunContext.create(base_data_dir=TEST_OUTPUT_DIR / "cart-context-limits", attempted_parts=1)
    context.begin_part(row, 1)

    monkeypatch.setattr(probe_cart_price, "collect_cart_action_inventory", lambda *_args: {"candidates": [], "candidate_count": 0, "high_confidence_count": 0})
    inventory = probe_cart_price.bounded_cart_action_inventory(page, row, observation, context=context)

    assert inventory["scan_count"] == 3
    assert context.candidate_scan_count == 3
    assert page.waits == [probe_cart_price.CART_ACTION_RESCAN_DELAY_MS, probe_cart_price.CART_ACTION_RESCAN_DELAY_MS]
    assert context.allow_cart_action_attempt() is True
    assert context.allow_cart_action_attempt() is False
    assert "max_cart_action_attempts_exceeded" in (context.stop_reason or "")

    cleanup_context = probe_cart_price.CartProbeRunContext.create(base_data_dir=TEST_OUTPUT_DIR / "cart-context-cleanup", attempted_parts=1)
    assert cleanup_context.allow_cleanup_attempt() is True
    assert cleanup_context.allow_cleanup_attempt() is False
    assert cleanup_context.allow_empty_cart_check() is True
    assert cleanup_context.allow_empty_cart_check() is True
    assert cleanup_context.allow_empty_cart_check() is False


def test_run_and_part_timeout_guards_stop_safely() -> None:
    run_context = probe_cart_price.CartProbeRunContext.create(
        base_data_dir=TEST_OUTPUT_DIR / "cart-context-run-timeout",
        attempted_parts=1,
        limits=probe_cart_price.CartProbeLimits(max_run_seconds=-1),
    )
    assert run_context.guard_run_time() is False
    assert "max_run_seconds_exceeded" in (run_context.stop_reason or "")

    row = probe_cart_price.CartProbeInputRow("Kawasaki", "41080-1514", "DISC,RR", "https://example.test/41080-1514", "282.32", prior_probe_note="see_price_in_cart")
    part_context = probe_cart_price.CartProbeRunContext.create(
        base_data_dir=TEST_OUTPUT_DIR / "cart-context-part-timeout",
        attempted_parts=1,
        limits=probe_cart_price.CartProbeLimits(max_part_seconds=-1),
    )
    part_context.begin_part(row, 1)
    assert part_context.guard_part_time() is False
    assert "max_part_seconds_exceeded" in (part_context.stop_reason or "")


def test_cart_action_diagnostics_files_are_safe_and_complete() -> None:
    row = probe_cart_price.CartProbeInputRow("Kawasaki", "41080-1514", "DISC,RR", "https://example.test/41080-1514", "282.32", prior_probe_note="see_price_in_cart")
    observation = SimpleNamespace(
        raw_evidence_summary={
            "product_association": {"confirmed": True},
            "selected_product_region": "KAWASAKI DISC 41080-1514 See Price in Cart",
        },
        page_classification="normal_product",
        price_visibility="see_price_in_cart",
    )
    inventory = {
        "scan_count": 1,
        "candidate_count": 1,
        "high_confidence_count": 1,
        "candidates": [
            probe_cart_price.score_cart_action_candidate(
                _control(1, "button", "See Price in Cart", data_testid="add-to-cart"),
                row=row,
                product_region="KAWASAKI DISC 41080-1514 See Price in Cart",
            )
        ],
        "visible_buttons": [],
        "visible_links": [],
        "product_panel_forms": [
            {
                "id": "footer-form",
                "controls": [
                    {"name": "_csrf_token", "type": "hidden", "value": "SECRET-CSRF-VALUE", "text": "SECRET-CSRF-VALUE"}
                ],
            }
        ],
    }
    part_dir = TEST_OUTPUT_DIR / "cart-action-diagnostics" / "41080-1514"

    probe_cart_price.save_cart_action_diagnostics(part_dir, row, observation, inventory)

    expected = {
        "cart_action_diagnostics.json",
        "cart_action_candidates.txt",
        "selected_product_region.txt",
        "visible_buttons.txt",
        "visible_links.txt",
        "product_panel_forms.txt",
        "cart_probe_page_classification.txt",
    }
    assert expected == {path.name for path in part_dir.iterdir()}
    combined = "\n".join(path.read_text(encoding="utf-8") for path in part_dir.iterdir())
    assert "See Price in Cart" in combined
    assert "cookie" not in combined.lower()
    assert "token" not in combined.lower()
    assert "SECRET-CSRF-VALUE" not in combined
    assert "[redacted]" in combined
    assert "<html" not in combined.lower()


def test_diagnostic_helper_does_not_create_timestamp_or_extra_part_folders() -> None:
    row = probe_cart_price.CartProbeInputRow("Kawasaki", "41080-1514", "DISC,RR", "https://example.test/41080-1514", "282.32", prior_probe_note="see_price_in_cart")
    context = probe_cart_price.CartProbeRunContext.create(base_data_dir=TEST_OUTPUT_DIR / "cart-context-pure-writer", attempted_parts=1)
    product_dir = context.begin_part(row, 1)
    observation = SimpleNamespace(
        raw_evidence_summary={"product_association": {"confirmed": True}, "selected_product_region": "KAWASAKI DISC 41080-1514 See Price in Cart"},
        page_classification="normal_product",
        price_visibility="see_price_in_cart",
    )

    probe_cart_price.save_cart_action_diagnostics(product_dir, row, observation, {"candidates": [], "candidate_count": 0, "high_confidence_count": 0})

    assert len([path for path in context.run_output_dir.iterdir() if path.is_dir()]) == 1
    assert not (product_dir / "41080-1514").exists()


def test_diagnose_cart_action_only_skips_confirmation_and_real_probe(monkeypatch) -> None:
    input_path = TEST_OUTPUT_DIR / "diagnose-cart-hidden-input.csv"
    input_path.write_text(
        "manufacturer,oem_part_number,product_name,product_url,reference_price,prior_probe_note\n"
        "Kawasaki,41080-1514,\"DISC,RR\",https://example.test/41080-1514,282.32,see_price_in_cart\n",
        encoding="utf-8",
    )
    captured = {}

    def fake_run(rows, *, context, headless):
        captured["rows"] = rows
        captured["output_dir"] = context.run_output_dir
        captured["headless"] = headless
        return probe_cart_price.CartProbeRun("motosport", "2026-07-11T00:00:00Z", completed_at="2026-07-11T00:01:00Z")

    monkeypatch.setattr(probe_cart_price.sys, "argv", ["probe_cart_price.py", "--competitor", "motosport", "--file", str(input_path), "--experimental-cart-pricing", "--diagnose-cart-action-only"])
    monkeypatch.setattr(probe_cart_price, "DATA_DIR", TEST_OUTPUT_DIR / "diagnose-main")
    monkeypatch.setattr(probe_cart_price, "run_cart_action_diagnostics", fake_run)
    monkeypatch.setattr(probe_cart_price, "run_cart_probe", lambda *args, **kwargs: pytest.fail("real cart probe should not run"))
    monkeypatch.setattr("builtins.input", lambda _prompt: pytest.fail("diagnostic mode should not ask for confirmation"))

    assert probe_cart_price.main() == 0
    assert captured["rows"][0].oem_part_number == "41080-1514"
    assert captured["headless"] is False


def test_dry_run_structure_mode_creates_one_run_and_part_folder_without_browser(monkeypatch) -> None:
    input_path = TEST_OUTPUT_DIR / "dry-run-cart-hidden-input.csv"
    input_path.write_text(
        "manufacturer,oem_part_number,product_name,product_url,reference_price,prior_probe_note\n"
        "Kawasaki,41080-1514,\"DISC,RR\",https://example.test/41080-1514,282.32,see_price_in_cart\n",
        encoding="utf-8",
    )
    base_dir = TEST_OUTPUT_DIR / f"dry-run-main-{uuid.uuid4().hex}"

    monkeypatch.setattr(probe_cart_price.sys, "argv", ["probe_cart_price.py", "--competitor", "motosport", "--file", str(input_path), "--max-parts", "1", "--experimental-cart-pricing", "--dry-run-structure"])
    monkeypatch.setattr(probe_cart_price, "DATA_DIR", base_dir)
    monkeypatch.setattr(probe_cart_price, "sync_playwright", lambda: pytest.fail("dry-run structure should not open a browser"))

    assert probe_cart_price.main() == 0
    run_base = base_dir / "output" / "competitor_probes" / "motosport_cart"
    run_dirs = [path for path in run_base.iterdir() if path.is_dir()]
    assert len(run_dirs) == 1
    assert len([path for path in run_dirs[0].iterdir() if path.is_dir()]) == 1
    metadata = json.loads((run_dirs[0] / "cart_probe_metadata.json").read_text(encoding="utf-8"))
    assert metadata["dry_run_structure"] is True
    assert metadata["directories_created_count"] == 2
    audit = json.loads((run_dirs[0] / "folder_audit.json").read_text(encoding="utf-8"))
    assert audit["expected_directories"] == 2
    assert audit["actual_directories_created"] == 2
    assert audit["folder_guard_passed"] is True


def test_folder_audit_fails_when_unexpected_directories_exist() -> None:
    row = probe_cart_price.CartProbeInputRow("Kawasaki", "41080-1514", "DISC,RR", "https://example.test/41080-1514", "282.32", prior_probe_note="see_price_in_cart")
    context = probe_cart_price.CartProbeRunContext.create(base_data_dir=TEST_OUTPUT_DIR / f"cart-audit-fail-{uuid.uuid4().hex}", attempted_parts=1)
    context.begin_part(row, 1)
    (context.run_output_dir / "unexpected-extra-folder").mkdir()
    run = probe_cart_price.CartProbeRun("motosport", context.started_at, completed_at=utc_now())
    run.rows.append(probe_cart_price.CartProbeResult(1, row, utc_now(), result_type="dry_run_structure"))

    probe_cart_price.write_outputs(context.run_output_dir, run, argparse.Namespace(file=Path("cart.csv"), max_parts=1, dry_run_structure=True, diagnose_cart_action_only=False), context=context)

    audit = json.loads((context.run_output_dir / "folder_audit.json").read_text(encoding="utf-8"))
    review = (context.run_output_dir / "cart_probe_review.txt").read_text(encoding="utf-8")
    assert audit["folder_guard_passed"] is False
    assert audit["warning"] == "unexpected_output_directories_created"
    assert run.stop_reason == "output_directory_guard_failed"
    assert "Folder guard: Failed" in review


def test_diagnostic_only_review_makes_no_click_and_folder_guard_clear() -> None:
    row = probe_cart_price.CartProbeInputRow("Kawasaki", "41080-1514", "DISC,RR", "https://example.test/41080-1514", "282.32", prior_probe_note="see_price_in_cart")
    context = probe_cart_price.CartProbeRunContext.create(base_data_dir=TEST_OUTPUT_DIR / f"cart-review-diagnostic-{uuid.uuid4().hex}", attempted_parts=1)
    context.begin_part(row, 1)
    run = probe_cart_price.CartProbeRun("motosport", context.started_at, completed_at=utc_now())
    run.rows.append(probe_cart_price.CartProbeResult(1, row, utc_now(), result_type="cart_action_diagnostics"))

    probe_cart_price.write_outputs(context.run_output_dir, run, argparse.Namespace(file=Path("cart.csv"), max_parts=1, dry_run_structure=False, diagnose_cart_action_only=True), context=context)

    review = (context.run_output_dir / "cart_probe_review.txt").read_text(encoding="utf-8")
    assert "Mode: diagnostic_only" in review
    assert "Cart action clicked: No" in review
    assert "Cart modified: No" in review
    assert "Expected folders: 2" in review
    assert "Actual folders created: 2" in review
    assert "Folder guard: Passed" in review


def test_inspect_cart_probe_outputs_metadata_reasons_and_cleanup_safety() -> None:
    base_dir = TEST_OUTPUT_DIR / f"inspect-cart-outputs-{uuid.uuid4().hex}"
    empty = base_dir / "20260711T000000Z"
    diagnostic = base_dir / "20260711T000001Z"
    exceeded = base_dir / "20260711T000002Z"
    evidence_only = base_dir / "20260711T000003Z"
    incomplete = base_dir / "20260711T000004Z"
    empty.mkdir(parents=True, exist_ok=True)
    diagnostic.mkdir(parents=True, exist_ok=True)
    exceeded.mkdir(parents=True, exist_ok=True)
    evidence_only.mkdir(parents=True, exist_ok=True)
    incomplete.mkdir(parents=True, exist_ok=True)
    (diagnostic / "41080-1514").mkdir()
    (diagnostic / "cart_probe_metadata.json").write_text(
        json.dumps(
            {
                "diagnose_cart_action_only": True,
                "dry_run_structure": False,
                "requested_max_parts": 1,
                "attempted_parts": 1,
                "directories_created_count": 2,
                "max_total_output_directories_created": 2,
                "folder_audit": {"folder_guard_passed": True},
            }
        ),
        encoding="utf-8",
    )
    (exceeded / "41080-1514").mkdir()
    (exceeded / "extra").mkdir()
    (exceeded / "cart_probe_metadata.json").write_text(
        json.dumps(
            {
                "diagnose_cart_action_only": True,
                "requested_max_parts": 1,
                "attempted_parts": 1,
                "directories_created_count": 3,
                "max_total_output_directories_created": 2,
                "folder_audit": {"folder_guard_passed": False},
            }
        ),
        encoding="utf-8",
    )
    (evidence_only / "cart_action_used.json").write_text("{}", encoding="utf-8")
    (incomplete / "cart_probe_metadata.json").write_text('{"mode": "real_cart_probe", "status": "initialized"}', encoding="utf-8")

    inspections = inspect_cart_probe_outputs.inspect_output_folders(base_dir)
    by_path = {Path(item.folder_path).name: item for item in inspections}

    assert by_path["20260711T000000Z"].possible_loop_artifact is True
    assert by_path["20260711T000000Z"].likely_reason == "empty_folder_no_metadata"
    assert by_path["20260711T000001Z"].possible_loop_artifact is False
    assert by_path["20260711T000001Z"].likely_reason == "valid_diagnostic_run"
    assert by_path["20260711T000001Z"].diagnose_cart_action_only is True
    assert by_path["20260711T000001Z"].directories_created_count == 2
    assert by_path["20260711T000002Z"].possible_loop_artifact is True
    assert by_path["20260711T000002Z"].likely_reason == "exceeds_directory_limit"
    assert by_path["20260711T000003Z"].protected_files_present is True
    assert by_path["20260711T000004Z"].possible_loop_artifact is True
    assert by_path["20260711T000004Z"].likely_reason == "incomplete_initialized_run"
    assert inspect_cart_probe_outputs.delete_empty_loop_artifacts(inspections) == []
    assert inspect_cart_probe_outputs.delete_empty_loop_artifacts(inspections, confirmation=inspect_cart_probe_outputs.DELETE_CONFIRMATION_TEXT, dry_run=True) == []
    deleted = inspect_cart_probe_outputs.delete_empty_loop_artifacts(inspections, confirmation=inspect_cart_probe_outputs.DELETE_CONFIRMATION_TEXT)
    assert str(empty) in deleted
    assert not empty.exists()
    assert diagnostic.exists()
    assert exceeded.exists()
    assert evidence_only.exists()


def test_inspector_is_read_only_and_creates_no_folders() -> None:
    base_dir = TEST_OUTPUT_DIR / f"inspect-read-only-{uuid.uuid4().hex}" / "motosport_cart"

    inspections = inspect_cart_probe_outputs.inspect_output_folders(base_dir)

    assert inspections == []
    assert not base_dir.exists()


def test_real_cart_click_is_blocked_when_latest_folder_audit_failed(monkeypatch) -> None:
    input_path = TEST_OUTPUT_DIR / f"blocked-real-cart-input-{uuid.uuid4().hex}.csv"
    input_path.write_text(
        "manufacturer,oem_part_number,product_name,product_url,reference_price,prior_probe_note\n"
        "Kawasaki,41080-1514,\"DISC,RR\",https://example.test/41080-1514,282.32,see_price_in_cart\n",
        encoding="utf-8",
    )
    base_dir = TEST_OUTPUT_DIR / f"blocked-real-cart-{uuid.uuid4().hex}"
    failed = base_dir / "output" / "competitor_probes" / "motosport_cart" / "20260711T000000Z"
    failed.mkdir(parents=True)
    (failed / "folder_audit.json").write_text('{"folder_guard_passed": false}', encoding="utf-8")
    monkeypatch.setattr(probe_cart_price.sys, "argv", ["probe_cart_price.py", "--competitor", "motosport", "--file", str(input_path), "--max-parts", "1", "--experimental-cart-pricing"])
    monkeypatch.setattr(probe_cart_price, "DATA_DIR", base_dir)
    monkeypatch.setattr("builtins.input", lambda _prompt: pytest.fail("real cart click should be blocked before confirmation"))
    monkeypatch.setattr(probe_cart_price, "run_cart_probe", lambda *args, **kwargs: pytest.fail("real cart probe should not run"))

    assert probe_cart_price.main() == 1
    assert len([path for path in failed.parent.iterdir() if path.is_dir()]) == 1


def test_real_cart_click_is_blocked_when_latest_run_is_incomplete(monkeypatch) -> None:
    input_path = TEST_OUTPUT_DIR / f"blocked-incomplete-cart-input-{uuid.uuid4().hex}.csv"
    input_path.write_text(
        "manufacturer,oem_part_number,product_name,product_url,reference_price,prior_probe_note\n"
        "Kawasaki,41080-1514,\"DISC,RR\",https://example.test/41080-1514,282.32,see_price_in_cart\n",
        encoding="utf-8",
    )
    base_dir = TEST_OUTPUT_DIR / f"blocked-incomplete-cart-{uuid.uuid4().hex}"
    incomplete = base_dir / "output" / "competitor_probes" / "motosport_cart" / "20260712T011824Z"
    incomplete.mkdir(parents=True)
    (incomplete / "cart_probe_metadata.json").write_text('{"mode": "real_cart_probe", "status": "initialized"}', encoding="utf-8")
    monkeypatch.setattr(probe_cart_price.sys, "argv", ["probe_cart_price.py", "--competitor", "motosport", "--file", str(input_path), "--max-parts", "1", "--experimental-cart-pricing"])
    monkeypatch.setattr(probe_cart_price, "DATA_DIR", base_dir)
    monkeypatch.setattr("builtins.input", lambda _prompt: pytest.fail("incomplete latest run should block before confirmation"))

    assert probe_cart_price.main() == 1
    assert len([path for path in incomplete.parent.iterdir() if path.is_dir()]) == 1


def test_real_cart_probe_interrupt_writes_final_outputs(monkeypatch) -> None:
    input_path = TEST_OUTPUT_DIR / f"interrupt-cart-input-{uuid.uuid4().hex}.csv"
    input_path.write_text(
        "manufacturer,oem_part_number,product_name,product_url,reference_price,prior_probe_note\n"
        "Kawasaki,41080-1514,\"DISC,RR\",https://example.test/41080-1514,282.32,see_price_in_cart\n",
        encoding="utf-8",
    )
    base_dir = TEST_OUTPUT_DIR / f"interrupt-cart-{uuid.uuid4().hex}"

    def interrupting_run(*_args, **_kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(probe_cart_price.sys, "argv", ["probe_cart_price.py", "--competitor", "motosport", "--file", str(input_path), "--max-parts", "1", "--experimental-cart-pricing"])
    monkeypatch.setattr(probe_cart_price, "DATA_DIR", base_dir)
    monkeypatch.setattr("builtins.input", lambda _prompt: probe_cart_price.CONFIRMATION_TEXT)
    monkeypatch.setattr(probe_cart_price, "run_cart_probe", interrupting_run)

    assert probe_cart_price.main() == 1
    run_base = base_dir / "output" / "competitor_probes" / "motosport_cart"
    run_dirs = [path for path in run_base.iterdir() if path.is_dir()]
    assert len(run_dirs) == 1
    metadata = json.loads((run_dirs[0] / "cart_probe_metadata.json").read_text(encoding="utf-8"))
    assert metadata["status"] == "stopped"
    assert metadata["stop_reason"] == "interrupted_by_user"
    assert (run_dirs[0] / "cart_probe_review.txt").exists()
    assert (run_dirs[0] / "folder_audit.json").exists()


def test_cart_probe_progress_file_records_live_step() -> None:
    row = probe_cart_price.CartProbeInputRow("Kawasaki", "41080-1514", "DISC,RR", "https://example.test/41080-1514", "282.32", prior_probe_note="see_price_in_cart")
    product_dir = TEST_OUTPUT_DIR / f"progress-file-{uuid.uuid4().hex}" / "41080-1514"

    probe_cart_price.write_cart_probe_progress(product_dir, row, step="loading_product_page", status="started")

    progress = json.loads((product_dir / "cart_probe_progress.json").read_text(encoding="utf-8"))
    assert progress["step"] == "loading_product_page"
    assert progress["status"] == "started"
    assert progress["oem_part_number"] == "41080-1514"


def test_cart_probe_review_outputs_exclude_secrets_and_state_is_experimental() -> None:
    row = probe_cart_price.CartProbeInputRow("Kawasaki", "34028-0327", "STEP", "https://example.test/34028-0327", "37.30", "20260711T000000Z")
    run = probe_cart_price.CartProbeRun("motosport", "2026-07-11T00:00:00Z", completed_at="2026-07-11T00:01:00Z")
    run.rows.append(
        probe_cart_price.CartProbeResult(
            1,
            row,
            utc_now(),
            product_association_confirmed=True,
            reference_price=Decimal("37.30"),
            cart_selling_price=Decimal("32.69"),
            quantity=1,
            line_subtotal=Decimal("32.69"),
            cart_price_confidence="medium",
            cleanup_status="success",
            result_type="cart_price_found",
        )
    )
    output_dir = TEST_OUTPUT_DIR / "cart-probe-output"
    output_dir.mkdir(parents=True, exist_ok=True)
    probe_cart_price.write_outputs(output_dir, run, argparse.Namespace(file=Path("cart.csv"), max_parts=1))
    combined = (output_dir / "cart_probe_review.txt").read_text(encoding="utf-8") + (output_dir / "cart_probe_metadata.json").read_text(encoding="utf-8")

    assert "Cart price results are experimental" in combined
    assert '"production_current_state_written": false' in combined
    assert "cookie" not in combined.lower()
    assert "token" not in combined.lower()
    assert "password" not in combined.lower()


def test_motosport_cart_probe_status_is_production_enabled() -> None:
    db = _empty_db("cart_probe_status.db")
    with connect_database(db) as conn:
        row = conn.execute("SELECT status, cart_price_probe_status FROM competitors WHERE competitor_code='motosport'").fetchone()

    assert row["status"] == "active"
    assert row["cart_price_probe_status"] == "production_enabled"
    page = TestClient(create_app(db), raise_server_exceptions=False).get("/quality")
    assert "Production Enabled" not in page.text
    assert "Competitor Review Status" not in page.text


def test_dashboard_competitor_selection_and_warning_render() -> None:
    db = _empty_db("dashboard_competitors.db")
    result = _upload_simple_batch(db, "dashboard-competitors.xlsx", "SKU-C", "Honda", "18327-MEN-A30")
    client = TestClient(create_app(db), raise_server_exceptions=False)

    page = client.get(f"/imports?import_batch_id={result.import_batch_id}")

    assert "Partzilla" in page.text
    assert "MotoSport" in page.text
    assert "Choose competitors" in page.text
    assert "Only supported OEM and competitor combinations will be checked." in page.text


def test_future_api_source_placeholder_and_file_sources() -> None:
    excel = ExcelInternalProductSource(Path("parts.xlsx"))
    csv_source = CsvInternalProductSource(Path("parts.csv"))
    api = ApiInternalProductSource(ApiSourceConfig(base_url="https://internal.example", auth_type="bearer", token_env_var_name="INTERNAL_API_TOKEN"))

    assert isinstance(excel, InternalProductSource)
    assert isinstance(csv_source, InternalProductSource)
    with pytest.raises(NotImplementedError):
        api.fetch_products()
    assert "real-token-value" not in json.dumps(api.__dict__, default=str)


def test_motosport_probe_price_can_render_as_fallback_comparison_column() -> None:
    db = _empty_db("comparison_probe_column.db")
    result = _upload_simple_batch(db, "comparison-probe.xlsx", "SKU-PROBE", "Honda", "18327-MEN-A30")
    confirm_import(db, result.import_batch_id)
    with connect_database(db) as conn:
        conn.execute(
            """
            INSERT INTO competitor_probe_results(competitor_key, manufacturer, oem_part_number, url, checked_at,
                http_status, page_classification, selling_price_cents, reference_price_cents, savings_percent,
                price_visibility, price_display_type, result_type,
                availability_raw, availability_status, parse_confidence, warnings_json, raw_result_json, created_at)
            VALUES ('motosport', 'Honda', '18327-MEN-A30', 'https://www.motosport.com/oem-parts/part-number/18327-MEN-A30',
                ?, 200, 'normal_product', 3999, NULL, NULL, 'visible', 'regular', 'selling_price_found',
                'In Stock', 'in_stock', 'high', '[]', '{}', ?)
            """,
            (utc_now(), utc_now()),
        )
    page = TestClient(create_app(db), raise_server_exceptions=False).get("/comparison")
    assert "<th>MotoSport</th>" in page.text
    assert "Probe" not in page.text
    assert "39.99" in page.text


def _prepared_collect_db(name: str) -> Path:
    db = _empty_db(name)
    with connect_database(db) as conn:
        seed_partzilla(conn)
        from app.database import upsert_product_and_listing

        upsert_product_and_listing(conn, PartRecord("", "Kawasaki", "41080-1514"))
    return db


def _input_csv(name: str) -> Path:
    path = TEST_OUTPUT_DIR / name
    path.write_text(
        "Test_Case_ID,Manufacturer,OEM_Part_Number,Search_Observed_Product_Name,Search_Observed_MSRP,Expected_Partzilla_URL,Test_Purpose,Verified_Date,Source_URL\n"
        "T1,Kawasaki,41080-1514,,,,,,\n",
        encoding="utf-8",
    )
    return path


def _count(db: Path, table: str) -> int:
    with connect_database(db) as conn:
        return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


class _FakeLocator:
    def __init__(self, page, selector: str, available: bool) -> None:
        self.page = page
        self.selector = selector
        self.available = available

    def count(self) -> int:
        return 1 if self.available else 0

    @property
    def first(self):
        return self

    def inner_text(self, **_kwargs) -> str:
        return getattr(self.page, "body_text", "")

    def click(self, **kwargs) -> None:
        if getattr(self.page, "click_timeouts_remaining", 0) > 0:
            self.page.click_timeouts_remaining -= 1
            self.page.click_timeouts.append(kwargs.get("timeout"))
            raise probe_cart_price.PlaywrightTimeoutError("simulated attentive_overlay intercepts pointer events")
        if getattr(self.page, "timeout_on_click", False):
            self.page.click_timeouts.append(kwargs.get("timeout"))
            raise probe_cart_price.PlaywrightTimeoutError("simulated click timeout")
        self.page.clicked.append(self.selector)


class _FakePage:
    def __init__(self) -> None:
        self.clicked: list[str] = []
        self.requested_selectors: list[str] = []

    def locator(self, selector: str) -> _FakeLocator:
        self.requested_selectors.append(selector)
        available = selector in {"button:has-text('Add to Cart')", 'a.cart-remove-item[title="Remove item from cart."][data-sku="KAW186Z-X001-Y001"]'}
        return _FakeLocator(self, selector, available)

    def wait_for_timeout(self, _milliseconds: int) -> None:
        return None


class _SelectorPage:
    def __init__(self, *, available: set[str], body_text: str = "") -> None:
        self.available = available
        self.body_text = body_text
        self.clicked: list[str] = []
        self.requested_selectors: list[str] = []
        self.waits: list[int] = []
        self.timeout_on_click = False
        self.click_timeouts_remaining = 0
        self.click_timeouts: list[int | None] = []
        self.removed_overlays: list[str] = []

    def locator(self, selector: str) -> _FakeLocator:
        self.requested_selectors.append(selector)
        return _FakeLocator(self, selector, selector in self.available or (selector == "body" and bool(self.body_text)))

    def evaluate(self, _script: str):
        self.removed_overlays.append("#attentive_overlay")
        return ["#attentive_overlay"]

    def wait_for_timeout(self, milliseconds: int) -> None:
        self.waits.append(milliseconds)


class _FormValidationPage:
    def __init__(self, *, exists: bool = True, action: str = "https://www.motosport.com/cart/add", method: str = "post", quantity: str = "1") -> None:
        self.exists = exists
        self.action = action
        self.method = method
        self.quantity = quantity

    def evaluate(self, _script: str):
        if not self.exists:
            return {"exists": False}
        return {
            "exists": True,
            "id": "addtocartform",
            "action": self.action,
            "method": self.method,
            "has_addtocartbutton": True,
            "addtocartbutton_type": "submit",
            "addtocartbutton_value": "Add to cart",
            "addtocartbutton_visible": True,
            "addtocartbutton_enabled": True,
            "quantities": [{"id": "qty", "name": "quantity", "value": self.quantity}],
        }


class _ChaparralFormValidationPage:
    def __init__(self, *, exists: bool = True, action: str = "https://www.chapmoto.com/cart.php", method: str = "post", quantity: str = "1") -> None:
        self.exists = exists
        self.action = action
        self.method = method
        self.quantity = quantity

    def evaluate(self, _script: str):
        if not self.exists:
            return {"exists": False}
        return {
            "exists": True,
            "id": "orderform",
            "action": self.action,
            "method": self.method,
            "has_addtocartbutton": True,
            "addtocartbutton_type": "submit",
            "addtocartbutton_value": "Add to Cart",
            "addtocartbutton_visible": True,
            "addtocartbutton_enabled": True,
            "quantities": [{"id": "quantity", "name": "amount", "value": self.quantity}],
        }


class _WaitPage:
    def __init__(self) -> None:
        self.waits: list[int] = []

    def wait_for_timeout(self, milliseconds: int) -> None:
        self.waits.append(milliseconds)


class _EvaluatePage:
    def evaluate(self, script: str):
        if "querySelectorAll('form')" in script:
            return [
                {
                    "index": 0,
                    "id": "product-form",
                    "class_summary": "purchase-panel",
                    "text_excerpt": "Quantity See Price in Cart",
                    "action": "",
                    "method": "post",
                }
            ]
        return [
            _control(1, "button", "See Price in Cart", data_testid="add-to-cart"),
            _control(2, "button", "Proceed to Checkout"),
            _control(3, "a", "Return Policy"),
        ]


def _control(index: int, tag_name: str, text: str, **overrides) -> dict[str, object]:
    control = {
        "index": index,
        "selector_hint": f"{tag_name}:nth-of-type({index})",
        "tag_name": tag_name,
        "role": "",
        "text": text,
        "link_text": text if tag_name == "a" else "",
        "aria_label": "",
        "title": "",
        "data_testid": "",
        "data_test": "",
        "data_cy": "",
        "id": "",
        "class_summary": "",
        "disabled": False,
        "visible": True,
        "enabled": True,
        "bounding_box": {"x": 10, "y": 20 + index, "width": 100, "height": 30},
    }
    control.update(overrides)
    return control
