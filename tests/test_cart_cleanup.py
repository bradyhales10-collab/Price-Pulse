"""Emptying the MotoSport cart after reading a price.

Reading a MotoSport price means adding the item to the cart, so the cart has to
be emptied afterwards or items accumulate across a run. Worse, a cart that is
not empty made the collector skip adding anything at all, so a single failed
cleanup silently cost every later part its price.
"""

from __future__ import annotations

from probe_cart_price import clear_whole_cart


class _CartPage:
    """A cart shaped like the real MotoSport one.

    Removal is a trash-can icon with an accessible name, the only text link is
    "Save For Later", and the word "Remove" appears nowhere on the page. That
    last detail is what defeated the original implementation.
    """

    def __init__(self, items: int, *, removals_work: bool = True) -> None:
        self.items = items
        self.removals_work = removals_work
        self.clicks = 0

    def locator(self, selector: str):
        page = self

        class _Locator:
            def count(self) -> int:
                if selector == "body":
                    return 1
                if page.items <= 0:
                    return 0
                if "aria-label*='Remove'" in selector or "cart-item" in selector:
                    return page.items
                return 0

            def inner_text(self, timeout: int | None = None) -> str:
                if page.items <= 0:
                    return "Your cart is empty"
                return " ".join(["Kawasaki OEM Parts Save For Later"] * page.items)

            @property
            def first(self):
                return self

            def click(self, timeout: int | None = None) -> None:
                page.clicks += 1
                if page.removals_work:
                    page.items -= 1

        return _Locator()

    def wait_for_timeout(self, milliseconds: int) -> None:
        return None


def test_every_item_is_removed_from_a_dirty_cart() -> None:
    page = _CartPage(3)

    result = clear_whole_cart(page)

    assert result["cleared"] is True
    assert result["removed"] == 3
    assert page.items == 0


def test_an_already_empty_cart_needs_no_work() -> None:
    page = _CartPage(0)

    result = clear_whole_cart(page)

    assert result["cleared"] is True
    assert result["removed"] == 0
    assert page.clicks == 0


def test_it_stops_rather_than_looping_when_removal_has_no_effect() -> None:
    """A click that does nothing will not start working on the tenth attempt,
    and a loop here would hang the whole run."""
    page = _CartPage(3, removals_work=False)

    result = clear_whole_cart(page)

    assert result["cleared"] is False
    assert result["reason"] == "remove_had_no_effect"
    assert page.clicks == 1


def test_items_with_no_remove_control_are_reported_not_guessed_at() -> None:
    class _NoRemove(_CartPage):
        def locator(self, selector: str):
            outer = self

            class _Locator:
                def count(self) -> int:
                    # Only the page body matches; nothing looks like a control.
                    return 1 if selector == "body" else 0

                def inner_text(self, timeout: int | None = None) -> str:
                    return "Some Item In Cart"

                @property
                def first(self):
                    return self

                def click(self, timeout: int | None = None) -> None:
                    outer.clicks += 1

            return _Locator()

    page = _NoRemove(2)

    result = clear_whole_cart(page)

    assert result["cleared"] is False
    assert result["reason"] == "no_remove_control_found"
    assert page.clicks == 0


def test_an_unreadable_cart_is_reported_rather_than_assumed_empty() -> None:
    """Assuming empty would let items accumulate silently, which is the problem
    this exists to prevent."""

    class _Broken:
        def locator(self, selector: str):
            raise RuntimeError("page gone")

    result = clear_whole_cart(_Broken())

    assert result["cleared"] is False
    assert result["reason"] == "cart_not_readable"


def test_a_dirty_cart_no_longer_causes_the_part_to_be_skipped() -> None:
    """The compounding failure: refusing to add meant no price for that part,
    and the cart stayed dirty, so every part after it failed the same way."""
    from pathlib import Path

    source = Path("collect_parts.py").read_text(encoding="utf-8")

    marker = source.index('cart_not_empty_before_add: {items_in_cart}')
    following = source[marker : marker + 700]

    # It must go to the cart page, because that is the only place the removal
    # controls exist, and then return to the product page to carry on.
    assert "MOTOSPORT_CART_URL" in following
    assert "clear_whole_cart(page)" in following
    assert "page.goto(product_url" in following


