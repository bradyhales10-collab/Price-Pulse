from __future__ import annotations

import html as html_lib
import re
from dataclasses import dataclass
from datetime import UTC, datetime

from app.classifiers.page_classifier import PageContext, classify_page, classify_price_visibility
from app.models import PartRecord
from app.parsers.availability_parser import parse_availability
from app.parsers.money_parser import parse_money
from app.parsers.supersession_parser import parse_supersession
from app.schemas.product_observation import (
    AccessContext,
    PageClassification,
    ParseConfidence,
    PriceValidationStatus,
    PriceVisibility,
    PriceVisibilityResult,
    ProductObservation,
    SessionStatus,
)
from app.url_builder import build_partzilla_product_url, canonicalize_partzilla_product_url

DATA_TESTID_RE_TEMPLATE = r"<(?P<tag>[a-zA-Z0-9]+)[^>]*data-testid=[\"']{testid}[\"'][^>]*>(?P<body>.*?)</(?P=tag)>"
DATA_TESTID_PREFIX_RE_TEMPLATE = (
    r"<(?P<tag>[a-zA-Z0-9]+)[^>]*data-testid=[\"']{prefix}[^\"']*[\"'][^>]*>(?P<body>.*?)</(?P=tag)>"
)
TITLE_RE = re.compile(r"<title[^>]*>(?P<body>.*?)</title>", re.IGNORECASE | re.DOTALL)
H1_RE = re.compile(r"<h1[^>]*>(?P<body>.*?)</h1>", re.IGNORECASE | re.DOTALL)
MSRP_RE = re.compile(r"MSRP:\s*(?P<money>\$[\d,]+(?:\.\d{2})?)", re.IGNORECASE)
PART_NUMBER_RE = re.compile(r"Part\s*#:\s*(?P<part>[A-Za-z0-9-]+)", re.IGNORECASE)
SHIPS_LINE_RE = re.compile(r"Ships?\s+in\s+[^\n\r.;,]+", re.IGNORECASE)


@dataclass(frozen=True)
class ProductParseInput:
    record: PartRecord
    requested_url: str
    final_url: str | None
    http_status: int | None
    page_title: str | None
    navigation_succeeded: bool
    exception_message: str | None
    visible_text: str
    html: str
    detected_signals: list[str]
    checked_at: str | None = None


