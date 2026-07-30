from __future__ import annotations

from app.web.queries import _pivot_price_history


def _entry(competitor: str, stamp: str, price: str, percent: str = "") -> dict[str, object]:
    return {
        "competitor_name": competitor,
        "effective_at": stamp,
        "new_price": price,
        "previous_price": None,
        "change_type": "first_observation",
        "percent_change": percent,
    }


def test_one_price_check_becomes_one_row_with_a_column_per_competitor() -> None:
    """Competitors are scanned in separate runs a minute or two apart. They
    still belong to the same price check and must share a single row."""
    rows = [
        _entry("Partzilla", "2026-07-30T15:48:00Z", "1.49"),
        _entry("Chaparral Motorsports", "2026-07-30T15:47:00Z", "1.99"),
    ]

    competitors, grid = _pivot_price_history(rows)

    assert competitors == ["Chaparral Motorsports", "Partzilla"]
    assert len(grid) == 1
    assert grid[0]["prices"]["Partzilla"]["price"] == "1.49"
    assert grid[0]["prices"]["Chaparral Motorsports"]["price"] == "1.99"


def test_separate_price_checks_stay_on_separate_rows_newest_first() -> None:
    rows = [
        _entry("Partzilla", "2026-08-02T09:00:00Z", "2.49"),
        _entry("Chaparral Motorsports", "2026-08-02T09:01:00Z", "2.19"),
        _entry("Partzilla", "2026-07-30T15:48:00Z", "1.49"),
        _entry("Chaparral Motorsports", "2026-07-30T15:47:00Z", "1.99"),
    ]

    _, grid = _pivot_price_history(rows)

    assert len(grid) == 2
    assert grid[0]["prices"]["Partzilla"]["price"] == "2.49"
    assert grid[1]["prices"]["Partzilla"]["price"] == "1.49"


def test_repeat_of_same_competitor_always_starts_a_new_row() -> None:
    """Two observations for one competitor must never collapse together, even
    if they happen close in time."""
    rows = [
        _entry("Partzilla", "2026-08-02T09:05:00Z", "2.49"),
        _entry("Partzilla", "2026-08-02T09:00:00Z", "1.49"),
    ]

    _, grid = _pivot_price_history(rows)

    assert len(grid) == 2


def test_competitor_missing_from_a_check_leaves_a_blank_cell() -> None:
    rows = [
        _entry("Partzilla", "2026-08-05T10:00:00Z", "3.00"),
        _entry("Partzilla", "2026-07-30T15:48:00Z", "1.49"),
        _entry("Chaparral Motorsports", "2026-07-30T15:47:00Z", "1.99"),
    ]

    competitors, grid = _pivot_price_history(rows)

    assert competitors == ["Chaparral Motorsports", "Partzilla"]
    assert grid[0]["prices"].get("Chaparral Motorsports") is None
    assert grid[1]["prices"]["Chaparral Motorsports"]["price"] == "1.99"


def test_unparsable_timestamps_do_not_crash_the_grid() -> None:
    rows = [
        _entry("Partzilla", "not-a-timestamp", "1.49"),
        _entry("Chaparral Motorsports", "", "1.99"),
    ]

    competitors, grid = _pivot_price_history(rows)

    assert competitors == ["Chaparral Motorsports", "Partzilla"]
    assert grid


def test_empty_history_produces_no_columns_or_rows() -> None:
    competitors, grid = _pivot_price_history([])

    assert competitors == []
    assert grid == []
