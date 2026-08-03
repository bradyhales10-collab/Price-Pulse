from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any

from app.config import DEFAULT_DATABASE_PATH
from app.models import PartRecord
from app.schemas.product_observation import PageClassification, ProductObservation
from app.url_builder import UnsupportedManufacturerError, build_partzilla_product_url

SCHEMA_VERSION = 9
PARTZILLA_CODE = "partzilla"
MOTOSPORT_CODE = "motosport"
CHAPARRAL_CODE = "chaparral"
REVZILLA_CODE = "revzilla"


def utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def connect_database(path: Path = DEFAULT_DATABASE_PATH) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def initialize_database(path: Path = DEFAULT_DATABASE_PATH) -> None:
    with connect_database(path) as conn:
        conn.executescript(SCHEMA_SQL)
        _apply_migrations(conn)
        seed_partzilla(conn)


def _apply_migrations(conn: sqlite3.Connection) -> None:
    applied_versions = {
        int(row["version"]) for row in conn.execute("SELECT version FROM schema_migrations").fetchall()
    }
    if 1 not in applied_versions:
        conn.execute(
            "INSERT INTO schema_migrations(version, description, applied_at) VALUES (?, ?, ?)",
            (1, "initial pricing monitor schema", utc_now()),
        )
    if 2 not in applied_versions:
        _ensure_column(conn, "current_listing_state", "reference_price_cents", "INTEGER")
        _ensure_column(conn, "current_listing_state", "savings_percent", "INTEGER")
        _ensure_column(conn, "current_listing_state", "price_display_type", "TEXT")
        _ensure_column(conn, "current_listing_state", "selling_price_confidence", "TEXT")
        _ensure_column(conn, "current_listing_state", "reference_price_confidence", "TEXT")
        _ensure_column(conn, "listing_history", "previous_reference_price_cents", "INTEGER")
        _ensure_column(conn, "listing_history", "new_reference_price_cents", "INTEGER")
        _ensure_column(conn, "listing_history", "previous_price_display_type", "TEXT")
        _ensure_column(conn, "listing_history", "new_price_display_type", "TEXT")
        conn.execute(
            "INSERT INTO schema_migrations(version, description, applied_at) VALUES (?, ?, ?)",
            (2, "add reference price and discount display fields", utc_now()),
        )
    if 3 not in applied_versions:
        conn.executescript(IMPORT_SCHEMA_SQL)
        conn.execute(
            "INSERT INTO schema_migrations(version, description, applied_at) VALUES (?, ?, ?)",
            (3, "add imports and internal product state", utc_now()),
        )
    if 4 not in applied_versions:
        _ensure_competitor_metadata(conn)
        conn.executescript(COMPETITOR_PROBE_SCHEMA_SQL)
        _ensure_column(conn, "internal_product_state", "source_type", "TEXT")
        _ensure_column(conn, "internal_product_state", "source_name", "TEXT")
        _ensure_column(conn, "internal_product_state", "external_sync_id", "TEXT")
        _ensure_column(conn, "internal_product_state", "last_source_sync_at", "TEXT")
        conn.execute(
            "INSERT INTO schema_migrations(version, description, applied_at) VALUES (?, ?, ?)",
            (4, "add multi competitor metadata and probe results", utc_now()),
        )
    if 5 not in applied_versions:
        conn.executescript(PRICING_REVIEW_SCHEMA_SQL)
        conn.execute(
            "INSERT INTO schema_migrations(version, description, applied_at) VALUES (?, ?, ?)",
            (5, "add pricing review decision workflow", utc_now()),
        )
    if 6 not in applied_versions:
        conn.executescript(PRICING_RULES_SCHEMA_SQL)
        _ensure_column(conn, "pricing_review_decisions", "applied_rule_codes_json", "TEXT")
        conn.execute(
            "INSERT INTO schema_migrations(version, description, applied_at) VALUES (?, ?, ?)",
            (6, "add configurable pricing rules", utc_now()),
        )
    if 7 not in applied_versions:
        conn.executescript(CHAPARRAL_CACHE_SCHEMA_SQL)
        conn.execute(
            "INSERT INTO schema_migrations(version, description, applied_at) VALUES (?, ?, ?)",
            (7, "add Chaparral resolution cache", utc_now()),
        )
    if 8 not in applied_versions:
        _ensure_column(conn, "pricing_review_decisions", "original_price_cents", "INTEGER")
        conn.execute(
            "INSERT INTO schema_migrations(version, description, applied_at) VALUES (?, ?, ?)",
            (8, "track original price before review updates", utc_now()),
        )
    if 9 not in applied_versions:
        conn.executescript(PRICING_RULE_MANUFACTURER_OVERRIDE_SCHEMA_SQL)
        conn.execute(
            "INSERT INTO schema_migrations(version, description, applied_at) VALUES (?, ?, ?)",
            (9, "add manufacturer-specific pricing rule overrides", utc_now()),
        )
    _ensure_column(conn, "competitor_probe_results", "price_visibility", "TEXT")
    _ensure_column(conn, "competitor_probe_results", "price_display_type", "TEXT")
    _ensure_column(conn, "competitor_probe_results", "result_type", "TEXT")
    _ensure_column(conn, "competitors", "cart_price_probe_status", "TEXT DEFAULT 'not_reviewed'")
    _ensure_column(conn, "pricing_review_decisions", "applied_rule_codes_json", "TEXT")
    _ensure_column(conn, "pricing_review_decisions", "original_price_cents", "INTEGER")
    conn.executescript(CART_PROBE_SCHEMA_SQL)
    conn.executescript(CHAPARRAL_CACHE_SCHEMA_SQL)
    conn.executescript(PRICING_REVIEW_SCHEMA_SQL)
    conn.executescript(PRICING_RULES_SCHEMA_SQL)
    conn.executescript(PRICING_RULE_MANUFACTURER_OVERRIDE_SCHEMA_SQL)
    seed_pricing_rules(conn)


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def _ensure_competitor_metadata(conn: sqlite3.Connection) -> None:
    _ensure_column(conn, "competitors", "status", "TEXT DEFAULT 'active'")
    _ensure_column(conn, "competitors", "requires_login", "INTEGER DEFAULT 0")
    _ensure_column(conn, "competitors", "supports_public_price", "INTEGER DEFAULT 0")
    _ensure_column(conn, "competitors", "supports_direct_part_url", "INTEGER DEFAULT 1")
    _ensure_column(conn, "competitors", "notes", "TEXT")
    _ensure_column(conn, "competitors", "legal_review_status", "TEXT DEFAULT 'review_needed'")
    _ensure_column(conn, "competitors", "cart_price_probe_status", "TEXT DEFAULT 'not_reviewed'")


