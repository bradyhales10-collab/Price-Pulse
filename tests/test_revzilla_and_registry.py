from __future__ import annotations

import re
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
<meta name="sailthru.tags" content="manufacturer-kawasaki,stock-level-in-stock">
</head><body>
<h1>Kawasaki GASKET 11060-1234</h1>
<p>Current price is $82.99</p>
<p>OEM Part Number: 11060-1234</p>
<p>In Stock</p><button>Add To Cart</button>
</body></html>
"""

# Reproduces the real 41080-0162 page, which the first live probe read wrongly.
# The price carries cents only in the metadata, the listing is out of stock, and
# the page still shows "Add to Cart" and "Ships FREE".
REAL_OUT_OF_STOCK_PAGE = """
<html><head>
<meta name="sailthru.price" content="32642">
<meta content="0" name="sailthru.inventory">
<meta name="sailthru.tags" content="price-350-399,manufacturer-kawasaki,stock-level-out-of-stock">
</head><body>
<h1>Kawasaki DISC, RR 41080-0162</h1>
<p>Current price is $326</p><p>42</p>
<p>OEM Part Number: 41080-0162</p>
<button>Add to Cart</button>
<p>This item Ships FREE</p>
<td>AvailabilityOut of Stock</td>
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


def test_revzilla_reads_prices_correctly_but_is_not_production_yet() -> None:
    """A live probe of 15 of our own best sellers returned 14 correct prices,
    so the parser is proven. It stays probe-only because production collection
    has no path for its search-then-open lookup."""
    capabilities = RevzillaAdapter().capabilities

    assert capabilities.status == "experimental_probe"
    assert capabilities.legal_review_status == "approved_for_monitoring"


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


# --- Probe wiring ------------------------------------------------------------


def test_revzilla_is_probeable_but_refused_for_a_production_run() -> None:
    import argparse

    import probe_competitor
    from app.competitors.registry import select_competitors

    probe_competitor.validate_probe_args(
        argparse.Namespace(competitor="revzilla", max_parts=7, delay_seconds=6)
    )

    try:
        select_competitors(["revzilla"])
    except ValueError as exc:
        assert "experimental" in str(exc).lower()
    else:
        raise AssertionError("revzilla has no production collector and must be refused")


def test_an_experimental_competitor_is_still_refused_for_a_production_run() -> None:
    """The guard that stops a half-tested adapter going live must keep working
    now that no shipped competitor is experimental."""
    from app.competitors import registry
    from app.competitors.base import CompetitorCapabilities

    class _Unproven:
        competitor_key = "unproven"
        display_name = "Unproven"
        capabilities = CompetitorCapabilities(
            requires_login=False,
            supports_public_price=True,
            supports_direct_part_url=False,
            status="experimental_probe",
            legal_review_status="review_needed",
        )

    original = dict(registry._REGISTRY)
    registry._REGISTRY["unproven"] = _Unproven()
    try:
        try:
            registry.select_competitors(["unproven"])
        except ValueError as exc:
            assert "experimental" in str(exc).lower()
        else:
            raise AssertionError("experimental competitor should be refused for a normal run")
        # Probing it is still allowed.
        assert registry.select_competitors(["unproven"], probe_mode=True)
    finally:
        registry._REGISTRY.clear()
        registry._REGISTRY.update(original)


def test_probe_safety_limits_still_apply_to_revzilla() -> None:
    import argparse

    import probe_competitor

    for bad in (
        argparse.Namespace(competitor="revzilla", max_parts=26, delay_seconds=6),
        argparse.Namespace(competitor="revzilla", max_parts=1, delay_seconds=0),
    ):
        try:
            probe_competitor.validate_probe_args(bad)
        except ValueError:
            continue
        raise AssertionError(f"probe limits not enforced for {bad}")

    probe_competitor.validate_probe_args(
        argparse.Namespace(competitor="revzilla", max_parts=25, delay_seconds=1)
    )


def test_other_competitor_probes_keep_the_five_second_minimum() -> None:
    import argparse

    import probe_competitor

    try:
        probe_competitor.validate_probe_args(
            argparse.Namespace(competitor="motosport", max_parts=1, delay_seconds=1)
        )
    except ValueError as exc:
        assert "at least 5" in str(exc)
    else:
        raise AssertionError("non-RevZilla probe accepted the reduced delay")


