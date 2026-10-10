"""Data models for HPC log events and rules."""

from typing import Any

from pydantic import BaseModel, Field


class LogEvent(BaseModel):
    """Normalized structured representation of an HPC log stream event."""

    log_id: str = Field(default="", description="Unique identifier of the log entry")
    node: str = Field(default="", description="Cluster node identifier (e.g., node-25)")
    subsystem: str = Field(default="", description="Subsystem or daemon reporting the event")
    event_type: str = Field(default="", description="Specific event category or type")
    timestamp: int = Field(default=0, description="Epoch timestamp of the event")
    flag: int = Field(default=0, description="Log flag or status code")
    message: str = Field(default="", description="Normalized message body")
    raw: str = Field(default="", description="Original unparsed log line")

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
        **data: Any,
    ) -> None:
        if log_id or node or subsystem or event_type or timestamp or flag or message or raw:
            data.setdefault("log_id", log_id)
            data.setdefault("node", node)
            data.setdefault("subsystem", subsystem)
            data.setdefault("event_type", event_type)
            data.setdefault("timestamp", timestamp)
            data.setdefault("flag", flag)
            data.setdefault("message", message)
            data.setdefault("raw", raw)
        super().__init__(**data)
