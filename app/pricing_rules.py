from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any

from app.database import connect_database, utc_now


@dataclass(frozen=True)
class PricingRule:
    rule_code: str
    rule_name: str
    rule_type: str
    is_enabled: bool
    priority: int
    settings: dict[str, Any]
    description: str


PRICING_RULE_PRESETS: dict[str, dict[str, Any]] = {
    "conservative": {
        "name": "Conservative",
        "description": "Stay close to the market while protecting a stronger margin.",
        "rules": {
            "skip_unsafe_competitor_data": {"enabled": True},
            "use_lowest_competitor": {"enabled": True, "setting_value": "0"},
            "round_to_99": {"enabled": True, "setting_value": "99"},
            "protect_minimum_margin": {"enabled": True, "setting_value": "30"},
        },
    },
    "match_market": {
        "name": "Match Market",
        "description": "Use the lowest reliable competitor price, round cleanly, and keep the standard margin guardrail.",
        "rules": {
            "skip_unsafe_competitor_data": {"enabled": True},
            "use_lowest_competitor": {"enabled": True, "setting_value": "0"},
            "round_to_99": {"enabled": True, "setting_value": "99"},
            "protect_minimum_margin": {"enabled": True, "setting_value": "20"},
        },
    },
    "protect_margin": {
        "name": "Protect Margin",
        "description": "Prioritize margin protection when competitor prices are unusually low.",
        "rules": {
            "skip_unsafe_competitor_data": {"enabled": True},
            "use_lowest_competitor": {"enabled": True, "setting_value": "0"},
            "round_to_99": {"enabled": True, "setting_value": "99"},
            "protect_minimum_margin": {"enabled": True, "setting_value": "35"},
        },
    },
    "aggressive": {
        "name": "Aggressive",
        "description": "Price slightly below the lowest reliable competitor while keeping a minimum margin floor.",
        "rules": {
            "skip_unsafe_competitor_data": {"enabled": True},
            "use_lowest_competitor": {"enabled": True, "setting_value": "-50"},
            "round_to_99": {"enabled": True, "setting_value": "99"},
            "protect_minimum_margin": {"enabled": True, "setting_value": "20"},
        },
    },
}


def list_pricing_rules(database: Path, *, enabled_only: bool = False) -> list[PricingRule]:
    with connect_database(database) as conn:
        where = "WHERE is_enabled=1" if enabled_only else ""
        rows = conn.execute(
            f"""
            SELECT rule_code, rule_name, rule_type, is_enabled, priority, settings_json, description
            FROM pricing_rules
            {where}
            ORDER BY priority, rule_name COLLATE NOCASE
            """
        ).fetchall()
    return [
        PricingRule(
            rule_code=row["rule_code"],
            rule_name=row["rule_name"],
            rule_type=row["rule_type"],
            is_enabled=bool(row["is_enabled"]),
            priority=int(row["priority"]),
            settings=_settings(row["settings_json"]),
            description=row["description"] or "",
        )
        for row in rows
    ]


def list_pricing_rule_presets() -> list[dict[str, Any]]:
    return [{"preset_code": code, **preset} for code, preset in PRICING_RULE_PRESETS.items()]


def apply_pricing_rule_preset(database: Path, preset_code: str) -> None:
    if preset_code not in PRICING_RULE_PRESETS:
        raise ValueError("Pricing rule preset not found.")
    for rule_code, settings in PRICING_RULE_PRESETS[preset_code]["rules"].items():
        update_pricing_rule(
            database,
            rule_code=rule_code,
            is_enabled=bool(settings.get("enabled")),
            setting_value=str(settings.get("setting_value", "")),
        )


