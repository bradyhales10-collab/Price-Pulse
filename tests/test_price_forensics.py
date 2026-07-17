from __future__ import annotations

from decimal import Decimal

from app.models import PartRecord
from app.parsers.partzilla_product_parser import ProductParseInput, parse_partzilla_product_page
from app.price_forensics import (
    PriceCandidateRole,
    add_manual_validation,
    apply_price_evidence_to_observation,
    build_price_dom_debug,
    build_price_evidence,
)
from app.schemas.product_observation import ParseConfidence, PriceValidationStatus


def test_labeled_msrp_plus_separate_selling_price() -> None:
    evidence = _evidence(
        """
        <h1>KAWASAKI OEM DISC | 41080-1514</h1>
        <span>MSRP: $282.32</span>
        <div data-testid="productPrice">$219.99 Quantity Add to Cart</div>
        """
    )

    assert evidence.selected_msrp == "282.32"
    assert evidence.selected_selling_price == "219.99"
    assert evidence.price_parse_confidence == ParseConfidence.HIGH


def test_one_price_labeled_msrp_only_is_not_selling_price() -> None:
    evidence = _evidence(
        """
        <h1>KAWASAKI OEM DISC | 41080-1514</h1>
        <span>MSRP: $282.32</span>
        """
    )

    assert evidence.selected_msrp == "282.32"
    assert evidence.selected_selling_price is None
    assert "ambiguous_single_price_candidate" in evidence.parse_warnings


def test_one_price_beside_add_to_cart_with_no_msrp_is_selling_price() -> None:
    evidence = _evidence(
        """
        <h1>KAWASAKI OEM DISC | 41080-1514</h1>
        <div>$219.99 Quantity Add to Cart</div>
        """
    )

    assert evidence.selected_msrp is None
    assert evidence.selected_selling_price == "219.99"


def test_ambiguous_unlabeled_price_is_not_selling_price() -> None:
    observation = _observation(
        """
        <h1>KAWASAKI OEM DISC | 41080-1514</h1>
        <span>$282.32</span>
        """
    )
    evidence = build_price_evidence(html=observation.html, visible_text=observation.visible_text, observation=observation.product)
    apply_price_evidence_to_observation(observation.product, evidence)

    assert evidence.selected_selling_price is None
    assert observation.product.selling_price is None
    assert observation.product.price_visibility.value == "unknown"
    assert "ambiguous_single_price_candidate" in observation.product.parse_warnings


def test_msrp_and_selling_price_can_be_equal_with_distinct_role_evidence() -> None:
    evidence = _evidence(
        """
        <h1>KAWASAKI OEM DISC | 41080-1514</h1>
        <span>MSRP: $282.32</span>
        <div data-testid="productPrice">$282.32 Quantity Add to Cart</div>
        """
    )

    assert evidence.selected_msrp == "282.32"
    assert evidence.selected_selling_price == "282.32"
    roles = [candidate.candidate_role for candidate in evidence.price_candidates]
    assert roles.count(PriceCandidateRole.MSRP) == 1
    assert roles.count(PriceCandidateRole.SELLING_PRICE) == 1


def test_recommended_product_price_excluded() -> None:
    evidence = _evidence(
        """
        <h1>KAWASAKI OEM DISC | 41080-1514</h1>
        <span>MSRP: $282.32</span>
        <h2>Riders Also Bought</h2>
        <div data-testid="productPrice">$89.99</div>
        """
    )

    assert [candidate.normalized_value for candidate in evidence.price_candidates] == ["282.32"]


def test_accessory_price_excluded() -> None:
    evidence = _evidence(
        """
        <h1>KAWASAKI OEM DISC | 41080-1514</h1>
        <span>MSRP: $282.32</span>
        <h2>Accessories</h2>
        <div data-testid="productPrice">$12.99</div>
        """
    )

    assert [candidate.normalized_value for candidate in evidence.price_candidates] == ["282.32"]


def test_recently_viewed_price_excluded() -> None:
    evidence = _evidence(
        """
        <h1>KAWASAKI OEM DISC | 41080-1514</h1>
        <span>MSRP: $282.32</span>
        <h2>Recently Viewed</h2>
        <div data-testid="productPrice">$282.32</div>
        """
    )

    assert len(evidence.price_candidates) == 1


