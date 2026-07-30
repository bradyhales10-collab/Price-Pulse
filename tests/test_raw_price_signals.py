from __future__ import annotations

import json

from app.models import PartRecord
from app.parsers.partzilla_product_parser import ProductParseInput, parse_partzilla_product_page
from app.price_forensics import (
    add_manual_validation,
    apply_price_evidence_to_observation,
    build_price_evidence,
)
from app.raw_price_signals import RawPriceRoleHint, discover_raw_price_signals
from app.schemas.product_observation import (
    AccessContext,
    ParseConfidence,
    PriceValidationStatus,
    SessionStatus,
)


def test_product_associated_structured_offer_price() -> None:
    bundle = _bundle(_json_ld_product(price="282.32"))

    signals = discover_raw_price_signals(html=bundle.html, visible_text=bundle.visible_text, observation=bundle.observation)

    assert _accepted_values(signals) == ["282.32"]
    assert signals[0].price_role_hint == RawPriceRoleHint.OFFER_PRICE


def test_price_specification_price_accepted() -> None:
    bundle = _bundle(_json_ld_product(price_specification="282.32"))
    signals = discover_raw_price_signals(html=bundle.html, visible_text=bundle.visible_text, observation=bundle.observation)

    assert _accepted_values(signals) == ["282.32"]


def test_unit_price_specification_price_accepted() -> None:
    bundle = _bundle(_json_ld_product(unit_price_specification="282.32"))
    signals = discover_raw_price_signals(html=bundle.html, visible_text=bundle.visible_text, observation=bundle.observation)

    assert _accepted_values(signals) == ["282.32"]


def test_aggregate_offer_low_and_high_price_accepted() -> None:
    bundle = _bundle(_json_ld_product(aggregate_low="100.00", aggregate_high="200.00"))
    signals = discover_raw_price_signals(html=bundle.html, visible_text=bundle.visible_text, observation=bundle.observation)

    assert _accepted_values(signals) == ["100.00", "200.00"]


def test_price_valid_until_rejected_and_not_parsed_as_202() -> None:
    bundle = _bundle(_json_ld_product(price="282.32", price_valid_until="2026-07-09T23:59:59Z"))
    signals = discover_raw_price_signals(html=bundle.html, visible_text=bundle.visible_text, observation=bundle.observation)

    rejected = [signal for signal in signals if signal.source_location.endswith("priceValidUntil")]
    assert rejected[0].rejection_reason == "unsupported_non_price_field"
    assert rejected[0].normalized_value is None
    assert "202" not in [signal.normalized_value for signal in signals]


def test_iso_date_and_timestamp_never_parse_as_money() -> None:
    for value in ("2026-07-09", "2026-07-09T23:59:59Z"):
        bundle = _bundle(_json_ld_product(price_valid_until=value))
        signals = discover_raw_price_signals(html=bundle.html, visible_text=bundle.visible_text, observation=bundle.observation)
        assert signals[0].normalized_value is None


def test_price_currency_rejected_as_price_value() -> None:
    bundle = _bundle(_json_ld_product(price="282.32", price_currency="USD"))
    signals = discover_raw_price_signals(html=bundle.html, visible_text=bundle.visible_text, observation=bundle.observation)
    rejected = [signal for signal in signals if signal.source_location.endswith("priceCurrency")]

    assert rejected[0].rejection_reason == "unsupported_non_price_field"


def test_structured_msrp() -> None:
    bundle = _bundle(_json_ld_product(msrp="380.98"))
    signals = discover_raw_price_signals(html=bundle.html, visible_text=bundle.visible_text, observation=bundle.observation)
    evidence = build_price_evidence(
        html=bundle.html,
        visible_text=bundle.visible_text,
        observation=bundle.observation,
        raw_price_signals=signals,
    )

    assert evidence.selected_msrp == "380.98"
    assert evidence.selected_selling_price is None


