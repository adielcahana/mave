"""Tests for the unified two-tier Regex Tree Engine (ParentEnvelope -> Child Rule)."""

import re
import time
from pathlib import Path

import pytest

from src.agent import ProblemResolution
from src.rules.engine import RuleTreeEngine, derive_format_string
from src.rules.models import LogEvent, ParentEnvelope, Rule


def test_default_regex_tree_loading_and_matching() -> None:
    """Verify RuleTreeEngine loads rules fixture and matches benign & error logs."""
    engine = RuleTreeEngine.with_defaults(rules_path="tests/fixtures/rules.json")
    assert len(engine.envelopes) >= 3
    assert engine.rule_count >= 10

    # 1. Benign fan telemetry matched under LANL_STANDARD
    fan_raw = (
        "10000 node-1 switch_module fan 1074220000 1 Fan speeds ( 3552 3534 3375 3500 3515 3479 )"
    )
    fan_event, fan_rule = engine.match(fan_raw)
    assert fan_event.is_unparsed is False
    assert fan_event.parent_id == "LANL_STANDARD"
    assert fan_event.get("node") == "node-1"
    assert fan_event.get("subsystem") == "switch_module"
    assert fan_rule is not None
    assert fan_rule.rule_type == "BENIGN_TELEMETRY"

    # 2. Actionable PSU failure matched under LANL_STANDARD
    psu_raw = "98745 Interconnect-0N00 switch_module psu 1110481498 1 PSU status ( on off )"
    psu_event, psu_rule = engine.match(psu_raw)
    assert psu_event.is_unparsed is False
    assert psu_rule is not None
    assert psu_rule.rule_type == "ACTIONABLE_ERROR"
    assert psu_rule.runbook_id == "RB-6a3c0815" or psu_rule.resolution is not None

    # 3. 6-token LANL log (missing node) matched under LANL_STANDARD
    fs_raw = (
        "48124  clusterfilesystem fdmn.full 1076621893 1 "
        "ServerFileSystem: ServerFileSystem domain root_domain is full"
    )
    fs_event, fs_rule = engine.match(fs_raw)
    assert fs_event.is_unparsed is False
    assert fs_event.get("subsystem") == "clusterfilesystem"
    assert fs_event.get("event_type") == "fdmn.full"
    assert fs_rule is not None
    assert fs_rule.rule_id == "RULE-FS-FULL-01"

    # 4. 8-token LANL extended daemon log matched under LANL_EXTENDED_DAEMON
    ext_raw = (
        "460903 resourcemgmtdaeomon node-25 server subsys 1145552216 1 "
        "failed to configure resourcemgmt subsystem err = 10"
    )
    ext_event, ext_rule = engine.match(ext_raw)
    assert ext_event.is_unparsed is False
    assert ext_event.parent_id == "LANL_EXTENDED_DAEMON"
    assert ext_event.get("subsystem") == "resourcemgmtdaeomon"
    assert ext_event.get("node") == "node-25"
    assert ext_rule is not None
    assert ext_rule.rule_id == "RULE-RESMGMT-ERR10-01"

    # 5. RFC5424 syslog matched under RFC5424_SYSLOG
    rfc_raw = "<165>1 2026-10-10T12:00:00Z hpc-head slurmctld 1234 ID47 job 998 failed"
    rfc_event, rfc_rule = engine.match(rfc_raw)
    assert rfc_event.is_unparsed is False
    assert rfc_event.parent_id == "RFC5424_SYSLOG"
    assert rfc_event.get("priority") == "165"
    assert rfc_event.get("procid") == "1234"
    assert rfc_event.message == "job 998 failed"
    assert rfc_rule is None


def test_unparsed_log_returns_raw_without_heuristics() -> None:
    """Verify unparsed log returns LogEvent with raw and is_unparsed=True."""
    engine = RuleTreeEngine.with_defaults(rules_path=None)
    unparsed_raw = "CUSTOM_LOG 2026-10-10 unexpected format syntax error code 55"
    event, matched_rule = engine.match(unparsed_raw)
    assert event.is_unparsed is True
    assert event.raw == unparsed_raw
    assert event.message == unparsed_raw
    assert event.fields == {}
    assert matched_rule is None


