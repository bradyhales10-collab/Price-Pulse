from __future__ import annotations

import html as html_lib
import json
import re
from dataclasses import dataclass, field, replace
from decimal import Decimal
from enum import Enum
from pathlib import Path

from app.parsers.money_parser import parse_money
from app.raw_price_signals import RawPriceRoleHint, RawPriceSignal
from app.schemas.product_observation import (
    ParseConfidence,
    PriceDisplayType,
    PriceValidationStatus,
    ProductObservation,
)

MONEY_RE = re.compile(r"\$[\d,]+(?:\.\d{2})?")
ELEMENT_RE = re.compile(
    r"<(?P<tag>(?!html\b|body\b|script\b|style\b)[a-zA-Z][a-zA-Z0-9-]*)(?P<attrs>[^>]*)>(?P<body>.*?)</(?P=tag)>",
    re.IGNORECASE | re.DOTALL,
)
ATTR_RE = re.compile(r"(?P<name>[a-zA-Z_:][-a-zA-Z0-9_:.]*)(?:\s*=\s*(?P<quote>[\"'])(?P<value>.*?)(?P=quote))?")
STOP_MARKERS = (
    "riders also bought",
    "partzilla picks",
    "recently viewed",
    "related categories",
    "need some help",
    "recommendationfeed",
    "accessories",
    "cart total",
    "shipping",
    "footer",
)
REJECT_CONTEXT_MARKERS = (
    "riders also bought",
    "partzilla picks",
    "recently viewed",
    "related product",
    "recommended",
    "accessory",
    "cart total",
    "shipping",
    "financing",
)
SAFE_ATTR_PREFIXES = ("data-testid", "data-price", "data-product", "data-role")
SAFE_ATTR_NAMES = {"aria-label", "role", "itemprop", "id"}


class PriceCandidateRole(str, Enum):
    MSRP = "msrp"
    REFERENCE_PRICE = "reference_price"
    SELLING_PRICE = "selling_price"
    UNKNOWN = "unknown"
    REJECTED = "rejected"


class PriceCandidateSourceType(str, Enum):
    VISIBLE_DOM = "visible_dom"
    STRUCTURED_PRODUCT_DATA = "structured_product_data"
    FALLBACK = "fallback"


@dataclass(frozen=True)
class PriceCandidate:
    raw_text: str
    normalized_value: str | None
    source_type: PriceCandidateSourceType
    visible_text_context: str
    nearby_label: str | None
    element_tag: str | None
    stable_attributes: dict[str, str]
    relative_location: str
    candidate_role: PriceCandidateRole
    candidate_confidence: ParseConfidence
    rejection_reason: str | None = None
    element_visible_text: str | None = None
    relationship_to_quantity_control: str | None = None
    relationship_to_purchase_action: str | None = None
    relationship_to_product_heading: str | None = None
    in_first_region: bool = True
    found_through_sibling_fallback: bool = False
    source_locations: list[str] = field(default_factory=list)
    corroboration_count: int = 1

    def to_json_dict(self) -> dict[str, object]:
        return {
            "raw_text": self.raw_text,
            "normalized_value": self.normalized_value,
            "source_type": self.source_type.value,
            "visible_text_context": self.visible_text_context,
            "nearby_label": self.nearby_label,
            "element_tag": self.element_tag,
            "stable_attributes": self.stable_attributes,
            "relative_location": self.relative_location,
            "candidate_role": self.candidate_role.value,
            "candidate_confidence": self.candidate_confidence.value,
            "rejection_reason": self.rejection_reason,
            "element_visible_text": self.element_visible_text,
            "relationship_to_quantity_control": self.relationship_to_quantity_control,
            "relationship_to_purchase_action": self.relationship_to_purchase_action,
            "relationship_to_product_heading": self.relationship_to_product_heading,
            "in_first_region": self.in_first_region,
            "found_through_sibling_fallback": self.found_through_sibling_fallback,
            "source_locations": self.source_locations or [self.relative_location],
            "corroboration_count": self.corroboration_count,
        }


@dataclass(frozen=True)
class ManualValidation:
    visually_confirmed_selling_price: str | None
    visually_confirmed_msrp: str | None
    selling_price_input: str
    msrp_input: str
    comparison: str
    field_comparisons: dict[str, dict[str, str | None]]

    def to_json_dict(self) -> dict[str, str | None]:
        return {
            "visually_confirmed_selling_price": self.visually_confirmed_selling_price,
            "visually_confirmed_msrp": self.visually_confirmed_msrp,
            "selling_price_input": self.selling_price_input,
            "msrp_input": self.msrp_input,
            "comparison": self.comparison,
            "selling_price": self.field_comparisons["selling_price"],
            "msrp": self.field_comparisons["msrp"],
        }


