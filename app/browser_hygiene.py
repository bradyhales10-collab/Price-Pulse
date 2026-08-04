"""Keep the browsers Price Pulse drives free of tracking scripts and popups.

Playwright's Chromium is a clean browser with no extensions, so nothing blocks
advertising or analytics. A normal desktop browser usually has an ad blocker or
tracking protection, which is why a page can behave completely differently in
each. On the sign-in page this mattered: tracking scripts were opening popups
that took focus away mid-typing and then closed themselves, making it
impossible to sign in.

Blocking these also cuts the number of requests a price check makes, since
none of it is needed to read a price.
"""

from __future__ import annotations

import re

# Hosts that only serve analytics, advertising or session recording. Matched
# against the request hostname.
TRACKING_HOST_PATTERNS = (
    r"^metrics\.",
    r"(^|\.)google-analytics\.com$",
    r"(^|\.)googletagmanager\.com$",
    r"(^|\.)googlesyndication\.com$",
    r"(^|\.)googleadservices\.com$",
    r"(^|\.)doubleclick\.net$",
    r"(^|\.)connect\.facebook\.net$",
    r"(^|\.)bat\.bing\.com$",
    r"(^|\.)analytics\.tiktok\.com$",
    r"(^|\.)hotjar\.com$",
    r"(^|\.)clarity\.ms$",
    r"(^|\.)segment\.(io|com)$",
    r"(^|\.)mixpanel\.com$",
    r"(^|\.)klaviyo\.com$",
    r"(^|\.)attentivemobile\.com$",
    r"(^|\.)criteo\.(com|net)$",
    r"(^|\.)taboola\.com$",
    r"(^|\.)outbrain\.com$",
    r"(^|\.)pinterest\.com$",
    r"(^|\.)snap\.licdn\.com$",
    r"(^|\.)cdn\.heapanalytics\.com$",
    r"(^|\.)fullstory\.com$",
    r"(^|\.)quantserve\.com$",
    r"(^|\.)scorecardresearch\.com$",
    # Live chat / contact centre widgets. Partzilla's opens a popup that
    # redirects to Google auth, closes, and is reopened by the widget.
    r"(^|\.)mypurecloud\.com$",
    r"(^|\.)genesys\.com$",
    r"(^|\.)genesyscloud\.com$",
    r"(^|\.)inindca\.com$",
    r"(^|\.)livechatinc\.com$",
    r"(^|\.)zopim\.com$",
    r"(^|\.)zdassets\.com$",
    r"(^|\.)intercom\.io$",
    r"(^|\.)drift\.com$",
)

# Never blocked, even if a pattern above would match: these can be needed to
# actually sign in when a site offers a social login.
SIGN_IN_HOST_ALLOWLIST = (
    "accounts.google.com",
    "www.facebook.com",
    "facebook.com",
    "appleid.apple.com",
    "login.microsoftonline.com",
)

_COMPILED = tuple(re.compile(pattern, re.IGNORECASE) for pattern in TRACKING_HOST_PATTERNS)


def is_tracking_host(hostname: str) -> bool:
    host = (hostname or "").strip().lower()
    if not host:
        return False
    if host in SIGN_IN_HOST_ALLOWLIST:
        return False
    return any(pattern.search(host) for pattern in _COMPILED)


def block_tracking_requests(context) -> None:
    """Abort requests to analytics and advertising hosts."""

    def handler(route):
        try:
            hostname = _hostname(route.request.url)
            if is_tracking_host(hostname):
                route.abort()
                return
            route.continue_()
        except Exception:
            # Never let request filtering break the page it is filtering.
            try:
                route.continue_()
            except Exception:
                pass

    context.route("**/*", handler)


def disable_popups(context) -> None:
    """Stop pages opening new windows at all.

    Closing popups is not enough when a widget reopens whatever gets closed,
    which is the loop seen on the Partzilla sign-in page. Nothing needed to
    type a username and password uses window.open, so removing it is safe here.
    A social sign-in that insists on a popup would be affected, which is why
    the sign-in tool exposes a flag to leave popups enabled.
    """
    context.add_init_script(
        "window.open = function () { return null; };"
        "document.addEventListener('click', function (event) {"
        "  var link = event.target && event.target.closest && event.target.closest('a[target=\"_blank\"]');"
        "  if (link) { link.removeAttribute('target'); }"
        "}, true);"
    )


def close_popup_pages(context, keep) -> None:
    """Close any page other than the one the person is using.

    Tracking scripts open popups that take focus and then close themselves.
    Closing them immediately stops them stealing a keystroke mid-sign-in.
    """

    def handler(page) -> None:
        if page is keep:
            return
        try:
            page.close()
        except Exception:
            pass

    context.on("page", handler)


def _hostname(url: str) -> str:
    from urllib.parse import urlparse

    try:
        return urlparse(url).hostname or ""
    except Exception:
        return ""
