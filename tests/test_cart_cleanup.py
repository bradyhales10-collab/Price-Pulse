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

    marker = source.index('observation.warnings.append("cart_not_empty_before_add")')
    following = source[marker : marker + 500]

    assert "clear_whole_cart(page)" in following


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
