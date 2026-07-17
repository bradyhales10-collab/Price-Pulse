from __future__ import annotations

from dataclasses import dataclass

from app.internal_sources.base import InternalProductRecord


@dataclass(frozen=True)
class ApiSourceConfig:
    base_url: str
    auth_type: str
    token_env_var_name: str
    page_size: int = 100
    updated_since: str | None = None
    timeout_seconds: int = 30


class ApiInternalProductSource:
    source_type = "api"

    def __init__(self, config: ApiSourceConfig):
        self.config = config
        self.source_name = config.base_url

    def list_available_sources(self) -> list[str]:
        return [self.source_name]

    def validate_connection(self) -> bool:
        raise NotImplementedError("Future API integration is not implemented. Do not add credentials here.")

    def fetch_products(self) -> list[InternalProductRecord]:
        raise NotImplementedError("Future API integration is not implemented. Tokens must come from environment variables later.")

    def normalize_product_record(self, raw: object) -> InternalProductRecord:
        raise NotImplementedError("Future API product normalization has not been implemented.")

    def import_products(self) -> int:
        raise NotImplementedError("Future API import is intentionally disabled until configured and reviewed.")
