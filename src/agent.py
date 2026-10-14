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
from src.knowledge.search import (
    read_local_file,
    search_external_knowledge,
    search_local_docs,
    search_web_docs,
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
    "read_local_file",
    "reset_circuit_breaker",
    "search_external_knowledge",
    "search_local_docs",
    "search_web_docs",
    "validate_suggested_regex",
]

SRE_SYSTEM_PROMPT = """You are an expert High Performance Computing (HPC) SRE.
Investigate cluster failure modes, determine root causes, assess incident severity,
and provide actionable, authoritative remediation steps grounded in official cluster runbooks.

Operational Guidelines:
1. Grounded Runbook & Local Investigation:
   - Always search local documentation first before escalating to web search.
   - Query cluster runbooks using `query_subsystem_docs`.
   - Use `search_local_docs` with grep-style regex queries for text/markdown docs or jq-style
     filters (e.g. `.subsystem == "unix.hw"`, `keywords=panic`) for JSON runbooks/rules.
   - Use `read_local_file` with line numbers as follow-up to inspect surrounding context,
     full remediation instructions, or specific sections around matched lines.
   - If the subsystem name is ambiguous or unknown, query `list_known_subsystems` first.

2. Web Search Fallback:
   - Only if local runbooks and local documentation do not contain sufficient guidance,
     query `search_web_docs` to search external technical documentation and web guides.

3. Diagnostic Rigor:
   - Formulate a concise error summary and identify precise technical root cause from runbooks/docs.
   - Assign an accurate severity level: CRITICAL, HIGH, MEDIUM, LOW, or INFO.

4. Actionable Remediation:
   - Provide clear, numbered, step-by-step remediation procedures to recover the cluster safely.
   - Prevent cascading failures (unmount corrupted domains, isolate flapping links, reset daemons).

5. Strict Citation Requirements:
   - Every resolution MUST cite specific runbooks or sources in `citations_and_reasons`.
   - Use the official citation identifier (e.g., "[LANL HPC Runbook: ...]" or "[Web Source: ...]")
     and technical reasons.

You must always structure your output as a ProblemResolution object.
"""


def validate_suggested_regex(
    regex: str,
    sample_raw_log: str,
    format_string: str = "",
) -> tuple[bool, str]:
    """Validate a suggested regex and format string against a sample raw log line.

    Validates:
    1. Syntax: Compiles regex with re.compile.
    2. Match: Checks pattern matches sample_raw_log.
    3. Named groups: Checks at least one named capture group is defined.
    4. Format string (if provided): Checks format_string does not reference missing groups.
    """
    try:
        pattern = re.compile(regex)
    except re.error as e:
        return False, f"Invalid regex syntax: {e}"

    match = pattern.search(sample_raw_log)
    if not match:
        return (
            False,
            f"Regex '{regex}' failed to match the incoming raw log line: '{sample_raw_log}'. "
            f"Please check token boundaries and delimiters.",
        )

    if not pattern.groupindex:
        return (
            False,
            "Regex matched line, but contains no named capture groups. "
            "Please define named capture groups like (?P<token_name>...).",
        )

    if format_string:
        try:
            format_string.format_map(match.groupdict())
        except KeyError as e:
            return (
                False,
                f"Format string references group '{e}' which is not in the regex capture groups: "
                f"{sorted(pattern.groupindex.keys())}.",
            )
        except Exception as e:
            return False, f"Invalid format string: {e}"

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
            timeout=active_settings.timeout,
            max_retries=2,
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
        retries=3,
    )
    agent.tool_plain(query_subsystem_docs)
    agent.tool_plain(list_known_subsystems)
    agent.tool_plain(search_local_docs)
    agent.tool_plain(read_local_file)
    agent.tool_plain(search_web_docs)

    # Attach backwards-compatible tool mapping attribute
    object.__setattr__(agent, "_function_tools", agent._function_toolset.tools)

    return agent


