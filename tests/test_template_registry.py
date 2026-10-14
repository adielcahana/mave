"""Tests for template_registry, validate_suggested_regex, and dynamic retry loop."""

import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from pydantic_ai.models.test import TestModel

from src.agent import (
    _build_incident_prompt,
    analyze_log,
    create_agent,
    reset_circuit_breaker,
    validate_suggested_regex,
)
from src.config import Settings
from src.pipeline import LogPipeline
from src.rules.models import LogEvent, ProblemResolutionWithRegex
from src.rules.template_registry import LogTemplateRegistry, derive_format_string


def test_log_templates_json_exists_and_valid() -> None:
    """Verify resources/log_templates.json exists and contains initial templates."""
    path = Path("resources/log_templates.json")
    assert path.is_file()
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    assert isinstance(data, list)
    template_ids = {t["template_id"] for t in data}
    assert "LANL_7_TOKEN_STANDARD" in template_ids
    assert "LANL_6_TOKEN_NO_NODE" in template_ids
    assert "LANL_7_TOKEN_INTERCONNECT" in template_ids
    assert "RFC5424_SYSLOG" in template_ids


def test_template_registry_single_pass_and_fallback(tmp_path: Path) -> None:
    """Verify single-pass combined regex disjunction and unparsed fallback."""
    templates = [
        {
            "template_id": "LANL_7_TOKEN_STANDARD",
            "regex": (
                r"^(?P<log_id>\S+)\s+(?P<node>node-\S+|\S+)\s+(?P<subsystem>\S+)\s+"
                r"(?P<event_type>\S+)\s+(?P<timestamp>\d+)\s+(?P<flag>\d+)\s+(?P<message>.*)$"
            ),
        },
        {
            "template_id": "RFC5424_SYSLOG",
            "regex": (
                r"^<(?P<priority>\d+)>(?P<version>\d+)\s+(?P<timestamp>\S+)\s+(?P<node>\S+)\s+"
                r"(?P<subsystem>\S+)\s+(?P<procid>\S+)\s+(?P<msgid>\S+)\s+(?P<message>.*)$"
            ),
        },
    ]
    tmpl_file = tmp_path / "templates.json"
    with open(tmpl_file, "w", encoding="utf-8") as f:
        json.dump(templates, f)

    registry = LogTemplateRegistry(templates_path=tmpl_file)

    # 1. Matching LANL standard
    lanl_line = "12345 node-12 switch_module psu 1110481498 1 PSU status ( off on )"
    event_lanl = registry.parse(lanl_line)
    assert event_lanl.is_unparsed is False
    assert event_lanl.log_id == "12345"
    assert event_lanl.node == "node-12"
    assert event_lanl.subsystem == "switch_module"
    assert event_lanl.event_type == "psu"
    assert event_lanl.timestamp == 1110481498
    assert event_lanl.flag == 1
    assert event_lanl.message == "PSU status ( off on )"

    # 2. Matching RFC5424
    rfc_line = "<165>1 2026-10-10T12:00:00Z hpc-head slurmctld 1234 ID47 job 998 failed"
    event_rfc = registry.parse(rfc_line)
    assert event_rfc.is_unparsed is False
    assert event_rfc.node == "hpc-head"
    assert event_rfc.subsystem == "slurmctld"
    assert event_rfc.message == "job 998 failed"
    # Extra capture groups preserved via Pydantic extra='allow'
    assert event_rfc.model_extra is not None
    assert event_rfc.model_extra["priority"] == "165"
    assert event_rfc.model_extra["procid"] == "1234"
    assert event_rfc.model_extra["msgid"] == "ID47"

    # 3. Unparsed line falls back to normalizer and sets is_unparsed=True
    unparsed_line = "CUSTOM_LOG 2026-10-10 unexpected format syntax error code 55"
    event_unparsed = registry.parse(unparsed_line)
    assert event_unparsed.is_unparsed is True
    assert event_unparsed.raw == unparsed_line


def test_template_registry_dynamic_registration(tmp_path: Path) -> None:
    """Verify immediate availability and persistence when registering a template."""
    tmpl_file = tmp_path / "dynamic_templates.json"
    registry = LogTemplateRegistry(templates_path=tmpl_file)

    unparsed_line = "CUSTOM_SYSTEM ALERT: disk controller corrupted on sda"
    assert registry.parse(unparsed_line).is_unparsed is True

    # Register dynamic template
    new_regex = r"^(?P<subsystem>CUSTOM_SYSTEM)\s+ALERT:\s+(?P<message>.*)$"
    registry.register_template("DYNAMIC_CUSTOM", new_regex, persist=True)

    # Immediately parses without fallback
    parsed = registry.parse(unparsed_line)
    assert parsed.is_unparsed is False
    assert parsed.subsystem == "CUSTOM_SYSTEM"
    assert parsed.message == "disk controller corrupted on sda"

    # Verify persisted to disk
    assert tmpl_file.is_file()
    with open(tmpl_file, encoding="utf-8") as f:
        persisted_data = json.load(f)
    assert len(persisted_data) == 1
    assert persisted_data[0]["template_id"] == "DYNAMIC_CUSTOM"


