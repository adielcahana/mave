"""Tests for Stream Pipeline Orchestrator and unified CLI."""

import asyncio
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import patch

import pytest
from pydantic_ai.models.test import TestModel

from main import main, run_demo, run_evaluate
from src.agent import ProblemResolution, create_agent
from src.config import Settings
from src.pipeline import LogPipeline, PipelineResult
from src.rules.engine import RuleTreeEngine


@pytest.fixture
def test_settings() -> Settings:
    return Settings(
        base_url="https://test.nvidia.com/v1",
        api_key="test-key",
        model_name="openai/gpt-oss-20b",
    )


@pytest.fixture
def mock_resolution() -> ProblemResolution:
    return ProblemResolution(
        error_summary="Memory ECC uncorrectable error",
        root_cause="Multi-bit memory corruption detected in DIMM bank 2",
        severity="CRITICAL",
        remediation_steps=[
            "Quarantine node immediately",
            "Replace faulty DIMM module in bank 2",
        ],
        citations_and_reasons=["[LANL HPC Runbook: memory-ecc-v1.0]: critical hardware fault"],
    )


@pytest.mark.anyio
async def test_pipeline_filters_telemetry(test_settings: Settings) -> None:
    """Verify benign telemetry returns TELEMETRY_FILTERED with cost_usd=0.0."""
    pipeline = LogPipeline(settings=test_settings)

    telemetry_line = (
        "10000 node-1 switch_module fan 1074220000 1 Fan speeds ( 3552 3534 3375 3500 3515 3479 )"
    )
    result = await pipeline.process_log(telemetry_line)

    assert isinstance(result, PipelineResult)
    assert result.status == "TELEMETRY_FILTERED"
    assert result.cost_usd == 0.0
    assert result.resolution is None
    assert result.latency_ms >= 0.0


@pytest.mark.anyio
async def test_pipeline_hits_fast_path(test_settings: Settings) -> None:
    """Verify pre-configured rule returns FAST_PATH_HIT with cost_usd=0.0."""
    pipeline = LogPipeline(settings=test_settings)

    known_error_line = (
        "130000 Interconnect-1T00 switch_module psu 1074220000 1 PSU status ( off on )"
    )
    result = await pipeline.process_log(known_error_line)

    assert isinstance(result, PipelineResult)
    assert result.status == "FAST_PATH_HIT"
    assert result.cost_usd == 0.0
    assert result.resolution is not None
    assert "PSU failure" in result.resolution.error_summary


@pytest.mark.anyio
async def test_pipeline_dynamic_cache_learning(
    test_settings: Settings,
    mock_resolution: ProblemResolution,
) -> None:
    """Verify novel error produces AGENT_RESOLVED, and second event returns FAST_PATH_HIT."""
    test_model = TestModel(custom_output_args=mock_resolution.model_dump())
    agent = create_agent(settings=test_settings, model=test_model)
    pipeline = LogPipeline(agent=agent, settings=test_settings)

    novel_error_line = (
        "150000 node-88 kernel error 1074220000 1 memory ECC uncorrectable error on channel 0"
    )

    # First occurrence -> slow-path agent triage
    result1 = await pipeline.process_log(novel_error_line)
    assert result1.status == "AGENT_RESOLVED"
    assert result1.cost_usd > 0.0
    assert result1.resolution is not None
    assert result1.resolution.error_summary == mock_resolution.error_summary

    # Second occurrence -> fast-path cache hit ($0, sub-millisecond)
    result2 = await pipeline.process_log(novel_error_line)
    assert result2.status == "FAST_PATH_HIT"
    assert result2.cost_usd == 0.0
    assert result2.resolution is not None
    assert result2.resolution.error_summary == mock_resolution.error_summary