@dataclass
class PriceEvidence:
    oem_part_number: str
    product_name: str | None
    timestamp: str
    primary_product_container: dict[str, str]
    primary_product_region: dict[str, object]
    candidate_discovery_methods_attempted: list[str]
    price_candidates: list[PriceCandidate]
    selected_msrp: str | None
    selected_selling_price: str | None
    decision_explanation: list[str]
    price_parse_confidence: ParseConfidence
    selected_reference_price: str | None = None
    selected_savings_percent: int | None = None
    selected_savings_amount: str | None = None
    price_display_type: PriceDisplayType = PriceDisplayType.UNKNOWN
    price_validation_status: PriceValidationStatus = PriceValidationStatus.NOT_MANUALLY_VALIDATED
    manual_validation: ManualValidation | None = None
    parse_warnings: list[str] = field(default_factory=list)

    def to_json_dict(self) -> dict[str, object]:
        return {
            "oem_part_number": self.oem_part_number,
            "product_name": self.product_name,
            "timestamp": self.timestamp,
            "primary_product_container": self.primary_product_container,
            "primary_product_region": self.primary_product_region,
            "candidate_discovery_methods_attempted": self.candidate_discovery_methods_attempted,
            "price_candidates": [candidate.to_json_dict() for candidate in self.price_candidates],
            "selected_msrp": self.selected_msrp,
            "selected_selling_price": self.selected_selling_price,
            "selected_reference_price": self.selected_reference_price,
            "selected_savings_percent": self.selected_savings_percent,
            "selected_savings_amount": self.selected_savings_amount,
            "price_display_type": self.price_display_type.value,
            "decision_explanation": self.decision_explanation,
            "price_parse_confidence": self.price_parse_confidence.value,
            "price_validation_status": self.price_validation_status.value,
            "manual_validation": self.manual_validation.to_json_dict() if self.manual_validation else None,
            "parse_warnings": self.parse_warnings,
        }


def build_price_evidence(
    *,
    html: str,
    visible_text: str,
    observation: ProductObservation,
    raw_price_signals: list[RawPriceSignal] | None = None,
    verified_visible_selling_price_raw: str | None = None,
    verified_visible_reference_price_raw: str | None = None,
    verified_visible_savings_text: str | None = None,
) -> PriceEvidence:
    container_html, container_text, region = _primary_product_region(
        html=html,
        visible_text=visible_text,
        part_number=observation.oem_part_number,
        manufacturer=observation.manufacturer_display or observation.manufacturer,
        product_name=observation.product_name,
    )
    structured_candidates = _structured_candidates(raw_price_signals or [])
    rendered_text_candidates = _discover_visible_discount_candidates(visible_text)
    verified_visible_candidates = _verified_visible_price_candidates(
        selling_price_raw=verified_visible_selling_price_raw,
        reference_price_raw=verified_visible_reference_price_raw,
        savings_text=verified_visible_savings_text,
    )
    first_region_candidates = (
        structured_candidates
        + verified_visible_candidates
        + _discover_candidates(container_html, container_text, in_first_region=True)
        + rendered_text_candidates
    )
    discovery_methods = [
        "product-associated structured offer/list price",
        "visible element inner text",
        "bounded region visible text",
        "safe stable attributes",
    ]
    fallback_used = False
    if not first_region_candidates:
        fallback_used = True
        discovery_methods.append("bounded product-panel sibling fallback")
        first_region_candidates = _discover_candidates(container_html, container_text, in_first_region=False, found_through_sibling_fallback=True)
    candidates = _deduplicate_candidates(_apply_discount_roles(first_region_candidates))
    selected_msrp = _select_candidate(candidates, PriceCandidateRole.MSRP)
    selected_reference = _select_candidate(candidates, PriceCandidateRole.REFERENCE_PRICE) or selected_msrp
    selected_selling = _select_candidate(candidates, PriceCandidateRole.SELLING_PRICE)
    warnings: list[str] = []
    explanation: list[str] = []
    discount_selling = _discount_selling_candidate(candidates)
    if discount_selling:
        selected_selling = discount_selling
        selected_reference = _reference_for_discount(candidates, discount_selling)
        explanation.append("Visible discounted purchase-panel price with SAVE text selected as current selling price")
    elif (
        selected_selling
        and selected_msrp
        and selected_selling.source_type == PriceCandidateSourceType.STRUCTURED_PRODUCT_DATA
        and selected_selling.normalized_value == selected_msrp.normalized_value
        and "sign in to see price" in container_text.lower()
    ):
        selected_selling = None
        warnings.append("structured_offer_matches_gated_msrp")
        explanation.append("Structured offer matched the visible MSRP while the payable price remained sign-in gated")

    selling_values = {
        candidate.normalized_value
        for candidate in candidates
        if candidate.candidate_role == PriceCandidateRole.SELLING_PRICE and candidate.normalized_value is not None
    }
    if len(selling_values) > 1 and not discount_selling:
        warnings.append("conflicting_selling_price_signals")
        selected_selling = None
        explanation.append("Conflicting product-associated selling-price values were preserved without selecting one")
    elif len(selling_values) > 1 and discount_selling:
        warnings.append("secondary_price_specification_differs")

    if selected_msrp:
        explanation.append("MSRP candidate had explicit MSRP/list-price evidence")
    if selected_reference and selected_reference != selected_msrp:
        explanation.append("Reference-price candidate had crossed-out/list/compare-at evidence")
    if selected_selling:
        if selected_selling.source_type == PriceCandidateSourceType.STRUCTURED_PRODUCT_DATA:
            explanation.append("Selling-price candidate came from product-associated structured offer data")
        else:
            explanation.append("Selling-price candidate had product-price or purchase-control evidence")

    active_candidates = [
        candidate
        for candidate in candidates
        if candidate.candidate_role in {PriceCandidateRole.MSRP, PriceCandidateRole.SELLING_PRICE, PriceCandidateRole.UNKNOWN}
    ]
    if len(active_candidates) == 1 and selected_selling is None:
        warnings.append("ambiguous_single_price_candidate")
        explanation.append("Only one price candidate was found without enough evidence to identify it as selling price")

    if selected_reference and selected_selling and selected_reference.normalized_value == selected_selling.normalized_value:
        explanation.append("MSRP and selling price have equal numeric values but separate role evidence")

    confidence = _price_confidence(selected_selling, warnings)
    savings_percent = _savings_percent(candidates)
    savings_amount = _savings_amount(selected_selling, selected_reference)
    price_display_type = PriceDisplayType.DISCOUNTED if selected_selling and selected_reference and selected_selling.normalized_value != selected_reference.normalized_value else PriceDisplayType.REGULAR if selected_selling else PriceDisplayType.UNKNOWN
    return PriceEvidence(
        oem_part_number=observation.oem_part_number,
        product_name=observation.product_name,
        timestamp=observation.checked_at,
        primary_product_container={key: str(value) for key, value in region.items()},
        primary_product_region=region | {"sibling_fallback_used": fallback_used},
        candidate_discovery_methods_attempted=discovery_methods,
        price_candidates=candidates,
        selected_msrp=selected_msrp.normalized_value if selected_msrp else None,
        selected_selling_price=selected_selling.normalized_value if selected_selling else None,
        selected_reference_price=selected_reference.normalized_value if selected_reference else None,
        selected_savings_percent=savings_percent,
        selected_savings_amount=savings_amount,
        price_display_type=price_display_type,
        decision_explanation=explanation,
        price_parse_confidence=confidence,
        parse_warnings=warnings,
    )


