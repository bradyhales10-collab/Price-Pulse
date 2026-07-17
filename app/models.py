from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class PartRecord:
    test_case_id: str
    manufacturer: str
    oem_part_number: str
    search_observed_product_name: str = ""
    search_observed_msrp: str = ""
    expected_partzilla_url: str = ""
    test_purpose: str = ""
    verified_date: str = ""
    source_url: str = ""


@dataclass(frozen=True)
class InvalidRow:
    row_number: int
    reason: str
    row: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class LoadResult:
    records: list[PartRecord]
    invalid_rows: list[InvalidRow]

    @property
    def has_invalid_rows(self) -> bool:
        return bool(self.invalid_rows)


@dataclass(frozen=True)
class ProbeDiagnostics:
    test_case_id: str | None
    manufacturer: str
    oem_part_number: str
    requested_url: str
    final_url: str | None
    http_status: int | None
    page_title: str | None
    detected_signals: list[str]
    timestamp: str
    navigation_succeeded: bool
    exception_message: str | None
    screenshot_path: str | None
    html_path: str | None
