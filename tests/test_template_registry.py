"""Tests for template_registry, validate_suggested_regex, and dynamic retry loop."""

import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from pydantic_ai.models.test import TestModel

from src.agent import (
    analyze_log,
    create_agent,
    validate_suggested_regex,
)
from src.config import Settings
from src.pipeline import LogPipeline
from src.rules.models import LogEvent, ProblemResolutionWithRegex
from src.rules.template_registry import LogTemplateRegistry


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
    """Verify validate_suggested_regex validates syntax, matching, and required groups."""
    sample_log = "2026-10-10 [storage_engine] Disk I/O timeout on volume /data"

    # 1. Invalid regex syntax
    valid, err = validate_suggested_regex(r"[unclosed bracket", sample_log)
    assert valid is False
    assert "Invalid regex syntax" in err

    # 2. Non-matching regex
    valid, err = validate_suggested_regex(r"^node-\d+\s+.*$", sample_log)
    assert valid is False
    assert "failed to match the incoming raw log line" in err

    # 3. Missing required groups (?P<subsystem>...) and (?P<message>...)
    valid, err = validate_suggested_regex(
        r"^(?P<timestamp>\S+)\s+\[(?P<subsystem>\S+)\]\s+.*$",
        sample_log,
    )
    assert valid is False
    assert "missing required named capture groups" in err
    assert "message" in err

    # 4. Valid syntax, match, and groups
    valid, err = validate_suggested_regex(
        r"^(?P<timestamp>\S+)\s+\[(?P<subsystem>[^\]]+)\]\s+(?P<message>.*)$",
        sample_log,
    )
    assert valid is True
    assert err == "OK"


@pytest.mark.anyio
async def test_analyze_log_with_unparsed_log_and_retry_feedback() -> None:
    """Verify analyze_log uses ProblemResolutionWithRegex and retries if regex invalid."""
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

    # Agent first returns invalid regex (missing message group), then corrected regex
    attempt = 0

    def mock_model_output(*args: Any, **kwargs: Any) -> ProblemResolutionWithRegex:
        nonlocal attempt
        attempt += 1
        if attempt == 1:
            # Bad regex: missing ?P<message>
            return ProblemResolutionWithRegex(
                error_summary="Fatal error in custom subsys",
                root_cause="Hardware error code 42",
                severity="CRITICAL",
                remediation_steps=["Reboot subsystem"],
                citations_and_reasons=["Custom Runbook"],
                suggested_log_template_regex=r"^(?P<subsystem>MY_SUBSYS):\s+error.*$",
            )
        # Correct regex
        return ProblemResolutionWithRegex(
            error_summary="Fatal error in custom subsys",
            root_cause="Hardware error code 42",
            severity="CRITICAL",
            remediation_steps=["Reboot subsystem"],
            citations_and_reasons=["Custom Runbook"],
            suggested_log_template_regex=r"^(?P<subsystem>MY_SUBSYS):\s+(?P<message>.*)$",
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

        agent = create_agent(settings=test_settings)
        resolution = await analyze_log(unparsed_event, agent=agent, settings=test_settings)

        assert isinstance(resolution, ProblemResolutionWithRegex)
        assert (
            resolution.suggested_log_template_regex
            == r"^(?P<subsystem>MY_SUBSYS):\s+(?P<message>.*)$"
        )
        assert mock_run.call_count == 2  # 1 initial + 1 retry


@pytest.mark.anyio
async def test_pipeline_dynamic_template_registration_on_unparsed_log(tmp_path: Path) -> None:
    """Verify pipeline registers dynamic regex from ProblemResolutionWithRegex for unparsed logs."""
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
