import json
import time
from pathlib import Path

import pytest

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


def test_persistent_rules_file_loading_and_dumping(tmp_path: Path) -> None:
    """Verify loading from JSON file, adding rules, auto-dumping, and human_validated flag."""
    rules_file = tmp_path / "rules.json"

    # 1. Initialize engine with defaults pointing to new file
    engine = RuleTreeEngine.with_defaults(rules_path=rules_file)
    assert engine.rule_count == 5
    assert rules_file.is_file()

    # Verify seed rules are marked human_validated=True and validation_status='VALIDATED'
    psu_rule = engine.get_rule("RULE-PSU-01")
    assert psu_rule is not None
    assert psu_rule.human_validated is True
    assert psu_rule.validation_status == "VALIDATED"

    # 2. Add a new rule with persist=True
    new_res = ProblemResolution(
        error_summary="Thermal trip detected",
        root_cause="CPU overheating above 95C",
        severity="CRITICAL",
        remediation_steps=["Throttle node", "Check cooling loop"],
        citations_and_reasons=["Thermal manual"],
    )
    new_rule = Rule(
        rule_id="RULE-THERMAL-01",
        subsystem="unix.hw",
        event_type="thermal",
        pattern=r"CPU thermal trip initiated",
        resolution=new_res,
        human_validated=False,
        validation_status="VALIDATED",
    )
    engine.add_rule(new_rule, persist=True)

    # 3. Verify file on disk contains the new rule
    with open(rules_file, encoding="utf-8") as f:
        data = json.load(f)
    rule_ids = [r["rule_id"] for r in data]
    assert "RULE-THERMAL-01" in rule_ids
    saved_thermal = next(r for r in data if r["rule_id"] == "RULE-THERMAL-01")
    assert saved_thermal["human_validated"] is False
    assert saved_thermal["validation_status"] == "VALIDATED"

    # 4. A fresh engine pointing to the same file loads all rules including the new one
    engine2 = RuleTreeEngine(rules_path=rules_file)
    assert engine2.rule_count == 6
    matched = engine2.match("100 node-1 unix.hw thermal 1000000000 1 CPU thermal trip initiated")
    assert matched is not None
    assert matched.error_summary == "Thermal trip detected"


def test_benign_validation_zero_false_positive_guardrail() -> None:
    """Verify validate_rule_against_benign catches and rejects rules matching benign telemetry."""
    engine = RuleTreeEngine()

    # Unsafe rule matching routine fan telemetry
    unsafe_rule = Rule(
        rule_id="RULE-UNSAFE-FAN",
        subsystem="switch_module",
        event_type="fan",
        pattern=r"Fan speeds.*",
        resolution=ProblemResolution(
            error_summary="Fan emergency",
            root_cause="Fan error",
            severity="CRITICAL",
        ),
    )
    assert engine.validate_rule_against_benign(unsafe_rule) is False

    # Safe rule matching specific hardware panic
    safe_rule = Rule(
        rule_id="RULE-SAFE-PANIC",
        subsystem="kernel",
        event_type="panic",
        pattern=r"kernel panic: uncorrectable machine check",
        resolution=ProblemResolution(
            error_summary="Kernel panic",
            root_cause="MCE crash",
            severity="CRITICAL",
        ),
    )
    assert engine.validate_rule_against_benign(safe_rule) is True


@pytest.mark.anyio
async def test_async_benign_validation_and_pending_state(tmp_path: Path) -> None:
    """Verify novel rule is immediately matchable in PENDING state and transitions to VALIDATED."""
    rules_file = tmp_path / "rules.json"
    engine = RuleTreeEngine.with_defaults(rules_path=rules_file)

    novel_res = ProblemResolution(
        error_summary="InfiniBand HCA firmware timeout",
        root_cause="HCA controller lockup",
        severity="HIGH",
        remediation_steps=["Reset HCA PCI bus"],
        citations_and_reasons=["InfiniBand guide"],
    )
    raw_event = "150000 node-101 infiniband error 1074220000 1 HCA firmware timeout on port 1"

    # Create novel rule: immediately registers with PENDING state
    rule = engine.create_rule_from_resolution(
        raw_event,
        novel_res,
        rule_id="RULE-HCA-TIMEOUT-01",
        auto_validate_async=True,
    )
    assert rule.validation_status == "PENDING"
    assert rule.human_validated is False

    # Immediate match during PENDING state: does NOT block!
    matched_pending = engine.match(raw_event)
    assert matched_pending is not None
    assert matched_pending.error_summary == "InfiniBand HCA firmware timeout"

    # Wait for background validation to finish
    is_valid = await engine.wait_for_validation(rule.rule_id)
    assert is_valid is True
    assert rule.validation_status == "VALIDATED"

    # Disk file is updated with VALIDATED status
    with open(rules_file, encoding="utf-8") as f:
        data = json.load(f)
    saved = next(r for r in data if r["rule_id"] == "RULE-HCA-TIMEOUT-01")
    assert saved["validation_status"] == "VALIDATED"
    assert saved["human_validated"] is False