def test_cart_total_excluded() -> None:
    evidence = _evidence(
        """
        <h1>KAWASAKI OEM DISC | 41080-1514</h1>
        <span>MSRP: $282.32</span>
        <h2>Cart Total</h2>
        <div>$999.99</div>
        """
    )

    assert [candidate.normalized_value for candidate in evidence.price_candidates] == ["282.32"]


def test_price_candidate_context_is_sanitized_and_length_limited() -> None:
    evidence = _evidence(
        """
        <h1>KAWASAKI OEM DISC | 41080-1514</h1>
        <div data-testid="productPrice">customer@example.com extra extra extra extra extra extra extra extra extra extra extra extra extra extra extra extra extra extra extra extra $219.99 Quantity Add to Cart</div>
        """
    )

    context = evidence.price_candidates[0].visible_text_context
    assert "customer@example.com" not in context
    assert "[redacted-email]" in context
    assert len(context) <= 200


def test_duplicate_price_candidates_are_deduplicated() -> None:
    evidence = _evidence(
        """
        <h1>KAWASAKI OEM DISC | 41080-1514</h1>
        <span data-testid="productPrice">$219.99 Quantity Add to Cart</span>
        <span data-testid="productPrice">$219.99 Quantity Add to Cart</span>
        """
    )

    assert len(evidence.price_candidates) == 1


def test_ambiguous_single_price_does_not_become_selling_price() -> None:
    evidence = _evidence(
        """
        <h1>KAWASAKI OEM DISC | 41080-1514</h1>
        <span>$219.99</span>
        """
    )

    assert evidence.selected_selling_price is None
    assert evidence.price_parse_confidence == ParseConfidence.LOW


def test_manual_confirmation_matches_parser() -> None:
    evidence = add_manual_validation(
        _evidence(
            """
            <h1>KAWASAKI OEM DISC | 41080-1514</h1>
            <span>MSRP: $282.32</span>
            <span data-testid="productPrice">$219.99 Quantity Add to Cart</span>
            """
        ),
        selling_price_input="$219.99",
        msrp_input="$282.32",
    )

    assert evidence.price_validation_status == PriceValidationStatus.PARSER_MATCHES_MANUAL
    assert evidence.manual_validation is not None
    assert evidence.manual_validation.comparison == "match"


def test_manual_confirmation_differs_from_parser() -> None:
    evidence = add_manual_validation(
        _evidence(
            """
            <h1>KAWASAKI OEM DISC | 41080-1514</h1>
            <span>MSRP: $282.32</span>
            <span data-testid="productPrice">$219.99 Quantity Add to Cart</span>
            """
        ),
        selling_price_input="$200.00",
        msrp_input="$282.32",
    )

    assert evidence.price_validation_status == PriceValidationStatus.PARSER_MISMATCH
    assert "price_parser_manual_mismatch" in evidence.parse_warnings
    assert evidence.price_parse_confidence == ParseConfidence.LOW


def test_manual_confirmation_unclear() -> None:
    evidence = add_manual_validation(
        _evidence(
            """
            <h1>KAWASAKI OEM DISC | 41080-1514</h1>
            <span>MSRP: $282.32</span>
            <span data-testid="productPrice">$219.99 Quantity Add to Cart</span>
            """
        ),
        selling_price_input="unclear",
        msrp_input="$282.32",
    )

    assert evidence.price_validation_status == PriceValidationStatus.MANUAL_UNCLEAR


def test_manual_confirmation_never_overwrites_parsed_values() -> None:
    observation = _observation(
        """
        <h1>KAWASAKI OEM DISC | 41080-1514</h1>
        <span>MSRP: $282.32</span>
        <span data-testid="productPrice">$219.99 Quantity Add to Cart</span>
        """
    )
    evidence = build_price_evidence(html=observation.html, visible_text=observation.visible_text, observation=observation.product)
    evidence = add_manual_validation(evidence, selling_price_input="$200.00", msrp_input="$100.00")
    apply_price_evidence_to_observation(observation.product, evidence)

    assert observation.product.selling_price == Decimal("219.99")
    assert observation.product.msrp == Decimal("282.32")
    assert observation.product.price_validation_status == PriceValidationStatus.PARSER_MISMATCH


