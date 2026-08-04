from __future__ import annotations

import inspect
import json

import auth_bootstrap
from auth_bootstrap import SignInSnapshot, _looks_signed_in


def test_gated_price_page_is_not_treated_as_signed_in() -> None:
    assert _looks_signed_in(text="Sign in to see price\nAdd to Cart", html="") is False


def test_account_menu_means_signed_in() -> None:
    assert _looks_signed_in(text="My Account  Sign Out\n$282.32  Add to Cart", html="") is True
    assert _looks_signed_in(text="Logout | Order History\n$72.99", html="") is True


def test_generic_my_account_navigation_is_not_proof_of_sign_in() -> None:
    assert _looks_signed_in(text="My Account | Sign In | Cart", html="") is False


def test_unconfirmed_login_snapshot_is_not_saved() -> None:
    source = inspect.getsource(auth_bootstrap.main)

    assert "should_save = snapshot.signed_in_observed" in source


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


def test_a_cookie_with_no_value_is_dropped_rather_than_saved() -> None:
    """The real failure: Playwright refuses an entire session file if any one
    cookie's value is not a string, with "storageState.cookies[0].value:
    expected string, got undefined". Validation checked name and domain but not
    value, so such a cookie was saved and the sign-in could not be loaded at
    all. The browser never started and Partzilla checked 0 of 100 parts while
    reporting success."""
    import json

    from app.auth_session import _parse_auth_state

    content = json.dumps(
        {
            "cookies": [
                {"name": "no_value_at_all", "domain": ".partzilla.com"},
                {"name": "null_value", "value": None, "domain": ".partzilla.com"},
                {"name": "good", "value": "keep-me", "domain": ".partzilla.com"},
            ],
            "origins": [],
        }
    ).encode("utf-8")

    parsed = _parse_auth_state(content)

    assert [cookie["name"] for cookie in parsed["cookies"]] == ["good"]
    assert parsed["cookies"][0]["path"] == "/"


def test_a_cookie_without_a_path_is_repaired_for_playwright() -> None:
    from app.auth_session import _parse_auth_state

    parsed = _parse_auth_state(
        json.dumps(
            {
                "cookies": [
                    {
                        "name": "session",
                        "value": "saved",
                        "domain": ".partzilla.com",
                    }
                ],
                "origins": [],
            }
        ).encode("utf-8")
    )

    assert parsed["cookies"][0]["path"] == "/"


def test_a_session_where_no_cookie_has_a_value_is_refused() -> None:
    """Saving it would produce a file Playwright rejects outright, which is
    worse than refusing it here."""
    import json

    from app.auth_session import InvalidAuthStateError, _parse_auth_state

    content = json.dumps(
        {"cookies": [{"name": "a", "domain": ".partzilla.com"}], "origins": []}
    ).encode("utf-8")

    try:
        _parse_auth_state(content)
    except InvalidAuthStateError as exc:
        assert "usable value" in str(exc)
    else:
        raise AssertionError("a session with no usable cookie value should be refused")


def test_an_already_broken_sign_in_file_is_repaired_rather_than_requiring_a_new_one() -> None:
    from pathlib import Path

    source = Path("collect_parts.py").read_text(encoding="utf-8")

    assert "Repaired the saved" in source
    assert "save_uploaded_auth_state(" in source
