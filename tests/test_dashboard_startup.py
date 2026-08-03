from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from app.database import SCHEMA_SQL, connect_database, initialize_database, utc_now
from app.web.app import create_app

TEST_OUTPUT_DIR = Path("data/output/test-artifacts")


def test_dashboard_startup_migrates_existing_schema_v1_database() -> None:
    TEST_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    db = TEST_OUTPUT_DIR / "dashboard_startup_v1.db"
    if db.exists():
        db.unlink()
    with connect_database(db) as conn:
        conn.executescript(SCHEMA_SQL)
        conn.execute(
            "INSERT INTO schema_migrations(version, description, applied_at) VALUES (1, 'initial pricing monitor schema', ?)",
            (utc_now(),),
        )

    initialize_database(db)
    response = TestClient(create_app(db), raise_server_exceptions=False).get("/")

    assert response.status_code == 200
    with connect_database(db) as conn:
        version = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
    assert version == 10
