from __future__ import annotations

from pathlib import Path

from app.internal_sources.base import InternalProductRecord


class CsvInternalProductSource:
    source_type = "csv_upload"

    def __init__(self, path: Path):
        self.path = path
        self.source_name = path.name

    def list_available_sources(self) -> list[str]:
        return [self.source_name]

    def validate_connection(self) -> bool:
        return self.path.exists() and self.path.suffix.lower() == ".csv"

    def fetch_products(self) -> list[InternalProductRecord]:
        raise NotImplementedError("CSV source is implemented through the dashboard upload/import pipeline.")

    def normalize_product_record(self, raw: object) -> InternalProductRecord:
        raise NotImplementedError("CSV normalization is handled by app.imports for the current upload path.")

    def import_products(self) -> int:
        raise NotImplementedError("Use the existing dashboard upload/import workflow for CSV files.")
