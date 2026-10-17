import re
from functools import lru_cache

from openai import AsyncOpenAI
from pydantic_ai import Agent, ModelRetry, RunContext
from pydantic_ai.models import KnownModelName, Model
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider

from src.config import get_settings
from src.knowledge.search import (
    read_local_file,
    search_local_docs,
    search_web_docs,
)
from src.rules.engine import sanitize_regex
from src.rules.models import LogEvent, ProblemResolution

__all__ = [
    "SRE_SYSTEM_PROMPT",
    "ProblemResolution",
    "analyze_log",
    "create_agent",
    "read_local_file",
    "search_local_docs",
    "search_web_docs",
    "validate_suggested_regex",
]

SRE_SYSTEM_PROMPT = """You are an expert High Performance Computing (HPC) SRE.
Investigate cluster failure modes, determine root causes, assess incident severity,
and provide actionable, authoritative remediation steps grounded in official cluster runbooks.

Operational Guidelines:
1. Grounded Runbook & Local Investigation:
   - Always search local documentation and runbooks first before escalating to web search.
   - Use `search_local_docs` with keyword/regex queries or jq-style filters
     (e.g. `keywords=panic`, `err = 10`, `unavailable`) to search `resources/runbooks.json`
     and local documentation files.
   - Use `read_local_file` with line numbers as follow-up to inspect surrounding context,
     full remediation instructions, or specific sections around matched lines.

2. Web Search Fallback:
   - Only if local runbooks and local documentation do not contain sufficient guidance,
     query `search_web_docs` to search external technical documentation and web guides.

3. Diagnostic Rigor:
   - Formulate a concise error summary and identify precise technical root cause from runbooks/docs.
   - Assign an accurate severity level: CRITICAL, HIGH, MEDIUM, LOW, or INFO.
   - If the log line is routine benign telemetry requiring no action,
     set `is_benign=True` and `severity="INFO"`.

4. Actionable Remediation:
   - Provide clear, numbered, step-by-step remediation procedures to recover the cluster safely.
   - Prevent cascading failures (unmount corrupted domains, isolate flapping links, reset daemons).

5. Strict Citation Requirements:
   - Every resolution MUST cite specific runbooks or sources in `citations_and_reasons`.
   - For local runbooks, use the official citation identifier (e.g., "[LANL HPC Runbook: ...]").
   - For web search results, ALWAYS include the full source URL in the citation
     (e.g., "[Web Source: <Title> (https://...)] - <reason>").

You must always structure your output as a ProblemResolution object.
"""

_MAX_RESULT_RETRIES = 2


