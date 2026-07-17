from __future__ import annotations

import argparse
import json
import shutil
from dataclasses import dataclass
from pathlib import Path

from app.config import DATA_DIR


DELETE_CONFIRMATION_TEXT = "DELETE EMPTY CART PROBE ARTIFACTS"
RESULT_FILENAMES = {"cart_probe_metadata.json", "cart_probe_summary.csv", "cart_probe_review.txt", "folder_audit.json"}
EVIDENCE_FILENAMES = {"cart_action_used.json", "cart_line_evidence.txt", "cart_price_evidence.json", "cleanup_evidence.txt"}


@dataclass(frozen=True)
class CartProbeOutputInspection:
    folder_path: str
    folder_name: str
    created_time: float
    modified_time: float
    metadata_exists: bool
    diagnose_cart_action_only: bool | None
    dry_run_structure: bool | None
    requested_max_parts: int | None
    attempted_parts: int | None
    directories_created_count: int | None
    max_total_output_directories_created: int | None
    immediate_subfolder_count: int
    file_count: int
    appears_empty: bool
    possible_loop_artifact: bool
    likely_reason: str
    folder_guard_passed: bool | None
    protected_files_present: bool


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Inspect experimental MotoSport cart probe output folders.")
    parser.add_argument("--competitor", required=True)
    parser.add_argument("--delete-empty-loop-artifacts", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--base-dir", type=Path, default=DATA_DIR / "output" / "competitor_probes" / "motosport_cart")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.competitor != "motosport":
        print("Error: this helper currently supports MotoSport only.")
        return 1
    inspections = inspect_output_folders(args.base_dir)
    deleted: list[str] = []
    delete_candidates = deletion_candidates(inspections)
    if args.delete_empty_loop_artifacts:
        print(json.dumps({"would_delete": delete_candidates}, indent=2))
        if args.dry_run:
            print(json.dumps({"folders": [item.__dict__ for item in inspections], "deleted": deleted, "dry_run": True}, indent=2))
            return 0
        confirmation = input(f"Type {DELETE_CONFIRMATION_TEXT} to delete empty artifacts: ").strip()
        if confirmation != DELETE_CONFIRMATION_TEXT:
            print("Cleanup was not confirmed. No folders were deleted.")
            print(json.dumps({"folders": [item.__dict__ for item in inspections], "deleted": deleted}, indent=2))
            return 1
        deleted = delete_empty_loop_artifacts(inspections, confirmation=confirmation)
    print(json.dumps({"folders": [item.__dict__ for item in inspections], "deleted": deleted}, indent=2))
    return 0


def inspect_output_folders(base_dir: Path, *, limit: int = 25) -> list[CartProbeOutputInspection]:
    if not base_dir.exists():
        return []
    folders = [path for path in base_dir.iterdir() if path.is_dir()]
    folders.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    return [inspect_one_folder(path) for path in folders[:limit]]


def inspect_one_folder(folder: Path) -> CartProbeOutputInspection:
    files = [path for path in folder.rglob("*") if path.is_file()]
    direct_files = {path.name for path in folder.iterdir() if path.is_file()}
    part_folders = [path for path in folder.iterdir() if path.is_dir()]
    metadata_exists = "cart_probe_metadata.json" in direct_files
    metadata = _read_json(folder / "cart_probe_metadata.json") if metadata_exists else {}
    folder_audit = _read_json(folder / "folder_audit.json") if "folder_audit.json" in direct_files else metadata.get("folder_audit") or {}
    all_file_names = {path.name for path in files}
    protected_files_present = bool(RESULT_FILENAMES.intersection(all_file_names) or EVIDENCE_FILENAMES.intersection(all_file_names))
    appears_empty = len(files) == 0
    incomplete_initialized_run = metadata_exists and metadata.get("status") == "initialized" and not folder_audit
    possible_loop_artifact = appears_empty or not metadata_exists or incomplete_initialized_run or metadata_exceeds_directory_limit(metadata, folder_audit, len(part_folders))
    likely_reason = classify_folder(metadata_exists, metadata, folder_audit, appears_empty, possible_loop_artifact, len(part_folders), protected_files_present)
    return CartProbeOutputInspection(
        folder_path=str(folder),
        folder_name=folder.name,
        created_time=folder.stat().st_ctime,
        modified_time=folder.stat().st_mtime,
        metadata_exists=metadata_exists,
        diagnose_cart_action_only=metadata.get("diagnose_cart_action_only") if metadata_exists else None,
        dry_run_structure=metadata.get("dry_run_structure") if metadata_exists else None,
        requested_max_parts=metadata.get("requested_max_parts") if metadata_exists else None,
        attempted_parts=metadata.get("attempted_parts") if metadata_exists else None,
        directories_created_count=metadata.get("directories_created_count") if metadata_exists else None,
        max_total_output_directories_created=metadata.get("max_total_output_directories_created") if metadata_exists else None,
        immediate_subfolder_count=len(part_folders),
        file_count=len(files),
        appears_empty=appears_empty,
        possible_loop_artifact=possible_loop_artifact,
        likely_reason=likely_reason,
        folder_guard_passed=folder_audit.get("folder_guard_passed") if folder_audit else None,
        protected_files_present=protected_files_present,
    )


def deletion_candidates(inspections: list[CartProbeOutputInspection]) -> list[str]:
    return [
        item.folder_path
        for item in inspections
        if item.possible_loop_artifact
        and not item.metadata_exists
        and item.appears_empty
        and item.file_count == 0
        and not item.protected_files_present
    ]


def delete_empty_loop_artifacts(inspections: list[CartProbeOutputInspection], *, confirmation: str | None = None, dry_run: bool = False) -> list[str]:
    if confirmation != DELETE_CONFIRMATION_TEXT:
        return []
    if dry_run:
        return []
    deleted: list[str] = []
    for item in inspections:
        folder = Path(item.folder_path)
        if item.folder_path in deletion_candidates(inspections) and folder.exists():
            shutil.rmtree(folder)
            deleted.append(str(folder))
    return deleted


def metadata_exceeds_directory_limit(metadata: dict[str, object], folder_audit: dict[str, object], immediate_subfolder_count: int) -> bool:
    directories_created = metadata.get("directories_created_count")
    max_directories = metadata.get("max_total_output_directories_created")
    if isinstance(directories_created, int) and isinstance(max_directories, int) and directories_created > max_directories:
        return True
    if folder_audit and folder_audit.get("folder_guard_passed") is False:
        return True
    requested_max = metadata.get("requested_max_parts")
    if isinstance(requested_max, int) and immediate_subfolder_count > requested_max:
        return True
    return False


def classify_folder(
    metadata_exists: bool,
    metadata: dict[str, object],
    folder_audit: dict[str, object],
    appears_empty: bool,
    possible_loop_artifact: bool,
    immediate_subfolder_count: int,
    protected_files_present: bool,
) -> str:
    if appears_empty:
        return "empty_folder_no_metadata"
    if not metadata_exists:
        return "old_failed_probe_artifact" if protected_files_present else "missing_metadata_possible_interrupted_run"
    if metadata.get("status") == "initialized" and not folder_audit:
        return "incomplete_initialized_run"
    if metadata_exceeds_directory_limit(metadata, folder_audit, immediate_subfolder_count):
        if folder_audit and folder_audit.get("folder_guard_passed") is False:
            return "exceeds_directory_limit"
        return "multiple_product_folders_unexpected"
    if metadata.get("diagnose_cart_action_only"):
        return "valid_diagnostic_run"
    if metadata.get("dry_run_structure"):
        return "valid_dry_run_structure"
    if metadata.get("experimental_cart_pricing"):
        return "valid_real_cart_probe"
    return "old_failed_probe_artifact" if possible_loop_artifact else "valid_real_cart_probe"


def _read_json(path: Path) -> dict[str, object]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


if __name__ == "__main__":
    raise SystemExit(main())