@pytest.mark.anyio
async def test_pipeline_burst_deduplication(
    test_settings: Settings,
    mock_resolution: ProblemResolution,
) -> None:
    """Verify burst of 10 novel errors results in only 1 agent call and 9 attached/cached."""
    call_count = 0

    async def slow_analyze_log(*args: Any, **kwargs: Any) -> ProblemResolution:
        nonlocal call_count
        call_count += 1
        # Add slight delay so concurrent tasks overlap in execution
        await asyncio.sleep(0.05)
        return mock_resolution

    rule_engine = RuleTreeEngine()  # Empty engine, no pre-configured rules
    pipeline = LogPipeline(rule_engine=rule_engine, settings=test_settings)

    burst_line = (
        "160000 node-101 Infiniband link_flap 1074220000 1 "
        "IB subnet manager link flap detected on hca-1"
    )
    lines = [burst_line] * 10

    with patch("src.pipeline.analyze_log", side_effect=slow_analyze_log):
        results = await asyncio.gather(*(pipeline.process_log(line) for line in lines))

    # Exactly 1 agent call must be made across the entire burst
    assert call_count == 1

    agent_resolved = [r for r in results if r.status == "AGENT_RESOLVED"]
    attached_or_cached = [r for r in results if r.status in ("IN_FLIGHT_ATTACHED", "FAST_PATH_HIT")]

    assert len(agent_resolved) == 1
    assert len(attached_or_cached) == 9

    # Total cost should reflect only the single LLM invocation
    total_cost = sum(r.cost_usd for r in results)
    assert pytest.approx(total_cost, rel=1e-3) == 0.0015

    # All returned resolutions must match
    for r in results:
        assert r.resolution is not None
        assert r.resolution.error_summary == mock_resolution.error_summary


@pytest.mark.anyio
async def test_pipeline_process_stream(test_settings: Settings) -> None:
    """Verify processing sync and async log streams."""
    pipeline = LogPipeline(settings=test_settings)

    lines = [
        "10000 node-1 switch_module fan 1074220000 1 Fan speeds ( 3552 3534 3375 3500 3515 3479 )",
        "130000 Interconnect-1T00 switch_module psu 1074220000 1 PSU status ( off on )",
    ]

    # Test sync iterable
    results: list[PipelineResult] = []
    async for item in pipeline.process_stream(lines):
        results.append(item)

    assert len(results) == 2
    assert results[0].status == "TELEMETRY_FILTERED"
    assert results[1].status == "FAST_PATH_HIT"

    # Test async iterable
    async def async_generator() -> AsyncIterator[str]:
        for line in lines:
            yield line

    async_results: list[PipelineResult] = []
    async for item in pipeline.process_stream(async_generator()):
        async_results.append(item)

    assert len(async_results) == 2
    assert async_results[0].status == "TELEMETRY_FILTERED"
    assert async_results[1].status == "FAST_PATH_HIT"


def test_cli_evaluate_runs_and_prints(capsys: pytest.CaptureFixture[str]) -> None:
    """Verify run_evaluate prints metrics table and SLA breakdown."""
    run_evaluate("data/golden_eval.json")
    captured = capsys.readouterr().out
    assert "HPC Golden Evaluation Metrics" in captured
    assert "HPC OPERATIONAL SLA BREAKDOWN" in captured
    assert "Wire-Speed Telemetry Dropping SLA" in captured
    assert "Fast-Path Rule Tree Resolution SLA" in captured


@pytest.mark.anyio
async def test_cli_demo_runs_cleanly(
    mock_resolution: ProblemResolution,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Verify run_demo executes all 5 stages cleanly."""
    with patch("src.pipeline.analyze_log", return_value=mock_resolution):
        await run_demo()
    captured = capsys.readouterr().out
    assert "[Stage 1] Wire-Speed Telemetry Filtration" in captured
    assert "[Stage 2] Sub-Millisecond Rule Tree Fast-Path" in captured
    assert "[Stage 3] Slow-Path SRE Agent Escalation" in captured
    assert "[Stage 4] Dynamic Cache Hit on Second Occurrence" in captured
    assert "[Stage 5] In-Flight Burst Storm Deduplication" in captured
    assert "DEMONSTRATION COMPLETE" in captured


def test_cli_main_dispatch() -> None:
    """Verify main entry point dispatches commands."""
    with (
        patch("sys.argv", ["main.py", "evaluate"]),
        patch("main.run_evaluate") as mock_eval,
    ):
        main()
        mock_eval.assert_called_once()
