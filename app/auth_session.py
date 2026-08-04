from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from app.config import PARTZILLA_AUTH_STATE_PATH, PRIVATE_DIR
from app.schemas.product_observation import (
    AccessContext,
    PageClassification,
    PriceVisibility,
    ProductObservation,
    SessionStatus,
)

SENSITIVE_DIAGNOSTIC_TERMS = (
    "cookie",
    "authorization",
    "bearer",
    "token",
    "password",
    "localstorage",
    "sessionstorage",
    "storage_state",
)


class MissingAuthStateError(FileNotFoundError):
    """Raised when authenticated inspection is requested before bootstrap."""


class InvalidAuthStateError(ValueError):
    """Raised when an uploaded browser session is not a Playwright storage state."""


def auth_state_path_for(competitor_key: str) -> Path:
    normalized = _safe_competitor_key(competitor_key)
    if normalized == "partzilla":
        return PARTZILLA_AUTH_STATE_PATH
    return PRIVATE_DIR / f"{normalized}_auth_state.json"


def require_auth_state(path: Path, *, competitor_name: str = "Partzilla") -> Path:
    if not path.exists():
        raise MissingAuthStateError(
            f"Saved {competitor_name} auth state was not found at {path}. "
            f"Run auth_bootstrap.py first, or auth_bootstrap.py --competitor {competitor_name.lower()}, and sign in manually in the opened browser."
        )
    return path


def require_competitor_auth_state(competitor_key: str, *, competitor_name: str | None = None) -> Path:
    return require_auth_state(auth_state_path_for(competitor_key), competitor_name=competitor_name or competitor_key)


def auth_state_exists(competitor_key: str) -> bool:
    return auth_state_path_for(competitor_key).exists()


def auth_state_status(competitor_key: str) -> dict[str, object]:
    path = auth_state_path_for(competitor_key)
    return {
        "exists": path.exists(),
        "updated_at": path.stat().st_mtime if path.exists() else None,
        "path": path,
    }


def save_uploaded_auth_state(competitor_key: str, content: bytes) -> Path:
    parsed = _parse_auth_state(content)
    path = auth_state_path_for(competitor_key)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(parsed, indent=2) + "\n", encoding="utf-8")
    return path


def delete_auth_state(competitor_key: str) -> bool:
    path = auth_state_path_for(competitor_key)
    if not path.exists():
        return False
    path.unlink()
    return True


def saved_session_is_usable(competitor_key: str, *, now: float | None = None) -> tuple[bool, str]:
    """Whether a saved sign-in is still good enough to attempt a run with.

    Checking only that the file exists is not enough. An expired session file
    is still a file, so a run would start, drive a browser, discover the
    session is dead partway through, and only then ask for a sign-in - by
    which point other competitors are already driving browsers of their own
    and the sign-in window cannot be used.

    This reads the cookie expiry times already stored in the file, so it
    needs no network and no browser. Session cookies (expires -1) cannot be
    judged this way and are treated as usable, since the alternative is
    forcing a sign-in before every run.
    """
    path = auth_state_path_for(competitor_key)
    if not path.exists():
        return (False, "no saved sign-in")
    try:
        parsed = _parse_auth_state(path.read_bytes())
    except InvalidAuthStateError as exc:
        return (False, f"saved sign-in is unreadable ({exc})")

    cookies = [cookie for cookie in parsed.get("cookies", []) if isinstance(cookie, dict)]
    if not cookies:
        return (False, "saved sign-in has no cookies")

    moment = time.time() if now is None else now
    dated = []
    for cookie in cookies:
        expires = cookie.get("expires")
        if isinstance(expires, int | float) and expires > 0:
            dated.append(float(expires))
    if not dated:
        # Only session cookies, whose lifetime is not recorded in the file.
        return (True, "saved sign-in present (expiry not recorded)")
    if max(dated) <= moment:
        return (False, "saved sign-in has expired")
    return (True, "saved sign-in looks current")


def _safe_competitor_key(value: str) -> str:
    return "".join(char for char in value.strip().lower() if char.isalnum() or char in ("_", "-")) or "competitor"