def _verified_visible_price_candidates(
    *,
    selling_price_raw: str | None,
    reference_price_raw: str | None,
    savings_text: str | None,
) -> list[PriceCandidate]:
    candidates: list[PriceCandidate] = []
    if selling_price_raw:
        context = " ".join(value for value in (selling_price_raw, savings_text, "add to cart") if value)
        candidates.append(
            _candidate_from_context(
                raw_text=selling_price_raw,
                context=context,
                tag=None,
                attrs={"data-testid": "productPrice"},
                relative_location='visible_dom[data-testid="productPrice"]',
                element_visible_text=context,
                source_type=PriceCandidateSourceType.VISIBLE_DOM,
                in_first_region=True,
                found_through_sibling_fallback=False,
            )
        )
    if reference_price_raw and reference_price_raw != selling_price_raw:
        candidates.append(
            _candidate_from_context(
                raw_text=reference_price_raw,
                context=f"{reference_price_raw} crossed out reference price",
                tag=None,
                attrs={"data-testid": "productPriceValue"},
                relative_location='visible_dom[data-testid="productPriceValue"]',
                element_visible_text=reference_price_raw,
                source_type=PriceCandidateSourceType.VISIBLE_DOM,
                in_first_region=True,
                found_through_sibling_fallback=False,
            )
        )
    return candidates


def apply_price_evidence_to_observation(observation: ProductObservation, evidence: PriceEvidence) -> ProductObservation:
    observation.msrp = Decimal(evidence.selected_msrp) if evidence.selected_msrp else None
    observation.msrp_raw = _raw_for_selected(evidence, PriceCandidateRole.MSRP)
    observation.selling_price = Decimal(evidence.selected_selling_price) if evidence.selected_selling_price else None
    observation.selling_price_raw = _raw_for_selected(evidence, PriceCandidateRole.SELLING_PRICE)
    observation.reference_price = Decimal(evidence.selected_reference_price) if evidence.selected_reference_price else None
    observation.reference_price_raw = _raw_for_selected(evidence, PriceCandidateRole.REFERENCE_PRICE) or _raw_for_selected(evidence, PriceCandidateRole.MSRP)
    observation.savings_percent = evidence.selected_savings_percent
    observation.savings_amount = Decimal(evidence.selected_savings_amount) if evidence.selected_savings_amount else None
    observation.price_display_type = evidence.price_display_type
    observation.price_parse_confidence = evidence.price_parse_confidence
    observation.selling_price_confidence = evidence.price_parse_confidence
    observation.reference_price_confidence = ParseConfidence.HIGH if evidence.selected_reference_price else ParseConfidence.LOW
    observation.price_validation_status = evidence.price_validation_status

    for warning in evidence.parse_warnings:
        if warning not in observation.parse_warnings:
            observation.parse_warnings.append(warning)

    if observation.access_context.value == "authenticated_session" and evidence.selected_msrp is None:
        observation.parse_warnings = [warning for warning in observation.parse_warnings if warning != "msrp_not_found"]

    if evidence.selected_selling_price is None and (
        observation.price_visibility.value == "visible" or "ambiguous_single_price_candidate" in evidence.parse_warnings
    ):
        from app.schemas.product_observation import PriceVisibility

        observation.price_visibility = PriceVisibility.UNKNOWN
    return observation


def _structured_candidates(raw_signals: list[RawPriceSignal]) -> list[PriceCandidate]:
    grouped: dict[tuple[str, PriceCandidateRole], list[RawPriceSignal]] = {}
    for index, signal in enumerate(raw_signals):
        if signal.rejection_reason is not None or signal.normalized_value is None:
            continue
        role = _role_from_raw_signal(signal)
        if role is None:
            continue
        grouped.setdefault((signal.normalized_value, role), []).append(signal)

    candidates: list[PriceCandidate] = []
    for (normalized_value, role), signals in grouped.items():
        first = signals[0]
        locations = [signal.source_location for signal in signals]
        confidence = _structured_candidate_confidence(role, signals)
        candidates.append(
            PriceCandidate(
                raw_text=first.raw_text if str(first.raw_text).startswith("$") else f"${normalized_value}",
                normalized_value=normalized_value,
                source_type=PriceCandidateSourceType.STRUCTURED_PRODUCT_DATA,
                visible_text_context=first.safe_context,
                nearby_label=first.price_role_hint.value,
                element_tag=None,
                stable_attributes={},
                relative_location=locations[0],
                candidate_role=role,
                candidate_confidence=confidence,
                rejection_reason=None,
                element_visible_text=first.safe_context,
                relationship_to_quantity_control=None,
                relationship_to_purchase_action="product-associated structured offer"
                if role == PriceCandidateRole.SELLING_PRICE
                else None,
                relationship_to_product_heading="same structured product object",
                in_first_region=False,
                found_through_sibling_fallback=False,
                source_locations=locations,
                corroboration_count=len(locations),
            )
        )
    return candidates


