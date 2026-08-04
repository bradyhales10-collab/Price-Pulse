"""Persistent browser profiles for competitors that require sign-in."""

from __future__ import annotations

import json
from pathlib import Path

from app.auth_session import auth_state_path_for
from app.config import DEFAULT_VIEWPORT, PRIVATE_DIR


def browser_profile_dir(competitor_key: str) -> Path:
    safe_key = "".join(
        character
        for character in competitor_key.strip().lower()
        if character.isalnum() or character in ("_", "-")
    ) or "competitor"
    return PRIVATE_DIR / "browser_profiles" / safe_key


def launch_persistent_competitor_context(
    playwright,
    competitor_key: str,
    *,
    headless: bool,
    slow_mo: int = 0,
    args: list[str] | None = None,
):
    """Open a durable profile and import a newer JSON session backup."""
    profile_dir = browser_profile_dir(competitor_key)
    profile_dir.mkdir(parents=True, exist_ok=True)
    context = playwright.chromium.launch_persistent_context(
        user_data_dir=str(profile_dir),
        headless=headless,
        slow_mo=slow_mo,
        args=args or [],
        viewport=DEFAULT_VIEWPORT,
    )
    _import_newer_storage_backup(context, competitor_key, profile_dir)
    return context


def save_persistent_session(context, competitor_key: str) -> Path:
    """Export the live Chromium profile as a portable session backup."""
    path = auth_state_path_for(competitor_key)
    path.parent.mkdir(parents=True, exist_ok=True)
    context.storage_state(path=str(path))
    marker = browser_profile_dir(competitor_key) / ".storage_state_imported"
    marker.write_text(str(path.stat().st_mtime_ns), encoding="ascii")
    return path


def primary_page(context):
    """Reuse Chromium's initial blank page instead of opening an extra tab."""
    for page in context.pages:
        if not page.is_closed():
            return page
    return context.new_page()


def _import_newer_storage_backup(context, competitor_key: str, profile_dir: Path) -> None:
    state_path = auth_state_path_for(competitor_key)
    if not state_path.exists():
        return
    marker = profile_dir / ".storage_state_imported"
    imported_mtime = -1
    try:
        imported_mtime = int(marker.read_text(encoding="ascii").strip())
    except (OSError, ValueError):
        pass
    current_mtime = state_path.stat().st_mtime_ns
    if imported_mtime >= current_mtime:
        return
    try:
        payload = json.loads(state_path.read_text(encoding="utf-8"))
        cookies = payload.get("cookies") if isinstance(payload, dict) else None
        if isinstance(cookies, list) and cookies:
            context.add_cookies(cookies)
        marker.write_text(str(current_mtime), encoding="ascii")
    except Exception:
        # Chromium's existing profile can still be used if the backup is bad.
        return