def test_a_failed_line_removal_falls_back_to_clearing_the_cart() -> None:
    from pathlib import Path

    source = Path("collect_parts.py").read_text(encoding="utf-8")

    assert 'return "success_after_full_clear"' in source
    assert "cart_cleared_by_emptying_whole_cart" in source


def test_a_cart_with_no_remove_text_is_still_emptied() -> None:
    """The real failure. MotoSport's cart removes a line with a trash-can icon
    and shows no "Remove" text anywhere, so an implementation keyed on that word
    found nothing and deleted nothing while reporting success as
    no_remove_control_found."""
    page = _CartPage(4)

    result = clear_whole_cart(page)

    assert result["cleared"] is True
    assert result["removed"] == 4
    assert page.items == 0


def test_a_failure_says_what_it_actually_saw() -> None:
    """Without this the only signal was a bare reason code, which is why the
    cause took a screenshot to identify."""
    page = _CartPage(3, removals_work=False)

    result = clear_whole_cart(page)

    assert "unchanged" in result["detail"]
    # It should be clear it waited rather than checking once and giving up.
    assert "after waiting" in result["detail"]


def test_removing_one_line_refuses_when_several_controls_are_present() -> None:
    """Clicking the first of several controls could remove the wrong item, which
    is worse than removing nothing. Broadening control detection must not lose
    that guard."""
    from probe_cart_price import safe_remove_fallback_selectors

    page = _CartPage(3)
    evidence = {"confirmed": True, "raw_cart_line_text": "Kawasaki OEM Parts Save For Later"}

    assert safe_remove_fallback_selectors(page, line_evidence=evidence) == []


class _SlowCartPage(_CartPage):
    """A cart whose re-render lags behind the click.

    Removing a line re-renders the cart, and how long that takes varies with its
    size and the network. This is the case that broke clearing: a few items were
    removed, then one re-render was slower than the fixed pause and clearing
    concluded the click had done nothing.
    """

    def __init__(self, items: int, *, lag_polls: int = 6) -> None:
        super().__init__(items)
        self.lag_polls = lag_polls
        self.pending = 0

    def locator(self, selector: str):
        page = self
        parent = super().locator(selector)

        class _Locator:
            def count(self) -> int:
                return parent.count()

            def inner_text(self, timeout: int | None = None) -> str:
                return parent.inner_text(timeout)

            @property
            def first(self):
                return self

            def click(self, timeout: int | None = None) -> None:
                page.clicks += 1
                page.pending = page.lag_polls

        return _Locator()

    def wait_for_timeout(self, milliseconds: int) -> None:
        if self.pending:
            self.pending -= 1
            if self.pending == 0:
                self.items -= 1


def test_a_slow_cart_re_render_is_waited_for_rather_than_called_a_failure() -> None:
    """The reported behaviour: the first few items were removed and then
    clearing stopped with items still in the cart."""
    page = _SlowCartPage(5)

    result = clear_whole_cart(page)

    assert result["cleared"] is True
    assert result["removed"] == 5
    assert page.items == 0


def test_waiting_does_not_mask_a_click_that_genuinely_does_nothing() -> None:
    """Waiting longer must not turn a real failure into an endless one."""
    page = _CartPage(3, removals_work=False)

    result = clear_whole_cart(page)

    assert result["cleared"] is False
    assert result["reason"] == "remove_had_no_effect"
    assert page.clicks == 1


def test_cart_based_competitors_use_a_durable_browser_profile() -> None:
    """MotoSport reads a price by adding the item to its cart, but needs no
    sign-in, so it was given a throwaway browser each run. The cart is real
    state that outlives a single part: it filled up during a run through the
    site's own cookies, and afterwards nothing could inspect or empty it,
    because that session no longer existed. An emptying tool opened the durable
    profile and correctly reported an empty cart while the collector's cart held
    ten items."""
    from app.competitors.registry import get_competitor

    for key in ("motosport", "chaparral"):
        adapter = get_competitor(key)
        assert getattr(adapter, "uses_cart_for_price", False) is True, key

    # A competitor that does not touch a cart should not be given a profile it
    # does not need.
    assert getattr(get_competitor("revzilla"), "uses_cart_for_price", False) is False


def test_the_collector_opens_a_profile_for_cart_competitors() -> None:
    from pathlib import Path

    source = Path("collect_parts.py").read_text(encoding="utf-8")

    assert 'getattr(get_competitor(competitor_key), "uses_cart_for_price", False)' in source
    # And reuses the profile's existing page rather than opening a second tab.
    assert "uses_profile = adapter_for_page.requires_login" in source


