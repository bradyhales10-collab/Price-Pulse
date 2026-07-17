from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit

from app.manufacturer_registry import competitor_manufacturers, partzilla_slug_for


class UnsupportedManufacturerError(ValueError):
    """Raised when URL construction is requested for an unsupported maker."""


def manufacturer_slug(manufacturer: str) -> str:
    slug = partzilla_slug_for(manufacturer)
    if slug is None:
        supported = ", ".join(competitor_manufacturers("partzilla"))
        raise UnsupportedManufacturerError(
            f"Unsupported manufacturer '{manufacturer}'. Supported: {supported}"
        )
    return slug


def build_partzilla_product_url(manufacturer: str, part_number: str) -> str:
    if not part_number.strip():
        raise ValueError("Part number cannot be blank.")
    slug = manufacturer_slug(manufacturer)
    return f"https://www.partzilla.com/product/{slug}/{part_number.strip()}"


def canonicalize_partzilla_product_url(url: str | None) -> str | None:
    if url is None or not url.strip():
        return None

    parsed = urlsplit(url.strip())
    if parsed.netloc.lower() != "www.partzilla.com":
        return url.strip()
    if not parsed.path.startswith("/product/"):
        return url.strip()

    return urlunsplit((parsed.scheme or "https", parsed.netloc, parsed.path, "", ""))
