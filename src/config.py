"""Application settings loaded using pydantic-settings."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Configuration loaded from environment variables and .env file."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    base_url: str = "https://integrate.api.nvidia.com/v1"
    api_key: str = "mock-offline-key"
    model_name: str = "nvidia/nemotron-3-super-120b-a12b"
    rules_path: str = "resources/rules.json"
    runbooks_path: str = "resources/runbooks.json"
    timeout: float = 120.0


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the singleton Settings instance."""
    return Settings()
