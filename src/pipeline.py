"""Two-tier pipeline orchestrator for HPC log processing.

Integrates single-pass Regex Tree (Parent Envelope -> Child Message Rules) for wire-speed
benign telemetry filtration and fast-path resolution, with SRE agent fallback and dynamic learning.
"""

import asyncio
import time
from collections.abc import AsyncIterable, AsyncIterator, Callable, Generator, Iterable
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, Field

from src.agent import analyze_log
from src.config import Settings, get_settings
from src.knowledge.docs_db import create_runbook_from_resolution
from src.rules.engine import RuleTreeEngine
from src.rules.models import LogEvent, ProblemResolution

__all__ = [
    "LogPipeline",
    "LogProcessingStats",
    "PipelineResult",
    "PipelineStatus",
    "SourceName",
]

PipelineStatus = Literal[
    "TELEMETRY_FILTERED",
    "FAST_PATH_HIT",
    "AGENT_RESOLVED",
]

SourceName = Literal["Stream", "CLI Input", "File"]


class PipelineResult(BaseModel):
    """Result of processing a log event through the streaming pipeline."""

    log_id: str = Field(default="", description="Identifier of the log entry")
    status: PipelineStatus = Field(description="Processing stage outcome")
    resolution: ProblemResolution | None = Field(
        default=None,
        description="Resolved remediation guidance if actionable",
    )
    cost_usd: float = Field(
        default=0.0,
        description="Estimated LLM inference cost in USD",
    )
    latency_ms: float = Field(
        default=0.0,
        description="Processing latency in milliseconds",
    )


