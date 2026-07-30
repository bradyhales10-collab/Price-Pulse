from __future__ import annotations

import argparse
import csv
import sys
import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path

from playwright.sync_api import sync_playwright

from app.auth_session import mark_authenticated_context, require_auth_state
from app.browser_probe import detect_page_signals
from app.config import (
    DEFAULT_DATABASE_PATH,
    DEFAULT_VIEWPORT,
    OUTPUT_DIR,
    PARTZILLA_AUTH_STATE_PATH,
    ProbeSettings,
)
from app.models import PartRecord
from app.parsers.partzilla_product_parser import build_parse_input_from_probe, parse_partzilla_product_page
from app.price_forensics import apply_price_evidence_to_observation, build_price_evidence
from app.raw_price_signals import discover_raw_price_signals
from app.url_builder import build_partzilla_product_url

BENCHMARK_PARTS = ["34028-0327", "41080-1514", "14081-005", "K53001-240", "13270-1800"]
BENCHMARK_EXPECTATIONS_PATH = Path("data/validation/benchmark_expectations.csv")
HEAVY_RESOURCE_TYPES = {"image", "font", "media"}


@dataclass(frozen=True)
class BenchmarkRow:
    mode: str
    part_number: str
    elapsed_seconds: str
    http_status: int | None
    selling_price: str
    reference_price: str
    savings_percent: str
    price_display_type: str
    price_confidence: str
    warnings: str
    price_found: bool
    parse_succeeded: bool
    selling_price_matches_expected: bool
    correct_selling_price: bool
    correct_reference_price: str


@dataclass(frozen=True)
class BenchmarkExpectation:
    part_number: str
    expected_selling_price: Decimal
    expected_reference_price: Decimal | None


@dataclass(frozen=True)
class ModeSummary:
    mode: str
    parts_attempted: int
    prices_found: int
    correct_selling_prices: int
    selling_price_accuracy_percent: Decimal
    expected_reference_prices: int
    correct_reference_prices: int
    reference_price_accuracy_percent: Decimal | None
    total_elapsed_seconds: Decimal
    average_seconds_per_part: Decimal
    warnings: int
    blocks: int
    challenges: int
    high_confidence_prices: int


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Benchmark safe sequential Partzilla collection modes.")
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE_PATH)
    parser.add_argument("--mode", choices=["full_browser", "lightweight_browser", "authenticated_html", "all"], default="all")
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR / "benchmarks")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    require_auth_state(PARTZILLA_AUTH_STATE_PATH)
    expectations = load_benchmark_expectations()
    modes = ["full_browser", "lightweight_browser", "authenticated_html"] if args.mode == "all" else [args.mode]
    rows: list[BenchmarkRow] = []
    started = time.perf_counter()
    with sync_playwright() as playwright:
        for mode in modes:
            rows.extend(_run_mode(playwright, mode, expectations))
    elapsed = time.perf_counter() - started
    args.output_dir.mkdir(parents=True, exist_ok=True)
    _write_results(args.output_dir / "benchmark_results.csv", rows)
    _write_review(args.output_dir / "benchmark_review.txt", rows, elapsed)
    print(f"Benchmark rows: {len(rows)}")
    print(f"Output: {args.output_dir}")
    return 0


def load_benchmark_expectations(path: Path = BENCHMARK_EXPECTATIONS_PATH) -> dict[str, BenchmarkExpectation]:
    with path.open(newline="", encoding="utf-8") as file:
        rows = csv.DictReader(file)
        return {
            row["part_number"]: BenchmarkExpectation(
                part_number=row["part_number"],
                expected_selling_price=Decimal(row["expected_selling_price"]),
                expected_reference_price=Decimal(row["expected_reference_price"]) if row["expected_reference_price"] else None,
            )
            for row in rows
        }


