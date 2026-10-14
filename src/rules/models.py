"""Data models for HPC log events, rules, and problem resolutions."""

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr

ValidationStatus = Literal["PENDING", "VALIDATED", "REJECTED"]


class ProblemResolution(BaseModel):
    """Structured resolution schema for HPC error diagnosis and remediation."""

    error_summary: str = Field(default="", description="Summary of the detected error")
    root_cause: str = Field(default="", description="Root cause of the error")
    severity: str = Field(
        default="UNKNOWN",
        description="Severity level (e.g., LOW, MEDIUM, HIGH, CRITICAL)",
    )
    remediation_steps: list[str] = Field(
        default_factory=list,
        description="Ordered remediation actions",
    )
    citations_and_reasons: list[str] = Field(
        default_factory=list,
        description="Citations and technical rationale",
    )
    suggested_log_template_regex: str = Field(
        default="",
        description=(
            "Generalized Python regular expression pattern matching this failure mode "
            "for fast-path problem resolution and template parsing."
        ),
    )
    format_string: str = Field(
        default="",
        description=(
            "Optional format string using named capture group keys "
            "to reconstruct the original raw log line."
        ),
    )


class ProblemResolutionWithRegex(ProblemResolution):
    """Backwards-compatible subclass for resolutions containing template regex."""

    pass


RuleType = Literal["ACTIONABLE_ERROR", "BENIGN_TELEMETRY"]


class Rule(BaseModel):
    """Pre-configured rule for fast-path problem resolution or routine telemetry filtration."""

    rule_id: str = Field(default="", description="Unique identifier for the rule")
    parent_id: str = Field(default="*", description="Parent envelope template ID or '*' for all")
    subsystem: str = Field(default="*", description="Subsystem filter or '*' for all")
    event_type: str = Field(default="*", description="Event type filter or '*' for all")
    pattern: str = Field(
        default="",
        description="Regular expression pattern to match against event.message",
    )
    rule_type: RuleType = Field(
        default="ACTIONABLE_ERROR",
        description="Rule classification: 'ACTIONABLE_ERROR' or 'BENIGN_TELEMETRY'",
    )
    resolution: ProblemResolution | None = Field(
        default=None,
        description="ProblemResolution payload returned upon match",
    )
    human_validated: bool = Field(
        default=False,
        description="Whether the rule has been verified by a human operator",
    )
    validation_status: ValidationStatus = Field(
        default="VALIDATED",
        description="Validation lifecycle state ('PENDING', 'VALIDATED', 'REJECTED')",
    )

    _compiled: re.Pattern[str] | None = PrivateAttr(default=None)

    def get_compiled_pattern(self) -> re.Pattern[str]:
        """Return or lazily compile the cached regex pattern."""
        if self._compiled is None:
            try:
                self._compiled = re.compile(self.pattern, re.IGNORECASE)
            except re.error:
                self._compiled = re.compile(re.escape(self.pattern), re.IGNORECASE)
        return self._compiled

    def matches(self, message: str) -> bool:
        """Check whether message matches the rule pattern."""
        try:
            return bool(self.get_compiled_pattern().search(message))
        except Exception:
            return False


class ParentEnvelope(BaseModel):
    """Parent log envelope regex defining outer log structure and extracting fields."""

    parent_id: str = Field(description="Unique identifier for the envelope template")
    envelope_regex: str = Field(
        description="Python regex with named capture groups (?P<name>...) ending in (?P<message>.*)"
    )
    description: str = Field(
        default="",
        description="Human-readable description of this log format",
    )
    children: list[Rule] = Field(
        default_factory=list,
        description="Child message rules scoped under this envelope",
    )

    _compiled: re.Pattern[str] | None = PrivateAttr(default=None)

    def get_compiled_pattern(self) -> re.Pattern[str]:
        """Return or lazily compile the cached envelope regex."""
        if self._compiled is None:
            self._compiled = re.compile(self.envelope_regex)
        return self._compiled

    def match(self, raw_line: str) -> re.Match[str] | None:
        """Match a raw log line against this envelope regex."""
        try:
            return self.get_compiled_pattern().search(raw_line)
        except Exception:
            return None

    def match_child(self, message: str) -> Rule | None:
        """Evaluate children rules against message, returning first non-rejected match."""
        for child in self.children:
            if child.validation_status != "REJECTED" and child.matches(message):
                return child
        return None


class LogEvent(BaseModel):
    """Normalized structured representation of an HPC log stream event."""

    model_config = ConfigDict(extra="allow")

    log_id: str = Field(default="", description="Unique identifier of the log entry")
    node: str = Field(default="", description="Cluster node identifier (e.g., node-25)")
    subsystem: str = Field(default="", description="Subsystem or daemon reporting the event")
    event_type: str = Field(default="", description="Specific event category or type")
    timestamp: int = Field(default=0, description="Epoch timestamp of the event")
    flag: int = Field(default=0, description="Log flag or status code")
    message: str = Field(default="", description="Normalized message body")
    raw: str = Field(default="", description="Original unparsed log line")
    format_string: str = Field(
        default="",
        description="Format string using named capture groups to reconstruct the original log",
    )
    is_unparsed: bool = Field(
        default=False,
        description="Whether log was unparsed by registered templates",
    )

    def __init__(
        self,
        log_id: str = "",
        node: str = "",
        subsystem: str = "",
        event_type: str = "",
        timestamp: int = 0,
        flag: int = 0,
        message: str = "",
        raw: str = "",
        format_string: str = "",
        is_unparsed: bool = False,
        **data: Any,
    ) -> None:
        if (
            log_id
            or node
            or subsystem
            or event_type
            or timestamp
            or flag
            or message
            or raw
            or format_string
            or is_unparsed
        ):
            data.setdefault("log_id", log_id)
            data.setdefault("node", node)
            data.setdefault("subsystem", subsystem)
            data.setdefault("event_type", event_type)
            data.setdefault("timestamp", timestamp)
            data.setdefault("flag", flag)
            data.setdefault("message", message)
            data.setdefault("raw", raw)
            data.setdefault("format_string", format_string)
            data.setdefault("is_unparsed", is_unparsed)
        super().__init__(**data)

    def reconstruct(self) -> str:
        """Reconstruct the original raw log line using format_string and key-value fields."""
        if self.format_string:
            try:
                data = self.model_dump()
                return self.format_string.format_map(data)
            except Exception:
                pass
        return self.raw