def test_structured_msrp_and_offer_price_equal() -> None:
    bundle = _bundle(_json_ld_product(price="282.32", msrp="282.32"))
    signals = discover_raw_price_signals(html=bundle.html, visible_text=bundle.visible_text, observation=bundle.observation)
    evidence = build_price_evidence(
        html=bundle.html,
        visible_text=bundle.visible_text,
        observation=bundle.observation,
        raw_price_signals=signals,
    )

    assert evidence.selected_msrp == "282.32"
    assert evidence.selected_selling_price == "282.32"


def test_structured_offer_price_with_no_msrp() -> None:
    bundle = _bundle(_json_ld_product(price="282.32"))
    signals = discover_raw_price_signals(html=bundle.html, visible_text=bundle.visible_text, observation=bundle.observation)
    evidence = build_price_evidence(
        html=bundle.html,
        visible_text=bundle.visible_text,
        observation=bundle.observation,
        raw_price_signals=signals,
    )

    assert evidence.selected_msrp is None
    assert evidence.selected_selling_price == "282.32"


def test_two_identical_selling_price_sources_deduplicated_with_corroboration() -> None:
    bundle = _bundle(_json_ld_product(price="282.32", price_specification="282.32"))
    signals = discover_raw_price_signals(html=bundle.html, visible_text=bundle.visible_text, observation=bundle.observation)
    evidence = build_price_evidence(
        html=bundle.html,
        visible_text=bundle.visible_text,
        observation=bundle.observation,
        raw_price_signals=signals,
    )
    selling_candidates = [candidate for candidate in evidence.price_candidates if candidate.candidate_role.value == "selling_price"]

    assert len(selling_candidates) == 1
    assert selling_candidates[0].corroboration_count == 2
    assert any(location.endswith("offers.price") for location in selling_candidates[0].source_locations)
    assert any(location.endswith("offers.priceSpecification.price") for location in selling_candidates[0].source_locations)


def test_conflicting_selling_price_values_preserved_and_warned() -> None:
    bundle = _bundle(_json_ld_product(price="282.32", price_specification="200.00"))
    signals = discover_raw_price_signals(html=bundle.html, visible_text=bundle.visible_text, observation=bundle.observation)
    evidence = build_price_evidence(
        html=bundle.html,
        visible_text=bundle.visible_text,
        observation=bundle.observation,
        raw_price_signals=signals,
    )

    assert evidence.selected_selling_price is None
    assert "conflicting_selling_price_signals" in evidence.parse_warnings
    assert sorted(
        candidate.normalized_value for candidate in evidence.price_candidates if candidate.candidate_role.value == "selling_price"
    ) == ["200.00", "282.32"]


def test_price_associated_with_wrong_part_number_is_rejected() -> None:
    bundle = _bundle(_json_ld_product(part_number="99999-9999", price="99.99"))
    signals = discover_raw_price_signals(html=bundle.html, visible_text=bundle.visible_text, observation=bundle.observation)

    assert signals[0].rejection_reason == "not_associated_with_requested_product"


def test_recommended_product_structured_price_is_rejected() -> None:
    bundle = _bundle(_json_ld_product(price="99.99", name="Recommended Widget", extra='"category":"recommended"'))
    signals = discover_raw_price_signals(html=bundle.html, visible_text=bundle.visible_text, observation=bundle.observation)

    assert signals[0].rejection_reason == "non_main_product_price_context"


def test_related_product_json_ld_price_is_rejected() -> None:
    html = _page(
        """
        <script type="application/ld+json">
        {"@type":"Product","sku":"12345","name":"Related item","offers":{"price":"22.22"}}
        </script>
        """
    )
    bundle = _bundle_from_page(html)
    signals = discover_raw_price_signals(html=bundle.html, visible_text=bundle.visible_text, observation=bundle.observation)

    assert signals[0].rejection_reason == "not_associated_with_requested_product"


def test_main_product_json_ld_offer_price_accepted() -> None:
    bundle = _bundle(_json_ld_product(price="282.32"))

    assert discover_raw_price_signals(html=bundle.html, visible_text=bundle.visible_text, observation=bundle.observation)[0].rejection_reason is None


