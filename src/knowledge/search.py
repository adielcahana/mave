"""External documentation and web knowledge retrieval tools for the SRE agent."""

import html
import re
from pathlib import Path
from typing import Any

import httpx

__all__ = [
    "search_external_knowledge",
    "search_local_docs",
    "search_web",
]

_DEFAULT_DOCS_DIR = Path("docs")
_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko)"


def _clean_html(raw_html: str) -> str:
    """Strip HTML tags and unescape HTML entities."""
    no_tags = re.sub(r"<[^<]+?>", "", raw_html)
    return html.unescape(no_tags).strip()


def search_web(query: str, max_results: int = 5) -> list[dict[str, str]]:
    """Query DuckDuckGo HTML search for technical documentation and troubleshooting guides.

    Args:
        query: Search keywords (e.g. error messages, subsystem daemons, hardware codes).
        max_results: Maximum number of search snippets to return.

    Returns:
        List of dicts with 'title', 'snippet', and 'url'.
    """
    clean_q = query.strip()
    if not clean_q:
        return []

    try:
        with httpx.Client(
            headers={"User-Agent": _USER_AGENT},
            timeout=8.0,
            follow_redirects=True,
        ) as client:
            resp = client.post(
                "https://html.duckduckgo.com/html/",
                data={"q": clean_q},
            )
            if resp.status_code != 200:
                return []
            text = resp.text
    except Exception:
        return []

    # Extract snippets and titles from DuckDuckGo HTML layout
    snippet_matches = re.findall(
        r'<a[^>]+class=[\'"]result__snippet[\'"][^>]*>(.*?)</a>',
        text,
        re.DOTALL,
    )
    title_matches = re.findall(
        r'<h2[^>]*class=[\'"]result__title[\'"][^>]*>(.*?)</h2>',
        text,
        re.DOTALL,
    )
    url_matches = re.findall(
        r'<a[^>]+class=[\'"]result__url[\'"][^>]*href=[\'"]([^\'"]+)[\'"]',
        text,
    )

    results: list[dict[str, str]] = []
    total = min(len(snippet_matches), len(title_matches), max_results)
    for i in range(total):
        title = _clean_html(title_matches[i])
        snippet = _clean_html(snippet_matches[i])
        url = url_matches[i] if i < len(url_matches) else "https://duckduckgo.com"
        if snippet:
            results.append({"title": title, "snippet": snippet, "url": url})

    return results


def search_local_docs(
    query: str,
    docs_dir: Path | str | None = None,
    max_results: int = 5,
) -> list[dict[str, str]]:
    """Scan local documentation files (.md, .txt, .rst) for relevant sections.

    Args:
        query: Search keywords or phrases.
        docs_dir: Directory containing documentation files.
        max_results: Maximum number of document excerpts to return.

    Returns:
        List of dicts with 'source', 'excerpt', and 'path'.
    """
    target_dir = Path(docs_dir) if docs_dir is not None else _DEFAULT_DOCS_DIR
    if not target_dir.is_dir():
        return []

    query_tokens = [w.lower() for w in query.split() if len(w) >= 3]
    if not query_tokens:
        return []

    matches: list[dict[str, Any]] = []

    for file_path in target_dir.rglob("*"):
        if not file_path.is_file() or file_path.suffix.lower() not in {".md", ".txt", ".rst"}:
            continue

        try:
            content = file_path.read_text(encoding="utf-8")
        except Exception:
            continue

        # Split into paragraphs or markdown sections
        paragraphs = re.split(r"\n\s*\n", content)
        for p in paragraphs:
            p_clean = p.strip()
            if not p_clean:
                continue
            p_lower = p_clean.lower()
            score = sum(1 for tok in query_tokens if tok in p_lower)
            if score > 0:
                matches.append(
                    {
                        "score": score,
                        "source": file_path.name,
                        "path": str(file_path),
                        "excerpt": p_clean[:500],
                    }
                )

    # Sort descending by match score
    matches.sort(key=lambda m: m["score"], reverse=True)
    return [
        {"source": m["source"], "path": m["path"], "excerpt": m["excerpt"]}
        for m in matches[:max_results]
    ]


def search_external_knowledge(query: str) -> str:
    """Query external knowledge sources including local cluster documentation and web search.

    Use this tool when standard cluster subsystem runbooks in `query_subsystem_docs`
    do not contain sufficient diagnosis, or when investigating novel error messages,
    third-party daemon faults, or hardware error codes.

    Args:
        query: Specific search query (e.g. 'slurm controller daemon hung',
            'lustre filesystem lnet error', 'infiniband port degraded state').

    Returns:
        Structured excerpts from local documentation and online technical articles.
    """
    clean_q = query.strip()
    if not clean_q:
        return "Please provide a non-empty search query."

    sections: list[str] = []

    # 1. Search local documentation
    local_hits = search_local_docs(clean_q)
    if local_hits:
        local_text = ["### Local Documentation Matches:"]
        for hit in local_hits:
            local_text.append(
                f"- **Source:** {hit['source']} (`{hit['path']}`)\n  **Excerpt:** {hit['excerpt']}"
            )
        sections.append("\n\n".join(local_text))

    # 2. Search web
    web_hits = search_web(clean_q)
    if web_hits:
        web_text = ["### Web Documentation & Troubleshooting Guides:"]
        for hit in web_hits:
            web_text.append(
                f"- **Title:** {hit['title']}\n"
                f"  **URL:** {hit['url']}\n"
                f"  **Snippet:** {hit['snippet']}"
            )
        sections.append("\n\n".join(web_text))

    if not sections:
        return (
            f"No external documentation or web troubleshooting guides found for '{clean_q}'. "
            f"Please verify spelling or broaden the search terms."
        )

    return "\n\n---\n\n".join(sections)