def parse_partzilla_product_page(parse_input: ProductParseInput) -> ProductObservation:
    checked_at = parse_input.checked_at or datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    context = PageContext(
        navigation_succeeded=parse_input.navigation_succeeded,
        http_status=parse_input.http_status,
        final_url=parse_input.final_url,
        page_title=parse_input.page_title,
        visible_text=parse_input.visible_text,
        detected_signals=parse_input.detected_signals,
        requested_part_number=parse_input.record.oem_part_number,
        exception_message=parse_input.exception_message,
    )
    classification = classify_page(context)
    price_visibility = classify_price_visibility(parse_input.visible_text)
    canonical_url = canonicalize_partzilla_product_url(parse_input.final_url) or canonicalize_partzilla_product_url(
        parse_input.requested_url
    )
    if classification.classification != PageClassification.NORMAL_PRODUCT:
        price_visibility = type(price_visibility)(PriceVisibility.UNKNOWN, price_visibility.evidence)
    elif price_visibility.visibility != PriceVisibility.SIGN_IN_REQUIRED and _extract_public_selling_price_raw(
        parse_input.visible_text,
        parse_input.html,
    ):
        price_visibility = PriceVisibilityResult(
            PriceVisibility.VISIBLE,
            _unique(price_visibility.evidence + ["Raw main-product price signal detected in product HTML"]),
        )

    warnings: list[str] = []
    if classification.classification != PageClassification.NORMAL_PRODUCT:
        warnings.append("non_product_page_no_product_parse")
        return ProductObservation(
            test_case_id=parse_input.record.test_case_id,
            manufacturer=parse_input.record.manufacturer,
            oem_part_number=parse_input.record.oem_part_number,
            observed_part_number=None,
            requested_url=parse_input.requested_url,
            final_url=parse_input.final_url,
            canonical_url=canonical_url,
            http_status=parse_input.http_status,
            page_title=parse_input.page_title,
            page_classification=classification.classification,
            price_visibility=price_visibility.visibility,
            classification_confidence=classification.confidence,
            classification_evidence=_unique(classification.evidence),
            product_name=None,
            manufacturer_display=None,
            msrp_raw=None,
            msrp=None,
            selling_price_raw=None,
            selling_price=None,
            availability_raw=None,
            availability_status=parse_availability(None).status,
            shipping_estimate=None,
            access_context=AccessContext.PUBLIC,
            session_status=SessionStatus.UNKNOWN,
            superseded_by_raw=None,
            supersession_detected=False,
            price_parse_confidence=ParseConfidence.LOW,
            price_validation_status=PriceValidationStatus.NOT_MANUALLY_VALIDATED,
            parse_confidence=ParseConfidence.LOW,
            parse_warnings=warnings,
            checked_at=checked_at,
        )

    observed_part_number = _extract_observed_part_number(parse_input.html, parse_input.visible_text)
    if observed_part_number and observed_part_number != parse_input.record.oem_part_number:
        warnings.append("observed_part_number_mismatch")
    if not observed_part_number:
        warnings.append("observed_part_number_not_found")

    manufacturer_display = _extract_manufacturer(parse_input.html, parse_input.visible_text)
    if not manufacturer_display:
        warnings.append("manufacturer_not_found")

    product_name = _extract_product_name(
        html=parse_input.html,
        page_title=parse_input.page_title,
        manufacturer_display=manufacturer_display or parse_input.record.manufacturer,
        requested_part_number=parse_input.record.oem_part_number,
    )
    if not product_name:
        warnings.append("product_name_not_found")
    supersession = parse_supersession(
        product_name=product_name,
        heading_text=_first_h1_text(parse_input.html),
        page_title=parse_input.page_title,
    )

    msrp_raw = _extract_msrp_raw(parse_input.html, parse_input.visible_text)
    msrp_result = parse_money(msrp_raw, warning_code="msrp_parse_failed") if msrp_raw else None
    if not msrp_raw:
        warnings.append("msrp_not_found")
    elif msrp_result is not None:
        warnings.extend(msrp_result.warnings)

    selling_price_raw = None
    selling_price = None
    if price_visibility.visibility == PriceVisibility.VISIBLE:
        selling_price_raw = _extract_public_selling_price_raw(parse_input.visible_text, parse_input.html)
        selling_result = parse_money(selling_price_raw, warning_code="selling_price_parse_failed")
        selling_price = selling_result.value
        warnings.extend(selling_result.warnings)
        if selling_price_raw is None:
            warnings.append("selling_price_not_found")

    availability_raw = _extract_availability_raw(parse_input.html, parse_input.visible_text)
    availability = parse_availability(availability_raw)
    if not availability_raw:
        warnings.append("availability_not_found")

    parse_confidence = _determine_parse_confidence(
        page_classification=classification.classification,
        classification_confidence=classification.confidence,
        observed_part_number=observed_part_number,
        requested_part_number=parse_input.record.oem_part_number,
        product_name=product_name,
        manufacturer_display=manufacturer_display,
        msrp_value=msrp_result.value if msrp_result is not None else None,
        availability_raw=availability_raw,
    )

    return ProductObservation(
        test_case_id=parse_input.record.test_case_id,
        manufacturer=parse_input.record.manufacturer,
        oem_part_number=parse_input.record.oem_part_number,
        observed_part_number=observed_part_number,
        requested_url=parse_input.requested_url,
        final_url=parse_input.final_url,
        canonical_url=canonical_url,
        http_status=parse_input.http_status,
        page_title=parse_input.page_title,
        page_classification=classification.classification,
        price_visibility=price_visibility.visibility,
        classification_confidence=classification.confidence,
        classification_evidence=_unique(classification.evidence),
        product_name=product_name,
        manufacturer_display=manufacturer_display,
        msrp_raw=msrp_result.raw if msrp_result is not None else None,
        msrp=msrp_result.value if msrp_result is not None else None,
        selling_price_raw=selling_price_raw,
        selling_price=selling_price,
        availability_raw=availability.raw,
        availability_status=availability.status,
        shipping_estimate=availability.shipping_estimate,
        access_context=AccessContext.PUBLIC,
        session_status=SessionStatus.UNKNOWN,
        superseded_by_raw=supersession.superseded_by_raw,
        supersession_detected=supersession.supersession_detected,
        price_parse_confidence=ParseConfidence.HIGH if selling_price is not None else ParseConfidence.LOW,
        price_validation_status=PriceValidationStatus.NOT_MANUALLY_VALIDATED,
        parse_confidence=parse_confidence,
        parse_warnings=_unique(warnings),
        checked_at=checked_at,
    )


