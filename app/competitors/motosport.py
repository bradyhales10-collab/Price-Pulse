from __future__ import annotations

import html as html_lib
import re
from decimal import Decimal

from app.competitors.base import CompetitorCapabilities, CompetitorObservation
from app.manufacturer_registry import competitor_manufacturers
from app.models import PartRecord
from app.parsers.money_parser import parse_money


MONEY_RE = re.compile(r"\$[\d,]+(?:\.\d{2})?")
PERCENT_RE = re.compile(r"(?P<percent>\d+)%\s*off", re.IGNORECASE)
SAVE_RE = re.compile(r"Save\s*(?P<amount>\$[\d,]+(?:\.\d{2})?)", re.IGNORECASE)
AVAILABILITY_RE = re.compile(r"(In Stock|Expected to Ship in [^\n\r<]+|Out of Stock|Unavailable)", re.IGNORECASE)
PRODUCT_HEADING_RE = re.compile(r"^(?P<name>.+?)\s*\(\s*(?P<part>[A-Za-z0-9_.\-/]+)\s*\)\s*$")
FINANCING_LINE_RE = re.compile(r"\b(?:or\s+)?(?:monthly\s+)?payments?\s+(?:as\s+low\s+as|of)\b", re.IGNORECASE)
ORDER_THRESHOLD_RE = re.compile(r"\borders?\s+over\s+\$[\d,]+(?:\.\d{2})?", re.IGNORECASE)
SEE_PRICE_IN_CART_RE = re.compile(r"\b(see price in cart|add to cart to see price|price in cart|view price in cart)\b", re.IGNORECASE)
BAD_PRODUCT_NAME_PHRASES = (
    "skip to content",
    "menu",
    "search",
    "cart",
    "account",
    "oem parts",
    "page not found",
    "not found",
    "access denied",
    "error",
)
REGION_STOP_PREFIXES = (
    "customers also viewed",
    "related products",
    "reviews",
    "recently viewed",
    "shop by",
    "need help",
)


