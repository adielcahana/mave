"""Tests for the production SRE Resolution Agent and knowledge tools wiring."""

from typing import Any

import pytest
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.models.test import TestModel

from src.agent import (
    SRE_SYSTEM_PROMPT,
    ProblemResolution,
    analyze_log,
    create_agent,
)
from src.config import Settings
from src.rules.models import LogEvent


def test_agent_tool_registry() -> None:
    """Verify query_subsystem_docs and list_known_subsystems are registered on agent."""
    test_settings = Settings(
        base_url="https://test.nvidia.com/v1",
        api_key="test-key",
        model_name="openai/gpt-oss-20b",
    )
    agent = create_agent(settings=test_settings)

    # Introspect tools registered on the agent
    tool_keys = set(agent._function_toolset.tools.keys())
    assert "query_subsystem_docs" in tool_keys, (
        f"Expected 'query_subsystem_docs' in agent tools, found: {tool_keys}"
    )
    assert "list_known_subsystems" in tool_keys, (
        f"Expected 'list_known_subsystems' in agent tools, found: {tool_keys}"
    )

    # Backwards-compatible _function_tools check
    fn_tools = getattr(agent, "_function_tools", None)
    if isinstance(fn_tools, dict):
        assert "query_subsystem_docs" in fn_tools
        assert "list_known_subsystems" in fn_tools


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
    assert "unix-hw-alt0-down-v2.2" in res.citations_and_reasons[0]

    dumped = res.model_dump()
    assert dumped["severity"] == "HIGH"
    assert isinstance(dumped["remediation_steps"], list)
    assert isinstance(dumped["citations_and_reasons"], list)


def test_agent_system_prompt_configuration() -> None:
    """Verify system prompt contains SRE role instructions and citation mandates."""
    test_settings = Settings(
        base_url="https://test.nvidia.com/v1",
        api_key="test-key",
        model_name="openai/gpt-oss-20b",
    )
    agent = create_agent(settings=test_settings)

    prompts = list(agent._system_prompts)
    assert len(prompts) > 0
    combined_prompt = " ".join(prompts)

    assert "SRE" in combined_prompt or "Site Reliability" in combined_prompt
    assert "query_subsystem_docs" in combined_prompt
    assert "citations_and_reasons" in combined_prompt or "citation" in combined_prompt.lower()
    assert SRE_SYSTEM_PROMPT in prompts


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
    test_settings = Settings(
        base_url="https://test.nvidia.com/v1",
        api_key="test-key",
        model_name="openai/gpt-oss-20b",
    )
    agent = create_agent(settings=test_settings, model=test_model)

    raw_line = (
        "1117769919 2005.06.03 node-25 clusterfilesystem: "
        "fdmn.full ServerFileSystem domain root_domain is full"
    )
    log_event = LogEvent(
        log_id="1117769919",
        node="node-25",
        subsystem="clusterfilesystem",
        event_type="fdmn.full",
        timestamp=1117769919,
        flag=1,
        message="ServerFileSystem domain root_domain is full",
        raw=raw_line,
    )

    resolution = await analyze_log(log_event, agent=agent, settings=test_settings)

    assert isinstance(resolution, ProblemResolution)
    assert resolution.severity == "CRITICAL"
    assert "ServerFileSystem domain full" in resolution.error_summary
    assert "100% capacity" in resolution.root_cause
    assert len(resolution.remediation_steps) == 2
    assert len(resolution.citations_and_reasons) == 1
    assert "clusterfilesystem-fdmn-full-v1.8" in resolution.citations_and_reasons[0]