def test_validate_suggested_regex() -> None:
    """Verify validate_suggested_regex validates syntax, matching, groups, and format."""
    sample_log = "2026-10-10 [storage_engine] Disk I/O timeout on volume /data"

    # 1. Invalid regex syntax
    valid, err = validate_suggested_regex(r"[unclosed bracket", sample_log)
    assert valid is False
    assert "Invalid regex syntax" in err

    # 2. Non-matching regex
    valid, err = validate_suggested_regex(r"^node-\d+\s+.*$", sample_log)
    assert valid is False
    assert "failed to match the incoming raw log line" in err

    # 3. No named capture groups at all
    valid, err = validate_suggested_regex(r"^\S+\s+\[\S+\]\s+.*$", sample_log)
    assert valid is False
    assert "contains no named capture groups" in err

    # 4. Arbitrary named groups (no required subsystem or message)
    valid, err = validate_suggested_regex(
        r"^(?P<time>\S+)\s+\[(?P<tag>[^\]]+)\]\s+(?P<payload>.*)$",
        sample_log,
    )
    assert valid is True
    assert err == "OK"

    # 5. Format string with missing reference
    valid, err = validate_suggested_regex(
        r"^(?P<time>\S+)\s+\[(?P<tag>[^\]]+)\]\s+(?P<payload>.*)$",
        sample_log,
        format_string="{time} [{missing_key}] {payload}",
    )
    assert valid is False
    assert "references group" in err

    # 6. Format string valid
    valid, err = validate_suggested_regex(
        r"^(?P<time>\S+)\s+\[(?P<tag>[^\]]+)\]\s+(?P<payload>.*)$",
        sample_log,
        format_string="{time} [{tag}] {payload}",
    )
    assert valid is True
    assert err == "OK"


@pytest.mark.anyio
async def test_analyze_log_with_unparsed_log_and_retry_feedback() -> None:
    """Verify analyze_log uses ProblemResolutionWithRegex and retries if regex invalid."""
    reset_circuit_breaker()
    sample_raw = "MY_SUBSYS: error code 42 fatal fault"
    unparsed_event = LogEvent(
        raw=sample_raw,
        message=sample_raw,
        is_unparsed=True,
    )

    test_settings = Settings(
        base_url="https://test.nvidia.com/v1",
        api_key="test-key",
        model_name="openai/gpt-oss-20b",
    )

    # Agent first returns invalid regex (no named capture groups), then corrected regex
    attempt = 0

    def mock_model_output(*args: Any, **kwargs: Any) -> ProblemResolutionWithRegex:
        nonlocal attempt
        attempt += 1
        if attempt == 1:
            # Bad regex: no named capture groups
            return ProblemResolutionWithRegex(
                error_summary="Fatal error in custom subsys",
                root_cause="Hardware error code 42",
                severity="CRITICAL",
                remediation_steps=["Reboot subsystem"],
                citations_and_reasons=["Custom Runbook"],
                suggested_log_template_regex=r"^MY_SUBSYS:\s+error.*$",
            )
        # Correct regex
        return ProblemResolutionWithRegex(
            error_summary="Fatal error in custom subsys",
            root_cause="Hardware error code 42",
            severity="CRITICAL",
            remediation_steps=["Reboot subsystem"],
            citations_and_reasons=["Custom Runbook"],
            suggested_log_template_regex=r"^(?P<service>MY_SUBSYS):\s+(?P<details>.*)$",
            format_string="{service}: {details}",
        )

    # Use a TestModel returning outputs
    # Because TestModel accepts custom_output_args, let's test using a subclass or mock
    with patch("pydantic_ai.Agent.run") as mock_run:

        class MockRunResult:
            def __init__(self, output: Any) -> None:
                self.output = output

            def all_messages(self) -> list[Any]:
                return []

        mock_run.side_effect = [
            MockRunResult(mock_model_output()),
            MockRunResult(mock_model_output()),
        ]

        try:
            agent = create_agent(settings=test_settings)
            resolution = await analyze_log(unparsed_event, agent=agent, settings=test_settings)

            assert isinstance(resolution, ProblemResolutionWithRegex)
            assert (
                resolution.suggested_log_template_regex
                == r"^(?P<service>MY_SUBSYS):\s+(?P<details>.*)$"
            )
            assert resolution.format_string == "{service}: {details}"
            assert mock_run.call_count == 2  # 1 initial + 1 retry
        finally:
            reset_circuit_breaker()


