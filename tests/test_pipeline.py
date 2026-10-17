"""Tests for Pipeline Orchestrator and unified CLI."""

import asyncio
import shutil
from pathlib import Path
from unittest.mock import patch

import pytest
from pydantic_ai.models.test import TestModel

from src.agent import ProblemResolution, create_agent
from src.config import Settings
from src.main import (
    LogProcessingStats,
    analyze_log_lines,
    main,
    run_evaluate,
)
from src.pipeline import LogPipeline, PipelineResult


@pytest.fixture
def test_settings(tmp_path: Path) -> Settings:
    rules_file = tmp_path / "rules.json"
    runbooks_file = tmp_path / "runbooks.json"
    shutil.copyfile("resources/rules.json", rules_file)
    shutil.copyfile("resources/runbooks.json", runbooks_file)
    return Settings(
        base_url="https://test.nvidia.com/v1",
        api_key="test-key",
        model_name="nvidia/nemotron-3-super-120b-a12b",
        rules_path=str(rules_file),
        runbooks_path=str(runbooks_file),
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
        suggested_log_template_regex=r"memory ECC uncorrectable error on channel \d+",
    )


@pytest.mark.anyio
async def test_pipeline_filters_telemetry(test_settings: Settings) -> None:
    """Verify benign telemetry child rule returns TELEMETRY_FILTERED."""
    pipeline = LogPipeline(settings=test_settings)

    telemetry_line = (
        "10000 node-1 switch_module fan 1074220000 1 Fan speeds ( 3552 3534 3375 3500 3515 3479 )"
    )
    result = await pipeline.process_log(telemetry_line)

    assert isinstance(result, PipelineResult)
    assert result.status == "TELEMETRY_FILTERED"
    assert result.resolution is None
    assert result.latency_ms >= 0.0


@pytest.mark.anyio
async def test_pipeline_hits_fast_path(test_settings: Settings) -> None:
    """Verify pre-configured rule returns FAST_PATH_HIT."""
    pipeline = LogPipeline(settings=test_settings)

    known_error_line = (
        "130000 Interconnect-1T00 switch_module psu 1074220000 1 PSU status ( off on )"
    )
    result = await pipeline.process_log(known_error_line)

    assert isinstance(result, PipelineResult)
    assert result.status == "FAST_PATH_HIT"
    assert result.resolution is not None
    assert "PSU failure" in result.resolution.error_summary


@pytest.mark.anyio
async def test_pipeline_dynamic_rule_learning(
    test_settings: Settings,
    mock_resolution: ProblemResolution,
) -> None:
    """Verify novel error produces AGENT_RESOLVED, and second event returns FAST_PATH_HIT."""
    test_model = TestModel(custom_output_args=mock_resolution.model_dump())
    pipeline = LogPipeline(settings=test_settings)

    novel_error_line1 = (
        "150000 node-88 kernel error 1074220000 1 memory ECC uncorrectable error on channel 0"
    )
    novel_error_line2 = (
        "150001 node-89 kernel error 1074220001 1 memory ECC uncorrectable error on channel 3"
    )

    with create_agent().override(model=test_model):
        result1 = await pipeline.process_log(novel_error_line1)
        assert result1.status == "AGENT_RESOLVED"
        assert result1.from_llm is True
        assert result1.resolution is not None
        assert result1.resolution.error_summary == mock_resolution.error_summary

        result2 = await pipeline.process_log(novel_error_line2)
        assert result2.status == "FAST_PATH_HIT"
        assert result2.from_llm is False
        assert result2.resolution is not None
        assert result2.resolution.error_summary == mock_resolution.error_summary


@pytest.mark.anyio
async def test_pipeline_dynamic_envelope_registration_on_unparsed_log(
    test_settings: Settings,
) -> None:
    """Verify pipeline registers dynamic parent envelope and child rule for unparsed logs."""
    unparsed_line = "STORAGE_CONTROLLER [ctrl-0] RAID failure on array 0"

    mock_res = ProblemResolution(
        error_summary="RAID failure",
        root_cause="Drive controller failure",
        severity="HIGH",
        remediation_steps=["Replace drive"],
        citations_and_reasons=["Storage Docs"],
        suggested_log_template_regex=(
            r"^STORAGE_CONTROLLER\s+\[(?P<controller>[^\]]+)\]\s+(?P<message>.*)$"
        ),
        suggested_message_regex=r"RAID (?:failure|degraded) on array \d+",
    )

    test_model = TestModel(custom_output_args=mock_res.model_dump())
    pipeline = LogPipeline(settings=test_settings)

    with create_agent().override(model=test_model):
        res1 = await pipeline.process_log(unparsed_line)
        assert res1.status == "AGENT_RESOLVED"

        similar_line = "STORAGE_CONTROLLER [ctrl-1] RAID degraded on array 2"
        res2 = await pipeline.process_log(similar_line)
        assert res2.status == "FAST_PATH_HIT"
        assert res2.resolution is not None
        assert res2.resolution.error_summary == "RAID failure"


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
    assert "Precision" in captured
    assert "Recall" in captured
    assert "Accuracy" in captured
    assert "F1 Score" in captured


