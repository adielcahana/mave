"""Entry point for HPC Log Problem Resolution System."""

import asyncio
import sys

from src.agent import create_agent
from src.config import get_settings

if sys.platform == "win32":
    import io

    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(encoding="utf-8")


async def run() -> None:
    settings = get_settings()
    agent = create_agent(settings)

    sample_log = (
        "460903 resourcemgmtdaeomon node-25 server subsys 1145552216 1 "
        "failed to configure resourcemgmt subsystem err = 10"
    )

    print(f"Analyzing log with model: {settings.model_name}")
    print(f"Log line: {sample_log}\n")

    prompt = (
        "Analyze this HPC log entry, query documentation if needed, "
        f"and suggest remediation: {sample_log}"
    )
    result = await agent.run(prompt, deps=settings)

    print("--- Resolution Output ---")
    print(f"Error Summary: {result.output.error_summary}\n")
    print(f"Root Cause: {result.output.root_cause}\n")
    print(f"Severity: {result.output.severity}\n")
    print("Remediation Steps:")
    for step in result.output.remediation_steps:
        print(f"  - {step}")
    print("\nCitations & Reasons:")
    for citation in result.output.citations_and_reasons:
        print(f"  - {citation}")


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
