"""Tests for Pipeline Orchestrator and unified CLI."""

import asyncio
import shutil
import time
from pathlib import Path
from unittest.mock import patch

import pytest
from pydantic_ai.models.test import TestModel

from main import (
    LogProcessingStats,
    analyze_log_lines,
    main,
    run_demo,
    run_evaluate,
    track_execution_timing,
)
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


@pytest.mark.anyio
async def test_analyze_log_lines_aggregates_stats(test_settings: Settings) -> None:
    """Verify analyze_log_lines processes lines and computes stats correctly."""
    lines = [
        "10000 node-1 switch_module fan 1074220000 1 Fan speeds ( 3552 3534 3375 3500 3515 3479 )",
        "130000 Interconnect-1T00 switch_module psu 1074220000 1 PSU status ( off on )",
    ]
    pipeline = LogPipeline(settings=test_settings)
    stats = await analyze_log_lines(lines, source_name="Test Stream", pipeline=pipeline)

    assert stats.total_lines == 2
    assert stats.benign_count == 1
    assert stats.error_count == 1
    assert stats.auto_annotated_count == 1
    assert len(stats.unique_errors) == 1


def test_cli_main_single_line_telemetry(capsys: pytest.CaptureFixture[str]) -> None:
    """Verify main() handles a single benign telemetry line via --line."""
    line = (
        "10000 node-1 switch_module fan 1074220000 1 Fan speeds ( 3552 3534 3375 3500 3515 3479 )"
    )
    with patch("sys.argv", ["main.py", "--line", line]):
        main()
    captured = capsys.readouterr().out
    assert "Input Log Line" in captured
    assert "Benign Telemetry" in captured


def test_cli_main_single_line_short_flag(capsys: pytest.CaptureFixture[str]) -> None:
    """Verify main() handles a single error log line via -l."""
    line = "130000 Interconnect-1T00 switch_module psu 1074220000 1 PSU status ( off on )"
    with patch("sys.argv", ["main.py", "-l", line]):
        main()
    captured = capsys.readouterr().out
    assert "Input Log Line" in captured
    assert "Actionable Errors" in captured
    assert "PSU failure" in captured


def test_cli_main_single_line_empty() -> None:
    """Verify main() exits cleanly with code 1 if empty log line is provided."""
    with (
        patch("sys.argv", ["main.py", "--line", "   "]),
        pytest.raises(SystemExit) as exc_info,
    ):
        main()
    assert exc_info.value.code == 1


@pytest.mark.anyio
async def test_analyze_log_lines_runs_concurrently(test_settings: Settings) -> None:
    """Verify analyze_log_lines processes lines concurrently rather than sequentially."""
    pipeline = LogPipeline(settings=test_settings)
    max_active = 0
    active = 0
    original_process = pipeline.process_log

    async def mock_process(line: str) -> PipelineResult:
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.01)
        res = await original_process(line)
        active -= 1
        return res

    fan_log = (
        "10000 node-1 switch_module fan 1074220000 1 Fan speeds ( 3552 3534 3375 3500 3515 3479 )"
    )
    with patch.object(pipeline, "process_log", side_effect=mock_process):
        lines = [
            fan_log,
            fan_log.replace("node-1", "node-2"),
            fan_log.replace("node-1", "node-3"),
        ]
        stats = await analyze_log_lines(lines, pipeline=pipeline)
        assert stats.total_lines == 3
        assert max_active >= 2, f"Expected concurrent execution, but max active was {max_active}"


@pytest.mark.anyio
async def test_analyze_log_lines_with_concurrency_limit(test_settings: Settings) -> None:
    """Verify analyze_log_lines respects concurrency_limit semaphore."""
    pipeline = LogPipeline(settings=test_settings)
    max_active = 0
    active = 0
    original_process = pipeline.process_log

    async def mock_process(line: str) -> PipelineResult:
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.01)
        res = await original_process(line)
        active -= 1
        return res

    fan_log = (
        "10000 node-1 switch_module fan 1074220000 1 Fan speeds ( 3552 3534 3375 3500 3515 3479 )"
    )
    with patch.object(pipeline, "process_log", side_effect=mock_process):
        lines = [fan_log] * 5
        stats = await analyze_log_lines(lines, pipeline=pipeline, concurrency_limit=2)
        assert stats.total_lines == 5
        assert max_active <= 2


@pytest.mark.anyio
async def test_pipeline_process_logs_concurrency_limit(test_settings: Settings) -> None:
    """Verify pipeline.process_logs respects concurrency_limit and returns expected results."""
    pipeline = LogPipeline(settings=test_settings)
    lines = [
        "10000 node-1 switch_module fan 1074220000 1 Fan speeds ( 3552 3534 3375 3500 3515 3479 )",
        "130000 Interconnect-1T00 switch_module psu 1074220000 1 PSU status ( off on )",
    ]
    results = await pipeline.process_logs(lines, concurrency_limit=1)
    assert len(results) == 2
    assert results[0].status == "TELEMETRY_FILTERED"
    assert results[1].status == "FAST_PATH_HIT"