def seed_partzilla(conn: sqlite3.Connection) -> int:
    now = utc_now()
    conn.execute(
        """
        INSERT INTO competitors(competitor_code, competitor_name, base_url, is_active, status, requires_login,
            supports_public_price, supports_direct_part_url, notes, legal_review_status, cart_price_probe_status, created_at, updated_at)
        VALUES (?, ?, ?, 1, 'active', 1, 0, 1, ?, 'review_needed', 'disabled', ?, ?)
        ON CONFLICT(competitor_code) DO UPDATE SET
            competitor_name=excluded.competitor_name,
            base_url=excluded.base_url,
            status=excluded.status,
            requires_login=excluded.requires_login,
            supports_public_price=excluded.supports_public_price,
            supports_direct_part_url=excluded.supports_direct_part_url,
            notes=excluded.notes,
            legal_review_status=COALESCE(competitors.legal_review_status, excluded.legal_review_status),
            cart_price_probe_status=COALESCE(competitors.cart_price_probe_status, excluded.cart_price_probe_status),
            is_active=1,
            updated_at=excluded.updated_at
        """,
        (PARTZILLA_CODE, "Partzilla", "https://www.partzilla.com", "Production baseline collector.", now, now),
    )
    _seed_partzilla_mappings(conn)
    seed_motosport(conn)
    seed_chaparral(conn)
    seed_revzilla(conn)
    return int(conn.execute("SELECT competitor_id FROM competitors WHERE competitor_code = ?", (PARTZILLA_CODE,)).fetchone()[0])


def seed_motosport(conn: sqlite3.Connection) -> int:
    now = utc_now()
    conn.execute(
        """
        INSERT INTO competitors(competitor_code, competitor_name, base_url, is_active, status, requires_login,
            supports_public_price, supports_direct_part_url, notes, legal_review_status, cart_price_probe_status, created_at, updated_at)
        VALUES (?, ?, ?, 1, 'active', 0, 1, 1, ?, 'approved_for_monitoring', 'production_enabled', ?, ?)
        ON CONFLICT(competitor_code) DO UPDATE SET
            competitor_name=excluded.competitor_name,
            base_url=excluded.base_url,
            status=excluded.status,
            requires_login=excluded.requires_login,
            supports_public_price=excluded.supports_public_price,
            supports_direct_part_url=excluded.supports_direct_part_url,
            notes=excluded.notes,
            legal_review_status=excluded.legal_review_status,
            cart_price_probe_status=excluded.cart_price_probe_status,
            is_active=1,
            updated_at=excluded.updated_at
        """,
        (MOTOSPORT_CODE, "MotoSport", "https://www.motosport.com", "Production competitor collector with public product and cart-price support.", now, now),
    )
    return int(conn.execute("SELECT competitor_id FROM competitors WHERE competitor_code = ?", (MOTOSPORT_CODE,)).fetchone()[0])


def seed_revzilla(conn: sqlite3.Connection) -> int:
    now = utc_now()
    conn.execute(
        """
        INSERT INTO competitors(competitor_code, competitor_name, base_url, is_active, status, requires_login,
            supports_public_price, supports_direct_part_url, notes, legal_review_status, cart_price_probe_status, created_at, updated_at)
        VALUES (?, ?, ?, 1, 'experimental_probe', 0, 1, 0, ?, 'approved_for_monitoring', 'disabled', ?, ?)
        ON CONFLICT(competitor_code) DO UPDATE SET
            competitor_name=excluded.competitor_name,
            base_url=excluded.base_url,
            status=excluded.status,
            requires_login=excluded.requires_login,
            supports_public_price=excluded.supports_public_price,
            supports_direct_part_url=excluded.supports_direct_part_url,
            notes=excluded.notes,
            legal_review_status=COALESCE(competitors.legal_review_status, excluded.legal_review_status),
            cart_price_probe_status=COALESCE(competitors.cart_price_probe_status, excluded.cart_price_probe_status),
            is_active=1,
            updated_at=excluded.updated_at
        """,
        (
            REVZILLA_CODE,
            "RevZilla",
            "https://www.revzilla.com",
            "Probe-verified but awaiting a production collector for its search-based lookup. "
            "OEM parts fulfilled by Montgomeryville Cycle Center; motorcycle brands only, no Polaris. "
            "Search-based lookup because product URLs embed a description slug. Prices on discontinued or "
            "out-of-stock listings are ignored.",
            now,
            now,
        ),
    )
    return int(conn.execute("SELECT competitor_id FROM competitors WHERE competitor_code = ?", (REVZILLA_CODE,)).fetchone()[0])


