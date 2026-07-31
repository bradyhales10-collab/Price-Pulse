"""RevZilla OEM parts adapter.

RevZilla runs an OEM fiche program fulfilled by Montgomeryville Cycle Center,
covering Honda, Kawasaki, Suzuki and Yamaha motorcycles. It does not carry
genuine Polaris OEM parts, so Polaris is deliberately absent from coverage.

Two things make this competitor different from Partzilla:

1. Product URLs embed a description slug, for example
   ``/oem/kawasaki/kawasaki-41080-1186-disc-fr``. A URL cannot be built from a
   part number alone, so lookups go through search, the same as Chaparral.
2. Listings stay visible with a price after they are discontinued or out of
   stock. A price on a dead listing is not a price we can compete with, so
   availability is checked before the price is accepted.
"""

from __future__ import annotations

import html as html_lib
import json
import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from urllib.parse import urlencode

from app.competitors.base import CompetitorCapabilities, CompetitorObservation
from app.manufacturer_registry import competitor_manufacturers, normalize_manufacturer
from app.models import PartRecord
from app.parsers.money_parser import parse_money

BASE_URL = "https://www.revzilla.com"
SEARCH_URL = f"{BASE_URL}/search"

# "Current price is $715.68" is the visible price label on OEM product pages.
CURRENT_PRICE_RE = re.compile(r"current\s+price\s+is\s*(\$[\d,]+(?:\.\d{2})?)", re.IGNORECASE)
# The page also carries the price in cents in a meta tag, which needs no money
# parsing. Both the real tag and a flattened "name: value" rendering are matched.
SAILTHRU_PRICE_RE = re.compile(
    r"sailthru\.price[\"']?(?:[^>]*?content=[\"']|\s*[:=]\s*[\"']?)(\d+)",
    re.IGNORECASE,
)
SAILTHRU_INVENTORY_RE = re.compile(
    r"sailthru\.inventory[\"']?(?:[^>]*?content=[\"']|\s*[:=]\s*[\"']?)(\d+)",
    re.IGNORECASE,
)
OEM_PART_NUMBER_RE = re.compile(r"OEM\s+Part\s+Number:\s*([A-Z0-9][A-Z0-9.\-/]*)", re.IGNORECASE)
PRODUCT_LINK_RE = re.compile(r"href=[\"'](/oem/[^\"'?#]+)[\"']", re.IGNORECASE)

DISCONTINUED_RE = re.compile(
    r"\b(discontinued|no\s+longer\s+available|closeout:\s*this\s+product\s+is\s+no\s+longer\s+available)\b",
    re.IGNORECASE,
)
OUT_OF_STOCK_RE = re.compile(r"\b(out\s+of\s+stock|currently\s+unavailable|sold\s+out)\b", re.IGNORECASE)
IN_STOCK_RE = re.compile(r"\b(in\s+stock|ships\s+(?:free|today|within)|add\s+to\s+cart)\b", re.IGNORECASE)
BACKORDER_RE = re.compile(r"\b(back\s*ordered|backorder|pre-?order)\b", re.IGNORECASE)

CAPTCHA_RE = re.compile(r"\b(captcha|verify\s+you\s+are\s+human|checking\s+your\s+browser)\b", re.IGNORECASE)
BLOCK_RE = re.compile(r"\b(access\s+denied|rate\s+limited|too\s+many\s+requests|forbidden)\b", re.IGNORECASE)
NOT_FOUND_RE = re.compile(r"\b(no\s+results|0\s+results|we\s+couldn'?t\s+find|no\s+products\s+found)\b", re.IGNORECASE)
SUPERSESSION_RE = re.compile(r"\bsupersedes\s+part\s+([A-Z0-9][A-Z0-9.\-/]*)", re.IGNORECASE)

# Availability states where a shown price is not something we can compete with.
UNSELLABLE_AVAILABILITY = {"discontinued", "out_of_stock"}