def update_pricing_rule(database: Path, *, rule_code: str, is_enabled: bool, setting_value: str = "") -> None:
    rules = {rule.rule_code: rule for rule in list_pricing_rules(database)}
    if rule_code not in rules:
        raise ValueError("Pricing rule not found.")
    rule = rules[rule_code]
    settings = dict(rule.settings)
    if rule.rule_type == "margin_floor":
        settings["minimum_margin_pct"] = _json_number(_decimal_setting(setting_value, "Minimum margin"))
    elif rule.rule_type == "rounding":
        ending = int(_decimal_setting(setting_value, "Ending cents"))
        if ending < 0 or ending > 99:
            raise ValueError("Ending cents must be between 0 and 99.")
        settings["ending_cents"] = ending
    elif rule.rule_type == "anchor":
        settings["adjustment_cents"] = int(_decimal_setting(setting_value, "Adjustment cents"))
    with connect_database(database) as conn:
        conn.execute(
            """
            UPDATE pricing_rules
            SET is_enabled=?, settings_json=?, updated_at=?
            WHERE rule_code=?
            """,
            (1 if is_enabled else 0, json.dumps(settings, sort_keys=True), utc_now(), rule_code),
        )


def suggest_price(row: dict[str, Any], rules: list[PricingRule], *, selected_rule_codes: set[str] | None = None) -> dict[str, Any]:
    selected = selected_rule_codes if selected_rule_codes is not None else {rule.rule_code for rule in rules if rule.is_enabled}
    applied: list[dict[str, Any]] = []
    proposed = _money(row.get("our_current_price"))
    lowest = _money(row.get("lowest_competitor_price"))
    cost = _money(row.get("current_cost"))

    for rule in rules:
        if not rule.is_enabled:
            continue
        is_selected = rule.rule_code in selected
        effect = "Not applied"
        before = proposed
        if is_selected:
            if rule.rule_type == "guardrail":
                if lowest is None:
                    effect = "No suggestion because no reliable competitor price is available."
                    proposed = None
            elif rule.rule_type == "anchor" and lowest is not None:
                adjustment = Decimal(rule.settings.get("adjustment_cents", 0)) / Decimal("100")
                proposed = lowest + adjustment
                effect = f"Started from lowest competitor price ${_format_money(lowest)}."
            elif rule.rule_type == "rounding" and proposed is not None:
                proposed = _round_to_ending(proposed, int(rule.settings.get("ending_cents", 99)))
                effect = f"Rounded from ${_format_money(before)} to ${_format_money(proposed)}."
            elif rule.rule_type == "margin_floor" and proposed is not None and cost is not None:
                minimum = Decimal(str(rule.settings.get("minimum_margin_pct", 20))) / Decimal("100")
                floor = (cost / (Decimal("1") - minimum)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
                if proposed < floor:
                    proposed = floor
                    effect = f"Raised to ${_format_money(floor)} to protect {rule.settings.get('minimum_margin_pct', 20)}% margin."
                else:
                    effect = f"Margin is already above {rule.settings.get('minimum_margin_pct', 20)}%."
        applied.append(
            {
                "rule_code": rule.rule_code,
                "rule_name": rule.rule_name,
                "selected": is_selected,
                "effect": effect,
            }
        )

    return {
        "suggested_price": _format_money(proposed) if proposed is not None else "",
        "applied_rules": applied,
        "selected_rule_codes": [item["rule_code"] for item in applied if item["selected"]],
    }


def _settings(value: str) -> dict[str, Any]:
    try:
        parsed = json.loads(value or "{}")
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _decimal_setting(value: str, label: str) -> Decimal:
    try:
        return Decimal(value.strip())
    except (InvalidOperation, AttributeError) as exc:
        raise ValueError(f"{label} must be a valid number.") from exc


def _json_number(value: Decimal) -> int | float:
    return int(value) if value == value.to_integral_value() else float(value)


def _money(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return None


def _format_money(value: Decimal | None) -> str:
    return "" if value is None else format(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP), "f")


def _round_to_ending(value: Decimal, ending_cents: int) -> Decimal:
    dollars = int(value)
    candidates = [
        Decimal(dollars) + (Decimal(ending_cents) / Decimal("100")),
        Decimal(max(0, dollars - 1)) + (Decimal(ending_cents) / Decimal("100")),
        Decimal(dollars + 1) + (Decimal(ending_cents) / Decimal("100")),
    ]
    positive = [candidate for candidate in candidates if candidate > 0]
    return min(positive, key=lambda candidate: abs(candidate - value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
