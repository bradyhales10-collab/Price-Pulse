from __future__ import annotations

import html as html_lib
import json
import re
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any
from urllib.parse import urlencode

from app.competitors.base import CompetitorCapabilities, CompetitorObservation
from app.manufacturer_registry import competitor_manufacturers, normalize_manufacturer
from app.models import PartRecord
from app.parsers.money_parser import parse_money

SEARCH_URL = "https://www.chapmoto.com/search/"
LOOKUP_URL = SEARCH_URL
MONEY_RE = re.compile(r"\$[\d,]+(?:\.\d{2})?")
PART_TOKEN_RE = re.compile(r"\b[A-Z0-9][A-Z0-9.\-/]{3,}\b", re.IGNORECASE)
HIDDEN_PRICE_RE = re.compile(r"\b(add\s+to\s+view\s+price|add\s+to\s+cart\s+to\s+view\s+price|view\s+price\s+in\s+cart|add\s+to\s+cart\s+to\s+see\s+price|see\s+price\s+in\s+cart)\b", re.IGNORECASE)
CAPTCHA_RE = re.compile(r"\b(captcha|verify\s+you\s+are\s+human|checking\s+your\s+browser)\b", re.IGNORECASE)
BLOCK_RE = re.compile(r"\b(access\s+denied|rate\s+limited|too\s+many\s+requests|forbidden)\b", re.IGNORECASE)
NOT_FOUND_RE = re.compile(r"\b(no\s+results|part\s+not\s+found|0\s+results|could\s+not\s+find)\b", re.IGNORECASE)
LOOKUP_ERROR_RE = re.compile(r"\b(error\s+pulling\s+part\s+data|unable\s+to\s+pull\s+part\s+data|lookup\s+error)\b", re.IGNORECASE)
SUPERSESSION_RE = re.compile(r"\b(super(?:seded|session)|supercede(?:d)?\s+(?:to|by)|substitut(?:ed|e)s?\s+by|replaces|replaced\s+by)\b", re.IGNORECASE)
STRUCTURED_PRICE_PLACEHOLDERS = {Decimal("9999.99")}
AVAILABILITY_RE = re.compile(
    r"\b(in\s+stock|ships\s+in\s+\d+(?:-\d+)?\s+days|available\s+to\s+order|back\s*ordered|backordered|out\s+of\s+stock|discontinued|unavailable)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ChaparralMatch:
    part_number: str
    product_name: str | None
    product_url: str | None
    diagram_url: str | None
    region_text: str
    selling_price: Decimal | None
    reference_price: Decimal | None
    price_source: str
    availability_raw: str | None
    availability_status: str
    superseded_part_number: str | None = None
    warnings: tuple[str, ...] = field(default_factory=tuple)


class ChaparralAdapter:
    competitor_key = "chaparral"
    display_name = "Chaparral Motorsports"
    login_page_url = "https://www.chaparral-racing.com/login"
    short_name = "Chaparral"
    supported_manufacturers = competitor_manufacturers("chaparral")
    lookup_url = LOOKUP_URL
    capabilities = CompetitorCapabilities(
        requires_login=False,
        supports_public_price=True,
        supports_direct_part_url=False,
        status="active",
        legal_review_status="approved_for_monitoring",
    )

    @property
    def requires_login(self) -> bool:
        return self.capabilities.requires_login

    @property
    def supports_public_price(self) -> bool:
        return self.capabilities.supports_public_price

    @property
    def supports_direct_part_url(self) -> bool:
        return self.capabilities.supports_direct_part_url

    def build_product_url(self, product: PartRecord) -> str:
        return build_search_url(product.oem_part_number)

    def parse_product_page(
        self,
        html: str,
        product: PartRecord,
        *,
        visible_text: str = "",
        final_url: str | None = None,
        http_status: int | None = None,
    ) -> CompetitorObservation:
        text = _clean_text(visible_text or html)
        normalized_part = normalize_part_number_for_match(product.oem_part_number)
        warnings: list[str] = []
        page_classification = _classify_page(text, http_status)
        if page_classification != "normal_product":
            warnings.append(page_classification)
            return _observation(
                product,
                final_url=final_url or self.lookup_url,
                http_status=http_status,
                page_classification=page_classification,
                lookup_status=_lookup_status_from_page_classification(page_classification),
                warnings=warnings,
            )

        match = select_exact_match(text=text, html=html, requested_part_number=product.oem_part_number, final_url=final_url)
        if match is None:
            if NOT_FOUND_RE.search(text):
                status = "part_not_found"
            elif LOOKUP_ERROR_RE.search(text):
                status = "lookup_error"
            else:
                status = "lookup_failed"
            warnings.append(status)
            page_status = "not_found" if status == "part_not_found" else ("normal_product" if status == "lookup_error" else "unknown")
            return _observation(
                product,
                final_url=final_url or self.lookup_url,
                http_status=http_status,
                page_classification=page_status,
                lookup_status=status,
                warnings=warnings,
                raw_evidence={"normalized_part_number": normalized_part},
            )

        structured = _structured_product_match(html, product.oem_part_number)
        selling_price = match.selling_price
        reference_price = match.reference_price or _msrp_from_text(text)
        price_source = match.price_source
        product_name = match.product_name
        canonical_url = match.product_url or match.diagram_url or final_url or self.lookup_url
        availability_raw = match.availability_raw
        availability_status = match.availability_status
        structured_price_accepted = False
        structured_price_rejected_reason = None
        if structured:
            product_name = structured.get("name") or product_name
            canonical_url = structured.get("url") or canonical_url
            availability_raw = availability_raw or structured.get("availability_raw")
            availability_status = normalize_availability(availability_raw)
            structured_price = structured.get("price")
            if structured_price is not None and not _is_structured_price_placeholder(structured_price):
                selling_price = structured_price
                price_source = "structured_data"
                structured_price_accepted = True
                warnings = [warning for warning in warnings if warning != "selling_price_hidden_in_cart"]
                if match.price_source == "cart_required":
                    warnings.append("structured_price_used_from_public_product_data")
            elif structured_price is not None:
                structured_price_rejected_reason = "placeholder_price"
                warnings.append("structured_price_placeholder_ignored")

        if selling_price is None and HIDDEN_PRICE_RE.search(text) and price_source in {"not_available", "msrp_only"}:
            price_source = "cart_required"
            if "selling_price_hidden_in_cart" not in warnings:
                warnings.append("selling_price_hidden_in_cart")

        warnings.extend(match.warnings)
        if selling_price is not None:
            warnings = [warning for warning in warnings if warning != "selling_price_hidden_in_cart"]
        lookup_status = _lookup_status_from_values(
            selling_price=selling_price,
            reference_price=reference_price,
            price_source=price_source,
            availability_status=availability_status,
            superseded_part_number=match.superseded_part_number,
            warnings=warnings,
        )
        price_visibility = "see_price_in_cart" if price_source == "cart_required" else ("visible" if selling_price is not None else "not_present")
        price_display_type = "cart_price_hidden" if price_source == "cart_required" else ("regular" if selling_price is not None else "unknown")
        return CompetitorObservation(
            competitor_key=self.competitor_key,
            manufacturer=normalize_manufacturer(product.manufacturer),
            oem_part_number=product.oem_part_number,
            observed_part_number=match.part_number,
            product_name=product_name,
            canonical_url=canonical_url,
            http_status=http_status,
            page_classification="normal_product",
            session_status="public",
            price_visibility=price_visibility,
            selling_price=selling_price,
            reference_price=reference_price,
            price_display_type=price_display_type,
            selling_price_confidence="high" if selling_price is not None else "low",
            reference_price_confidence="high" if reference_price is not None else "low",
            availability_raw=availability_raw,
            availability_status=availability_status,
            supersession_detected=bool(match.superseded_part_number),
            superseded_by_raw=match.superseded_part_number,
            warnings=warnings,
            parser_version="chaparral-v1",
            raw_evidence_summary={
                "competitor": "Chaparral Motorsports",
                "requested_part_number": product.oem_part_number,
                "normalized_part_number": normalized_part,
                "matched_part_number": match.part_number,
                "price_source": price_source,
                "product_url": match.product_url,
                "diagram_url": match.diagram_url,
                "resolved_url": canonical_url,
                "lookup_status": lookup_status,
                "currency": "USD",
                "region_text": match.region_text,
                "structured_data_used": structured_price_accepted,
                "structured_price_rejected_reason": structured_price_rejected_reason,
            },
            parse_confidence="high" if selling_price is not None or lookup_status in {"part_found_price_hidden", "msrp_only", "superseded"} else "medium",
        )

    def normalize_availability(self, raw: str | None) -> str:
        return normalize_availability(raw)

    def normalize_supersession(self, raw: str | None) -> tuple[bool, str | None]:
        return (bool(raw), raw)


def normalize_part_number_for_match(value: str) -> str:
    return "".join(char for char in value.upper() if char.isalnum())


def build_search_url(part_number: str) -> str:
    return f"{SEARCH_URL}?{urlencode({'q': part_number, 'type': 'oem'})}"


def normalize_availability(raw: str | None) -> str:
    if not raw:
        return "unknown"
    lowered = raw.lower()
    if "in stock" in lowered or "instock" in lowered:
        return "in_stock"
    if "ships in" in lowered:
        return "ships_in"
    if "available to order" in lowered:
        return "available_to_order"
    if "backorder" in lowered or "back ordered" in lowered:
        return "backordered"
    if "out of stock" in lowered or "unavailable" in lowered:
        return "out_of_stock"
    if "discontinued" in lowered:
        return "discontinued"
    return "unknown"


def select_exact_match(*, text: str, html: str = "", requested_part_number: str, final_url: str | None = None) -> ChaparralMatch | None:
    normalized = normalize_part_number_for_match(requested_part_number)
    lines = _clean_lines(text)
    candidate_indexes = [
        index for index, line in enumerate(lines)
        if any(normalize_part_number_for_match(token) == normalized for token in PART_TOKEN_RE.findall(line))
    ]
    if not candidate_indexes:
        return None

    seen_regions: set[str] = set()
    matches: list[ChaparralMatch] = []
    links = _links_near_part(html, normalized)
    for index in candidate_indexes:
        start = index
        next_index = next((candidate for candidate in candidate_indexes if candidate > index), len(lines))
        end = _match_region_end(lines, index=index, next_index=next_index)
        region_lines = lines[start:end]
        region_text = "\n".join(region_lines)
        if region_text in seen_regions:
            continue
        seen_regions.add(region_text)
        matches.append(_match_from_region(region_lines, requested_part_number, region_text, links, final_url))

    unique = _dedupe_matches(matches)
    priced = [match for match in unique if match.selling_price is not None]
    original_priced = [match for match in priced if not match.superseded_part_number]
    if original_priced:
        # Search results can include replacement cards with lower prices. The
        # requested OEM card is authoritative whenever it is present.
        priced = original_priced
    if len({str(match.selling_price) for match in priced}) > 1:
        if priced and all(match.superseded_part_number for match in priced):
            preferred = sorted(priced, key=_match_sort_key)[0]
            return ChaparralMatch(
                **{
                    **preferred.__dict__,
                    "warnings": tuple((*preferred.warnings, "multiple_supersession_options")),
                }
            )
        first = priced[0]
        return ChaparralMatch(
            part_number=first.part_number,
            product_name=first.product_name,
            product_url=first.product_url,
            diagram_url=first.diagram_url,
            region_text="\n---\n".join(match.region_text for match in priced),
            selling_price=None,
            reference_price=None,
            price_source="not_available",
            availability_raw=first.availability_raw,
            availability_status=first.availability_status,
            warnings=("multiple_exact_matches", "conflicting_prices"),
        )
    preferred = sorted(unique, key=_match_sort_key)[0]
    if len(unique) > 1:
        return ChaparralMatch(
            **{**preferred.__dict__, "warnings": tuple((*preferred.warnings, "deduped_multiple_exact_matches"))}
        )
    return preferred


def _match_region_end(lines: list[str], *, index: int, next_index: int) -> int:
    """Stop at the next visible OEM card instead of merging search results."""
    limit = min(len(lines), index + 12, next_index)
    for position in range(index + 1, limit):
        if lines[position].strip().casefold() == "oem":
            return position
    return limit


def _match_from_region(region_lines: list[str], requested_part_number: str, region_text: str, links: dict[str, str], final_url: str | None) -> ChaparralMatch:
    money_values = [parse_money(match.group(0)).value for match in MONEY_RE.finditer(region_text)]
    money_values = [value for value in money_values if value is not None]
    selling_price = None
    reference_price = None
    msrp_only_region = "msrp" in region_text.lower() and not re.search(r"\b(your\s+price|sale|price\s*:|now)\b", region_text, re.IGNORECASE)
    if len(money_values) == 1 and msrp_only_region:
        reference_price = money_values[0]
        price_source = "msrp_only"
    elif len(money_values) == 1:
        selling_price = money_values[0]
    elif len(money_values) >= 2:
        reference_price = max(money_values)
        selling_price = min(money_values)
    price_source = "lookup_page" if selling_price is not None else "not_available"
    if HIDDEN_PRICE_RE.search(region_text):
        price_source = "cart_required"
        selling_price = None
    elif reference_price is not None and selling_price is None:
        price_source = "msrp_only"
    availability_raw = _first_match(AVAILABILITY_RE, region_text)
    superseded = _superseded_part(region_text, requested_part_number)
    product_name = _product_name(region_lines, requested_part_number)
    product_url = links.get("product_url")
    diagram_url = links.get("diagram_url") or final_url
    warnings: list[str] = []
    if superseded:
        warnings.append("superseded")
    if price_source == "cart_required":
        warnings.append("selling_price_hidden_in_cart")
    if reference_price is not None and selling_price is None:
        warnings.append("msrp_only")
    return ChaparralMatch(
        part_number=requested_part_number,
        product_name=product_name,
        product_url=product_url,
        diagram_url=diagram_url,
        region_text=region_text,
        selling_price=selling_price,
        reference_price=reference_price,
        price_source=price_source,
        availability_raw=availability_raw,
        availability_status=normalize_availability(availability_raw),
        superseded_part_number=superseded,
        warnings=tuple(warnings),
    )


def _dedupe_matches(matches: list[ChaparralMatch]) -> list[ChaparralMatch]:
    output: list[ChaparralMatch] = []
    seen: set[tuple[str, str | None, str | None, str | None]] = set()
    for match in matches:
        key = (normalize_part_number_for_match(match.part_number), match.product_url, str(match.selling_price), str(match.reference_price))
        if key in seen:
            continue
        seen.add(key)
        output.append(match)
    return output


def _match_sort_key(match: ChaparralMatch) -> tuple[int, int, int]:
    return (
        0 if match.product_url else 1,
        0 if match.selling_price is not None else 1,
        0 if match.diagram_url else 1,
    )


def _lookup_status(match: ChaparralMatch) -> str:
    return _lookup_status_from_values(
        selling_price=match.selling_price,
        reference_price=match.reference_price,
        price_source=match.price_source,
        availability_status=match.availability_status,
        superseded_part_number=match.superseded_part_number,
        warnings=list(match.warnings),
    )


def _lookup_status_from_values(
    *,
    selling_price: Decimal | None,
    reference_price: Decimal | None,
    price_source: str,
    availability_status: str,
    superseded_part_number: str | None,
    warnings: list[str] | tuple[str, ...],
) -> str:
    if "multiple_exact_matches" in warnings:
        return "multiple_exact_matches"
    if superseded_part_number:
        return "superseded"
    if availability_status == "discontinued":
        return "discontinued"
    if availability_status == "out_of_stock":
        return "out_of_stock"
    if availability_status == "available_to_order":
        return "available_to_order"
    if price_source == "cart_required":
        return "part_found_price_hidden"
    if selling_price is not None:
        return "price_found"
    if reference_price is not None:
        return "msrp_only"
    return "lookup_failed"


def _classify_page(text: str, http_status: int | None) -> str:
    if http_status in {403, 429} or BLOCK_RE.search(text):
        return "blocked"
    if CAPTCHA_RE.search(text):
        return "challenge"
    if http_status == 404 and "OEM Part Number Lookup" not in text:
        return "not_found"
    return "normal_product"


def _lookup_status_from_page_classification(value: str) -> str:
    return {
        "blocked": "blocked_or_rate_limited",
        "challenge": "captcha_detected",
        "not_found": "part_not_found",
        "navigation_error": "lookup_failed",
    }.get(value, "lookup_failed")


def _structured_product_match(html: str, requested_part_number: str) -> dict[str, Any] | None:
    normalized = normalize_part_number_for_match(requested_part_number)
    for raw_script in re.findall(r"<script[^>]+type=[\"']application/ld\+json[\"'][^>]*>(.*?)</script>", html, flags=re.IGNORECASE | re.DOTALL):
        decoded = html_lib.unescape(raw_script).strip()
        try:
            parsed = json.loads(decoded)
        except json.JSONDecodeError:
            continue
        for item in _walk_jsonld(parsed):
            if not isinstance(item, dict) or item.get("@type") != "Product":
                continue
            sku = str(item.get("sku") or item.get("mpn") or "")
            if normalize_part_number_for_match(sku) != normalized:
                continue
            offers = item.get("offers") if isinstance(item.get("offers"), dict) else {}
            price = _decimal_from_value(offers.get("price"))
            availability_raw = str(offers.get("availability") or "")
            return {
                "name": item.get("name"),
                "url": offers.get("url"),
                "price": price,
                "availability_raw": availability_raw.rsplit("/", 1)[-1] if availability_raw else None,
            }
    return None


def _walk_jsonld(value: Any):
    if isinstance(value, dict):
        yield value
        graph = value.get("@graph")
        if isinstance(graph, list):
            for item in graph:
                yield from _walk_jsonld(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk_jsonld(item)


def _decimal_from_value(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value).replace(",", "").strip())
    except Exception:
        return None


def _is_structured_price_placeholder(value: Decimal) -> bool:
    return value in STRUCTURED_PRICE_PLACEHOLDERS


def _msrp_from_text(text: str) -> Decimal | None:
    match = re.search(r"\bMSRP\s*(\$[\d,]+(?:\.\d{2})?)", text, flags=re.IGNORECASE)
    if not match:
        return None
    return parse_money(match.group(1)).value


def _observation(
    product: PartRecord,
    *,
    final_url: str,
    http_status: int | None,
    page_classification: str,
    lookup_status: str,
    warnings: list[str],
    raw_evidence: dict[str, Any] | None = None,
) -> CompetitorObservation:
    return CompetitorObservation(
        competitor_key="chaparral",
        manufacturer=normalize_manufacturer(product.manufacturer),
        oem_part_number=product.oem_part_number,
        canonical_url=final_url,
        http_status=http_status,
        page_classification=page_classification,
        session_status="public",
        price_visibility="not_present",
        price_display_type="unknown",
        availability_status="unknown",
        warnings=warnings,
        parser_version="chaparral-v1",
        raw_evidence_summary={
            "competitor": "Chaparral Motorsports",
            "requested_part_number": product.oem_part_number,
            "normalized_part_number": normalize_part_number_for_match(product.oem_part_number),
            "lookup_status": lookup_status,
            "currency": "USD",
            **(raw_evidence or {}),
        },
        parse_confidence="low",
    )


def _links_near_part(html: str, normalized: str) -> dict[str, str]:
    links: dict[str, str] = {}
    for href, label in re.findall(r"<a[^>]+href=[\"']([^\"']+)[\"'][^>]*>(.*?)</a>", html, flags=re.IGNORECASE | re.DOTALL):
        label_text = _clean_text(label)
        href_text = html_lib.unescape(href)
        if normalized not in normalize_part_number_for_match(label_text + " " + href_text):
            continue
        full = href_text if href_text.startswith("http") else f"https://www.chapmoto.com{href_text}"
        if "/oem/" in href_text and "product_url" not in links:
            links["diagram_url"] = full
        elif "product_url" not in links:
            links["product_url"] = full
    return links


def _product_name(lines: list[str], requested_part_number: str) -> str | None:
    normalized = normalize_part_number_for_match(requested_part_number)
    for line in lines:
        if normalize_part_number_for_match(line) == normalized:
            continue
        if normalize_part_number_for_match(requested_part_number) in normalize_part_number_for_match(line):
            cleaned = re.sub(re.escape(requested_part_number), "", line, flags=re.IGNORECASE).strip(" -:|")
            if cleaned:
                return cleaned
        if MONEY_RE.search(line) or AVAILABILITY_RE.search(line):
            continue
        if 3 <= len(line) <= 120:
            return line
    return None


def _superseded_part(text: str, requested_part_number: str) -> str | None:
    if not SUPERSESSION_RE.search(text):
        return None
    requested = normalize_part_number_for_match(requested_part_number)
    for token in PART_TOKEN_RE.findall(text):
        normalized = normalize_part_number_for_match(token)
        if any(char.isdigit() for char in normalized) and normalized != requested:
            return token
    return None


def _first_match(pattern: re.Pattern[str], text: str) -> str | None:
    match = pattern.search(text)
    return " ".join(match.group(0).split()) if match else None


def _clean_text(value: str) -> str:
    without_tags = re.sub(r"<[^>]+>", "\n", value)
    return "\n".join(line.strip() for line in html_lib.unescape(without_tags).splitlines() if line.strip())


def _clean_lines(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if line.strip()]
