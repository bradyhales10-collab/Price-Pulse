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
from app.competitors.registry import get_competitor
from app.config import DEFAULT_VIEWPORT
from app.models import PartRecord

# Session states that mean the saved sign-in can no longer be used.
UNUSABLE_SESSION_STATES = {"authentication_required", "expired_or_invalid"}
# States that say nothing about the sign-in, so it should not be discarded.
INCONCLUSIVE_SESSION_STATES = {"blocked", "challenge", "navigation_error", "unknown"}


def verify_saved_session(
    competitor_key: str,
    probe_part: PartRecord,
    *,
    headless: bool = False,
    timeout_ms: int = 30000,
    settle_ms: int = 2500,
) -> tuple[bool, str]:
    """Load one real page with the saved sign-in and report whether it worked.

    Returns (ok_to_proceed, reason).

    False is returned only when the site positively says we are signed out:
    an expired session, or prices still hidden behind a sign-in. Being blocked,
    challenged or unreachable says nothing about the sign-in, so those keep the
    saved session rather than sending someone to sign in again over a session
    that is working.
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
            # Same viewport as collection. A site can serve different markup to
            # a headless browser or an unusual window size, so the check has to
            # run under the same conditions as the run it is clearing.
            context = browser.new_context(storage_state=str(state_path), viewport=DEFAULT_VIEWPORT)
            page = context.new_page()
            response = page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            http_status = response.status if response is not None else None
            # Prices are rendered after load, so give the page time to finish
            # before reading it.
            try:
                page.wait_for_load_state("networkidle", timeout=8000)
            except Exception:
                pass
            page.wait_for_timeout(settle_ms)
            html = page.content()
            text = page.locator("body").inner_text(timeout=5000) if page.locator("body").count() else ""
            observation = adapter.parse_product_page(
                html, probe_part, visible_text=text, final_url=page.url, http_status=http_status
            )
            context.close()
            browser.close()
    except (PlaywrightTimeoutError, PlaywrightError) as exc:
        # Not reaching the site says nothing about the sign-in either.
        return (True, f"could not check: the site was unreachable ({type(exc).__name__}). Sign-in kept.")
    except Exception as exc:
        return (True, f"could not check ({type(exc).__name__}). Sign-in kept.")

    session_state = _value(observation.session_status)
    price_state = _value(observation.price_visibility)

    page_state = _value(observation.page_classification)

    # Being blocked says nothing about whether the sign-in is valid, so it must
    # not be reported as a failed sign-in. Treating it as one sent people back
    # to sign in repeatedly over a saved session that was working fine.
    if page_state in {"blocked", "challenge"}:
        return (
            True,
            f"could not check: the site refused the request ({page_state}). "
            f"The saved sign-in was kept, since this says nothing about it.",
        )

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
        f"could not confirm the sign-in (no price read; page looked like "
        f"'{_value(observation.page_classification) or 'nothing'}', session "
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