@dataclass(frozen=True)
class RevzillaMatch:
    part_number: str | None
    product_name: str | None
    product_url: str | None
    selling_price: Decimal | None
    availability_raw: str | None
    availability_status: str
    superseded_part_number: str | None
    price_source: str


class RevzillaAdapter:
    competitor_key = "revzilla"
    display_name = "RevZilla"
    short_name = "RevZilla"
    supported_manufacturers = competitor_manufacturers("revzilla")
    lookup_url = SEARCH_URL
    capabilities = CompetitorCapabilities(
        requires_login=False,
        supports_public_price=True,
        supports_direct_part_url=False,
        status="experimental_probe",
        legal_review_status="review_needed",
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
        page_classification = _classify_page(text, http_status)
        if page_classification != "normal_product":
            return _empty_observation(
                product,
                final_url=final_url or self.lookup_url,
                http_status=http_status,
                page_classification=page_classification,
                lookup_status=_lookup_status_from_page(page_classification),
                warnings=[page_classification],
            )

        match = extract_match(html=html, text=text, requested_part_number=product.oem_part_number, final_url=final_url)
        if match is None or match.part_number is None:
            status = "part_not_found" if NOT_FOUND_RE.search(text) else "lookup_failed"
            return _empty_observation(
                product,
                final_url=final_url or self.lookup_url,
                http_status=http_status,
                page_classification="not_found" if status == "part_not_found" else "unknown",
                lookup_status=status,
                warnings=[status],
            )

        warnings: list[str] = []
        selling_price = match.selling_price
        price_source = match.price_source

        # A discontinued or out-of-stock listing keeps showing its last price.
        # Treating that as a live competitor price would drag suggestions down.
        if selling_price is not None and match.availability_status in UNSELLABLE_AVAILABILITY:
            warnings.append(f"price_ignored_{match.availability_status}")
            selling_price = None
            price_source = "unavailable_listing"

        if match.superseded_part_number:
            warnings.append("superseded")

        lookup_status = _lookup_status(
            selling_price=selling_price,
            availability_status=match.availability_status,
            superseded_part_number=match.superseded_part_number,
        )
        return CompetitorObservation(
            competitor_key=self.competitor_key,
            manufacturer=normalize_manufacturer(product.manufacturer),
            oem_part_number=product.oem_part_number,
            observed_part_number=match.part_number,
            product_name=match.product_name,
            canonical_url=match.product_url or final_url or self.lookup_url,
            http_status=http_status,
            page_classification="normal_product",
            session_status="public",
            price_visibility="visible" if selling_price is not None else "not_present",
            selling_price=selling_price,
            reference_price=None,
            price_display_type="regular" if selling_price is not None else "unknown",
            selling_price_confidence="high" if selling_price is not None else "low",
            reference_price_confidence="low",
            availability_raw=match.availability_raw,
            availability_status=match.availability_status,
            supersession_detected=bool(match.superseded_part_number),
            superseded_by_raw=match.superseded_part_number,
            warnings=warnings,
            parser_version="revzilla-v1",
            raw_evidence_summary={
                "competitor": "RevZilla",
                "requested_part_number": product.oem_part_number,
                "normalized_part_number": normalize_part_number_for_match(product.oem_part_number),
                "matched_part_number": match.part_number,
                "price_source": price_source,
                "product_url": match.product_url,
                "lookup_status": lookup_status,
                "availability_status": match.availability_status,
                "currency": "USD",
                "fulfilled_by": "Montgomeryville Cycle Center",
            },
            parse_confidence="high" if selling_price is not None else "medium",
        )

    def search_result_product_url(self, html: str, product: PartRecord) -> str | None:
        """Pick the OEM product link for this part from a search results page.

        RevZilla product URLs embed a description slug, so the collector has to
        search first and then follow the matching result. Only links whose slug
        contains the requested part number are accepted, so a near-miss result
        is never followed.
        """
        return select_search_result(html, product.oem_part_number)

    def normalize_availability(self, raw: str | None) -> str:
        return normalize_availability(raw)

    def normalize_supersession(self, raw: str | None) -> tuple[bool, str | None]:
        return (bool(raw), raw)


def select_search_result(html: str, requested_part_number: str) -> str | None:
    """Return the first /oem/ link whose slug matches the requested part."""
    normalized = normalize_part_number_for_match(requested_part_number)
    if not normalized:
        return None
    for path in PRODUCT_LINK_RE.findall(html or ""):
        if normalize_part_number_for_match(path).find(normalized) != -1:
            return f"{BASE_URL}{path}"
    return None


def build_search_url(part_number: str) -> str:
    if not part_number.strip():
        raise ValueError("Part number cannot be blank.")
    return f"{SEARCH_URL}?{urlencode({'query': part_number.strip()})}"


def normalize_part_number_for_match(value: str) -> str:
    return "".join(char for char in value.upper() if char.isalnum())


def normalize_availability(raw: str | None) -> str:
    if not raw:
        return "unknown"
    lowered = raw.lower()
    if DISCONTINUED_RE.search(lowered):
        return "discontinued"
    if OUT_OF_STOCK_RE.search(lowered):
        return "out_of_stock"
    if BACKORDER_RE.search(lowered):
        return "backordered"
    if IN_STOCK_RE.search(lowered):
        return "in_stock"
    return "unknown"


def extract_availability(text: str, html: str = "") -> tuple[str | None, str]:
    """Availability wins over price: check the strongest negative signal first."""
    inventory = SAILTHRU_INVENTORY_RE.search(html or text)
    discontinued = DISCONTINUED_RE.search(text)
    if discontinued:
        return (" ".join(discontinued.group(0).split()), "discontinued")
    out_of_stock = OUT_OF_STOCK_RE.search(text)
    if out_of_stock:
        return (" ".join(out_of_stock.group(0).split()), "out_of_stock")
    if inventory is not None and inventory.group(1) == "0":
        return ("inventory 0", "out_of_stock")
    backorder = BACKORDER_RE.search(text)
    if backorder:
        return (" ".join(backorder.group(0).split()), "backordered")
    in_stock = IN_STOCK_RE.search(text)
    if in_stock:
        return (" ".join(in_stock.group(0).split()), "in_stock")
    return (None, "unknown")


def extract_price(text: str, html: str = "") -> tuple[Decimal | None, str]:
    """Prefer the cents value in the page metadata over parsing dollar text."""
    meta = SAILTHRU_PRICE_RE.search(html or "")
    if meta:
        cents = int(meta.group(1))
        if cents > 0:
            return (Decimal(cents) / Decimal("100"), "page_metadata")
    visible = CURRENT_PRICE_RE.search(text)
    if visible:
        parsed = parse_money(visible.group(1)).value
        if parsed is not None:
            return (parsed, "visible_price")
    return (None, "not_available")


def extract_match(
    *,
    html: str,
    text: str,
    requested_part_number: str,
    final_url: str | None = None,
) -> RevzillaMatch | None:
    """Read a product page, confirming the OEM part number actually matches.

    Search results are followed by the collector, so by the time this runs the
    page should be a product page. The part number is still verified, because
    landing on a near-miss product would silently record the wrong price.
    """
    normalized = normalize_part_number_for_match(requested_part_number)
    structured = _structured_product(html, requested_part_number)

    observed = None
    for candidate in OEM_PART_NUMBER_RE.findall(text):
        if normalize_part_number_for_match(candidate) == normalized:
            observed = candidate
            break
    if observed is None and structured:
        sku = str(structured.get("sku") or "")
        if normalize_part_number_for_match(sku) == normalized:
            observed = sku
    if observed is None:
        return None

    selling_price, price_source = extract_price(text, html)
    if selling_price is None and structured and structured.get("price") is not None:
        selling_price = structured["price"]
        price_source = "structured_data"

    availability_raw, availability_status = extract_availability(text, html)
    if availability_status == "unknown" and structured and structured.get("availability_raw"):
        availability_raw = structured["availability_raw"]
        availability_status = normalize_availability(availability_raw)

    superseded = SUPERSESSION_RE.search(text)
    product_url = _product_url(html, final_url)

    return RevzillaMatch(
        part_number=observed,
        product_name=(structured or {}).get("name") or _product_name(text, requested_part_number),
        product_url=product_url,
        selling_price=selling_price,
        availability_raw=availability_raw,
        availability_status=availability_status,
        superseded_part_number=superseded.group(1) if superseded else None,
        price_source=price_source,
    )


def _product_url(html: str, final_url: str | None) -> str | None:
    if final_url and "/oem/" in final_url:
        return final_url
    match = PRODUCT_LINK_RE.search(html or "")
    if match:
        return f"{BASE_URL}{match.group(1)}"
    return final_url


def _product_name(text: str, requested_part_number: str) -> str | None:
    normalized = normalize_part_number_for_match(requested_part_number)
    for line in (line.strip() for line in text.splitlines()):
        if not line or len(line) > 120:
            continue
        if normalized in normalize_part_number_for_match(line) and not CURRENT_PRICE_RE.search(line):
            return line
    return None


def _structured_product(html: str, requested_part_number: str) -> dict[str, Any] | None:
    normalized = normalize_part_number_for_match(requested_part_number)
    pattern = r"<script[^>]+type=[\"']application/ld\+json[\"'][^>]*>(.*?)</script>"
    for raw in re.findall(pattern, html or "", flags=re.IGNORECASE | re.DOTALL):
        try:
            parsed = json.loads(html_lib.unescape(raw).strip())
        except json.JSONDecodeError:
            continue
        for item in _walk(parsed):
            if not isinstance(item, dict) or item.get("@type") != "Product":
                continue
            sku = str(item.get("sku") or item.get("mpn") or "")
            if normalize_part_number_for_match(sku) != normalized:
                continue
            offers = item.get("offers") if isinstance(item.get("offers"), dict) else {}
            availability = str(offers.get("availability") or "")
            return {
                "name": item.get("name"),
                "sku": sku,
                "price": _decimal(offers.get("price")),
                "availability_raw": availability.rsplit("/", 1)[-1] if availability else None,
            }
    return None


def _walk(value: Any):
    if isinstance(value, dict):
        yield value
        graph = value.get("@graph")
        if isinstance(graph, list):
            for item in graph:
                yield from _walk(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk(item)


def _decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value).replace(",", "").strip())
    except Exception:
        return None