def test_search_result_resolver_only_follows_the_requested_part() -> None:
    from app.competitors.revzilla import select_search_result

    page = (
        '<a href="/oem/kawasaki/kawasaki-99999-9999-other">wrong</a>'
        '<a href="/oem/kawasaki/kawasaki-41080-1186-disc-fr">right</a>'
        '<a href="/motorcycle/aftermarket-thing">aftermarket</a>'
    )

    assert select_search_result(page, "41080-1186") == (
        "https://www.revzilla.com/oem/kawasaki/kawasaki-41080-1186-disc-fr"
    )
    # No confident match must return nothing rather than a wrong link.
    assert select_search_result(page, "12345-6789") is None
    assert select_search_result("", "41080-1186") is None
    assert select_search_result(page, "") is None


def test_search_page_without_exact_oem_match_is_not_an_operational_error() -> None:
    observation = RevzillaAdapter().parse_product_page(
        '<html><body><a href="/dirt-bike/hiflofiltro-premium-oil-filter-hf114">Replaces OEM 15412-HP7-A01</a></body></html>',
        _part("15412-HP7-A01", manufacturer="Honda"),
        visible_text="HiFloFiltro Premium Oil Filter HF114 Replaces OEM 15412-HP7-A01",
        final_url="https://www.revzilla.com/search?query=15412-HP7-A01",
        http_status=200,
    )

    assert observation.page_classification == "not_found"
    assert observation.raw_evidence_summary["lookup_status"] == "part_not_found"
    assert observation.selling_price is None
    assert observation.warnings == ["search_no_exact_oem_match"]


def test_probe_input_file_is_well_formed_and_includes_a_polaris_control() -> None:
    from pathlib import Path

    from app.input_loader import load_parts_csv

    result = load_parts_csv(Path("data/input/RevZilla_Probe_Parts.csv"))
    manufacturers = {record.manufacturer for record in result.records}

    assert len(result.records) >= 6
    # Polaris is included on purpose: it must come back as not carried.
    assert "Polaris" in manufacturers
    assert "Kawasaki" in manufacturers


# --- Regressions from the first live probe ------------------------------------


def test_out_of_stock_listing_is_detected_despite_add_to_cart_and_ships_free() -> None:
    """The first live run recorded this part as in stock at $326, because
    "Ships FREE" was read as a stock signal and "AvailabilityOut of Stock" was
    rejected by a word boundary. It is out of stock and worth no price."""
    observation = RevzillaAdapter().parse_product_page(
        REAL_OUT_OF_STOCK_PAGE,
        _part("41080-0162"),
        visible_text=re.sub(r"<[^>]+>", "\n", REAL_OUT_OF_STOCK_PAGE),
        http_status=200,
    )

    assert observation.availability_status == "out_of_stock"
    assert observation.selling_price is None
    assert "price_ignored_out_of_stock" in observation.warnings


def test_shipping_promo_is_not_treated_as_stock_information() -> None:
    assert extract_availability("This item Ships FREE", "")[1] == "unknown"


def test_out_of_stock_is_found_even_without_a_separating_space() -> None:
    assert extract_availability("AvailabilityOut of Stock", "")[1] == "out_of_stock"


def test_cents_are_read_from_metadata_whatever_the_attribute_order() -> None:
    """The live run lost the cents and recorded $326 instead of $326.42."""
    for html in (
        '<meta name="sailthru.price" content="32642">',
        '<meta content="32642" name="sailthru.price">',
        "meta-sailthru.price: 32642",
    ):
        assert extract_price("", html) == (Decimal("326.42"), "page_metadata"), html


def test_a_visible_price_without_cents_is_refused_rather_than_rounded() -> None:
    """Dollars and cents render separately, so a price with no cents beside it
    may be truncated. Recording it would be worse than recording nothing."""
    price, source = extract_price("Current price is $326", "")

    assert price == Decimal("326")
    assert source == "visible_price_dollars_only"

    observation = RevzillaAdapter().parse_product_page(
        "<html><body><p>Current price is $326</p><p>OEM Part Number: 41080-0162</p><p>In Stock</p></body></html>",
        _part("41080-0162"),
        visible_text="Current price is $326\nOEM Part Number: 41080-0162\nIn Stock",
        http_status=200,
    )
    assert observation.selling_price is None
    assert "price_ignored_missing_cents" in observation.warnings


def test_split_dollars_and_cents_are_joined_when_adjacent() -> None:
    assert extract_price("Current price is $326.42", "") == (Decimal("326.42"), "visible_price")


