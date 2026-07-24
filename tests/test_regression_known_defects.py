from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from app.database import connect_database, create_scan_run, initialize_database, persist_observation, seed_partzilla, upsert_product_and_listing
from app.models import PartRecord
from app.parsers.partzilla_product_parser import ProductParseInput, parse_partzilla_product_page
from app.price_forensics import apply_price_evidence_to_observation, build_price_evidence
from app.raw_price_signals import discover_raw_price_signals
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
from audit_database import audit_database


def test_supersession_does_not_leak_through_real_parser_sequence() -> None:
    sequence = [
        ("41080-1514", "DISC", None),
        ("14081-005", "SUPERSEDED BY 14081007 BRACKET A ,BALANCER", "14081007"),
        ("14094-0051", "COVER,SEAT UNDER,RR,U", None),
        ("K53001-240", "SOLO SEAT", None),
        ("92071-2128", "GROMMET", None),
        ("99999-0001", "SUPERSEDED BY 999990002 TEST", "999990002"),
        ("3099", "STAR LTD II ANT 7 1/4", None),
    ]
    observations = [_parse_product(part, name, stale_visible_text="SUPERSEDED BY 14081007") for part, name, _ in sequence]

    assert [obs.superseded_by_raw for obs in observations] == [target for _, _, target in sequence]
    assert [obs.supersession_detected for obs in observations] == [target is not None for _, _, target in sequence]


def test_discounted_34028_fixture_selects_customer_payable_price() -> None:
    html = """
    <html><body><main>
    <h1>KAWASAKI OEM STEP | 34028-0327</h1>
    <span data-testid="productDetailPartNumber">Part #: 34028-0327</span>
    <span data-testid="productFilterValueManufacturer-KAWASAKI">KAWASAKI</span>
    <script type="application/ld+json">{
      "@context":"https://schema.org","@type":"Product","mpn":"34028-0327",
      "name":"KAWASAKI OEM STEP | 34028-0327 | 340280327",
      "offers":{"@type":"Offer","price":37.3,"priceCurrency":"USD",
        "priceSpecification":{"@type":"UnitPriceSpecification","price":37.3}}
    }</script>
    <div>$32.69 SAVE 13%</div>
    <span data-testid="productPriceValue">$37.30</span>
    <button data-testid="stockInfoText">In Stock</button>
    <button>Add to Cart</button>
    </main></body></html>
    """
    observation = _parse_product("34028-0327", "STEP", html=html)
    signals = discover_raw_price_signals(html=html, visible_text=_text_for("34028-0327", "STEP"), observation=observation)
    evidence = build_price_evidence(html=html, visible_text=_text_for("34028-0327", "STEP"), observation=observation, raw_price_signals=signals)
    apply_price_evidence_to_observation(observation, evidence)

    assert observation.selling_price == Decimal("32.69")
    assert observation.reference_price == Decimal("37.30")
    assert observation.savings_percent == 13
    assert observation.price_display_type == PriceDisplayType.DISCOUNTED
    assert "secondary_price_specification_differs" in observation.parse_warnings


def test_partzilla_visible_discount_overrides_stale_structured_offer_price() -> None:
    html = """
    <html><body><main>
    <h1>KAWASAKI OEM FORK CLAMP | 99969-3880</h1>
    <span data-testid="productDetailPartNumber">Part #: 99969-3880</span>
    <span data-testid="productFilterValueManufacturer-KAWASAKI">KAWASAKI</span>
    <script type="application/ld+json">{
      "@context":"https://schema.org","@type":"Product","mpn":"99969-3880",
      "name":"KAWASAKI OEM FORK CLAMP | 99969-3880",
      "offers":{"@type":"Offer","price":681.41,"priceCurrency":"USD",
        "priceSpecification":{"@type":"UnitPriceSpecification","price":681.41}}
    }</script>
    <button data-testid="stockInfoText">In Stock</button>
    <button>Add to Cart</button>
    </main></body></html>
    """
    visible_text = (
        "KAWASAKI OEM FORK CLAMP | 99969-3880\n"
        "Part #: 99969-3880\n"
        "$633.74 SAVE 7%\n"
        "In Stock\nQuantity\nAdd to Cart"
    )
    record = PartRecord("", "Kawasaki", "99969-3880")
    observation = parse_partzilla_product_page(
        ProductParseInput(
            record=record,
            requested_url="https://www.partzilla.com/product/kawasaki/99969-3880",
            final_url="https://www.partzilla.com/product/kawasaki/99969-3880",
            http_status=200,
            page_title="KAWASAKI OEM FORK CLAMP - 99969-3880 | partzilla.com",
            navigation_succeeded=True,
            exception_message=None,
            visible_text=visible_text,
            html=html,
            detected_signals=[],
        )
    )
    signals = discover_raw_price_signals(html=html, visible_text=visible_text, observation=observation)
    evidence = build_price_evidence(
        html=html,
        visible_text=visible_text,
        observation=observation,
        raw_price_signals=signals,
    )
    apply_price_evidence_to_observation(observation, evidence)

    assert observation.selling_price == Decimal("633.74")
    assert observation.reference_price == Decimal("681.41")
    assert observation.savings_percent == 7
    assert observation.price_display_type == PriceDisplayType.DISCOUNTED


