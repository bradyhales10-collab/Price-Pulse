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
from urllib.parse import urlencode, urlparse

from app.competitors.base import CompetitorCapabilities, CompetitorObservation
from app.manufacturer_registry import competitor_manufacturers, normalize_manufacturer
from app.models import PartRecord

BASE_URL = "https://www.revzilla.com"
SEARCH_URL = f"{BASE_URL}/search"

# The page metadata is the reliable source: it carries the price in cents and
# the stock level, both of which the visible page can contradict.
#   meta-sailthru.price     -> 32642 means $326.42
#   meta-sailthru.inventory -> 0 means out of stock
#   meta-sailthru.tags      -> includes stock-level-out-of-stock
META_TAG_RE = re.compile(r"<meta\b[^>]*>", re.IGNORECASE)
META_CONTENT_RE = re.compile(r"content=[\"\']([^\"\']*)[\"\']", re.IGNORECASE)

# Dollars and cents render in separate elements, so extracted text can arrive as
# "Current price is $326" followed by "42" on the next line. Cents are captured
# separately and their absence is treated as a warning, never as .00.
CURRENT_PRICE_RE = re.compile(
    r"current\s+price\s+is\s*\$\s*([\d,]+)(?:\s*\.\s*(\d{2}))?",
    re.IGNORECASE,
)
OEM_PART_NUMBER_RE = re.compile(r"OEM\s+Part\s+Number:\s*([A-Z0-9][A-Z0-9.\-/]*)", re.IGNORECASE)
PRODUCT_LINK_RE = re.compile(r"href=[\"\'](/oem/[^\"\'?#]+)[\"\']", re.IGNORECASE)

# No leading \b: the product table renders as "AvailabilityOut of Stock" with no
# separating space, which a word boundary would reject.
DISCONTINUED_RE = re.compile(
    r"(discontinued|no\s+longer\s+available|closeout:\s*this\s+product\s+is\s+no\s+longer\s+available)",
    re.IGNORECASE,
)
OUT_OF_STOCK_RE = re.compile(r"(out\s+of\s+stock|currently\s+unavailable|sold\s+out)", re.IGNORECASE)
IN_STOCK_RE = re.compile(r"(in\s+stock|instock)", re.IGNORECASE)
BACKORDER_RE = re.compile(r"(back\s*ordered|backorder|pre-?order)", re.IGNORECASE)

CAPTCHA_RE = re.compile(r"\b(captcha|verify\s+you\s+are\s+human|checking\s+your\s+browser)\b", re.IGNORECASE)
BLOCK_RE = re.compile(r"\b(access\s+denied|rate\s+limited|too\s+many\s+requests|forbidden)\b", re.IGNORECASE)
NOT_FOUND_RE = re.compile(r"\b(no\s+results|0\s+results|we\s+couldn'?t\s+find|no\s+products\s+found)\b", re.IGNORECASE)
SUPERSESSION_RE = re.compile(r"\bsupersedes\s+part\s+([A-Z0-9][A-Z0-9.\-/]*)", re.IGNORECASE)

# Exact out-of-stock product pages still carry useful competitor pricing. Keep
# that price in the catalog while preserving the availability status so users
# can distinguish a current in-stock offer from an unavailable listed price.
# Discontinued pages remain excluded because their price is no longer current.


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
            exact_search_exhausted = _is_search_results_url(final_url)
            status = "part_not_found" if NOT_FOUND_RE.search(text) or exact_search_exhausted else "lookup_failed"
            warning = "search_no_exact_oem_match" if exact_search_exhausted else status
            return _empty_observation(
                product,
                final_url=final_url or self.lookup_url,
                http_status=http_status,
                page_classification="not_found" if status == "part_not_found" else "unknown",
                lookup_status=status,
                warnings=[warning],
            )

        warnings: list[str] = []
        selling_price = match.selling_price
        price_source = match.price_source

        if selling_price is not None and match.availability_status == "discontinued":
            warnings.append("price_ignored_discontinued")
            selling_price = None
            price_source = "unavailable_listing"
        elif selling_price is not None and match.availability_status == "out_of_stock":
            warnings.append("listed_price_out_of_stock")
        elif selling_price is not None and match.availability_status not in {"in_stock", "backordered"}:
            warnings.append(f"price_ignored_{match.availability_status or 'unknown_availability'}")
            selling_price = None
            price_source = "unavailable_listing"

        # A price without cents is probably truncated, so it is not recorded.
        if selling_price is not None and price_source == "visible_price_dollars_only":
            warnings.append("price_ignored_missing_cents")
            selling_price = None

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
                "product_association": {
                    "confirmed": True,
                    "requested_part_number": product.oem_part_number,
                    "observed_part_number": match.part_number,
                    "basis": "oem_part_number_on_product_page",
                },
                "price_evidence": {
                    "price_source": price_source,
                    "availability_status": match.availability_status,
                    "availability_raw": match.availability_raw,
                    "price_accepted": selling_price is not None,
                },
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


