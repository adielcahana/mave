import time

from src.agent import ProblemResolution
from src.rules.engine import RuleTreeEngine, extract_template
from src.rules.models import Rule
from src.rules.normalizer import parse_log_line


def test_rule_tree_match_preconfigured_rule():
    engine = RuleTreeEngine()
    resolution = ProblemResolution(
        error_summary="PSU failure",
        root_cause="Power supply unit degraded",
        severity="HIGH",
        remediation_steps=["Replace PSU module"],
        citations_and_reasons=["Hardware manual section 4.2"],
    )
    rule = Rule(
        rule_id="RULE-PSU-01",
        subsystem="switch_module",
        event_type="psu",
        pattern=r"PSU status \( (?:off on|on off) \)",
        resolution=resolution,
    )
    engine.add_rule(rule)

    event = parse_log_line(
        "98745 Interconnect-0N00 switch_module psu 1110481498 1 PSU status ( off on )"
    )
    matched = engine.match(event)
    assert matched is not None
    assert matched.severity == "HIGH"
    assert matched.error_summary == "PSU failure"


def test_rule_tree_dynamic_cache_resolution():
    engine = RuleTreeEngine()
    raw = (
        "460903 resourcemgmtdaeomon node-25 server subsys 1145552216 1 "
        "failed to configure resourcemgmt subsystem err = 10"
    )
    event = parse_log_line(raw)
    assert engine.match(event) is None

    res = ProblemResolution(
        error_summary="Resource daemon init failed",
        root_cause="Daemon unable to configure subsystem",
        severity="CRITICAL",
        remediation_steps=["Restart daemon", "Check node controller"],
        citations_and_reasons=["Resourcemgmt docs"],
    )
    engine.cache_resolution(event, res)

    # Next occurrence hits the cache
    matched = engine.match(event)
    assert matched is not None
    assert matched.error_summary == "Resource daemon init failed"


def test_template_parameterization_recurring_errors():
    engine = RuleTreeEngine()
    raw1 = (
        "460903 resourcemgmtdaeomon node-25 server subsys 1145552216 1 "
        "failed to configure resourcemgmt subsystem err = 10"
    )
    event1 = parse_log_line(raw1)
    res = ProblemResolution(
        error_summary="Resource daemon init failed",
        root_cause="Daemon unable to configure subsystem",
        severity="CRITICAL",
        remediation_steps=["Restart daemon", "Check node controller"],
        citations_and_reasons=["Resourcemgmt docs"],
    )
    engine.cache_resolution(event1, res)

    # Different node and different error code in recurring burst
    raw2 = (
        "460950 resourcemgmtdaeomon node-99 server subsys 1145552299 1 "
        "failed to configure resourcemgmt subsystem err = 42"
    )
    event2 = parse_log_line(raw2)
    matched = engine.match(event2)
    assert matched is not None
    assert matched.error_summary == "Resource daemon init failed"

    # Filesystem domain parameterized matching
    fs_raw1 = (
        "2557586 node-D7 clusterfilesystem clusterfilesystem.no_server 1072983906 1 "
        "ClusterFileSystem: There is no server for ServerFileSystem domain storage1034"
    )
    fs_event1 = parse_log_line(fs_raw1)
    fs_res = ProblemResolution(
        error_summary="No server for storage domain",
        root_cause="Storage server unavailable",
        severity="HIGH",
        remediation_steps=["Check storage controller"],
        citations_and_reasons=["ClusterFileSystem guide"],
    )
    engine.cache_resolution(fs_event1, fs_res)

    fs_raw2 = (
        "2557582 node-D7 clusterfilesystem clusterfilesystem.no_server 1072983890 1 "
        "ClusterFileSystem: There is no server for ServerFileSystem domain storage1033"
    )
    fs_event2 = parse_log_line(fs_raw2)
    matched_fs = engine.match(fs_event2)
    assert matched_fs is not None
    assert matched_fs.error_summary == "No server for storage domain"


def test_wildcard_event_type_matching():
    engine = RuleTreeEngine()
    rule = Rule(
        rule_id="RULE-NET-01",
        subsystem="unix.hw",
        event_type="*",
        pattern=r"Component .*alt0.* is in the unavailable state",
        resolution=ProblemResolution(
            error_summary="alt0 interface unavailable",
            root_cause="NIC failure",
            severity="HIGH",
            remediation_steps=["Restart interface"],
            citations_and_reasons=["Unix.hw guide"],
        ),
    )
    engine.add_rule(rule)

    raw = (
        r"2557285 node-233 unix.hw state_change.unavailable 1072945921 1 "
        r"Component State Change: Component \042alt0\042 is in the unavailable state (HWID=1328)"
    )
    event = parse_log_line(raw)
    matched = engine.match(event)
    assert matched is not None
    assert matched.error_summary == "alt0 interface unavailable"


def test_subsystem_wildcard_rule_matching():
    engine = RuleTreeEngine()
    rule = Rule(
        rule_id="RULE-GLOBAL-PANIC",
        subsystem="*",
        event_type="*",
        pattern=r"kernel panic: fatal exception",
        resolution=ProblemResolution(
            error_summary="Kernel panic",
            root_cause="Kernel crash",
            severity="CRITICAL",
            remediation_steps=["Reboot compute node"],
            citations_and_reasons=["OS Manual"],
        ),
    )
    engine.add_rule(rule)

    event = parse_log_line("1000 node-1 kernel sys 1000000000 1 kernel panic: fatal exception")
    matched = engine.match(event)
    assert matched is not None
    assert matched.severity == "CRITICAL"


