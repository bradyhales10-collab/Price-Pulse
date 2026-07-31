from __future__ import annotations

from decimal import Decimal

from app.comparison import competitor_sql_parts, competitor_state_aliases
from app.competitors.registry import competitor_short_names, get_competitor, list_competitors
from app.competitors.revzilla import (
    RevzillaAdapter,
    build_search_url,
    extract_availability,
    extract_price,
    normalize_availability,
)
from app.models import PartRecord

DISCONTINUED_PAGE = """
<html><head>
<meta name="sailthru.price" content="71568">
<meta name="sailthru.inventory" content="0">
</head><body>
<h1>Kawasaki DISC, FR 41080-1186</h1>
<p>Current price is $715.68</p>
<p>OEM Part Number: 41080-1186</p>
<p>Availability Closeout: This product is no longer available</p>
<p><strong>This item is currently out of stock.</strong></p>
<a href="/oem/kawasaki/kawasaki-41080-1186-disc-fr">link</a>
</body></html>
"""

LIVE_PAGE = """
<html><head>
<meta name="sailthru.price" content="8299">
<meta name="sailthru.inventory" content="12">
</head><body>
<h1>Kawasaki GASKET 11060-1234</h1>
<p>Current price is $82.99</p>
<p>OEM Part Number: 11060-1234</p>
<p>In Stock</p><button>Add To Cart</button>
</body></html>
"""


def _part(part_number: str, manufacturer: str = "Kawasaki") -> PartRecord:
    return PartRecord(test_case_id="t", manufacturer=manufacturer, oem_part_number=part_number)


# --- RevZilla adapter --------------------------------------------------------


def test_lookup_goes_through_search_because_urls_embed_a_description_slug() -> None:
    adapter = RevzillaAdapter()

    assert adapter.supports_direct_part_url is False
    assert adapter.build_product_url(_part("41080-1186")) == build_search_url("41080-1186")
    assert "41080-1186" in build_search_url("41080-1186")


def test_price_on_a_discontinued_listing_is_not_used() -> None:
    """RevZilla keeps showing a price after a part is dead. Treating that as a
    live competitor price would drag suggested prices down."""
    observation = RevzillaAdapter().parse_product_page(
        DISCONTINUED_PAGE,
        _part("41080-1186"),
        final_url="https://www.revzilla.com/oem/kawasaki/kawasaki-41080-1186-disc-fr",
        http_status=200,
    )

    assert observation.observed_part_number == "41080-1186"
    assert observation.availability_status == "discontinued"
    assert observation.selling_price is None
    assert "price_ignored_discontinued" in observation.warnings
    assert observation.raw_evidence_summary["lookup_status"] == "discontinued"


def test_live_in_stock_listing_yields_a_price() -> None:
    observation = RevzillaAdapter().parse_product_page(
        LIVE_PAGE,
        _part("11060-1234"),
        final_url="https://www.revzilla.com/oem/kawasaki/kawasaki-11060-1234-gasket",
        http_status=200,
    )

    assert str(observation.selling_price) == "82.99"
    assert observation.availability_status == "in_stock"
    assert observation.selling_price_confidence == "high"


def test_a_page_for_a_different_part_is_not_accepted_as_a_match() -> None:
    observation = RevzillaAdapter().parse_product_page(
        "<html><body><p>OEM Part Number: 99999-0000</p><p>Current price is $10.00</p><p>In Stock</p></body></html>",
        _part("11060-1234"),
        http_status=200,
    )

    assert observation.selling_price is None
    assert observation.observed_part_number is None


def test_blocking_and_captcha_are_reported_not_parsed() -> None:
    adapter = RevzillaAdapter()

    blocked = adapter.parse_product_page("<html><body>Access Denied</body></html>", _part("1"), http_status=403)
    challenge = adapter.parse_product_page(
        "<html><body>Please verify you are human</body></html>", _part("1"), http_status=200
    )

    assert blocked.page_classification == "blocked"
    assert challenge.page_classification == "challenge"
    assert blocked.selling_price is None and challenge.selling_price is None


def test_price_comes_from_the_cents_metadata_when_present() -> None:
    html = '<meta name="sailthru.price" content="71568">'

    assert extract_price("", html) == (Decimal("715.68"), "page_metadata")
    # Visible text is the fallback.
    assert extract_price("Current price is $82.99", "")[1] == "visible_price"
    # A zero price is not a real price.
    assert extract_price("", '<meta name="sailthru.price" content="0">') == (None, "not_available")


def test_zero_inventory_counts_as_out_of_stock() -> None:
    assert extract_availability("", '<meta name="sailthru.inventory" content="0">')[1] == "out_of_stock"


def test_availability_wording_is_normalised() -> None:
    assert normalize_availability("This product is no longer available") == "discontinued"
    assert normalize_availability("Out of Stock") == "out_of_stock"
    assert normalize_availability("In Stock") == "in_stock"
    assert normalize_availability(None) == "unknown"


def test_polaris_is_not_claimed_because_revzilla_has_no_polaris_oem_fiche() -> None:
    makers = RevzillaAdapter().supported_manufacturers

    assert "Polaris" not in makers
    assert set(makers) == {"Honda", "Yamaha", "Kawasaki", "Suzuki"}


def test_revzilla_starts_out_as_experimental() -> None:
    """It has not been confirmed against the live site yet, so it must not be
    treated as a production collector."""
    assert RevzillaAdapter().capabilities.status == "experimental_probe"


# --- Registry-driven refactor ------------------------------------------------


def test_registry_exposes_every_competitor_including_the_new_one() -> None:
    keys = [adapter.competitor_key for adapter in list_competitors()]

    assert keys == ["partzilla", "motosport", "chaparral", "revzilla"]
    assert get_competitor("revzilla").display_name == "RevZilla"


def test_short_names_come_from_the_adapters() -> None:
    names = competitor_short_names()

    assert names["Chaparral Motorsports"] == "Chaparral"
    assert names["RevZilla"] == "RevZilla"


def test_comparison_sql_is_generated_for_every_registered_competitor() -> None:
    """Adding an adapter must be enough for its prices to flow through the
    comparison query, with no SQL editing."""
    columns, joins, product_name = competitor_sql_parts()

    for adapter in list_competitors():
        key = adapter.competitor_key
        assert f"{key}_selling_price_cents" in columns, key
        assert f"competitor_code='{key}'" in joins, key

    # Historical aliases are preserved so existing filters keep working.
    aliases = competitor_state_aliases()
    assert aliases["partzilla"] == "ps"
    assert aliases["motosport"] == "ms"
    assert aliases["chaparral"] == "cs"
    # A new competitor gets a generated alias.
    assert aliases["revzilla"] == "revzilla_s"
    assert "revzilla_s.product_name" in product_name


def test_unsafe_competitor_keys_are_rejected_before_reaching_sql() -> None:
    from app.comparison import _sql_aliases

    for bad in ("bad key", "drop--table", "1competitor", "quote'key"):
        try:
            _sql_aliases(bad)
        except ValueError:
            continue
        raise AssertionError(f"unsafe key was accepted: {bad!r}")
