"""Two-tier pipeline orchestrator for HPC log processing.

Integrates wire-speed telemetry filtration, sub-millisecond Rule Tree matching,
and SRE agent fallback with dynamic caching.
"""

import asyncio
import time
from collections.abc import AsyncIterable, AsyncIterator, Callable, Generator, Iterable
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, Field
from pydantic_ai import Agent

from src.agent import _resolve_settings, analyze_log, create_agent
from src.config import Settings
from src.knowledge.docs_db import create_runbook_from_resolution
from src.rules.engine import RuleTreeEngine, extract_template
from src.rules.models import LogEvent, ProblemResolution
from src.rules.telemetry_filter import TelemetryFilter
from src.rules.template_registry import LogTemplateRegistry

__all__ = [
    "LogPipeline",
    "LogProcessingStats",
    "PipelineResult",
    "PipelineStatus",
]

PipelineStatus = Literal[
    "TELEMETRY_FILTERED",
    "FAST_PATH_HIT",
    "IN_FLIGHT_ATTACHED",
    "AGENT_RESOLVED",
]


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

    def record_result(self, result: PipelineResult) -> None:
        """Accumulate counts, costs, and deduplicated errors from a pipeline result."""
        self.total_cost_usd += result.cost_usd

        if result.status == "TELEMETRY_FILTERED":
            self.benign_count += 1
            return

        self.error_count += 1
        if result.status in ("FAST_PATH_HIT", "IN_FLIGHT_ATTACHED"):
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
    """Two-tier stream pipeline orchestrating filtration, fast-path, and SRE triage."""

    def __init__(
        self,
        rule_engine: RuleTreeEngine | None = None,
        agent: Agent[Settings, ProblemResolution] | None = None,
        settings: Settings | None = None,
        agent_cost_usd: float = 0.0015,
        template_registry: LogTemplateRegistry | None = None,
        on_result: Callable[[PipelineResult], Any] | None = None,
        persist_runbooks: bool = False,
        concurrency_limit: int | None = 5,
    ) -> None:
        self.settings: Settings = _resolve_settings(settings)
        self.rule_engine: RuleTreeEngine = (
            rule_engine
            if rule_engine is not None
            else RuleTreeEngine.with_defaults(rules_path=self.settings.rules_path)
        )
        self.agent: Agent[Settings, ProblemResolution] = (
            agent if agent is not None else create_agent(settings=self.settings)
        )
        self.agent_cost_usd: float = agent_cost_usd
        self.template_registry: LogTemplateRegistry = (
            template_registry if template_registry is not None else LogTemplateRegistry()
        )
        self.persist_runbooks: bool = persist_runbooks
        self.concurrency_limit: int | None = concurrency_limit
        self._llm_semaphore: asyncio.Semaphore | None = (
            asyncio.Semaphore(concurrency_limit)
            if concurrency_limit and concurrency_limit > 0
            else None
        )
        self._in_flight: dict[str, asyncio.Future[ProblemResolution]] = {}
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
        source_name: str = "Stream",
        total_lines: int = 0,
    ) -> Generator[LogProcessingStats, None, None]:
        """Context manager to record pipeline metrics in real-time via callbacks."""
        stats = LogProcessingStats(file_name=source_name, total_lines=total_lines)
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
        source_name: str = "Stream",
        concurrency_limit: int | None = 5,
    ) -> LogProcessingStats:
        """Analyze a list of log lines concurrently, collecting stats via pipeline callbacks."""
        with self.collect_stats(source_name=source_name, total_lines=len(raw_lines)) as stats:
            await self.process_logs(raw_lines, concurrency_limit=concurrency_limit)
        return stats

    def _get_dedup_key(self, event: LogEvent) -> str:
        """Derive canonical deduplication key from event subsystem and message template."""
        subsys = event.subsystem.strip().lower()
        template = extract_template(event.message)
        return f"{subsys}:{template}" if subsys else template

    async def process_log(self, raw_line: str) -> PipelineResult:
        """Process a single raw HPC log line through the two-tier pipeline.

        1. Parses raw line via LogTemplateRegistry (combined regex disjunction or fallback).
        2. Filters routine benign telemetry at wire speed (<0.1ms, $0 cost).
        3. Matches against pre-configured and dynamically learned rules (<0.5ms, $0 cost).
        4. Deduplicates concurrent in-flight novel error bursts ($0 cost).
        5. Dispatches unresolved novel errors to the SRE Agent for grounded diagnosis.
        6. Dynamically caches resolutions into RuleTreeEngine and registers new templates.
        """
        start = time.perf_counter()

        # Step 1: Parse line via template registry
        event = self.template_registry.parse(raw_line)

        # Step 2: Wire-speed routine telemetry filtration
        if TelemetryFilter.is_routine_telemetry(event):
            latency_ms = (time.perf_counter() - start) * 1000.0
            return self._notify_result(
                PipelineResult(
                    log_id=event.log_id,
                    status="TELEMETRY_FILTERED",
                    resolution=None,
                    cost_usd=0.0,
                    latency_ms=latency_ms,
                )
            )

        # Step 3: Fast-path rule matching (pre-configured rules + dynamic cache)
        matched_resolution = self.rule_engine.match(event)
        if matched_resolution is not None:
            latency_ms = (time.perf_counter() - start) * 1000.0
            return self._notify_result(
                PipelineResult(
                    log_id=event.log_id,
                    status="FAST_PATH_HIT",
                    resolution=matched_resolution,
                    cost_usd=0.0,
                    latency_ms=latency_ms,
                )
            )

        # Step 4: In-flight burst deduplication
        dedup_key = self._get_dedup_key(event)
        if dedup_key in self._in_flight:
            fut = self._in_flight[dedup_key]
            try:
                attached_resolution = await fut
                latency_ms = (time.perf_counter() - start) * 1000.0
                return self._notify_result(
                    PipelineResult(
                        log_id=event.log_id,
                        status="IN_FLIGHT_ATTACHED",
                        resolution=attached_resolution,
                        cost_usd=0.0,
                        latency_ms=latency_ms,
                    )
                )
            except Exception:
                pass

        loop = asyncio.get_running_loop()
        leader_future: asyncio.Future[ProblemResolution] = loop.create_future()
        self._in_flight[dedup_key] = leader_future

        try:
            # Step 5: Slow-path SRE agent escalation
            resolution = await analyze_log(
                event,
                agent=self.agent,
                settings=self.settings,
                known_groups=self.template_registry.get_known_groups_info(),
            )

            # If unparsed and returned a regex resolution, register new template immediately
            if event.is_unparsed and bool(resolution.suggested_log_template_regex):
                new_tmpl_id = f"DYNAMIC_{event.subsystem or 'UNPARSED'}_{int(time.time() * 1000)}"
                self.template_registry.register_template(
                    template_id=new_tmpl_id,
                    regex=resolution.suggested_log_template_regex,
                    format_string=resolution.format_string,
                    sample_raw_log=event.raw,
                    persist=False,
                )

            # Step 6: Dynamic rule creation into RuleTreeEngine for subsequent instant hits
            self.rule_engine.create_rule_from_resolution(event, resolution)

            # Step 7: Dynamic runbook synthesis and learning into cluster knowledge base
            create_runbook_from_resolution(
                event,
                resolution,
                persist=self.persist_runbooks,
            )

            if not leader_future.done():
                leader_future.set_result(resolution)

            latency_ms = (time.perf_counter() - start) * 1000.0
            return self._notify_result(
                PipelineResult(
                    log_id=event.log_id,
                    status="AGENT_RESOLVED",
                    resolution=resolution,
                    cost_usd=self.agent_cost_usd,
                    latency_ms=latency_ms,
                )
            )
        except BaseException as exc:
            if not leader_future.done():
                leader_future.set_exception(exc)
            raise
        finally:
            self._in_flight.pop(dedup_key, None)

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
