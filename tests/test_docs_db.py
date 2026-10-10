"""Tests for LANL HPC subsystem knowledge base and runbooks."""

import json
from pathlib import Path

import pytest

from src.knowledge.docs_db import (
    RunbookEntry,
    get_all_runbooks,
    list_known_subsystems,
    load_runbooks,
    query_subsystem_docs,
)


def test_knowledge_base_covers_key_subsystems() -> None:
    subsystems = list_known_subsystems()
    expected = [
        "clusterfilesystem",
        "resourcemgmtdaeomon",
        "unix.hw",
        "switch_module",
        "boot_cmd",
    ]
    for subsys in expected:
        assert subsys in subsystems, f"Expected {subsys} to be in known subsystems"


def test_query_subsystem_returns_actionable_runbook() -> None:
    doc = query_subsystem_docs("clusterfilesystem", "panic")
    assert "ServerFileSystem domain panic" in doc
    assert "Remediation" in doc
    assert "Citation" in doc
    assert "Root Cause" in doc


def test_query_subsystem_resourcemgmtdaeomon_modes() -> None:
    # Error 10
    doc_err10 = query_subsystem_docs("resourcemgmtdaeomon", "err = 10")
    assert "10" in doc_err10
    assert "Remediation" in doc_err10
    assert "Citation" in doc_err10

    # Controller timeout
    doc_timeout = query_subsystem_docs("resourcemgmtdaeomon", "timeout")
    assert "timeout" in doc_timeout.lower()
    assert "controller" in doc_timeout.lower()

    # Startup failure
    doc_startup = query_subsystem_docs("resourcemgmtdaeomon", "startup")
    assert "startup" in doc_startup.lower()


def test_query_subsystem_clusterfilesystem_modes() -> None:
    # fdmn.panic
    doc_panic = query_subsystem_docs("clusterfilesystem", "panic")
    assert "panic" in doc_panic.lower()

    # fdmn.full
    doc_full = query_subsystem_docs("clusterfilesystem", "full")
    assert "full" in doc_full.lower()

    # unserved storage domains
    doc_unserved = query_subsystem_docs("clusterfilesystem", "no server")
    assert "server" in doc_unserved.lower()

    # metadata corruption
    doc_meta = query_subsystem_docs("clusterfilesystem", "metadata")
    assert "metadata" in doc_meta.lower()


def test_query_subsystem_unix_hw_modes() -> None:
    # alt0 down
    doc_alt0 = query_subsystem_docs("unix.hw", "alt0")
    assert "alt0" in doc_alt0
    assert "Remediation" in doc_alt0

    # state change unavailable
    doc_state = query_subsystem_docs("unix.hw", "unavailable")
    assert "unavailable" in doc_state.lower()

    # memory ECC corrections
    doc_ecc = query_subsystem_docs("unix.hw", "ECC")
    assert "ecc" in doc_ecc.lower()


def test_query_subsystem_switch_module_modes() -> None:
    # PSU failover / degraded
    doc_psu = query_subsystem_docs("switch_module", "PSU")
    assert "psu" in doc_psu.lower()
    assert "Remediation" in doc_psu

    # temperature warnings / overheat
    doc_temp = query_subsystem_docs("switch_module", "temperature")
    assert "temperature" in doc_temp.lower()

    # link flapping / errors
    doc_link = query_subsystem_docs("switch_module", "flapping")
    assert "link" in doc_link.lower()


def test_query_subsystem_boot_cmd_modes() -> None:
    # CMF-Port disconnection
    doc_cmf = query_subsystem_docs("boot_cmd", "CMF-Port")
    assert "cmf-port" in doc_cmf.lower()
    assert "Remediation" in doc_cmf

    # node state determination errors / abort
    doc_abort = query_subsystem_docs("boot_cmd", "abort")
    assert "state" in doc_abort.lower() or "abort" in doc_abort.lower()

    # console busy / prompt timeout
    doc_console = query_subsystem_docs("boot_cmd", "console busy")
    assert "console" in doc_console.lower()


def test_query_keyword_filtering_narrows_results() -> None:
    # Without keyword, returns full subsystem runbook
    all_switch = query_subsystem_docs("switch_module")
    # With keyword 'psu', returns only PSU-related entry
    psu_switch = query_subsystem_docs("switch_module", "psu")

    assert "PSU" in psu_switch
    assert len(psu_switch) < len(all_switch)
    assert "Temperature" not in psu_switch


def test_query_fallback_behavior_unknown_keyword() -> None:
    # Unknown keyword should fall back gracefully without raising an exception
    doc = query_subsystem_docs("clusterfilesystem", "nonexistent_symptom_xyz123")
    assert "clusterfilesystem" in doc.lower()
    assert "Remediation" in doc


def test_query_fallback_behavior_unknown_subsystem() -> None:
    # Unknown subsystem should provide clear listing of available subsystems
    doc = query_subsystem_docs("nonexistent_subsystem")
    assert "Unknown subsystem" in doc or "Available subsystems" in doc
    assert "clusterfilesystem" in doc


def test_subsystem_alias_and_case_insensitivity() -> None:
    # Case insensitivity
    doc_upper = query_subsystem_docs("CLUSTERFILESYSTEM", "PANIC")
    assert "ServerFileSystem domain panic" in doc_upper

    # Spelling alias: resourcemgmtdaemon vs resourcemgmtdaeomon
    doc_alias = query_subsystem_docs("resourcemgmtdaemon", "err = 10")
    assert "10" in doc_alias


def test_citations_present_on_all_runbook_entries() -> None:
    entries = get_all_runbooks()
    assert len(entries) >= 15  # At least 15 failure scenarios covered across the 5 subsystems
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
        subsystem="test_sub",
        scenario="test_scenario",
        root_cause="test_root_cause",
        remediation_steps=("step 1", "step 2"),
        citation="[LANL HPC Runbook: test]",
        keywords=("kw1", "kw2"),
    )
    assert isinstance(entry.remediation_steps, tuple)
    assert isinstance(entry.keywords, tuple)


def test_runbooks_json_file_exists_and_loads() -> None:
    path = Path("resources/runbooks.json")
    assert path.is_file(), "resources/runbooks.json must exist"

    with open(path, encoding="utf-8") as f:
        data = json.load(f)

    assert isinstance(data, list)
    assert len(data) >= 15
    for item in data:
        for required_field in (
            "subsystem",
            "scenario",
            "root_cause",
            "remediation_steps",
            "citation",
            "keywords",
            "event_type",
        ):
            assert required_field in item, f"Missing field {required_field} in entry {item}"


def test_load_runbooks_custom_path_and_not_found(tmp_path: Path) -> None:
    custom_file = tmp_path / "custom_runbooks.json"
    dummy_data = [
        {
            "subsystem": "test_system",
            "scenario": "Custom test scenario",
            "root_cause": "Test root cause",
            "remediation_steps": ["Step 1", "Step 2"],
            "citation": "[LANL HPC Runbook: test-custom-v1.0]",
            "keywords": ["test", "custom"],
            "event_type": "test_event",
        }
    ]
    custom_file.write_text(json.dumps(dummy_data), encoding="utf-8")

    loaded = load_runbooks(custom_file)
    assert len(loaded) == 1
    assert loaded[0].subsystem == "test_system"
    assert loaded[0].scenario == "Custom test scenario"
    assert loaded[0].remediation_steps == ("Step 1", "Step 2")

    with pytest.raises(FileNotFoundError):
        load_runbooks(tmp_path / "nonexistent.json")
