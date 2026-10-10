"""Minimal agent harness example connecting to NVIDIA NIM using pydantic-ai."""

from openai import AsyncOpenAI
from pydantic_ai import Agent
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider

from src.config import Settings, get_settings


def create_agent(settings: Settings | None = None) -> Agent[Settings, str]:
    """Create a minimal Pydantic-AI agent connected to NVIDIA NIM."""
    active_settings = settings or get_settings()

    client = AsyncOpenAI(
        base_url=active_settings.base_url,
        api_key=active_settings.api_key,
    )
    model = OpenAIChatModel(
        active_settings.model_name,
        provider=OpenAIProvider(openai_client=client),
    )

    return Agent(
        model=model,
        deps_type=Settings,
        system_prompt="You are a helpful assistant.",
    )