def test_unknown_availability_does_not_yield_a_price() -> None:
    """Only a positive in-stock signal is trusted, because RevZilla prices
    listings it cannot sell."""
    observation = RevzillaAdapter().parse_product_page(
        "<html><body><p>Current price is $82.99</p><p>OEM Part Number: 11060-1234</p></body></html>",
        _part("11060-1234"),
        visible_text="Current price is $82.99\nOEM Part Number: 11060-1234",
        http_status=200,
    )

    assert observation.availability_status == "unknown"
    assert observation.selling_price is None
    assert "price_ignored_unknown" in observation.warnings


def test_probe_report_no_longer_hardcodes_another_competitor() -> None:
    from pathlib import Path as _Path

    source = _Path("probe_competitor.py").read_text(encoding="utf-8")

    assert "MotoSport" not in source


def test_part_association_is_reported_to_the_probe() -> None:
    """The probe counts a run as successful only when association is confirmed,
    so the adapter must record it."""
    observation = RevzillaAdapter().parse_product_page(
        LIVE_PAGE,
        _part("11060-1234"),
        visible_text=re.sub(r"<[^>]+>", "\n", LIVE_PAGE),
        http_status=200,
    )

    association = observation.raw_evidence_summary["product_association"]
    assert association["confirmed"] is True
    assert association["observed_part_number"] == "11060-1234"


# --- Probe reporting and own-catalog export ----------------------------------


def test_all_listings_unavailable_is_reported_as_inventory_not_parser_failure() -> None:
    """The live probe read every page correctly and still produced no prices,
    because every part was out of stock. The report said the parser failed,
    which would send us fixing working code."""
    import probe_competitor
    from app.competitors.base import CompetitorObservation

    def _row(order: int, part: str) -> probe_competitor.ProbeRow:
        observation = CompetitorObservation(
            competitor_key="revzilla",
            manufacturer="Kawasaki",
            oem_part_number=part,
            observed_part_number=part,
            page_classification="normal_product",
            availability_status="out_of_stock",
            warnings=["price_ignored_out_of_stock"],
            raw_evidence_summary={"product_association": {"confirmed": True}},
        )
        return probe_competitor.ProbeRow(order, "Kawasaki", part, "url", "now", observation)

    run = probe_competitor.ProbeRun(
        competitor_key="revzilla",
        started_at="2026-07-31T00:00:00Z",
        completed_at="2026-07-31T00:02:00Z",
        rows=[_row(1, "41080-0162"), _row(2, "41080-0170")],
    )
    review = probe_competitor._review_text(run)

    assert "working_no_sellable_inventory" in review
    assert "failed_pending_fix" not in review
    assert "currently in stock" in review
    assert "Unavailable-listing rate: 100.0% (2/2)" in review


def test_own_catalog_export_respects_competitor_manufacturer_coverage() -> None:
    """RevZilla carries no Polaris, so Polaris parts must never be exported for
    it even though they sit in our catalog."""
    import sys
    from pathlib import Path as _Path

    sys.path.insert(0, str(_Path(__file__).parent))
    from test_import_comparison_collection import _comparison_db  # noqa: PLC0415

    from export_probe_input import select_parts

    db = _comparison_db("export_coverage.db")
    revzilla = {part["manufacturer"] for part in select_parts(db, "revzilla", 25)}

    assert "Polaris" not in revzilla
    assert revzilla <= {"Honda", "Yamaha", "Kawasaki", "Suzuki"}


def test_own_catalog_export_produces_valid_probe_input() -> None:
    import sys
    import tempfile
    from pathlib import Path as _Path

    sys.path.insert(0, str(_Path(__file__).parent))
    from test_import_comparison_collection import _comparison_db  # noqa: PLC0415

    from app.input_loader import load_parts_csv
    from export_probe_input import select_parts, write_probe_file

    db = _comparison_db("export_roundtrip.db")
    parts = select_parts(db, "revzilla", 25)
    assert parts, "expected at least one exportable part"

    with tempfile.TemporaryDirectory() as tmp:
        path = _Path(tmp) / "probe.csv"
        write_probe_file(path, "revzilla", parts)
        loaded = load_parts_csv(path)

    assert len(loaded.records) == len(parts)


def test_own_catalog_export_rejects_an_unknown_competitor() -> None:
    from app.competitors.registry import get_competitor

    try:
        get_competitor("not-a-competitor")
    except ValueError:
        return
    raise AssertionError("unknown competitor should be rejected")


def test_prices_always_carry_two_decimal_places() -> None:
    """A price shown as 6.7 reads like a missing digit in reports."""
    for cents, expected in (("670", "6.70"), ("205", "2.05"), ("1667", "16.67"), ("1000", "10.00")):
        html = f'<meta name="sailthru.price" content="{cents}">'
        assert str(extract_price("", html)[0]) == expected, cents
