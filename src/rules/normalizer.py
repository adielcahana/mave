"""Wire-speed log normalizer for HPC log streams."""

import re

from src.rules.models import LogEvent


def parse_log_line(raw: str) -> LogEvent:
    """Parse a raw HPC log line into a strongly-typed LogEvent.

    Gracefully handles variable headers (standard 4-token, empty node 3-token,
    extended daemon 5-token), missing fields, extra whitespace, and escaped characters.
    """
    stripped = raw.strip()
    if not stripped:
        return LogEvent(raw=raw)

    parts = stripped.split()
    total_parts = len(parts)

    if total_parts == 1:
        return LogEvent(log_id=parts[0], raw=raw)
    if total_parts == 2:
        return LogEvent(log_id=parts[0], node=parts[1], raw=raw)

    # Locate timestamp and flag by finding adjacent numeric tokens
    ts_idx = -1
    for i in range(2, min(total_parts - 1, 7)):
        try:
            float(parts[i])
            int(parts[i + 1])
            ts_idx = i
            break
        except ValueError:
            continue

    # Fallback search across remaining tokens if not found in primary window
    if ts_idx == -1:
        for i in range(1, total_parts - 1):
            try:
                float(parts[i])
                int(parts[i + 1])
                ts_idx = i
                break
            except ValueError:
                continue

    if ts_idx == -1:
        # Fallback when no valid timestamp + flag pair is identified
        header_len = min(4, total_parts)
        header = parts[:header_len]
        message = " ".join(parts[header_len:])
        return LogEvent(
            log_id=header[0] if header else "",
            node=header[1] if len(header) > 1 else "",
            subsystem=header[2] if len(header) > 2 else "",
            event_type=header[3] if len(header) > 3 else "",
            timestamp=0,
            flag=0,
            message=message,
            raw=raw,
        )

    ts_str = parts[ts_idx]
    flag_str = parts[ts_idx + 1]
    try:
        timestamp = int(float(ts_str))
    except ValueError:
        timestamp = 0
    try:
        flag = int(flag_str)
    except ValueError:
        flag = 0

    # Extract message after timestamp and flag, preserving internal spacing
    match = re.search(
        r"\b" + re.escape(ts_str) + r"\s+" + re.escape(flag_str) + r"(?:\s+(.*))?$",
        raw,
        flags=re.DOTALL,
    )
    if match and match.group(1) is not None:
        message = match.group(1).strip()
    else:
        message = " ".join(parts[ts_idx + 2 :])

    header = parts[:ts_idx]
    log_id = header[0] if header else ""
    node = ""
    subsystem = ""
    event_type = ""

    header_len = len(header)
    if header_len == 4:
        # Standard format: <log_id> <node> <subsystem> <event_type>
        node = header[1]
        subsystem = header[2]
        event_type = header[3]
    elif header_len == 3:
        # Missing node format: <log_id> <subsystem> <event_type>
        node = ""
        subsystem = header[1]
        event_type = header[2]
    elif header_len == 5:
        # Extended format (e.g., resourcemgmtdaeomon node-25 server subsys)
        if any(header[2].startswith(prefix) for prefix in ("node-", "Interconnect", "gige")):
            node = header[2]
            subsystem = header[1]
            event_type = header[4]
        elif any(header[1].startswith(prefix) for prefix in ("node-", "Interconnect", "gige")):
            node = header[1]
            subsystem = header[2]
            event_type = header[4]
        else:
            node = header[2]
            subsystem = header[1]
            event_type = header[4]
    elif header_len == 2:
        subsystem = header[1]
    elif header_len > 5:
        node = header[1]
        subsystem = header[2]
        event_type = ".".join(header[3:])

    return LogEvent(
        log_id=log_id,
        node=node,
        subsystem=subsystem,
        event_type=event_type,
        timestamp=timestamp,
        flag=flag,
        message=message,
        raw=raw,
    )
