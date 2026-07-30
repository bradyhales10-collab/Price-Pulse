from __future__ import annotations

import re
import sys
from pathlib import Path

from fastapi.testclient import TestClient

from app.web.app import create_app

sys.path.insert(0, str(Path(__file__).parent))

from test_dashboard import _dashboard_db  # noqa: E402
from test_import_comparison_collection import _comparison_db  # noqa: E402


def _client(db: Path) -> TestClient:
    return TestClient(create_app(db), raise_server_exceptions=False)


def test_review_step_is_hidden_until_there_is_something_to_review() -> None:
    """Showing the review step before any results exist made the page look busy
    and contradicted the numbered steps at the top."""
    page = _client(_dashboard_db("flow_hidden.db")).get("/imports").text

    assert "Review Results" not in page
    assert "Show All Prices" not in page
    # The earlier steps must still be there.
    assert "Upload Parts File" in page


def test_review_step_appears_once_results_exist() -> None:
    page = _client(_comparison_db("flow_shown.db")).get("/imports").text

    assert "Review Results" in page
    assert "data-comparison-table" in page


def test_status_message_still_shows_when_the_review_step_is_hidden() -> None:
    """Login/refresh redirects land on /imports with a message. Hiding the
    review step must not swallow it."""
    page = _client(_dashboard_db("flow_message.db")).get("/imports?message=Partzilla+login+will+open").text

    assert "Partzilla login will open" in page


def test_dedicated_comparison_page_always_shows_the_review_section() -> None:
    """Only the Price Check page gates the section; /comparison is the review
    page itself and must always render it."""
    page = _client(_dashboard_db("flow_comparison.db")).get("/comparison").text

    assert "Review Results" in page


def test_recent_files_is_collapsed_by_default() -> None:
    page = _client(_comparison_db("flow_recent.db")).get("/imports").text
    elements = re.findall(r'<details id="recent-files"[^>]*>', page)

    assert elements, "recent files block should render when upload history exists"
    assert " open" not in elements[0]


def test_save_selected_button_is_not_duplicated() -> None:
    page = _client(_comparison_db("flow_save.db")).get("/imports").text

    assert page.count("Save Selected") == 1
    assert "Export Selected Results" in page


def test_competitor_picker_comes_before_the_readiness_checklist() -> None:
    """Choosing competitors is the real decision in this step, so it should not
    sit below the reassurance checklist."""
    source = Path("app/web/templates/imports.html").read_text(encoding="utf-8")

    assert source.index('id="price-check-form"') < source.index('class="readiness-list"')


def test_step_two_has_only_one_preview_collapsible() -> None:
    """Column mapping was folded into the row preview to cut down the number of
    expandable panels in this step."""
    source = Path("app/web/templates/imports.html").read_text(encoding="utf-8")

    assert source.count('class="preview-details"') == 1
    assert "Preview uploaded rows and recognized columns" in source
    assert "Show recognized columns" not in source
