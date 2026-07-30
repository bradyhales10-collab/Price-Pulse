from __future__ import annotations

import re
from dataclasses import dataclass

from app.schemas.product_observation import AvailabilityStatus

SHIPS_IN_RE = re.compile(r"ships?\s+in\s+(?P<estimate>[^.;,\n\r]+)", re.IGNORECASE)


@dataclass(frozen=True)
class AvailabilityParseResult:
    raw: str | None
    status: AvailabilityStatus
    shipping_estimate: str | None


def parse_availability(raw: str | None) -> AvailabilityParseResult:
    if raw is None or not raw.strip():
        return AvailabilityParseResult(raw=None, status=AvailabilityStatus.UNKNOWN, shipping_estimate=None)

    cleaned = " ".join(raw.split())
    lowered = cleaned.lower()

    ships_match = SHIPS_IN_RE.search(cleaned)
    if ships_match:
        estimate = ships_match.group("estimate").strip()
        return AvailabilityParseResult(
            raw=cleaned,
            status=AvailabilityStatus.SHIPS_IN,
            shipping_estimate=estimate,
        )

    if "in stock" in lowered:
        return AvailabilityParseResult(raw=cleaned, status=AvailabilityStatus.IN_STOCK, shipping_estimate=None)

    if "out of stock" in lowered:
        return AvailabilityParseResult(raw=cleaned, status=AvailabilityStatus.OUT_OF_STOCK, shipping_estimate=None)

    if "unavailable" in lowered:
        return AvailabilityParseResult(raw=cleaned, status=AvailabilityStatus.UNAVAILABLE, shipping_estimate=None)

    return AvailabilityParseResult(raw=cleaned, status=AvailabilityStatus.UNKNOWN, shipping_estimate=None)
