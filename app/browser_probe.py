from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

from app.config import (
    DEFAULT_VIEWPORT,
    DIAGNOSTICS_DIR,
    HTML_DIR,
    SCREENSHOTS_DIR,
    ProbeSettings,
    ensure_data_directories,
)
from app.models import PartRecord, ProbeDiagnostics
from app.url_builder import build_partzilla_product_url


LOGGER = logging.getLogger(__name__)

VISIBLE_SIGNAL_PATTERNS = {
    "Sign in to see price": ["sign in to see price", "login to see price"],
    "MSRP": ["msrp"],
    "In Stock": ["in stock"],
    "Ships in": ["ships in", "usually ships"],
    "Add to Cart": ["add to cart", "add-to-cart"],
}

VISIBLE_BLOCK_PATTERNS = {
    "Access Denied": ["access denied", "request blocked", "forbidden"],
    "CAPTCHA or challenge language": [
        "captcha",
        "verify you are human",
        "checking your browser",
        "are you a human",
        "security check",
        "complete the security check",
    ],
}


def _timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _safe_filename(value: str) -> str:
    return "".join(char if char.isalnum() or char in ("-", "_") else "_" for char in value)


def detect_page_signals(text: str, html: str) -> list[str]:
    visible_text = text.lower()
    full_html = html.lower()
    signals = [
        signal
        for signal, patterns in VISIBLE_SIGNAL_PATTERNS.items()
        if any(pattern in visible_text or pattern in full_html for pattern in patterns)
    ]

    # Block/challenge terms should be visible page content, not just vendor script names.
    signals.extend(
        signal
        for signal, patterns in VISIBLE_BLOCK_PATTERNS.items()
        if any(pattern in visible_text for pattern in patterns)
    )

    if "cf-challenge" in full_html or "g-recaptcha-response" in visible_text:
        signals.append("CAPTCHA or challenge language")

    return [
        signal
        for index, signal in enumerate(signals)
        if signal not in signals[:index]
    ]


def _write_report(path: Path, diagnostics: ProbeDiagnostics) -> None:
    lines = [
        f"timestamp: {diagnostics.timestamp}",
        f"test_case_id: {diagnostics.test_case_id or ''}",
        f"manufacturer: {diagnostics.manufacturer}",
        f"oem_part_number: {diagnostics.oem_part_number}",
        f"requested_url: {diagnostics.requested_url}",
        f"final_url: {diagnostics.final_url or ''}",
        f"http_response_status: {diagnostics.http_status if diagnostics.http_status is not None else ''}",
        f"page_title: {diagnostics.page_title or ''}",
        f"detected_page_signals: {', '.join(diagnostics.detected_signals) or 'None'}",
        f"navigation_succeeded: {diagnostics.navigation_succeeded}",
        f"exception_message: {diagnostics.exception_message or ''}",
        f"screenshot_path: {diagnostics.screenshot_path or ''}",
        f"html_path: {diagnostics.html_path or ''}",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def probe_partzilla_page(record: PartRecord, settings: ProbeSettings) -> ProbeDiagnostics:
    ensure_data_directories()

    requested_url = build_partzilla_product_url(record.manufacturer, record.oem_part_number)
    stamp = _timestamp()
    stem = f"{stamp}_{_safe_filename(record.oem_part_number)}"
    screenshot_path = SCREENSHOTS_DIR / f"{stem}.png"
    html_path = HTML_DIR / f"{stem}.html"
    report_path = DIAGNOSTICS_DIR / f"{stem}.txt"

    final_url: str | None = None
    status: int | None = None
    title: str | None = None
    html = ""
    text = ""
    signals: list[str] = []
    navigation_succeeded = False
    exception_message: str | None = None

    LOGGER.info("Starting single-page probe for %s at %s", record.oem_part_number, requested_url)

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                headless=settings.headless,
                slow_mo=settings.slow_mo,
            )
            context = browser.new_context(viewport=DEFAULT_VIEWPORT)
            page = context.new_page()
            page.set_default_timeout(settings.timeout)
            page.set_default_navigation_timeout(settings.timeout)

            response = page.goto(requested_url, wait_until="domcontentloaded", timeout=settings.timeout)
            navigation_succeeded = True
            if response is not None:
                status = response.status

            page.wait_for_timeout(settings.render_settle_ms)
            final_url = page.url
            title = page.title()
            html = page.content()
            text = page.locator("body").inner_text(timeout=5000) if page.locator("body").count() else ""
            signals = detect_page_signals(text=text, html=html)

            page.screenshot(path=screenshot_path, full_page=True)
            html_path.write_text(html, encoding="utf-8")
            context.close()
            browser.close()

    except (PlaywrightTimeoutError, PlaywrightError, Exception) as exc:
        exception_message = str(exc)
        LOGGER.exception("Probe failed for %s", record.oem_part_number)

        try:
            if html:
                html_path.write_text(html, encoding="utf-8")
        except OSError:
            LOGGER.exception("Could not write partial HTML diagnostics.")

    diagnostics = ProbeDiagnostics(
        test_case_id=record.test_case_id,
        manufacturer=record.manufacturer,
        oem_part_number=record.oem_part_number,
        requested_url=requested_url,
        final_url=final_url,
        http_status=status,
        page_title=title,
        detected_signals=signals,
        timestamp=stamp,
        navigation_succeeded=navigation_succeeded,
        exception_message=exception_message,
        screenshot_path=str(screenshot_path) if screenshot_path.exists() else None,
        html_path=str(html_path) if html_path.exists() else None,
    )

    _write_report(report_path, diagnostics)
    LOGGER.info("Diagnostics report saved to %s", report_path)
    return diagnostics
