from __future__ import annotations

from pathlib import Path

from app.internal_sources.base import InternalProductRecord


class ExcelInternalProductSource:
    source_type = "excel_upload"

    def __init__(self, path: Path):
        self.path = path
        self.source_name = path.name

    def list_available_sources(self) -> list[str]:
        return [self.source_name]

    def validate_connection(self) -> bool:
        return self.path.exists() and self.path.suffix.lower() == ".xlsx"

    def fetch_products(self) -> list[InternalProductRecord]:
        raise NotImplementedError("Excel source is implemented through the dashboard upload/import pipeline.")

    def normalize_product_record(self, raw: object) -> InternalProductRecord:
        raise NotImplementedError("Excel normalization is handled by app.imports for the current upload path.")

    def import_products(self) -> int:
        raise NotImplementedError("Use the existing dashboard upload/import workflow for Excel files.")