def _run_mode(playwright, mode: str, expectations: dict[str, BenchmarkExpectation]) -> list[BenchmarkRow]:
    settings = ProbeSettings(headless=False, timeout=30000)
    browser = playwright.chromium.launch(headless=False)
    context = browser.new_context(storage_state=str(PARTZILLA_AUTH_STATE_PATH), viewport=DEFAULT_VIEWPORT)
    if mode == "lightweight_browser":
        context.route("**/*", lambda route: route.abort() if route.request.resource_type in HEAVY_RESOURCE_TYPES else route.continue_())
    page = context.new_page()
    rows = []
    for part_number in BENCHMARK_PARTS:
        started = time.perf_counter()
        status = None
        html = ""
        text = ""
        title = None
        final_url = None
        url = build_partzilla_product_url("Kawasaki", part_number)
        try:
            response = page.goto(url, wait_until="domcontentloaded", timeout=settings.timeout)
            status = response.status if response is not None else None
            if status in {401, 403, 429}:
                break
            if mode != "authenticated_html":
                page.wait_for_timeout(1000)
            html = page.content()
            text = page.locator("body").inner_text(timeout=5000) if page.locator("body").count() else ""
            title = page.title()
            final_url = page.url
        except Exception as exc:
            text = str(exc)
        rows.append(_parse_benchmark_row(mode, part_number, url, final_url, status, title, html, text, time.perf_counter() - started, expectations))
        if rows[-1].warnings in {"blocked", "challenge", "authentication_lost"}:
            break
    context.close()
    browser.close()
    return rows


def _parse_benchmark_row(mode: str, part_number: str, requested_url: str, final_url: str | None, status: int | None, title: str | None, html: str, text: str, elapsed: float, expectations: dict[str, BenchmarkExpectation] | None = None) -> BenchmarkRow:
    record = PartRecord(test_case_id="", manufacturer="Kawasaki", oem_part_number=part_number)
    observation = parse_partzilla_product_page(
        build_parse_input_from_probe(
            record=record,
            html=html,
            visible_text=text,
            final_url=final_url,
            http_status=status,
            page_title=title,
            navigation_succeeded=status is not None,
            exception_message=None,
            detected_signals=detect_page_signals(text=text, html=html),
        )
    )
    mark_authenticated_context(observation, auth_state_loaded=True)
    signals = discover_raw_price_signals(html=html, visible_text=text, observation=observation)
    evidence = build_price_evidence(html=html, visible_text=text, observation=observation, raw_price_signals=signals)
    apply_price_evidence_to_observation(observation, evidence)
    selling = str(observation.selling_price) if observation.selling_price is not None else ""
    reference = str(observation.reference_price) if observation.reference_price is not None else ""
    expectation = (expectations or load_benchmark_expectations()).get(part_number)
    selling_matches = _decimal_matches(selling, expectation.expected_selling_price) if expectation else False
    reference_score = _reference_score(reference, expectation)
    parse_succeeded = observation.page_classification.value == "normal_product" and observation.session_status.value == "authenticated"
    return BenchmarkRow(
        mode=mode,
        part_number=part_number,
        elapsed_seconds=f"{elapsed:.3f}",
        http_status=status,
        selling_price=selling,
        reference_price=reference,
        savings_percent=str(observation.savings_percent) if observation.savings_percent is not None else "",
        price_display_type=observation.price_display_type.value,
        price_confidence=observation.price_parse_confidence.value,
        warnings="; ".join(observation.parse_warnings),
        price_found=bool(selling),
        parse_succeeded=parse_succeeded,
        selling_price_matches_expected=selling_matches,
        correct_selling_price=bool(selling) and selling_matches,
        correct_reference_price=reference_score,
    )


def _decimal_matches(actual: str, expected: Decimal) -> bool:
    if not actual:
        return False
    try:
        return Decimal(actual) == expected
    except InvalidOperation:
        return False


def _reference_score(actual: str, expectation: BenchmarkExpectation | None) -> str:
    if expectation is None or expectation.expected_reference_price is None:
        return "not_applicable"
    return "true" if _decimal_matches(actual, expectation.expected_reference_price) else "false"


def _write_results(path: Path, rows: list[BenchmarkRow]) -> None:
    with path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=list(BenchmarkRow.__dataclass_fields__))
        writer.writeheader()
        writer.writerows([row.__dict__ for row in rows])