@pytest.mark.anyio
async def test_analyze_log_with_test_model_calling_tools() -> None:
    """Verify that agent calls registered domain tools during investigation."""
    citation_text = "[LANL HPC Runbook: switch-module-psu-failover-v2.0]: single-feed redundancy"
    mock_resolution_data = {
        "error_summary": "PSU failover degraded on switch module",
        "root_cause": "Switch chassis power supply unit lost AC feed",
        "severity": "HIGH",
        "remediation_steps": [
            "Inspect rear PSU LEDs and AC power cable",
            "Hot-swap failed PSU module with compatible spare",
        ],
        "citations_and_reasons": [citation_text],
    }

    test_model = TestModel(
        call_tools=["query_subsystem_docs"],
        custom_output_args=mock_resolution_data,
    )
    test_settings = Settings(
        base_url="https://test.nvidia.com/v1",
        api_key="test-key",
        model_name="openai/gpt-oss-20b",
    )
    agent = create_agent(settings=test_settings, model=test_model)

    raw_line = "1117770000 2005.06.03 switch-01 switch_module: psu PSU status ( off on ) degraded"
    log_event = LogEvent(
        log_id="1117770000",
        node="switch-01",
        subsystem="switch_module",
        event_type="psu",
        timestamp=1117770000,
        flag=1,
        message="PSU status ( off on ) degraded",
        raw=raw_line,
    )

    resolution = await analyze_log(log_event, agent=agent, settings=test_settings)
    assert isinstance(resolution, ProblemResolution)
    assert resolution.severity == "HIGH"
    assert len(resolution.remediation_steps) == 2
    assert len(resolution.citations_and_reasons) == 1
    assert "switch-module-psu-failover-v2.0" in resolution.citations_and_reasons[0]


@pytest.mark.anyio
async def test_analyze_log_offline_graceful_fallback() -> None:
    """Verify analyze_log handles offline/error states and returns a grounded resolution."""

    def failing_fn(messages: Any, info: Any) -> Any:
        raise ConnectionError("NVIDIA NIM endpoint unreachable (offline test)")

    test_settings = Settings(
        base_url="https://test.nvidia.com/v1",
        api_key="test-key",
        model_name="openai/gpt-oss-20b",
    )
    failing_agent = create_agent(settings=test_settings, model=FunctionModel(failing_fn))

    raw_line = (
        "1117780000 2005.06.03 node-44 clusterfilesystem: "
        "fdmn.panic domain panic unrecoverable disk error"
    )
    log_event = LogEvent(
        log_id="1117780000",
        node="node-44",
        subsystem="clusterfilesystem",
        event_type="fdmn.panic",
        timestamp=1117780000,
        flag=1,
        message="domain panic unrecoverable disk error",
        raw=raw_line,
    )

    resolution = await analyze_log(log_event, agent=failing_agent, settings=test_settings)

    assert isinstance(resolution, ProblemResolution)
    assert len(resolution.remediation_steps) > 0
    assert len(resolution.citations_and_reasons) > 0
    assert "clusterfilesystem" in resolution.citations_and_reasons[0].lower()


def test_fallback_offline_resolution_subsystem_and_keyword_priority() -> None:
    """Verify _fallback_offline_resolution prioritizes subsystem+keyword matches
    and resolves aliases.
    """
    from src.agent import _fallback_offline_resolution

    # Test 1: Subsystem alias "resourcemgmt" resolved to "resourcemgmtdaeomon"
    # and keyword "err = 10" matches config err10 runbook rather than controller timeout or startup
    event_err10 = LogEvent(
        log_id="1",
        node="node-1",
        subsystem="resourcemgmt",
        event_type="server",
        message="failed to configure resourcemgmt subsystem err = 10",
    )
    res_err10 = _fallback_offline_resolution(event_err10, Exception("offline test"))
    assert "resourcemgmt-config-err10" in res_err10.citations_and_reasons[0]

    # Test 2: Subsystem with no matching keywords falls back to general subsystem entry
    event_general = LogEvent(
        log_id="2",
        node="node-2",
        subsystem="cluster_filesystem",
        event_type="general",
        message="some unclassified filesystem event",
    )
    res_general = _fallback_offline_resolution(event_general, Exception("offline test"))
    assert "clusterfilesystem" in res_general.citations_and_reasons[0].lower()
