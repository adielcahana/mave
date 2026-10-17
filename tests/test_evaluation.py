"""Tests for Curated Golden Evaluation Benchmark and Evaluation Harness."""

import json
from pathlib import Path

import pytest

from src.evaluation import (
    BenchmarkMetrics,
    evaluate_benchmark,
    format_benchmark_summary,
    format_metrics_table,
)
from src.rules.engine import RuleTreeEngine


@pytest.fixture
def golden_eval_path() -> Path:
    return Path("data/golden_eval.json")


@pytest.fixture
def mock_rule_engine() -> RuleTreeEngine:
    return RuleTreeEngine.with_defaults(rules_path="tests/fixtures/rules.json")


def test_evaluation_benchmark_executes(golden_eval_path: Path, mock_rule_engine: RuleTreeEngine):
    """Verify running benchmark on data/golden_eval.json returns valid metrics with total == 150."""
    metrics = evaluate_benchmark(golden_eval_path, mock_rule_engine)

    assert isinstance(metrics, BenchmarkMetrics)
    assert metrics.total == 150
    confusion_sum = (
        metrics.true_positives
        + metrics.true_negatives
        + metrics.false_positives
        + metrics.false_negatives
    )
    assert confusion_sum == 150
    assert 0.0 <= metrics.precision <= 1.0
    assert 0.0 <= metrics.recall <= 1.0
    assert 0.0 <= metrics.f1 <= 1.0
    assert 0.0 <= metrics.accuracy <= 1.0
    assert metrics.telemetry_filtered > 0
    assert metrics.fast_path_hits > 0
    assert metrics.agent_fallbacks > 0
    assert metrics.telemetry_filtered + metrics.fast_path_hits + metrics.agent_fallbacks == 150


def test_golden_dataset_structure(golden_eval_path: Path):
    """Verify all 150 entries in data/golden_eval.json have required keys and diverse categories."""
    assert golden_eval_path.exists(), f"{golden_eval_path} does not exist"

    with open(golden_eval_path, encoding="utf-8") as f:
        data = json.load(f)

    assert isinstance(data, list)
    assert len(data) == 150, f"Expected exactly 150 entries, found {len(data)}"

    required_keys = {"raw", "is_actionable", "expected_severity", "category"}
    categories = set()
    severities = set()
    actionable_count = 0
    non_actionable_count = 0

    for idx, entry in enumerate(data):
        assert isinstance(entry, dict), f"Entry {idx} is not a dict"
        missing = required_keys - set(entry.keys())
        assert not missing, f"Entry {idx} missing keys: {missing}"
        assert "expected_subsystem" not in entry

        assert isinstance(entry["raw"], str) and len(entry["raw"].strip()) > 0
        assert isinstance(entry["is_actionable"], bool)
        assert isinstance(entry["expected_severity"], str)
        assert isinstance(entry["category"], str)

        categories.add(entry["category"])
        severities.add(entry["expected_severity"])

        if entry["is_actionable"]:
            actionable_count += 1
        else:
            non_actionable_count += 1

    # Diverse categories across routine telemetry and failure modes
    assert len(categories) >= 10, f"Expected at least 10 categories, found {len(categories)}"
    # Balanced mix of actionable and non-actionable
    assert actionable_count > 0 and non_actionable_count > 0
    assert abs(actionable_count - non_actionable_count) <= 20


def test_telemetry_invariance_in_evaluation(
    golden_eval_path: Path, mock_rule_engine: RuleTreeEngine
):
    """Verify zero false positives on normal cluster telemetry (ambient=*, normal, Link ok)."""
    metrics = evaluate_benchmark(golden_eval_path, mock_rule_engine)
    assert metrics.false_positives == 0, f"False positives: {metrics.false_positives}"
    assert metrics.precision == 1.0


def test_format_metrics_table_and_summary(golden_eval_path: Path, mock_rule_engine: RuleTreeEngine):
    """Verify pretty text table formatters work cleanly."""
    metrics = evaluate_benchmark(golden_eval_path, mock_rule_engine)
    table = format_metrics_table(metrics)
    summary = format_benchmark_summary(metrics)

    assert "Evaluation Metrics" in table or "Precision" in table
    assert str(metrics.total) in table
    assert f"{metrics.precision:.1%}" in table or "100" in table
    assert len(summary) > 0


def test_evaluate_benchmark_string_path_and_default_engine(
    golden_eval_path: Path, mock_rule_engine: RuleTreeEngine
):
    """Verify evaluate_benchmark works with str path and creates default engine when None."""
    metrics = evaluate_benchmark(str(golden_eval_path), rule_engine=mock_rule_engine)
    assert metrics.total == 150
    assert metrics.fast_path_hits > 0


def test_golden_log_entry_alias_is_error():
    """Verify GoldenLogEntry maps backwards-compatible 'is_error' key."""
    from src.evaluation import GoldenLogEntry

    entry = GoldenLogEntry.model_validate(
        {
            "raw": "1000 node-1 switch_module status 100 1 normal",
            "is_error": True,
            "category": "test",
        }
    )
    assert entry.is_actionable is True


def test_evaluate_benchmark_empty_engine(golden_eval_path: Path):
    """Verify evaluate_benchmark with empty rule engine routes all logs to agent fallback."""
    empty_engine = RuleTreeEngine()
    metrics = evaluate_benchmark(golden_eval_path, rule_engine=empty_engine)
    assert metrics.fast_path_hits == 0
    assert metrics.telemetry_filtered == 0
    assert metrics.agent_fallbacks == 150


def test_evaluate_benchmark_missing_file():
    """Verify evaluate_benchmark raises FileNotFoundError when benchmark file is missing."""
    with pytest.raises(FileNotFoundError):
        evaluate_benchmark(Path("data/non_existent_benchmark.json"))


def test_evaluate_benchmark_invalid_json(tmp_path: Path):
    """Verify evaluate_benchmark raises ValueError when benchmark is not a JSON list."""
    bad_file = tmp_path / "bad.json"
    bad_file.write_text(json.dumps({"key": "value"}), encoding="utf-8")
    with pytest.raises(ValueError, match="must contain a JSON array"):
        evaluate_benchmark(bad_file)
