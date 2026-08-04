from __future__ import annotations

from pathlib import Path

from app.auth_session import (
    InvalidAuthStateError,
    MissingAuthStateError,
    auth_state_path_for,
    delete_auth_state,
    determine_session_status,
    require_auth_state,
    save_uploaded_auth_state,
    serialized_observation_has_sensitive_fields,
    write_sanitized_authenticated_diagnostics,
)
from app.models import PartRecord
from app.parsers.partzilla_product_parser import ProductParseInput, parse_partzilla_product_page
from app.schemas.product_observation import PageClassification, PriceVisibility, SessionStatus

TEST_OUTPUT_DIR = Path("data/output/test-artifacts")


def test_missing_auth_state_tells_user_to_bootstrap() -> None:
    missing_path = Path("data/private/definitely_missing_auth_state.json")

    try:
        require_auth_state(missing_path)
    except MissingAuthStateError as exc:
        assert "Run auth_bootstrap.py first" in str(exc)
    else:
        raise AssertionError("missing auth state should raise")


def test_competitor_auth_state_paths_are_private_and_stable() -> None:
    assert auth_state_path_for("partzilla").as_posix().endswith("data/private/partzilla_auth_state.json")
    assert auth_state_path_for("MotoSport").as_posix().endswith("data/private/motosport_auth_state.json")


def test_authenticated_session_classification() -> None:
    observation = _product_observation(
        visible_text="KAWASAKI OEM DISC 41080-1514\nPart #: 41080-1514\nManufacturer: KAWASAKI\nMSRP: $282.32\nPrice\n$241.99\nIn Stock\nQuantity",
        html=_normal_html('<div data-testid="productPrice">$241.99</div>'),
    )

    assert observation.price_visibility == PriceVisibility.VISIBLE
    assert determine_session_status(observation, auth_state_loaded=True) == SessionStatus.AUTHENTICATED


def test_authentication_required_without_saved_state() -> None:
    observation = _product_observation(
        visible_text="KAWASAKI OEM DISC 41080-1514\nPart #: 41080-1514\nManufacturer: KAWASAKI\nMSRP: $282.32\nSign In To See Price\nShips in 3 to 4 days",
        html=_normal_html('<button data-testid="authModalButton">Sign In To See Price MSRP: $282.32</button>'),
    )

    assert determine_session_status(observation, auth_state_loaded=False) == SessionStatus.AUTHENTICATION_REQUIRED


def test_expired_or_invalid_saved_state() -> None:
    observation = _product_observation(
        visible_text="KAWASAKI OEM DISC 41080-1514\nPart #: 41080-1514\nManufacturer: KAWASAKI\nMSRP: $282.32\nSign In To See Price\nShips in 3 to 4 days",
        html=_normal_html('<button data-testid="authModalButton">Sign In To See Price MSRP: $282.32</button>'),
    )

    assert determine_session_status(observation, auth_state_loaded=True) == SessionStatus.EXPIRED_OR_INVALID


def test_blocked_and_challenge_session_statuses() -> None:
    blocked = _product_observation(visible_text="Access denied", html="<html><body>Access denied</body></html>", http_status=403)
    challenge = _product_observation(
        visible_text="Complete the security check",
        html="<html><body>Complete the security check</body></html>",
    )

    assert blocked.page_classification == PageClassification.BLOCKED
    assert challenge.page_classification == PageClassification.CHALLENGE
    assert determine_session_status(blocked, auth_state_loaded=True) == SessionStatus.BLOCKED
    assert determine_session_status(challenge, auth_state_loaded=True) == SessionStatus.CHALLENGE


def test_observation_json_has_no_cookie_or_token_fields() -> None:
    observation = _product_observation(
        visible_text="KAWASAKI OEM DISC 41080-1514\nPart #: 41080-1514\nManufacturer: KAWASAKI\nMSRP: $282.32\nPrice\n$241.99\nIn Stock\nQuantity",
        html=_normal_html('<div data-testid="productPrice">$241.99</div>'),
    )

    assert serialized_observation_has_sensitive_fields(observation) is False


def test_sanitized_diagnostics_do_not_include_auth_state_content() -> None:
    TEST_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    diagnostics_path = TEST_OUTPUT_DIR / "sanitized_auth_diagnostics.txt"
    observation = _product_observation(
        visible_text="KAWASAKI OEM DISC 41080-1514\nPart #: 41080-1514\nManufacturer: KAWASAKI\nMSRP: $282.32\nPrice\n$241.99\nIn Stock\nQuantity",
        html=_normal_html('<div data-testid="productPrice">$241.99</div>'),
    )

    write_sanitized_authenticated_diagnostics(
        diagnostics_path,
        observation,
        exception_message="cookie SECRET_VALUE token SECRET_VALUE",
    )

    content = diagnostics_path.read_text(encoding="utf-8").lower()
    assert "secret_value" not in content
    assert "cookie" not in content
    assert "token" not in content


def test_data_private_is_gitignored() -> None:
    gitignore = Path(".gitignore").read_text(encoding="utf-8")

    assert "data/private/" in gitignore


def test_uploaded_auth_state_is_validated_and_saved() -> None:
    path = auth_state_path_for("testauth")
    delete_auth_state("testauth")

    saved = save_uploaded_auth_state(
        "testauth",
        b'{"cookies":[{"name":"session","value":"abc","domain":".example.com"}],"origins":[]}',
    )

    assert saved == path
    assert path.exists()
    delete_auth_state("testauth")


def test_uploaded_auth_state_rejects_invalid_json() -> None:
    try:
        save_uploaded_auth_state("testauth", b"not json")
    except InvalidAuthStateError as exc:
        assert "valid Playwright login session" in str(exc)
    else:
        raise AssertionError("invalid auth state should raise")


def _normal_html(extra: str) -> str:
    return f"""
    <html><title>KAWASAKI OEM DISC - 41080-1514 | partzilla.com</title>
    <body><h1>KAWASAKI OEM DISC | 41080-1514</h1>
    <span data-testid="productDetailPartNumber">Part #: 41080-1514</span>
    <span data-testid="productFilterValueManufacturer-KAWASAKI">KAWASAKI</span>
    <span>MSRP: $282.32</span>
    {extra}
    <button data-testid="stockInfoText">In Stock</button>
    </body></html>
    """


def _product_observation(visible_text: str, html: str, http_status: int | None = 200):
    record = PartRecord(test_case_id="KAW-004", manufacturer="Kawasaki", oem_part_number="41080-1514")
    return parse_partzilla_product_page(
        ProductParseInput(
            record=record,
            requested_url="https://www.partzilla.com/product/kawasaki/41080-1514",
            final_url="https://www.partzilla.com/product/kawasaki/41080-1514",
            http_status=http_status,
            page_title="KAWASAKI OEM DISC - 41080-1514 | partzilla.com",
            navigation_succeeded=True,
            exception_message=None,
            visible_text=visible_text,
            html=html,
            detected_signals=[],
            checked_at="2026-07-08T00:00:00Z",
        )
    )