def _write_review(path: Path, rows: list[BenchmarkRow], elapsed: float) -> None:
    summaries = summarize_modes(rows)
    recommendation = recommend_mode(summaries)
    lines = ["PARTZILLA COLLECTION BENCHMARK", f"Total elapsed seconds: {elapsed:.3f}", ""]
    for summary in summaries:
        lines.extend(
            [
                summary.mode,
                f"Parts attempted: {summary.parts_attempted}",
                f"Prices found: {summary.prices_found}",
                f"Correct selling prices: {summary.correct_selling_prices}",
                f"Selling-price accuracy: {summary.selling_price_accuracy_percent}%",
                f"Expected reference prices: {summary.expected_reference_prices}",
                f"Correct reference prices: {summary.correct_reference_prices}",
                "Reference-price accuracy: not_applicable" if summary.reference_price_accuracy_percent is None else f"Reference-price accuracy: {summary.reference_price_accuracy_percent}%",
                f"Total elapsed seconds: {summary.total_elapsed_seconds}",
                f"Average seconds per part: {summary.average_seconds_per_part}",
                f"Warnings: {summary.warnings}",
                f"Blocks: {summary.blocks}",
                f"Challenges: {summary.challenges}",
                f"High-confidence prices: {summary.high_confidence_prices}",
                "",
            ]
        )
    lines.extend(
        [
            f"Recommended production mode: {recommendation.mode if recommendation else 'none'}",
            "Recommendation rule: selling-price accuracy first, then access failures, confidence, then speed.",
        ]
    )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def summarize_modes(rows: list[BenchmarkRow]) -> list[ModeSummary]:
    summaries: list[ModeSummary] = []
    for mode in sorted({row.mode for row in rows}):
        mode_rows = [row for row in rows if row.mode == mode]
        total = sum(Decimal(row.elapsed_seconds) for row in mode_rows)
        attempted = len(mode_rows)
        expected_references = [row for row in mode_rows if row.correct_reference_price != "not_applicable"]
        correct_references = [row for row in expected_references if row.correct_reference_price == "true"]
        summaries.append(
            ModeSummary(
                mode=mode,
                parts_attempted=attempted,
                prices_found=sum(1 for row in mode_rows if row.price_found),
                correct_selling_prices=sum(1 for row in mode_rows if row.correct_selling_price),
                selling_price_accuracy_percent=_percent(sum(1 for row in mode_rows if row.correct_selling_price), attempted),
                expected_reference_prices=len(expected_references),
                correct_reference_prices=len(correct_references),
                reference_price_accuracy_percent=_percent(len(correct_references), len(expected_references)) if expected_references else None,
                total_elapsed_seconds=total.quantize(Decimal("0.001")),
                average_seconds_per_part=(total / Decimal(attempted)).quantize(Decimal("0.001")) if attempted else Decimal("0.000"),
                warnings=sum(1 for row in mode_rows if row.warnings),
                blocks=sum(1 for row in mode_rows if row.http_status in {401, 403, 429}),
                challenges=sum(1 for row in mode_rows if "challenge" in row.warnings.lower()),
                high_confidence_prices=sum(1 for row in mode_rows if row.price_confidence == "high"),
            )
        )
    return summaries


def recommend_mode(summaries: list[ModeSummary]) -> ModeSummary | None:
    if not summaries:
        return None
    return sorted(
        summaries,
        key=lambda summary: (
            summary.selling_price_accuracy_percent,
            -summary.blocks,
            -summary.challenges,
            summary.high_confidence_prices,
            summary.correct_selling_prices,
            -summary.average_seconds_per_part,
        ),
        reverse=True,
    )[0]


def _percent(numerator: int, denominator: int) -> Decimal:
    if denominator == 0:
        return Decimal("0.0")
    return (Decimal(numerator) / Decimal(denominator) * Decimal("100")).quantize(Decimal("0.1"))


if __name__ == "__main__":
    sys.exit(main())