def _structured_candidate_confidence(role: PriceCandidateRole, signals: list[RawPriceSignal]) -> ParseConfidence:
    locations = [signal.source_location.lower() for signal in signals]
    if role == PriceCandidateRole.SELLING_PRICE and any(location.endswith(".offers.price") for location in locations):
        return ParseConfidence.HIGH
    if len(signals) >= 2:
        return ParseConfidence.HIGH
    return ParseConfidence.MEDIUM


def _role_from_raw_signal(signal: RawPriceSignal) -> PriceCandidateRole | None:
    if signal.price_role_hint == RawPriceRoleHint.MSRP:
        return PriceCandidateRole.MSRP
    if signal.price_role_hint == RawPriceRoleHint.LIST_PRICE:
        return PriceCandidateRole.REFERENCE_PRICE
    if signal.price_role_hint in {RawPriceRoleHint.OFFER_PRICE, RawPriceRoleHint.SELLING_PRICE}:
        return PriceCandidateRole.SELLING_PRICE
    return None


def add_manual_validation(
    evidence: PriceEvidence,
    *,
    selling_price_input: str,
    msrp_input: str,
) -> PriceEvidence:
    selling_answer = _parse_manual_answer(selling_price_input)
    msrp_answer = _parse_manual_answer(msrp_input)
    field_comparisons = {
        "selling_price": _field_comparison(
            manual_answer=selling_answer,
            parsed_value=evidence.selected_selling_price,
            manual_input=selling_price_input,
        ),
        "msrp": _field_comparison(
            manual_answer=msrp_answer,
            parsed_value=evidence.selected_msrp,
            manual_input=msrp_input,
        ),
    }
    comparison = _overall_manual_comparison(field_comparisons)
    evidence.manual_validation = ManualValidation(
        visually_confirmed_selling_price=selling_answer["value"],
        visually_confirmed_msrp=msrp_answer["value"],
        selling_price_input=selling_price_input.strip(),
        msrp_input=msrp_input.strip(),
        comparison=comparison,
        field_comparisons=field_comparisons,
    )
    if comparison == "match":
        evidence.price_validation_status = PriceValidationStatus.PARSER_MATCHES_MANUAL
    elif comparison == "mismatch":
        evidence.price_validation_status = PriceValidationStatus.PARSER_MISMATCH
        evidence.price_parse_confidence = ParseConfidence.LOW
        if "price_parser_manual_mismatch" not in evidence.parse_warnings:
            evidence.parse_warnings.append("price_parser_manual_mismatch")
    else:
        evidence.price_validation_status = PriceValidationStatus.MANUAL_UNCLEAR
    return evidence


def write_price_evidence(path: Path, evidence: PriceEvidence) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(evidence.to_json_dict(), indent=2) + "\n", encoding="utf-8")


def build_price_dom_debug(
    *,
    html: str,
    visible_text: str,
    observation: ProductObservation,
    evidence: PriceEvidence,
) -> dict[str, object]:
    region_html, region_text, region = _primary_product_region(
        html=html,
        visible_text=visible_text,
        part_number=observation.oem_part_number,
        manufacturer=observation.manufacturer_display or observation.manufacturer,
        product_name=observation.product_name,
    )
    money_elements: list[dict[str, object]] = []
    for index, match in enumerate(ELEMENT_RE.finditer(region_html)):
        element_text = _clean_html_text(match.group(0))
        values = [money.group(0) for money in MONEY_RE.finditer(element_text)]
        if not values and "$" not in element_text:
            continue
        money_elements.append(
            {
                "tag": match.group("tag").lower(),
                "safe_attributes": _safe_attrs(match.group("attrs")),
                "visible_text": _sanitize_region_text(element_text, 500),
                "normalized_monetary_values": [
                    format(parsed.value, "f")
                    for raw in values
                    for parsed in [parse_money(raw)]
                    if parsed.value is not None
                ],
                "relative_dom_path": f"primary_product_region.element_{index}",
                "relationship_to_product_heading": _relationship(element_text, observation.product_name or "")
                or _relationship(element_text, observation.oem_part_number),
                "relationship_to_quantity_control": _relationship(element_text, "quantity"),
                "relationship_to_purchase_action": _relationship(element_text, "add to cart") or _relationship(element_text, "cart"),
            }
        )
    return {
        "primary_product_region": region,
        "safe_region_tag": region.get("safe_element_tag"),
        "safe_region_attributes": region.get("safe_stable_attributes"),
        "region_visible_text": _sanitize_region_text(region_text, 2000),
        "detected_product_heading": region.get("detected_product_heading"),
        "detected_quantity_control_text": region.get("detected_quantity_control_text"),
        "detected_purchase_action_text": region.get("detected_purchase_action_text"),
        "money_containing_elements": money_elements,
        "price_candidates": [candidate.to_json_dict() for candidate in evidence.price_candidates],
    }


