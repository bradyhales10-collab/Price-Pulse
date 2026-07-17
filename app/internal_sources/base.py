from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class InternalProductRecord:
    internal_sku: str
    manufacturer: str
    oem_part_number: str
    our_current_price: Decimal
    product_name: str = ""
    current_cost: Decimal | None = None
    product_category: str = ""
    units_sold_12m: int | None = None
    inventory_qty: int | None = None
    scan_priority: str = ""
    is_active: bool = True
    source_type: str = ""
    source_name: str = ""
    external_sync_id: str | None = None


@runtime_checkable
class InternalProductSource(Protocol):
    source_type: str
    source_name: str

    def list_available_sources(self) -> list[str]: ...

    def validate_connection(self) -> bool: ...

    def fetch_products(self) -> list[InternalProductRecord]: ...

    def normalize_product_record(self, raw: object) -> InternalProductRecord: ...

    def import_products(self) -> int: ...
