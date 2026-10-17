"""Evaluation harness and metrics calculations for HPC problem resolution."""

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, model_validator

from src.rules.engine import RuleTreeEngine

__all__ = [
    "BenchmarkMetrics",
    "GoldenLogEntry",
    "evaluate_benchmark",
    "format_benchmark_summary",
    "format_metrics_table",
]


class GoldenLogEntry(BaseModel):
    """Labeled log entry from the curated golden evaluation dataset."""

    raw: str = Field(description="Raw HPC log line")
    is_actionable: bool = Field(
        default=False,
        description="Whether log represents an actionable incident",
    )
    expected_severity: str = Field(default="INFO", description="Expected incident severity")
    category: str = Field(default="", description="Specific failure or telemetry category")

    @model_validator(mode="before")
    @classmethod
    def _normalize_keys(cls, data: Any) -> Any:
        if isinstance(data, dict):
            if "is_actionable" not in data and "is_error" in data:
                data["is_actionable"] = data["is_error"]
        return data


class BenchmarkMetrics(BaseModel):
    """Performance and triage metrics across the evaluation benchmark."""

    total: int = Field(default=0, description="Total number of evaluated log instances")
    precision: float = Field(default=0.0, description="Precision (TP / (TP + FP))")
    recall: float = Field(default=0.0, description="Recall (TP / (TP + FN))")
    f1: float = Field(default=0.0, description="F1 Score harmonic mean")
    accuracy: float = Field(default=0.0, description="Overall classification accuracy")
    telemetry_filtered: int = Field(
        default=0,
        description="Benign telemetry entries filtered at wire speed",
    )
    fast_path_hits: int = Field(
        default=0,
        description="Actionable logs resolved via sub-millisecond rule tree",
    )
    agent_fallbacks: int = Field(
        default=0,
        description="Unrecognized actionable logs escalated to SRE Agent",
    )
    false_positives: int = Field(
        default=0,
        description="Benign logs incorrectly marked actionable",
    )
    false_negatives: int = Field(
        default=0,
        description="Actionable logs missed or marked benign",
    )
    true_positives: int = Field(
        default=0,
        description="Actionable logs correctly identified",
    )
    true_negatives: int = Field(
        default=0,
        description="Benign logs correctly filtered",
    )


def evaluate_benchmark(
    benchmark_path: Path | str,
    rule_engine: RuleTreeEngine | None = None,
) -> BenchmarkMetrics:
    """Evaluate triage accuracy and fast-path resolution across the golden benchmark.

    Evaluates log stream triage deterministically via the unified RuleTreeEngine:
    1. Matches raw log lines against `RuleTreeEngine.match` (Parent Envelope -> Child Rule).
    2. If matched rule has `rule_type == 'BENIGN_TELEMETRY'`, the log is filtered
       (predicted `is_actionable = False`).
    3. Otherwise, if a child rule with a resolution matched, registers a fast-path hit;
       if no child rule matched, registers an agent fallback (both `is_actionable = True`).
    4. Computes Precision, Recall, F1, Accuracy, and confusion counts.
    """
    path = Path(benchmark_path)
    if not path.is_file():
        raise FileNotFoundError(f"Golden benchmark dataset not found at {path}")

    with open(path, encoding="utf-8") as f:
        data = json.load(f)

    if not isinstance(data, list):
        raise ValueError(f"Golden benchmark at {path} must contain a JSON array")

    entries = [GoldenLogEntry.model_validate(item) for item in data]
    total = len(entries)

    engine = rule_engine if rule_engine is not None else RuleTreeEngine.with_defaults()

    tp = 0
    fp = 0
    tn = 0
    fn = 0
    telemetry_filtered = 0
    fast_path_hits = 0
    agent_fallbacks = 0

    for entry in entries:
        _event, matched_rule = engine.match(entry.raw)

        if matched_rule is not None and matched_rule.rule_type == "BENIGN_TELEMETRY":
            telemetry_filtered += 1
            predicted_actionable = False
        else:
            if matched_rule is not None and matched_rule.resolution is not None:
                fast_path_hits += 1
            else:
                agent_fallbacks += 1
            predicted_actionable = True

        actual_actionable = entry.is_actionable

        if predicted_actionable and actual_actionable:
            tp += 1
        elif predicted_actionable and not actual_actionable:
            fp += 1
        elif not predicted_actionable and not actual_actionable:
            tn += 1
        else:
            fn += 1

    precision = tp / (tp + fp) if (tp + fp) > 0 else 0.0
    recall = tp / (tp + fn) if (tp + fn) > 0 else 0.0
    f1 = (2 * precision * recall) / (precision + recall) if (precision + recall) > 0 else 0.0
    accuracy = (tp + tn) / total if total > 0 else 0.0

    return BenchmarkMetrics(
        total=total,
        precision=precision,
        recall=recall,
        f1=f1,
        accuracy=accuracy,
        telemetry_filtered=telemetry_filtered,
        fast_path_hits=fast_path_hits,
        agent_fallbacks=agent_fallbacks,
        false_positives=fp,
        false_negatives=fn,
        true_positives=tp,
        true_negatives=tn,
    )


def format_metrics_table(metrics: BenchmarkMetrics) -> str:
    """Format benchmark evaluation metrics into an aligned ASCII table."""
    filt_pct = (metrics.telemetry_filtered / metrics.total * 100) if metrics.total > 0 else 0.0
    fast_pct = (metrics.fast_path_hits / metrics.total * 100) if metrics.total > 0 else 0.0
    fall_pct = (metrics.agent_fallbacks / metrics.total * 100) if metrics.total > 0 else 0.0

    sep = "+" + "-" * 59 + "+"
    header = f"|{'HPC Golden Evaluation Metrics':^59}|"

    def row(label: str, value: str) -> str:
        return f"| {label:<27} | {value:<27} |"

    lines = [
        sep,
        header,
        sep,
        row("Total Evaluations", str(metrics.total)),
        row("Accuracy", f"{metrics.accuracy:.1%}"),
        row("Precision", f"{metrics.precision:.1%}"),
        row("Recall", f"{metrics.recall:.1%}"),
        row("F1 Score", f"{metrics.f1:.3f}"),
        sep,
        row("Telemetry Filtered (Drop)", f"{metrics.telemetry_filtered} ({filt_pct:.1f}%)"),
        row("Fast-Path Hits ($0, <1ms)", f"{metrics.fast_path_hits} ({fast_pct:.1f}%)"),
        row("Agent Fallbacks (LLM)", f"{metrics.agent_fallbacks} ({fall_pct:.1f}%)"),
        sep,
        row("True Positives", str(metrics.true_positives)),
        row("True Negatives", str(metrics.true_negatives)),
        row("False Positives", str(metrics.false_positives)),
        row("False Negatives", str(metrics.false_negatives)),
        sep,
    ]
    return "\n".join(lines)


def format_benchmark_summary(metrics: BenchmarkMetrics) -> str:
    """Return a comprehensive text summary of evaluation benchmark metrics."""
    return format_metrics_table(metrics)