def build_parse_input_from_probe(
    record: PartRecord,
    html: str,
    visible_text: str,
    final_url: str | None,
    http_status: int | None,
    page_title: str | None,
    navigation_succeeded: bool,
    exception_message: str | None,
    detected_signals: list[str],
    checked_at: str | None = None,
) -> ProductParseInput:
    return ProductParseInput(
        record=record,
        requested_url=build_partzilla_product_url(record.manufacturer, record.oem_part_number),
        final_url=final_url,
        http_status=http_status,
        page_title=page_title,
        navigation_succeeded=navigation_succeeded,
        exception_message=exception_message,
        visible_text=visible_text,
        html=html,
        detected_signals=detected_signals,
        checked_at=checked_at,
    )


def _extract_observed_part_number(html: str, visible_text: str) -> str | None:
    raw = _text_by_testid(html, "productDetailPartNumber")
    if raw:
        match = PART_NUMBER_RE.search(raw)
        return match.group("part") if match else raw.replace("Part #:", "").strip()

    match = PART_NUMBER_RE.search(visible_text)
    return match.group("part") if match else None


def _extract_manufacturer(html: str, visible_text: str) -> str | None:
    raw = _text_by_testid_prefix(html, "productFilterValueManufacturer-")
    if raw:
        return raw.strip()

    match = re.search(r"Manufacturer:\s*(?P<manufacturer>[A-Za-z0-9 &.-]+)", visible_text, re.IGNORECASE)
    return match.group("manufacturer").strip() if match else None


def _extract_product_name(
    html: str,
    page_title: str | None,
    manufacturer_display: str,
    requested_part_number: str,
) -> str | None:
    h1_text = _first_h1_text(html)
    candidates = [h1_text, page_title]
    for candidate in candidates:
        if not candidate:
            continue
        normalized = _normalize_product_name(candidate, manufacturer_display, requested_part_number)
        if normalized:
            return normalized
    return None


def _extract_msrp_raw(html: str, visible_text: str) -> str | None:
    for source in (_text_by_testid(html, "authModalButton") or "", visible_text, _main_product_html(html)):
        match = MSRP_RE.search(source)
        if match:
            return match.group("money")
    return None


def _extract_availability_raw(html: str, visible_text: str) -> str | None:
    raw = _text_by_testid(html, "stockInfoText")
    if raw:
        return raw

    aria = _aria_label_by_testid(html, "stockInfoText")
    if aria:
        return aria

    match = SHIPS_LINE_RE.search(visible_text)
    if match:
        return match.group(0)

    for phrase in ("In Stock", "Out of Stock", "Unavailable"):
        if phrase.lower() in visible_text.lower():
            return phrase

    return None


def _extract_public_selling_price_raw(visible_text: str, html: str = "") -> str | None:
    visible_lines = _main_product_lines(visible_text)
    visible_region = " ".join(visible_lines)
    discounted = re.search(
        r"(?P<price>\$[\d,]+(?:\.\d{2})?)\s*SAVE\s+\d{1,3}%",
        visible_region,
        re.IGNORECASE,
    )
    if discounted:
        return discounted.group("price")

    raw = _extract_html_main_product_selling_price_raw(html)
    if raw:
        return raw

    for index, line in enumerate(visible_lines):
        lowered = line.lower()
        if "msrp" in lowered or "sign in to see price" in lowered:
            continue
        if "price" in lowered:
            window = " ".join(visible_lines[index : index + 3])
            match = re.search(r"\$[\d,]+(?:\.\d{2})?", window)
            if match:
                return match.group(0)
    return None


