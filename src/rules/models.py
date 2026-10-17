"""Data models for HPC log events, regex tree envelopes, child rules, and resolutions."""

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr

ValidationStatus = Literal["PENDING", "VALIDATED", "REJECTED"]
RuleType = Literal["ACTIONABLE_ERROR", "BENIGN_TELEMETRY"]


class ProblemResolution(BaseModel):
    """Structured resolution schema for HPC error diagnosis and remediation."""

    error_summary: str = Field(default="", description="Summary of the detected error")
    root_cause: str = Field(default="", description="Root cause of the error")
    severity: str = Field(
        default="UNKNOWN",
        description="Severity level (e.g., INFO, LOW, MEDIUM, HIGH, CRITICAL)",
    )
    is_benign: bool = Field(
        default=False,
        description="True if the log event represents routine benign telemetry requiring no action",
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
            "Generalized Python regular expression pattern matching the parent log envelope "
            "(when unparsed) or the child failure message (when envelope is parsed)."
        ),
    )
    suggested_message_regex: str = Field(
        default="",
        description=(
            "Optional generalized Python regex matching the extracted message body "
            "when synthesizing a new parent envelope for an unparsed log."
        ),
    )
    format_string: str = Field(
        default="",
        description=(
            "Optional format string using named capture group keys "
            "to reconstruct the original raw log line."
        ),
    )


class Rule(BaseModel):
    """Child message rule scoped under a ParentEnvelope for fast-path resolution or filtering."""

    rule_id: str = Field(default="", description="Unique identifier for the rule")
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
    """Parent log envelope regex defining outer log structure and child message rules."""

    parent_id: str = Field(description="Unique identifier for the envelope template")
    envelope_regex: str = Field(description="Python regex with named capture groups (?P<name>...)")
    format_string: str = Field(
        default="",
        description="Format string using named capture groups to reconstruct the original log",
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
    """Generic key-value representation of a log stream event extracted via regex."""

    model_config = ConfigDict(extra="allow")

    raw: str = Field(default="", description="Original raw log line")
    message: str = Field(default="", description="Extracted message body or raw line if unparsed")
    format_string: str = Field(
        default="",
        description="Format string using named capture groups to reconstruct the original log",
    )
    is_unparsed: bool = Field(
        default=False,
        description="Whether log was unparsed by registered parent envelopes",
    )
    parent_id: str = Field(
        default="",
        description="Identifier of the matched ParentEnvelope, or empty if unparsed",
    )

    @property
    def fields(self) -> dict[str, Any]:
        """Return dictionary of dynamically extracted capture group fields."""
        return dict(self.model_extra or {})

    def get(self, key: str, default: Any = "") -> Any:
        """Retrieve a dynamic capture group field or core attribute by key."""
        if self.model_extra and key in self.model_extra:
            return self.model_extra[key]
        if key in {"raw", "message", "format_string", "is_unparsed", "parent_id"}:
            return getattr(self, key, default)
        return default

    def reconstruct(self) -> str:
        """Reconstruct the original raw log line using format_string and key-value fields."""
        if self.format_string:
            try:
                return self.format_string.format_map(self.model_dump())
            except Exception:
                pass
        return self.raw