@pytest.mark.anyio
async def test_pipeline_in_flight_burst_deduplication(
    test_settings: Settings,
    mock_resolution: ProblemResolution,
) -> None:
    """Verify concurrent identical novel errors debounce via IN_FLIGHT_ATTACHED."""
    test_model = TestModel(custom_output_args=mock_resolution.model_dump())
    agent = create_agent(settings=test_settings, model=test_model)
    pipeline = LogPipeline(agent=agent, settings=test_settings)

    novel_error_line = (
        "160000 node-99 kernel error 1074220000 1 unknown critical bus fault 0xdeadbeef"
    )

    call_count = 0

    async def mock_analyze(*args: object, **kwargs: object) -> ProblemResolution:
        nonlocal call_count
        call_count += 1
        await asyncio.sleep(0.02)  # Simulate LLM latency
        return mock_resolution

    with patch("src.pipeline.analyze_log", side_effect=mock_analyze):
        results = await pipeline.process_logs([novel_error_line] * 4)

    assert len(results) == 4
    # The leader is AGENT_RESOLVED, subsequent concurrent coroutines attach
    statuses = [r.status for r in results]
    assert statuses[0] == "AGENT_RESOLVED"
    assert statuses.count("IN_FLIGHT_ATTACHED") == 3
    assert call_count == 1


def test_log_processing_stats_recording_and_timing(mock_resolution: ProblemResolution) -> None:
    """Verify LogProcessingStats records outcomes and timing correctly."""
    stats = LogProcessingStats(file_name="test.log", total_lines=3)

    r_benign = PipelineResult(log_id="1", status="TELEMETRY_FILTERED", cost_usd=0.0)
    r_fast = PipelineResult(
        log_id="2",
        status="FAST_PATH_HIT",
        resolution=mock_resolution,
        cost_usd=0.0,
    )
    r_agent = PipelineResult(
        log_id="3",
        status="AGENT_RESOLVED",
        resolution=mock_resolution,
        cost_usd=0.0015,
    )

    stats.record_results([r_benign, r_fast, r_agent])

    assert stats.benign_count == 1
    assert stats.error_count == 2
    assert stats.auto_annotated_count == 1
    assert stats.llm_needed_count == 1
    assert stats.total_cost_usd == pytest.approx(0.0015)
    assert len(stats.unique_errors) == 1
    assert stats.unique_errors[mock_resolution.root_cause.strip()][1] == 2

    with track_execution_timing(stats):
        time.sleep(0.01)

    assert stats.elapsed_time_s >= 0.005
    assert stats.throughput_lines_per_sec > 0.0


@pytest.mark.anyio
async def test_pipeline_custom_callback_invoked(test_settings: Settings) -> None:
    """Verify registered callbacks fire on every processed log event."""
    received: list[PipelineResult] = []
    pipeline = LogPipeline(settings=test_settings, on_result=received.append)

    line = (
        "10000 node-1 switch_module fan 1074220000 1 Fan speeds ( 3552 3534 3375 3500 3515 3479 )"
    )
    await pipeline.process_log(line)

    assert len(received) == 1
    assert received[0].status == "TELEMETRY_FILTERED"


@pytest.mark.anyio
async def test_pipeline_collect_stats_context_manager(test_settings: Settings) -> None:
    """Verify collect_stats context manager hooks callbacks and finalizes stats."""
    pipeline = LogPipeline(settings=test_settings)
    fan_line = (
        "10000 node-1 switch_module fan 1074220000 1 Fan speeds ( 3552 3534 3375 3500 3515 3479 )"
    )
    psu_line = "130000 Interconnect-1T00 switch_module psu 1074220000 1 PSU status ( off on )"
    lines = [fan_line, psu_line]

    with pipeline.collect_stats(source_name="Test Context", total_lines=len(lines)) as stats:
        await pipeline.process_logs(lines)

    assert stats.file_name == "Test Context"
    assert stats.total_lines == 2
    assert stats.benign_count == 1
    assert stats.error_count == 1
    assert stats.auto_annotated_count == 1
    assert stats.elapsed_time_s > 0.0
    assert stats.throughput_lines_per_sec > 0.0


@pytest.mark.anyio
async def test_pipeline_analyze_method(test_settings: Settings) -> None:
    """Verify pipeline.analyze returns complete LogProcessingStats."""
    pipeline = LogPipeline(settings=test_settings)
    fan_line = (
        "10000 node-1 switch_module fan 1074220000 1 Fan speeds ( 3552 3534 3375 3500 3515 3479 )"
    )
    psu_line = "130000 Interconnect-1T00 switch_module psu 1074220000 1 PSU status ( off on )"
    lines = [fan_line, psu_line]

    stats = await pipeline.analyze(lines, source_name="Stream Test")
    assert stats.total_lines == 2
    assert stats.benign_count == 1
    assert stats.error_count == 1
    assert stats.auto_annotated_count == 1
