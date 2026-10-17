"""Tests for the production SRE Resolution Agent, ModelRetry regex validation, and tools."""

from typing import Any

import pytest
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.models.test import TestModel

from src.agent import (
    SRE_SYSTEM_PROMPT,
    ProblemResolution,
    _build_incident_prompt,
    analyze_log,
    create_agent,
    validate_suggested_regex,
)
from src.config import get_settings
from src.rules.models import LogEvent


def test_get_settings_is_singleton() -> None:
    """Verify get_settings returns a cached singleton instance."""
    assert get_settings() is get_settings()


def test_agent_tool_registry() -> None:
    """Verify search and file-reading tools are registered on singleton agent."""
    agent = create_agent()

    tool_keys = set(agent._function_toolset.tools.keys())
    assert tool_keys == {
        "search_local_docs",
        "read_local_file",
        "search_web_docs",
    }


def test_problem_resolution_schema_validation() -> None:
    """Verify validation and serialization of ProblemResolution."""
    res = ProblemResolution(
        error_summary="Interface alt0 down",
        root_cause="NIFF detected network loss",
        severity="HIGH",
        remediation_steps=[
            "Inspect physical patch cable seating",
            "Restart interface via ifconfig alt0 down && ifconfig alt0 up",
        ],
        citations_and_reasons=[
            "[LANL HPC Runbook: unix-hw-alt0-down-v2.2]: primary communication link failure"
        ],
    )

    assert res.error_summary == "Interface alt0 down"
    assert res.root_cause == "NIFF detected network loss"
    assert res.severity == "HIGH"
    assert len(res.remediation_steps) == 2
    assert len(res.citations_and_reasons) == 1

    dumped = res.model_dump()
    assert dumped["severity"] == "HIGH"
    assert isinstance(dumped["remediation_steps"], list)
    assert isinstance(dumped["citations_and_reasons"], list)


def test_agent_system_prompt_configuration() -> None:
    """Verify system prompt contains SRE role instructions and citation mandates."""
    agent = create_agent()

    prompts = list(agent._system_prompts)
    assert len(prompts) > 0
    combined_prompt = " ".join(prompts)

    assert "SRE" in combined_prompt
    assert "search_local_docs" in combined_prompt
    assert "citations_and_reasons" in combined_prompt
    assert SRE_SYSTEM_PROMPT in prompts


def test_validate_suggested_regex() -> None:
    """Verify validate_suggested_regex validates syntax, matching, groups, and format."""
    sample_log = "2026-10-10 [storage_engine] Disk I/O timeout on volume /data"

    valid, err = validate_suggested_regex(r"[unclosed bracket", sample_log)
    assert valid is False
    assert "Invalid regex syntax" in err

    valid, err = validate_suggested_regex(r"^node-\d+\s+.*$", sample_log)
    assert valid is False
    assert "failed to match the incoming raw log line" in err

    valid, err = validate_suggested_regex(r"^\S+\s+\[\S+\]\s+.*$", sample_log)
    assert valid is False
    assert "contains no named capture groups" in err

    valid, err = validate_suggested_regex(
        r"^(?P<time>\S+)\s+\[(?P<tag>[^\]]+)\]\s+(?P<payload>.*)$",
        sample_log,
    )
    assert valid is True
    assert err == "OK"

    valid, err = validate_suggested_regex(
        r"^(?P<time>\S+)\s+\[(?P<tag>[^\]]+)\]\s+(?P<payload>.*)$",
        sample_log,
        format_string="{time} [{missing_key}] {payload}",
    )
    assert valid is False
    assert "references group" in err

    valid, err = validate_suggested_regex(
        r"^(?P<time>\S+)\s+\[(?P<tag>[^\]]+)\]\s+(?P<payload>.*)$",
        sample_log,
        format_string="{time} [{tag}] {payload}",
    )
    assert valid is True
    assert err == "OK"


