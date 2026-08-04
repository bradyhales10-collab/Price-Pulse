"""Keep the sign-in browser usable.

Playwright's Chromium has no extensions, so nothing blocks what a desktop
browser normally would. On Partzilla's sign-in page a live chat widget opened a
popup, which redirected to Google auth, closed, and was reopened by the widget,
making it impossible to type a password.

Only that widget is blocked. Analytics and advertising hosts are deliberately
left alone: blocking them never fixed the popup loop, and a site whose own
analytics never load is an obvious anomaly that can get the browser refused
outright. This is used on the sign-in page only, not during collection, so a
price check behaves exactly as it did before any of this existed.
"""

from __future__ import annotations

import re

# Live chat and contact centre widgets, the ones observed opening popups on a
# sign-in page.
POPUP_WIDGET_HOST_PATTERNS = (
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

# Never blocked, so a social sign-in still works.
SIGN_IN_HOST_ALLOWLIST = (
    "accounts.google.com",
    "www.facebook.com",
    "facebook.com",
    "appleid.apple.com",
    "login.microsoftonline.com",
)

# Partzilla occasionally tries to open this first-party metrics endpoint as a
# separate tab during sign-in. Normal in-page metrics requests remain allowed;
# only a top-level navigation in a secondary page is stopped.
POPUP_NAVIGATION_HOSTS = ("metrics.partzilla.com",)

# Chromium refusing to create new tabs at all. Stronger than removing
# window.open from the page, which a script can work around.
NO_POPUP_BROWSER_ARGS = (
    "--block-new-web-contents",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-notifications",
)

_COMPILED = tuple(re.compile(pattern, re.IGNORECASE) for pattern in POPUP_WIDGET_HOST_PATTERNS)


def is_popup_widget_host(hostname: str) -> bool:
    host = (hostname or "").strip().lower()
    if not host:
        return False
    if host in SIGN_IN_HOST_ALLOWLIST:
        return False
    return any(pattern.search(host) for pattern in _COMPILED)


def is_popup_navigation_host(hostname: str) -> bool:
    host = (hostname or "").strip().lower()
    return any(host == blocked or host.endswith(f".{blocked}") for blocked in POPUP_NAVIGATION_HOSTS)


def block_popup_widgets(context, *, primary_page=None) -> None:
    """Abort requests to chat widgets that open popups."""

    def handler(route):
        try:
            request = route.request
            hostname = _hostname(request.url)
            is_secondary_navigation = False
            if is_popup_navigation_host(hostname):
                try:
                    is_secondary_navigation = bool(request.is_navigation_request()) and (
                        primary_page is None or request.frame.page is not primary_page
                    )
                except Exception:
                    is_secondary_navigation = primary_page is None
            if is_popup_widget_host(hostname) or is_secondary_navigation:
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
    """Stop pages opening new windows.

    Closing popups is not enough when a widget reopens whatever gets closed.
    Nothing needed to type a username and password uses window.open, so removing
    it is safe here. A social sign-in that insists on a popup would be affected,
    which is why the sign-in tool exposes a flag to leave popups enabled.
    """
    context.add_init_script(
        "window.open = function () { return null; };"
        "document.addEventListener('click', function (event) {"
        "  var link = event.target && event.target.closest"
        "    && event.target.closest('a[target=\"_blank\"]');"
        "  if (link) { link.removeAttribute('target'); }"
        "}, true);"
    )


def close_popup_pages(context, keep) -> None:
    """Close any page other than the one the person is using."""

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
        return urlparse(url or "").hostname or ""
    except Exception:
        return ""
