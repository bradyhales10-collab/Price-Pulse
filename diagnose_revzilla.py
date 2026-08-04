"""Reproduce the RevZilla collector failure and show exactly what is wrong.

Every fix attempt so far has relied on indirect evidence. This calls the
RevZilla collector the same way a real price check does and prints the actual
traceback, plus which file was loaded, so there is no guessing left.

Run it from the Price-Pulse folder:
    .venv\\Scripts\\python.exe diagnose_revzilla.py
"""

from __future__ import annotations

import ast
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def section(title: str) -> None:
    print("")
    print("=" * 60)
    print(f"  {title}")
    print("=" * 60)


def main() -> int:
    section("1. Which collect_parts.py is on disk here")
    target = ROOT / "collect_parts.py"
    if not target.exists():
        print(f"  MISSING: {target}")
        print("  This folder is not a Part Pulse checkout.")
        return 1
    stat = target.stat()
    print(f"  path : {target}")
    print(f"  size : {stat.st_size:,} bytes")
    print(f"  saved: {stat.st_mtime}")

    section("2. Is the function defined at the top level of that file")
    source = target.read_text(encoding="utf-8", errors="replace")
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        print(f"  SYNTAX ERROR in the file itself: {exc}")
        print("  That would break everything. Run Repair Part Pulse.cmd.")
        return 1
    top_level = [node.name for node in tree.body if isinstance(node, ast.FunctionDef)]
    wanted = "collect_one_search_based_part"
    print(f"  top-level functions found: {len(top_level)}")
    print(f"  '{wanted}' present: {wanted in top_level}")
    if wanted not in top_level:
        nested = [n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == wanted]
        print(f"  but found nested (wrong scope) copies: {len(nested)}")

    section("3. Stale compiled caches next to it")
    caches = [c for c in ROOT.rglob("__pycache__") if ".venv" not in c.parts]
    if not caches:
        print("  none found (good)")
    for cache in caches:
        files = list(cache.glob("collect_parts*.pyc"))
        for f in files:
            print(f"  {f}  (saved {f.stat().st_mtime})")
        if not files:
            print(f"  {cache}  (no collect_parts cache inside)")

    section("4. Any OTHER collect_parts.py Python might load instead")
    print("  sys.path entries:")
    for entry in sys.path:
        print(f"    {entry or '(current directory)'}")
    duplicates = []
    for entry in sys.path:
        if not entry:
            continue
        candidate = Path(entry) / "collect_parts.py"
        if candidate.exists() and candidate.resolve() != target.resolve():
            duplicates.append(candidate)
    print(f"  duplicates found elsewhere: {len(duplicates)}")
    for dup in duplicates:
        print(f"    {dup}")

    section("5. Import the module and look for the function")
    try:
        import collect_parts
    except Exception:
        print("  IMPORT FAILED. Full traceback:")
        traceback.print_exc()
        return 1
    loaded_from = getattr(collect_parts, "__file__", "unknown")
    print(f"  imported from: {loaded_from}")
    print(f"  same file as above: {Path(loaded_from).resolve() == target.resolve()}")
    print(f"  has {wanted}: {hasattr(collect_parts, wanted)}")
    print(f"  registered collectors: {sorted(collect_parts.PRODUCTION_COLLECTORS)}")

    section("6. Call the RevZilla collector exactly like a price check does")
    collector = collect_parts.PRODUCTION_COLLECTORS.get("revzilla")
    if collector is None:
        print("  revzilla has no registered collector at all.")
        return 1
    print(f"  collector: {collector}")
    names = getattr(getattr(collector, "__code__", None), "co_names", ())
    print(f"  names it calls: {names}")
    for name in names:
        in_module = hasattr(collect_parts, name)
        print(f"    {name}: {'FOUND' if in_module else 'NOT FOUND in module'}")

    print("")
    print("  Now calling it with deliberately empty arguments. A complaint about")
    print("  arguments or a missing database is EXPECTED and fine. What matters is")
    print("  whether a NameError appears here.")
    print("")
    try:
        collector(None, None, None, None, None, delay_seconds=0)
    except NameError:
        print("  *** REPRODUCED THE BUG. This is the real traceback: ***")
        traceback.print_exc()
        print("")
        print("  The line above that says NameError is the actual cause.")
        return 1
    except Exception as exc:
        print(f"  No NameError. Got {type(exc).__name__} instead, which is expected:")
        print(f"    {exc}")
        print("")
        print("  This means the collector wiring is FINE in this folder.")
        print("  If a price check still reports a NameError, it is not running")
        print("  this copy of the program.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