def _build_incident_prompt(
    log_event: LogEvent,
    known_groups: dict[str, dict[str, str]] | list[str] | None = None,
    known_group_names: list[str] | None = None,
) -> str:
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
        f"- Raw Log: {log_event.raw}\n"
    )
    if log_event.model_extra:
        for k, v in log_event.model_extra.items():
            base_prompt += f"- {k}: {v}\n"

    base_prompt += (
        f"\nInstructions:\n"
        f"1. First search local knowledge using `query_subsystem_docs` "
        f"(subsystem='{log_event.subsystem}', keyword='{log_event.event_type}')\n"
        f"   or `search_local_docs` (grep regex on text docs, jq filters on JSON files).\n"
        f"   Use `read_local_file` with line numbers to follow up on matched files and sections.\n"
        f"2. Only if local search yields no relevant match, query `search_web_docs` for "
        f"external technical guides.\n"
        f"3. Diagnose the root cause and determine severity (CRITICAL, HIGH, MEDIUM, LOW, INFO).\n"
        f"4. Provide ordered remediation steps and cite specific runbooks or web sources "
        f"in citations_and_reasons."
    )
    if log_event.is_unparsed:
        groups_data = known_groups if known_groups is not None else known_group_names
        base_prompt += (
            "\n4. This log line was UNPARSED by all registered cluster log templates.\n"
            "You MUST formulate a Python regex with named capture groups (?P<name>...)\n"
            f"in `suggested_log_template_regex` that accurately parses: '{log_event.raw}'.\n"
            "You may define whatever named capture groups are appropriate for this log line.\n"
        )
        if isinstance(groups_data, dict) and groups_data:
            lines: list[str] = []
            for grp, info in sorted(groups_data.items()):
                pat = info.get("pattern", r"\S+")
                ex = info.get("example", "")
                if ex:
                    lines.append(f"   - `(?P<{grp}>{pat})` (e.g., '{ex}')")
                else:
                    lines.append(f"   - `(?P<{grp}>{pat})`")
            guide = "\n".join(lines)
            base_prompt += (
                "To maintain naming consistency across cluster logs, reuse existing group names "
                f"and regex patterns where applicable:\n{guide}\n"
                "Or introduce new descriptive names if the tokens represent new concepts.\n"
            )
        elif isinstance(groups_data, list) and groups_data:
            known_str = ", ".join(f"'{n}'" for n in groups_data)
            base_prompt += (
                "To maintain naming consistency, consider reusing existing group names "
                f"where applicable: [{known_str}], or introduce new descriptive names.\n"
            )
        base_prompt += (
            "Optionally, provide a format string in `format_string` "
            "(e.g., '{timestamp} {node}: {message}') to restore the original log line."
        )
    else:
        base_prompt += (
            f"\n5. The log envelope was successfully parsed, but the message body is novel.\n"
            f"Formulate a generalized Python regular expression pattern in "
            f"`suggested_log_template_regex` that matches this specific failure message "
            f"body ('{log_event.message}') across future occurrences, "
            f"parameterizing variable entities "
            f"(e.g. node names, device identifiers, addresses, numbers, or paths) "
            f"to enable instant fast-path rule matching in the cluster rule engine."
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
    known_groups: dict[str, dict[str, str]] | list[str] | None = None,
    known_group_names: list[str] | None = None,
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

    groups_info = known_groups if known_groups is not None else known_group_names
    prompt = _build_incident_prompt(log_event, known_groups=groups_info)

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
            current_res.format_string,
        )

        retries = 0
        message_history = result.all_messages()
        while not valid and retries < 2:
            retries += 1
            retry_prompt = (
                f"The suggested_log_template_regex or format_string failed validation:\n"
                f"{err_msg}\n\n"
                f"Raw log line to match: '{log_event.raw}'\n"
                f"Please correct the `suggested_log_template_regex` so it is valid, "
                f"matches the raw log line, and contains named groups like (?P<token>...)."
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
                current_res.format_string,
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
