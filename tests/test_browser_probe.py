from __future__ import annotations

from app.browser_probe import detect_page_signals


def test_detects_visible_product_page_signals() -> None:
    signals = detect_page_signals(
        text="Sign In To See Price MSRP: $282.32 In Stock Ships in 3 to 4 days",
        html="<html></html>",
    )

    assert signals == ["Sign in to see price", "MSRP", "In Stock", "Ships in"]


def test_recaptcha_script_alone_is_not_reported_as_challenge() -> None:
    signals = detect_page_signals(
        text="KAWASAKI OEM DISC Sign In To See Price",
        html='<script src="https://www.google.com/recaptcha/enterprise.js"></script>',
    )

    assert "CAPTCHA or challenge language" not in signals


def test_visible_challenge_text_is_reported() -> None:
    signals = detect_page_signals(
        text="Please verify you are human before continuing.",
        html="<html></html>",
    )

    assert "CAPTCHA or challenge language" in signals
