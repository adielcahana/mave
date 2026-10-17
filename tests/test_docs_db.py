"""Tests for HPC cluster knowledge base and runbooks."""

import json
from pathlib import Path

import pytest

from src.knowledge.docs_db import (
    RunbookEntry,
    add_runbook_entry,
    create_runbook_from_resolution,
    get_all_runbooks,
    load_runbooks,
)
from src.rules.models import LogEvent, ProblemResolution


def test_citations_present_on_all_runbook_entries() -> None:
    entries = get_all_runbooks()
    assert len(entries) >= 15
    for entry in entries:
        assert isinstance(entry, RunbookEntry)
        assert entry.citation.startswith("[LANL HPC Runbook:")
        assert entry.citation.endswith("]")
        assert len(entry.remediation_steps) >= 2
        assert len(entry.root_cause) > 10
        assert isinstance(entry.remediation_steps, tuple)
        assert isinstance(entry.keywords, tuple)


def test_runbook_entry_immutability_and_tuple_types() -> None:
    entry = RunbookEntry(
        scenario="test_scenario",
        root_cause="test_root_cause",
        remediation_steps=("step 1", "step 2"),
        citation="[LANL HPC Runbook: test]",
        keywords=("kw1", "kw2"),
    )
    assert isinstance(entry.remediation_steps, tuple)
    assert isinstance(entry.keywords, tuple)
    assert "### test_scenario" in entry.format_text()


def test_runbooks_json_file_exists_and_loads() -> None:
    path = Path("resources/runbooks.json")
    assert path.is_file(), "resources/runbooks.json must exist"

    with open(path, encoding="utf-8") as f:
        data = json.load(f)

    assert isinstance(data, list)
    assert len(data) >= 15
    for item in data:
        for required_field in (
            "scenario",
            "root_cause",
            "remediation_steps",
            "citation",
            "keywords",
        ):
            assert required_field in item, f"Missing field {required_field} in entry {item}"
        assert "subsystem" not in item
        assert "event_type" not in item


def test_load_runbooks_custom_path_and_not_found(tmp_path: Path) -> None:
    custom_file = tmp_path / "custom_runbooks.json"
    dummy_data = [
        {
            "scenario": "Custom test scenario",
            "root_cause": "Test root cause",
            "remediation_steps": ["Step 1", "Step 2"],
            "citation": "[LANL HPC Runbook: test-custom-v1.0]",
            "keywords": ["test", "custom"],
        }
    ]
    custom_file.write_text(json.dumps(dummy_data), encoding="utf-8")

    loaded = load_runbooks(custom_file)
    assert len(loaded) == 1
    assert loaded[0].scenario == "Custom test scenario"
    assert loaded[0].remediation_steps == ("Step 1", "Step 2")

    with pytest.raises(FileNotFoundError):
        load_runbooks(tmp_path / "nonexistent.json")


def test_add_and_save_runbook_entry(tmp_path: Path) -> None:
    custom_entry = RunbookEntry(
        scenario="Slurm controller unresponsive",
        root_cause="Slurmctld daemon thread deadlock during heavy job throughput",
        remediation_steps=("Restart slurmctld service", "Check /var/log/slurmctld.log"),
        citation="[HPC Community: slurm-deadlock-v1.0]",
        keywords=("slurm", "slurmctld", "deadlock"),
    )
    save_file = tmp_path / "saved_runbooks.json"
    add_runbook_entry(custom_entry, persist=True, path=save_file)

    assert save_file.is_file()
    with open(save_file, encoding="utf-8") as f:
        data = json.load(f)
    assert any(item["scenario"] == "Slurm controller unresponsive" for item in data)


def test_create_runbook_from_resolution(tmp_path: Path) -> None:
    event = LogEvent(
        log_id="999001",
        node="node-101",
        component="infiniband_fabric",
        timestamp=1074220000,
        message="ib0 port link down symbol error rate exceeded",
        raw=(
            "999001 node-101 infiniband_fabric port_error 1074220000 1 "
            "ib0 port link down symbol error rate exceeded"
        ),
    )
    resolution = ProblemResolution(
        error_summary="InfiniBand fabric link degradation",
        root_cause=(
            "High physical symbol error rate on HCA port 1 caused by transceiver dirty optics"
        ),
        severity="HIGH",
        remediation_steps=[
            "Clean fiber optic connector pins on HCA port 1",
            "Perform ibportstate reset",
        ],
        citations_and_reasons=["[Web Source: Mellanox IB Troubleshooting Guide]"],
    )

    save_file = tmp_path / "learned_runbooks.json"
    entry = create_runbook_from_resolution(event, resolution, persist=True, path=save_file)

    assert entry.scenario == "InfiniBand fabric link degradation"
    assert "symbol" in entry.keywords or "infiniband_fabric" in entry.keywords
    assert save_file.is_file()
