"""The comparison tool must not change anything, and must produce a like-for-
like view of both engines. If it quietly wrote prices, or mis-read a part, the
comparison would be worse than useless: it would look like evidence.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from decimal import Decimal
from pathlib import Path

from app.database import (
    connect_database,
    initialize_database,
    normalize_part_number,
    seed_chaparral,
    seed_motosport,
    seed_partzilla,
    seed_revzilla,
    utc_now,
)

PARTS = [
    ("DRIVE BELT", "163.99", "107.19", 8073, ["189.99", "192.50", "188.00", None]),
    ("PIN, DOWEL", "4.49", "2.00", 12, ["3.99", "4.10", None, None]),
    ("BATTERY GYZ20HA", "177.66", "100.00", 120, ["177.66", "190.35", None, None]),
]


def _build(database: Path) -> None:
    initialize_database(database)
    now = utc_now()
    with connect_database(database) as conn:
        competitor_ids = [seed_partzilla(conn), seed_motosport(conn), seed_chaparral(conn), seed_revzilla(conn)]
        listing_id = 0
        for index, (name, price, cost, qty, competitor_prices) in enumerate(PARTS, start=1):
            part_number = f"PN-{index:04d}"
            conn.execute(
                "INSERT INTO products(product_id, manufacturer, oem_part_number, normalized_part_number, "
                "product_name, is_active, created_at, updated_at) VALUES (?,?,?,?,?,1,?,?)",
                (index, "Polaris", part_number, normalize_part_number(part_number), name, now, now),
            )
            conn.execute(
                "INSERT INTO internal_product_state(product_id, internal_sku, our_current_price_cents, "
                "current_cost_cents, units_sold_12m, is_active, updated_at) VALUES (?,?,?,?,?,1,?)",
                (index, f"SKU{index}", int(float(price) * 100), int(float(cost) * 100), qty, now),
            )
            for competitor_id, competitor_price in zip(competitor_ids, competitor_prices, strict=False):
                listing_id += 1
                conn.execute(
                    "INSERT INTO competitor_listings(listing_id, product_id, competitor_id, "
                    "competitor_part_number, canonical_url, is_active, first_seen_at, last_seen_at, "
                    "created_at, updated_at) VALUES (?,?,?,?,?,1,?,?,?,?)",
                    (listing_id, index, competitor_id, part_number, "https://x", now, now, now, now),
                )
                if competitor_price is not None:
                    conn.execute(
                        "INSERT INTO current_listing_state(listing_id, selling_price_cents, price_display_type, "
                        "availability_status, first_observed_at, last_successful_check_at, last_changed_at, "
                        "updated_at) VALUES (?,?, 'regular','in_stock',?,?,?,?)",
                        (listing_id, int(float(competitor_price) * 100), now, now, now, now),
                    )


def test_the_comparison_changes_no_stored_prices() -> None:
    """The entire premise is that both engines can be judged without risk."""
    with tempfile.TemporaryDirectory() as tmp:
        database = Path(tmp) / "compare.db"
        _build(database)

        with connect_database(database) as conn:
            before = sorted(
                (row["listing_id"], row["selling_price_cents"])
                for row in conn.execute("SELECT listing_id, selling_price_cents FROM current_listing_state")
            )
            prices_before = sorted(
                (row["product_id"], row["our_current_price_cents"])
                for row in conn.execute("SELECT product_id, our_current_price_cents FROM internal_product_state")
            )

        result = subprocess.run(
            [sys.executable, "compare_pricing_engines.py", "--database", str(database)],
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert result.returncode == 0, result.stderr

        with connect_database(database) as conn:
            after = sorted(
                (row["listing_id"], row["selling_price_cents"])
                for row in conn.execute("SELECT listing_id, selling_price_cents FROM current_listing_state")
            )
            prices_after = sorted(
                (row["product_id"], row["our_current_price_cents"])
                for row in conn.execute("SELECT product_id, our_current_price_cents FROM internal_product_state")
            )

        assert before == after
        assert prices_before == prices_after
        assert "Nothing was changed" in result.stdout


def test_the_comparison_reports_both_engines_for_each_part() -> None:
    from app.comparison import ComparisonFilters, comparison_rows
    from app.pricing_rules import list_pricing_rules
    from compare_pricing_engines import evaluate_row

    with tempfile.TemporaryDirectory() as tmp:
        database = Path(tmp) / "compare.db"
        _build(database)
        rows = comparison_rows(database, ComparisonFilters())
        rules = list_pricing_rules(database)

        results = [result for row in rows if (result := evaluate_row(row, rules, Decimal("20")))]

    assert len(results) == len(PARTS)
    for result in results:
        assert result["category"]
        assert result["sensitivity"] in {"HIGH", "MEDIUM", "LOW"}
        assert result["new_action"]
        assert result["reason"]


def test_a_part_with_no_current_price_is_skipped_rather_than_guessed() -> None:
    from compare_pricing_engines import evaluate_row

    assert evaluate_row({"our_current_price": "", "product_name": "DRIVE BELT"}, [], Decimal("20")) is None
    assert evaluate_row({"our_current_price": "0", "product_name": "DRIVE BELT"}, [], Decimal("20")) is None


def test_an_out_of_stock_competitor_is_not_treated_as_the_market() -> None:
    """A price nobody can buy is not a price we compete with."""
    from compare_pricing_engines import _quotes_from_row

    quotes = _quotes_from_row(
        {
            "partzilla_selling_price": "100.00",
            "partzilla_availability_status": "out_of_stock",
            "motosport_selling_price": "120.00",
            "motosport_availability_status": "in_stock",
        }
    )
    by_name = {quote.name: quote for quote in quotes}

    assert by_name["Partzilla"].in_stock is False
    assert by_name["MotoSport"].in_stock is True
