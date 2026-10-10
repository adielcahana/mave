"""Two-tier pipeline orchestrator for HPC log processing.

Integrates wire-speed telemetry filtration, sub-millisecond Rule Tree matching,
and SRE agent fallback with dynamic caching.
"""

import time
from collections.abc import AsyncIterable, AsyncIterator, Iterable
from typing import Literal

from pydantic import BaseModel, Field
from pydantic_ai import Agent

from src.agent import _resolve_settings, analyze_log, create_agent
from src.config import Settings
from src.rules.engine import RuleTreeEngine
from src.rules.models import ProblemResolution, ProblemResolutionWithRegex
from src.rules.telemetry_filter import TelemetryFilter
from src.rules.template_registry import LogTemplateRegistry

__all__ = [
    "LogPipeline",
    "PipelineResult",
    "PipelineStatus",
]

PipelineStatus = Literal[
    "TELEMETRY_FILTERED",
    "FAST_PATH_HIT",
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
        agent_cost_usd: float = 0.0015,
        template_registry: LogTemplateRegistry | None = None,
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

    async def process_log(self, raw_line: str) -> PipelineResult:
        """Process a single raw HPC log line through the two-tier pipeline.

        1. Parses raw line via LogTemplateRegistry (combined regex disjunction or fallback).
        2. Filters routine benign telemetry at wire speed (<0.1ms, $0 cost).
        3. Matches against pre-configured and dynamically learned rules (<0.5ms, $0 cost).
        4. Dispatches unresolved novel errors to the SRE Agent for grounded diagnosis.
        5. Dynamically caches resolutions into RuleTreeEngine and registers new templates.
        """
        start = time.perf_counter()

        # Step 1: Parse line via template registry
        event = self.template_registry.parse(raw_line)

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

        # Step 4: Slow-path SRE agent escalation
        resolution = await analyze_log(
            event,
            agent=self.agent,
            settings=self.settings,
        )

        # If unparsed and returned a regex resolution, register new template immediately
        if event.is_unparsed and isinstance(resolution, ProblemResolutionWithRegex):
            new_tmpl_id = f"DYNAMIC_{event.subsystem or 'UNPARSED'}_{int(time.time() * 1000)}"
            self.template_registry.register_template(
                template_id=new_tmpl_id,
                regex=resolution.suggested_log_template_regex,
                persist=False,
            )

        # Step 5: Dynamic rule creation into RuleTreeEngine for subsequent instant hits
        self.rule_engine.create_rule_from_resolution(event, resolution)

        latency_ms = (time.perf_counter() - start) * 1000.0
        return PipelineResult(
            log_id=event.log_id,
            status="AGENT_RESOLVED",
            resolution=resolution,
            cost_usd=self.agent_cost_usd,
            latency_ms=latency_ms,
        )

    async def process_logs(
        self,
        logs: Iterable[str],
    ) -> list[PipelineResult]:
        """Process an iterable of log lines sequentially."""
        results: list[PipelineResult] = []
        for line in logs:
            results.append(await self.process_log(line))
        return results

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
