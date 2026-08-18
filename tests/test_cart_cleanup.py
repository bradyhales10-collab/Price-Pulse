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
                    # A real cart page holding items always shows an order
                    # summary with a subtotal, which is what distinguishes it
                    # from an empty one when no removal control can be found.
                    return "Your Cart Some Item In Cart Order Summary Subtotal $42.00"

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
                    # The exact href match is now tried before the class.
                    if "saveforlater=0" in selector:
                        return 4
                    if "stepper" in selector:
                        return 0
                    # The trap: the stepper also answers a bare aria-label match.
                    if selector == "button[aria-label*='Remove' i]":
                        return 4
                    return 0

            return _Locator()

    selector, count = find_remove_controls(_RealCart())

    assert "saveforlater=0" in selector
    assert count == 4


def test_the_genuine_remove_link_is_preferred_over_generic_matches() -> None:
    from probe_cart_price import REMOVE_CONTROL_SELECTORS

    generic = REMOVE_CONTROL_SELECTORS.index("button[aria-label*='Remove' i]:not([class*='stepper' i])")

    for specific in ("a[href*='saveforlater=0']", "a.cart-remove-item[href*='saveforlater=0']"):
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


def test_a_lagging_badge_does_not_make_a_full_cart_look_empty() -> None:
    """The badge takes a few seconds to update. Reading "control present but no
    number" as zero items meant that right after a page load, before the badge
    populated, a full cart looked empty: clearing declared success and stopped
    without removing anything.

    Emptiness is decided by whether a removal control exists, which appears with
    the DOM rather than seconds later. If there is a control, there is something
    to remove, whatever the badge currently says.
    """
    from probe_cart_price import cart_is_empty

    class _Page:
        def __init__(self, controls: int, badge: int, text: str) -> None:
            self.controls = controls
            self.badge = badge
            self.text = text

        def locator(self, selector: str):
            page = self

            class _Locator:
                def count(self) -> int:
                    if selector == "body":
                        return 1
                    return page.controls if "cart-remove-item" in selector else 0

                def inner_text(self, timeout: int | None = None) -> str:
                    return page.text

            return _Locator()

        def evaluate(self, script: str, arg=None):
            return self.badge

    full_but_badge_not_loaded = _Page(4, 0, "Your Cart Order Summary Subtotal $1593.19")
    assert cart_is_empty(full_but_badge_not_loaded) is False

    genuinely_empty = _Page(0, 0, "Your Cart is Empty Continue Shopping")
    assert cart_is_empty(genuinely_empty) is True

    # And the hidden mini-cart text still must not fool it.
    mini_cart_claims_empty = _Page(4, 4, "Your Cart Item Remove your cart is empty Subtotal")
    assert cart_is_empty(mini_cart_claims_empty) is False


def test_a_removal_is_confirmed_even_while_the_badge_trails() -> None:
    """Comparing badge counts read a successful removal as unchanged for several
    seconds, which stopped clearing partway."""
    from probe_cart_price import clear_whole_cart

    class _LaggingBadgeCart:
        def __init__(self, items: int) -> None:
            self.items = items
            self.badge = items
            self.lag = 0
            self.clicks = 0

        def evaluate(self, script: str, arg=None):
            if "querySelector" in script and arg:
                self.items -= 1
                self.clicks += 1
                self.lag = 5
                return True
            return self.badge

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
                    page.lag = 5

                def inner_text(self, timeout: int | None = None) -> str:
                    return "Your Cart is Empty" if page.items <= 0 else "Your Cart Subtotal $100 Remove"

            return _Locator()

        def wait_for_timeout(self, milliseconds: int) -> None:
            if self.lag:
                self.lag -= 1
                if self.lag == 0:
                    self.badge = self.items

    for size in (4, 19):
        cart = _LaggingBadgeCart(size)
        result = clear_whole_cart(cart)
        assert result["cleared"] is True, size
        assert cart.items == 0, size


def test_a_stale_control_after_the_last_removal_is_not_reported_as_failure() -> None:
    """A real run removed all five items and then reported failure on the last
    one, with the reload immediately afterwards confirming the cart was empty:

        removed: 5, reason: remove_had_no_effect
        Cart lines remaining after reload: 0
        The cart is empty.

    The page kept showing one stale control after the final removal. Reloading
    and looking again distinguishes that from a removal that genuinely did
    nothing.
    """
    from probe_cart_price import clear_whole_cart

    class _StaleLastControl:
        def __init__(self, items: int) -> None:
            self.items = items
            self.stale = False
            self.clicks = 0
            self.reloads = 0

        def evaluate(self, script: str, arg=None):
            if "querySelector" in script and arg:
                self.clicks += 1
                if self.items > 0:
                    self.items -= 1
                if self.items == 0:
                    self.stale = True
                return True
            return self.items

        def locator(self, selector: str):
            page = self

            class _Locator:
                def count(self) -> int:
                    if selector == "body":
                        return 1
                    visible = page.items + (1 if page.stale else 0)
                    if visible <= 0:
                        return 0
                    return visible if "cart-remove-item" in selector else 0

                @property
                def first(self):
                    return self

                def click(self, timeout: int | None = None, force: bool = False) -> None:
                    page.clicks += 1
                    if page.items > 0:
                        page.items -= 1
                    if page.items == 0:
                        page.stale = True

                def inner_text(self, timeout: int | None = None) -> str:
                    if page.items <= 0 and not page.stale:
                        return "Your Cart is Empty"
                    return "Your Cart Subtotal $100 Remove"

            return _Locator()

        def reload(self, **kwargs) -> None:
            self.reloads += 1
            self.stale = False

        def wait_for_timeout(self, milliseconds: int) -> None:
            return None

    cart = _StaleLastControl(5)
    result = clear_whole_cart(cart)

    assert result["cleared"] is True
    assert cart.items == 0
    assert cart.reloads >= 1