def test_the_sites_own_cart_count_is_preferred_over_inferring_rows() -> None:
    """MotoSport shows an item count on the cart icon. That is what the site
    itself believes is in the cart, so it is a better signal than inferring rows
    from the layout, which is what repeated fixes had been guessing at."""
    from probe_cart_price import cart_badge_count

    class _Page:
        def __init__(self, badge_text: str) -> None:
            self.badge_text = badge_text

        def evaluate(self, script: str):
            import re

            match = re.search(r"\b(\d{1,3})\b", self.badge_text)
            return int(match.group(1)) if match else -1

    assert cart_badge_count(_Page("3")) == 3
    assert cart_badge_count(_Page("Cart 10")) == 10
    assert cart_badge_count(_Page("Cart")) == -1


def test_a_page_with_no_badge_falls_back_to_counting_rows() -> None:
    """Not every cart shows a count, so the previous behaviour has to remain."""
    from probe_cart_price import count_cart_lines

    class _Page(_CartPage):
        def evaluate(self, script: str):
            return -1

    page = _Page(4)

    assert count_cart_lines(page) == 4


def test_cleanup_opens_the_cart_page_as_a_last_resort() -> None:
    """Removal controls only exist on the cart page. By the time cleanup runs
    the collector may be on a product page, where there is nothing to click, so
    the item stays and every later part inherits it."""
    from pathlib import Path

    source = Path("collect_parts.py").read_text(encoding="utf-8")

    assert "MOTOSPORT_CART_URL" in source
    assert 'return "success_after_visiting_cart"' in source
    assert "cart_cleared_after_opening_the_cart_page" in source


def test_the_quantity_stepper_is_not_mistaken_for_the_remove_control() -> None:
    """From the real cart markup: MotoSport has two controls whose accessible
    name contains "Remove".

        <button class="cart-line-item__stepper-btn--down" aria-label="Remove item">-</button>
        <a class="cart-remove-item ... cart-line-item__remove-fallback"
           title="Remove item from cart.">Remove</a>

    The stepper comes first in the document, so a generic aria-label match found
    it and clicked it. That only decrements the quantity, so lines stayed in the
    cart while their quantities changed.
    """
    from probe_cart_price import find_remove_controls

    class _RealCart:
        def locator(self, selector: str):
            class _Locator:
                def count(self) -> int:
                    if selector == "a.cart-remove-item":
                        return 4
                    if "stepper" in selector:
                        return 0
                    # The trap: the stepper also answers a bare aria-label match.
                    if selector == "button[aria-label*='Remove' i]":
                        return 4
                    return 0

            return _Locator()

    selector, count = find_remove_controls(_RealCart())

    assert selector == "a.cart-remove-item"
    assert count == 4


def test_the_genuine_remove_link_is_preferred_over_generic_matches() -> None:
    from probe_cart_price import REMOVE_CONTROL_SELECTORS

    generic = REMOVE_CONTROL_SELECTORS.index("button[aria-label*='Remove' i]:not([class*='stepper' i])")

    for specific in ("a.cart-remove-item", "a[class*='remove-fallback' i]", "a[href*='cartremoveqty' i]"):
        assert REMOVE_CONTROL_SELECTORS.index(specific) < generic, specific


def test_stepper_buttons_are_excluded_from_the_shape_fallback() -> None:
    """The fallback finds a control by shape, and a stepper is the same shape:
    an icon button inside a cart row beside the quantity box."""
    from probe_cart_price import FIND_REMOVE_BY_SHAPE

    assert "stepper" in FIND_REMOVE_BY_SHAPE
    assert "'-'" in FIND_REMOVE_BY_SHAPE


def test_a_dirty_cart_is_detected_from_the_header_badge() -> None:
    """The emptiness check ran on the product page, where cart lines are never
    shown, so it always answered "empty" and recovery never triggered. A failed
    cleanup therefore went unnoticed: items built up over a long run, while a
    short run rarely failed often enough to reveal it.

    The header badge carries the site's own count and is present on every page,
    including a product page, so it can actually detect this.
    """
    from pathlib import Path

    source = Path("collect_parts.py").read_text(encoding="utf-8")

    assert "items_in_cart = cart_badge_count(page)" in source
    # ensure_cart_empty is kept only as a fallback for a cart showing no badge.
    marker = source.index("items_in_cart = cart_badge_count(page)")
    assert "ensure_cart_empty(page)" in source[marker : marker + 300]


