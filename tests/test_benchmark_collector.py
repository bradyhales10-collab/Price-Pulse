from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from benchmark_collector import (
    BENCHMARK_EXPECTATIONS_PATH,
    BenchmarkExpectation,
    BenchmarkRow,
    _decimal_matches,
    _reference_score,
    load_benchmark_expectations,
    recommend_mode,
    summarize_modes,
)


def test_non_null_wrong_price_is_not_correct() -> None:
    expectation = BenchmarkExpectation("14081-005", Decimal("34.94"), Decimal("37.71"))
    row = _row("authenticated_html", "14081-005", selling_price="37.71", expectation=expectation)

    assert row.price_found is True
    assert row.selling_price_matches_expected is False
    assert row.correct_selling_price is False


def test_reference_price_is_not_accepted_as_selling_price() -> None:
    expectation = BenchmarkExpectation("13270-1800", Decimal("0.79"), Decimal("0.85"))
    row = _row("authenticated_html", "13270-1800", selling_price="0.85", reference_price="0.85", expectation=expectation)

    assert row.correct_selling_price is False
    assert row.correct_reference_price == "true"


def test_null_price_is_false() -> None:
    expectation = BenchmarkExpectation("34028-0327", Decimal("32.69"), Decimal("37.30"))
    row = _row("authenticated_html", "34028-0327", selling_price="", expectation=expectation)

    assert row.price_found is False
    assert row.correct_selling_price is False


def test_exact_decimal_selling_price_match_is_true() -> None:
    assert _decimal_matches("32.69", Decimal("32.69")) is True
    assert _decimal_matches("32.690", Decimal("32.69")) is True
    assert _decimal_matches("32.68", Decimal("32.69")) is False


def test_authenticated_html_known_discount_cases_score_false() -> None:
    expectations = load_benchmark_expectations()

    assert _row("authenticated_html", "14081-005", selling_price="37.71", expectation=expectations["14081-005"]).correct_selling_price is False
    assert _row("authenticated_html", "13270-1800", selling_price="0.85", expectation=expectations["13270-1800"]).correct_selling_price is False


def test_full_browser_benchmark_rows_score_five_of_five() -> None:
    expectations = load_benchmark_expectations()
    rows = [
        _row("full_browser", "34028-0327", selling_price="32.69", reference_price="37.30", expectation=expectations["34028-0327"]),
        _row("full_browser", "41080-1514", selling_price="282.32", expectation=expectations["41080-1514"]),
        _row("full_browser", "14081-005", selling_price="34.94", reference_price="37.71", expectation=expectations["14081-005"]),
        _row("full_browser", "K53001-240", selling_price="340.06", expectation=expectations["K53001-240"]),
        _row("full_browser", "13270-1800", selling_price="0.79", reference_price="0.85", expectation=expectations["13270-1800"]),
    ]
    summary = summarize_modes(rows)[0]

    assert summary.correct_selling_prices == 5
    assert summary.selling_price_accuracy_percent == Decimal("100.0")
    assert summary.correct_reference_prices == 3


def test_recommendation_prioritizes_accuracy_over_speed() -> None:
    rows = [
        _row("full_browser", "34028-0327", selling_price="32.69", elapsed="2.000", expectation=BenchmarkExpectation("34028-0327", Decimal("32.69"), Decimal("37.30"))),
        _row("authenticated_html", "34028-0327", selling_price="", elapsed="0.500", expectation=BenchmarkExpectation("34028-0327", Decimal("32.69"), Decimal("37.30"))),
    ]

    assert recommend_mode(summarize_modes(rows)).mode == "full_browser"


def test_faster_inaccurate_mode_is_not_recommended() -> None:
    expectations = load_benchmark_expectations()
    rows = [
        _row("full_browser", "14081-005", selling_price="34.94", elapsed="1.900", expectation=expectations["14081-005"]),
        _row("authenticated_html", "14081-005", selling_price="37.71", elapsed="1.000", expectation=expectations["14081-005"]),
    ]

    assert recommend_mode(summarize_modes(rows)).mode == "full_browser"


def test_expected_benchmark_values_live_in_manifest_not_parser() -> None:
    expectations = load_benchmark_expectations()

    assert BENCHMARK_EXPECTATIONS_PATH == Path("data/validation/benchmark_expectations.csv")
    assert expectations["13270-1800"].expected_selling_price == Decimal("0.79")
    assert _reference_score("", expectations["41080-1514"]) == "not_applicable"


def _row(
    mode: str,
    part_number: str,
    *,
    selling_price: str,
    expectation: BenchmarkExpectation,
    reference_price: str = "",
    elapsed: str = "1.000",
) -> BenchmarkRow:
    matches = _decimal_matches(selling_price, expectation.expected_selling_price)
    return BenchmarkRow(
        mode=mode,
        part_number=part_number,
        elapsed_seconds=elapsed,
        http_status=200,
        selling_price=selling_price,
        reference_price=reference_price,
        savings_percent="",
        price_display_type="regular",
        price_confidence="high",
        warnings="",
        price_found=bool(selling_price),
        parse_succeeded=True,
        selling_price_matches_expected=matches,
        correct_selling_price=bool(selling_price) and matches,
        correct_reference_price=_reference_score(reference_price, expectation),
    )
