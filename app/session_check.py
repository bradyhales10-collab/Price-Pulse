"""Verify a saved sign-in against the live site before collecting anything.

Checking the session file offline is not enough. Cookies can carry a future
expiry date while the site has already invalidated the session server-side,
which reads as valid on disk and fails the moment a real page loads. The only
reliable answer comes from loading one real page with the saved session and
asking the competitor's own parser whether we are still signed in.

One page load per competitor that needs a sign-in, before any collection
browsers open.
"""

from __future__ import annotations

from pathlib import Path

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

from app.auth_session import auth_state_path_for
from app.browser_hygiene import block_tracking_requests, close_popup_pages
from app.competitors.registry import get_competitor
from app.models import PartRecord

# Session states that mean the saved sign-in can no longer be used.
UNUSABLE_SESSION_STATES = {"authentication_required", "expired_or_invalid"}
# States that say nothing about the sign-in, so it should not be discarded.
INCONCLUSIVE_SESSION_STATES = {"blocked", "challenge", "navigation_error", "unknown"}


def verify_saved_session(
    competitor_key: str,
    probe_part: PartRecord,
    *,
    headless: bool = True,
    timeout_ms: int = 30000,
    settle_ms: int = 1200,
) -> tuple[bool, str]:
    """Load one real page with the saved sign-in and report whether it worked.

    Returns (confirmed, reason). Only a positively confirmed sign-in returns
    True. Anything else, including a site that could not be reached, returns
    False, because starting a run on an unconfirmed sign-in is what produced
    the failure this exists to prevent.
    """
    adapter = get_competitor(competitor_key)
    if not adapter.requires_login:
        return (True, "no sign-in needed")

    state_path = auth_state_path_for(competitor_key)
    if not state_path.exists():
        return (False, "no saved sign-in")

    try:
        url = adapter.build_product_url(probe_part)
    except Exception as exc:
        return (True, f"could not build a check URL ({exc})")

    browser = None
    context = None
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=headless)
            context = browser.new_context(storage_state=str(state_path))
            block_tracking_requests(context)
            page = context.new_page()
            close_popup_pages(context, page)
            response = page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            http_status = response.status if response is not None else None
            page.wait_for_timeout(settle_ms)
            html = page.content()
            text = page.locator("body").inner_text(timeout=5000) if page.locator("body").count() else ""
            observation = adapter.parse_product_page(
                html, probe_part, visible_text=text, final_url=page.url, http_status=http_status
            )
            context.close()
            browser.close()
    except (PlaywrightTimeoutError, PlaywrightError) as exc:
        return (False, f"could not reach the site to check the sign-in ({type(exc).__name__})")
    except Exception as exc:
        return (False, f"sign-in check could not run ({type(exc).__name__}: {exc})")

    session_state = _value(observation.session_status)
    price_state = _value(observation.price_visibility)

    if session_state in UNUSABLE_SESSION_STATES:
        return (False, f"signed out on the live site ({session_state})")
    if price_state == "sign_in_required":
        return (False, "the live site is hiding prices behind a sign-in")

    # A visible price is the real proof. These competitors hide prices from
    # anyone not signed in, so seeing one means the saved session worked. This
    # is checked before session_status because an adapter can pass through a
    # status of "unknown" while still having read a price perfectly well, which
    # made a working sign-in look like a failure.
    if observation.selling_price is not None:
        return (True, f"confirmed signed in: read a price of {observation.selling_price}")
    if session_state == "authenticated":
        return (True, "confirmed signed in on the live site")

    # No price and no confirmation. Not treated as good enough, because doing so
    # is what let a dead session start a full run.
    return (
        False,
        f"could not confirm the sign-in (no price read, session reported "
        f"'{session_state or 'nothing'}', prices '{price_state or 'nothing'}')",
    )


def first_probe_part(input_csv: Path, competitor_key: str) -> PartRecord | None:
    """First part in the input file that this competitor actually carries.

    Using a real part from the run being started means the check exercises the
    same page type the run will, rather than a hardcoded example that may have
    been discontinued.
    """
    from app.input_loader import load_parts_csv
    from app.manufacturer_registry import competitor_supports_manufacturer

    try:
        records = load_parts_csv(input_csv).records
    except Exception:
        return None
    for record in records:
        try:
            if competitor_supports_manufacturer(competitor_key, record.manufacturer):
                return record
        except Exception:
            continue
    return None


def _value(field: object) -> str:
    return str(getattr(field, "value", field) or "")