def write_price_dom_debug(path: Path, debug: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(debug, indent=2) + "\n", encoding="utf-8")


def write_product_purchase_region_text(
    path: Path,
    *,
    html: str,
    visible_text: str,
    observation: ProductObservation,
) -> None:
    _, region_text, _ = _primary_product_region(
        html=html,
        visible_text=visible_text,
        part_number=observation.oem_part_number,
        manufacturer=observation.manufacturer_display or observation.manufacturer,
        product_name=observation.product_name,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_sanitize_region_text(region_text, 2000) + "\n", encoding="utf-8")


def _primary_product_region(
    *,
    html: str,
    visible_text: str,
    part_number: str,
    manufacturer: str | None,
    product_name: str | None,
) -> tuple[str, str, dict[str, object]]:
    semantic = _extract_semantic_product_markup(html)
    if semantic:
        text = _clean_html_text(semantic)
        return semantic, text, _region_metadata("structured product semantic markup", semantic, text, part_number, manufacturer)

    heading = _extract_broad_heading_region(html, part_number, manufacturer)
    if heading:
        text = _clean_html_text(heading)
        return heading, text, _region_metadata("main product heading plus bounded product-detail region", heading, text, part_number, manufacturer)

    if product_name and part_number in visible_text and ("quantity" in visible_text.lower() or "add to cart" in visible_text.lower()):
        section_html = _cut_before_stop_markers(html)
        section_text = _main_product_visible_text(visible_text)
        return section_html, section_text, _region_metadata(
            "product title + manufacturer + quantity/purchase controls",
            section_html,
            section_text,
            part_number,
            manufacturer,
        )

    fallback_html = _cut_before_stop_markers(html)
    fallback_text = _main_product_visible_text(visible_text)
    return fallback_html, fallback_text, _region_metadata("existing stable selectors", fallback_html, fallback_text, part_number, manufacturer)


def _discover_candidates(
    container_html: str,
    container_text: str,
    *,
    in_first_region: bool,
    found_through_sibling_fallback: bool = False,
) -> list[PriceCandidate]:
    candidates: list[PriceCandidate] = []
    for index, match in enumerate(ELEMENT_RE.finditer(container_html)):
        element_html = match.group(0)
        element_text = _clean_html_text(element_html)
        if "$" not in element_text:
            continue
        attrs = _safe_attrs(match.group("attrs"))
        for money_match in MONEY_RE.finditer(element_text):
            context = _sanitize_context(element_text, money_match.start(), money_match.end())
            candidates.append(
                _candidate_from_context(
                    raw_text=money_match.group(0),
                    context=context,
                    tag=match.group("tag").lower(),
                    attrs=attrs,
                    relative_location=f"primary_product_container.element_{index}",
                    element_visible_text=element_text,
                    in_first_region=in_first_region,
                    found_through_sibling_fallback=found_through_sibling_fallback,
                )
            )

    if not candidates and "$" in container_text:
        for index, money_match in enumerate(MONEY_RE.finditer(container_text)):
            context = _sanitize_context(container_text, money_match.start(), money_match.end())
            candidates.append(
                _candidate_from_context(
                    raw_text=money_match.group(0),
                    context=context,
                    tag=None,
                    attrs={},
                    relative_location=f"primary_product_container.fallback_text_{index}",
                    element_visible_text=context,
                    source_type=PriceCandidateSourceType.FALLBACK,
                    in_first_region=in_first_region,
                    found_through_sibling_fallback=found_through_sibling_fallback,
                )
            )
    return candidates


def _discover_visible_discount_candidates(visible_text: str) -> list[PriceCandidate]:
    product_text = _main_product_visible_text(visible_text)
    candidates: list[PriceCandidate] = []
    pattern = re.compile(
        r"(?P<price>\$[\d,]+(?:\.\d{2})?)\s*SAVE\s+(?P<percent>\d{1,3})%",
        re.IGNORECASE,
    )
    for index, match in enumerate(pattern.finditer(product_text)):
        context = _sanitize_context(product_text, match.start(), match.end())
        candidates.append(
            _candidate_from_context(
                raw_text=match.group("price"),
                context=context,
                tag=None,
                attrs={},
                relative_location=f"rendered_product_discount_{index}",
                element_visible_text=match.group(0),
                source_type=PriceCandidateSourceType.FALLBACK,
                in_first_region=True,
                found_through_sibling_fallback=False,
            )
        )
    return candidates


def _candidate_from_context(
    *,
    raw_text: str,
    context: str,
    tag: str | None,
    attrs: dict[str, str],
    relative_location: str,
    element_visible_text: str | None,
    in_first_region: bool,
    found_through_sibling_fallback: bool,
    source_type: PriceCandidateSourceType = PriceCandidateSourceType.VISIBLE_DOM,
) -> PriceCandidate:
    normalized = parse_money(raw_text).value
    normalized_value = format(normalized, "f") if normalized is not None else None
    context = _sanitize_region_text(context, 200)
    nearby_label = _nearby_label(context, attrs)
    role, confidence, rejection_reason = _classify_candidate(context, attrs, nearby_label)
    return PriceCandidate(
        raw_text=raw_text,
        normalized_value=normalized_value,
        source_type=source_type,
        visible_text_context=context,
        nearby_label=nearby_label,
        element_tag=tag,
        stable_attributes=attrs,
        relative_location=relative_location,
        candidate_role=role,
        candidate_confidence=confidence,
        rejection_reason=rejection_reason,
        element_visible_text=_sanitize_region_text(element_visible_text or context, 500),
        relationship_to_quantity_control=_relationship(context, "quantity"),
        relationship_to_purchase_action=_relationship(context, "add to cart") or _relationship(context, "cart"),
        relationship_to_product_heading=_relationship(context, "kawasaki") or _relationship(context, "oem"),
        in_first_region=in_first_region,
        found_through_sibling_fallback=found_through_sibling_fallback,
    )


