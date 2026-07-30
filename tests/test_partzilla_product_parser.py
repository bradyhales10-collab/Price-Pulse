from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from app.models import PartRecord
from app.parsers.partzilla_product_parser import ProductParseInput, parse_partzilla_product_page
from app.schemas.product_observation import AvailabilityStatus, PageClassification, PriceVisibility

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def test_parses_minimal_41080_1514_fixture() -> None:
    html = (FIXTURES_DIR / "partzilla_41080_1514_minimal.html").read_text(encoding="utf-8")
    visible_text = "\n".join(
        [
            "KAWASAKI OEM DISC | 41080-1514",
            "Part #: 41080-1514",
            "Sign In To See Price",
            "MSRP: $282.32",
            "Manufacturer: KAWASAKI",
            "Ships in 3 to 4 days",
            "Quantity",
        ]
    )
    record = PartRecord(test_case_id="KAW-004", manufacturer="Kawasaki", oem_part_number="41080-1514")

    observation = parse_partzilla_product_page(
        ProductParseInput(
            record=record,
            requested_url="https://www.partzilla.com/product/kawasaki/41080-1514",
            final_url="https://www.partzilla.com/product/kawasaki/41080-1514",
            http_status=200,
            page_title="KAWASAKI OEM DISC - 41080-1514 | partzilla.com",
            navigation_succeeded=True,
            exception_message=None,
            visible_text=visible_text,
            html=html,
            detected_signals=["Sign in to see price", "MSRP", "Ships in"],
            checked_at="2026-07-08T00:08:32Z",
        )
    )

    assert observation.page_classification == PageClassification.NORMAL_PRODUCT
    assert observation.canonical_url == "https://www.partzilla.com/product/kawasaki/41080-1514"
    assert observation.price_visibility == PriceVisibility.SIGN_IN_REQUIRED
    assert observation.observed_part_number == "41080-1514"
    assert observation.product_name == "DISC"
    assert observation.manufacturer_display == "KAWASAKI"
    assert observation.msrp_raw == "$282.32"
    assert observation.msrp == Decimal("282.32")
    assert observation.selling_price is None
    assert observation.availability_status == AvailabilityStatus.SHIPS_IN
    assert observation.shipping_estimate == "3 to 4 days"
    assert observation.parse_warnings == []


def test_part_number_mismatch_adds_warning() -> None:
    html = (FIXTURES_DIR / "partzilla_41080_1514_minimal.html").read_text(encoding="utf-8")
    record = PartRecord(test_case_id="KAW-X", manufacturer="Kawasaki", oem_part_number="99999-9999")

    observation = parse_partzilla_product_page(
        ProductParseInput(
            record=record,
            requested_url="https://www.partzilla.com/product/kawasaki/99999-9999",
            final_url="https://www.partzilla.com/product/kawasaki/41080-1514",
            http_status=200,
            page_title="KAWASAKI OEM DISC - 41080-1514 | partzilla.com",
            navigation_succeeded=True,
            exception_message=None,
            visible_text="KAWASAKI OEM DISC 41080-1514 MSRP: $282.32 Manufacturer: KAWASAKI Sign In To See Price Ships in 3 to 4 days",
            html=html,
            detected_signals=[],
        )
    )

    assert "observed_part_number_mismatch" in observation.parse_warnings


def test_sign_in_required_product_has_null_selling_price() -> None:
    html = (FIXTURES_DIR / "partzilla_41080_1514_minimal.html").read_text(encoding="utf-8")
    record = PartRecord(test_case_id="KAW-004", manufacturer="Kawasaki", oem_part_number="41080-1514")

    observation = parse_partzilla_product_page(
        ProductParseInput(
            record=record,
            requested_url="https://www.partzilla.com/product/kawasaki/41080-1514",
            final_url="https://www.partzilla.com/product/kawasaki/41080-1514?titan_sku=41080-1514",
            http_status=200,
            page_title="KAWASAKI OEM DISC - 41080-1514 | partzilla.com",
            navigation_succeeded=True,
            exception_message=None,
            visible_text="KAWASAKI OEM DISC 41080-1514 MSRP: $282.32 Manufacturer: KAWASAKI Sign In To See Price Ships in 3 to 4 days Quantity",
            html=html,
            detected_signals=[],
        )
    )

    assert observation.price_visibility == PriceVisibility.SIGN_IN_REQUIRED
    assert observation.selling_price is None
    assert observation.selling_price_raw is None