@pytest.mark.anyio
async def test_pipeline_dynamic_template_registration_on_unparsed_log(tmp_path: Path) -> None:
    """Verify pipeline registers dynamic regex from ProblemResolutionWithRegex for unparsed logs."""
    reset_circuit_breaker()
    tmpl_file = tmp_path / "pipe_templates.json"
    with open(tmpl_file, "w", encoding="utf-8") as f:
        json.dump([], f)

    registry = LogTemplateRegistry(templates_path=tmpl_file)
    test_settings = Settings(
        base_url="https://test.nvidia.com/v1",
        api_key="test-key",
        model_name="openai/gpt-oss-20b",
        rules_path=str(tmp_path / "rules.json"),
    )

    unparsed_line = "STORAGE_CONTROLLER RAID failure on controller 0"

    mock_res = ProblemResolutionWithRegex(
        error_summary="RAID failure",
        root_cause="Drive controller failure",
        severity="HIGH",
        remediation_steps=["Replace drive"],
        citations_and_reasons=["Storage Docs"],
        suggested_log_template_regex=r"^(?P<subsystem>STORAGE_CONTROLLER)\s+(?P<message>.*)$",
    )

    test_model = TestModel(custom_output_args=mock_res.model_dump())
    agent = create_agent(settings=test_settings, model=test_model)

    pipeline = LogPipeline(
        agent=agent,
        settings=test_settings,
        template_registry=registry,
    )

    # Initially, line is unparsed
    res1 = await pipeline.process_log(unparsed_line)
    assert res1.status == "AGENT_RESOLVED"

    # Dynamic registration happened in pipeline
    # Now check if registry immediately parses a subsequent identical/similar log line
    similar_line = "STORAGE_CONTROLLER RAID degraded on controller 1"
    event2 = registry.parse(similar_line)
    assert event2.is_unparsed is False
    assert event2.subsystem == "STORAGE_CONTROLLER"
    assert event2.message == "RAID degraded on controller 1"


def test_log_event_allows_extra_dynamic_fields(tmp_path: Path) -> None:
    """Verify LogEvent accepts arbitrary extra fields and pipeline/agent preserves them."""
    # 1. Direct instantiation
    event = LogEvent(
        log_id="101",
        node="gpu-node-04",
        subsystem="slurm",
        event_type="job_fail",
        job_id="99812",
        cluster="capitol",
        exit_code=137,
    )
    assert event.model_extra is not None
    assert event.model_extra["job_id"] == "99812"
    assert event.model_extra["cluster"] == "capitol"
    assert event.model_extra["exit_code"] == 137
    dumped = event.model_dump()
    assert dumped["job_id"] == "99812"
    assert dumped["exit_code"] == 137

    # 2. Extra fields in incident prompt
    prompt = _build_incident_prompt(event)
    assert "- job_id: 99812" in prompt
    assert "- cluster: capitol" in prompt
    assert "- exit_code: 137" in prompt

    # 3. Dynamic template extraction into LogEvent extra fields
    tmpl_file = tmp_path / "custom_tmpl.json"
    registry = LogTemplateRegistry(templates_path=tmpl_file)
    registry.register_template(
        "K8S_CONTAINER",
        r"^\[(?P<cluster>[^\]]+)\]\s+(?P<subsystem>\S+)\s+pod/(?P<pod_name>\S+)\s+PID:(?P<pid>\d+)\s+(?P<message>.*)$",
    )
    raw = "[us-east-1] k8s_runtime pod/nginx-789 PID:4123 connection reset by peer"
    parsed_event = registry.parse(raw)
    assert parsed_event.is_unparsed is False
    assert parsed_event.subsystem == "k8s_runtime"
    assert parsed_event.message == "connection reset by peer"
    assert parsed_event.model_extra is not None
    assert parsed_event.model_extra["cluster"] == "us-east-1"
    assert parsed_event.model_extra["pod_name"] == "nginx-789"
    assert parsed_event.model_extra["pid"] == "4123"
    # Format string restoration
    assert parsed_event.format_string != ""
    assert parsed_event.reconstruct() == raw


def test_format_string_restoration_and_group_names(tmp_path: Path) -> None:
    """Verify derive_format_string, get_known_group_names, and LogEvent.reconstruct."""
    import re

    pattern = re.compile(
        r"^\[(?P<zone>\S+)\]\s+(?P<svc>\S+):\s+(?P<err_code>\d+)\s+-(?P<details>.*)$"
    )
    raw = "[us-west-2] auth_proxy: 504 -Gateway Timeout on upstream"
    derived_fmt = derive_format_string(pattern, raw)
    assert derived_fmt == "[{zone}] {svc}: {err_code} -{details}"

    # LogEvent reconstruction
    event = LogEvent(
        raw=raw,
        format_string=derived_fmt,
        zone="us-west-2",
        svc="auth_proxy",
        err_code=504,
        details="Gateway Timeout on upstream",
    )
    assert event.reconstruct() == raw

    # Known group names inspection
    registry = LogTemplateRegistry(templates_path=tmp_path / "t.json")
    registry.register_template("T1", r"^(?P<alpha>\w+)\s+(?P<beta>\w+)$")
    registry.register_template("T2", r"^(?P<beta>\w+)\s+(?P<gamma>\w+)$")
    names = registry.get_known_group_names()
    assert names == ["alpha", "beta", "gamma"]