def test_partzilla_gated_msrp_is_not_selected_from_structured_offer() -> None:
    html = """
    <html><body><main>
    <h1>KAWASAKI OEM FORK CLAMP | 99969-3880</h1>
    <span data-testid="productDetailPartNumber">Part #: 99969-3880</span>
    <button data-testid="authModalButton">Sign In To See Price</button>
    <span>MSRP: $681.41</span>
    <script type="application/ld+json">{
      "@context":"https://schema.org","@type":"Product","mpn":"99969-3880",
      "name":"KAWASAKI OEM FORK CLAMP | 99969-3880",
      "offers":{"@type":"Offer","price":681.41,"priceCurrency":"USD",
        "priceSpecification":{"@type":"UnitPriceSpecification","price":681.41}}
    }</script>
    <button data-testid="stockInfoText">In Stock</button>
    </main></body></html>
    """
    visible_text = (
        "KAWASAKI OEM FORK CLAMP | 99969-3880\n"
        "Part #: 99969-3880\n"
        "Sign In To See Price\n"
        "MSRP: $681.41\n"
        "In Stock"
    )
    record = PartRecord("", "Kawasaki", "99969-3880")
    observation = parse_partzilla_product_page(
        ProductParseInput(
            record=record,
            requested_url="https://www.partzilla.com/product/kawasaki/99969-3880",
            final_url="https://www.partzilla.com/product/kawasaki/99969-3880",
            http_status=200,
            page_title="KAWASAKI OEM FORK CLAMP - 99969-3880 | partzilla.com",
            navigation_succeeded=True,
            exception_message=None,
            visible_text=visible_text,
            html=html,
            detected_signals=[],
        )
    )
    signals = discover_raw_price_signals(html=html, visible_text=visible_text, observation=observation)
    evidence = build_price_evidence(
        html=html,
        visible_text=visible_text,
        observation=observation,
        raw_price_signals=signals,
    )

    assert evidence.selected_msrp == "681.41"
    assert evidence.selected_selling_price is None
    assert "structured_offer_matches_gated_msrp" in evidence.parse_warnings


def test_audit_detects_scan_run_supersession_carry_forward() -> None:
    db = Path("data/output/test-artifacts/audit_regression.db")
    if db.exists():
        db.unlink()
    initialize_database(db)
    with connect_database(db) as conn:
        competitor_id = seed_partzilla(conn)
        listing_ids = []
        for part in ["14081-005", "14094-0051", "K53001-240", "92071-2128"]:
            _, listing_id, _, _ = upsert_product_and_listing(conn, PartRecord(test_case_id="", manufacturer="Kawasaki", oem_part_number=part))
            listing_ids.append(listing_id)
        run_id = create_scan_run(conn, competitor_id=competitor_id, requested_part_count=4)
        for listing_id in listing_ids:
            persist_observation(
                conn,
                scan_run_id=run_id,
                listing_id=listing_id,
                observation=_observation(superseded_by_raw="14081007"),
                observation_json_path="obs.json",
            )

    findings = audit_database(db)
    assert any(finding.code == "supersession_carry_forward_pattern" for finding in findings)


def _parse_product(part_number: str, product_name: str, *, html: str | None = None, stale_visible_text: str = "") -> ProductObservation:
    html = html or f"""
    <html><title>KAWASAKI OEM {product_name} - {part_number} | partzilla.com</title>
    <body><main><h1>KAWASAKI OEM {product_name} | {part_number}</h1>
    <span data-testid="productDetailPartNumber">Part #: {part_number}</span>
    <span data-testid="productFilterValueManufacturer-KAWASAKI">KAWASAKI</span>
    <span>MSRP: $1.00</span>
    <button data-testid="stockInfoText">Ships in 3 to 4 days</button>
    </main></body></html>
    """
    record = PartRecord(test_case_id="", manufacturer="Kawasaki", oem_part_number=part_number)
    return parse_partzilla_product_page(
        ProductParseInput(
            record=record,
            requested_url=f"https://www.partzilla.com/product/kawasaki/{part_number}",
            final_url=f"https://www.partzilla.com/product/kawasaki/{part_number}",
            http_status=200,
            page_title=f"KAWASAKI OEM {product_name} - {part_number} | partzilla.com",
            navigation_succeeded=True,
            exception_message=None,
            visible_text=_text_for(part_number, product_name, stale_visible_text=stale_visible_text),
            html=html,
            detected_signals=[],
            checked_at="2026-07-09T00:00:00Z",
        )
    )


def _text_for(part_number: str, product_name: str, *, stale_visible_text: str = "") -> str:
    return "\n".join(
        [
            f"KAWASAKI OEM {product_name} | {part_number}",
            f"Part #: {part_number}",
            "Manufacturer: KAWASAKI",
            "In Stock",
            "$32.69 SAVE 13%",
            "$37.30",
            "Add to Cart",
            stale_visible_text,
        ]
    )


def _observation(*, superseded_by_raw: str | None) -> ProductObservation:
    return ProductObservation(
        test_case_id="",
        manufacturer="Kawasaki",
        oem_part_number="41080-1514",
        observed_part_number="41080-1514",
        requested_url="",
        final_url="",
        canonical_url="",
        http_status=200,
        page_title="",
        page_classification=PageClassification.NORMAL_PRODUCT,
        price_visibility=PriceVisibility.VISIBLE,
        classification_confidence=ParseConfidence.HIGH,
        classification_evidence=[],
        product_name="DISC",
        manufacturer_display="KAWASAKI",
        msrp_raw=None,
        msrp=None,
        selling_price_raw="$1.00",
        selling_price=Decimal("1.00"),
        availability_raw="In Stock",
        availability_status=AvailabilityStatus.IN_STOCK,
        shipping_estimate=None,
        access_context=AccessContext.AUTHENTICATED_SESSION,
        session_status=SessionStatus.AUTHENTICATED,
        superseded_by_raw=superseded_by_raw,
        supersession_detected=superseded_by_raw is not None,
        price_parse_confidence=ParseConfidence.HIGH,
        price_validation_status=PriceValidationStatus.NOT_MANUALLY_VALIDATED,
        parse_confidence=ParseConfidence.HIGH,
        parse_warnings=[],
        checked_at="2026-07-09T00:00:00Z",
    )
