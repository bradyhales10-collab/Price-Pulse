from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

from app.competitors.chaparral import build_search_url
from app.config import DATA_DIR, DEFAULT_VIEWPORT, ensure_data_directories
from app.database import utc_now


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Diagnose Chaparral OEM search lookup behavior.")
    parser.add_argument("--manufacturer", default="Honda")
    parser.add_argument("--part-number", default="15410-MFJ-D02")
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--timeout-ms", type=int, default=30000)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    ensure_data_directories()
    run_id = utc_now().replace(":", "").replace("-", "")
    output_dir = DATA_DIR / "output" / "competitor_probes" / "chaparral_diagnostic" / run_id / args.part_number
    output_dir.mkdir(parents=True, exist_ok=True)
    network: list[dict[str, Any]] = []
    redirects: list[dict[str, Any]] = []
    summary: dict[str, Any] = {
        "competitor": "Chaparral Motorsports",
        "lookup_url": build_search_url(args.part_number),
        "manufacturer": args.manufacturer,
        "part_number": args.part_number,
        "started_at": utc_now(),
        "output_dir": str(output_dir),
    }

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=args.headless)
        context = browser.new_context(viewport=DEFAULT_VIEWPORT)
        page = context.new_page()
        page.set_default_timeout(args.timeout_ms)
        page.set_default_navigation_timeout(args.timeout_ms)
        page.on("request", lambda request: network.append(_request_record(request)))
        page.on("response", lambda response: network.append(_response_record(response)))
        page.on("framenavigated", lambda frame: redirects.append({"url": frame.url, "timestamp": utc_now()}))

        response = page.goto(build_search_url(args.part_number), wait_until="domcontentloaded", timeout=args.timeout_ms)
        page.wait_for_timeout(1500)
        summary["initial_http_status"] = response.status if response is not None else None
        summary["initial_url"] = page.url
        _save_page_state(page, output_dir, "before")
        try:
            page.wait_for_load_state("networkidle", timeout=args.timeout_ms)
        except PlaywrightTimeoutError:
            summary["networkidle_timeout"] = True
        summary["controls_before_submit"] = _controls(page)
        summary["lookup_wait_result"] = _wait_for_lookup_result(page, args.part_number, args.timeout_ms)
        page.wait_for_timeout(1500)
        summary["result"] = "search_loaded"
        summary["final_url"] = page.url
        summary["cookies"] = context.cookies()
        summary["controls_after_submit"] = summary["controls_before_submit"]
        summary["flow_classification"] = _classify_flow(network, summary["initial_url"], summary["final_url"])
        summary["completed_at"] = utc_now()
        _save_page_state(page, output_dir, "after")
        context.close()
        browser.close()

    (output_dir / "network.json").write_text(json.dumps(network, indent=2) + "\n", encoding="utf-8")
    (output_dir / "redirects.json").write_text(json.dumps(redirects, indent=2) + "\n", encoding="utf-8")
    (output_dir / "diagnostic_summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    (output_dir / "diagnostic_summary.txt").write_text(_summary_text(summary, network), encoding="utf-8")
    print(f"Chaparral diagnostic output: {output_dir}")
    return 0


def _save_page_state(page, output_dir: Path, label: str) -> None:
    prefix = "01" if label == "before" else "03"
    page.screenshot(path=str(output_dir / f"{prefix}_{label}_submission.png"), full_page=True)
    (output_dir / f"{prefix}_{label}_submission.html").write_text(page.content(), encoding="utf-8")
    text = page.locator("body").inner_text(timeout=5000) if page.locator("body").count() else ""
    (output_dir / f"{prefix}_{label}_visible_text.txt").write_text(text, encoding="utf-8")
    (output_dir / f"{prefix}_{label}_forms.json").write_text(json.dumps(_forms(page), indent=2) + "\n", encoding="utf-8")
    (output_dir / f"{prefix}_{label}_links.json").write_text(json.dumps(_links(page), indent=2) + "\n", encoding="utf-8")


def _controls(page) -> dict[str, Any]:
    return {
        "fields": page.locator("input, textarea, select").evaluate_all(
            """els => els.map(el => ({
                tag: el.tagName,
                type: el.getAttribute('type'),
                name: el.getAttribute('name'),
                id: el.id,
                placeholder: el.getAttribute('placeholder'),
                ariaLabel: el.getAttribute('aria-label'),
                visibleText: el.innerText || el.value || ''
            }))"""
        ),
        "buttons": page.locator("button, input[type=submit], input[type=button]").evaluate_all(
            """els => els.map(el => ({
                tag: el.tagName,
                type: el.getAttribute('type'),
                name: el.getAttribute('name'),
                id: el.id,
                text: el.innerText || el.value || '',
                ariaLabel: el.getAttribute('aria-label')
            }))"""
        ),
    }


def _forms(page) -> list[dict[str, Any]]:
    return page.locator("form").evaluate_all(
        """forms => forms.map(form => ({
            action: form.action,
            method: form.method,
            id: form.id,
            name: form.getAttribute('name'),
            fields: Array.from(form.querySelectorAll('input, textarea, select')).map(el => ({
                tag: el.tagName,
                type: el.getAttribute('type'),
                name: el.getAttribute('name'),
                id: el.id,
                placeholder: el.getAttribute('placeholder')
            }))
        }))"""
    )


def _links(page) -> list[dict[str, str]]:
    return page.locator("a[href]").evaluate_all(
        """links => links.map(link => ({ href: link.href, text: (link.innerText || '').trim() })).filter(link => link.href || link.text)"""
    )


def _request_record(request) -> dict[str, Any]:
    return {
        "event": "request",
        "timestamp": utc_now(),
        "method": request.method,
        "url": request.url,
        "resource_type": request.resource_type,
        "post_data": _safe_post_data(request),
    }


def _response_record(response) -> dict[str, Any]:
    return {
        "event": "response",
        "timestamp": utc_now(),
        "status": response.status,
        "url": response.url,
        "content_type": response.headers.get("content-type", ""),
    }


def _safe_post_data(request) -> str | None:
    try:
        value = request.post_data
    except Exception:
        return None
    if value is None:
        return None
    return value[:4000]


def _wait_for_lookup_result(page, part_number: str, timeout_ms: int) -> str:
    deadline = time.monotonic() + (timeout_ms / 1000)
    normalized = "".join(char for char in part_number.upper() if char.isalnum())
    last_state = "unknown"
    while time.monotonic() < deadline:
        try:
            text = page.locator("body").inner_text(timeout=2000) if page.locator("body").count() else ""
        except Exception:
            text = ""
        text_normalized = "".join(char for char in text.upper() if char.isalnum())
        if normalized and normalized in text_normalized:
            return "part_number_rendered"
        if "LOADING" not in text.upper() and "0 % COMPLETE" not in text.upper() and "0% COMPLETE" not in text.upper():
            return "loading_cleared"
        last_state = "loading"
        page.wait_for_timeout(1000)
    return f"timeout_{last_state}"


def _classify_flow(network: list[dict[str, Any]], initial_url: str, final_url: str) -> dict[str, Any]:
    request_urls = [item["url"] for item in network if item.get("event") == "request"]
    response_types = [item.get("content_type", "") for item in network if item.get("event") == "response"]
    post_requests = [item for item in network if item.get("event") == "request" and item.get("method") == "POST"]
    json_responses = [content_type for content_type in response_types if "json" in content_type.lower()]
    graph_requests = [url for url in request_urls if "graphql" in url.lower()]
    return {
        "standard_navigation": initial_url != final_url,
        "post_request_count": len(post_requests),
        "ajax_or_json_detected": bool(json_responses),
        "graphql_detected": bool(graph_requests),
        "likely_flow": _likely_flow(initial_url, final_url, post_requests, json_responses, graph_requests),
        "post_requests": post_requests,
        "json_response_content_types": json_responses,
        "graphql_request_urls": graph_requests,
    }


def _likely_flow(initial_url: str, final_url: str, post_requests: list[dict[str, Any]], json_responses: list[str], graph_requests: list[str]) -> str:
    if graph_requests:
        return "graphql_request"
    if json_responses:
        return "ajax_or_json_endpoint"
    if post_requests and initial_url != final_url:
        return "standard_html_form_submission"
    if post_requests:
        return "post_request_with_javascript_rendered_result"
    if initial_url != final_url:
        return "get_navigation"
    return "javascript_rendered_or_no_submit_detected"


def _summary_text(summary: dict[str, Any], network: list[dict[str, Any]]) -> str:
    flow = summary.get("flow_classification", {})
    lines = [
        "CHAPARRAL OEM SEARCH DIAGNOSTIC",
        f"Part number: {summary['part_number']}",
        f"Lookup URL: {summary['lookup_url']}",
        f"Initial URL: {summary.get('initial_url', '')}",
        f"Final URL: {summary.get('final_url', '')}",
        f"Result: {summary.get('result', '')}",
        f"Likely flow: {flow.get('likely_flow', '')}",
        f"POST requests: {flow.get('post_request_count', 0)}",
        f"AJAX/JSON detected: {flow.get('ajax_or_json_detected', False)}",
        f"GraphQL detected: {flow.get('graphql_detected', False)}",
        f"Lookup wait result: {summary.get('lookup_wait_result', '')}",
        f"Network events: {len(network)}",
        "",
        "Review diagnostic_summary.json, network.json, screenshots, page HTML, visible text, forms, and links for search result selectors/endpoints.",
    ]
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    sys.exit(main())