def seed_chaparral(conn: sqlite3.Connection) -> int:
    now = utc_now()
    conn.execute(
        """
        INSERT INTO competitors(competitor_code, competitor_name, base_url, is_active, status, requires_login,
            supports_public_price, supports_direct_part_url, notes, legal_review_status, cart_price_probe_status, created_at, updated_at)
        VALUES (?, ?, ?, 1, 'active', 0, 1, 0, ?, 'approved_for_monitoring', 'production_enabled', ?, ?)
        ON CONFLICT(competitor_code) DO UPDATE SET
            competitor_name=excluded.competitor_name,
            base_url=excluded.base_url,
            status=excluded.status,
            requires_login=excluded.requires_login,
            supports_public_price=excluded.supports_public_price,
            supports_direct_part_url=excluded.supports_direct_part_url,
            notes=excluded.notes,
            legal_review_status=excluded.legal_review_status,
            cart_price_probe_status=excluded.cart_price_probe_status,
            is_active=1,
            updated_at=excluded.updated_at
        """,
        (
            CHAPARRAL_CODE,
            "Chaparral Motorsports",
            "https://www.chapmoto.com",
            "Production competitor collector using OEM bulk lookup plus verified resolved URL cache.",
            now,
            now,
        ),
    )
    return int(conn.execute("SELECT competitor_id FROM competitors WHERE competitor_code = ?", (CHAPARRAL_CODE,)).fetchone()[0])


def seed_competitor(conn: sqlite3.Connection, competitor_code: str) -> int:
    normalized = competitor_code.strip().lower()
    if normalized == PARTZILLA_CODE:
        return seed_partzilla(conn)
    if normalized == MOTOSPORT_CODE:
        return seed_motosport(conn)
    if normalized == CHAPARRAL_CODE:
        return seed_chaparral(conn)
    raise ValueError(f"Unknown competitor: {competitor_code}")


def seed_pricing_rules(conn: sqlite3.Connection) -> None:
    now = utc_now()
    defaults = [
        (
            "skip_unsafe_competitor_data",
            "Skip Unsafe Competitor Data",
            "guardrail",
            1,
            5,
            '{"skip_hidden_prices": true, "skip_missing_prices": true}',
            "Do not create an automatic suggestion when the only competitor data is hidden, missing, or unclear.",
        ),
        (
            "use_lowest_competitor",
            "Use Lowest Competitor",
            "anchor",
            1,
            10,
            '{"adjustment_cents": 0}',
            "Start from the lowest reliable competitor selling price.",
        ),
        (
            "round_to_99",
            "Round To .99",
            "rounding",
            1,
            20,
            '{"ending_cents": 99}',
            "Round the suggestion to a retail-friendly .99 ending.",
        ),
        (
            "protect_minimum_margin",
            "Protect Minimum Margin",
            "margin_floor",
            1,
            30,
            '{"minimum_margin_pct": 20}',
            "Raise the suggestion when needed so it does not fall below the minimum gross margin.",
        ),
        (
            "keep_price_on_low_value_items",
            "Keep Price On Low-Value Items",
            "low_price_floor",
            1,
            40,
            '{"minimum_price": 5}',
            "Do not suggest lowering our price on inexpensive parts. Matching the market is not worth it at this value, and loyalty benefits such as RM Cash already add value for the customer. Price increases are still allowed.",
        ),
    ]
    for code, name, rule_type, is_enabled, priority, settings_json, description in defaults:
        conn.execute(
            """
            INSERT INTO pricing_rules(rule_code, rule_name, rule_type, is_enabled, priority, settings_json,
                description, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(rule_code) DO UPDATE SET
                rule_name=excluded.rule_name,
                rule_type=excluded.rule_type,
                description=excluded.description,
                updated_at=excluded.updated_at
            """,
            (code, name, rule_type, is_enabled, priority, settings_json, description, now, now),
        )


def _seed_partzilla_mappings(conn: sqlite3.Connection) -> None:
    mappings = {
        "Kawasaki": "kawasaki",
        "Honda": "honda",
        "Yamaha": "yamaha",
        "Suzuki": "suzuki",
        "Polaris": "polaris",
        "Can-Am": "can-am",
        "Arctic Cat": "arctic-cat",
        "Sea-Doo": "sea-doo",
        "Ski-Doo": "ski-doo",
    }
    now = utc_now()
    for manufacturer, slug in mappings.items():
        conn.execute(
            """
            INSERT INTO competitor_manufacturer_mappings(
                competitor_key, manufacturer_display_name, manufacturer_alias,
                competitor_manufacturer_slug, is_supported, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, 1, ?, ?)
            ON CONFLICT(competitor_key, manufacturer_display_name, manufacturer_alias)
            DO UPDATE SET competitor_manufacturer_slug=excluded.competitor_manufacturer_slug,
                is_supported=1, updated_at=excluded.updated_at
            """,
            (PARTZILLA_CODE, manufacturer, manufacturer, slug, now, now),
        )