@pytest.mark.anyio
async def test_analyze_log_with_unparsed_log_and_model_retry() -> None:
    """Verify output_validator raises ModelRetry when regex is invalid and succeeds on retry."""
    sample_raw = "MY_SUBSYS: error code 42 fatal fault"
    unparsed_event = LogEvent(
        raw=sample_raw,
        message=sample_raw,
        is_unparsed=True,
    )

    attempt = 0

    def custom_model_fn(_messages: list[Any], _info: Any) -> Any:
        nonlocal attempt
        attempt += 1
        from pydantic_ai.messages import ModelResponse, ToolCallPart

        if attempt == 1:
            args = ProblemResolution(
                error_summary="Fatal error in custom subsys",
                root_cause="Hardware error code 42",
                severity="CRITICAL",
                remediation_steps=["Reboot subsystem"],
                citations_and_reasons=["Custom Runbook"],
                suggested_log_template_regex=r"^MY_SUBSYS:\s+error.*$",
            ).model_dump()
        else:
            args = ProblemResolution(
                error_summary="Fatal error in custom subsys",
                root_cause="Hardware error code 42",
                severity="CRITICAL",
                remediation_steps=["Reboot subsystem"],
                citations_and_reasons=["Custom Runbook"],
                suggested_log_template_regex=r"^(?P<service>MY_SUBSYS):\s+(?P<details>.*)$",
                format_string="{service}: {details}",
            ).model_dump()
        return ModelResponse(parts=[ToolCallPart(tool_name="final_result", args=args)])

    agent = create_agent()
    with agent.override(model=FunctionModel(custom_model_fn)):
        resolution = await analyze_log(unparsed_event)

    assert isinstance(resolution, ProblemResolution)
    assert resolution.suggested_log_template_regex == r"^(?P<service>MY_SUBSYS):\s+(?P<details>.*)$"
    assert resolution.format_string == "{service}: {details}"
    assert attempt == 2


@pytest.mark.anyio
async def test_analyze_log_keeps_valid_regex_when_only_format_string_fails() -> None:
    """Verify valid regex is retained if only format_string fails validation after retries."""
    sample_raw = "MY_SUBSYS: error code 42 fatal fault"
    unparsed_event = LogEvent(raw=sample_raw, message=sample_raw, is_unparsed=True)

    bad_fmt_res = ProblemResolution(
        error_summary="Fatal error",
        root_cause="Error 42",
        severity="CRITICAL",
        remediation_steps=["Reboot"],
        citations_and_reasons=["Runbook"],
        suggested_log_template_regex=r"^(?P<service>MY_SUBSYS):\s+(?P<message>.*)$",
        format_string="{service}: {nonexistent_group}",
    )

    test_model = TestModel(custom_output_args=bad_fmt_res.model_dump())
    agent = create_agent()
    with agent.override(model=test_model):
        resolution = await analyze_log(unparsed_event)

    assert resolution.suggested_log_template_regex == r"^(?P<service>MY_SUBSYS):\s+(?P<message>.*)$"
    assert resolution.format_string == ""


def test_build_incident_prompt_generic_fields() -> None:
    """Verify _build_incident_prompt renders arbitrary key-value fields without fixed schema."""
    event = LogEvent(
        raw="[us-east-1] slurm job_id=99812 failed",
        message="job_id=99812 failed",
        cluster="us-east-1",
        service="slurm",
        job_id="99812",
    )
    prompt = _build_incident_prompt(event)
    assert "- cluster: us-east-1" in prompt
    assert "- service: slurm" in prompt
    assert "- job_id: 99812" in prompt


@pytest.mark.anyio
async def test_analyze_log_with_test_model() -> None:
    """Verify analyze_log returns valid ProblemResolution with citations and remediation."""
    mock_resolution_data = {
        "error_summary": "ServerFileSystem domain full panic",
        "root_cause": "Storage domain reached 100% capacity limit halting active job I/O",
        "severity": "CRITICAL",
        "remediation_steps": [
            "Purge expired scratch files and temporary core dumps",
            "Expand storage domain volume capacity using addvol",
        ],
        "citations_and_reasons": [
            "[LANL HPC Runbook: clusterfilesystem-fdmn-full-v1.8]: storage domain 100% limit"
        ],
    }

    test_model = TestModel(custom_output_args=mock_resolution_data)
    raw_line = (
        "1117769919 node-25 clusterfilesystem fdmn.full 1117769919 1 "
        "ServerFileSystem domain root_domain is full"
    )
    log_event = LogEvent(
        raw=raw_line,
        message="ServerFileSystem domain root_domain is full",
        log_id="1117769919",
        node="node-25",
    )

    agent = create_agent()
    with agent.override(model=test_model):
        resolution = await analyze_log(log_event)

    assert isinstance(resolution, ProblemResolution)
    assert resolution.severity == "CRITICAL"
    assert "ServerFileSystem domain full" in resolution.error_summary
    assert len(resolution.remediation_steps) == 2


@pytest.mark.anyio
async def test_analyze_log_raises_on_model_failure() -> None:
    """Verify analyze_log re-raises exception when LLM inference fails."""

    def failing_fn(_messages: Any, _info: Any) -> Any:
        raise ConnectionError("NVIDIA NIM endpoint unreachable (offline test)")

    log_event = LogEvent(
        raw="1117780000 node-44 clusterfilesystem fdmn.panic 1117780000 1 domain panic",
        message="domain panic",
    )

    agent = create_agent()
    with (
        agent.override(model=FunctionModel(failing_fn)),
        pytest.raises(ConnectionError, match="NVIDIA NIM endpoint unreachable"),
    ):
        await analyze_log(log_event)
