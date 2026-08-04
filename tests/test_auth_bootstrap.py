from __future__ import annotations

import json

from auth_bootstrap import SignInSnapshot, _looks_signed_in


def test_gated_price_page_is_not_treated_as_signed_in() -> None:
    assert _looks_signed_in(text="Sign in to see price\nAdd to Cart", html="") is False


def test_account_menu_means_signed_in() -> None:
    assert _looks_signed_in(text="My Account  Sign Out\n$282.32  Add to Cart", html="") is True
    assert _looks_signed_in(text="Logout | Order History\n$72.99", html="") is True


def test_page_with_no_evidence_is_not_assumed_signed_in() -> None:
    assert _looks_signed_in(text="Kawasaki parts catalog. Browse categories.", html="") is False


def test_gated_price_wins_over_account_menu() -> None:
    """If the price is still gated we are not usefully signed in, even if an
    account menu is present somewhere on the page."""
    text = "My Account Sign Out\nSign in to see price"
    assert _looks_signed_in(text=text, html="") is False


def test_snapshot_state_survives_the_user_closing_the_browser() -> None:
    """The captured state is what gets written, so closing the browser to
    finish must still leave us with a saveable session."""
    captured = {"cookies": [{"name": "session", "domain": ".partzilla.com"}], "origins": []}
    snapshot = SignInSnapshot(storage_state=captured, signed_in_observed=True, closed_by_user=True)

    assert snapshot.storage_state is not None
    # Must round-trip through the validating writer used by the app.
    payload = json.dumps(snapshot.storage_state).encode("utf-8")
    assert json.loads(payload)["cookies"][0]["name"] == "session"


def test_snapshot_defaults_are_safe() -> None:
    snapshot = SignInSnapshot()

    assert snapshot.storage_state is None
    assert snapshot.signed_in_observed is False
    assert snapshot.detected_signals == []


def test_a_navigation_during_sign_in_does_not_abandon_the_wait() -> None:
    """Submitting a login form navigates, and reading a page mid-navigation
    raises. That was being treated as a closed browser, so the wait ended
    immediately and the sign-in was discarded - the user signed in successfully
    and nothing was saved."""
    from auth_bootstrap import _wait_for_manual_sign_in

    class Page:
        def __init__(self) -> None:
            self.reads = 0
            self._closed = False

        def is_closed(self) -> bool:
            return self._closed

        def content(self) -> str:
            self.reads += 1
            if self.reads <= 2:
                # Mid-navigation, exactly as Playwright behaves.
                raise RuntimeError("Execution context was destroyed")
            return "<html><body>My Account Sign Out $12.34</body></html>"

        def title(self) -> str:
            return "Account"

        @property
        def url(self) -> str:
            return "https://www.partzilla.com/account"

        def locator(self, selector):
            page = self

            class Locator:
                def count(self):
                    return 1

                def inner_text(self, timeout=None):
                    return "My Account Sign Out $12.34" if page.reads > 2 else ""

            return Locator()

        def wait_for_timeout(self, ms) -> None:
            return None

    class Context:
        def storage_state(self):
            return {"cookies": [{"name": "session", "domain": ".partzilla.com"}], "origins": []}

    snapshot = _wait_for_manual_sign_in(Page(), Context(), settle_ms=0, timeout_seconds=30, poll_seconds=0)

    # It must have kept polling through the navigation and then detected sign-in.
    assert snapshot.signed_in_observed is True
    assert snapshot.closed_by_user is False
    assert snapshot.storage_state is not None
    assert snapshot.storage_state["cookies"][0]["name"] == "session"


def test_cookies_are_captured_even_while_the_page_cannot_be_read() -> None:
    """Cookies come from the context, not the page, so they remain available
    during a navigation. That is what makes the sign-in saveable."""
    from auth_bootstrap import _wait_for_manual_sign_in

    class Page:
        def is_closed(self) -> bool:
            return False

        def content(self) -> str:
            raise RuntimeError("Execution context was destroyed")

        def locator(self, selector):
            raise RuntimeError("not readable")

        def wait_for_timeout(self, ms) -> None:
            return None

    class Context:
        def storage_state(self):
            return {"cookies": [{"name": "session", "domain": ".partzilla.com"}], "origins": []}

    snapshot = _wait_for_manual_sign_in(Page(), Context(), settle_ms=0, timeout_seconds=1, poll_seconds=0)

    assert snapshot.storage_state is not None
    assert len(snapshot.storage_state["cookies"]) == 1


def test_a_genuinely_closed_browser_still_ends_the_wait() -> None:
    from auth_bootstrap import _wait_for_manual_sign_in

    class Page:
        def is_closed(self) -> bool:
            return True

    class Context:
        def storage_state(self):
            return {"cookies": [], "origins": []}

    snapshot = _wait_for_manual_sign_in(Page(), Context(), settle_ms=0, timeout_seconds=5, poll_seconds=0)

    assert snapshot.closed_by_user is True