def test_visible_main_product_selling_price_is_decimal() -> None:
    html = """
    <html><title>KAWASAKI OEM FILTER - 92071-2128 | partzilla.com</title>
    <body><h1>KAWASAKI OEM FILTER | 92071-2128</h1>
    <span data-testid="productDetailPartNumber">Part #: 92071-2128</span>
    <span>MSRP: $19.99</span>
    <span data-testid="productFilterValueManufacturer-KAWASAKI">KAWASAKI</span>
    <button data-testid="stockInfoText">In Stock</button></body></html>
    """
    record = PartRecord(test_case_id="KAW-015", manufacturer="Kawasaki", oem_part_number="92071-2128")

    observation = parse_partzilla_product_page(
        ProductParseInput(
            record=record,
            requested_url="https://www.partzilla.com/product/kawasaki/92071-2128",
            final_url="https://www.partzilla.com/product/kawasaki/92071-2128",
            http_status=200,
            page_title="KAWASAKI OEM FILTER - 92071-2128 | partzilla.com",
            navigation_succeeded=True,
            exception_message=None,
            visible_text="KAWASAKI OEM FILTER 92071-2128\nManufacturer: KAWASAKI\nMSRP: $19.99\nPrice\n$14.25\nIn Stock\nQuantity",
            html=html,
            detected_signals=[],
        )
    )

    assert observation.price_visibility == PriceVisibility.VISIBLE
    assert observation.selling_price == Decimal("14.25")
    assert observation.selling_price_raw == "$14.25"


def test_main_product_price_ignores_recommendation_prices() -> None:
    html = """
    <html><title>KAWASAKI OEM DISC - 41080-1514 | partzilla.com</title>
    <body><h1>KAWASAKI OEM DISC | 41080-1514</h1>
    <span data-testid="productDetailPartNumber">Part #: 41080-1514</span>
    <span data-testid="productFilterValueManufacturer-KAWASAKI">KAWASAKI</span>
    <span>MSRP: $282.32</span>
    <div data-testid="productPrice">$241.99</div>
    <button data-testid="stockInfoText">Ships in 3 to 4 days</button>
    <h2>Riders Also Bought</h2><div data-testid="productPrice">$89.99</div>
    </body></html>
    """
    record = PartRecord(test_case_id="KAW-004", manufacturer="Kawasaki", oem_part_number="41080-1514")

    observation = parse_partzilla_product_page(
        ProductParseInput(
            record=record,
            requested_url="https://www.partzilla.com/product/kawasaki/41080-1514",
            final_url="https://www.partzilla.com/product/kawasaki/41080-1514",
            http_status=200,
            page_title="KAWASAKI OEM DISC - 41080-1514 | partzilla.com",
            navigation_succeeded=True,
            exception_message=None,
            visible_text="KAWASAKI OEM DISC 41080-1514\nManufacturer: KAWASAKI\nMSRP: $282.32\nPrice\n$241.99\nShips in 3 to 4 days\nQuantity\nRiders Also Bought\n$89.99",
            html=html,
            detected_signals=[],
        )
    )

    assert observation.selling_price == Decimal("241.99")
    assert observation.selling_price_raw == "$241.99"


def test_main_product_html_price_is_visible_without_price_label() -> None:
    html = """
    <html><title>KAWASAKI OEM DISC - 41080-1514 | partzilla.com</title>
    <body><h1>KAWASAKI OEM DISC | 41080-1514</h1>
    <span data-testid="productDetailPartNumber">Part #: 41080-1514</span>
    <span data-testid="productFilterValueManufacturer-KAWASAKI">KAWASAKI</span>
    <span>MSRP: $282.32</span>
    <div data-testid="productPrice">$241.99</div>
    <button data-testid="stockInfoText">In Stock</button>
    </body></html>
    """
    record = PartRecord(test_case_id="KAW-004", manufacturer="Kawasaki", oem_part_number="41080-1514")

    observation = parse_partzilla_product_page(
        ProductParseInput(
            record=record,
            requested_url="https://www.partzilla.com/product/kawasaki/41080-1514",
            final_url="https://www.partzilla.com/product/kawasaki/41080-1514",
            http_status=200,
            page_title="KAWASAKI OEM DISC - 41080-1514 | partzilla.com",
            navigation_succeeded=True,
            exception_message=None,
            visible_text="KAWASAKI OEM DISC 41080-1514\nPart #: 41080-1514\nManufacturer: KAWASAKI\nMSRP: $282.32\n$241.99\nIn Stock\nQuantity",
            html=html,
            detected_signals=[],
        )
    )

    assert observation.price_visibility == PriceVisibility.VISIBLE
    assert observation.selling_price == Decimal("241.99")


