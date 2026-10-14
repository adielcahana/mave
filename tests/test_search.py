"""Tests for external knowledge and web search tools."""

from pathlib import Path
from unittest.mock import MagicMock, patch

from src.knowledge.search import (
    search_external_knowledge,
    search_local_docs,
    search_web,
)


def test_search_local_docs(tmp_path: Path) -> None:
    """Verify search_local_docs scans markdown and text files and scores by keyword."""
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()

    slurm_doc = docs_dir / "slurm.md"
    slurm_doc.write_text(
        "# Slurm Troubleshooting\n\n"
        "If the slurmctld controller daemon is unresponsive, check socket connectivity.\n"
        "Restart the service using systemctl restart slurmctld.\n",
        encoding="utf-8",
    )

    lustre_doc = docs_dir / "lustre.txt"
    lustre_doc.write_text(
        "Lustre Storage System\n\n"
        "OST failover occurs when storage targets experience disk timeout.\n",
        encoding="utf-8",
    )

    # Search matching slurm
    results = search_local_docs("slurmctld daemon unresponsive", docs_dir=docs_dir)
    assert len(results) > 0
    assert results[0]["source"] == "slurm.md"
    assert "slurmctld" in results[0]["excerpt"]

    # Search with no match
    no_results = search_local_docs("kubernetes pod eviction", docs_dir=docs_dir)
    assert len(no_results) == 0

    # Search with empty query
    empty_results = search_local_docs("", docs_dir=docs_dir)
    assert len(empty_results) == 0


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


def test_search_external_knowledge_aggregates(tmp_path: Path) -> None:
    """Verify search_external_knowledge aggregates local docs and web results."""
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    (docs_dir / "ib.md").write_text(
        "InfiniBand link degradation: clean transceiver optic pins.",
        encoding="utf-8",
    )

    with patch("src.knowledge.search._DEFAULT_DOCS_DIR", docs_dir):
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

    assert "Local Documentation Matches" in output
    assert "ib.md" in output
    assert "Web Documentation & Troubleshooting Guides" in output
    assert "Mellanox InfiniBand" in output
    assert "https://mellanox.com/ib" in output


def test_search_external_knowledge_empty_query() -> None:
    output = search_external_knowledge("   ")
    assert "Please provide a non-empty search query" in output
