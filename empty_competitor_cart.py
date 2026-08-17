"""Empty a competitor's cart, and report exactly what the remove controls are.

Two purposes. It clears a cart that has accumulated items, which otherwise has
to be done by hand. And it prints the real markup of the removal controls, so
selectors can be based on what the page actually contains rather than guessed
at.

It uses the same browser profile the collector uses, so it sees the same cart
and the same session.

    .venv\\Scripts\\python.exe empty_competitor_cart.py motosport
    .venv\\Scripts\\python.exe empty_competitor_cart.py motosport --inspect-only
"""

from __future__ import annotations

import argparse
import sys

from playwright.sync_api import sync_playwright

from app.browser_profile import launch_persistent_competitor_context

CART_URLS = {
    "motosport": "https://www.motosport.com/cart",
    "chaparral": "https://www.chaparral-racing.com/cart",
    "partzilla": "https://www.partzilla.com/cart",
}

# Reported so the shape of each line is visible, not just whether a guess hit.
INSPECT_SCRIPT = """
() => {
  const results = [];
  const seen = new Set();
  const clickable = document.querySelectorAll('button, a, [role="button"], input[type="submit"], [onclick]');
  for (const el of clickable) {
    const label = (el.getAttribute('aria-label') || '') + ' ' +
                  (el.getAttribute('title') || '') + ' ' +
                  (el.className || '') + ' ' +
                  (el.id || '') + ' ' +
                  (el.getAttribute('data-testid') || '') + ' ' +
                  (el.textContent || '').trim().slice(0, 40);
    const looksRemoval = /remove|delete|trash|bin|discard/i.test(label);
    const hasIcon = el.querySelector('svg, i, use, img') !== null;
    if (!looksRemoval && !(hasIcon && el.closest('[class*="cart" i], [class*="item" i], tr'))) continue;
    const key = el.tagName + '|' + (el.className || '') + '|' + (el.getAttribute('aria-label') || '');
    if (seen.has(key)) continue;
    seen.add(key);
    results.push({
      tag: el.tagName.toLowerCase(),
      id: el.id || '',
      className: typeof el.className === 'string' ? el.className : '',
      ariaLabel: el.getAttribute('aria-label') || '',
      title: el.getAttribute('title') || '',
      dataTestId: el.getAttribute('data-testid') || '',
      name: el.getAttribute('name') || '',
      type: el.getAttribute('type') || '',
      text: (el.textContent || '').trim().slice(0, 40),
      hasIcon: hasIcon,
      formAction: el.form ? (el.form.getAttribute('action') || '') : '',
      outer: el.outerHTML.slice(0, 300)
    });
  }
  return results;
}
"""


def describe_controls(page) -> list[dict]:
    try:
        return page.evaluate(INSPECT_SCRIPT) or []
    except Exception as exc:
        print(f"  Could not inspect the page: {exc}")
        return []


def main() -> int:
    parser = argparse.ArgumentParser(description="Empty a competitor's cart and report its remove controls.")
    parser.add_argument("competitor", nargs="?", default="motosport")
    parser.add_argument("--inspect-only", action="store_true", help="Report the controls without clicking anything.")
    parser.add_argument("--headless", action="store_true", help="Run without showing the browser.")
    args = parser.parse_args()

    competitor = args.competitor.strip().lower()
    url = CART_URLS.get(competitor)
    if not url:
        print(f"No cart address known for '{competitor}'. Known: {', '.join(sorted(CART_URLS))}")
        return 1

    print("=" * 68)
    print(f"  {competitor} cart")
    print("=" * 68)

    with sync_playwright() as playwright:
        context = launch_persistent_competitor_context(playwright, competitor, headless=args.headless)
        page = context.pages[0] if context.pages else context.new_page()
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(3000)

            from probe_cart_price import clear_whole_cart, count_cart_lines

            lines = count_cart_lines(page)
            print(f"\nCart lines detected: {lines}")

            controls = describe_controls(page)
            print(f"Possible removal controls found: {len(controls)}\n")
            for index, control in enumerate(controls[:12], start=1):
                print(f"  {index}. <{control['tag']}>")
                for field in ("id", "className", "ariaLabel", "title", "dataTestId", "name", "type", "text", "formAction"):
                    if control.get(field):
                        print(f"       {field}: {control[field]}")
                print(f"       has icon: {control['hasIcon']}")
                print(f"       html: {control['outer'][:200]}")
                print("")

            if args.inspect_only:
                print("Inspect only, so nothing was changed.")
                return 0

            print("Clearing the cart...\n")
            result = clear_whole_cart(page)
            print(f"  cleared: {result['cleared']}")
            print(f"  removed: {result['removed']}")
            print(f"  reason:  {result['reason']}")
            if result.get("detail"):
                print(f"  detail:  {result['detail']}")

            page.reload(wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(2500)
            remaining = count_cart_lines(page)
            print(f"\nCart lines remaining after reload: {remaining}")
            if remaining:
                print("\nStill not empty. Send the control details above to Claude:")
                print("they show what the page actually contains, so the selector")
                print("can be based on that rather than guessed at.")
            else:
                print("\nThe cart is empty.")
            return 0
        finally:
            try:
                context.close()
            except Exception:
                pass


if __name__ == "__main__":
    sys.exit(main())