def test_supersession_fields_do_not_change_observed_part() -> None:
    html = """
    <html><title>KAWASAKI OEM SUPERSEDED BY 14081007 BRACKET A ,BALANCER - 14081-005 | partzilla.com</title>
    <body><h1>KAWASAKI OEM SUPERSEDED BY 14081007 BRACKET A ,BALANCER | 14081-005</h1>
    <span data-testid="productDetailPartNumber">Part #: 14081-005</span>
    <span data-testid="productFilterValueManufacturer-KAWASAKI">KAWASAKI</span>
    <span>MSRP: $37.71</span>
    <button data-testid="authModalButton">Sign In To See Price MSRP: $37.71</button>
    <button data-testid="stockInfoText">Ships in 3 to 4 days</button>
    </body></html>
    """
    record = PartRecord(test_case_id="KAW-014", manufacturer="Kawasaki", oem_part_number="14081-005")

    observation = parse_partzilla_product_page(
        ProductParseInput(
            record=record,
            requested_url="https://www.partzilla.com/product/kawasaki/14081-005",
            final_url="https://www.partzilla.com/product/kawasaki/14081-005",
            http_status=200,
            page_title="KAWASAKI OEM SUPERSEDED BY 14081007 BRACKET A ,BALANCER - 14081-005 | partzilla.com",
            navigation_succeeded=True,
            exception_message=None,
            visible_text="KAWASAKI OEM SUPERSEDED BY 14081007 BRACKET A ,BALANCER 14081-005\nPart #: 14081-005\nManufacturer: KAWASAKI\nMSRP: $37.71\nSign In To See Price\nShips in 3 to 4 days",
            html=html,
            detected_signals=[],
        )
    )

    assert observation.observed_part_number == "14081-005"
    assert observation.supersession_detected is True
    assert observation.superseded_by_raw == "14081007"


def test_missing_visible_msrp_remains_warning() -> None:
    html = """
    <html><title>KAWASAKI OEM GROMMET - 92071-2128 | partzilla.com</title>
    <body><h1>KAWASAKI OEM GROMMET | 92071-2128</h1>
    <span data-testid="productDetailPartNumber">Part #: 92071-2128</span>
    <span data-testid="productFilterValueManufacturer-KAWASAKI">KAWASAKI</span>
    <button data-testid="stockInfoText">Ships in 3 to 4 days</button>
    <script>{"priceMsrp":"$9.99","priceDisplay":"$6.97"}</script>
    </body></html>
    """
    record = PartRecord(test_case_id="KAW-015", manufacturer="Kawasaki", oem_part_number="92071-2128")

    observation = parse_partzilla_product_page(
        ProductParseInput(
            record=record,
            requested_url="https://www.partzilla.com/product/kawasaki/92071-2128",
            final_url="https://www.partzilla.com/product/kawasaki/92071-2128",
            http_status=200,
            page_title="KAWASAKI OEM GROMMET - 92071-2128 | partzilla.com",
            navigation_succeeded=True,
            exception_message=None,
            visible_text="KAWASAKI OEM GROMMET 92071-2128\nPart #: 92071-2128\nManufacturer: KAWASAKI\nSign In To See Price\nShips in 3 to 4 days",
            html=html,
            detected_signals=[],
        )
    )

    assert observation.msrp is None
    assert "msrp_not_found" in observation.parse_warnings


def test_blocked_page_does_not_parse_product_fields() -> None:
    record = PartRecord(test_case_id="KAW-004", manufacturer="Kawasaki", oem_part_number="41080-1514")

    observation = parse_partzilla_product_page(
        ProductParseInput(
            record=record,
            requested_url="https://www.partzilla.com/product/kawasaki/41080-1514",
            final_url="https://www.partzilla.com/product/kawasaki/41080-1514",
            http_status=403,
            page_title="Just a moment...",
            navigation_succeeded=True,
            exception_message=None,
            visible_text="www.partzilla.com Performing security verification",
            html="<html><title>Just a moment...</title><h1>www.partzilla.com</h1></html>",
            detected_signals=[],
        )
    )

    assert observation.page_classification == PageClassification.BLOCKED
    assert observation.price_visibility == PriceVisibility.UNKNOWN
    assert observation.product_name is None
    assert observation.msrp is None
    assert observation.parse_warnings == ["non_product_page_no_product_parse"]
