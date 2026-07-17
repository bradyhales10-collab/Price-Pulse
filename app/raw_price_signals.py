from __future__ import annotations

import html as html_lib
import json
import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from app.parsers.money_parser import parse_money
from app.schemas.product_observation import ParseConfidence, ProductObservation


MONEYISH_RE = re.compile(r"\$?\s*\d{1,3}(?:,\d{3})*(?:\.\d{2})?|\$?\s*\d+(?:\.\d{2})?")
SCRIPT_RE = re.compile(r"<script(?P<attrs>[^>]*)>(?P<body>.*?)</script>", re.IGNORECASE | re.DOTALL)
META_RE = re.compile(r"<meta(?P<attrs>[^>]*)>", re.IGNORECASE | re.DOTALL)
ELEMENT_RE = re.compile(r"<(?P<tag>[a-zA-Z][a-zA-Z0-9-]*)(?P<attrs>[^>]*)>", re.IGNORECASE)
ATTR_RE = re.compile(r"(?P<name>[a-zA-Z_:][-a-zA-Z0-9_:.]*)(?:\s*=\s*(?P<quote>[\"'])(?P<value>.*?)(?P=quote))?")


class RawPriceSourceCategory(str, Enum):
    VISIBLE_DOM = "visible_dom"
    HIDDEN_DOM = "hidden_dom"
    META_TAG = "meta_tag"
    DATA_ATTRIBUTE = "data_attribute"
    JSON_LD = "json_ld"
    STRUCTURED_PRODUCT_DATA = "structured_product_data"
    INLINE_PRODUCT_STATE = "inline_product_state"
    UNKNOWN = "unknown"


class RawPriceRoleHint(str, Enum):
    MSRP = "msrp"
    SELLING_PRICE = "selling_price"
    LIST_PRICE = "list_price"
    OFFER_PRICE = "offer_price"
    NON_PRICE_METADATA = "non_price_metadata"
    UNKNOWN = "unknown"


SUPPORTED_PRICE_FIELDS = {"price", "offerprice", "lowprice", "highprice", "msrp", "listprice", "list_price"}
REJECTED_PRICE_FIELDS = {
    "pricevaliduntil",
    "pricecurrency",
    "pricetype",
    "pricecomponenttype",
    "eligiblequantity",
    "validfrom",
    "validthrough",
    "availabilitystarts",
    "availabilityends",
}
ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}(?:[tT]\d{2}:\d{2}:\d{2}(?:\.\d+)?Z?)?$")


@dataclass(frozen=True)
class RawPriceSignal:
    raw_text: str
    normalized_value: str | None
    source_category: RawPriceSourceCategory
    source_location: str
    associated_part_number: str | None
    product_association_evidence: list[str]
    price_role_hint: RawPriceRoleHint
    visibility: str
    confidence: ParseConfidence
    safe_context: str
    rejection_reason: str | None = None

    def to_json_dict(self) -> dict[str, object]:
        return {
            "raw_text": self.raw_text,
            "normalized_value": self.normalized_value,
            "source_category": self.source_category.value,
            "source_location": self.source_location,
            "associated_part_number": self.associated_part_number,
            "product_association_evidence": self.product_association_evidence,
            "price_role_hint": self.price_role_hint.value,
            "visibility": self.visibility,
            "confidence": self.confidence.value,
            "safe_context": self.safe_context,
            "rejection_reason": self.rejection_reason,
        }


def discover_raw_price_signals(
    *,
    html: str,
    visible_text: str,
    observation: ProductObservation,
) -> list[RawPriceSignal]:
    signals: list[RawPriceSignal] = []
    signals.extend(_json_ld_signals(html, observation))
    signals.extend(_inline_product_state_signals(html, observation))
    signals.extend(_meta_signals(html, observation))
    signals.extend(_data_attribute_signals(html, observation))
    return _deduplicate_signals(signals)


def accepted_product_price_signals(signals: list[RawPriceSignal]) -> list[RawPriceSignal]:
    return [signal for signal in signals if signal.rejection_reason is None]


