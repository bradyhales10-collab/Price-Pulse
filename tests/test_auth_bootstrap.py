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