def _parse_auth_state(content: bytes) -> dict[str, Any]:
    if len(content) > 2_000_000:
        raise InvalidAuthStateError("Login session file is too large.")
    try:
        parsed = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InvalidAuthStateError("Upload a valid Playwright login session JSON file.") from exc
    if not isinstance(parsed, dict):
        raise InvalidAuthStateError("Login session file must contain a JSON object.")
    cookies = parsed.get("cookies")
    origins = parsed.get("origins", [])
    if not isinstance(cookies, list) or not isinstance(origins, list):
        raise InvalidAuthStateError("Login session file is missing Playwright cookies/origins.")
    # Playwright refuses an entire session file if any one cookie has a value
    # that is not a string, failing with
    # "storageState.cookies[0].value: expected string, got undefined". Validation
    # here checked name and domain but not value, so such a cookie was saved and
    # the sign-in then could not be loaded at all: the browser never started and
    # the competitor checked zero parts.
    #
    # A cookie captured mid-navigation can legitimately arrive without a value,
    # so these are dropped rather than treated as a corrupt file. The rest of the
    # session is still usable.
    usable_cookies = []
    for cookie in cookies:
        if not isinstance(cookie, dict):
            raise InvalidAuthStateError("Login session file has an invalid cookie entry.")
        if not isinstance(cookie.get("name"), str) or not isinstance(cookie.get("domain"), str):
            raise InvalidAuthStateError("Login session file has an invalid cookie entry.")
        if not isinstance(cookie.get("value"), str):
            continue
        usable_cookies.append(cookie)
    if cookies and not usable_cookies:
        raise InvalidAuthStateError("No cookie in the login session file has a usable value.")
    parsed["cookies"] = usable_cookies
    return parsed


def mark_authenticated_context(
    observation: ProductObservation,
    *,
    auth_state_loaded: bool,
) -> ProductObservation:
    observation.access_context = AccessContext.AUTHENTICATED_SESSION
    observation.session_status = determine_session_status(observation, auth_state_loaded=auth_state_loaded)
    return observation


def determine_session_status(
    observation: ProductObservation,
    *,
    auth_state_loaded: bool,
) -> SessionStatus:
    if observation.page_classification == PageClassification.BLOCKED:
        return SessionStatus.BLOCKED
    if observation.page_classification == PageClassification.CHALLENGE:
        return SessionStatus.CHALLENGE
    if observation.page_classification == PageClassification.NAVIGATION_ERROR:
        return SessionStatus.NAVIGATION_ERROR
    if observation.page_classification != PageClassification.NORMAL_PRODUCT:
        return SessionStatus.UNKNOWN

    if observation.price_visibility == PriceVisibility.SIGN_IN_REQUIRED:
        return SessionStatus.EXPIRED_OR_INVALID if auth_state_loaded else SessionStatus.AUTHENTICATION_REQUIRED

    if observation.price_visibility == PriceVisibility.VISIBLE and observation.selling_price is not None:
        return SessionStatus.AUTHENTICATED

    return SessionStatus.UNKNOWN


def write_authenticated_observation(path: Path, observation: ProductObservation) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(observation.to_json_dict(), indent=2) + "\n", encoding="utf-8")


def write_sanitized_authenticated_diagnostics(
    path: Path,
    observation: ProductObservation,
    *,
    exception_message: str | None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        f"checked_at: {observation.checked_at}",
        f"access_context: {observation.access_context.value}",
        f"session_status: {observation.session_status.value}",
        f"manufacturer: {observation.manufacturer}",
        f"oem_part_number: {observation.oem_part_number}",
        f"observed_part_number: {observation.observed_part_number or ''}",
        f"requested_url: {observation.requested_url}",
        f"final_url: {observation.final_url or ''}",
        f"canonical_url: {observation.canonical_url or ''}",
        f"http_status: {observation.http_status if observation.http_status is not None else ''}",
        f"page_title: {observation.page_title or ''}",
        f"page_classification: {observation.page_classification.value}",
        f"price_visibility: {observation.price_visibility.value}",
        f"product_name: {observation.product_name or ''}",
        f"msrp_raw: {observation.msrp_raw or ''}",
        f"selling_price_raw: {observation.selling_price_raw or ''}",
        f"availability_raw: {observation.availability_raw or ''}",
        f"price_parse_confidence: {observation.price_parse_confidence.value}",
        f"price_validation_status: {observation.price_validation_status.value}",
        f"parse_confidence: {observation.parse_confidence.value}",
        f"parse_warnings: {', '.join(observation.parse_warnings) or 'None'}",
        f"exception_message: {_sanitize_exception(exception_message)}",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def serialized_observation_has_sensitive_fields(observation: ProductObservation) -> bool:
    serialized = json.dumps(observation.to_json_dict(), sort_keys=True).lower()
    return any(term in serialized for term in SENSITIVE_DIAGNOSTIC_TERMS)


def _sanitize_exception(exception_message: str | None) -> str:
    if not exception_message:
        return ""
    lowered = exception_message.lower()
    if any(term in lowered for term in SENSITIVE_DIAGNOSTIC_TERMS):
        return "[redacted]"
    return exception_message
