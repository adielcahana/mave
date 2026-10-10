"""Application settings loaded using pydantic-settings."""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Configuration loaded from environment variables and .env file."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    base_url: str
    api_key: str
    model_name: str = "openai/gpt-oss-20b"
    rules_path: str = "rules/rules.json"


def get_settings() -> Settings:
    """Return an instance of Settings."""
    return Settings()