def test_main_product_inline_product_state_offer_price_accepted() -> None:
    html = _page(
        """
        <script id="__NEXT_DATA__" type="application/json">
        {"props":{"product":{"partNumber":"41080-1514","name":"DISC","offerPrice":"282.32"}}}
        </script>
        """
    )
    bundle = _bundle_from_page(html)
    signals = discover_raw_price_signals(html=bundle.html, visible_text=bundle.visible_text, observation=bundle.observation)

    assert _accepted_values(signals) == ["282.32"]


def test_unknown_unassociated_raw_price_rejected() -> None:
    html = _page('<script type="application/ld+json">{"price":"42.00"}</script>')
    bundle = _bundle_from_page(html)
    signals = discover_raw_price_signals(html=bundle.html, visible_text=bundle.visible_text, observation=bundle.observation)

    assert signals[0].rejection_reason == "not_associated_with_requested_product"


def test_missing_authenticated_msrp_is_not_automatic_warning() -> None:
    bundle = _bundle(_json_ld_product(price="282.32"))
    bundle.observation.access_context = AccessContext.AUTHENTICATED_SESSION
    bundle.observation.session_status = SessionStatus.AUTHENTICATED
    signals = discover_raw_price_signals(html=bundle.html, visible_text=bundle.visible_text, observation=bundle.observation)
    evidence = build_price_evidence(
        html=bundle.html,
        visible_text=bundle.visible_text,
        observation=bundle.observation,
        raw_price_signals=signals,
    )
    apply_price_evidence_to_observation(bundle.observation, evidence)

    assert "msrp_not_found" not in bundle.observation.parse_warnings


def test_equal_public_msrp_and_authenticated_selling_price_allowed() -> None:
    bundle = _bundle(_json_ld_product(price="282.32"))
    signals = discover_raw_price_signals(html=bundle.html, visible_text=bundle.visible_text, observation=bundle.observation)
    evidence = build_price_evidence(
        html=bundle.html,
        visible_text=bundle.visible_text,
        observation=bundle.observation,
        raw_price_signals=signals,
    )

    assert evidence.selected_selling_price == "282.32"
    assert evidence.selected_msrp is None


def test_raw_signal_output_is_sanitized() -> None:
    bundle = _bundle(_json_ld_product(price="282.32", extra='"email":"customer@example.com","token":"SECRET"'))
    signal = discover_raw_price_signals(html=bundle.html, visible_text=bundle.visible_text, observation=bundle.observation)[0]

    assert "customer@example.com" not in signal.safe_context
    assert "SECRET" not in signal.safe_context


def test_no_account_data_enters_raw_signals() -> None:
    bundle = _bundle(_json_ld_product(price="282.32", extra='"account":"bob@example.com"'))
    serialized = json.dumps(
        [signal.to_json_dict() for signal in discover_raw_price_signals(html=bundle.html, visible_text=bundle.visible_text, observation=bundle.observation)]
    )

    assert "bob@example.com" not in serialized


def test_manual_validation_matches_structured_price() -> None:
    bundle = _bundle(_json_ld_product(price="282.32"))
    signals = discover_raw_price_signals(html=bundle.html, visible_text=bundle.visible_text, observation=bundle.observation)
    evidence = build_price_evidence(
        html=bundle.html,
        visible_text=bundle.visible_text,
        observation=bundle.observation,
        raw_price_signals=signals,
    )
    evidence = add_manual_validation(evidence, selling_price_input="282.32", msrp_input="none")

    assert evidence.price_validation_status == PriceValidationStatus.PARSER_MATCHES_MANUAL


def test_manual_validation_catches_incorrect_structured_price() -> None:
    bundle = _bundle(_json_ld_product(price="200.00"))
    signals = discover_raw_price_signals(html=bundle.html, visible_text=bundle.visible_text, observation=bundle.observation)
    evidence = build_price_evidence(
        html=bundle.html,
        visible_text=bundle.visible_text,
        observation=bundle.observation,
        raw_price_signals=signals,
    )
    evidence = add_manual_validation(evidence, selling_price_input="282.32", msrp_input="none")

    assert evidence.price_validation_status == PriceValidationStatus.PARSER_MISMATCH
    assert evidence.price_parse_confidence == ParseConfidence.LOW