def test_manual_numeric_value_versus_parser_null_is_mismatch() -> None:
    evidence = add_manual_validation(_evidence("<h1>KAWASAKI OEM DISC | 41080-1514</h1>"), selling_price_input="282.32", msrp_input="none")

    assert evidence.manual_validation is not None
    assert evidence.manual_validation.field_comparisons["selling_price"]["comparison"] == "mismatch"
    assert evidence.manual_validation.field_comparisons["msrp"]["comparison"] == "match"
    assert evidence.price_validation_status == PriceValidationStatus.PARSER_MISMATCH


def test_manual_none_versus_parser_null_is_match() -> None:
    evidence = add_manual_validation(_evidence("<h1>KAWASAKI OEM DISC | 41080-1514</h1>"), selling_price_input="none", msrp_input="none")

    assert evidence.price_validation_status == PriceValidationStatus.PARSER_MATCHES_MANUAL


def test_manual_none_versus_parsed_numeric_is_mismatch() -> None:
    evidence = add_manual_validation(
        _evidence('<h1>KAWASAKI OEM DISC | 41080-1514</h1><div data-testid="productPrice">$219.99 Quantity Add to Cart</div>'),
        selling_price_input="none",
        msrp_input="none",
    )

    assert evidence.manual_validation is not None
    assert evidence.manual_validation.field_comparisons["selling_price"]["comparison"] == "mismatch"
    assert evidence.price_validation_status == PriceValidationStatus.PARSER_MISMATCH


def test_one_field_matches_and_one_field_mismatches_is_parser_mismatch() -> None:
    evidence = add_manual_validation(
        _evidence('<h1>KAWASAKI OEM DISC | 41080-1514</h1><span>MSRP: $282.32</span>'),
        selling_price_input="282.32",
        msrp_input="282.32",
    )

    assert evidence.manual_validation is not None
    assert evidence.manual_validation.field_comparisons["selling_price"]["comparison"] == "mismatch"
    assert evidence.manual_validation.field_comparisons["msrp"]["comparison"] == "match"
    assert evidence.price_validation_status == PriceValidationStatus.PARSER_MISMATCH


def test_contradictory_price_evidence_is_not_added_to_classification_evidence() -> None:
    observation = _observation(
        '<h1>KAWASAKI OEM DISC | 41080-1514</h1><div data-testid="productPrice">$219.99 Quantity Add to Cart</div>'
    )

    evidence_text = " ".join(observation.product.classification_evidence).lower()
    assert "msrp found" not in evidence_text
    assert "selling price found" not in evidence_text


def test_main_heading_and_purchase_panel_in_sibling_columns() -> None:
    evidence = _evidence(
        """
        <section data-testid="product-detail">
          <div class="identity"><h1>KAWASAKI OEM DISC | 41080-1514</h1><p>Manufacturer: KAWASAKI</p></div>
          <div class="purchase"><p>$282.32 Quantity Add to Cart</p></div>
        </section>
        """
    )

    assert evidence.primary_product_region["product_heading_inside_region"] is True
    assert evidence.selected_selling_price == "282.32"


def test_price_located_in_purchase_panel_sibling() -> None:
    evidence = _evidence(
        """
        <section data-testid="product-detail">
          <div><h1>KAWASAKI OEM DISC | 41080-1514</h1><p>Part #: 41080-1514</p></div>
          <aside><p>Our Price $282.32</p><p>Quantity 1 Add to Cart</p></aside>
        </section>
        """
    )

    assert evidence.selected_selling_price == "282.32"


def test_price_in_direct_text_node() -> None:
    evidence = _evidence(
        """
        <h1>KAWASAKI OEM DISC | 41080-1514</h1>
        $282.32 Quantity Add to Cart
        """
    )

    assert evidence.selected_selling_price == "282.32"