def test_a_removal_that_truly_does_nothing_is_still_reported() -> None:
    """Reloading must not turn a real failure into a false success."""
    from probe_cart_price import clear_whole_cart

    class _NeverRemoves:
        def __init__(self) -> None:
            self.reloads = 0

        def evaluate(self, script: str, arg=None):
            if "querySelector" in script and arg:
                return True
            return 3

        def locator(self, selector: str):
            class _Locator:
                def count(self) -> int:
                    if selector == "body":
                        return 1
                    return 3 if "cart-remove-item" in selector else 0

                @property
                def first(self):
                    return self

                def click(self, timeout: int | None = None, force: bool = False) -> None:
                    return None

                def inner_text(self, timeout: int | None = None) -> str:
                    return "Your Cart Subtotal $100 Remove"

            return _Locator()

        def reload(self, **kwargs) -> None:
            self.reloads += 1

        def wait_for_timeout(self, milliseconds: int) -> None:
            return None

    cart = _NeverRemoves()
    result = clear_whole_cart(cart)

    assert result["cleared"] is False
    assert result["reason"] == "remove_had_no_effect"
    # Three controls remain, so it reports without paying for a reload.
    assert "unchanged" in result["detail"]
    assert cart.reloads == 0


def test_the_working_selector_is_remembered_rather_than_reprobed() -> None:
    """Probing 22 selectors in two forms each costs 44 queries to the browser,
    and that probe sat inside a loop polling 25 times per removal: roughly 1,100
    queries to remove one item, against one text read and one click before any of
    this. That is why checking became extremely slow."""
    import probe_cart_price

    class _CountingPage:
        def __init__(self) -> None:
            self.queries = 0

        def locator(self, selector: str):
            self.queries += 1

            class _Locator:
                def count(self) -> int:
                    return 3 if "cart-remove-item" in selector else 0

            return _Locator()

    probe_cart_price._KNOWN_REMOVE_SELECTOR = ""
    first = _CountingPage()
    probe_cart_price.find_remove_controls(first)

    second = _CountingPage()
    probe_cart_price.find_remove_controls(second)

    # Once known, finding the control costs a single query.
    assert second.queries == 1


def test_counting_controls_during_a_poll_costs_one_query() -> None:
    import probe_cart_price

    class _CountingPage:
        def __init__(self) -> None:
            self.queries = 0

        def locator(self, selector: str):
            self.queries += 1

            class _Locator:
                def count(self) -> int:
                    return 2

            return _Locator()

    probe_cart_price._KNOWN_REMOVE_SELECTOR = "a.cart-remove-item"
    page = _CountingPage()

    assert probe_cart_price._count_known_controls(page) == 2
    assert page.queries == 1


def test_a_link_that_saves_for_later_is_never_used_to_remove() -> None:
    """The cause of the Saved Items list. MotoSport distinguishes the two actions
    in the link itself:

        ...&cartremoveqty=1&saveforlater=0   removes the item
        ...&cartremoveqty=1&saveforlater=1   moves it to Saved Items

    Both carry the class cart-remove-item, so matching on the class alone could
    save an item instead of removing it. Every part checked would then leave one
    behind, which is how the list grew.
    """
    from probe_cart_price import REMOVE_CONTROL_SELECTORS

    # The exact removing form is tried first.
    assert REMOVE_CONTROL_SELECTORS[0] == "a[href*='saveforlater=0']"

    # And no selector may match a saving link.
    class _OnlySavingLinks:
        def locator(self, selector: str):
            class _Locator:
                def count(self) -> int:
                    # Stands in for a page whose only cart-remove-item links
                    # save rather than remove.
                    if "saveforlater=0" in selector:
                        return 0
                    if "not([href*='saveforlater=1'])" in selector:
                        return 0
                    return 3 if "cart-remove-item" in selector else 0

            return _Locator()

    import probe_cart_price
    from probe_cart_price import find_remove_controls

    probe_cart_price._KNOWN_REMOVE_SELECTOR = ""
    selector, _ = find_remove_controls(_OnlySavingLinks())

    # It may fall through to a generic match, but never to one that would save.
    assert "saveforlater=1" not in selector


def test_saved_items_can_be_cleared() -> None:
    """Saved Items do not affect a price reading, but they render on the cart
    page, so a long list slows every cart page load."""
    from probe_cart_price import clear_saved_items

    class _SavedItemsPage:
        def __init__(self, count: int) -> None:
            self.count_remaining = count
            self.clicks = 0

        def locator(self, selector: str):
            page = self

            class _Locator:
                def count(self) -> int:
                    return page.count_remaining if "saveforlater=1" in selector else 0

                @property
                def first(self):
                    return self

                def click(self, timeout: int | None = None, force: bool = False) -> None:
                    page.clicks += 1
                    page.count_remaining -= 1

            return _Locator()

        def evaluate(self, script: str, arg=None):
            return True

        def reload(self, **kwargs) -> None:
            return None

        def wait_for_timeout(self, milliseconds: int) -> None:
            return None

    for size in (0, 5, 40):
        page = _SavedItemsPage(size)
        result = clear_saved_items(page)
        assert result["cleared"] is True, size
        assert result["removed"] == size, size
        assert page.count_remaining == 0, size
