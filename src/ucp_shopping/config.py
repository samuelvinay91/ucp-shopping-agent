"""Configuration management for the UCP Shopping Agent."""

from __future__ import annotations

from common.config import Settings as BaseSettings


class Settings(BaseSettings):
    """UCP Shopping Agent configuration.

    Inherits common provider keys and infrastructure settings from
    ``common.config.Settings`` and adds shopping-agent-specific options.
    """

    # Service identity
    service_name: str = "ucp-shopping-agent"
    service_version: str = "0.1.0"
    host: str = "0.0.0.0"
    port: int = 8020

    # LLM configuration
    default_model: str = "gpt-4o-mini"

    # Merchant discovery
    known_merchant_urls: list[str] = [
        "http://localhost:8020/merchants/techzone",
        "http://localhost:8020/merchants/homegoods",
        "http://localhost:8020/merchants/megamart",
    ]
    max_merchants: int = 10

    # Timeouts and limits
    comparison_timeout: int = 30
    discovery_timeout: int = 10
    checkout_timeout: int = 60
    max_results_per_merchant: int = 20

    # Human-in-the-loop
    human_confirmation_required: bool = True

    # Session management
    session_ttl_seconds: int = 3600


def get_settings() -> Settings:
    """Return a cached settings instance."""
    return Settings()
