from __future__ import annotations

import csv
from decimal import Decimal
from pathlib import Path

from app.authenticated_validation import update_authenticated_summary, write_authenticated_review
from app.price_forensics import PriceCandidate, PriceCandidateRole, PriceCandidateSourceType, PriceEvidence
from app.schemas.product_observation import (
    AccessContext,
    AvailabilityStatus,
    PageClassification,
    ParseConfidence,
    PriceValidationStatus,
    PriceVisibility,
    ProductObservation,
    SessionStatus,
)
from app.validation import load_validation_manifest

TEST_OUTPUT_DIR = Path("data/output/test-artifacts")


def test_authenticated_summary_updates_without_duplicate_rows() -> None:
    parts = load_validation_manifest(Path("data/validation/authenticated_validation_parts.csv"))
    part = parts[0]
    TEST_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    summary_path = TEST_OUTPUT_DIR / "authenticated_summary.csv"

    update_authenticated_summary(summary_path, part, _observation("282.32"), _evidence("282.32"), TEST_OUTPUT_DIR / "first.json")
    update_authenticated_summary(summary_path, part, _observation("200.00"), _evidence("200.00"), TEST_OUTPUT_DIR / "second.json")

    with summary_path.open("r", newline="", encoding="utf-8") as csv_file:
        rows = list(csv.DictReader(csv_file))

    assert len(rows) == 1
    assert rows[0]["selling_price"] == "200.00"
    assert rows[0]["observation_json_path"].endswith("second.json")


def test_authenticated_validation_review_generation() -> None:
    parts = load_validation_manifest(Path("data/validation/authenticated_validation_parts.csv"))
    part = parts[0]
    TEST_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    summary_path = TEST_OUTPUT_DIR / "authenticated_review_summary.csv"
    review_path = TEST_OUTPUT_DIR / "authenticated_review.txt"
    update_authenticated_summary(summary_path, part, _observation("282.32"), _evidence("282.32"), TEST_OUTPUT_DIR / "obs.json")

    write_authenticated_review(review_path, parts, summary_path)

    content = review_path.read_text(encoding="utf-8")
    assert "PART 1 OF 5" in content
    assert "Session status: authenticated" in content
    assert "PART 2 OF 5" in content
    assert "Status: NOT YET RUN" in content
    assert "Total cases: 5" in content
    assert "Completed: 1" in content


def _observation(price: str) -> ProductObservation:
    return ProductObservation(
        test_case_id="KAW-AUTH-001",
        manufacturer="Kawasaki",
        oem_part_number="41080-1514",
        observed_part_number="41080-1514",
        requested_url="https://www.partzilla.com/product/kawasaki/41080-1514",
        final_url="https://www.partzilla.com/product/kawasaki/41080-1514",
        canonical_url="https://www.partzilla.com/product/kawasaki/41080-1514",
        http_status=200,
        page_title="KAWASAKI OEM DISC - 41080-1514 | partzilla.com",
        page_classification=PageClassification.NORMAL_PRODUCT,
        price_visibility=PriceVisibility.VISIBLE,
        classification_confidence=ParseConfidence.HIGH,
        classification_evidence=["HTTP 200"],
        product_name="DISC",
        manufacturer_display="KAWASAKI",
        msrp_raw=None,
        msrp=None,
        selling_price_raw=f"${price}",
        selling_price=Decimal(price),
        availability_raw="Ships in 3 to 4 days",
        availability_status=AvailabilityStatus.SHIPS_IN,
        shipping_estimate="3 to 4 days",
        access_context=AccessContext.AUTHENTICATED_SESSION,
        session_status=SessionStatus.AUTHENTICATED,
        superseded_by_raw=None,
        supersession_detected=False,
        price_parse_confidence=ParseConfidence.HIGH,
        price_validation_status=PriceValidationStatus.NOT_MANUALLY_VALIDATED,
        parse_confidence=ParseConfidence.HIGH,
        parse_warnings=[],
        checked_at="2026-07-08T00:00:00Z",
    )


def _evidence(price: str) -> PriceEvidence:
    candidate = PriceCandidate(
        raw_text=f"${price}",
        normalized_value=price,
        source_type=PriceCandidateSourceType.STRUCTURED_PRODUCT_DATA,
        visible_text_context="structured product offer",
        nearby_label="offer_price",
        element_tag=None,
        stable_attributes={},
        relative_location="json_ld.script_1.offers.price",
        candidate_role=PriceCandidateRole.SELLING_PRICE,
        candidate_confidence=ParseConfidence.HIGH,
        source_locations=["json_ld.script_1.offers.price", "json_ld.script_1.offers.priceSpecification.price"],
        corroboration_count=2,
    )
    return PriceEvidence(
        oem_part_number="41080-1514",
        product_name="DISC",
        timestamp="2026-07-08T00:00:00Z",
        primary_product_container={},
        primary_product_region={},
        candidate_discovery_methods_attempted=[],
        price_candidates=[candidate],
        selected_msrp=None,
        selected_selling_price=price,
        decision_explanation=[],
        price_parse_confidence=ParseConfidence.HIGH,
    )