class _Bundle:
    def __init__(self, html: str):
        self.html = html
        self.visible_text = "KAWASAKI OEM DISC | 41080-1514 Part #: 41080-1514 Manufacturer: KAWASAKI In Stock Quantity"
        self.observation = _observation(html, self.visible_text)


def _bundle(fragment: str) -> _Bundle:
    return _bundle_from_page(_page(fragment))


def _bundle_from_page(html: str) -> _Bundle:
    return _Bundle(html)


def _page(fragment: str) -> str:
    return f"""
    <html><body>
    <h1>KAWASAKI OEM DISC | 41080-1514</h1>
    <p>Part #: 41080-1514</p>
    <p>Manufacturer: KAWASAKI</p>
    {fragment}
    </body></html>
    """


def _json_ld_product(
    *,
    part_number: str = "41080-1514",
    name: str = "DISC",
    price: str | None = None,
    msrp: str | None = None,
    price_specification: str | None = None,
    unit_price_specification: str | None = None,
    aggregate_low: str | None = None,
    aggregate_high: str | None = None,
    price_valid_until: str | None = None,
    price_currency: str | None = None,
    extra: str = "",
) -> str:
    fields = [
        '"@type":"Product"',
        f'"sku":"{part_number}"',
        f'"mpn":"{part_number}"',
        f'"name":"KAWASAKI OEM {name}"',
    ]
    if price is not None:
        offer_fields = ['"@type":"Offer"', f'"price":"{price}"']
        if price_specification is not None:
            offer_fields.append(f'"priceSpecification":{{"@type":"PriceSpecification","price":"{price_specification}"}}')
        if unit_price_specification is not None:
            offer_fields.append(f'"unitPriceSpecification":{{"@type":"UnitPriceSpecification","price":"{unit_price_specification}"}}')
        if price_valid_until is not None:
            offer_fields.append(f'"priceValidUntil":"{price_valid_until}"')
        if price_currency is not None:
            offer_fields.append(f'"priceCurrency":"{price_currency}"')
        fields.append(f'"offers":{{{",".join(offer_fields)}}}')
    elif price_specification is not None:
        fields.append(f'"offers":{{"@type":"Offer","priceSpecification":{{"@type":"PriceSpecification","price":"{price_specification}"}}}}')
    elif unit_price_specification is not None:
        fields.append(f'"offers":{{"@type":"Offer","unitPriceSpecification":{{"@type":"UnitPriceSpecification","price":"{unit_price_specification}"}}}}')
    elif aggregate_low is not None or aggregate_high is not None:
        aggregate_fields = ['"@type":"AggregateOffer"']
        if aggregate_low is not None:
            aggregate_fields.append(f'"lowPrice":"{aggregate_low}"')
        if aggregate_high is not None:
            aggregate_fields.append(f'"highPrice":"{aggregate_high}"')
        fields.append(f'"offers":{{{",".join(aggregate_fields)}}}')
    elif price_valid_until is not None:
        fields.append(f'"offers":{{"@type":"Offer","priceValidUntil":"{price_valid_until}"}}')
    if msrp is not None:
        fields.append(f'"msrp":"{msrp}"')
    if extra:
        fields.append(extra)
    return f'<script type="application/ld+json">{{{",".join(fields)}}}</script>'


def _observation(html: str, visible_text: str):
    record = PartRecord(test_case_id="KAW-004", manufacturer="Kawasaki", oem_part_number="41080-1514")
    return parse_partzilla_product_page(
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
            detected_signals=[],
            checked_at="2026-07-08T00:00:00Z",
        )
    )


def _accepted_values(signals) -> list[str | None]:
    return [signal.normalized_value for signal in signals if signal.rejection_reason is None]
