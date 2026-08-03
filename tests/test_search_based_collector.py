from __future__ import annotations

import re
import tempfile
from pathlib import Path

import collect_parts
from app.competitors.registry import get_competitor
from app.config import ProbeSettings
from app.database import (
    connect_database,
    initialize_database,
    normalize_part_number,
    seed_revzilla,
    utc_now,
)
from app.resolution_cache import (
    cache_stats,
    cached_product_url,
    invalidate_product_url,
    save_product_url,
)

PART = "09168-08016"
PRODUCT_PATH = "/oem/suzuki/suzuki-09168-08016-washer"
SEARCH_HTML = f'<html><body><a href="{PRODUCT_PATH}">result</a></body></html>'
PRODUCT_HTML = (
    '<html><head><meta name="sailthru.price" content="205">'
    '<meta name="sailthru.inventory" content="1000"></head><body>'
    f"<p>Suzuki WASHER {PART}</p><p>OEM Part Number: {PART}</p></body></html>"
)
WRONG_PRODUCT_HTML = (
    '<html><head><meta name="sailthru.price" content="999">'
    '<meta name="sailthru.inventory" content="1000"></head><body>'
    "<p>OEM Part Number: 99999-9999</p></body></html>"
)


class FakePage:
    """Records navigations so request counts can be asserted."""

    def __init__(self, product_html: str = PRODUCT_HTML) -> None:
        self.visits: list[str] = []
        self._url = ""
        self._product_html = product_html

    def goto(self, url, **kwargs):
        self.visits.append(url)
        self._url = url
        return type("Response", (), {"status": 200})()

    @property
    def url(self) -> str:
        return self._url

    def content(self) -> str:
        if "old-url" in self._url:
            # A stale cached URL now shows a different part.
            return WRONG_PRODUCT_HTML
        return self._product_html if "/oem/" in self._url else SEARCH_HTML

    def wait_for_timeout(self, milliseconds) -> None:
        return None

    def locator(self, selector):
        page = self

        class Locator:
            def count(self):
                return 1

            def inner_text(self, timeout=None):
                return re.sub(r"<[^>]+>", "\n", page.content())

        return Locator()


def _database(tmp: str) -> tuple[Path, object]:
    database = Path(tmp) / "collector.db"
    initialize_database(database)
    now = utc_now()
    with connect_database(database) as conn:
        competitor_id = seed_revzilla(conn)
        conn.execute(
            """
            INSERT INTO products(product_id, manufacturer, oem_part_number, normalized_part_number,
                product_name, is_active, created_at, updated_at)
            VALUES (1, 'Suzuki', ?, ?, 'Washer', 1, ?, ?)
            """,
            (PART, normalize_part_number(PART), now, now),
        )
        conn.execute(
            """
            INSERT INTO competitor_listings(listing_id, product_id, competitor_id, competitor_part_number,
                canonical_url, is_active, first_seen_at, last_seen_at, created_at, updated_at)
            VALUES (1, 1, ?, ?, 'https://www.revzilla.com/search', 1, ?, ?, ?, ?)
            """,
            (competitor_id, PART, now, now, now, now),
        )
        conn.execute(
            """
            INSERT INTO scan_runs(scan_run_id, competitor_id, started_at, requested_part_count, run_status)
            VALUES (1, ?, ?, 1, 'running')
            """,
            (competitor_id, now),
        )
    planned = type(
        "Planned",
        (),
        {
            "run_order": 1,
            "manufacturer": "Suzuki",
            "oem_part_number": PART,
            "product_id": 1,
            "listing_id": 1,
            "current_price_cents": None,
        },
    )()
    return database, planned


def _collect(database, page, planned):
    return collect_parts.collect_one_search_based_part(
        database,
        page,
        planned,
        1,
        ProbeSettings(timeout=5000, render_settle_ms=0),
        adapter=get_competitor("revzilla"),
        delay_seconds=0,
    )