def test_cli_main_dispatch() -> None:
    """Verify main entry point dispatches evaluate command."""
    with (
        patch("sys.argv", ["main.py", "evaluate"]),
        patch("src.main.run_evaluate") as mock_eval,
    ):
        main()
        mock_eval.assert_called_once()


@pytest.mark.anyio
async def test_analyze_log_lines_aggregates_stats(test_settings: Settings) -> None:
    """Verify analyze_log_lines processes lines and computes stats with SourceName literal."""
    lines = [
        "10000 node-1 switch_module fan 1074220000 1 Fan speeds ( 3552 3534 3375 3500 3515 3479 )",
        "130000 Interconnect-1T00 switch_module psu 1074220000 1 PSU status ( off on )",
    ]
    pipeline = LogPipeline(settings=test_settings)
    stats = await analyze_log_lines(lines, source_name="Stream", pipeline=pipeline)

    assert stats.source_name == "Stream"
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
async def test_pipeline_concurrent_post_facto_deduplication(
    tmp_path: Path,
) -> None:
    """Verify concurrent cold-start workers deduplicate learned rules after the fact."""
    rules_file = tmp_path / "empty_rules.json"
    runbooks_file = tmp_path / "runbooks.json"
    rules_file.write_text("[]", encoding="utf-8")
    shutil.copyfile("resources/runbooks.json", runbooks_file)
    cold_settings = Settings(
        base_url="https://test.nvidia.com/v1",
        api_key="test-key",
        model_name="nvidia/nemotron-3-super-120b-a12b",
        rules_path=str(rules_file),
        runbooks_path=str(runbooks_file),
    )

    res_variant_a = ProblemResolution(
        error_summary="Bus fault",
        root_cause="Critical hardware bus fault",
        severity="CRITICAL",
        remediation_steps=["Reset node"],
        citations_and_reasons=["[LANL HPC Runbook: bus-fault]"],
        suggested_log_template_regex=(
            r"^(?P<id>\d+)\s+(?P<host>\S+)\s+(?P<message>critical bus fault \S+)$"
        ),
        suggested_message_regex=r"critical bus fault 0x[0-9a-fA-F]+",
    )
    res_variant_b = ProblemResolution(
        error_summary="Bus fault",
        root_cause="Critical hardware bus fault",
        severity="CRITICAL",
        remediation_steps=["Reset node"],
        citations_and_reasons=["[LANL HPC Runbook: bus-fault]"],
        suggested_log_template_regex=(
            r"^(?P<log_id>\d+)\s+(?P<node>\S+)\s+(?P<message>critical bus fault .+)$"
        ),
        suggested_message_regex=r"critical bus fault \S+",
    )

    pipeline = LogPipeline(settings=cold_settings)

    lines = [
        "160000 node-98 critical bus fault 0xdeadbeef",
        "160001 node-99 critical bus fault 0xcafebabe",
    ]

    idx = 0

    async def mock_analyze(*_args: object, **_kwargs: object) -> ProblemResolution:
        nonlocal idx
        idx += 1
        await asyncio.sleep(0.01)
        return res_variant_a if idx == 1 else res_variant_b

    with patch("src.pipeline.analyze_log", side_effect=mock_analyze):
        results = await pipeline.process_logs(lines, concurrency_limit=2)

    assert len(results) == 2
    assert all(r.status == "AGENT_RESOLVED" for r in results)
    # Post-facto deduplication ensures only 1 parent envelope and 1 child rule remain
    assert len(pipeline.rule_engine.envelopes) == 1
    assert len(pipeline.rule_engine.rules) == 1


def test_log_processing_stats_recording(mock_resolution: ProblemResolution) -> None:
    """Verify LogProcessingStats records outcomes, lane counts, and timing correctly."""
    stats = LogProcessingStats(source_name="File", file_name="test.log", total_lines=4)
    assert stats.remaining_count == 4

    r_benign = PipelineResult(log_id="1", status="TELEMETRY_FILTERED")
    r_fast = PipelineResult(
        log_id="2",
        status="FAST_PATH_HIT",
        resolution=mock_resolution,
    )
    r_agent = PipelineResult(
        log_id="3",
        status="AGENT_RESOLVED",
        resolution=mock_resolution,
        from_llm=True,
    )

    stats.record_results([r_benign, r_fast, r_agent])
    stats.finalize(0.01)

    assert stats.processed_count == 3
    assert stats.remaining_count == 1
    assert stats.fast_lane_count == 2
    assert stats.llm_lane_count == 1
    assert stats.benign_count == 1
    assert stats.error_count == 2
    assert stats.auto_annotated_count == 1
    assert stats.llm_needed_count == 1
    assert len(stats.unique_errors) == 1
    assert stats.throughput_lines_per_sec > 0.0