def test_a_screen_reader_only_control_can_still_be_clicked() -> None:
    """MotoSport's real removal control carries the class sr-only, so it is
    visually hidden. Playwright refuses to click an invisible element and waits
    until it times out, which is what happened: the right control was found and
    then reported unclickable with
    "click failed on a.cart-remove-item: TimeoutError".
    """
    from probe_cart_price import _click_possibly_hidden

    class _HiddenControlPage:
        def __init__(self, works_with: str) -> None:
            self.attempts = 0
            self.works_with = works_with

        def locator(self, selector: str):
            page = self

            class _Locator:
                @property
                def first(self):
                    return self

                def click(self, timeout: int | None = None, force: bool = False) -> None:
                    page.attempts += 1
                    if ("force" if force else "normal") != page.works_with:
                        raise TimeoutError("element is not visible")

            return _Locator()

        def evaluate(self, script: str, arg=None):
            page = self
            page.attempts += 1
            return page.works_with == "javascript"

    forced = _HiddenControlPage("force")
    assert _click_possibly_hidden(forced, "a.cart-remove-item") is True

    scripted = _HiddenControlPage("javascript")
    assert _click_possibly_hidden(scripted, "a.cart-remove-item") is True

    impossible = _HiddenControlPage("never")
    assert _click_possibly_hidden(impossible, "a.cart-remove-item") is False


def test_an_ordinary_click_is_preferred_before_forcing() -> None:
    """Forcing bypasses the checks that catch a control covered by something
    else, so it should only be reached when a normal click has failed."""
    from probe_cart_price import _click_possibly_hidden

    class _VisiblePage:
        def __init__(self) -> None:
            self.forced = False

        def locator(self, selector: str):
            page = self

            class _Locator:
                @property
                def first(self):
                    return self

                def click(self, timeout: int | None = None, force: bool = False) -> None:
                    if force:
                        page.forced = True

            return _Locator()

    page = _VisiblePage()
    assert _click_possibly_hidden(page, "a.cart-remove-item") is True
    assert page.forced is False


def test_a_hidden_mini_cart_saying_empty_does_not_stop_clearing() -> None:
    """The reported failure: clearing removed 10 items, declared the cart empty,
    and stopped with 9 still in it.

    MotoSport's markup includes a hidden mini-cart panel whose text contains
    "your cart is empty" regardless of what the cart actually holds. Matching on
    page text therefore reported success while items remained. The badge carries
    the site's own count and is what decides now.
    """
    from probe_cart_price import _cart_empty_text, clear_whole_cart

    misleading = "Your Cart | ELEMENT-AIR FILTER | Remove | your cart is empty | CHECKOUT"
    # The text really does claim empty, which is why it fooled the old check.
    assert _cart_empty_text(misleading) is True

    class _MiniCartTrap:
        def __init__(self, items: int) -> None:
            self.items = items
            self.clicks = 0

        def locator(self, selector: str):
            page = self

            class _Locator:
                def count(self) -> int:
                    if selector == "body":
                        return 1
                    if page.items <= 0:
                        return 0
                    return page.items if ("cart-remove-item" in selector or "cart-item" in selector) else 0

                @property
                def first(self):
                    return self

                def click(self, timeout: int | None = None, force: bool = False) -> None:
                    page.clicks += 1
                    page.items -= 1

                def inner_text(self, timeout: int | None = None) -> str:
                    return misleading

            return _Locator()

        def evaluate(self, script: str, arg=None):
            if "querySelector" in script and arg:
                self.clicks += 1
                self.items -= 1
                return True
            return self.items

        def wait_for_timeout(self, milliseconds: int) -> None:
            return None

    page = _MiniCartTrap(19)
    result = clear_whole_cart(page)

    assert result["cleared"] is True
    assert result["removed"] == 19
    assert page.items == 0