def write_raw_price_signals(path: Path, *, observation: ProductObservation, signals: list[RawPriceSignal]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "oem_part_number": observation.oem_part_number,
        "product_name": observation.product_name,
        "signals": [signal.to_json_dict() for signal in signals],
    }
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _json_ld_signals(html: str, observation: ProductObservation) -> list[RawPriceSignal]:
    signals: list[RawPriceSignal] = []
    for index, match in enumerate(SCRIPT_RE.finditer(html)):
        attrs = _attrs(match.group("attrs"))
        if "ld+json" not in attrs.get("type", "").lower():
            continue
        for item in _json_items(match.group("body")):
            signals.extend(_signals_from_object(item, observation, RawPriceSourceCategory.JSON_LD, f"json_ld.script_{index}"))
    return signals


def _inline_product_state_signals(html: str, observation: ProductObservation) -> list[RawPriceSignal]:
    signals: list[RawPriceSignal] = []
    for index, match in enumerate(SCRIPT_RE.finditer(html)):
        attrs = _attrs(match.group("attrs"))
        if "ld+json" in attrs.get("type", "").lower():
            continue
        body = html_lib.unescape(match.group("body"))
        if observation.oem_part_number.lower() not in body.lower():
            continue
        for item in _json_items(body):
            signals.extend(
                _signals_from_object(
                    item,
                    observation,
                    RawPriceSourceCategory.INLINE_PRODUCT_STATE,
                    f"inline_product_state.script_{index}",
                )
            )
    return signals


def _meta_signals(html: str, observation: ProductObservation) -> list[RawPriceSignal]:
    signals: list[RawPriceSignal] = []
    for index, match in enumerate(META_RE.finditer(html)):
        attrs = _attrs(match.group("attrs"))
        key = " ".join([attrs.get("property", ""), attrs.get("name", ""), attrs.get("itemprop", "")]).lower()
        content = attrs.get("content", "")
        if "price" not in key or not content:
            continue
        normalized = _normalize_price(content)
        if normalized is None:
            continue
        evidence, rejected = _association_for_text(" ".join(attrs.values()), observation)
        signals.append(
            _signal(
                raw_text=content,
                normalized_value=normalized,
                source_category=RawPriceSourceCategory.META_TAG,
                source_location=f"meta_tag.{index}",
                associated_part_number=observation.oem_part_number if evidence else None,
                product_association_evidence=evidence,
                price_role_hint=_role_from_key(key),
                visibility="metadata",
                confidence=ParseConfidence.MEDIUM if not rejected else ParseConfidence.LOW,
                safe_context=_safe_context(" ".join(attrs.values())),
                rejection_reason=rejected,
            )
        )
    return signals


def _data_attribute_signals(html: str, observation: ProductObservation) -> list[RawPriceSignal]:
    signals: list[RawPriceSignal] = []
    for index, match in enumerate(ELEMENT_RE.finditer(html)):
        attrs = _attrs(match.group("attrs"))
        relevant = {key: value for key, value in attrs.items() if key.startswith("data-") and "price" in key}
        if not relevant:
            continue
        attrs_text = " ".join([match.group("attrs"), " ".join(relevant.values())])
        evidence, rejected = _association_for_text(attrs_text, observation)
        for key, value in relevant.items():
            normalized = _normalize_price(value)
            if normalized is None:
                continue
            signals.append(
                _signal(
                    raw_text=value,
                    normalized_value=normalized,
                    source_category=RawPriceSourceCategory.DATA_ATTRIBUTE,
                    source_location=f"data_attribute.element_{index}.{key}",
                    associated_part_number=observation.oem_part_number if evidence else None,
                    product_association_evidence=evidence,
                    price_role_hint=_role_from_key(key),
                    visibility="unknown",
                    confidence=ParseConfidence.HIGH if evidence and not rejected else ParseConfidence.LOW,
                    safe_context=_safe_context(attrs_text),
                    rejection_reason=rejected,
                )
            )
    return signals