def _classify_page(text: str, http_status: int | None) -> str:
    if http_status in {403, 429} or BLOCK_RE.search(text):
        return "blocked"
    if CAPTCHA_RE.search(text):
        return "challenge"
    if http_status == 404:
        return "not_found"
    return "normal_product"


def _lookup_status_from_page(value: str) -> str:
    return {
        "blocked": "blocked_or_rate_limited",
        "challenge": "captcha_detected",
        "not_found": "part_not_found",
    }.get(value, "lookup_failed")


def _lookup_status(*, selling_price: Decimal | None, availability_status: str, superseded_part_number: str | None) -> str:
    if superseded_part_number:
        return "superseded"
    if availability_status == "discontinued":
        return "discontinued"
    if availability_status == "out_of_stock":
        return "out_of_stock"
    if selling_price is not None:
        return "price_found"
    return "lookup_failed"


def _clean_text(value: str) -> str:
    without_tags = re.sub(r"<[^>]+>", "\n", value)
    return "\n".join(line.strip() for line in html_lib.unescape(without_tags).splitlines() if line.strip())


def _empty_observation(
    product: PartRecord,
    *,
    final_url: str,
    http_status: int | None,
    page_classification: str,
    lookup_status: str,
    warnings: list[str],
) -> CompetitorObservation:
    return CompetitorObservation(
        competitor_key="revzilla",
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
        parser_version="revzilla-v1",
        raw_evidence_summary={
            "competitor": "RevZilla",
            "requested_part_number": product.oem_part_number,
            "lookup_status": lookup_status,
            "currency": "USD",
        },
        parse_confidence="low",
    )
