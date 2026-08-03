"""Product URL cache for competitors that have to search for a part.

Some competitors cannot build a product URL from a part number, so every lookup
costs two requests: a search, then the product page. Remembering the resolved
URL turns later runs back into a single request, which matters most on large
runs where request volume is what risks getting an address blocked.

The cache is keyed by competitor, so one table serves every search-based
competitor rather than each one growing its own.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from app.database import connect_database, utc_now
from app.manufacturer_registry import normalize_manufacturer


def normalize_part_key(value: str) -> str:
    return "".join(char for char in (value or "").upper() if char.isalnum())


def cached_product_url(
    database_path: Path,
    competitor_key: str,
    manufacturer: str,
    part_number: str,
) -> str | None:
    with connect_database(database_path) as conn:
        row = conn.execute(
            """
            SELECT resolved_url
            FROM competitor_resolution_cache
            WHERE competitor_key=? AND manufacturer=? AND normalized_part_number=? AND is_valid=1
            """,
            (competitor_key, normalize_manufacturer(manufacturer), normalize_part_key(part_number)),
        ).fetchone()
    return str(row["resolved_url"]) if row else None


def save_product_url(
    database_path: Path,
    competitor_key: str,
    manufacturer: str,
    part_number: str,
    resolved_url: str,
    product_identifier: str | None = None,
) -> None:
    now = utc_now()
    with connect_database(database_path) as conn:
        _save(conn, competitor_key, manufacturer, part_number, resolved_url, product_identifier, now)


def _save(
    conn: sqlite3.Connection,
    competitor_key: str,
    manufacturer: str,
    part_number: str,
    resolved_url: str,
    product_identifier: str | None,
    now: str,
) -> None:
    conn.execute(
        """
        INSERT INTO competitor_resolution_cache(competitor_key, manufacturer, part_number,
            normalized_part_number, resolved_url, product_identifier, resolved_at, last_verified_at,
            is_valid, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
        ON CONFLICT(competitor_key, manufacturer, normalized_part_number) DO UPDATE SET
            part_number=excluded.part_number,
            resolved_url=excluded.resolved_url,
            product_identifier=excluded.product_identifier,
            last_verified_at=excluded.last_verified_at,
            is_valid=1,
            updated_at=excluded.updated_at
        """,
        (
            competitor_key,
            normalize_manufacturer(manufacturer),
            part_number,
            normalize_part_key(part_number),
            resolved_url,
            product_identifier,
            now,
            now,
            now,
            now,
        ),
    )


def invalidate_product_url(
    database_path: Path,
    competitor_key: str,
    manufacturer: str,
    part_number: str,
) -> None:
    """Mark a cached URL unusable so the next run searches again.

    Called when a cached page no longer shows the part we asked for, which
    happens when a competitor reorganises its catalogue.
    """
    with connect_database(database_path) as conn:
        conn.execute(
            """
            UPDATE competitor_resolution_cache
            SET is_valid=0, updated_at=?
            WHERE competitor_key=? AND manufacturer=? AND normalized_part_number=?
            """,
            (utc_now(), competitor_key, normalize_manufacturer(manufacturer), normalize_part_key(part_number)),
        )


def cache_stats(database_path: Path, competitor_key: str) -> dict[str, int]:
    with connect_database(database_path) as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) total,
                   SUM(CASE WHEN is_valid=1 THEN 1 ELSE 0 END) valid
            FROM competitor_resolution_cache
            WHERE competitor_key=?
            """,
            (competitor_key,),
        ).fetchone()
    return {"total": int(row["total"] or 0), "valid": int(row["valid"] or 0)}
