"""Local documentation (grep/jq) and web knowledge retrieval tools for the SRE agent."""

import html
import json
import re
from pathlib import Path
from typing import Any

import httpx

__all__ = [
    "read_local_file",
    "search_external_knowledge",
    "search_local_docs",
    "search_web",
    "search_web_docs",
]

_DEFAULT_SEARCH_PATHS = [Path("docs"), Path("resources")]
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


def search_web_docs(query: str, max_results: int = 5) -> str:
    """Query external technical documentation and web troubleshooting guides via DuckDuckGo.

    Use this tool ONLY when local documentation and cluster runbooks (via `query_subsystem_docs`
    and `search_local_docs`) do not contain sufficient guidance for an incident.

    Args:
        query: Specific search terms (e.g. 'lustre lnet configuration error',
            'slurm controller deadlocks').
        max_results: Maximum search result snippets to return (default 5).

    Returns:
        Structured excerpts with title, URL, and snippet text.
    """
    clean_q = query.strip()
    if not clean_q:
        return "Please provide a non-empty search query."

    hits = search_web(clean_q, max_results=max_results)
    if not hits:
        return (
            f"No external web documentation or troubleshooting guides found for '{clean_q}'. "
            f"Please verify spelling or broaden the search terms."
        )

    sections: list[str] = [f"### Web Search Results for '{clean_q}':"]
    for hit in hits:
        sections.append(
            f"- **Title:** {hit['title']}\n  **URL:** {hit['url']}\n  **Snippet:** {hit['snippet']}"
        )
    return "\n\n".join(sections)


def _parse_jq_filter(query: str) -> tuple[str, str] | None:
    """Extract (field_name, expected_value) from common jq filter expressions.

    Supports:
      - select(.subsystem == "unix.hw")
      - .subsystem == "unix.hw" or .subsystem="unix.hw"
      - subsystem=unix.hw or subsystem="unix.hw"
      - .keywords[] == "panic" or .keywords == "panic"
    """
    # Pattern: select(.field == "val") or .field == "val" or field="val"
    m = re.match(
        r"^(?:select\()?\s*\.?([\w.]+)(?:\[\])?\s*(?:==|=)\s*['\"]?([^'\"]+?)['\"]?\s*\)?$",
        query.strip(),
    )
    if m:
        field_name = m.group(1).split(".")[-1]
        expected_val = m.group(2).strip().lower()
        return field_name, expected_val
    return None


def _evaluate_json_match(
    item: dict[str, Any],
    query: str,
    jq_filter: tuple[str, str] | None,
) -> bool:
    """Check if a JSON object matches a jq filter or general keyword/regex grep."""
    if jq_filter is not None:
        field_name, expected_val = jq_filter
        if field_name in item:
            val = item[field_name]
            if isinstance(val, (list, tuple)):
                return any(expected_val in str(x).lower() for x in val)
            return expected_val in str(val).lower()
        return False

    # General keyword / regex grep across all fields in the JSON object
    q_lower = query.lower()
    for v in item.values():
        if isinstance(v, (list, tuple)):
            if any(q_lower in str(x).lower() for x in v):
                return True
        elif q_lower in str(v).lower():
            return True
    return False


def _search_json_file(
    file_path: Path,
    query: str,
    max_results: int = 3,
) -> list[str]:
    """Execute a jq-like query or field grep on a JSON file."""
    try:
        content = file_path.read_text(encoding="utf-8")
        data = json.loads(content)
    except Exception:
        return []

    items = data if isinstance(data, list) else [data]
    jq_filter = _parse_jq_filter(query)

    # Check for simple field projection: e.g. .scenario or .remediation_steps
    projection = None
    if query.strip().startswith(".") and "==" not in query and "=" not in query:
        projection = query.strip().lstrip(".").rstrip("[]")

    matches: list[str] = []
    for idx, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            continue

        if projection and projection in item:
            val_repr = json.dumps(item[projection], indent=2)
            matches.append(f"[File: {file_path.as_posix()} #{idx} .{projection}]\n{val_repr}")
            if len(matches) >= max_results:
                break
        elif _evaluate_json_match(item, query, jq_filter):
            item_repr = json.dumps(item, indent=2)
            matches.append(f"[File: {file_path.as_posix()} #{idx}]\n{item_repr}")
            if len(matches) >= max_results:
                break

    return matches


def _search_text_file(
    file_path: Path,
    query: str,
    max_results: int = 5,
) -> list[str]:
    """Execute a grep-like line scan across a text or Markdown file."""
    try:
        lines = file_path.read_text(encoding="utf-8").splitlines()
    except Exception:
        return []

    try:
        pattern = re.compile(query, re.IGNORECASE)
    except re.error:
        pattern = re.compile(re.escape(query), re.IGNORECASE)

    matches: list[str] = []
    for line_no, line in enumerate(lines, start=1):
        if pattern.search(line):
            matches.append(f"{file_path.as_posix()}:{line_no}: {line.strip()}")
            if len(matches) >= max_results:
                break
    return matches