def _extract_html_main_product_selling_price_raw(html: str) -> str | None:
    if not html:
        return None

    main_html = _main_product_html(html)
    candidates = (
        _text_by_testid(main_html, "productPrice"),
        _text_by_testid(main_html, "productDetailPrice"),
        _text_by_testid_prefix(main_html, "productPrice"),
    )
    for candidate in candidates:
        if not candidate:
            continue
        lowered = candidate.lower()
        if "msrp" in lowered or "sign in to see price" in lowered:
            continue
        match = re.search(r"\$[\d,]+(?:\.\d{2})?", candidate)
        if match:
            return match.group(0)
    return None


def _main_product_html(html: str) -> str:
    lowered = html.lower()
    stop_markers = (
        "riders also bought",
        "partzilla picks",
        "recently viewed",
        "related categories",
        "need some help",
        "recommendationfeed",
    )
    stop_indexes = [lowered.find(marker) for marker in stop_markers if lowered.find(marker) >= 0]
    return html[: min(stop_indexes)] if stop_indexes else html


def _main_product_lines(visible_text: str) -> list[str]:
    lines = [line.strip() for line in visible_text.splitlines() if line.strip()]
    stop_markers = (
        "riders also bought",
        "partzilla picks",
        "recently viewed",
        "related categories",
        "need some help",
    )
    for index, line in enumerate(lines):
        if any(marker in line.lower() for marker in stop_markers):
            return lines[:index]
    return lines


def _text_by_testid(html: str, testid: str) -> str | None:
    pattern = DATA_TESTID_RE_TEMPLATE.format(testid=re.escape(testid))
    match = re.search(pattern, html, re.IGNORECASE | re.DOTALL)
    return _clean_html_text(match.group("body")) if match else None


def _text_by_testid_prefix(html: str, prefix: str) -> str | None:
    pattern = DATA_TESTID_PREFIX_RE_TEMPLATE.format(prefix=re.escape(prefix))
    match = re.search(pattern, html, re.IGNORECASE | re.DOTALL)
    return _clean_html_text(match.group("body")) if match else None


def _aria_label_by_testid(html: str, testid: str) -> str | None:
    pattern = rf"<[a-zA-Z0-9]+[^>]*data-testid=[\"']{re.escape(testid)}[\"'][^>]*aria-label=[\"'](?P<label>[^\"']+)[\"'][^>]*>"
    match = re.search(pattern, html, re.IGNORECASE | re.DOTALL)
    return html_lib.unescape(match.group("label")).strip() if match else None


def _first_h1_text(html: str) -> str | None:
    match = H1_RE.search(html)
    return _clean_html_text(match.group("body")) if match else None


def _clean_html_text(value: str) -> str:
    without_tags = re.sub(r"<[^>]+>", " ", value)
    return " ".join(html_lib.unescape(without_tags).split())


def _normalize_product_name(raw: str, manufacturer_display: str, requested_part_number: str) -> str | None:
    value = html_lib.unescape(raw)
    value = re.sub(r"\|.*$", "", value)
    value = re.sub(r"\s+-\s+.*$", "", value)
    value = value.replace(requested_part_number, "")
    value = re.sub(re.escape(manufacturer_display), "", value, flags=re.IGNORECASE)
    value = re.sub(r"\bOEM\b", "", value, flags=re.IGNORECASE)
    value = " ".join(value.split(" -|")).strip(" -|")
    value = " ".join(value.split())
    return value or None


def _determine_parse_confidence(
    page_classification: PageClassification,
    classification_confidence: ParseConfidence,
    observed_part_number: str | None,
    requested_part_number: str,
    product_name: str | None,
    manufacturer_display: str | None,
    msrp_value: object | None,
    availability_raw: str | None,
) -> ParseConfidence:
    if page_classification != PageClassification.NORMAL_PRODUCT:
        return ParseConfidence.LOW

    core_values = [
        observed_part_number == requested_part_number,
        bool(product_name),
        bool(manufacturer_display),
        msrp_value is not None,
        bool(availability_raw),
    ]
    if classification_confidence == ParseConfidence.HIGH and all(core_values):
        return ParseConfidence.HIGH
    if sum(1 for value in core_values if value) >= 3:
        return ParseConfidence.MEDIUM
    return ParseConfidence.LOW


def _unique(values: list[str]) -> list[str]:
    return [value for index, value in enumerate(values) if value and value not in values[:index]]
