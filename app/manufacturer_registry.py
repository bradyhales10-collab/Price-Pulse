from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

MANUFACTURER_COVERAGE_CONFIG = Path(__file__).resolve().parent / "competitors" / "manufacturer_coverage.json"


@dataclass(frozen=True)
class ManufacturerConfig:
    display_name: str
    partzilla_slug: str | None
    aliases: tuple[str, ...]


def _load_config() -> dict[str, Any]:
    return json.loads(MANUFACTURER_COVERAGE_CONFIG.read_text(encoding="utf-8"))


def _write_config(config: dict[str, Any]) -> None:
    MANUFACTURER_COVERAGE_CONFIG.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")


def _clean_alias(value: str) -> str:
    return " ".join(value.strip().replace("_", " ").split()).lower()


def all_manufacturers() -> tuple[ManufacturerConfig, ...]:
    config = _load_config()
    return tuple(
        ManufacturerConfig(
            display_name=str(item["display_name"]),
            partzilla_slug=item.get("partzilla_slug"),
            aliases=tuple(_clean_alias(str(alias)) for alias in item.get("aliases", [])),
        )
        for item in config["manufacturers"]
    )


def manufacturer_coverage_settings() -> dict[str, Any]:
    config = _load_config()
    manufacturers = [str(item["display_name"]) for item in config["manufacturers"]]
    competitors = {
        str(key).strip().lower(): tuple(str(name) for name in value)
        for key, value in dict(config["competitors"]).items()
    }
    return {"manufacturers": manufacturers, "competitors": competitors}


def save_manufacturer_coverage_settings(selected: dict[str, list[str]]) -> None:
    config = _load_config()
    allowed = {str(item["display_name"]) for item in config["manufacturers"]}
    normalized: dict[str, list[str]] = {}
    for competitor_key in dict(config["competitors"]):
        selected_names = selected.get(str(competitor_key).strip().lower(), [])
        normalized[str(competitor_key).strip().lower()] = [name for name in selected_names if name in allowed]
    config["competitors"] = normalized
    _write_config(config)


def normalize_manufacturer(value: str) -> str:
    cleaned = " ".join(value.strip().replace("_", " ").split())
    lowered = cleaned.lower()
    for config in all_manufacturers():
        if lowered == config.display_name.lower() or lowered in config.aliases:
            return config.display_name
    return cleaned


def competitor_manufacturers(competitor_key: str) -> tuple[str, ...]:
    return manufacturer_coverage_settings()["competitors"].get(competitor_key.strip().lower(), ())


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
    for config in all_manufacturers():
        if normalized == config.display_name:
            return config.partzilla_slug
    return None


def supported_partzilla_manufacturer(value: str) -> bool:
    return partzilla_slug_for(value) is not None
