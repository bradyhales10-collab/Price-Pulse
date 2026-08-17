"""Emptying the MotoSport cart after reading a price.

Reading a MotoSport price means adding the item to the cart, so the cart has to
be emptied afterwards or items accumulate across a run. Worse, a cart that is
not empty made the collector skip adding anything at all, so a single failed
cleanup silently cost every later part its price.
"""

from __future__ import annotations

from probe_cart_price import clear_whole_cart


class _CartPage:
    """A cart holding a number of lines, each with a Remove control."""

    def __init__(self, items: int, *, removals_work: bool = True) -> None:
        self.items = items
        self.removals_work = removals_work
        self.clicks = 0

    def locator(self, selector: str):
        page = self

        class _Locator:
            def count(self) -> int:
                return 1

            def inner_text(self, timeout: int | None = None) -> str:
                if page.items <= 0:
                    return "Your cart is empty"
                return " ".join(["Item Remove"] * page.items)

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
                    return 1

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