@dataclass
class LogProcessingStats:
    """Quantitative stats and remedy recommendations for a processed log stream."""

    source_name: SourceName = "Stream"
    file_name: str = ""
    total_lines: int = 0
    benign_count: int = 0
    error_count: int = 0
    auto_annotated_count: int = 0
    llm_needed_count: int = 0
    total_cost_usd: float = 0.0
    elapsed_time_s: float = 0.0
    throughput_lines_per_sec: float = 0.0
    unique_errors: dict[str, tuple[PipelineResult, int]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.file_name:
            self.file_name = self.source_name

    def record_result(self, result: PipelineResult) -> None:
        """Accumulate counts, costs, and deduplicated errors from a pipeline result."""
        self.total_cost_usd += result.cost_usd

        if result.status == "TELEMETRY_FILTERED":
            self.benign_count += 1
            return

        self.error_count += 1
        if result.status == "FAST_PATH_HIT":
            self.auto_annotated_count += 1
        elif result.status == "AGENT_RESOLVED":
            self.llm_needed_count += 1

        if result.resolution:
            key = result.resolution.root_cause.strip()
            if key in self.unique_errors:
                prev_res, count = self.unique_errors[key]
                self.unique_errors[key] = (prev_res, count + 1)
            else:
                self.unique_errors[key] = (result, 1)

    def record_results(self, results: Iterable[PipelineResult]) -> None:
        """Accumulate metrics across an iterable of pipeline results."""
        for result in results:
            self.record_result(result)

    def finalize(self, elapsed_seconds: float) -> None:
        """Finalize execution timing and throughput statistics."""
        self.elapsed_time_s = elapsed_seconds
        self.throughput_lines_per_sec = (
            (self.total_lines / elapsed_seconds) if elapsed_seconds > 0 else 0.0
        )


class LogPipeline:
    """Two-tier stream pipeline orchestrating Regex Tree matching and SRE agent triage."""

    def __init__(
        self,
        rule_engine: RuleTreeEngine | None = None,
        settings: Settings | None = None,
        agent_cost_usd: float = 0.0015,
        on_result: Callable[[PipelineResult], Any] | None = None,
        persist_runbooks: bool = False,
        concurrency_limit: int | None = 5,
    ) -> None:
        self.settings: Settings = settings or get_settings()
        self.rule_engine: RuleTreeEngine = (
            rule_engine
            if rule_engine is not None
            else RuleTreeEngine.with_defaults(rules_path=self.settings.rules_path)
        )
        self.agent_cost_usd: float = agent_cost_usd
        self.persist_runbooks: bool = persist_runbooks
        self.concurrency_limit: int | None = concurrency_limit
        self._callbacks: list[Callable[[PipelineResult], Any]] = []
        if on_result is not None:
            self._callbacks.append(on_result)

    def add_callback(self, callback: Callable[[PipelineResult], Any]) -> None:
        """Register a callback to be invoked on every processed PipelineResult."""
        if callback not in self._callbacks:
            self._callbacks.append(callback)

    def remove_callback(self, callback: Callable[[PipelineResult], Any]) -> None:
        """Remove a registered result callback."""
        if callback in self._callbacks:
            self._callbacks.remove(callback)

    def _notify_result(self, result: PipelineResult) -> PipelineResult:
        """Dispatch a PipelineResult to all registered callbacks and return the result."""
        for cb in self._callbacks:
            try:
                cb(result)
            except Exception:
                pass
        return result

    @contextmanager
    def collect_stats(
        self,
        source_name: SourceName = "Stream",
        total_lines: int = 0,
        file_name: str = "",
    ) -> Generator[LogProcessingStats, None, None]:
        """Context manager to record pipeline metrics in real-time via callbacks."""
        stats = LogProcessingStats(
            source_name=source_name,
            file_name=file_name or source_name,
            total_lines=total_lines,
        )
        self.add_callback(stats.record_result)
        start_time = time.perf_counter()
        try:
            yield stats
        finally:
            self.remove_callback(stats.record_result)
            stats.finalize(time.perf_counter() - start_time)

    async def analyze(
        self,
        raw_lines: list[str],
        source_name: SourceName = "Stream",
        concurrency_limit: int | None = 5,
        file_name: str = "",
    ) -> LogProcessingStats:
        """Analyze a list of log lines concurrently, collecting stats via pipeline callbacks."""
        with self.collect_stats(
            source_name=source_name,
            total_lines=len(raw_lines),
            file_name=file_name,
        ) as stats:
            await self.process_logs(raw_lines, concurrency_limit=concurrency_limit)
        return stats

    def _match_fast_path(
        self,
        event: LogEvent,
        matched_rule: Any,
        start: float,
    ) -> PipelineResult | None:
        """Return a fast-path or telemetry-filtered PipelineResult if matched_rule applies."""
        log_id = str(event.get("log_id", ""))
        if matched_rule is not None and matched_rule.rule_type == "BENIGN_TELEMETRY":
            latency_ms = (time.perf_counter() - start) * 1000.0
            return self._notify_result(
                PipelineResult(
                    log_id=log_id,
                    status="TELEMETRY_FILTERED",
                    resolution=None,
                    cost_usd=0.0,
                    latency_ms=latency_ms,
                )
            )
        if matched_rule is not None and matched_rule.resolution is not None:
            latency_ms = (time.perf_counter() - start) * 1000.0
            return self._notify_result(
                PipelineResult(
                    log_id=log_id,
                    status="FAST_PATH_HIT",
                    resolution=matched_rule.resolution,
                    cost_usd=0.0,
                    latency_ms=latency_ms,
                )
            )
        return None

    async def _escalate_and_learn(
        self,
        event: LogEvent,
        start: float,
    ) -> tuple[ProblemResolution, PipelineResult]:
        """Escalate event to the SRE Agent, register/dedup learned rules, and build result."""
        log_id = str(event.get("log_id", ""))
        resolution = await analyze_log(
            event,
            known_groups=self.rule_engine.get_known_groups_info(),
        )
        self.rule_engine.create_rule_from_resolution(event, resolution)
        if not resolution.is_benign:
            create_runbook_from_resolution(
                event,
                resolution,
                persist=self.persist_runbooks,
            )
        latency_ms = (time.perf_counter() - start) * 1000.0
        if resolution.is_benign:
            res = self._notify_result(
                PipelineResult(
                    log_id=log_id,
                    status="TELEMETRY_FILTERED",
                    resolution=None,
                    cost_usd=self.agent_cost_usd,
                    latency_ms=latency_ms,
                )
            )
        else:
            res = self._notify_result(
                PipelineResult(
                    log_id=log_id,
                    status="AGENT_RESOLVED",
                    resolution=resolution,
                    cost_usd=self.agent_cost_usd,
                    latency_ms=latency_ms,
                )
            )
        return resolution, res

    async def process_log(self, raw_line: str) -> PipelineResult:
        """Process a single raw HPC log line through the two-tier Regex Tree pipeline.

        1. Matches raw line against the Regex Tree (Parent Envelope -> Child Rule).
        2. Filters benign telemetry child rules at wire speed (<0.1ms, $0 cost).
        3. Resolves actionable error child rules via fast-path (<0.1ms, $0 cost).
        4. Escalates novel logs to the SRE Agent and registers/deduplicates parent/child rules.
        """
        start = time.perf_counter()

        event, matched_rule = self.rule_engine.match(raw_line)
        fast_res = self._match_fast_path(event, matched_rule, start)
        if fast_res is not None:
            return fast_res

        _, result = await self._escalate_and_learn(event, start)
        return result

    async def process_logs(
        self,
        logs: Iterable[str],
        concurrency_limit: int | None = 5,
    ) -> list[PipelineResult]:
        """Process an iterable of log lines concurrently via asyncio."""
        log_list = list(logs)
        if not log_list:
            return []
        limit = concurrency_limit if concurrency_limit is not None else self.concurrency_limit
        if limit and limit > 0:
            sem = asyncio.Semaphore(limit)

            async def _worker(line: str) -> PipelineResult:
                async with sem:
                    return await self.process_log(line)

            return list(await asyncio.gather(*(_worker(line) for line in log_list)))
        return list(await asyncio.gather(*(self.process_log(line) for line in log_list)))

    async def process_stream(
        self,
        stream: AsyncIterable[str] | Iterable[str],
    ) -> AsyncIterator[PipelineResult]:
        """Process an asynchronous or synchronous iterable of log lines."""
        if isinstance(stream, AsyncIterable):
            async for raw_line in stream:
                yield await self.process_log(raw_line)
        else:
            for raw_line in stream:
                yield await self.process_log(raw_line)
