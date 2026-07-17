from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation


MONEY_RE = re.compile(r"\$\s*(?P<amount>\d{1,3}(?:,\d{3})*(?:\.\d{2})?|\d+(?:\.\d{2})?)")


@dataclass(frozen=True)
class MoneyParseResult:
    raw: str | None
    value: Decimal | None
    warnings: list[str] = field(default_factory=list)


def parse_money(raw: str | None, warning_code: str = "money_parse_failed") -> MoneyParseResult:
    if raw is None:
        return MoneyParseResult(raw=None, value=None)

    cleaned = raw.strip()
    match = MONEY_RE.search(cleaned)
    if not match:
        return MoneyParseResult(raw=cleaned, value=None, warnings=[warning_code])

    amount = match.group("amount").replace(",", "")
    try:
        return MoneyParseResult(raw=cleaned, value=Decimal(amount))
    except InvalidOperation:
        return MoneyParseResult(raw=cleaned, value=None, warnings=[warning_code])