def normalize_part_number(part_number: str) -> str:
    return part_number.strip().upper()


def money_to_cents(value: Decimal | str | None) -> int | None:
    if value is None or value == "":
        return None
    decimal = value if isinstance(value, Decimal) else Decimal(str(value))
    return int((decimal * Decimal("100")).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def cents_to_money(cents: int | None) -> str:
    if cents is None:
        return ""
    return format((Decimal(cents) / Decimal("100")).quantize(Decimal("0.01")), "f")


def _format_money_decimal(value: Decimal) -> str:
    return format(value.quantize(Decimal("0.01")), "f")


@dataclass(frozen=True)
class ImportStats:
    rows_read: int = 0
    products_inserted: int = 0
    products_updated: int = 0
    listings_inserted: int = 0
    listings_updated: int = 0
    invalid_rows: int = 0


def upsert_product_and_listing(conn: sqlite3.Connection, record: PartRecord) -> tuple[int, int, bool, bool]:
    now = utc_now()
    normalized = normalize_part_number(record.oem_part_number)
    existing_product = conn.execute(
        "SELECT product_id, product_name FROM products WHERE manufacturer = ? AND normalized_part_number = ?",
        (record.manufacturer, normalized),
    ).fetchone()
    product_inserted = existing_product is None
    if existing_product is None:
        cur = conn.execute(
            """
            INSERT INTO products(manufacturer, oem_part_number, normalized_part_number, product_name, is_active, created_at, updated_at)
            VALUES (?, ?, ?, ?, 1, ?, ?)
            """,
            (record.manufacturer, record.oem_part_number, normalized, record.search_observed_product_name or None, now, now),
        )
        product_id = int(cur.lastrowid)
    else:
        product_id = int(existing_product["product_id"])
        conn.execute(
            """
            UPDATE products SET oem_part_number=?, product_name=COALESCE(NULLIF(?, ''), product_name), updated_at=?
            WHERE product_id=?
            """,
            (record.oem_part_number, record.search_observed_product_name, now, product_id),
        )

    competitor_id = seed_partzilla(conn)
    try:
        canonical_url = build_partzilla_product_url(record.manufacturer, record.oem_part_number)
    except UnsupportedManufacturerError:
        canonical_url = ""
    listing_id, listing_inserted = upsert_competitor_listing(
        conn,
        product_id=product_id,
        competitor_id=competitor_id,
        competitor_part_number=record.oem_part_number,
        canonical_url=canonical_url,
    )
    return product_id, listing_id, product_inserted, listing_inserted


def upsert_competitor_listing(
    conn: sqlite3.Connection,
    *,
    product_id: int,
    competitor_id: int,
    competitor_part_number: str,
    canonical_url: str,
) -> tuple[int, bool]:
    now = utc_now()
    existing_listing = conn.execute(
        "SELECT listing_id FROM competitor_listings WHERE product_id=? AND competitor_id=?",
        (product_id, competitor_id),
    ).fetchone()
    listing_inserted = existing_listing is None
    if existing_listing is None:
        cur = conn.execute(
            """
            INSERT INTO competitor_listings(product_id, competitor_id, competitor_part_number, canonical_url, is_active,
                first_seen_at, last_seen_at, created_at, updated_at)
            VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?)
            """,
            (product_id, competitor_id, competitor_part_number, canonical_url, now, now, now, now),
        )
        listing_id = int(cur.lastrowid)
    else:
        listing_id = int(existing_listing["listing_id"])
        conn.execute(
            """
            UPDATE competitor_listings SET competitor_part_number=?, canonical_url=?, last_seen_at=?, updated_at=?
            WHERE listing_id=?
            """,
            (competitor_part_number, canonical_url, now, now, listing_id),
        )
    return listing_id, listing_inserted


def table_counts(conn: sqlite3.Connection) -> dict[str, int]:
    tables = [
        "products",
        "competitors",
        "competitor_listings",
        "scan_runs",
        "scan_events",
        "current_listing_state",
        "listing_history",
    ]
    return {table: int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]) for table in tables}


def create_scan_run(conn: sqlite3.Connection, *, competitor_id: int, requested_part_count: int, run_status: str = "running") -> int:
    now = utc_now()
    cur = conn.execute(
        """
        INSERT INTO scan_runs(competitor_id, started_at, requested_part_count, attempted_part_count,
            successful_part_count, changed_part_count, warning_count, blocked_count, challenge_count, error_count, run_status)
        VALUES (?, ?, ?, 0, 0, 0, 0, 0, 0, 0, ?)
        """,
        (competitor_id, now, requested_part_count, run_status),
    )
    return int(cur.lastrowid)