def _classify_candidate(
    context: str,
    attrs: dict[str, str],
    nearby_label: str | None,
) -> tuple[PriceCandidateRole, ParseConfidence, str | None]:
    lowered = " ".join([context, " ".join(f"{key}={value}" for key, value in attrs.items())]).lower()
    if any(marker in lowered for marker in REJECT_CONTEXT_MARKERS):
        return PriceCandidateRole.REJECTED, ParseConfidence.HIGH, "outside_primary_product_purchase_area"
    if nearby_label and nearby_label.lower() in {"msrp", "list price"}:
        return PriceCandidateRole.MSRP if nearby_label.lower() == "msrp" else PriceCandidateRole.REFERENCE_PRICE, ParseConfidence.HIGH, None
    if "msrp" in lowered:
        return PriceCandidateRole.MSRP, ParseConfidence.HIGH, None
    if "listprice" in lowered or "list-price" in lowered:
        return PriceCandidateRole.REFERENCE_PRICE, ParseConfidence.HIGH, None
    if "save " in lowered and "%" in lowered:
        return PriceCandidateRole.SELLING_PRICE, ParseConfidence.HIGH, None
    if "compare at" in lowered or "was " in lowered or "original" in lowered or "crossed" in lowered:
        return PriceCandidateRole.REFERENCE_PRICE, ParseConfidence.HIGH, None
    if "productpricevalue" in lowered:
        return PriceCandidateRole.REFERENCE_PRICE, ParseConfidence.MEDIUM, None
    if "productprice" in lowered or "product-price" in lowered:
        return PriceCandidateRole.SELLING_PRICE, ParseConfidence.HIGH, None
    purchase_signals = sum(1 for signal in ("quantity", "add to cart", "add-to-cart", "cart") if signal in lowered)
    if purchase_signals >= 2:
        return PriceCandidateRole.SELLING_PRICE, ParseConfidence.HIGH, None
    if purchase_signals == 1:
        return PriceCandidateRole.SELLING_PRICE, ParseConfidence.MEDIUM, None
    return PriceCandidateRole.UNKNOWN, ParseConfidence.LOW, "insufficient_price_role_evidence"


def _nearby_label(context: str, attrs: dict[str, str]) -> str | None:
    lowered = context.lower()
    attr_text = " ".join(attrs.values()).lower()
    if "msrp" in lowered or "msrp" in attr_text:
        return "MSRP"
    if "list price" in lowered or "listprice" in attr_text:
        return "List Price"
    if "sale price" in lowered or "selling price" in lowered:
        return "Selling Price"
    if "save " in lowered and "%" in lowered:
        return "Sale Price"
    if "price" in lowered or "price" in attr_text:
        return "Price"
    return None


def _select_candidate(candidates: list[PriceCandidate], role: PriceCandidateRole) -> PriceCandidate | None:
    role_candidates = [candidate for candidate in candidates if candidate.candidate_role == role]
    if not role_candidates:
        return None
    high = [candidate for candidate in role_candidates if candidate.candidate_confidence == ParseConfidence.HIGH]
    return (high or role_candidates)[0]


def _price_confidence(selected_selling: PriceCandidate | None, warnings: list[str]) -> ParseConfidence:
    if warnings or selected_selling is None:
        return ParseConfidence.LOW
    if selected_selling.candidate_confidence == ParseConfidence.HIGH:
        return ParseConfidence.HIGH
    return ParseConfidence.MEDIUM


def _raw_for_selected(evidence: PriceEvidence, role: PriceCandidateRole) -> str | None:
    if role == PriceCandidateRole.MSRP:
        selected = evidence.selected_msrp
    elif role == PriceCandidateRole.REFERENCE_PRICE:
        selected = evidence.selected_reference_price
    else:
        selected = evidence.selected_selling_price
    if selected is None:
        return None
    for candidate in evidence.price_candidates:
        if candidate.candidate_role == role and candidate.normalized_value == selected:
            return candidate.raw_text
    return None


def _parse_manual_answer(value: str) -> dict[str, str | None]:
    cleaned = value.strip().lower()
    if cleaned == "unclear" or cleaned == "":
        return {"kind": "unclear", "value": None}
    if cleaned == "none":
        return {"kind": "none", "value": None}
    result = parse_money(cleaned if "$" in cleaned else f"${cleaned}", warning_code="manual_price_parse_failed")
    if result.value is None:
        return {"kind": "unclear", "value": None}
    return {"kind": "numeric", "value": format(result.value, "f")}


def _field_comparison(
    *,
    manual_answer: dict[str, str | None],
    parsed_value: str | None,
    manual_input: str,
) -> dict[str, str | None]:
    kind = manual_answer["kind"]
    manual_value = manual_answer["value"]
    if kind == "unclear":
        comparison = "unclear"
    elif kind == "none":
        comparison = "match" if parsed_value is None else "mismatch"
    else:
        comparison = "match" if manual_value == parsed_value else "mismatch"
    return {
        "manual_value": manual_value,
        "manual_input": manual_input.strip(),
        "parsed_value": parsed_value,
        "comparison": comparison,
    }