class MotoSportAdapter:
    competitor_key = "motosport"
    display_name = "MotoSport"
    supported_manufacturers = competitor_manufacturers("motosport")
    capabilities = CompetitorCapabilities(
        requires_login=False,
        supports_public_price=True,
        supports_direct_part_url=True,
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
        return f"https://www.motosport.com/oem-parts/part-number/{product.oem_part_number.strip()}"

    def parse_product_page(self, html: str, product: PartRecord, *, visible_text: str = "", final_url: str | None = None, http_status: int | None = None) -> CompetitorObservation:
        text = _clean_text(visible_text or html)
        lines = _clean_lines(text)
        lowered = text.lower()
        warnings: list[str] = []
        page_classification = "normal_product"
        if http_status in {401, 403, 429} or any(phrase in lowered for phrase in ("access denied", "request blocked", "too many requests")):
            page_classification = "blocked"
        elif any(phrase in lowered for phrase in ("captcha", "verify you are human", "checking your browser")):
            page_classification = "challenge"
        elif http_status == 404 or "not found" in lowered:
            page_classification = "not_found"
        heading = _find_product_heading(lines, product.oem_part_number)
        association = _product_association(product.oem_part_number, heading, page_classification)
        region_lines, region_start = _selected_region(lines, heading["line_index"] if heading else None)
        region_text = "\n".join(region_lines)
        selected_product_region = region_text if association["confirmed"] else ""
        price_evidence = _price_evidence(lines, region_start, len(region_lines), association["confirmed"])
        product_name = heading["product_name"] if heading else None
        observed_part_number = heading["observed_part_number"] if heading else None
        selling_price = None
        reference_price = None
        savings_percent = None
        savings_amount = None
        availability_raw = None
        price_visibility = "unknown"
        if page_classification != "normal_product":
            warnings.append(page_classification)
            price_visibility = "not_present"
        elif not association["confirmed"]:
            warnings.append("product_association_not_confirmed")
            price_visibility = "unknown"
        else:
            see_price_in_cart = _see_price_in_cart_detected(region_lines)
            price_candidates = [candidate for candidate in price_evidence["candidates"] if candidate["role"] == "product_price"]
            cluster = _parse_product_price_cluster(price_candidates, region_lines, region_start, see_price_in_cart=see_price_in_cart)
            selling_price = cluster["selling_price"]
            reference_price = cluster["reference_price"]
            savings_percent = cluster["savings_percent"]
            savings_amount = cluster["savings_amount"]
            price_evidence["price_cluster"] = cluster["evidence"]
            price_evidence["see_price_in_cart_detected"] = see_price_in_cart
            if cluster["ambiguous"]:
                warnings.append("ambiguous_price_candidates")
                for candidate in price_candidates:
                    candidate["rejection_reason"] = candidate["rejection_reason"] or "ambiguous_price_candidates"
            else:
                for candidate in price_candidates:
                    if candidate["raw_value"] == cluster["selling_raw_value"] and candidate["line_index"] == cluster["selling_line_index"]:
                        candidate["accepted"] = True
                        candidate["accepted_role"] = "selling_price"
                    elif candidate["raw_value"] == cluster["reference_raw_value"] and candidate["line_index"] == cluster["reference_line_index"]:
                        candidate["accepted"] = True
                        candidate["accepted_role"] = "reference_price"
            if see_price_in_cart:
                price_visibility = "see_price_in_cart"
                price_evidence["cart_price_probe_todo"] = "Future cart_price_probe is disabled and would require explicit user approval, separate legal review, strict max parts, no checkout, cart cleanup, and separate experimental status."
                warnings.append("selling_price_hidden_in_cart")
            elif selling_price is not None:
                price_visibility = "visible"
            elif not price_candidates:
                price_visibility = "not_present"
            else:
                price_visibility = "unknown"
            if selling_price is None and not see_price_in_cart:
                warnings.append("selling_price_not_found")
            if reference_price is not None and price_visibility != "see_price_in_cart" and not (savings_percent is not None or savings_amount is not None):
                warnings.append("reference_price_role_ambiguous")
            availability_raw = _availability(region_text)
            if not availability_raw:
                warnings.append("availability_not_found")
        price_display_type = "unknown"
        if price_visibility == "see_price_in_cart":
            price_display_type = "cart_price_hidden"
        elif selling_price is not None:
            price_display_type = "discounted" if reference_price is not None or savings_percent is not None or savings_amount is not None else "regular"
        confidence = "low"
        if price_visibility == "see_price_in_cart" and page_classification == "normal_product" and association["confirmed"]:
            confidence = "high" if reference_price is not None else "medium"
        elif selling_price is not None and page_classification == "normal_product" and association["confirmed"]:
            confidence = "medium" if warnings else "high"
        evidence_summary = {
            "product_association": association,
            "selected_product_region": selected_product_region,
            "price_evidence": price_evidence,
            "availability": availability_raw,
        }
        return CompetitorObservation(
            competitor_key=self.competitor_key,
            manufacturer=product.manufacturer,
            oem_part_number=product.oem_part_number,
            observed_part_number=observed_part_number,
            product_name=product_name,
            canonical_url=final_url or self.build_product_url(product),
            http_status=http_status,
            page_classification=page_classification,
            session_status="public",
            price_visibility=price_visibility,
            selling_price=selling_price,
            reference_price=reference_price,
            savings_percent=savings_percent,
            savings_amount=savings_amount,
            price_display_type=price_display_type,
            selling_price_confidence="low" if price_visibility == "see_price_in_cart" else confidence,
            reference_price_confidence="high" if reference_price is not None and "reference_price_role_ambiguous" not in warnings else "low",
            availability_raw=availability_raw,
            availability_status=self.normalize_availability(availability_raw),
            warnings=warnings,
            parser_version="motosport-probe-v3",
            raw_evidence_summary=evidence_summary,
            parse_confidence=confidence,
        )

    def normalize_availability(self, raw: str | None) -> str:
        if not raw:
            return "unknown"
        lowered = raw.lower()
        if "in stock" in lowered:
            return "in_stock"
        if "expected to ship" in lowered:
            return "ships_in"
        if "out of stock" in lowered:
            return "out_of_stock"
        if "unavailable" in lowered:
            return "unavailable"
        return "unknown"

    def normalize_supersession(self, raw: str | None) -> tuple[bool, str | None]:
        return (False, None)


def _clean_text(value: str) -> str:
    without_tags = re.sub(r"<[^>]+>", "\n", value)
    return "\n".join(line.strip() for line in html_lib.unescape(without_tags).splitlines() if line.strip())


def _clean_lines(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if line.strip()]


def _find_product_heading(lines: list[str], part_number: str) -> dict[str, object] | None:
    requested = _normalize_part(part_number)
    for index, line in enumerate(lines):
        match = PRODUCT_HEADING_RE.match(line.strip())
        if not match:
            continue
        product_name = " ".join(match.group("name").split())
        observed_part_number = match.group("part").strip()
        if _normalize_part(observed_part_number) != requested:
            continue
        if _is_bad_product_name(product_name):
            continue
        return {
            "line_index": index,
            "product_name": product_name,
            "observed_part_number": observed_part_number,
            "heading_text": line.strip(),
        }
    return None


def _product_association(part_number: str, heading: dict[str, object] | None, page_classification: str) -> dict[str, object]:
    if page_classification != "normal_product":
        return {
            "confirmed": False,
            "requested_part_number": part_number,
            "observed_part_number": heading["observed_part_number"] if heading else None,
            "product_name": heading["product_name"] if heading else None,
            "reason": f"page_classification_{page_classification}",
        }
    if not heading:
        return {
            "confirmed": False,
            "requested_part_number": part_number,
            "observed_part_number": None,
            "product_name": None,
            "reason": "part_heading_not_found",
        }
    return {
        "confirmed": True,
        "requested_part_number": part_number,
        "observed_part_number": heading["observed_part_number"],
        "product_name": heading["product_name"],
        "reason": "matching_product_heading",
    }


def _selected_region(lines: list[str], heading_index: int | None) -> tuple[list[str], int]:
    if heading_index is None:
        return ([], 0)
    selected: list[str] = []
    for line in lines[heading_index : heading_index + 80]:
        lowered = line.lower()
        if selected and any(lowered.startswith(prefix) for prefix in REGION_STOP_PREFIXES):
            break
        selected.append(line)
    return (selected, heading_index)


def _price_evidence(lines: list[str], region_start: int, region_length: int, association_confirmed: bool) -> dict[str, object]:
    region_end = region_start + region_length
    candidates: list[dict[str, object]] = []
    heading_line_index = region_start if association_confirmed and region_length else None
    for index, line in enumerate(lines):
        in_region = association_confirmed and region_start <= index < region_end
        for match in MONEY_RE.finditer(line):
            raw_value = match.group(0)
            role = "product_price"
            rejection_reason = None
            if _is_savings_amount(line, match):
                role = "savings_amount"
                rejection_reason = "savings_amount_not_selling_price"
            elif _is_order_threshold(line, match):
                role = "order_threshold"
                rejection_reason = "order_threshold_not_selling_price"
            elif _is_financing_amount(line, match):
                role = "financing_payment"
                rejection_reason = "financing_payment_not_selling_price"
            elif not in_region:
                rejection_reason = "outside_selected_product_region"
            candidates.append(
                {
                    "raw_value": raw_value,
                    "line_index": index,
                    "raw_text": line,
                    "region_identifier": "selected_product_region" if in_region else "global_or_unselected_region",
                    "distance_to_product_heading_lines": index - heading_line_index if heading_line_index is not None else None,
                    "distance_to_requested_part_number_lines": index - heading_line_index if heading_line_index is not None else None,
                    "within_selected_product_region": in_region,
                    "role": role if in_region else "global_or_unselected",
                    "accepted": False,
                    "accepted_role": None,
                    "rejection_reason": rejection_reason,
                }
            )
    for candidate in candidates:
        if candidate["role"] == "product_price" and not candidate["rejection_reason"]:
            continue
        candidate["accepted"] = False
    return {
        "region_identifier": "selected_product_region",
        "region_start_line_index": region_start if association_confirmed else None,
        "region_end_line_index": region_end - 1 if association_confirmed and region_length else None,
        "candidate_count": len(candidates),
        "selected_region_candidate_count": sum(1 for candidate in candidates if candidate["within_selected_product_region"]),
        "candidates": candidates[:25],
    }


def _parse_product_price_cluster(price_candidates: list[dict[str, object]], region_lines: list[str], region_start: int, *, see_price_in_cart: bool = False) -> dict[str, object]:
    result: dict[str, object] = {
        "selling_price": None,
        "reference_price": None,
        "savings_percent": None,
        "savings_amount": None,
        "selling_raw_value": None,
        "selling_line_index": None,
        "reference_raw_value": None,
        "reference_line_index": None,
        "ambiguous": False,
        "evidence": {
            "rule": "no_product_price_candidates",
            "savings_math_valid": None,
            "notes": [],
        },
    }
    if not price_candidates:
        return result
    if see_price_in_cart:
        reference = price_candidates[0]
        result.update(
            {
                "reference_price": parse_money(str(reference["raw_value"])).value,
                "reference_raw_value": reference["raw_value"],
                "reference_line_index": reference["line_index"],
                "evidence": {
                    "rule": "see_price_in_cart_reference_only",
                    "savings_math_valid": None,
                    "notes": ["Product panel says price is hidden in cart; visible product-panel dollar amount is reference/list evidence only."],
                },
            }
        )
        return result
    if len(price_candidates) == 1:
        selling = price_candidates[0]
        result.update(
            {
                "selling_price": parse_money(str(selling["raw_value"])).value,
                "selling_raw_value": selling["raw_value"],
                "selling_line_index": selling["line_index"],
                "evidence": {
                    "rule": "single_product_associated_price",
                    "savings_math_valid": None,
                    "notes": ["No reference/list/save evidence found near the price cluster."],
                },
            }
        )
        return result
    if len(price_candidates) == 2 and abs(int(price_candidates[0]["line_index"]) - int(price_candidates[1]["line_index"])) <= 1:
        selling = price_candidates[0]
        reference = price_candidates[1]
        selling_value = parse_money(str(selling["raw_value"])).value
        reference_value = parse_money(str(reference["raw_value"])).value
        nearby_text = _nearby_price_cluster_text(region_lines, region_start, int(selling["line_index"]))
        savings_percent = _savings_percent(nearby_text)
        savings_amount = _savings_amount(nearby_text)
        math_valid = _savings_math_valid(selling_value, reference_value, savings_amount, savings_percent)
        if savings_amount is not None or savings_percent is not None:
            result.update(
                {
                    "selling_price": selling_value,
                    "reference_price": reference_value,
                    "savings_percent": savings_percent,
                    "savings_amount": savings_amount,
                    "selling_raw_value": selling["raw_value"],
                    "selling_line_index": selling["line_index"],
                    "reference_raw_value": reference["raw_value"],
                    "reference_line_index": reference["line_index"],
                    "evidence": {
                        "rule": "same_line_current_reference_with_nearby_savings",
                        "savings_math_valid": math_valid,
                        "notes": ["First product-panel price is treated as current/customer-payable; second same-line price is treated as reference."],
                    },
                }
            )
            return result
    result["ambiguous"] = True
    result["evidence"] = {
        "rule": "role_evidence_insufficient",
        "savings_math_valid": None,
        "notes": ["Multiple product-panel price candidates remain after rejecting financing, thresholds, and savings amounts."],
    }
    return result


def _nearby_price_cluster_text(region_lines: list[str], region_start: int, price_line_index: int) -> str:
    local_index = max(0, price_line_index - region_start)
    start = max(0, local_index - 1)
    end = min(len(region_lines), local_index + 3)
    return "\n".join(region_lines[start:end])


def _see_price_in_cart_detected(region_lines: list[str]) -> bool:
    if not region_lines:
        return False
    price_line_indexes = [index for index, line in enumerate(region_lines) if MONEY_RE.search(line)]
    for index, line in enumerate(region_lines):
        if not SEE_PRICE_IN_CART_RE.search(line):
            continue
        if not price_line_indexes:
            return index <= 8
        nearest_price_distance = min(abs(index - price_index) for price_index in price_line_indexes)
        if nearest_price_distance <= 4:
            return True
    return False


def _is_bad_product_name(value: str) -> bool:
    normalized = " ".join(value.lower().split())
    return not normalized or any(phrase == normalized or phrase in normalized for phrase in BAD_PRODUCT_NAME_PHRASES)


def _normalize_part(value: str) -> str:
    return value.strip().casefold()


def _is_savings_amount(line: str, money_match: re.Match[str]) -> bool:
    save_match = SAVE_RE.search(line)
    return bool(save_match and save_match.start("amount") <= money_match.start() and save_match.end("amount") >= money_match.end())


def _is_financing_amount(line: str, money_match: re.Match[str]) -> bool:
    return bool(FINANCING_LINE_RE.search(line) and "payments" in line[: money_match.start()].lower())


def _is_order_threshold(line: str, money_match: re.Match[str]) -> bool:
    match = ORDER_THRESHOLD_RE.search(line)
    return bool(match and match.start() <= money_match.start() and match.end() >= money_match.end())


def _savings_math_valid(selling_price: Decimal | None, reference_price: Decimal | None, savings_amount: Decimal | None, savings_percent: int | None) -> bool | None:
    if selling_price is None or reference_price is None:
        return None
    expected_savings = reference_price - selling_price
    amount_valid = True
    percent_valid = True
    if savings_amount is not None:
        amount_valid = abs(expected_savings - savings_amount) <= Decimal("0.01")
    if savings_percent is not None and reference_price > 0:
        actual_percent = (expected_savings / reference_price) * Decimal("100")
        percent_valid = abs(actual_percent - Decimal(savings_percent)) <= Decimal("1.25")
    if savings_amount is None and savings_percent is None:
        return None
    return amount_valid and percent_valid


def _savings_percent(text: str) -> int | None:
    match = PERCENT_RE.search(text)
    return int(match.group("percent")) if match else None


def _savings_amount(text: str) -> Decimal | None:
    match = SAVE_RE.search(text)
    return parse_money(match.group("amount")).value if match else None


def _availability(text: str) -> str | None:
    match = AVAILABILITY_RE.search(text)
    return " ".join(match.group(0).split()) if match else None