def complete_scan_run(conn: sqlite3.Connection, scan_run_id: int) -> None:
    row = conn.execute(
        """
        SELECT
          COUNT(*) attempted,
          SUM(CASE WHEN page_classification='normal_product' AND price_found=1 THEN 1 ELSE 0 END) successful,
          SUM(CASE WHEN parse_warning_count > 0 THEN 1 ELSE 0 END) warnings,
          SUM(CASE WHEN page_classification='blocked' THEN 1 ELSE 0 END) blocked,
          SUM(CASE WHEN page_classification='challenge' THEN 1 ELSE 0 END) challenge,
          SUM(CASE WHEN page_classification IN ('navigation_error','unknown') THEN 1 ELSE 0 END) errors
        FROM scan_events WHERE scan_run_id=?
        """,
        (scan_run_id,),
    ).fetchone()
    changed = int(conn.execute("SELECT COUNT(*) FROM listing_history WHERE scan_event_id IN (SELECT scan_event_id FROM scan_events WHERE scan_run_id=?)", (scan_run_id,)).fetchone()[0])
    status = "completed"
    if row["blocked"]:
        status = "stopped_blocked"
    elif row["challenge"]:
        status = "stopped_challenge"
    elif row["errors"]:
        status = "failed"
    elif row["warnings"]:
        status = "completed_with_warnings"
    conn.execute(
        """
        UPDATE scan_runs SET completed_at=?, attempted_part_count=?, successful_part_count=?, changed_part_count=?,
            warning_count=?, blocked_count=?, challenge_count=?, error_count=?, run_status=?
        WHERE scan_run_id=?
        """,
        (
            utc_now(),
            int(row["attempted"] or 0),
            int(row["successful"] or 0),
            changed,
            int(row["warnings"] or 0),
            int(row["blocked"] or 0),
            int(row["challenge"] or 0),
            int(row["errors"] or 0),
            status,
            scan_run_id,
        ),
    )