def _signals_from_object(
    obj: Any,
    observation: ProductObservation,
    source_category: RawPriceSourceCategory,
    source_location: str,
) -> list[RawPriceSignal]:
    signals: list[RawPriceSignal] = []
    for path, value in _walk(obj):
        key = path[-1].lower() if path else ""
        field_role = _structured_field_role(path)
        if field_role is None:
            continue
        context_obj = obj
        context_text = _safe_json_context(context_obj)
        evidence, rejected = _association_for_object(context_obj, observation)
        normalized = _normalize_structured_money(value) if field_role != RawPriceRoleHint.NON_PRICE_METADATA else None
        rejection_reason = rejected
        if field_role == RawPriceRoleHint.NON_PRICE_METADATA:
            rejection_reason = "unsupported_non_price_field"
        elif normalized is None:
            rejection_reason = "invalid_structured_money_value"
        signals.append(
            _signal(
                raw_text=str(value),
                normalized_value=normalized,
                source_category=source_category,
                source_location=f"{source_location}.{'.'.join(path)}",
                associated_part_number=observation.oem_part_number if evidence else None,
                product_association_evidence=evidence,
                price_role_hint=field_role,
                visibility="structured_data",
                confidence=ParseConfidence.HIGH if evidence and not rejection_reason else ParseConfidence.LOW,
                safe_context=context_text,
                rejection_reason=rejection_reason,
            )
        )
    return signals


def _signal(**kwargs) -> RawPriceSignal:
    return RawPriceSignal(**kwargs)


def _association_for_object(obj: Any, observation: ProductObservation) -> tuple[list[str], str | None]:
    text = _safe_json_context(obj, limit=1000)
    return _association_for_text(text, observation)


def _association_for_text(text: str, observation: ProductObservation) -> tuple[list[str], str | None]:
    lowered = text.lower()
    evidence: list[str] = []
    part_identifier_match = re.search(r'"(?:sku|mpn|partnumber|part_number|oem_part_number)"\s*:\s*"(?P<part>[^"]+)"', lowered)
    if part_identifier_match and observation.oem_part_number.lower() != part_identifier_match.group("part").lower():
        return evidence, "not_associated_with_requested_product"

    if observation.oem_part_number.lower() in lowered:
        evidence.append("same object contains OEM part number")
    if observation.canonical_url and observation.canonical_url.lower() in lowered:
        evidence.append("same object contains current product URL")
    if observation.final_url and observation.final_url.lower() in lowered:
        evidence.append("same object contains current product URL")
    if observation.product_name and observation.product_name.lower() in lowered:
        evidence.append("same object contains main product title")
    has_semantic_product_offer = "product" in lowered and ("offer" in lowered or "price" in lowered)
    if has_semantic_product_offer:
        evidence.append("semantic Product/Offer relationship")

    if any(marker in lowered for marker in ("riders also bought", "recommended", "related", "recently viewed", "accessory")):
        return evidence, "non_main_product_price_context"
    strong_evidence = [item for item in evidence if item != "semantic Product/Offer relationship"]
    if not strong_evidence:
        return evidence, "not_associated_with_requested_product"
    return evidence, None


def _role_from_key(key: str) -> RawPriceRoleHint:
    lowered = key.lower()
    if "msrp" in lowered:
        return RawPriceRoleHint.MSRP
    if "list" in lowered:
        return RawPriceRoleHint.LIST_PRICE
    if "offer" in lowered:
        return RawPriceRoleHint.OFFER_PRICE
    if "price" in lowered:
        return RawPriceRoleHint.OFFER_PRICE
    return RawPriceRoleHint.UNKNOWN