def test_default_rules_loading():
    engine = RuleTreeEngine.with_defaults()
    assert engine.rule_count > 0

    # PSU failure match via preloaded rules
    psu_event = parse_log_line(
        "98745 Interconnect-0N00 switch_module psu 1110481498 1 PSU status ( on off )"
    )
    matched_psu = engine.match(psu_event)
    assert matched_psu is not None
    assert matched_psu.severity == "HIGH"

    # Filesystem panic match via preloaded rules
    fs_raw = (
        "48124 node-1 clusterfilesystem fdmn.panic 1076621893 1 "
        "ServerFileSystem: ServerFileSystem domain data_domain is panicked"
    )
    fs_event = parse_log_line(fs_raw)
    matched_fs = engine.match(fs_event)
    assert matched_fs is not None
    assert matched_fs.severity == "CRITICAL"


def test_cache_with_raw_string():
    engine = RuleTreeEngine()
    raw_line = "98745 Interconnect-0N00 switch_module psu 1110481498 1 PSU status ( off on )"
    res = ProblemResolution(
        error_summary="PSU error",
        root_cause="PSU off",
        severity="HIGH",
        remediation_steps=["Swap PSU"],
        citations_and_reasons=["Hardware manual"],
    )
    engine.cache_resolution(raw_line, res)

    matched = engine.match(raw_line)
    assert matched is not None
    assert matched.error_summary == "PSU error"


def test_extract_template_normalization():
    tmpl1 = extract_template(
        "failed to configure resourcemgmt subsystem err = 10 on node-25 at 0xdeadbeef"
    )
    tmpl2 = extract_template(
        "failed to configure resourcemgmt subsystem err = 42 on node-99 at 0x1234"
    )
    assert tmpl1 == tmpl2
    assert "<NUM>" in tmpl1
    assert "<NODE>" in tmpl1
    assert "<HEX>" in tmpl1


def test_sub_millisecond_matching_speed():
    engine = RuleTreeEngine.with_defaults()
    res = ProblemResolution(
        error_summary="Test error",
        root_cause="Test cause",
        severity="MEDIUM",
        remediation_steps=["Do nothing"],
        citations_and_reasons=["Test guide"],
    )
    sample_raw = (
        "460903 resourcemgmtdaeomon node-25 server subsys 1145552216 1 "
        "failed to configure resourcemgmt subsystem err = 10"
    )
    engine.cache_resolution(sample_raw, res)

    event = parse_log_line(sample_raw)

    # Execute 1,000 matches
    start = time.perf_counter()
    for _ in range(1000):
        matched = engine.match(event)
        assert matched is not None
    elapsed = time.perf_counter() - start

    # Elapsed time for 1,000 matches must be well below 500ms (0.5ms per match)
    assert elapsed < 0.5, f"1,000 matches took {elapsed:.4f}s (> 0.5s limit)"


def test_fallback_cache_scoped_to_empty_subsystem():
    """Verify that message-only fallback caches are only queried when subsystem is empty."""
    engine = RuleTreeEngine()
    res = ProblemResolution(
        error_summary="Subsystem A specific error",
        root_cause="Subsystem A failure",
        severity="HIGH",
        remediation_steps=["Fix sub_a"],
        citations_and_reasons=["Sub_a runbook"],
    )

    raw_sub_a = "100 node-1 sub_a event_type 1000000000 1 generic failure code 42"
    engine.cache_resolution(raw_sub_a, res)

    # Event with different subsystem must NOT match via message fallback
    raw_sub_b = "101 node-2 sub_b event_type 1000000001 1 generic failure code 42"
    matched_b = engine.match(raw_sub_b)
    assert matched_b is None

    # Event with empty subsystem SHOULD match via message fallback
    raw_no_sub = "102 node-3 - event_type 1000000002 1 generic failure code 42"
    event_no_sub = parse_log_line(raw_no_sub)
    event_no_sub.subsystem = ""
    matched_no_sub = engine.match(event_no_sub)
    assert matched_no_sub is not None
    assert matched_no_sub.error_summary == "Subsystem A specific error"


def test_lru_cache_capacity_bound_and_eviction():
    """Verify LRU cache respects max_cache_size and evicts least recently used entries."""
    engine = RuleTreeEngine(max_cache_size=3)

    for i in range(4):
        raw = f"100 node-1 sub ev 1000000000 1 error message variant {chr(65 + i)}"
        res = ProblemResolution(
            error_summary=f"Error {i}",
            root_cause=f"Cause {i}",
            severity="LOW",
            remediation_steps=[f"Fix {i}"],
            citations_and_reasons=[f"Doc {i}"],
        )
        engine.cache_resolution(raw, res)

    # Max cache capacity should cap at 3
    assert engine.cache_size == 3
    assert engine.template_cache_size == 3

    # The oldest entry (message 0 / variant A) should have been evicted
    raw_0 = "100 node-1 sub ev 1000000000 1 error message variant A"
    assert engine.match(raw_0) is None

    # The newest entry (message 3 / variant D) should still be in cache
    raw_3 = "100 node-1 sub ev 1000000000 1 error message variant D"
    matched_3 = engine.match(raw_3)
    assert matched_3 is not None
    assert matched_3.error_summary == "Error 3"
