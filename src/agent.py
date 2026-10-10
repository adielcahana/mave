"""Production SRE Resolution Agent connecting to NVIDIA NIM with rich domain knowledge tools."""

from openai import AsyncOpenAI
from pydantic_ai import Agent
from pydantic_ai.models import KnownModelName, Model
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider

from src.config import Settings, get_settings
from src.knowledge.docs_db import (
    get_all_runbooks,
    list_known_subsystems,
    query_subsystem_docs,
)
from src.rules.models import LogEvent, ProblemResolution

__all__ = [
    "SRE_SYSTEM_PROMPT",
    "ProblemResolution",
    "analyze_log",
    "create_agent",
    "list_known_subsystems",
    "query_subsystem_docs",
]

SRE_SYSTEM_PROMPT = """You are an expert High Performance Computing (HPC) SRE.
Investigate cluster failure modes, determine root causes, assess incident severity,
and provide actionable, authoritative remediation steps grounded in official cluster runbooks.

Operational Guidelines:
1. Grounded Runbook Investigation:
   - Before forming any diagnosis, query runbooks using `query_subsystem_docs`.
   - Use incident subsystem name and symptom keywords (error codes, daemons, hardware).
   - If the subsystem name is ambiguous or unknown, query `list_known_subsystems` first.

2. Diagnostic Rigor:
   - Formulate a concise error summary and identify precise technical root cause from runbooks.
   - Assign an accurate severity level: CRITICAL, HIGH, MEDIUM, LOW, or INFO.

3. Actionable Remediation:
   - Provide clear, numbered, step-by-step remediation procedures to recover the cluster safely.
   - Prevent cascading failures (unmount corrupted domains, isolate flapping links, reset daemons).

4. Strict Citation Requirements:
   - Every resolution MUST cite specific runbooks and rationale in `citations_and_reasons`.
   - Use the official citation identifier (e.g., "[LANL HPC Runbook: ...]") and technical reasons.

You must always structure your output as a ProblemResolution object.
"""


def _resolve_settings(settings: Settings | None = None) -> Settings:
    """Resolve active configuration settings with fallback for offline test environments."""
    if settings is not None:
        return settings
    try:
        return get_settings()
    except Exception:
        return Settings(
            base_url="https://integrate.api.nvidia.com/v1",
            api_key="mock-offline-key",
            model_name="openai/gpt-oss-20b",
        )


def create_agent(
    settings: Settings | None = None,
    model: Model | KnownModelName | str | None = None,
) -> Agent[Settings, ProblemResolution]:
    """Create and configure the production Pydantic-AI SRE Resolution Agent.

    Configured with NVIDIA NIM inference and domain tools (query_subsystem_docs,
    list_known_subsystems) to investigate novel cluster error events.
    """
    active_settings = _resolve_settings(settings)

    if model is not None:
        chosen_model: Model | KnownModelName | str = model
    else:
        client = AsyncOpenAI(
            base_url=active_settings.base_url,
            api_key=active_settings.api_key,
        )
        chosen_model = OpenAIChatModel(
            active_settings.model_name,
            provider=OpenAIProvider(openai_client=client),
        )

    agent: Agent[Settings, ProblemResolution] = Agent(
        model=chosen_model,
        deps_type=Settings,
        output_type=ProblemResolution,
        system_prompt=SRE_SYSTEM_PROMPT,
    )
    agent.tool_plain(query_subsystem_docs)
    agent.tool_plain(list_known_subsystems)

    # Attach backwards-compatible tool mapping attribute
    object.__setattr__(agent, "_function_tools", agent._function_toolset.tools)

    return agent


def _build_incident_prompt(log_event: LogEvent) -> str:
    """Format structured LogEvent fields into an incident prompt for SRE agent investigation."""
    return (
        f"Investigate the following HPC cluster incident and formulate a grounded resolution:\n"
        f"- Log ID: {log_event.log_id}\n"
        f"- Node: {log_event.node}\n"
        f"- Subsystem: {log_event.subsystem}\n"
        f"- Event Type: {log_event.event_type}\n"
        f"- Timestamp: {log_event.timestamp}\n"
        f"- Flag: {log_event.flag}\n"
        f"- Message: {log_event.message}\n"
        f"- Raw Log: {log_event.raw}\n\n"
        f"Instructions:\n"
        f"1. Query subsystem runbooks via `query_subsystem_docs` "
        f"(subsystem='{log_event.subsystem}', keyword='{log_event.event_type}').\n"
        f"2. Diagnose the root cause and determine severity (CRITICAL, HIGH, MEDIUM, LOW, INFO).\n"
        f"3. Provide ordered remediation steps and cite specific runbooks in citations_and_reasons."
    )


def _fallback_offline_resolution(log_event: LogEvent, exc: Exception) -> ProblemResolution:
    """Generate a grounded ProblemResolution fallback using local runbooks when offline."""
    # Look for matching runbooks in the local database
    normalized_subsys = log_event.subsystem.strip().lower()
    matching_entries = [
        entry
        for entry in get_all_runbooks()
        if entry.subsystem == normalized_subsys
        or any(kw in log_event.message.lower() for kw in entry.keywords)
    ]

    if matching_entries:
        entry = matching_entries[0]
        severity = "CRITICAL" if "panic" in log_event.message.lower() else "HIGH"
        return ProblemResolution(
            error_summary=f"[{log_event.node}] {entry.scenario} ({log_event.message})",
            root_cause=entry.root_cause,
            severity=severity,
            remediation_steps=list(entry.remediation_steps),
            citations_and_reasons=[
                f"{entry.citation}: grounded fallback for {log_event.subsystem} (offline: {exc})"
            ],
        )

    # General fallback for undocumented subsystems
    docs_snippet = query_subsystem_docs(log_event.subsystem, log_event.message)
    trimmed_snippet = docs_snippet[:120].strip()
    return ProblemResolution(
        error_summary=f"[{log_event.node}] Incident in {log_event.subsystem}: {log_event.message}",
        root_cause=f"Subsystem error in {log_event.subsystem}. Excerpt: {trimmed_snippet}...",
        severity="MEDIUM",
        remediation_steps=[
            f"Inspect system event log on node {log_event.node}.",
            f"Query subsystem status for {log_event.subsystem}.",
            "Verify network reachability and daemon responsiveness.",
        ],
        citations_and_reasons=[
            f"[LANL HPC Knowledge Base: {log_event.subsystem}]: offline triage resolution"
        ],
    )


async def analyze_log(
    log_event: LogEvent,
    agent: Agent[Settings, ProblemResolution] | None = None,
    settings: Settings | None = None,
) -> ProblemResolution:
    """Analyze an HPC log event and return a grounded ProblemResolution.

    Convenience helper that builds an incident investigation prompt from the
    LogEvent fields (node, subsystem, event_type, message, timestamp, log_id)
    and executes the SRE agent. Gracefully handles mock/offline environments.
    """
    active_settings = _resolve_settings(settings)
    active_agent = agent if agent is not None else create_agent(settings=active_settings)

    prompt = _build_incident_prompt(log_event)

    try:
        result = await active_agent.run(prompt, deps=active_settings)
        return result.output
    except Exception as exc:
        return _fallback_offline_resolution(log_event, exc)
