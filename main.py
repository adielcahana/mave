"""Sample entry point running the minimal Pydantic-AI agent connected to NVIDIA NIM."""

import asyncio
import io
import sys

from src.agent import create_agent
from src.config import get_settings

if sys.platform == "win32" and isinstance(sys.stdout, io.TextIOWrapper):
    sys.stdout.reconfigure(encoding="utf-8")


async def run() -> None:
    settings = get_settings()
    agent = create_agent(settings)

    print(f"Connecting to NVIDIA NIM model: {settings.model_name}...")
    result = await agent.run(
        "Hello! Please confirm connection in one short sentence.", deps=settings
    )
    print("Response:", result.output)


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
