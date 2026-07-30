from __future__ import annotations

from dataclasses import dataclass, field

from app.schemas.product_observation import (
    ClassificationResult,
    PageClassification,
    ParseConfidence,
    PriceVisibility,
    PriceVisibilityResult,
)

BLOCKED_STATUSES = {401, 403, 429}
BLOCKED_PHRASES = [
    "access denied",
    "request blocked",
    "automated request",
    "too many requests",
    "forbidden",
]
CHALLENGE_PHRASES = [
    "captcha",
    "verify you are human",
    "checking your browser",
    "security challenge",
    "complete the security check",
]
NOT_FOUND_PHRASES = [
    "product not found",
    "page not found",
    "404 not found",
    "we could not find",
]


@dataclass(frozen=True)
class PageContext:
    navigation_succeeded: bool
    http_status: int | None
    final_url: str | None
    page_title: str | None
    visible_text: str
    detected_signals: list[str] = field(default_factory=list)
    requested_part_number: str | None = None
    exception_message: str | None = None


def classify_page(context: PageContext) -> ClassificationResult:
    visible = context.visible_text.lower()
    title = (context.page_title or "").lower()
    evidence: list[str] = []

    if not context.navigation_succeeded or context.exception_message:
        if context.exception_message:
            evidence.append(f"Navigation exception: {context.exception_message}")
        else:
            evidence.append("Navigation did not succeed")
        return _classification_result(PageClassification.NAVIGATION_ERROR, ParseConfidence.HIGH, evidence)

    if context.http_status is not None:
        evidence.append(f"HTTP {context.http_status}")

    if context.http_status in BLOCKED_STATUSES or _contains_any(visible, BLOCKED_PHRASES):
        if context.http_status in BLOCKED_STATUSES:
            evidence.append(f"Blocked HTTP status {context.http_status}")
        if _contains_any(visible, BLOCKED_PHRASES):
            evidence.append("Visible blocked/request-denied language found")
        return _classification_result(PageClassification.BLOCKED, ParseConfidence.HIGH, evidence)

    if _contains_any(visible, CHALLENGE_PHRASES):
        evidence.append("Visible challenge language found")
        return _classification_result(PageClassification.CHALLENGE, ParseConfidence.HIGH, evidence)

    if context.http_status == 404 or _contains_any(visible, NOT_FOUND_PHRASES):
        if context.http_status == 404:
            evidence.append("HTTP 404")
        if _contains_any(visible, NOT_FOUND_PHRASES):
            evidence.append("Visible not-found language found")
        return _classification_result(PageClassification.NOT_FOUND, ParseConfidence.HIGH, evidence)

    product_signals = _normal_product_evidence(context, visible, title)
    if len(product_signals) >= 3:
        evidence.extend(product_signals)
        confidence = ParseConfidence.HIGH if len(product_signals) >= 4 else ParseConfidence.MEDIUM
        return _classification_result(PageClassification.NORMAL_PRODUCT, confidence, evidence)

    evidence.append("Insufficient product-page evidence")
    return _classification_result(PageClassification.UNKNOWN, ParseConfidence.LOW, evidence)


def classify_price_visibility(visible_text: str) -> PriceVisibilityResult:
    visible = visible_text.lower()
    if "sign in to see price" in visible or "login to see price" in visible:
        return PriceVisibilityResult(PriceVisibility.SIGN_IN_REQUIRED, ["Visible Sign In To See Price text found"])

    if _has_public_selling_price(visible_text):
        return PriceVisibilityResult(PriceVisibility.VISIBLE, ["Raw main-product price signal detected"])

    if "msrp" in visible:
        return PriceVisibilityResult(PriceVisibility.NOT_PRESENT, ["Raw MSRP text signal detected"])

    return PriceVisibilityResult(PriceVisibility.NOT_PRESENT, ["No public price signal found"])


def _normal_product_evidence(context: PageContext, visible: str, title: str) -> list[str]:
    evidence: list[str] = []

    if context.http_status == 200:
        evidence.append("HTTP 200")

    if context.requested_part_number and (
        context.requested_part_number.lower() in visible or context.requested_part_number.lower() in title
    ):
        evidence.append("OEM part number found")

    if "manufacturer:" in visible:
        evidence.append("Manufacturer found")

    if "sign in to see price" in visible or "add to cart" in visible:
        evidence.append("Product action found")

    if "ships in" in visible or "in stock" in visible:
        evidence.append("Availability information found")

    if "quantity" in visible:
        evidence.append("Quantity selector found")

    return evidence


def _contains_any(text: str, phrases: list[str]) -> bool:
    return any(phrase in text for phrase in phrases)


def _classification_result(
    classification: PageClassification,
    confidence: ParseConfidence,
    evidence: list[str],
) -> ClassificationResult:
    return ClassificationResult(classification, confidence, _unique(evidence))


def _unique(values: list[str]) -> list[str]:
    return [value for index, value in enumerate(values) if value not in values[:index]]


def _has_public_selling_price(visible_text: str) -> bool:
    lines = _main_product_lines(visible_text)
    for index, line in enumerate(lines):
        if "sign in to see price" in line:
            return False
        if "price" in line and "msrp" not in line:
            window = " ".join(lines[index : index + 3])
            if "$" in window:
                return True
    return False


def _main_product_lines(visible_text: str) -> list[str]:
    lines = [line.strip().lower() for line in visible_text.splitlines() if line.strip()]
    stop_markers = (
        "riders also bought",
        "partzilla picks",
        "recently viewed",
        "related categories",
        "need some help",
    )
    for index, line in enumerate(lines):
        if any(marker in line for marker in stop_markers):
            return lines[:index]
    return lines