def _structured_field_role(path: tuple[str, ...]) -> RawPriceRoleHint | None:
    if not path:
        return None
    key = path[-1].lower()
    compact_key = key.replace("_", "").replace("-", "")
    if compact_key in REJECTED_PRICE_FIELDS:
        return RawPriceRoleHint.NON_PRICE_METADATA
    if compact_key not in SUPPORTED_PRICE_FIELDS:
        return None
    parent_types = [part.lower() for part in path[:-1]]
    if compact_key == "msrp":
        return RawPriceRoleHint.MSRP
    if compact_key in {"listprice", "list_price"}:
        return RawPriceRoleHint.LIST_PRICE
    if compact_key in {"lowprice", "highprice"}:
        return RawPriceRoleHint.OFFER_PRICE
    if compact_key == "offerprice":
        return RawPriceRoleHint.OFFER_PRICE
    if compact_key == "price":
        if any("pricespecification" in part.lower() or "unitpricespecification" in part.lower() for part in parent_types):
            return RawPriceRoleHint.OFFER_PRICE
        if any("offers" == part.lower() or "offer" in part.lower() for part in parent_types):
            return RawPriceRoleHint.OFFER_PRICE
        return RawPriceRoleHint.OFFER_PRICE
    return None


def _normalize_price(value: Any) -> str | None:
    if isinstance(value, (int, float)):
        return f"{value:.2f}"
    raw = str(value)
    parsed = parse_money(raw if "$" in raw else f"${raw}")
    return format(parsed.value, "f") if parsed.value is not None else None


def _normalize_structured_money(value: Any) -> str | None:
    if isinstance(value, bool) or isinstance(value, (dict, list)):
        return None
    if isinstance(value, (int, float)):
        return f"{value:.2f}"
    raw = str(value).strip()
    if not raw or ISO_DATE_RE.match(raw) or "://" in raw:
        return None
    if re.search(r"[A-Za-z]", raw) and "$" not in raw:
        return None
    if not re.fullmatch(r"\$?\s*\d{1,3}(?:,\d{3})*(?:\.\d{2})?|\$?\s*\d+(?:\.\d{2})?", raw):
        return None
    return _normalize_price(raw)


def _json_items(raw: str) -> list[Any]:
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return []
    return parsed if isinstance(parsed, list) else [parsed]


def _walk(value: Any, path: tuple[str, ...] = ()) -> list[tuple[tuple[str, ...], Any]]:
    found: list[tuple[tuple[str, ...], Any]] = []
    if isinstance(value, dict):
        for key, child in value.items():
            found.extend(_walk(child, path + (str(key),)))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            found.extend(_walk(child, path + (str(index),)))
    else:
        found.append((path, value))
    return found


def _safe_json_context(obj: Any, limit: int = 500) -> str:
    try:
        raw = json.dumps(obj, sort_keys=True)
    except TypeError:
        raw = str(obj)
    return _safe_context(raw, limit=limit)


def _safe_context(value: str, limit: int = 500) -> str:
    sanitized = re.sub(r"[\w.+-]+@[\w.-]+\.[a-zA-Z]{2,}", "[redacted-email]", html_lib.unescape(value))
    sanitized = re.sub(
        r'(?i)"?(cookie|authorization|bearer|token|password|localstorage|sessionstorage)"?\s*:\s*"[^"]*"',
        r'"\1":"[redacted]"',
        sanitized,
    )
    sanitized = re.sub(r"(?i)(cookie|authorization|bearer|token|password|localstorage|sessionstorage)[^,\\s\"]*", "[redacted]", sanitized)
    return " ".join(sanitized.split())[:limit]


def _attrs(attrs_text: str) -> dict[str, str]:
    attrs: dict[str, str] = {}
    for match in ATTR_RE.finditer(attrs_text):
        attrs[match.group("name").lower()] = html_lib.unescape(match.group("value") or "")
    return attrs


def _deduplicate_signals(signals: list[RawPriceSignal]) -> list[RawPriceSignal]:
    unique: list[RawPriceSignal] = []
    seen: set[tuple[str | None, str, str, str | None]] = set()
    for signal in signals:
        key = (
            signal.normalized_value,
            signal.source_location,
            signal.price_role_hint.value,
            signal.rejection_reason,
        )
        if key in seen:
            continue
        seen.add(key)
        unique.append(signal)
    return unique