@pytest.mark.anyio
async def test_async_benign_validation_rejection_deactivation(tmp_path: Path) -> None:
    """Verify rule that matches benign cases gets REJECTED and deactivated from tree matching."""
    rules_file = tmp_path / "rules.json"
    engine = RuleTreeEngine.with_defaults(rules_path=rules_file)

    bad_res = ProblemResolution(
        error_summary="False alarm on fan",
        root_cause="Bogus rule",
        severity="CRITICAL",
    )
    # A log line whose extracted template would falsely match routine fan telemetry
    raw_bad_event = (
        "10000 node-1 switch_module fan 1074220000 1 Fan speeds ( 3552 3534 3375 3500 3515 3479 )"
    )

    bad_rule = engine.create_rule_from_resolution(
        raw_bad_event,
        bad_res,
        rule_id="RULE-BAD-FAN-01",
        auto_validate_async=True,
    )
    assert bad_rule.validation_status == "PENDING"

    # Wait for async validation to execute
    is_valid = await engine.wait_for_validation(bad_rule.rule_id)
    assert is_valid is False
    assert bad_rule.validation_status == "REJECTED"

    # Rule is deactivated: matching tree must NOT match this rejected rule anymore!
    assert engine.match_rule(raw_bad_event) is None

    # Status on disk is marked as REJECTED
    with open(rules_file, encoding="utf-8") as f:
        data = json.load(f)
    saved = next(r for r in data if r["rule_id"] == "RULE-BAD-FAN-01")
    assert saved["validation_status"] == "REJECTED"


def test_create_rule_from_resolution_uses_agent_suggested_regex():
    """Verify create_rule_from_resolution uses suggested_log_template_regex from resolution."""
    engine = RuleTreeEngine()
    raw_event = "100 node-1 scsi error 1000000000 1 disk I/O error on device sda sector 48912"
    event = parse_log_line(raw_event)

    res = ProblemResolution(
        error_summary="Disk I/O error",
        root_cause="Bad sector on storage block",
        severity="HIGH",
        remediation_steps=["Remap sector", "Run fsck"],
        citations_and_reasons=["Storage runbook"],
        suggested_log_template_regex=r"disk I/O error on device \w+ sector \d+",
    )

    rule = engine.create_rule_from_resolution(event, res, rule_id="RULE-DISK-IO-01")
    assert rule.pattern == r"disk I/O error on device \w+ sector \d+"

    # Match event with a different device name (sdb) and different sector
    different_raw = "101 node-2 scsi error 1000000001 1 disk I/O error on device sdb sector 99123"
    different_event = parse_log_line(different_raw)
    matched = engine.match(different_event)
    assert matched is not None
    assert matched.error_summary == "Disk I/O error"


def test_create_rule_from_resolution_invalid_regex_fallback():
    """Verify invalid suggested_log_template_regex safely falls back to template_to_regex."""
    engine = RuleTreeEngine()
    raw_event = "100 node-1 kernel panic 1000000000 1 unexpected kernel error code 55"
    event = parse_log_line(raw_event)

    res = ProblemResolution(
        error_summary="Kernel panic",
        root_cause="Kernel exception",
        severity="CRITICAL",
        remediation_steps=["Reboot"],
        citations_and_reasons=["OS guide"],
        suggested_log_template_regex=r"[unclosed bracket syntax error",
    )

    rule = engine.create_rule_from_resolution(event, res)
    assert rule.pattern != r"[unclosed bracket syntax error"
    assert "<NUM>" in extract_template(event.message)


def test_generic_quoted_tokens_in_template_and_regex():
    """Verify domain-agnostic quoted literals (both double-quoted and octal \\042) match."""
    engine = RuleTreeEngine()
    res = ProblemResolution(
        error_summary="WWID device error",
        root_cause="SCSI bus drop",
        severity="HIGH",
        remediation_steps=["Check fabric"],
        citations_and_reasons=["Fibre channel guide"],
    )

    # First event with escaped octal quote \\042
    raw1 = (
        r"500 node-1 scsi error 1000000000 1 "
        r"WWID \04201000010000000000000000000000000\042 bus failure"
    )
    event1 = parse_log_line(raw1)
    engine.create_rule_from_resolution(event1, res)

    # Second event with a different WWID and standard double quotes
    raw2 = '501 node-2 scsi error 1000000001 1 WWID "02000020000000000000000000000000" bus failure'
    event2 = parse_log_line(raw2)
    matched2 = engine.match(event2)
    assert matched2 is not None
    assert matched2.error_summary == "WWID device error"


