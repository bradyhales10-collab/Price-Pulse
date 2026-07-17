from __future__ import annotations

import re
from dataclasses import dataclass, field


SUPERSEDED_BY_RE = re.compile(r"\bSUPERSEDED\s+BY\s+(?P<part>[A-Za-z0-9-]+)", re.IGNORECASE)


@dataclass(frozen=True)
class SupersessionResult:
    supersession_detected: bool = False
    superseded_by_raw: str | None = None
    evidence: list[str] = field(default_factory=list)


def parse_supersession(*, product_name: str | None, page_title: str | None, heading_text: str | None) -> SupersessionResult:
    sources = (
        ("product_name", product_name),
        ("heading_text", heading_text),
        ("page_title", page_title),
    )
    for source_name, source in sources:
        if not source:
            continue
        match = SUPERSEDED_BY_RE.search(source)
        if match:
            return SupersessionResult(
                supersession_detected=True,
                superseded_by_raw=match.group("part"),
                evidence=[f"{source_name}: SUPERSEDED BY {match.group('part')}"],
            )
    return SupersessionResult()
