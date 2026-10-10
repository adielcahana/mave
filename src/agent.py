"""HPC Log analysis agent harness using pydantic-ai."""

from openai import AsyncOpenAI
from pydantic import BaseModel, Field
from pydantic_ai import Agent, RunContext
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider

from src.config import Settings, get_settings


class ProblemResolution(BaseModel):
    """Structured resolution output for an HPC error."""

    error_summary: str = Field(description="Clear explanation of the error identified in the log")
    root_cause: str = Field(description="Probable root cause based on log context and knowledge")
    severity: str = Field(description="Severity level: CRITICAL, HIGH, MEDIUM, LOW")
    remediation_steps: list[str] = Field(description="Actionable steps to resolve the issue")
    citations_and_reasons: list[str] = Field(
        description="Supporting reasons and citations/references"
    )


def create_agent(settings: Settings | None = None) -> Agent[Settings, ProblemResolution]:
    """Create and configure the pydantic-ai agent for HPC log analysis."""
    active_settings = settings or get_settings()

    client = AsyncOpenAI(
        base_url=active_settings.base_url,
        api_key=active_settings.api_key,
    )
    model = OpenAIChatModel(
        active_settings.model_name,
        provider=OpenAIProvider(openai_client=client),
    )

    agent: Agent[Settings, ProblemResolution] = Agent(
        model=model,
        deps_type=Settings,
        output_type=ProblemResolution,
        retries=3,
        system_prompt=(
            "You are an expert High Performance Computing (HPC) Site Reliability Engineer.\n"
            "Analyze the given HPC log message, consult available subsystem documentation "
            "using tools if needed, and respond with a structured ProblemResolution object.\n"
            "Ensure all fields are filled:\n"
            "- error_summary: concise summary of the error\n"
            "- root_cause: root cause explanation\n"
            "- severity: CRITICAL, HIGH, MEDIUM, or LOW\n"
            "- remediation_steps: list of clear actionable steps\n"
            "- citations_and_reasons: list of references/citations and justification"
        ),
    )

    @agent.tool
    async def query_subsystem_docs(ctx: RunContext[Settings], subsystem: str) -> str:
        """Query documentation for a specific HPC subsystem or daemon."""
        docs_db = {
            "resourcemgmtdaeomon": (
                "Subsystem: Resource Management Daemon. "
                "Error 10 indicates inability to initialize subsystem configuration "
                "or communicate with the cluster node controller. "
                "Remediation: Verify cluster daemon configuration in /etc/sysconfig, "
                "check node-controller connectivity, and verify memory/resource limits."
            ),
            "slurm": (
                "Subsystem: Slurm Workload Manager. "
                "Coordinates resource allocation and job scheduling across cluster nodes."
            ),
        }
        default_msg = (
            f"No specific documentation found for subsystem '{subsystem}'. "
            "Check standard HPC cluster telemetry."
        )
        return docs_db.get(subsystem.lower(), default_msg)

    return agent
