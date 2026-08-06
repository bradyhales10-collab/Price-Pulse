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


def test_an_unexpected_parser_exception_is_recorded_not_left_unhandled() -> None:
    """A page structure the parser has never seen should produce a clear
    recorded row, not an unhandled exception that counts toward the
    two-consecutive-errors stop after just two occurrences."""
    with tempfile.TemporaryDirectory() as tmp:
        database, planned = _database(tmp)

        class BrokenAdapter:
            competitor_key = "revzilla"
            display_name = "RevZilla"
            supported_manufacturers = ("Suzuki",)

            def build_product_url(self, record):
                return "https://www.revzilla.com/search"

            def search_result_product_url(self, html, record):
                return f"https://www.revzilla.com{PRODUCT_PATH}"

            def parse_product_page(self, *args, **kwargs):
                raise KeyError("sku")

        row = collect_parts.collect_one_search_based_part(
            database,
            FakePage(),
            planned,
            1,
            ProbeSettings(timeout=5000, render_settle_ms=0),
            adapter=BrokenAdapter(),
            delay_seconds=0,
        )

        assert row.result_type != "error"
        assert "sku" in (row.status_reason or "")
        assert row.selling_price is None


def test_nothing_is_defined_after_the_script_entry_point() -> None:
    """The bug that broke RevZilla for days.

    collect_parts.py ends with `if __name__ == "__main__": sys.exit(main())`.
    Anything defined below that line does not exist yet when the file is run
    as a script, because main() executes at that point. Three functions,
    including collect_one_search_based_part, had been appended after it.

    Importing the module hid this completely: on import the __main__ block is
    skipped, execution reaches the end, and every function is defined. So the
    module looked healthy in every check that imported it, while every real
    price check, which runs the file as a script, raised
    NameError: name 'collect_one_search_based_part' is not defined.

    This check is static for that reason: it does not care whether the module
    imports cleanly, only where things sit relative to the entry point.
    """
    import ast
    from pathlib import Path

    for filename in (
        "collect_parts.py",
        "probe_competitor.py",
        "local_collector.py",
        "local_collector_agent.py",
        "dashboard.py",
        "diagnose_part_pulse.py",
        "diagnose_revzilla.py",
        "export_probe_input.py",
        "clear_stuck_jobs.py",
        "show_last_error.py",
    ):
        path = Path(filename)
        if not path.exists():
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))

        entry_line = None
        for node in tree.body:
            if isinstance(node, ast.If):
                test = node.test
                if (
                    isinstance(test, ast.Compare)
                    and isinstance(test.left, ast.Name)
                    and test.left.id == "__name__"
                ):
                    entry_line = node.lineno
        if entry_line is None:
            continue

        stranded = [
            node.name
            for node in tree.body
            if isinstance(node, ast.FunctionDef | ast.ClassDef) and node.lineno > entry_line
        ]
        assert not stranded, (
            f"{filename}: {stranded} are defined after the "
            f'`if __name__ == "__main__"` block on line {entry_line}. '
            "When the file runs as a script they will not exist yet, and any code "
            "reached from main() that calls them raises NameError."
        )


def test_zero_parts_attempted_is_never_reported_as_success() -> None:
    """Partzilla reported "completed" having attempted 0 of 100 parts, so a run
    that fell over during browser setup looked like a clean finish: no error, no
    message, and nothing to investigate. A later, real run reported "completed
    with warnings" at 95 of 994 parts, showing the same problem applied to any
    partial run, not only a run that attempted nothing at all: reaching this
    function with result.run_status still "running" means the loop never
    recorded why it stopped, regardless of how far it got."""
    from collect_parts import _normalized_completed_run_status

    assert _normalized_completed_run_status("completed", completed=0, total=100) == "failed"
    assert _normalized_completed_run_status("completed_with_warnings", completed=95, total=994) == "failed"
    # A genuinely full run is untouched.
    assert _normalized_completed_run_status("completed", completed=100, total=100) == "completed"
    assert _normalized_completed_run_status("completed_with_warnings", completed=994, total=994) == "completed_with_warnings"
    # Nothing planned at all is not a failure.
    assert _normalized_completed_run_status("completed", completed=0, total=0) == "completed"
    # A real stop reason is preserved rather than overwritten.
    assert _normalized_completed_run_status("stopped_blocked", completed=0, total=100) == "stopped_blocked"
    assert _normalized_completed_run_status("stopped_blocked", completed=50, total=994) == "stopped_blocked"


def test_an_unusable_saved_sign_in_is_recorded_rather_than_silent() -> None:
    from pathlib import Path

    source = Path("collect_parts.py").read_text(encoding="utf-8")

    assert "saved_sign_in_unusable" in source
    assert "collection_crashes" in source
