from __future__ import annotations

from decimal import Decimal
from pathlib import Path
import csv

import validate_step3
from app.schemas.product_observation import (
    AccessContext,
    AvailabilityStatus,
    PageClassification,
    ParseConfidence,
    PriceVisibility,
    PriceValidationStatus,
    ProductObservation,
    SessionStatus,
)
from app.validation import (
    find_validation_part,
    load_validation_manifest,
    update_validation_summary,
    write_validation_review,
)


def observation(part_number: str = "41080-1514", warnings: list[str] | None = None) -> ProductObservation:
    return ProductObservation(
        test_case_id="KAW-004",
        manufacturer="Kawasaki",
        oem_part_number=part_number,
        observed_part_number=part_number,
        requested_url=f"https://www.partzilla.com/product/kawasaki/{part_number}",
        final_url=f"https://www.partzilla.com/product/kawasaki/{part_number}?titan_sku={part_number}",
        canonical_url=f"https://www.partzilla.com/product/kawasaki/{part_number}",
        http_status=200,
        page_title=f"KAWASAKI OEM DISC - {part_number} | partzilla.com",
        page_classification=PageClassification.NORMAL_PRODUCT,
        price_visibility=PriceVisibility.SIGN_IN_REQUIRED,
        classification_confidence=ParseConfidence.HIGH,
        classification_evidence=["HTTP 200"],
        product_name="DISC",
        manufacturer_display="KAWASAKI",
        msrp_raw="$282.32",
        msrp=Decimal("282.32"),
        selling_price_raw=None,
        selling_price=None,
        availability_raw="Ships in 3 to 4 days",
        availability_status=AvailabilityStatus.SHIPS_IN,
        shipping_estimate="3 to 4 days",
        access_context=AccessContext.PUBLIC,
        session_status=SessionStatus.UNKNOWN,
        superseded_by_raw=None,
        supersession_detected=False,
        price_parse_confidence=ParseConfidence.LOW,
        price_validation_status=PriceValidationStatus.NOT_MANUALLY_VALIDATED,
        parse_confidence=ParseConfidence.HIGH,
        parse_warnings=warnings or [],
        checked_at="2026-07-08T00:00:00Z",
    )


def test_validation_manifest_loading() -> None:
    parts = load_validation_manifest(Path("data/validation/step3_validation_parts.csv"))

    assert [part.oem_part_number for part in parts] == [
        "41080-1514",
        "55061-5438-739",
        "K53001-240",
        "14081-005",
        "92071-2128",
    ]


def test_invalid_part_rejected_by_validate_step3(monkeypatch) -> None:
    called = False

    def fail_if_called(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("inspect should not be called")

    monkeypatch.setattr(validate_step3, "inspect_validation_part", fail_if_called)
    monkeypatch.setattr(validate_step3.sys, "argv", ["validate_step3.py", "--part-number", "NOPE"])

    assert validate_step3.main() == 1
    assert called is False


TEST_OUTPUT_DIR = Path("data/output/test-artifacts")


def test_validation_summary_update_without_duplicate_rows() -> None:
    parts = load_validation_manifest(Path("data/validation/step3_validation_parts.csv"))
    part = find_validation_part(parts, "41080-1514")
    TEST_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    summary_path = TEST_OUTPUT_DIR / "summary_no_duplicates.csv"

    update_validation_summary(summary_path, part, observation(), TEST_OUTPUT_DIR / "first.json")
    update_validation_summary(
        summary_path,
        part,
        observation(warnings=["msrp_not_found"]),
        TEST_OUTPUT_DIR / "second.json",
    )

    with summary_path.open("r", newline="", encoding="utf-8") as csv_file:
        rows = list(csv.DictReader(csv_file))

    assert len(rows) == 1
    assert rows[0]["oem_part_number"] == "41080-1514"
    assert rows[0]["observation_json_path"].endswith("second.json")
    assert rows[0]["parse_warnings"] == "msrp_not_found"


def test_validation_review_report_generation() -> None:
    parts = load_validation_manifest(Path("data/validation/step3_validation_parts.csv"))
    part = find_validation_part(parts, "41080-1514")
    TEST_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    summary_path = TEST_OUTPUT_DIR / "summary_review.csv"
    review_path = TEST_OUTPUT_DIR / "review.txt"
    update_validation_summary(summary_path, part, observation(), TEST_OUTPUT_DIR / "observation.json")

    write_validation_review(review_path, parts, summary_path)

    report = review_path.read_text(encoding="utf-8")
    assert "PART 1 OF 5" in report
    assert "Page classification: normal_product" in report
    assert "PART 2 OF 5" in report
    assert "Status: NOT YET RUN" in report
    assert "Completed: 1" in report
    assert "Normal products: 1" in report
