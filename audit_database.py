from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path

from app.config import DEFAULT_DATABASE_PATH
from app.database import cents_to_money, connect_database


@dataclass(frozen=True)
class AuditFinding:
    code: str
    severity: str
    detail: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Audit Part Pulse database for suspicious collection patterns.")
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE_PATH)
    return parser.parse_args()


def audit_database(database: Path = DEFAULT_DATABASE_PATH) -> list[AuditFinding]:
    with connect_database(database) as conn:
        findings = []
        findings.extend(_repeated_supersession_targets(conn))
        findings.extend(_supersession_carry_forward(conn))
        findings.extend(_discount_price_mismatch(conn))
        return findings


def _repeated_supersession_targets(conn) -> list[AuditFinding]:
    rows = conn.execute(
        """
        SELECT se.scan_run_id, s.superseded_by_raw, COUNT(*) count,
               GROUP_CONCAT(p.oem_part_number, ', ') parts
        FROM scan_events se
        JOIN current_listing_state s ON s.listing_id=se.listing_id
        JOIN competitor_listings l ON l.listing_id=se.listing_id
        JOIN products p ON p.product_id=l.product_id
        WHERE s.supersession_detected=1 AND s.superseded_by_raw IS NOT NULL
        GROUP BY se.scan_run_id, s.superseded_by_raw
        HAVING COUNT(*) >= 3
        """
    ).fetchall()
    return [
        AuditFinding(
            "repeated_superseded_by_raw",
            "high",
            f"scan_run_id={row['scan_run_id']} repeats superseded_by_raw={row['superseded_by_raw']} on {row['count']} products: {row['parts']}",
        )
        for row in rows
    ]


def _supersession_carry_forward(conn) -> list[AuditFinding]:
    rows = conn.execute(
        """
        SELECT se.scan_run_id, se.scan_event_id, se.checked_at, p.oem_part_number, s.superseded_by_raw,
               h.change_type, h.previous_selling_price_cents, h.new_selling_price_cents,
               h.previous_availability_status, h.new_availability_status
        FROM scan_events se
        JOIN current_listing_state s ON s.listing_id=se.listing_id
        JOIN competitor_listings l ON l.listing_id=se.listing_id
        JOIN products p ON p.product_id=l.product_id
        LEFT JOIN listing_history h ON h.scan_event_id=se.scan_event_id
        WHERE s.supersession_detected=1 AND s.superseded_by_raw IS NOT NULL
        ORDER BY se.scan_run_id, se.scan_event_id
        """
    ).fetchall()
    findings: list[AuditFinding] = []
    by_run: dict[int, list] = {}
    for row in rows:
        by_run.setdefault(int(row["scan_run_id"]), []).append(row)
    for run_id, run_rows in by_run.items():
        streak: list = []
        previous_target = None
        for row in run_rows:
            target = row["superseded_by_raw"]
            if target == previous_target:
                streak.append(row)
            else:
                if len(streak) >= 3:
                    findings.append(_carry_finding(run_id, previous_target, streak))
                streak = [row]
                previous_target = target
        if len(streak) >= 3:
            findings.append(_carry_finding(run_id, previous_target, streak))
        for row in run_rows:
            unchanged_price = row["previous_selling_price_cents"] == row["new_selling_price_cents"]
            unchanged_availability = row["previous_availability_status"] == row["new_availability_status"]
            if row["change_type"] == "supersession_change" and unchanged_price and unchanged_availability:
                findings.append(
                    AuditFinding(
                        "suspicious_supersession_only_change",
                        "high",
                        f"scan_run_id={run_id} part={row['oem_part_number']} changed only supersession to {row['superseded_by_raw']}",
                    )
                )
    return findings


def _carry_finding(run_id: int, target: str, rows: list) -> AuditFinding:
    parts = ", ".join(row["oem_part_number"] for row in rows)
    first_event = rows[0]["scan_event_id"]
    last_event = rows[-1]["scan_event_id"]
    return AuditFinding(
        "supersession_carry_forward_pattern",
        "critical",
        f"scan_run_id={run_id} repeats superseded_by_raw={target} from event {first_event} through {last_event}: {parts}",
    )


def _discount_price_mismatch(conn) -> list[AuditFinding]:
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(current_listing_state)").fetchall()}
    if not {"reference_price_cents", "price_display_type", "savings_percent"}.issubset(columns):
        return [
            AuditFinding(
                "discount_audit_unavailable_old_schema",
                "info",
                "current_listing_state does not yet have reference_price_cents/savings_percent/price_display_type columns",
            )
        ]
    rows = conn.execute(
        """
        SELECT p.oem_part_number, s.selling_price_cents, s.reference_price_cents, s.savings_percent, s.price_display_type
        FROM current_listing_state s
        JOIN competitor_listings l ON l.listing_id=s.listing_id
        JOIN products p ON p.product_id=l.product_id
        WHERE s.price_display_type='discounted' AND s.reference_price_cents IS NOT NULL
          AND s.selling_price_cents = s.reference_price_cents
        """
    ).fetchall()
    return [
        AuditFinding(
            "discounted_price_equals_reference",
            "critical",
            f"part={row['oem_part_number']} selling_price={cents_to_money(row['selling_price_cents'])} equals reference_price={cents_to_money(row['reference_price_cents'])}",
        )
        for row in rows
    ]


def main() -> int:
    args = parse_args()
    findings = audit_database(args.database)
    print(f"Database: {args.database}")
    print(f"Findings: {len(findings)}")
    for finding in findings:
        print(f"[{finding.severity.upper()}] {finding.code}: {finding.detail}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
