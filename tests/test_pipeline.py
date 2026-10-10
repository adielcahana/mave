"""Tests for Pipeline Orchestrator and unified CLI."""

import shutil
from pathlib import Path
from unittest.mock import patch

import pytest
from pydantic_ai.models.test import TestModel

from main import main, run_demo, run_evaluate
from src.agent import ProblemResolution, create_agent
from src.config import Settings
from src.pipeline import LogPipeline, PipelineResult


@pytest.fixture
def test_settings(tmp_path: Path) -> Settings:
    rules_file = tmp_path / "rules.json"
    shutil.copyfile("rules/rules.json", rules_file)
    return Settings(
        base_url="https://test.nvidia.com/v1",
        api_key="test-key",
        model_name="openai/gpt-oss-20b",
        rules_path=str(rules_file),
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
async def test_pipeline_process_logs(test_settings: Settings) -> None:
    """Verify processing multiple log lines."""
    pipeline = LogPipeline(settings=test_settings)

    lines = [
        "10000 node-1 switch_module fan 1074220000 1 Fan speeds ( 3552 3534 3375 3500 3515 3479 )",
        "130000 Interconnect-1T00 switch_module psu 1074220000 1 PSU status ( off on )",
    ]

    results = await pipeline.process_logs(lines)

    assert len(results) == 2
    assert results[0].status == "TELEMETRY_FILTERED"
    assert results[1].status == "FAST_PATH_HIT"


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
    """Verify run_demo executes cleanly."""
    with patch("src.pipeline.analyze_log", return_value=mock_resolution):
        await run_demo()
    captured = capsys.readouterr().out
    assert "[Stage 1] Wire-Speed Telemetry Filtration" in captured
    assert "[Stage 2] Sub-Millisecond Rule Tree Fast-Path" in captured
    assert "[Stage 3] Slow-Path SRE Agent Escalation" in captured
    assert "[Stage 4] Dynamic Cache Hit on Second Occurrence" in captured
    assert "DEMONSTRATION COMPLETE" in captured


def test_cli_main_dispatch() -> None:
    """Verify main entry point dispatches commands."""
    with (
        patch("sys.argv", ["main.py", "evaluate"]),
        patch("main.run_evaluate") as mock_eval,
    ):
        main()
        mock_eval.assert_called_once()
