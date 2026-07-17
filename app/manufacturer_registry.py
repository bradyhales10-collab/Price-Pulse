from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


MANUFACTURER_COVERAGE_CONFIG = Path(__file__).resolve().parent / "competitors" / "manufacturer_coverage.json"


@dataclass(frozen=True)
class ManufacturerConfig:
    display_name: str
    partzilla_slug: str | None
    aliases: tuple[str, ...]


def _load_config() -> dict[str, object]:
    return json.loads(MANUFACTURER_COVERAGE_CONFIG.read_text(encoding="utf-8"))


def _clean_alias(value: str) -> str:
    return " ".join(value.strip().replace("_", " ").split()).lower()


_CONFIG = _load_config()
MANUFACTURERS = tuple(
    ManufacturerConfig(
        display_name=str(item["display_name"]),
        partzilla_slug=item.get("partzilla_slug"),
        aliases=tuple(_clean_alias(str(alias)) for alias in item.get("aliases", [])),
    )
    for item in _CONFIG["manufacturers"]
)
COMPETITOR_MANUFACTURER_COVERAGE = {
    str(key).strip().lower(): tuple(str(name) for name in value)
    for key, value in dict(_CONFIG["competitors"]).items()
}


def normalize_manufacturer(value: str) -> str:
    cleaned = " ".join(value.strip().replace("_", " ").split())
    lowered = cleaned.lower()
    for config in MANUFACTURERS:
        if lowered == config.display_name.lower() or lowered in config.aliases:
            return config.display_name
    return cleaned


def competitor_manufacturers(competitor_key: str) -> tuple[str, ...]:
    return COMPETITOR_MANUFACTURER_COVERAGE.get(competitor_key.strip().lower(), ())


def competitor_supports_manufacturer(competitor_key: str, manufacturer: str) -> bool:
    normalized = normalize_manufacturer(manufacturer)
    return normalized in competitor_manufacturers(competitor_key)


def manufacturer_support_metadata(competitor_key: str, manufacturer: str, oem_part_number: str) -> dict[str, object]:
    competitor = competitor_key.strip().lower()
    normalized = normalize_manufacturer(manufacturer)
    supported = competitor_supports_manufacturer(competitor, manufacturer)
    reason = "" if supported else f"{competitor} does not carry OEM manufacturer {normalized}."
    return {
        "manufacturer": manufacturer,
        "normalized_manufacturer": normalized,
        "competitor": competitor,
        "oem_part_number": oem_part_number,
        "manufacturer_supported": supported,
        "lookup_status": "ready" if supported else "manufacturer_not_carried",
        "status_reason": reason,
    }


def partzilla_slug_for(value: str) -> str | None:
    normalized = normalize_manufacturer(value)
    if not competitor_supports_manufacturer("partzilla", normalized):
        return None
    for config in MANUFACTURERS:
        if normalized == config.display_name:
            return config.partzilla_slug
    return None


def supported_partzilla_manufacturer(value: str) -> bool:
    return partzilla_slug_for(value) is not None