def test_price_embedded_in_longer_visible_string() -> None:
    evidence = _evidence(
        """
        <h1>KAWASAKI OEM DISC | 41080-1514</h1>
        <p>Our Price $282.32 Quantity 1 Add to Cart</p>
        """
    )

    assert evidence.selected_selling_price == "282.32"


def test_single_unlabeled_price_beside_quantity_and_purchase_action() -> None:
    evidence = _evidence(
        """
        <h1>KAWASAKI OEM DISC | 41080-1514</h1>
        <p>$282.32 Quantity Add to Cart</p>
        """
    )

    assert evidence.selected_selling_price == "282.32"
    assert evidence.price_parse_confidence == ParseConfidence.HIGH


def test_single_unrelated_price_outside_product_region_is_excluded() -> None:
    evidence = _evidence(
        """
        <h1>KAWASAKI OEM DISC | 41080-1514</h1>
        <h2>Recently Viewed</h2>
        <p>$282.32</p>
        """
    )

    assert evidence.price_candidates == []
    assert evidence.selected_selling_price is None


def test_msrp_absent_remains_null() -> None:
    evidence = _evidence(
        """
        <h1>KAWASAKI OEM DISC | 41080-1514</h1>
        <p>$282.32 Quantity Add to Cart</p>
        """
    )

    assert evidence.selected_msrp is None
    assert evidence.selected_selling_price == "282.32"


def test_debug_output_excludes_account_information() -> None:
    bundle = _observation(
        """
        <section data-testid="product-detail">
          <h1>KAWASAKI OEM DISC | 41080-1514</h1>
          <p>customer@example.com My Account $282.32 Quantity Add to Cart</p>
        </section>
        """
    )
    evidence = build_price_evidence(html=bundle.html, visible_text=bundle.visible_text, observation=bundle.product)
    debug = build_price_dom_debug(html=bundle.html, visible_text=bundle.visible_text, observation=bundle.product, evidence=evidence)
    serialized = str(debug).lower()

    assert "customer@example.com" not in serialized
    assert "my account" not in serialized


def test_debug_output_is_bounded_and_sanitized() -> None:
    long_text = " ".join(["extra"] * 600)
    bundle = _observation(f'<h1>KAWASAKI OEM DISC | 41080-1514</h1><p>{long_text} $282.32 Quantity Add to Cart token SECRET</p>')
    evidence = build_price_evidence(html=bundle.html, visible_text=bundle.visible_text, observation=bundle.product)
    debug = build_price_dom_debug(html=bundle.html, visible_text=bundle.visible_text, observation=bundle.product, evidence=evidence)

    assert len(debug["region_visible_text"]) <= 2000
    assert "SECRET" not in debug["region_visible_text"]


def test_price_mismatch_warning_propagates_to_observation() -> None:
    bundle = _observation("<h1>KAWASAKI OEM DISC | 41080-1514</h1>")
    evidence = build_price_evidence(html=bundle.html, visible_text=bundle.visible_text, observation=bundle.product)
    evidence = add_manual_validation(evidence, selling_price_input="282.32", msrp_input="none")
    apply_price_evidence_to_observation(bundle.product, evidence)

    assert "price_parser_manual_mismatch" in evidence.parse_warnings
    assert "price_parser_manual_mismatch" in bundle.product.parse_warnings


class _ObservationBundle:
    def __init__(self, html: str):
        self.html = html
        self.visible_text = _visible_text(html)
        self.product = _product_observation(html, self.visible_text)


def _evidence(body_html: str):
    bundle = _observation(body_html)
    return build_price_evidence(html=bundle.html, visible_text=bundle.visible_text, observation=bundle.product)


def _observation(body_html: str) -> _ObservationBundle:
    html = f"<html><body>{body_html}<p>Part #: 41080-1514</p><p>Manufacturer: KAWASAKI</p><p>In Stock</p><p>Quantity</p></body></html>"
    return _ObservationBundle(html)


def _product_observation(html: str, visible_text: str):
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


def _visible_text(html: str) -> str:
    import re

    return " ".join(re.sub(r"<[^>]+>", " ", html).split())
