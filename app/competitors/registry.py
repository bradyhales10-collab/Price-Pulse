from __future__ import annotations

from app.competitors.base import CompetitorAdapter
from app.competitors.chaparral import ChaparralAdapter
from app.competitors.motosport import MotoSportAdapter
from app.competitors.partzilla import PartzillaAdapter
from app.competitors.revzilla import RevzillaAdapter

_REGISTRY: dict[str, CompetitorAdapter] = {
    "partzilla": PartzillaAdapter(),
    "motosport": MotoSportAdapter(),
    "chaparral": ChaparralAdapter(),
    "revzilla": RevzillaAdapter(),
}


def short_display_name(adapter: CompetitorAdapter) -> str:
    """Short label for table cells and column headings."""
    return getattr(adapter, "short_name", None) or adapter.display_name


def login_page_url(adapter: CompetitorAdapter) -> str:
    """Where a person should go to sign in.

    A product page was used for this previously, which for a signed-out
    visitor redirects and loads tracking pages, making the window unusable.
    Falls back to the site root, which is always a safe place to sign in from.
    """
    declared = getattr(adapter, "login_page_url", None)
    if declared:
        return str(declared)
    base = str(getattr(adapter, "lookup_url", "") or "")
    if base.startswith("http"):
        parts = base.split("/")
        if len(parts) >= 3:
            return f"{parts[0]}//{parts[2]}/"
    return base


def competitor_short_names() -> dict[str, str]:
    """Full display name -> short label, for every registered competitor."""
    return {adapter.display_name: short_display_name(adapter) for adapter in list_competitors()}


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
