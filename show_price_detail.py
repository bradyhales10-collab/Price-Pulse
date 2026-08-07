"""Show why a part's price was not captured for a competitor.

When a page shows more than one different selling price and nothing marks
which is the real one, the parser records no price rather than guess. This
prints every price it found on that page and what it concluded, which is what
is needed to decide the right rule.

    .venv\\Scripts\\python.exe show_price_detail.py partzilla 1333424
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def main() -> int:
    if len(sys.argv) < 3:
        print("Usage: show_price_detail.py <competitor> <OEM part number>")
        print("Example: show_price_detail.py partzilla 1333424")
        return 1
    competitor, part_number = sys.argv[1].strip().lower(), sys.argv[2].strip()

    # Partzilla is the only competitor that signs in, and its collector writes
    # to authenticated_diagnostics rather than <competitor>_collection_diagnostics.
    candidates = [
        ROOT / "data" / "output" / f"{competitor}_collection_diagnostics",
        ROOT / "data" / "output" / "authenticated_diagnostics",
    ]
    base = next((path for path in candidates if path.exists()), candidates[0])
    if not base.exists():
        print(f"No diagnostics found at {base}")
        print("Available diagnostics folders:")
        for folder in sorted((ROOT / "data" / "output").glob("*_collection_diagnostics")):
            print(f"  {folder.name}")
        return 1

    # Folders are named <timestamp>_<part number>; newest first.
    matches = sorted(
        (d for d in base.iterdir() if d.is_dir() and part_number.upper() in d.name.upper()),
        key=lambda d: d.stat().st_mtime,
        reverse=True,
    )
    if not matches:
        print(f"No record found for part {part_number} under {base}")
        print("Most recent parts recorded there:")
        recent = sorted(base.iterdir(), key=lambda d: d.stat().st_mtime, reverse=True)[:10]
        for folder in recent:
            print(f"  {folder.name}")
        return 1

    observation_path = matches[0] / "observation.json"
    if not observation_path.exists():
        print(f"Found {matches[0]} but it has no observation.json")
        return 1

    print(f"Reading: {observation_path}")
    print("")
    data = json.loads(observation_path.read_text(encoding="utf-8"))

    print(f"part:              {data.get('oem_part_number')}")
    print(f"page type:         {data.get('page_classification')}")
    print(f"price recorded:    {data.get('selling_price')}")
    print(f"warnings:          {data.get('parse_warnings') or data.get('warnings')}")
    print("")

    evidence = data.get("raw_evidence_summary") or {}
    candidates = evidence.get("price_candidates") or data.get("price_candidates") or []
    if candidates:
        print(f"Prices found on the page ({len(candidates)}):")
        for candidate in candidates:
            if not isinstance(candidate, dict):
                print(f"  {candidate}")
                continue
            print(
                f"  value={candidate.get('normalized_value') or candidate.get('value')}"
                f"  role={candidate.get('candidate_role')}"
                f"  source={candidate.get('source_type')}"
                f"  label={str(candidate.get('label') or candidate.get('context') or '')[:60]}"
            )
    else:
        # The candidates live in a sibling file written by the same collector,
        # not inside observation.json.
        evidence_path = observation_path.parent / "price_evidence.json"
        if evidence_path.exists():
            print(f"Price detail from: {evidence_path.name}")
            evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
            found = evidence.get("candidates") or evidence.get("price_candidates") or []
            if found:
                print(f"\nPrices found on the page ({len(found)}):")
                for candidate in found:
                    if not isinstance(candidate, dict):
                        print(f"  {candidate}")
                        continue
                    print(
                        f"  value={candidate.get('normalized_value') or candidate.get('raw_text')}"
                        f"  role={candidate.get('candidate_role')}"
                        f"  source={candidate.get('source_type')}"
                    )
                    context = candidate.get("context_text") or candidate.get("raw_text") or ""
                    if context:
                        print(f"        context: {str(context)[:100]}")
            for line in evidence.get("explanation") or []:
                print(f"  - {line}")
        else:
            print("No price candidate detail was recorded. Full observation:")
            print(json.dumps(data, indent=2)[:3000])

    explanation = evidence.get("explanation") or data.get("explanation")
    if explanation:
        print("")
        print("Why it decided that:")
        for line in explanation if isinstance(explanation, list) else [explanation]:
            print(f"  - {line}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
