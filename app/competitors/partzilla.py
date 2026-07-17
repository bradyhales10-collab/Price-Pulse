from __future__ import annotations

from app.competitors.base import CompetitorCapabilities, CompetitorObservation
from app.manufacturer_registry import competitor_manufacturers
from app.models import PartRecord
from app.parsers.partzilla_product_parser import build_parse_input_from_probe, parse_partzilla_product_page
from app.url_builder import build_partzilla_product_url


class PartzillaAdapter:
    competitor_key = "partzilla"
    display_name = "Partzilla"
    supported_manufacturers = competitor_manufacturers("partzilla")
    capabilities = CompetitorCapabilities(
        requires_login=True,
        supports_public_price=False,
        supports_direct_part_url=True,
        status="active",
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
        return build_partzilla_product_url(product.manufacturer, product.oem_part_number)

    def parse_product_page(self, html: str, product: PartRecord, *, visible_text: str = "", final_url: str | None = None, http_status: int | None = None) -> CompetitorObservation:
        observation = parse_partzilla_product_page(
            build_parse_input_from_probe(
                record=product,
                html=html,
                visible_text=visible_text,
                final_url=final_url,
                http_status=http_status,
                page_title=None,
                navigation_succeeded=http_status is None or http_status < 500,
                exception_message=None,
                detected_signals=[],
            )
        )
        return CompetitorObservation(
            competitor_key=self.competitor_key,
            manufacturer=product.manufacturer,
            oem_part_number=product.oem_part_number,
            observed_part_number=observation.observed_part_number,
            product_name=observation.product_name,
            canonical_url=observation.canonical_url,
            http_status=observation.http_status,
            page_classification=observation.page_classification.value,
            session_status=observation.session_status.value,
            price_visibility=observation.price_visibility.value,
            selling_price=observation.selling_price,
            reference_price=observation.reference_price,
            savings_percent=observation.savings_percent,
            savings_amount=observation.savings_amount,
            price_display_type=observation.price_display_type.value,
            selling_price_confidence=observation.selling_price_confidence.value,
            reference_price_confidence=observation.reference_price_confidence.value,
            availability_raw=observation.availability_raw,
            availability_status=observation.availability_status.value,
            supersession_detected=observation.supersession_detected,
            superseded_by_raw=observation.superseded_by_raw,
            warnings=observation.parse_warnings,
            parser_version="partzilla-v1",
            parse_confidence=observation.parse_confidence.value,
        )

    def normalize_availability(self, raw: str | None) -> str:
        from app.parsers.availability_parser import parse_availability

        return parse_availability(raw).status.value

    def normalize_supersession(self, raw: str | None) -> tuple[bool, str | None]:
        return (bool(raw), raw)