def search_local_docs(
    query: str,
    file_pattern: str = "*",
    max_results: int = 5,
    search_paths: list[Path | str] | None = None,
) -> str:
    """Search local documentation and cluster knowledge files using grep-like and jq-like queries.

    Search Execution Mechanism:
    - Text/Markdown files (.md, .txt, .rst, .log in docs/):
      Executed as a grep-style regex/keyword scan line-by-line across files,
      returning matching line numbers and excerpts.
    - Structured JSON files (.json in resources/ and docs/):
      Executed as jq-style filter expressions or field matching.
      Supported query styles:
        * jq filter expressions: e.g. `.subsystem == "unix.hw"`,
          `select(.subsystem == "clusterfilesystem")`
        * key=value filters: e.g. `subsystem=resourcemgmtdaeomon`, `keywords=panic`
        * jq field extraction: `.[].scenario`, `.root_cause`
        * keyword grep across records: scans all JSON fields for the query string/regex.

    Always run `search_local_docs` first before escalating to web search.

    Args:
        query: Grep regex pattern for text files, or jq filter/field expression for JSON files.
        file_pattern: Optional glob to target specific files (e.g. '*.md', '*.json').
        max_results: Maximum number of matching entries to return.
        search_paths: Optional list of directories or files to search.
            Defaults to docs/ and resources/.

    Returns:
        Formatted excerpts and matching records with file paths and line numbers.
    """
    clean_q = query.strip()
    if not clean_q:
        return "Please provide a non-empty search query."

    paths = [Path(p) for p in search_paths] if search_paths else _DEFAULT_SEARCH_PATHS
    collected_files: list[Path] = []
    for p in paths:
        if p.is_file():
            collected_files.append(p)
        elif p.is_dir():
            collected_files.extend(p.rglob(file_pattern))

    if not collected_files:
        return f"No documentation or knowledge files found in search paths: {paths}."

    all_matches: list[str] = []
    for file_path in sorted(collected_files):
        if not file_path.is_file():
            continue

        ext = file_path.suffix.lower()
        if ext == ".json":
            matches = _search_json_file(file_path, clean_q, max_results=max_results)
            all_matches.extend(matches)
        elif ext in {".md", ".txt", ".rst", ".log", ".conf"}:
            matches = _search_text_file(file_path, clean_q, max_results=max_results)
            all_matches.extend(matches)

        if len(all_matches) >= max_results:
            break

    if not all_matches:
        return (
            f"No local documentation or runbook matches found for '{clean_q}'. "
            f"Executed search via grep across text/markdown files and jq on JSON runbooks/rules. "
            f"If local documentation is exhausted, escalate to `search_web_docs`."
        )

    return "\n\n---\n\n".join(all_matches[:max_results])


def search_external_knowledge(query: str) -> str:
    """Composite knowledge search helper combining local doc search with web fallback."""
    local_res = search_local_docs(query)
    has_local_matches = not (
        "No local documentation or runbook matches found" in local_res
        or "No documentation or knowledge files found" in local_res
    )
    if has_local_matches:
        return f"### Local Documentation Matches:\n{local_res}"

    web_res = search_web_docs(query)
    return f"{local_res}\n\n---\n\n{web_res}"


def read_local_file(
    file_path: str,
    start_line: int = 1,
    end_line: int | None = None,
    max_lines: int = 100,
) -> str:
    """Read a local documentation or knowledge file with 1-based line numbers.

    Use this tool as a follow-up to `search_local_docs` to inspect surrounding context,
    detailed remediation instructions, or specific sections around matched lines.

    Args:
        file_path: Relative or absolute path to the file (e.g. 'docs/slurm.md',
            'resources/runbooks.json', 'resources/rules.json').
        start_line: First line to read (1-based, inclusive, defaults to 1).
        end_line: Last line to read (1-based, inclusive). If omitted, reads up to
            max_lines starting from start_line.
        max_lines: Maximum number of lines to display if end_line is omitted (default 100).

    Returns:
        Header with file path and line range, followed by line-numbered content:
        'LineNo | Content'.
    """
    clean_p = file_path.strip().strip("'\"")
    if not clean_p:
        return "Please provide a valid file_path to read."

    target = Path(clean_p)
    if not target.is_file():
        for prefix in [Path("docs"), Path("resources")]:
            candidate = prefix / clean_p
            if candidate.is_file():
                target = candidate
                break

    if not target.is_file():
        return (
            f"File not found: '{file_path}'. "
            f"Please verify the path returned by `search_local_docs`."
        )

    try:
        lines = target.read_text(encoding="utf-8").splitlines()
    except Exception as e:
        return f"Error reading file '{file_path}': {e}"

    total_lines = len(lines)
    if total_lines == 0:
        return f"[File: {target.as_posix()}] (Empty file)"

    start_idx = max(1, start_line)
    if end_line is not None:
        end_idx = min(total_lines, max(start_idx, end_line))
    else:
        end_idx = min(total_lines, start_idx + max_lines - 1)

    if start_idx > total_lines:
        return (
            f"[File: {target.as_posix()}] start_line {start_line} exceeds "
            f"total lines ({total_lines})."
        )

    selected = lines[start_idx - 1 : end_idx]
    formatted_lines: list[str] = [
        f"=== [File: {target.as_posix()}] (Lines {start_idx}-{end_idx} of {total_lines}) ==="
    ]
    for idx, line in enumerate(selected, start=start_idx):
        formatted_lines.append(f"{idx:4d} | {line}")

    return "\n".join(formatted_lines)
