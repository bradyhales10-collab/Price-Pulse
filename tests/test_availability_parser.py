from __future__ import annotations

from app.parsers.availability_parser import parse_availability
from app.schemas.product_observation import AvailabilityStatus


def test_in_stock() -> None:
    result = parse_availability("In Stock")

    assert result.status == AvailabilityStatus.IN_STOCK


def test_ships_in_estimate() -> None:
    result = parse_availability("Ships in 3 to 4 days")

    assert result.status == AvailabilityStatus.SHIPS_IN
    assert result.shipping_estimate == "3 to 4 days"


def test_out_of_stock() -> None:
    result = parse_availability("Out of Stock")

    assert result.status == AvailabilityStatus.OUT_OF_STOCK


def test_unknown_availability_wording() -> None:
    result = parse_availability("Call for details")

    assert result.status == AvailabilityStatus.UNKNOWN
