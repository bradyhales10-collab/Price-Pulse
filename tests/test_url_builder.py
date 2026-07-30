from __future__ import annotations

import pytest

from app.url_builder import (
    UnsupportedManufacturerError,
    build_partzilla_product_url,
    canonicalize_partzilla_product_url,
)


def test_builds_kawasaki_url_without_encoding_hyphens() -> None:
    assert (
        build_partzilla_product_url("Kawasaki", "41080-1514")
        == "https://www.partzilla.com/product/kawasaki/41080-1514"
    )


def test_builds_kawasaki_url_for_complex_part_number() -> None:
    assert (
        build_partzilla_product_url(" Kawasaki ", "KMT4X7-3-4")
        == "https://www.partzilla.com/product/kawasaki/KMT4X7-3-4"
    )


def test_rejects_unsupported_manufacturer() -> None:
    with pytest.raises(UnsupportedManufacturerError):
        build_partzilla_product_url("Ducati", "12345")


def test_builds_honda_url_for_multi_oem_readiness() -> None:
    assert build_partzilla_product_url("Honda", "12345") == "https://www.partzilla.com/product/honda/12345"


def test_builds_partzilla_only_manufacturer_urls() -> None:
    assert build_partzilla_product_url("ArcticCat", "A-1") == "https://www.partzilla.com/product/arctic-cat/A-1"
    assert build_partzilla_product_url("Sea Doo", "S-1") == "https://www.partzilla.com/product/sea-doo/S-1"
    assert build_partzilla_product_url("Ski Doo", "SK-1") == "https://www.partzilla.com/product/ski-doo/SK-1"


def test_rejects_motosport_only_manufacturer_for_partzilla_url() -> None:
    with pytest.raises(UnsupportedManufacturerError):
        build_partzilla_product_url("KTM", "KTM-1")


def test_canonical_url_removes_query_parameters() -> None:
    assert (
        canonicalize_partzilla_product_url("https://www.partzilla.com/product/kawasaki/41080-1514?titan_sku=41080-1514")
        == "https://www.partzilla.com/product/kawasaki/41080-1514"
    )


def test_canonical_url_removes_fragment() -> None:
    assert (
        canonicalize_partzilla_product_url("https://www.partzilla.com/product/kawasaki/41080-1514#reviews")
        == "https://www.partzilla.com/product/kawasaki/41080-1514"
    )


def test_canonical_url_removes_query_and_fragment() -> None:
    assert (
        canonicalize_partzilla_product_url("https://www.partzilla.com/product/kawasaki/41080-1514?titan_sku=41080-1514#reviews")
        == "https://www.partzilla.com/product/kawasaki/41080-1514"
    )


def test_clean_canonical_url_remains_unchanged() -> None:
    assert (
        canonicalize_partzilla_product_url("https://www.partzilla.com/product/kawasaki/41080-1514")
        == "https://www.partzilla.com/product/kawasaki/41080-1514"
    )