def _is_search_results_url(value: str | None) -> bool:
    if not value:
        return False
    parsed = urlparse(value)
    return parsed.netloc.lower() in {"revzilla.com", "www.revzilla.com"} and parsed.path.rstrip("/") == "/search"


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


def _money_2dp(value: Decimal) -> Decimal:
    """Prices always carry two decimals, so 6.70 does not read as 6.7."""
    return value.quantize(Decimal("0.01"))


def meta_value(html: str, name: str) -> str | None:
    """Read a meta tag's content regardless of attribute order.

    Also accepts a flattened "meta-name: value" rendering, so the same code
    works on extracted text as well as raw HTML.
    """
    for tag in META_TAG_RE.findall(html or ""):
        if not re.search(rf"[\"\' ]{re.escape(name)}[\"\' ]", tag, re.IGNORECASE):
            continue
        content = META_CONTENT_RE.search(tag)
        if content:
            return content.group(1).strip()
    flattened = re.search(rf"(?:meta-)?{re.escape(name)}\s*:\s*([^\n\r]+)", html or "", re.IGNORECASE)
    return flattened.group(1).strip() if flattened else None


def extract_availability(text: str, html: str = "") -> tuple[str | None, str]:
    """Determine stock level, preferring page metadata over visible wording.

    Order matters. The visible page carries an "Add to Cart" button and a
    "Ships FREE" badge even on listings that are out of stock, so neither can
    be used as evidence of availability.
    """
    source = html or text

    # Discontinued is checked first: it is a stronger statement than out of
    # stock, because the part is not coming back at all.
    discontinued = DISCONTINUED_RE.search(text)
    if discontinued:
        return (" ".join(discontinued.group(0).split()), "discontinued")

    tags = (meta_value(source, "sailthru.tags") or "").lower()
    if "stock-level-out-of-stock" in tags:
        return ("stock-level-out-of-stock", "out_of_stock")

    inventory = meta_value(source, "sailthru.inventory")
    if inventory is not None and inventory.isdigit():
        if int(inventory) == 0:
            return ("inventory 0", "out_of_stock")
        return (f"inventory {inventory}", "in_stock")

    out_of_stock = OUT_OF_STOCK_RE.search(text)
    if out_of_stock:
        return (" ".join(out_of_stock.group(0).split()), "out_of_stock")
    backorder = BACKORDER_RE.search(text)
    if backorder:
        return (" ".join(backorder.group(0).split()), "backordered")
    in_stock = IN_STOCK_RE.search(text)
    if in_stock:
        return (" ".join(in_stock.group(0).split()), "in_stock")
    return (None, "unknown")


def extract_price(text: str, html: str = "") -> tuple[Decimal | None, str]:
    """Read the price, preferring the cents value in the page metadata.

    The visible price splits dollars and cents across elements, so extracted
    text can read "Current price is $326" with the cents on the next line.
    Whole dollars are therefore reported as a distinct source so the caller can
    treat them with suspicion rather than silently recording the wrong price.
    """
    source = html or text
    cents = meta_value(source, "sailthru.price")
    if cents is not None and cents.isdigit() and int(cents) > 0:
        return (_money_2dp(Decimal(int(cents)) / Decimal("100")), "page_metadata")

    visible = CURRENT_PRICE_RE.search(text)
    if visible:
        dollars = visible.group(1).replace(",", "")
        fraction = visible.group(2)
        if not dollars.isdigit():
            return (None, "not_available")
        if fraction:
            return (_money_2dp(Decimal(f"{dollars}.{fraction}")), "visible_price")
        # Cents were not found next to the dollars, so this may be truncated.
        return (_money_2dp(Decimal(dollars)), "visible_price_dollars_only")
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
    if availability_status == "unknown":
        return "availability_unknown"
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