def _overall_manual_comparison(field_comparisons: dict[str, dict[str, str | None]]) -> str:
    comparisons = [field["comparison"] for field in field_comparisons.values()]
    if "unclear" in comparisons:
        return "unclear"
    if "mismatch" in comparisons:
        return "mismatch"
    return "match"


def _deduplicate_candidates(candidates: list[PriceCandidate]) -> list[PriceCandidate]:
    unique: list[PriceCandidate] = []
    seen: set[tuple[object, ...]] = set()
    for candidate in candidates:
        key = (
            candidate.normalized_value,
            candidate.candidate_role.value,
            candidate.nearby_label,
            candidate.visible_text_context,
            tuple(sorted(candidate.stable_attributes.items())),
        )
        if key in seen:
            continue
        seen.add(key)
        unique.append(candidate)
    return unique


def _apply_discount_roles(candidates: list[PriceCandidate]) -> list[PriceCandidate]:
    has_save_price = any(_savings_percent_from_text(candidate.visible_text_context) is not None for candidate in candidates)
    if not has_save_price:
        return candidates
    adjusted: list[PriceCandidate] = []
    for candidate in candidates:
        if candidate.normalized_value is None:
            adjusted.append(candidate)
            continue
        if _savings_percent_from_text(candidate.visible_text_context) is not None:
            adjusted.append(
                replace(
                    candidate,
                    candidate_role=PriceCandidateRole.SELLING_PRICE,
                    candidate_confidence=ParseConfidence.HIGH,
                    rejection_reason=None,
                    nearby_label=candidate.nearby_label or "Sale Price",
                )
            )
        elif candidate.candidate_role == PriceCandidateRole.SELLING_PRICE or "productpricevalue" in " ".join(candidate.stable_attributes.values()).lower():
            adjusted.append(
                replace(
                    candidate,
                    candidate_role=PriceCandidateRole.REFERENCE_PRICE,
                    candidate_confidence=ParseConfidence.HIGH,
                    rejection_reason=None,
                    nearby_label=candidate.nearby_label or "Reference Price",
                )
            )
        else:
            adjusted.append(candidate)
    return adjusted


def _discount_selling_candidate(candidates: list[PriceCandidate]) -> PriceCandidate | None:
    for candidate in candidates:
        if candidate.candidate_role == PriceCandidateRole.SELLING_PRICE and _savings_percent_from_text(candidate.visible_text_context) is not None:
            return candidate
    return None


def _reference_for_discount(candidates: list[PriceCandidate], selling: PriceCandidate) -> PriceCandidate | None:
    references = [
        candidate
        for candidate in candidates
        if candidate.candidate_role in {PriceCandidateRole.REFERENCE_PRICE, PriceCandidateRole.MSRP}
        and candidate.normalized_value is not None
        and Decimal(candidate.normalized_value) > Decimal(selling.normalized_value or "0")
    ]
    if not references:
        return None
    return sorted(references, key=lambda candidate: Decimal(candidate.normalized_value or "0"))[0]


def _savings_percent(candidates: list[PriceCandidate]) -> int | None:
    for candidate in candidates:
        value = _savings_percent_from_text(candidate.visible_text_context)
        if value is not None:
            return value
    return None


def _savings_percent_from_text(text: str) -> int | None:
    match = re.search(r"\bSAVE\s+(?P<percent>\d{1,3})%", text, re.IGNORECASE)
    return int(match.group("percent")) if match else None


def _savings_amount(selling: PriceCandidate | None, reference: PriceCandidate | None) -> str | None:
    if not selling or not reference or not selling.normalized_value or not reference.normalized_value:
        return None
    amount = Decimal(reference.normalized_value) - Decimal(selling.normalized_value)
    return format(amount, "f") if amount > 0 else None


def _extract_semantic_product_markup(html: str) -> str | None:
    match = re.search(
        r"<(?P<tag>[a-zA-Z][a-zA-Z0-9-]*)[^>]*(?:itemscope|itemtype=[\"'][^\"']*Product[^\"']*[\"'])[^>]*>.*?</(?P=tag)>",
        html,
        flags=re.IGNORECASE | re.DOTALL,
    )
    return _cut_before_stop_markers(match.group(0)) if match else None


def _extract_broad_heading_region(html: str, part_number: str, manufacturer: str | None) -> str | None:
    product_detail = _extract_product_detail_region(html, part_number, manufacturer)
    if product_detail:
        return product_detail
    match = re.search(r"<h1[^>]*>.*?" + re.escape(part_number) + r".*?</h1>", html, re.IGNORECASE | re.DOTALL)
    if not match:
        return None
    return _cut_before_stop_markers(html[match.start() :])


