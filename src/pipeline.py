"""Two-tier streaming pipeline orchestrator for HPC log streams.

Integrates wire-speed telemetry filtration, sub-millisecond Rule Tree matching,
in-flight burst deduplication, and SRE agent fallback with dynamic caching.
"""

import asyncio
import time
from collections.abc import AsyncIterable, AsyncIterator, Iterable
from typing import Literal

from pydantic import BaseModel, Field
from pydantic_ai import Agent

from src.agent import _resolve_settings, analyze_log, create_agent
from src.config import Settings
from src.rules.engine import RuleTreeEngine, extract_template
from src.rules.models import LogEvent, ProblemResolution
from src.rules.normalizer import parse_log_line
from src.rules.telemetry_filter import TelemetryFilter

__all__ = [
    "LogPipeline",
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


class LogPipeline:
    """Two-tier stream pipeline orchestrating filtration, fast-path, and SRE triage."""

    def __init__(
        self,
        rule_engine: RuleTreeEngine | None = None,
        agent: Agent[Settings, ProblemResolution] | None = None,
        settings: Settings | None = None,
        burst_window_seconds: float = 5.0,
        agent_cost_usd: float = 0.0015,
        max_burst_entries: int = 10_000,
    ) -> None:
        self.settings: Settings = _resolve_settings(settings)
        self.rule_engine: RuleTreeEngine = (
            rule_engine if rule_engine is not None else RuleTreeEngine.with_defaults()
        )
        self.agent: Agent[Settings, ProblemResolution] = (
            agent if agent is not None else create_agent(settings=self.settings)
        )
        self.burst_window_seconds: float = burst_window_seconds
        self.agent_cost_usd: float = agent_cost_usd
        self._max_burst_entries: int = max_burst_entries

        # In-flight deduplication: dedup_key -> Future[ProblemResolution]
        self._in_flight: dict[str, asyncio.Future[ProblemResolution]] = {}
        # Recent burst cache: dedup_key -> (ProblemResolution, monotonic_timestamp)
        self._recent_bursts: dict[str, tuple[ProblemResolution, float]] = {}

    def _cleanup_bursts(self, now: float | None = None) -> None:
        """Evict burst entries older than the burst window."""
        current_time = time.monotonic() if now is None else now
        expired = [
            k
            for k, (_, ts) in self._recent_bursts.items()
            if current_time - ts >= self.burst_window_seconds
        ]
        for k in expired:
            self._recent_bursts.pop(k, None)

    def _record_burst(self, dedup_key: str, resolution: ProblemResolution) -> None:
        """Record resolution in recent burst cache with TTL eviction and capacity ceiling."""
        now = time.monotonic()
        self._cleanup_bursts(now)
        self._recent_bursts[dedup_key] = (resolution, now)
        if len(self._recent_bursts) > self._max_burst_entries:
            oldest_key = min(self._recent_bursts, key=lambda k: self._recent_bursts[k][1])
            self._recent_bursts.pop(oldest_key, None)

    def _get_dedup_key(self, event: LogEvent) -> str:
        """Derive canonical deduplication key from event subsystem and message template."""
        subsys = event.subsystem.strip().lower()
        template = extract_template(event.message)
        return f"{subsys}:{template}" if subsys else template

    async def process_log(self, raw_line: str) -> PipelineResult:
        """Process a single raw HPC log line through the two-tier streaming pipeline.

        1. Normalizes raw line into a strongly typed LogEvent.
        2. Filters routine benign telemetry at wire speed (<0.1ms, $0 cost).
        3. Matches against pre-configured and dynamically learned rules (<0.5ms, $0 cost).
        4. Deduplicates novel error bursts in-flight or within a 5-second window ($0 cost).
        5. Dispatches unresolved novel errors to the SRE Agent for grounded diagnosis.
        6. Dynamically caches resolutions into RuleTreeEngine for subsequent instant hits.
        """
        start = time.perf_counter()

        # Step 1: Normalize line
        event = parse_log_line(raw_line)

        # Step 2: Wire-speed routine telemetry filtration
        if TelemetryFilter.is_routine_telemetry(event):
            latency_ms = (time.perf_counter() - start) * 1000.0
            return PipelineResult(
                log_id=event.log_id,
                status="TELEMETRY_FILTERED",
                resolution=None,
                cost_usd=0.0,
                latency_ms=latency_ms,
            )

        # Step 3: Fast-path rule matching (pre-configured rules + dynamic cache)
        matched_resolution = self.rule_engine.match(event)
        if matched_resolution is not None:
            latency_ms = (time.perf_counter() - start) * 1000.0
            return PipelineResult(
                log_id=event.log_id,
                status="FAST_PATH_HIT",
                resolution=matched_resolution,
                cost_usd=0.0,
                latency_ms=latency_ms,
            )

        # Step 4: In-flight burst deduplication and burst window check
        dedup_key = self._get_dedup_key(event)

        if dedup_key in self._in_flight:
            # Another coroutine is already analyzing this novel error burst
            fut = self._in_flight[dedup_key]
            try:
                attached_resolution = await fut
                status: PipelineStatus = "IN_FLIGHT_ATTACHED"
                cost_usd = 0.0
            except (Exception, asyncio.CancelledError):
                attached_resolution = await analyze_log(
                    event, agent=self.agent, settings=self.settings
                )
                self.rule_engine.cache_resolution(event, attached_resolution)
                self._record_burst(dedup_key, attached_resolution)
                status = "AGENT_RESOLVED"
                cost_usd = self.agent_cost_usd

            latency_ms = (time.perf_counter() - start) * 1000.0
            return PipelineResult(
                log_id=event.log_id,
                status=status,
                resolution=attached_resolution,
                cost_usd=cost_usd,
                latency_ms=latency_ms,
            )

        # Check recent burst window (<5 seconds)
        now = time.monotonic()
        self._cleanup_bursts(now)
        if dedup_key in self._recent_bursts:
            recent_res, recent_ts = self._recent_bursts[dedup_key]
            if now - recent_ts < self.burst_window_seconds:
                latency_ms = (time.perf_counter() - start) * 1000.0
                return PipelineResult(
                    log_id=event.log_id,
                    status="IN_FLIGHT_ATTACHED",
                    resolution=recent_res,
                    cost_usd=0.0,
                    latency_ms=latency_ms,
                )

        # Step 5: Slow-path SRE agent escalation
        loop = asyncio.get_running_loop()
        leader_future: asyncio.Future[ProblemResolution] = loop.create_future()
        self._in_flight[dedup_key] = leader_future

        try:
            resolution = await analyze_log(
                event,
                agent=self.agent,
                settings=self.settings,
            )

            # Step 6: Dynamic caching into RuleTreeEngine and burst window tracking
            self.rule_engine.cache_resolution(event, resolution)
            self._record_burst(dedup_key, resolution)

            if not leader_future.done():
                leader_future.set_result(resolution)

            latency_ms = (time.perf_counter() - start) * 1000.0
            # Step 7: Return AGENT_RESOLVED
            return PipelineResult(
                log_id=event.log_id,
                status="AGENT_RESOLVED",
                resolution=resolution,
                cost_usd=self.agent_cost_usd,
                latency_ms=latency_ms,
            )
        except BaseException as exc:
            if not leader_future.done():
                leader_future.set_exception(exc)
            raise
        finally:
            self._in_flight.pop(dedup_key, None)

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