def test_regex_tree_custom_parent_envelope_and_child_matching() -> None:
    """Verify custom ParentEnvelope parses arbitrary key-value fields and matches child rules."""
    engine = RuleTreeEngine()

    custom_env = ParentEnvelope(
        parent_id="K8S_CONTAINER_ENVELOPE",
        envelope_regex=(
            r"^\[(?P<timestamp>\d+)\]\s+<(?P<namespace>[\w-]+)>\s+"
            r"(?P<pod>[\w-]+):\s+(?P<message>.*)$"
        ),
        description="Kubernetes container log",
    )
    child_rule = Rule(
        rule_id="RULE-POD-OOM",
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
    event, matched = engine.match(raw_k8s_line)

    assert event.is_unparsed is False
    assert event.message == "container compute terminated with OOMKilled"
    assert event.get("namespace") == "hpc-jobs"
    assert event.get("pod") == "slurm-worker-42"
    assert matched is not None
    assert matched.rule_id == "RULE-POD-OOM"


def test_persistent_rules_file_loading_and_dumping(tmp_path: Path) -> None:
    """Verify loading from JSON file, adding rules, auto-dumping, and human_validated flag."""
    import shutil

    rules_file = tmp_path / "rules.json"
    shutil.copyfile("tests/fixtures/rules.json", rules_file)

    engine = RuleTreeEngine.with_defaults(rules_path=rules_file)
    initial_count = engine.rule_count
    assert initial_count >= 10
    assert rules_file.is_file()

    psu_rule = engine.get_rule("RULE-PSU-01")
    assert psu_rule is not None
    assert psu_rule.human_validated is True
    assert psu_rule.validation_status == "VALIDATED"

    new_res = ProblemResolution(
        error_summary="Thermal trip detected",
        root_cause="CPU overheating above 95C",
        severity="CRITICAL",
        remediation_steps=["Throttle node", "Check cooling loop"],
        citations_and_reasons=["Thermal manual"],
    )
    new_rule = Rule(
        rule_id="RULE-THERMAL-01",
        pattern=r"CPU thermal trip initiated",
        resolution=new_res,
        human_validated=False,
        validation_status="VALIDATED",
    )
    engine.add_rule("LANL_STANDARD", new_rule, persist=True)

    engine2 = RuleTreeEngine(rules_path=rules_file)
    assert engine2.rule_count == initial_count + 1
    _, matched = engine2.match("100 node-1 unix.hw thermal 1000000000 1 CPU thermal trip initiated")
    assert matched is not None
    assert matched.resolution is not None
    assert matched.resolution.error_summary == "Thermal trip detected"


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
        suggested_log_template_regex=r"HCA firmware timeout on port \d+",
    )
    raw_event = "150000 node-101 infiniband error 1074220000 1 HCA firmware timeout on port 1"

    rule = engine.create_rule_from_resolution(
        raw_event,
        novel_res,
        rule_id="RULE-HCA-TIMEOUT-01",
        auto_validate_async=True,
    )
    assert rule.validation_status == "PENDING"
    assert rule.human_validated is False

    # Immediate match during PENDING state
    _, matched_pending = engine.match(raw_event)
    assert matched_pending is not None
    assert matched_pending.rule_id == "RULE-HCA-TIMEOUT-01"

    is_valid = await engine.wait_for_validation(rule.rule_id)
    assert is_valid is True
    assert rule.validation_status == "VALIDATED"
    assert rule.rule_id not in engine._validation_tasks


@pytest.mark.anyio
async def test_async_benign_validation_rejection_deactivation(tmp_path: Path) -> None:
    """Verify rule that matches benign cases gets REJECTED and skipped during matching."""
    rules_file = tmp_path / "rules.json"
    engine = RuleTreeEngine.with_defaults(rules_path=rules_file)

    bad_res = ProblemResolution(
        error_summary="False alarm on fan",
        root_cause="Bogus rule",
        severity="CRITICAL",
        suggested_log_template_regex=r"Fan speeds.*",
    )
    raw_bad_event = (
        "10000 node-1 switch_module fan 1074220000 1 Fan speeds ( 3552 3534 3375 3500 3515 3479 )"
    )

    bad_rule = engine.create_rule_from_resolution(
        raw_bad_event,
        bad_res,
        rule_id="RULE-BAD-FAN-01",
        auto_validate_async=True,
    )
    is_valid = await engine.wait_for_validation(bad_rule.rule_id)
    assert is_valid is False
    assert bad_rule.validation_status == "REJECTED"


def test_regex_tree_dynamic_parent_learning_on_unparsed() -> None:
    """Verify create_rule_from_resolution creates ParentEnvelope and child rule when unparsed."""
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
            r"^PROPRIETARY_DEVICE\s+\[(?P<device_id>\d+)\]\s+"
            r"(?P<message>error_code=\d+\s+status=\w+)$"
        ),
    )

    rule = engine.create_rule_from_resolution(event, resolution)
    assert rule.rule_id.startswith("RULE-AUTO-")

    second_raw = "PROPRIETARY_DEVICE [505] error_code=99 status=offline"
    second_event, second_matched = engine.match(second_raw)
    assert second_event.is_unparsed is False
    assert second_event.get("device_id") == "505"
    assert second_matched is not None
    assert second_matched.resolution is not None
    assert second_matched.resolution.error_summary == "Proprietary device failure"


def test_format_string_derivation_and_log_event_reconstruct() -> None:
    """Verify derive_format_string, LogEvent.reconstruct, and known group tracking."""
    pattern = re.compile(
        r"^\[(?P<zone>\S+)\]\s+(?P<svc>\S+):\s+(?P<err_code>\d+)\s+-(?P<details>.*)$"
    )
    raw = "[us-west-2] auth_proxy: 504 -Gateway Timeout on upstream"
    derived_fmt = derive_format_string(pattern, raw)
    assert derived_fmt == "[{zone}] {svc}: {err_code} -{details}"

    engine = RuleTreeEngine()
    engine.register_template(
        "CUSTOM_PROXY",
        pattern.pattern,
        sample_raw_log=raw,
        persist=False,
    )
    parsed_event, _ = engine.match(raw)
    assert parsed_event.is_unparsed is False
    assert parsed_event.reconstruct() == raw

    info = engine.get_known_groups_info()
    assert info["zone"]["example"] == "us-west-2"
    assert info["svc"]["example"] == "auth_proxy"


def test_sub_millisecond_matching_speed() -> None:
    """Verify 1,000 Regex Tree matches complete well under 0.5s (<0.5ms per match)."""
    engine = RuleTreeEngine.with_defaults(rules_path="tests/fixtures/rules.json")
    sample_raw = (
        "460903 resourcemgmtdaeomon node-25 server subsys 1145552216 1 "
        "failed to configure resourcemgmt subsystem err = 10"
    )

    start = time.perf_counter()
    for _ in range(1000):
        _, matched = engine.match(sample_raw)
        assert matched is not None
    elapsed = time.perf_counter() - start
    assert elapsed < 0.5, f"1,000 matches took {elapsed:.4f}s (> 0.5s limit)"