def validate_suggested_regex(
    regex: str,
    sample_raw_log: str,
    format_string: str = "",
) -> tuple[bool, str]:
    """Validate a suggested regex and optional format string against a sample raw log line.

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


@lru_cache(maxsize=1)
def create_agent(
    model: Model | KnownModelName | str | None = None,
) -> Agent[LogEvent, ProblemResolution]:
    """Create and configure the singleton Pydantic-AI SRE Resolution Agent."""
    settings = get_settings()

    if model is not None:
        chosen_model: Model | KnownModelName | str = model
    else:
        client = AsyncOpenAI(
            base_url=settings.base_url,
            api_key=settings.api_key,
            timeout=settings.timeout,
            max_retries=2,
        )
        chosen_model = OpenAIChatModel(
            settings.model_name,
            provider=OpenAIProvider(openai_client=client),
        )

    agent: Agent[LogEvent, ProblemResolution] = Agent(
        model=chosen_model,
        deps_type=LogEvent,
        output_type=ProblemResolution,
        system_prompt=SRE_SYSTEM_PROMPT,
        retries=_MAX_RESULT_RETRIES,
    )
    agent.tool_plain(search_local_docs)
    agent.tool_plain(read_local_file)
    agent.tool_plain(search_web_docs)

    @agent.output_validator
    def _validate_resolution_output(
        ctx: RunContext[LogEvent],
        output: ProblemResolution,
    ) -> ProblemResolution:
        for cite in output.citations_and_reasons:
            lower_cite = cite.lower()
            if (
                ("web source" in lower_cite or "web:" in lower_cite)
                and "http://" not in lower_cite
                and "https://" not in lower_cite
                and ctx.retry < _MAX_RESULT_RETRIES
            ):
                raise ModelRetry(
                    f"Web citation '{cite}' in `citations_and_reasons` is missing its source URL. "
                    f"Every web citation must include the full http:// or https:// URL "
                    f"(e.g., '[Web Source: Title (https://...)] - reason')."
                )

        log_event = ctx.deps
        if not log_event.is_unparsed:
            if output.suggested_log_template_regex.strip():
                output.suggested_log_template_regex = sanitize_regex(
                    output.suggested_log_template_regex,
                    log_event.message,
                )
            return output

        output.suggested_log_template_regex = sanitize_regex(
            output.suggested_log_template_regex,
            log_event.raw,
        )
        if output.suggested_message_regex.strip():
            output.suggested_message_regex = sanitize_regex(
                output.suggested_message_regex,
                log_event.message,
            )

        valid, err_msg = validate_suggested_regex(
            output.suggested_log_template_regex,
            log_event.raw,
            output.format_string,
        )
        if not valid:
            if ctx.retry < _MAX_RESULT_RETRIES:
                raise ModelRetry(
                    f"The suggested_log_template_regex or format_string failed validation:\n"
                    f"{err_msg}\n\n"
                    f"Raw log line to match: '{log_event.raw}'\n"
                    f"Please correct `suggested_log_template_regex` or `format_string` so it "
                    f"is valid, matches the raw log line, and contains named groups like "
                    f"(?P<token>...)."
                )
            regex_only_valid, _ = validate_suggested_regex(
                output.suggested_log_template_regex,
                log_event.raw,
            )
            if regex_only_valid:
                output.format_string = ""
            else:
                output.suggested_log_template_regex = ""
                output.format_string = ""
        return output

    return agent


def _build_incident_prompt(
    log_event: LogEvent,
    known_groups: dict[str, dict[str, str]] | list[str] | None = None,
) -> str:
    """Format generic LogEvent key-value fields into an incident prompt for SRE investigation."""
    base_prompt = (
        "Investigate the following HPC cluster incident and formulate a grounded resolution:\n"
    )
    for k, v in log_event.fields.items():
        base_prompt += f"- {k}: {v}\n"
    base_prompt += f"- Message: {log_event.message}\n"
    base_prompt += f"- Raw Log: {log_event.raw}\n"

    base_prompt += (
        "\nInstructions:\n"
        "1. First search local knowledge using `search_local_docs` "
        "(grep regex on text docs, jq filters or keywords on JSON runbooks).\n"
        "   Use `read_local_file` with line numbers to follow up on matched files and sections.\n"
        "2. Only if local search yields no relevant match, query `search_web_docs` for "
        "external technical guides.\n"
        "3. Diagnose the root cause and determine severity (CRITICAL, HIGH, MEDIUM, LOW, INFO). "
        "If the log is routine benign telemetry, set `is_benign=True`.\n"
        "4. Provide ordered remediation steps and cite specific runbooks or web sources "
        "in `citations_and_reasons` (for web sources, ALWAYS include the full http/https URL)."
    )
    if log_event.is_unparsed:
        base_prompt += (
            "\n5. This log line was UNPARSED by all registered cluster log envelopes.\n"
            "You MUST formulate a Python regex with named capture groups (?P<name>...)\n"
            f"in `suggested_log_template_regex` that accurately parses: '{log_event.raw}', "
            "including a `(?P<message>.+)` group for the trailing message payload.\n"
            "Also provide a generalized Python regex in `suggested_message_regex` that matches "
            "the extracted `message` payload across future occurrences (parameterizing variable "
            "entities like IDs, nodes, component/device names, addresses, or numbers; if the "
            "log contains literal octal escapes like `\\042`, match them with `\\\\042.+?\\\\042` "
            "rather than quotes).\n"
        )
        if isinstance(known_groups, dict) and known_groups:
            lines: list[str] = []
            for grp, info in sorted(known_groups.items()):
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
        elif isinstance(known_groups, list) and known_groups:
            known_str = ", ".join(f"'{n}'" for n in known_groups)
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
            f"(e.g. node names, device identifiers, addresses, numbers, or paths; if the "
            f"message contains literal octal escapes like `\\042`, match them with "
            f"`\\\\042.+?\\\\042` rather than quotes) "
            f"to enable instant fast-path rule matching in the cluster rule engine."
        )
    return base_prompt


async def analyze_log(
    log_event: LogEvent,
    known_groups: dict[str, dict[str, str]] | list[str] | None = None,
) -> ProblemResolution:
    """Analyze an HPC log event and return a grounded ProblemResolution."""
    agent = create_agent()
    prompt = _build_incident_prompt(log_event, known_groups=known_groups)
    result = await agent.run(prompt, deps=log_event)
    return result.output