def test_emptiness_is_decided_by_the_badge_when_it_is_available() -> None:
    from probe_cart_price import cart_is_empty

    class _Page:
        def __init__(self, badge: int) -> None:
            self.badge = badge

        def evaluate(self, script: str, arg=None):
            return self.badge

        def locator(self, selector: str):
            class _Locator:
                def count(self) -> int:
                    return 1

                def inner_text(self, timeout: int | None = None) -> str:
                    # Claims empty regardless, as the real page does.
                    return "your cart is empty"

            return _Locator()

    assert cart_is_empty(_Page(0)) is True
    assert cart_is_empty(_Page(9)) is False
    assert cart_is_empty(_Page(19)) is False


def test_a_cart_control_showing_no_number_means_the_cart_is_empty() -> None:
    """The reported contradiction: clearing removed 8 items and the badge was
    seen reaching zero, yet the report said "left 1 line(s), unchanged" and then
    "95 lines remaining".

    An empty cart shows the cart control with no number at all. That was read as
    "unknown", so the caller fell back to counting rows, whose selectors match
    unrelated page elements: a genuinely empty cart came back as 95 lines.
    """
    from probe_cart_price import cart_badge_count, cart_is_empty

    class _Page:
        def __init__(self, badge_result: int) -> None:
            self.badge_result = badge_result

        def evaluate(self, script: str, arg=None):
            return self.badge_result

        def locator(self, selector: str):
            class _Locator:
                def count(self) -> int:
                    return 1

                def inner_text(self, timeout: int | None = None) -> str:
                    return "your cart is empty"

            return _Locator()

    # A control with a number: that many items.
    assert cart_badge_count(_Page(19)) == 19
    assert cart_is_empty(_Page(19)) is False

    # A control present but showing no number: empty, not unknown.
    assert cart_badge_count(_Page(0)) == 0
    assert cart_is_empty(_Page(0)) is True

    # No cart control found at all is genuinely unknown.
    assert cart_badge_count(_Page(-1)) == -1


def test_a_cart_clears_completely_at_several_sizes() -> None:
    """End to end against a cart that behaves as the real one does: a number
    while items remain, no number once empty."""
    from probe_cart_price import clear_whole_cart

    class _RealisticCart:
        def __init__(self, items: int) -> None:
            self.items = items
            self.clicks = 0

        def evaluate(self, script: str, arg=None):
            if "querySelector" in script and arg:
                if self.items > 0:
                    self.items -= 1
                    self.clicks += 1
                return True
            return self.items if self.items > 0 else 0

        def locator(self, selector: str):
            page = self

            class _Locator:
                def count(self) -> int:
                    if selector == "body":
                        return 1
                    if page.items <= 0:
                        return 0
                    return page.items if "cart-remove-item" in selector else 0

                @property
                def first(self):
                    return self

                def click(self, timeout: int | None = None, force: bool = False) -> None:
                    page.clicks += 1
                    page.items -= 1

                def inner_text(self, timeout: int | None = None) -> str:
                    return "Your Cart | Item | Remove | your cart is empty | CHECKOUT"

            return _Locator()

        def wait_for_timeout(self, milliseconds: int) -> None:
            return None

    for size in (1, 9, 19, 40):
        cart = _RealisticCart(size)
        result = clear_whole_cart(cart)
        assert result["cleared"] is True, size
        assert result["removed"] == size, size
        assert cart.items == 0, size


def test_removal_prefers_controls_outside_the_saved_items_section() -> None:
    """MotoSport's cart page also lists Saved Items, each with its own Remove
    link and the same part numbers and prices as a cart line. An unscoped match
    can remove a saved row while the cart line stays exactly where it was, which
    looks identical to removal silently failing.
    """
    from probe_cart_price import find_remove_controls

    class _PageWithSavedItems:
        def __init__(self) -> None:
            self.asked: list[str] = []

        def locator(self, selector: str):
            page = self
            page.asked.append(selector)

            class _Locator:
                def count(self) -> int:
                    # Only the scoped selector is treated as matching, standing
                    # in for a page where the cart row is outside Saved Items.
                    return 1 if "saved" in selector else 0

            return _Locator()

    selector, count = find_remove_controls(_PageWithSavedItems())

    assert count == 1
    assert "saved" in selector


def test_the_shape_fallback_skips_saved_items() -> None:
    from probe_cart_price import FIND_REMOVE_BY_SHAPE

    assert "saved" in FIND_REMOVE_BY_SHAPE
    assert "closest" in FIND_REMOVE_BY_SHAPE
