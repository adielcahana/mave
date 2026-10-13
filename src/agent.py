import re
import time

from openai import AsyncOpenAI
from pydantic_ai import Agent
from pydantic_ai.models import KnownModelName, Model
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider

from src.config import Settings, get_settings
from src.knowledge.docs_db import (
    list_known_subsystems,
    query_subsystem_docs,
)
from src.rules.models import LogEvent, ProblemResolution, ProblemResolutionWithRegex

__all__ = [
    "SRE_SYSTEM_PROMPT",
    "ProblemResolution",
    "ProblemResolutionWithRegex",
    "analyze_log",
    "create_agent",
    "list_known_subsystems",
    "query_subsystem_docs",
    "reset_circuit_breaker",
    "validate_suggested_regex",
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


def validate_suggested_regex(regex: str, sample_raw_log: str) -> tuple[bool, str]:
    """Validate a suggested regular expression against a sample raw log line.

    Validates:
    1. Syntax: Compiles regex with re.compile.
    2. Match: Checks pattern matches sample_raw_log.
    3. Named groups: Checks (?P<subsystem>...) and (?P<message>...) are in pattern.groupindex.
    4. If all pass, returns (True, "OK").
    """
    try:
        pattern = re.compile(regex)
    except re.error as e:
        return False, f"Invalid regex syntax: {e}"

    if not pattern.search(sample_raw_log):
        return (
            False,
            f"Regex '{regex}' failed to match the incoming raw log line: '{sample_raw_log}'. "
            f"Please check token boundaries and delimiters.",
        )

    found = set(pattern.groupindex.keys())
    required = {"subsystem", "message"}
    missing = required - found
    if missing:
        missing_sorted = sorted(list(missing))
        found_sorted = sorted(list(found))
        return (
            False,
            f"Regex matched line, but is missing required named capture groups: {missing_sorted}. "
            f"Found groups: {found_sorted}. It must contain at least 'subsystem' and 'message'.",
        )

    return True, "OK"


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
            timeout=8.0,
            max_retries=0,
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
    base_prompt = (
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
    if log_event.is_unparsed:
        base_prompt += (
            f"\n4. This log line was UNPARSED by all registered cluster log templates. "
            f"You MUST formulate and provide a valid Python regular expression with named "
            f"capture groups in `suggested_log_template_regex` that accurately parses this "
            f"format. The regex MUST match the raw log line: '{log_event.raw}' and MUST "
            f"include named capture groups (?P<subsystem>...) and (?P<message>...)."
        )
    return base_prompt


_circuit_breaker_open: bool = False
_circuit_breaker_last_failure: float = 0.0
_CIRCUIT_BREAKER_COOLDOWN: float = 30.0


def reset_circuit_breaker() -> None:
    """Reset the LLM inference circuit breaker."""
    global _circuit_breaker_open, _circuit_breaker_last_failure
    _circuit_breaker_open = False
    _circuit_breaker_last_failure = 0.0


async def analyze_log(
    log_event: LogEvent,
    agent: Agent[Settings, ProblemResolution] | None = None,
    settings: Settings | None = None,
) -> ProblemResolution:
    """Analyze an HPC log event and return a grounded ProblemResolution or
    ProblemResolutionWithRegex.

    When log_event.is_unparsed is True:
      - Uses dynamic agent output type ProblemResolutionWithRegex.
      - Validates suggested_log_template_regex against log_event.raw.
      - If validation fails, provides the detailed error message back to the agent
        in a retry loop (up to 2 retries).
      - Returns ProblemResolutionWithRegex when valid.
    When log_event.is_unparsed is False:
      - Uses standard ProblemResolution output without regex requirements.

    Raises:
      RuntimeError: If the LLM circuit breaker is open.
      Exception: If LLM inference fails.
    """
    global _circuit_breaker_open, _circuit_breaker_last_failure

    active_settings = _resolve_settings(settings)
    active_agent = agent if agent is not None else create_agent(settings=active_settings)
    is_live_network = isinstance(active_agent.model, OpenAIChatModel)

    if _circuit_breaker_open:
        now = time.monotonic()
        if now - _circuit_breaker_last_failure < _CIRCUIT_BREAKER_COOLDOWN:
            raise RuntimeError("LLM circuit breaker open: requests throttled after failure")
        _circuit_breaker_open = False

    prompt = _build_incident_prompt(log_event)

    try:
        if not log_event.is_unparsed:
            result = await active_agent.run(prompt, deps=active_settings)
            if is_live_network:
                _circuit_breaker_open = False
            return result.output

        # When log_event.is_unparsed is True:
        # Run with output_type=ProblemResolutionWithRegex and validate regex with retry loop
        result = await active_agent.run(
            prompt,
            deps=active_settings,
            output_type=ProblemResolutionWithRegex,
        )
        current_res = result.output
        valid, err_msg = validate_suggested_regex(
            current_res.suggested_log_template_regex,
            log_event.raw,
        )

        retries = 0
        message_history = result.all_messages()
        while not valid and retries < 2:
            retries += 1
            retry_prompt = (
                f"The suggested_log_template_regex failed validation:\n{err_msg}\n\n"
                f"Raw log line to match: '{log_event.raw}'\n"
                f"Please correct the `suggested_log_template_regex` so it is valid, "
                f"matches the raw log line, and contains named groups (?P<subsystem>...) "
                f"and (?P<message>...)."
            )
            result = await active_agent.run(
                retry_prompt,
                deps=active_settings,
                output_type=ProblemResolutionWithRegex,
                message_history=message_history,
            )
            current_res = result.output
            valid, err_msg = validate_suggested_regex(
                current_res.suggested_log_template_regex,
                log_event.raw,
            )
            message_history = result.all_messages()

        if is_live_network:
            _circuit_breaker_open = False
        return current_res

    except Exception:
        if is_live_network:
            _circuit_breaker_open = True
            _circuit_breaker_last_failure = time.monotonic()
        raise