def test_first_lookup_searches_then_opens_the_product_page() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        database, planned = _database(tmp)
        page = FakePage()

        row = _collect(database, page, planned)

        assert len(page.visits) == 2, page.visits
        assert "/search" in page.visits[0]
        assert PRODUCT_PATH in page.visits[1]
        assert row.selling_price == "2.05"
        assert row.price_source_category == "revzilla_search"


def test_second_lookup_uses_the_cached_url_and_halves_the_requests() -> None:
    """This is what makes a run of thousands affordable: one request per part
    instead of two, once the URL is known."""
    with tempfile.TemporaryDirectory() as tmp:
        database, planned = _database(tmp)

        _collect(database, FakePage(), planned)
        warm = FakePage()
        row = _collect(database, warm, planned)

        assert len(warm.visits) == 1, warm.visits
        assert PRODUCT_PATH in warm.visits[0]
        assert row.selling_price == "2.05"
        assert row.price_source_category == "revzilla_cached_url"


def test_a_cached_url_showing_the_wrong_part_is_dropped_and_researched() -> None:
    """Competitors reorganise their catalogues, so a stale URL must not be
    trusted forever or silently priced as the wrong part."""
    with tempfile.TemporaryDirectory() as tmp:
        database, planned = _database(tmp)
        save_product_url(
            database,
            "revzilla",
            "Suzuki",
            PART,
            "https://www.revzilla.com/oem/suzuki/suzuki-old-url",
        )

        page = FakePage(product_html=PRODUCT_HTML)
        row = _collect(database, page, planned)

        # Cached URL tried first, found not to match, then a fresh search.
        assert len(page.visits) == 3, page.visits
        assert "suzuki-old-url" in page.visits[0]
        assert "/search" in page.visits[1]
        assert row.selling_price == "2.05"
        # The cache now points at the correct page.
        assert PRODUCT_PATH in (cached_product_url(database, "revzilla", "Suzuki", PART) or "")


def test_a_page_for_the_wrong_part_is_never_cached() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        database, planned = _database(tmp)

        row = _collect(database, FakePage(product_html=WRONG_PRODUCT_HTML), planned)

        assert row.selling_price is None
        assert cached_product_url(database, "revzilla", "Suzuki", PART) is None


def test_cache_is_keyed_per_competitor() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        database, _ = _database(tmp)
        save_product_url(database, "revzilla", "Suzuki", PART, "https://www.revzilla.com/a")

        assert cached_product_url(database, "revzilla", "Suzuki", PART) is not None
        assert cached_product_url(database, "chaparral", "Suzuki", PART) is None


def test_cache_key_ignores_punctuation_and_case() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        database, _ = _database(tmp)
        save_product_url(database, "revzilla", "Suzuki", PART, "https://www.revzilla.com/a")

        assert cached_product_url(database, "revzilla", "suzuki", "0916808016") is not None


def test_invalidating_a_url_forces_a_fresh_search() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        database, planned = _database(tmp)
        _collect(database, FakePage(), planned)
        assert cache_stats(database, "revzilla")["valid"] == 1

        invalidate_product_url(database, "revzilla", "Suzuki", PART)
        assert cache_stats(database, "revzilla")["valid"] == 0

        page = FakePage()
        _collect(database, page, planned)
        assert len(page.visits) == 2, page.visits


def test_unsupported_manufacturer_costs_no_requests() -> None:
    """RevZilla carries no Polaris, so a Polaris part must not be looked up."""
    with tempfile.TemporaryDirectory() as tmp:
        database, planned = _database(tmp)
        planned.manufacturer = "Polaris"
        page = FakePage()

        row = _collect(database, page, planned)

        assert page.visits == []
        assert row.result_type == "manufacturer_not_carried"


def test_revzilla_now_has_a_production_collector() -> None:
    collect_parts.assert_production_collector_exists("revzilla")
    assert "revzilla" in collect_parts.PRODUCTION_COLLECTORS
