from __future__ import annotations

from pathlib import Path

import pytest

from app.input_loader import PartNotFoundError, find_part_record, load_parts_csv

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def test_loads_valid_csv() -> None:
    result = load_parts_csv(FIXTURES_DIR / "valid_parts.csv")

    assert len(result.records) == 1
    assert result.invalid_rows == []
    assert result.records[0].manufacturer == "Kawasaki"
    assert result.records[0].oem_part_number == "13270-1800"


def test_rejects_blank_part_number_without_crashing() -> None:
    result = load_parts_csv(FIXTURES_DIR / "blank_part_number.csv")

    assert result.records == []
    assert len(result.invalid_rows) == 1
    assert result.invalid_rows[0].reason == "Blank OEM part number"


def test_rejects_blank_manufacturer_without_crashing() -> None:
    result = load_parts_csv(FIXTURES_DIR / "blank_manufacturer.csv")

    assert result.records == []
    assert len(result.invalid_rows) == 1
    assert result.invalid_rows[0].reason == "Blank manufacturer"


def test_preserves_complex_part_numbers() -> None:
    part_numbers = ["41080-0729-11H", "K53001-240", "KMT4X7-3-4", "46092-S013", "3099"]

    result = load_parts_csv(FIXTURES_DIR / "complex_part_numbers.csv")

    assert [record.oem_part_number for record in result.records] == part_numbers


def test_finds_requested_part_number() -> None:
    result = load_parts_csv(FIXTURES_DIR / "lookup_parts.csv")

    record = find_part_record(result.records, "KMT4X7-3-4")

    assert record.test_case_id == "KAW-002"


def test_missing_requested_part_number_raises() -> None:
    result = load_parts_csv(FIXTURES_DIR / "valid_parts.csv")

    with pytest.raises(PartNotFoundError):
        find_part_record(result.records, "NOPE")
