from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Any


class PageClassification(str, Enum):
    NORMAL_PRODUCT = "normal_product"
    BLOCKED = "blocked"
    CHALLENGE = "challenge"
    NOT_FOUND = "not_found"
    NAVIGATION_ERROR = "navigation_error"
    UNKNOWN = "unknown"


class PriceVisibility(str, Enum):
    VISIBLE = "visible"
    SIGN_IN_REQUIRED = "sign_in_required"
    NOT_PRESENT = "not_present"
    UNKNOWN = "unknown"


class AccessContext(str, Enum):
    PUBLIC = "public"
    AUTHENTICATED_SESSION = "authenticated_session"


class SessionStatus(str, Enum):
    AUTHENTICATED = "authenticated"
    AUTHENTICATION_REQUIRED = "authentication_required"
    EXPIRED_OR_INVALID = "expired_or_invalid"
    BLOCKED = "blocked"
    CHALLENGE = "challenge"
    NAVIGATION_ERROR = "navigation_error"
    UNKNOWN = "unknown"


class AvailabilityStatus(str, Enum):
    IN_STOCK = "in_stock"
    SHIPS_IN = "ships_in"
    AVAILABLE_TO_ORDER = "available_to_order"
    BACKORDERED = "backordered"
    OUT_OF_STOCK = "out_of_stock"
    DISCONTINUED = "discontinued"
    UNAVAILABLE = "unavailable"
    UNKNOWN = "unknown"


class ParseConfidence(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class PriceDisplayType(str, Enum):
    REGULAR = "regular"
    DISCOUNTED = "discounted"
    UNKNOWN = "unknown"


class PriceValidationStatus(str, Enum):
    PARSER_MATCHES_MANUAL = "parser_matches_manual"
    PARSER_MISMATCH = "parser_mismatch"
    MANUAL_UNCLEAR = "manual_unclear"
    NOT_MANUALLY_VALIDATED = "not_manually_validated"


@dataclass(frozen=True)
class ClassificationResult:
    classification: PageClassification
    confidence: ParseConfidence
    evidence: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class PriceVisibilityResult:
    visibility: PriceVisibility
    evidence: list[str] = field(default_factory=list)


@dataclass
class ProductObservation:
    test_case_id: str | None
    manufacturer: str
    oem_part_number: str
    observed_part_number: str | None
    requested_url: str
    final_url: str | None
    canonical_url: str | None
    http_status: int | None
    page_title: str | None
    page_classification: PageClassification
    price_visibility: PriceVisibility
    classification_confidence: ParseConfidence
    classification_evidence: list[str]
    product_name: str | None
    manufacturer_display: str | None
    msrp_raw: str | None
    msrp: Decimal | None
    selling_price_raw: str | None
    selling_price: Decimal | None
    availability_raw: str | None
    availability_status: AvailabilityStatus
    shipping_estimate: str | None
    access_context: AccessContext
    session_status: SessionStatus
    superseded_by_raw: str | None
    supersession_detected: bool
    price_parse_confidence: ParseConfidence
    price_validation_status: PriceValidationStatus
    parse_confidence: ParseConfidence
    parse_warnings: list[str]
    checked_at: str
    reference_price_raw: str | None = None
    reference_price: Decimal | None = None
    savings_percent: int | None = None
    savings_amount: Decimal | None = None
    price_display_type: PriceDisplayType = PriceDisplayType.UNKNOWN
    selling_price_confidence: ParseConfidence = ParseConfidence.LOW
    reference_price_confidence: ParseConfidence = ParseConfidence.LOW

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "test_case_id": self.test_case_id,
            "manufacturer": self.manufacturer,
            "oem_part_number": self.oem_part_number,
            "observed_part_number": self.observed_part_number,
            "requested_url": self.requested_url,
            "final_url": self.final_url,
            "canonical_url": self.canonical_url,
            "http_status": self.http_status,
            "page_title": self.page_title,
            "page_classification": self.page_classification.value,
            "price_visibility": self.price_visibility.value,
            "access_context": self.access_context.value,
            "session_status": self.session_status.value,
            "classification_confidence": self.classification_confidence.value,
            "classification_evidence": self.classification_evidence,
            "product_name": self.product_name,
            "superseded_by_raw": self.superseded_by_raw,
            "supersession_detected": self.supersession_detected,
            "manufacturer_display": self.manufacturer_display,
            "msrp_raw": self.msrp_raw,
            "msrp": _decimal_to_json(self.msrp),
            "selling_price_raw": self.selling_price_raw,
            "selling_price": _decimal_to_json(self.selling_price),
            "reference_price_raw": self.reference_price_raw,
            "reference_price": _decimal_to_json(self.reference_price),
            "savings_percent": self.savings_percent,
            "savings_amount": _decimal_to_json(self.savings_amount),
            "price_display_type": self.price_display_type.value,
            "selling_price_confidence": self.selling_price_confidence.value,
            "reference_price_confidence": self.reference_price_confidence.value,
            "availability_raw": self.availability_raw,
            "availability_status": self.availability_status.value,
            "shipping_estimate": self.shipping_estimate,
            "price_parse_confidence": self.price_parse_confidence.value,
            "price_validation_status": self.price_validation_status.value,
            "parse_confidence": self.parse_confidence.value,
            "parse_warnings": self.parse_warnings,
            "checked_at": self.checked_at,
        }


def _decimal_to_json(value: Decimal | None) -> str | None:
    if value is None:
        return None
    return format(value, "f")
