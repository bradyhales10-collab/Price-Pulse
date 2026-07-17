from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Protocol

from app.models import PartRecord


@dataclass(frozen=True)
class CompetitorCapabilities:
    requires_login: bool
    supports_public_price: bool
    supports_direct_part_url: bool
    status: str
    legal_review_status: str = "review_needed"


@dataclass
class CompetitorObservation:
    competitor_key: str
    manufacturer: str
    oem_part_number: str
    observed_part_number: str | None = None
    product_name: str | None = None
    canonical_url: str | None = None
    http_status: int | None = None
    page_classification: str = "unknown"
    session_status: str = "unknown"
    price_visibility: str = "unknown"
    selling_price: Decimal | None = None
    reference_price: Decimal | None = None
    savings_percent: int | None = None
    savings_amount: Decimal | None = None
    price_display_type: str = "unknown"
    selling_price_confidence: str = "low"
    reference_price_confidence: str = "low"
    availability_raw: str | None = None
    availability_status: str = "unknown"
    supersession_detected: bool = False
    superseded_by_raw: str | None = None
    warnings: list[str] = field(default_factory=list)
    parser_version: str = "1"
    raw_evidence_summary: dict[str, Any] = field(default_factory=dict)
    parse_confidence: str = "low"

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "competitor_key": self.competitor_key,
            "manufacturer": self.manufacturer,
            "oem_part_number": self.oem_part_number,
            "observed_part_number": self.observed_part_number,
            "product_name": self.product_name,
            "canonical_url": self.canonical_url,
            "http_status": self.http_status,
            "page_classification": self.page_classification,
            "session_status": self.session_status,
            "price_visibility": self.price_visibility,
            "selling_price": _decimal(self.selling_price),
            "reference_price": _decimal(self.reference_price),
            "savings_percent": self.savings_percent,
            "savings_amount": _decimal(self.savings_amount),
            "price_display_type": self.price_display_type,
            "selling_price_confidence": self.selling_price_confidence,
            "reference_price_confidence": self.reference_price_confidence,
            "availability_raw": self.availability_raw,
            "availability_status": self.availability_status,
            "supersession_detected": self.supersession_detected,
            "superseded_by_raw": self.superseded_by_raw,
            "warnings": self.warnings,
            "parser_version": self.parser_version,
            "raw_evidence_summary": self.raw_evidence_summary,
            "parse_confidence": self.parse_confidence,
        }


class CompetitorAdapter(Protocol):
    competitor_key: str
    display_name: str
    supported_manufacturers: tuple[str, ...]
    capabilities: CompetitorCapabilities

    @property
    def requires_login(self) -> bool: ...

    @property
    def supports_public_price(self) -> bool: ...

    @property
    def supports_direct_part_url(self) -> bool: ...

    def build_product_url(self, product: PartRecord) -> str: ...

    def parse_product_page(self, html: str, product: PartRecord, *, visible_text: str = "", final_url: str | None = None, http_status: int | None = None) -> CompetitorObservation: ...

    def normalize_availability(self, raw: str | None) -> str: ...

    def normalize_supersession(self, raw: str | None) -> tuple[bool, str | None]: ...


def _decimal(value: Decimal | None) -> str | None:
    return format(value, "f") if value is not None else None
