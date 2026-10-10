from src.rules.models import LogEvent
from src.rules.normalizer import parse_log_line
from src.rules.telemetry_filter import TelemetryFilter


def test_parse_standard_hpc_log():
    raw = (
        "460903 resourcemgmtdaeomon node-25 server subsys 1145552216 1 "
        "failed to configure resourcemgmt subsystem err = 10"
    )
    event = parse_log_line(raw)
    assert isinstance(event, LogEvent)
    assert event.log_id == "460903"
    assert event.subsystem in ("resourcemgmtdaeomon", "server")
    assert event.timestamp == 1145552216
    assert "err = 10" in event.message


def test_parse_edge_case_empty_node():
    raw = (
        "48124  clusterfilesystem fdmn.full 1076621893 1 "
        "ServerFileSystem: ServerFileSystem domain root_domain is full"
    )
    event = parse_log_line(raw)
    assert event.log_id == "48124"
    assert event.subsystem == "clusterfilesystem"
    assert event.event_type == "fdmn.full"
    assert event.node == ""
    assert event.timestamp == 1076621893
    assert event.flag == 1
    assert "root_domain is full" in event.message


def test_parse_len_4_standard_format():
    raw = "2560243 gige3 gige temperature 1073198499 1 normal"
    event = parse_log_line(raw)
    assert event.log_id == "2560243"
    assert event.node == "gige3"
    assert event.subsystem == "gige"
    assert event.event_type == "temperature"
    assert event.timestamp == 1073198499
    assert event.flag == 1
    assert event.message == "normal"


def test_parse_line_with_escaped_quotes_and_backslashes():
    raw = (
        r"2557285 node-233 unix.hw state_change.unavailable 1072945921 1 "
        r"Component State Change: Component \042alt0\042 is in the unavailable state (HWID=1328)"
    )
    event = parse_log_line(raw)
    assert event.log_id == "2557285"
    assert event.node == "node-233"
    assert event.subsystem == "unix.hw"
    assert event.event_type == "state_change.unavailable"
    assert "alt0" in event.message


def test_parse_malformed_graceful():
    # Empty string
    event_empty = parse_log_line("")
    assert event_empty.log_id == ""
    assert event_empty.message == ""

    # Truncated line without timestamp
    event_short = parse_log_line("123 node-1")
    assert event_short.log_id == "123"
    assert event_short.timestamp == 0

    # Non-numeric timestamp
    event_bad_ts = parse_log_line("123 node-1 subsys event not_a_ts not_a_flag msg")
    assert event_bad_ts.log_id == "123"
    assert event_bad_ts.timestamp == 0


def test_telemetry_filter_identifies_normal():
    raw = "2560243 gige3 gige temperature 1073198499 1 normal"
    event = parse_log_line(raw)
    assert TelemetryFilter.is_routine_telemetry(event) is True


def test_telemetry_filter_retains_error():
    raw = (
        "460903 resourcemgmtdaeomon node-25 server subsys 1145552216 1 "
        "failed to configure resourcemgmt subsystem err = 10"
    )
    event = parse_log_line(raw)
    assert TelemetryFilter.is_routine_telemetry(event) is False


def test_telemetry_filter_fan_speeds():
    raw = (
        "290554 Interconnect-0N00 switch_module fan 1121448124 1 "
        "Fan speeds ( 3552 3534 3375 **** 3515 3479 )"
    )
    event = parse_log_line(raw)
    assert TelemetryFilter.is_routine_telemetry(event) is True


def test_telemetry_filter_link_ok():
    raw = "2582655 Interconnect-1T00 switch_module bcast-error 1074220138 1 Link ok"
    event = parse_log_line(raw)
    assert TelemetryFilter.is_routine_telemetry(event) is True


def test_telemetry_filter_ambient():
    # Benign ambient telemetry
    benign_raw = "2559883 node-150 node temperature 1073139000 1 ambient=28"
    benign_event = parse_log_line(benign_raw)
    assert TelemetryFilter.is_routine_telemetry(benign_event) is True

    # Error containing ambient
    error_raw = r"204577 node-232 node psu 1078347510 1 psu failure\ ambient=28"
    error_event = parse_log_line(error_raw)
    assert TelemetryFilter.is_routine_telemetry(error_event) is False


def test_telemetry_filter_status_running():
    # Routine running status
    running_raw = "2313342 full partition status 1061583461 -1 running"
    event = parse_log_line(running_raw)
    assert TelemetryFilter.is_routine_telemetry(event) is True


def test_telemetry_filter_responding():
    # Routine responding
    resp_raw = "100 node-10 node status 1073104145 1 responding"
    event_resp = parse_log_line(resp_raw)
    assert TelemetryFilter.is_routine_telemetry(event_resp) is True

    # Failure: not responding
    not_resp_raw = "2558264 node-127 node status 1073104145 1 not responding"
    event_not_resp = parse_log_line(not_resp_raw)
    assert TelemetryFilter.is_routine_telemetry(event_not_resp) is False