def _extract_product_detail_region(html: str, part_number: str, manufacturer: str | None) -> str | None:
    tag_pattern = re.compile(
        r"<(?P<tag>main|section|article|div)(?P<attrs>[^>]*)>(?P<body>.*?)</(?P=tag)>",
        re.IGNORECASE | re.DOTALL,
    )
    candidates: list[tuple[int, str]] = []
    for match in tag_pattern.finditer(html):
        element_html = match.group(0)
        text = _clean_html_text(element_html).lower()
        attrs = match.group("attrs").lower()
        score = 0
        has_identity = False
        if part_number.lower() in text:
            score += 3
            has_identity = True
        if manufacturer and manufacturer.lower() in text:
            score += 2
            has_identity = True
        if "<h1" in element_html.lower():
            score += 2
            has_identity = True
        if "quantity" in text:
            score += 2
        if "add to cart" in text or "add-to-cart" in attrs:
            score += 2
        if "$" in text:
            score += 1
        if any(marker in attrs for marker in ("product", "detail", "purchase")):
            score += 1
        if score >= 5 and has_identity:
            candidates.append((score, _cut_before_stop_markers(element_html)))
    if not candidates:
        return None
    candidates.sort(key=lambda item: (item[0], -len(item[1])), reverse=True)
    return candidates[0][1]


def _region_metadata(
    method: str,
    region_html: str,
    region_text: str,
    part_number: str,
    manufacturer: str | None,
) -> dict[str, object]:
    tag_match = re.search(r"<(?P<tag>[a-zA-Z][a-zA-Z0-9-]*)(?P<attrs>[^>]*)>", region_html)
    lowered = region_text.lower()
    attrs = _safe_attrs(tag_match.group("attrs")) if tag_match else {}
    heading = _first_match_text(region_html, r"<h1[^>]*>(.*?)</h1>")
    quantity = _snippet_for_keyword(region_text, "quantity")
    purchase = _snippet_for_keyword(region_text, "add to cart") or _snippet_for_keyword(region_text, "cart")
    return {
        "method": method,
        "identifying_evidence": "; ".join(
            value
            for value in (
                "OEM part number found" if part_number.lower() in lowered else "",
                "manufacturer found" if manufacturer and manufacturer.lower() in lowered else "",
                "quantity control found" if "quantity" in lowered else "",
                "purchase action found" if "add to cart" in lowered or "cart" in lowered else "",
                "raw money text found" if "$" in region_text else "",
            )
            if value
        ),
        "safe_element_tag": tag_match.group("tag").lower() if tag_match else None,
        "safe_stable_attributes": attrs,
        "product_heading_inside_region": bool(heading),
        "quantity_control_inside_region": "quantity" in lowered,
        "purchase_action_inside_region": "add to cart" in lowered or "cart" in lowered,
        "detected_product_heading": _sanitize_region_text(heading or "", 200) if heading else None,
        "detected_quantity_control_text": _sanitize_region_text(quantity or "", 200) if quantity else None,
        "detected_purchase_action_text": _sanitize_region_text(purchase or "", 200) if purchase else None,
    }


def _cut_before_stop_markers(value: str) -> str:
    lowered = value.lower()
    indexes = [lowered.find(marker) for marker in STOP_MARKERS if lowered.find(marker) >= 0]
    return value[: min(indexes)] if indexes else value


def _main_product_visible_text(visible_text: str) -> str:
    return _cut_before_stop_markers(visible_text)


def _safe_attrs(attrs_text: str) -> dict[str, str]:
    attrs: dict[str, str] = {}
    for match in ATTR_RE.finditer(attrs_text):
        name = match.group("name").lower()
        value = html_lib.unescape(match.group("value") or "").strip()
        if name in SAFE_ATTR_NAMES or any(name.startswith(prefix) for prefix in SAFE_ATTR_PREFIXES):
            if any(term in name or term in value.lower() for term in ("cookie", "token", "authorization", "session")):
                continue
            attrs[name] = value
    return attrs


def _sanitize_context(text: str, start: int, end: int) -> str:
    text = re.sub(r"[\w.+-]+@[\w.-]+\.[a-zA-Z]{2,}", "[redacted-email]", text)
    left = max(0, start - 180)
    right = min(len(text), end + 90)
    context = " ".join(text[left:right].split())
    return context[:200]


def _sanitize_region_text(text: str, limit: int) -> str:
    sanitized = re.sub(r"[\w.+-]+@[\w.-]+\.[a-zA-Z]{2,}", "[redacted-email]", text)
    sanitized = re.sub(r"(?i)\b(account|sign out|my garage|order history)\b.*?(?=\s{2,}|$)", "", sanitized)
    sanitized = re.sub(r"(?i)(cookie|authorization|bearer|token|password|localstorage|sessionstorage)[^ ]*", "[redacted]", sanitized)
    return _limit_text(sanitized, limit)


def _relationship(context: str, keyword: str) -> str | None:
    if not keyword:
        return None
    lowered = context.lower()
    if keyword not in lowered:
        return None
    return "same_visible_context"


def _first_match_text(html: str, pattern: str) -> str | None:
    match = re.search(pattern, html, re.IGNORECASE | re.DOTALL)
    return _limit_text(_clean_html_text(match.group(1)), 200) if match else None


def _snippet_for_keyword(text: str, keyword: str) -> str | None:
    lowered = text.lower()
    index = lowered.find(keyword)
    if index < 0:
        return None
    return _limit_text(text[max(0, index - 80) : index + 120], 200)


def _limit_text(value: str, limit: int) -> str:
    return " ".join(value.split())[:limit]


def _clean_html_text(value: str) -> str:
    without_scripts = re.sub(r"<script[^>]*>.*?</script>", " ", value, flags=re.IGNORECASE | re.DOTALL)
    without_styles = re.sub(r"<style[^>]*>.*?</style>", " ", without_scripts, flags=re.IGNORECASE | re.DOTALL)
    without_tags = re.sub(r"<[^>]+>", " ", without_styles)
    return " ".join(html_lib.unescape(without_tags).split())