def test_regex_tree_parent_envelope_and_child_matching():
    """Verify ParentEnvelope parses arbitrary envelope and matches child rules without subsystem."""
    from src.rules.models import ParentEnvelope, Rule

    engine = RuleTreeEngine()

    # Register custom envelope without traditional 'subsystem' or 'event_type' fields
    custom_env = ParentEnvelope(
        parent_id="K8S_CONTAINER_ENVELOPE",
        envelope_regex=r"^\[(?P<timestamp>\d+)\]\s+<(?P<namespace>[\w-]+)>\s+(?P<pod>[\w-]+):\s+(?P<message>.*)$",
        description="Kubernetes container log",
    )
    # Add child rule under this envelope
    child_rule = Rule(
        rule_id="RULE-POD-OOM",
        parent_id="K8S_CONTAINER_ENVELOPE",
        pattern=r"container (?P<container>\w+) terminated with OOMKilled",
        rule_type="ACTIONABLE_ERROR",
        resolution=ProblemResolution(
            error_summary="Pod OOMKilled",
            root_cause="Container exceeded memory limit",
            severity="HIGH",
            remediation_steps=["Increase memory limit in pod spec"],
            citations_and_reasons=["K8s Runbook: OOM Recovery"],
        ),
    )
    custom_env.children.append(child_rule)
    engine.add_envelope(custom_env)

    raw_k8s_line = (
        "[1710000000] <hpc-jobs> slurm-worker-42: container compute terminated with OOMKilled"
    )
    event, matched = engine.match_raw(raw_k8s_line)

    assert event.is_unparsed is False
    assert event.message == "container compute terminated with OOMKilled"
    assert event.model_extra is not None
    assert event.model_extra["namespace"] == "hpc-jobs"
    assert event.model_extra["pod"] == "slurm-worker-42"

    assert matched is not None
    assert matched.rule_id == "RULE-POD-OOM"
    assert matched.resolution is not None
    assert matched.resolution.error_summary == "Pod OOMKilled"


def test_regex_tree_benign_child_matching():
    """Verify child rule with rule_type='BENIGN_TELEMETRY' is correctly matched."""
    from src.rules.models import Rule

    engine = RuleTreeEngine.with_defaults()

    benign_rule = Rule(
        rule_id="RULE-BENIGN-HEARTBEAT",
        parent_id="*",
        pattern=r"routine heartbeat ping seq=\d+",
        rule_type="BENIGN_TELEMETRY",
    )
    engine.add_rule(benign_rule)

    raw = "100 node-1 daemon status 1000000000 1 routine heartbeat ping seq=409"
    event, matched = engine.match_raw(raw)
    assert matched is not None
    assert matched.rule_type == "BENIGN_TELEMETRY"
    assert matched.resolution is None


def test_regex_tree_dynamic_parent_learning_on_unparsed():
    """Verify create_rule_from_resolution creates new ParentEnvelope when log is unparsed."""
    from src.rules.models import LogEvent

    engine = RuleTreeEngine()
    unparsed_raw = "PROPRIETARY_DEVICE [404] error_code=99 status=offline"
    event = LogEvent(raw=unparsed_raw, message=unparsed_raw, is_unparsed=True)

    resolution = ProblemResolution(
        error_summary="Proprietary device failure",
        root_cause="Device hardware offline",
        severity="HIGH",
        remediation_steps=["Reset device"],
        citations_and_reasons=["Proprietary manual"],
        suggested_log_template_regex=(
            r"^PROPRIETARY_DEVICE\s+\[(?P<device_id>\d+)\]\s+(?P<message>error_code=\d+\s+status=\w+)$"
        ),
    )

    rule = engine.create_rule_from_resolution(event, resolution)
    assert rule.parent_id.startswith("ENV-")

    # Second log line with different device ID matches via the newly created parent envelope!
    second_raw = "PROPRIETARY_DEVICE [505] error_code=99 status=offline"
    second_event, second_matched = engine.match_raw(second_raw)
    assert second_event.is_unparsed is False
    assert second_event.model_extra is not None
    assert second_event.model_extra["device_id"] == "505"
