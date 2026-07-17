from __future__ import annotations

from app.competitors.base import CompetitorAdapter
from app.competitors.chaparral import ChaparralAdapter
from app.competitors.motosport import MotoSportAdapter
from app.competitors.partzilla import PartzillaAdapter


_REGISTRY: dict[str, CompetitorAdapter] = {
    "partzilla": PartzillaAdapter(),
    "motosport": MotoSportAdapter(),
    "chaparral": ChaparralAdapter(),
}


def list_competitors() -> list[CompetitorAdapter]:
    return list(_REGISTRY.values())


def get_competitor(key: str) -> CompetitorAdapter:
    normalized = key.strip().lower()
    if normalized not in _REGISTRY:
        raise ValueError(f"Unknown competitor: {key}")
    return _REGISTRY[normalized]


def select_competitors(keys: list[str], *, allow_experimental: bool = False, probe_mode: bool = False) -> list[CompetitorAdapter]:
    selected = [get_competitor(key) for key in (keys or ["partzilla"])]
    experimental = [adapter.display_name for adapter in selected if adapter.capabilities.status == "experimental_probe"]
    if experimental and not (allow_experimental or probe_mode):
        names = ", ".join(experimental)
        raise ValueError(f"{names} is experimental. Use probe_competitor.py or pass --allow-experimental-competitors.")
    return selected
