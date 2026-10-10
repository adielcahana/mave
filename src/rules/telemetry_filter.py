"""Wire-speed routine telemetry filter for HPC cluster log streams."""

import re

from src.rules.models import LogEvent


class TelemetryFilter:
    """Wire-speed filter for identifying routine, benign HPC telemetry."""

    # Explicit error indicators that must NEVER be treated as routine telemetry
    _ERROR_INDICATORS: tuple[str, ...] = (
        "fail",
        "err",
        "panic",
        "critical",
        "warning",
        "unavailable",
        "not responding",
        "not-responding",
        "corrupt",
        "fatal",
        "down",
        "timeout",
        "exception",
        "bad",
        "full",
    )

    _ROUTINE_EXACT_MESSAGES: frozenset[str] = frozenset(
        {
            "normal",
            "link ok",
            "running",
            "responding",
        }
    )

    _ROUTINE_PREFIXES: tuple[str, ...] = (
        "fan speeds",
        "ambient=",
        "ambient =",
        "link ok",
        "status running",
    )

    @classmethod
    def is_routine_telemetry(cls, event: LogEvent) -> bool:
        """Return True if event represents benign routine telemetry, False otherwise."""
        msg = event.message.strip().lower()
        if not msg:
            return False

        # If any failure or error indicator is present, retain for actionable handling
        for indicator in cls._ERROR_INDICATORS:
            if indicator in msg:
                return False

        # Exact match for common heartbeat tokens
        if msg in cls._ROUTINE_EXACT_MESSAGES:
            return True

        # Prefix checks (e.g. 'fan speeds ( ... )', 'ambient=28')
        for prefix in cls._ROUTINE_PREFIXES:
            if msg.startswith(prefix):
                return True

        # Regex check for ambient pattern: ambient=\d+
        if re.search(r"\bambient\s*=\s*\d+", msg):
            return True

        # Status running check
        if event.event_type.lower() == "status" and msg == "running":
            return True

        return False