def persist_observation(
    conn: sqlite3.Connection,
    *,
    scan_run_id: int,
    listing_id: int,
    observation: ProductObservation,
    observation_json_path: str | None,
    price_source_category: str | None = None,
) -> str:
    now = observation.checked_at or utc_now()
    price_found = observation.selling_price is not None
    navigation_succeeded = observation.page_classification != PageClassification.NAVIGATION_ERROR
    with conn:
        cur = conn.execute(
            """
            INSERT INTO scan_events(scan_run_id, listing_id, checked_at, http_status, page_classification, session_status,
                navigation_succeeded, price_found, price_parse_confidence, parse_warning_count, parse_warnings,
                observation_json_path, error_message)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
            """,
            (
                scan_run_id,
                listing_id,
                now,
                observation.http_status,
                observation.page_classification.value,
                observation.session_status.value,
                1 if navigation_succeeded else 0,
                1 if price_found else 0,
                observation.price_parse_confidence.value,
                len(observation.parse_warnings),
                "; ".join(observation.parse_warnings),
                observation_json_path,
            ),
        )
        scan_event_id = int(cur.lastrowid)
        successful = (
            observation.page_classification.value == "normal_product"
            and price_found
            and observation.session_status.value not in {"expired_or_invalid", "authentication_required", "blocked", "challenge", "navigation_error"}
        )
        state = conn.execute("SELECT * FROM current_listing_state WHERE listing_id=?", (listing_id,)).fetchone()
        if not successful:
            if state is not None:
                conn.execute(
                    "UPDATE current_listing_state SET consecutive_failure_count=consecutive_failure_count+1, updated_at=? WHERE listing_id=?",
                    (utc_now(), listing_id),
                )
            return "warning or failure"

        new_price = money_to_cents(observation.selling_price)
        new_reference_price = money_to_cents(observation.reference_price)
        changes: list[str] = []
        previous = dict(state) if state is not None else None
        if previous is None:
            changes.append("first_observation")
        else:
            if previous["selling_price_cents"] != new_price:
                changes.append("price_change")
            if previous["reference_price_cents"] != new_reference_price:
                changes.append("reference_price_change")
            if (previous["price_display_type"] or "unknown") != observation.price_display_type.value:
                changes.append("price_display_type_change")
            if previous["availability_status"] != observation.availability_status.value:
                changes.append("availability_change")
            if bool(previous["supersession_detected"]) != observation.supersession_detected or previous["superseded_by_raw"] != observation.superseded_by_raw:
                changes.append("supersession_change")

        change_type = _change_type(changes)
        last_changed_at = now if change_type else previous["last_changed_at"]
        if previous is None:
            conn.execute(
                """
                INSERT INTO current_listing_state(listing_id, selling_price_cents, currency_code, availability_raw,
                    availability_status, product_name, observed_part_number, supersession_detected, superseded_by_raw,
                    reference_price_cents, savings_percent, price_display_type, selling_price_confidence,
                    reference_price_confidence, price_source_category, price_parse_confidence, first_observed_at, last_successful_check_at,
                    last_changed_at, consecutive_failure_count, updated_at)
                VALUES (?, ?, 'USD', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)
                """,
                (
                    listing_id,
                    new_price,
                    observation.availability_raw,
                    observation.availability_status.value,
                    observation.product_name,
                    observation.observed_part_number,
                    1 if observation.supersession_detected else 0,
                    observation.superseded_by_raw,
                    new_reference_price,
                    observation.savings_percent,
                    observation.price_display_type.value,
                    observation.selling_price_confidence.value,
                    observation.reference_price_confidence.value,
                    price_source_category,
                    observation.price_parse_confidence.value,
                    now,
                    now,
                    now,
                    utc_now(),
                ),
            )
        else:
            conn.execute(
                """
                UPDATE current_listing_state SET selling_price_cents=?, currency_code='USD', availability_raw=?,
                    availability_status=?, product_name=?, observed_part_number=?, supersession_detected=?,
                    superseded_by_raw=?, reference_price_cents=?, savings_percent=?, price_display_type=?,
                    selling_price_confidence=?, reference_price_confidence=?, price_source_category=?, price_parse_confidence=?,
                    last_successful_check_at=?, last_changed_at=?, consecutive_failure_count=0, updated_at=?
                WHERE listing_id=?
                """,
                (
                    new_price,
                    observation.availability_raw,
                    observation.availability_status.value,
                    observation.product_name,
                    observation.observed_part_number,
                    1 if observation.supersession_detected else 0,
                    observation.superseded_by_raw,
                    new_reference_price,
                    observation.savings_percent,
                    observation.price_display_type.value,
                    observation.selling_price_confidence.value,
                    observation.reference_price_confidence.value,
                    price_source_category,
                    observation.price_parse_confidence.value,
                    now,
                    last_changed_at,
                    utc_now(),
                    listing_id,
                ),
            )
        if change_type:
            conn.execute(
                """
                INSERT INTO listing_history(listing_id, scan_event_id, effective_at, change_type,
                    previous_selling_price_cents, new_selling_price_cents,
                    previous_reference_price_cents, new_reference_price_cents,
                    previous_price_display_type, new_price_display_type,
                    previous_availability_status, new_availability_status,
                    previous_supersession_detected, new_supersession_detected,
                    previous_superseded_by_raw, new_superseded_by_raw, change_details_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    listing_id,
                    scan_event_id,
                    now,
                    change_type,
                    previous["selling_price_cents"] if previous else None,
                    new_price,
                    previous["reference_price_cents"] if previous else None,
                    new_reference_price,
                    previous["price_display_type"] if previous else None,
                    observation.price_display_type.value,
                    previous["availability_status"] if previous else None,
                    observation.availability_status.value,
                    previous["supersession_detected"] if previous else None,
                    1 if observation.supersession_detected else 0,
                    previous["superseded_by_raw"] if previous else None,
                    observation.superseded_by_raw,
                    _change_details(previous, observation, new_price, new_reference_price, changes),
                ),
            )
        return change_type or "no change"


def _change_type(changes: list[str]) -> str | None:
    if not changes:
        return None
    if "first_observation" in changes:
        return "first_observation"
    if len(changes) > 1:
        return "multiple_changes"
    if changes[0] in {"reference_price_change", "price_display_type_change"}:
        return "multiple_changes"
    return changes[0]


def _change_details(previous: dict[str, Any] | None, observation: ProductObservation, new_cents: int | None, new_reference_cents: int | None, changes: list[str]) -> str | None:
    if not changes:
        return None
    details: dict[str, Any] = {"changed_fields": changes}
    if previous is not None and "price_change" in changes and previous["selling_price_cents"] is not None and new_cents is not None:
        previous_price = Decimal(previous["selling_price_cents"]) / Decimal("100")
        new_price = Decimal(new_cents) / Decimal("100")
        dollar = new_price - previous_price
        percent = (dollar / previous_price * Decimal("100")) if previous_price != 0 else Decimal("0")
        details.update(
            {
                "previous_price": _format_money_decimal(previous_price),
                "new_price": _format_money_decimal(new_price),
                "dollar_change": _format_money_decimal(dollar),
                "percent_change": format(percent.quantize(Decimal("0.0001")), "f"),
            }
        )
    if "reference_price_change" in changes:
        details["previous_reference_price"] = cents_to_money(previous["reference_price_cents"] if previous else None)
        details["new_reference_price"] = cents_to_money(new_reference_cents)
    if "price_display_type_change" in changes:
        details["previous_price_display_type"] = previous["price_display_type"] if previous else None
        details["new_price_display_type"] = observation.price_display_type.value
    return json.dumps(details)


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    description TEXT NOT NULL,
    applied_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS products (
    product_id INTEGER PRIMARY KEY,
    manufacturer TEXT NOT NULL,
    oem_part_number TEXT NOT NULL,
    normalized_part_number TEXT NOT NULL,
    internal_sku TEXT,
    product_name TEXT,
    product_category TEXT,
    is_active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(manufacturer, normalized_part_number)
);
CREATE TABLE IF NOT EXISTS competitors (
    competitor_id INTEGER PRIMARY KEY,
    competitor_code TEXT NOT NULL UNIQUE,
    competitor_name TEXT NOT NULL,
    base_url TEXT NOT NULL,
    is_active INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'active',
    requires_login INTEGER NOT NULL DEFAULT 0,
    supports_public_price INTEGER NOT NULL DEFAULT 0,
    supports_direct_part_url INTEGER NOT NULL DEFAULT 1,
    notes TEXT,
    legal_review_status TEXT NOT NULL DEFAULT 'review_needed',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS competitor_listings (
    listing_id INTEGER PRIMARY KEY,
    product_id INTEGER NOT NULL REFERENCES products(product_id),
    competitor_id INTEGER NOT NULL REFERENCES competitors(competitor_id),
    competitor_part_number TEXT NOT NULL,
    canonical_url TEXT NOT NULL,
    is_active INTEGER NOT NULL DEFAULT 1,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(product_id, competitor_id)
);
CREATE TABLE IF NOT EXISTS scan_runs (
    scan_run_id INTEGER PRIMARY KEY,
    competitor_id INTEGER NOT NULL REFERENCES competitors(competitor_id),
    started_at TEXT NOT NULL,
    completed_at TEXT,
    requested_part_count INTEGER NOT NULL,
    attempted_part_count INTEGER NOT NULL DEFAULT 0,
    successful_part_count INTEGER NOT NULL DEFAULT 0,
    changed_part_count INTEGER NOT NULL DEFAULT 0,
    warning_count INTEGER NOT NULL DEFAULT 0,
    blocked_count INTEGER NOT NULL DEFAULT 0,
    challenge_count INTEGER NOT NULL DEFAULT 0,
    error_count INTEGER NOT NULL DEFAULT 0,
    run_status TEXT NOT NULL CHECK(run_status IN ('running','completed','completed_with_warnings','stopped_blocked','stopped_challenge','failed'))
);
CREATE TABLE IF NOT EXISTS scan_events (
    scan_event_id INTEGER PRIMARY KEY,
    scan_run_id INTEGER NOT NULL REFERENCES scan_runs(scan_run_id),
    listing_id INTEGER NOT NULL REFERENCES competitor_listings(listing_id),
    checked_at TEXT NOT NULL,
    http_status INTEGER,
    page_classification TEXT NOT NULL,
    session_status TEXT NOT NULL,
    navigation_succeeded INTEGER NOT NULL,
    price_found INTEGER NOT NULL,
    price_parse_confidence TEXT,
    parse_warning_count INTEGER NOT NULL,
    parse_warnings TEXT,
    observation_json_path TEXT,
    error_message TEXT
);
CREATE TABLE IF NOT EXISTS current_listing_state (
    listing_id INTEGER PRIMARY KEY REFERENCES competitor_listings(listing_id),
    selling_price_cents INTEGER,
    reference_price_cents INTEGER,
    savings_percent INTEGER,
    price_display_type TEXT,
    selling_price_confidence TEXT,
    reference_price_confidence TEXT,
    currency_code TEXT NOT NULL DEFAULT 'USD',
    availability_raw TEXT,
    availability_status TEXT,
    product_name TEXT,
    observed_part_number TEXT,
    supersession_detected INTEGER NOT NULL DEFAULT 0,
    superseded_by_raw TEXT,
    price_source_category TEXT,
    price_parse_confidence TEXT,
    first_observed_at TEXT NOT NULL,
    last_successful_check_at TEXT NOT NULL,
    last_changed_at TEXT NOT NULL,
    consecutive_failure_count INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS listing_history (
    history_id INTEGER PRIMARY KEY,
    listing_id INTEGER NOT NULL REFERENCES competitor_listings(listing_id),
    scan_event_id INTEGER NOT NULL REFERENCES scan_events(scan_event_id),
    effective_at TEXT NOT NULL,
    change_type TEXT NOT NULL CHECK(change_type IN ('first_observation','price_change','availability_change','supersession_change','multiple_changes')),
    previous_selling_price_cents INTEGER,
    new_selling_price_cents INTEGER,
    previous_reference_price_cents INTEGER,
    new_reference_price_cents INTEGER,
    previous_price_display_type TEXT,
    new_price_display_type TEXT,
    previous_availability_status TEXT,
    new_availability_status TEXT,
    previous_supersession_detected INTEGER,
    new_supersession_detected INTEGER,
    previous_superseded_by_raw TEXT,
    new_superseded_by_raw TEXT,
    change_details_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_products_part ON products(manufacturer, normalized_part_number);
CREATE INDEX IF NOT EXISTS idx_events_run ON scan_events(scan_run_id);
CREATE INDEX IF NOT EXISTS idx_history_listing_time ON listing_history(listing_id, effective_at);
"""


IMPORT_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS import_batches (
    import_batch_id INTEGER PRIMARY KEY,
    original_filename TEXT NOT NULL,
    stored_filename TEXT NOT NULL,
    file_sha256 TEXT NOT NULL,
    worksheet_name TEXT,
    uploaded_at TEXT NOT NULL,
    validated_at TEXT,
    imported_at TEXT,
    status TEXT NOT NULL,
    rows_read INTEGER NOT NULL DEFAULT 0,
    valid_rows INTEGER NOT NULL DEFAULT 0,
    invalid_rows INTEGER NOT NULL DEFAULT 0,
    inserted_rows INTEGER NOT NULL DEFAULT 0,
    updated_rows INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS import_batch_rows (
    import_batch_row_id INTEGER PRIMARY KEY,
    import_batch_id INTEGER NOT NULL REFERENCES import_batches(import_batch_id),
    source_row_number INTEGER NOT NULL,
    product_id INTEGER,
    row_status TEXT NOT NULL,
    action TEXT,
    validation_errors_json TEXT,
    source_values_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS internal_product_state (
    product_id INTEGER PRIMARY KEY REFERENCES products(product_id),
    internal_sku TEXT NOT NULL,
    our_current_price_cents INTEGER NOT NULL,
    current_cost_cents INTEGER,
    product_category TEXT,
    units_sold_12m INTEGER,
    inventory_qty INTEGER,
    scan_priority TEXT,
    is_active INTEGER NOT NULL DEFAULT 1,
    source_import_batch_id INTEGER REFERENCES import_batches(import_batch_id),
    source_type TEXT,
    source_name TEXT,
    external_sync_id TEXT,
    last_source_sync_at TEXT,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_internal_sku ON internal_product_state(internal_sku);
CREATE INDEX IF NOT EXISTS idx_import_rows_batch ON import_batch_rows(import_batch_id);
"""


COMPETITOR_PROBE_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS competitor_manufacturer_mappings (
    mapping_id INTEGER PRIMARY KEY,
    competitor_key TEXT NOT NULL,
    manufacturer_display_name TEXT NOT NULL,
    manufacturer_alias TEXT NOT NULL,
    competitor_manufacturer_slug TEXT,
    is_supported INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(competitor_key, manufacturer_display_name, manufacturer_alias)
);
CREATE TABLE IF NOT EXISTS competitor_probe_results (
    probe_result_id INTEGER PRIMARY KEY,
    competitor_key TEXT NOT NULL,
    product_id INTEGER REFERENCES products(product_id),
    manufacturer TEXT NOT NULL,
    oem_part_number TEXT NOT NULL,
    url TEXT NOT NULL,
    checked_at TEXT NOT NULL,
    http_status INTEGER,
    page_classification TEXT NOT NULL,
    selling_price_cents INTEGER,
    reference_price_cents INTEGER,
    savings_percent INTEGER,
    price_visibility TEXT,
    price_display_type TEXT,
    result_type TEXT,
    availability_raw TEXT,
    availability_status TEXT,
    parse_confidence TEXT,
    warnings_json TEXT NOT NULL,
    raw_result_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_probe_competitor_part ON competitor_probe_results(competitor_key, oem_part_number);
"""


CART_PROBE_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS competitor_cart_probe_results (
    cart_probe_result_id INTEGER PRIMARY KEY,
    competitor_key TEXT NOT NULL,
    manufacturer TEXT NOT NULL,
    oem_part_number TEXT NOT NULL,
    product_url TEXT NOT NULL,
    checked_at TEXT NOT NULL,
    product_association_confirmed INTEGER NOT NULL DEFAULT 0,
    reference_price_cents INTEGER,
    cart_selling_price_cents INTEGER,
    quantity INTEGER,
    line_subtotal_cents INTEGER,
    cart_price_confidence TEXT,
    cleanup_status TEXT NOT NULL,
    warnings_json TEXT NOT NULL,
    raw_result_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cart_probe_competitor_part ON competitor_cart_probe_results(competitor_key, oem_part_number);
"""


CHAPARRAL_CACHE_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS chaparral_resolution_cache (
    cache_id INTEGER PRIMARY KEY,
    manufacturer TEXT NOT NULL,
    part_number TEXT NOT NULL,
    normalized_part_number TEXT NOT NULL,
    resolved_url TEXT NOT NULL,
    product_identifier TEXT,
    resolved_at TEXT NOT NULL,
    last_verified_at TEXT,
    is_valid INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(manufacturer, normalized_part_number)
);
CREATE INDEX IF NOT EXISTS idx_chaparral_cache_part ON chaparral_resolution_cache(normalized_part_number, is_valid);
"""


PRICING_REVIEW_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS pricing_review_decisions (
    review_decision_id INTEGER PRIMARY KEY,
    product_id INTEGER NOT NULL REFERENCES products(product_id),
    review_status TEXT NOT NULL DEFAULT 'Pending Review',
    original_price_cents INTEGER,
    suggested_new_price_cents INTEGER,
    notes TEXT,
    reviewer TEXT,
    reviewed_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(product_id)
);
CREATE INDEX IF NOT EXISTS idx_pricing_review_status ON pricing_review_decisions(review_status);
"""


PRICING_RULES_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS pricing_rules (
    pricing_rule_id INTEGER PRIMARY KEY,
    rule_code TEXT NOT NULL UNIQUE,
    rule_name TEXT NOT NULL,
    rule_type TEXT NOT NULL,
    is_enabled INTEGER NOT NULL DEFAULT 1,
    priority INTEGER NOT NULL DEFAULT 100,
    settings_json TEXT NOT NULL DEFAULT '{}',
    description TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pricing_rules_enabled_priority ON pricing_rules(is_enabled, priority);
"""


PRICING_RULE_MANUFACTURER_OVERRIDE_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS pricing_rule_manufacturer_overrides (
    override_id INTEGER PRIMARY KEY,
    manufacturer TEXT NOT NULL UNIQUE,
    is_enabled INTEGER NOT NULL DEFAULT 0,
    adjustment_cents INTEGER,
    ending_cents INTEGER,
    minimum_margin_pct REAL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pricing_rule_oem_overrides_enabled ON pricing_rule_manufacturer_overrides(is_enabled, manufacturer);
"""
