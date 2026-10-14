"""Tests for local documentation (grep/jq) and web search tools."""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from src.knowledge.search import (
    search_external_knowledge,
    search_local_docs,
    search_web,
    search_web_docs,
)


def test_search_local_docs_grep_text(tmp_path: Path) -> None:
    """Verify search_local_docs performs line-by-line grep with line numbers."""
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()

    slurm_doc = docs_dir / "slurm.md"
    slurm_doc.write_text(
        "# Slurm Troubleshooting\n"
        "Node state draining frequently occurs.\n"
        "If the slurmctld controller daemon is unresponsive, check socket connectivity.\n"
        "Restart the service using systemctl restart slurmctld.\n",
        encoding="utf-8",
    )

    # Grep-like search by keyword
    output = search_local_docs("slurmctld", search_paths=[docs_dir])
    assert "slurm.md:3: If the slurmctld controller daemon is unresponsive" in output

    # Grep regex search
    regex_out = search_local_docs(r"systemctl\s+restart", search_paths=[docs_dir])
    assert "slurm.md:4: Restart the service using systemctl restart slurmctld." in regex_out


def test_search_local_docs_jq_json(tmp_path: Path) -> None:
    """Verify search_local_docs executes jq-style queries on JSON files."""
    res_dir = tmp_path / "resources"
    res_dir.mkdir()

    runbooks_file = res_dir / "runbooks.json"
    runbooks_data = [
        {
            "subsystem": "unix.hw",
            "scenario": "Network interface alt0 down",
            "root_cause": "NIFF heartbeat lost on interface alt0",
            "remediation_steps": ["Check physical cable seating", "Run ifconfig alt0 up"],
            "keywords": ["alt0", "down", "niff"],
        },
        {
            "subsystem": "clusterfilesystem",
            "scenario": "Domain panic on volume /data",
            "root_cause": "AdvFS metadata corruption halts I/O",
            "remediation_steps": ["Unmount volume", "Run advfsck"],
            "keywords": ["panic", "advfs", "corruption"],
        },
    ]
    runbooks_file.write_text(json.dumps(runbooks_data), encoding="utf-8")

    # JQ filter style: .subsystem == "unix.hw"
    jq_out = search_local_docs('.subsystem == "unix.hw"', search_paths=[res_dir])
    assert "Network interface alt0 down" in jq_out
    assert "unix.hw" in jq_out
    assert "AdvFS" not in jq_out

    # JQ filter style: select(.subsystem == "clusterfilesystem")
    select_out = search_local_docs(
        'select(.subsystem == "clusterfilesystem")',
        search_paths=[res_dir],
    )
    assert "Domain panic on volume /data" in select_out
    assert "advfsck" in select_out

    # Key=value filter style
    kv_out = search_local_docs("subsystem=unix.hw", search_paths=[res_dir])
    assert "Network interface alt0 down" in kv_out

    # JQ keyword array filter
    kw_out = search_local_docs('.keywords[] == "panic"', search_paths=[res_dir])
    assert "Domain panic on volume /data" in kw_out

    # JQ field projection: .scenario
    proj_out = search_local_docs(".scenario", search_paths=[res_dir])
    assert "Network interface alt0 down" in proj_out
    assert "Domain panic on volume /data" in proj_out


def test_search_local_docs_empty_and_no_match(tmp_path: Path) -> None:
    """Verify search_local_docs handles empty query and no matches gracefully."""
    empty_out = search_local_docs("")
    assert "Please provide a non-empty search query" in empty_out

    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()
    no_match_out = search_local_docs("unknown_symbol_error", search_paths=[empty_dir])
    assert "No documentation or knowledge files found" in no_match_out or "No local" in no_match_out


def test_search_web_docs_formatting() -> None:
    """Verify search_web_docs formats search results into markdown."""
    with patch(
        "src.knowledge.search.search_web",
        return_value=[
            {
                "title": "Slurm Troubleshooting Guide",
                "snippet": "Resolving node draining and controller deadlocks.",
                "url": "https://slurm.schedmd.com/troubleshoot.html",
            }
        ],
    ):
        output = search_web_docs("slurm deadlock")

    assert "### Web Search Results for 'slurm deadlock':" in output
    assert "**Title:** Slurm Troubleshooting Guide" in output
    assert "https://slurm.schedmd.com/troubleshoot.html" in output
    assert "controller deadlocks" in output


def test_search_web_docs_no_results() -> None:
    """Verify search_web_docs handles empty results gracefully."""
    with patch("src.knowledge.search.search_web", return_value=[]):
        output = search_web_docs("completely_nonexistent_hpc_error_xyz")
    assert "No external web documentation" in output


def test_search_web_parses_html() -> None:
    """Verify search_web extracts title, snippet, and url from mock DuckDuckGo HTML."""
    mock_html = (
        "<html><body>"
        '<h2 class="result__title"><a href="https://example.com/guide">'
        "Slurm Troubleshooting Guide</a></h2>"
        '<a class="result__snippet" href="https://example.com/guide">'
        "Guide to resolving daemon freezes and network splits.</a>"
        '<a class="result__url" href="https://example.com/guide">https://example.com/guide</a>'
        "</body></html>"
    )

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.text = mock_html

    with patch("httpx.Client.post", return_value=mock_resp):
        hits = search_web("slurm freeze", max_results=3)

    assert len(hits) == 1
    assert hits[0]["title"] == "Slurm Troubleshooting Guide"
    assert "resolving daemon freezes" in hits[0]["snippet"]
    assert hits[0]["url"] == "https://example.com/guide"


def test_search_web_handles_network_error() -> None:
    """Verify search_web gracefully handles network exceptions."""
    with patch("httpx.Client.post", side_effect=RuntimeError("Network timeout")):
        hits = search_web("network error test")
    assert hits == []


def test_search_external_knowledge_composite(tmp_path: Path) -> None:
    """Verify search_external_knowledge falls back to web if local has no hits."""
    empty_dir = tmp_path / "empty_docs"
    empty_dir.mkdir()

    with patch("src.knowledge.search._DEFAULT_SEARCH_PATHS", [empty_dir]):
        with patch(
            "src.knowledge.search.search_web",
            return_value=[
                {
                    "title": "Mellanox InfiniBand Architecture",
                    "snippet": "Troubleshooting physical layer symbol errors.",
                    "url": "https://mellanox.com/ib",
                }
            ],
        ):
            output = search_external_knowledge("InfiniBand symbol error")

    assert "Web Search Results" in output
    assert "Mellanox InfiniBand" in output
