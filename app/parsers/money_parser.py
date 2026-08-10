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
    # A leading minus sits outside the matched amount, so check the original
    # text: "-$5.00" would otherwise parse as positive 5.
    negative = cleaned.lstrip().startswith("-") or "-$" in cleaned
    try:
        value = Decimal(amount)
        if negative:
            value = -value
    except InvalidOperation:
        return MoneyParseResult(raw=cleaned, value=None, warnings=[warning_code])

    # A competitor price of zero, or below it, is never a real price. It comes
    # from a placeholder, a page that had not finished rendering, or a control
    # whose value defaults to 0. Two parts recorded Chaparral at $0.00 and it
    # then became the lowest competitor price, which would drive a pricing
    # decision to an absurd conclusion. Treated as no price at all, so the
    # missing-price handling applies instead of a wrong number being trusted.
    if value <= 0:
        return MoneyParseResult(raw=cleaned, value=None, warnings=["non_positive_price_rejected"])

    return MoneyParseResult(raw=cleaned, value=value)
