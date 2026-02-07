"""Centralized configuration management using pydantic-settings."""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Base settings shared across all projects."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # LLM Providers
    openai_api_key: str = ""
    anthropic_api_key: str = ""
    google_api_key: str = ""
    cohere_api_key: str = ""
    huggingface_token: str = ""

    # Infrastructure
    database_url: str = "postgresql://aiportfolio:localdev@localhost:5432/aiportfolio"
    redis_url: str = "redis://localhost:6379/0"
    qdrant_url: str = "http://localhost:6333"

    # Search
    tavily_api_key: str = ""
    serper_api_key: str = ""

    # Image Generation
    replicate_api_token: str = ""
    stability_api_key: str = ""

    # Observability
    langchain_tracing_v2: bool = False
    langchain_api_key: str = ""
    langchain_project: str = "ai-engineer-portfolio"

    # Application
    log_level: str = "INFO"
    environment: str = "development"
