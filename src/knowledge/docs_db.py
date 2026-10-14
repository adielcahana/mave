"""Authoritative LANL HPC subsystem knowledge base and runbooks."""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.rules.models import LogEvent, ProblemResolution

__all__ = [
    "RunbookEntry",
    "add_runbook_entry",
    "create_runbook_from_resolution",
    "get_all_runbooks",
    "list_known_subsystems",
    "load_runbooks",
    "query_subsystem_docs",
    "resolve_subsystem",
    "save_runbooks",
]


@dataclass(frozen=True)
class RunbookEntry:
    """Individual failure mode runbook entry for an HPC cluster subsystem."""

    subsystem: str
    scenario: str
    root_cause: str
    remediation_steps: tuple[str, ...]
    citation: str
    keywords: tuple[str, ...] = ()
    event_type: str = "*"

    def __post_init__(self) -> None:
        if not isinstance(self.remediation_steps, tuple):
            object.__setattr__(self, "remediation_steps", tuple(self.remediation_steps))
        if not isinstance(self.keywords, tuple):
            object.__setattr__(self, "keywords", tuple(self.keywords))

    def to_dict(self) -> dict[str, Any]:
        """Convert entry to JSON-serializable dictionary."""
        return {
            "subsystem": self.subsystem,
            "scenario": self.scenario,
            "root_cause": self.root_cause,
            "remediation_steps": list(self.remediation_steps),
            "citation": self.citation,
            "keywords": list(self.keywords),
            "event_type": self.event_type,
        }

    def format_text(self) -> str:
        """Render the runbook entry into standard structured markdown."""
        steps_formatted = "\n".join(
            f"{i + 1}. {step}" for i, step in enumerate(self.remediation_steps)
        )
        return (
            f"### [Subsystem: {self.subsystem}] {self.scenario}\n"
            f"**Root Cause:** {self.root_cause}\n"
            f"**Remediation Steps:**\n{steps_formatted}\n"
            f"**Citation:** {self.citation}"
        )


def _resolve_runbooks_path(custom_path: Path | str | None = None) -> Path:
    """Resolve path to runbooks.json, prioritizing explicit path, package root, and cwd."""
    if custom_path is not None:
        return Path(custom_path)

    # 1. Check relative to module directory: <repo_root>/resources/runbooks.json
    pkg_path = Path(__file__).resolve().parent.parent.parent / "resources" / "runbooks.json"
    if pkg_path.is_file():
        return pkg_path

    # 2. Check relative to current working directory
    cwd_path = Path("resources/runbooks.json")
    if cwd_path.is_file():
        return cwd_path.resolve()

    return pkg_path


def load_runbooks(path: Path | str | None = None) -> list[RunbookEntry]:
    """Load runbook entries dynamically from a JSON resource file."""
    runbooks_path = _resolve_runbooks_path(path)
    if not runbooks_path.is_file():
        raise FileNotFoundError(f"Runbooks resource file not found at: {runbooks_path}")

    with open(runbooks_path, encoding="utf-8") as f:
        data = json.load(f)

    return [
        RunbookEntry(
            subsystem=item["subsystem"],
            scenario=item["scenario"],
            root_cause=item["root_cause"],
            remediation_steps=tuple(item.get("remediation_steps", ())),
            citation=item["citation"],
            keywords=tuple(item.get("keywords", ())),
            event_type=item.get("event_type", "*"),
        )
        for item in data
    ]


def _derive_known_subsystems(entries: list[RunbookEntry]) -> list[str]:
    """Derive list of distinct subsystems from runbook entries preserving discovery order."""
    seen: set[str] = set()
    subsystems: list[str] = []
    for entry in entries:
        if entry.subsystem not in seen:
            seen.add(entry.subsystem)
            subsystems.append(entry.subsystem)
    return subsystems


# Active runbook database loaded dynamically from resource file
_RUNBOOKS: list[RunbookEntry] = load_runbooks()

# Canonical list of documented LANL HPC subsystems dynamically derived from runbooks
KNOWN_SUBSYSTEMS: list[str] = _derive_known_subsystems(_RUNBOOKS)

# Common aliases and spelling variations
_SUBSYSTEM_ALIASES: dict[str, str] = {
    "resourcemgmtdaemon": "resourcemgmtdaeomon",
    "resourcemgmt": "resourcemgmtdaeomon",
    "rmd": "resourcemgmtdaeomon",
    "cluster_filesystem": "clusterfilesystem",
    "serverfilesystem": "clusterfilesystem",
    "advfs": "clusterfilesystem",
    "cfs": "clusterfilesystem",
    "unix_hw": "unix.hw",
    "hw": "unix.hw",
    "hardware": "unix.hw",
    "switch": "switch_module",
    "interconnect": "switch_module",
    "boot": "boot_cmd",
    "bootcmd": "boot_cmd",
}


def list_known_subsystems() -> list[str]:
    """Return the list of supported and documented LANL HPC subsystems."""
    return list(KNOWN_SUBSYSTEMS)


def get_all_runbooks() -> list[RunbookEntry]:
    """Return all documented runbook entries in the knowledge base."""
    return list(_RUNBOOKS)


def save_runbooks(path: Path | str | None = None) -> None:
    """Serialize and save the current in-memory runbooks to a JSON file."""
    runbooks_path = _resolve_runbooks_path(path)
    data = [entry.to_dict() for entry in _RUNBOOKS]
    runbooks_path.parent.mkdir(parents=True, exist_ok=True)
    with open(runbooks_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def add_runbook_entry(
    entry: RunbookEntry,
    persist: bool = False,
    path: Path | str | None = None,
) -> None:
    """Add a new runbook entry to the knowledge base and optionally persist to disk."""
    global KNOWN_SUBSYSTEMS
    existing_idx = next(
        (
            i
            for i, e in enumerate(_RUNBOOKS)
            if e.subsystem == entry.subsystem and e.scenario == entry.scenario
        ),
        None,
    )
    if existing_idx is not None:
        _RUNBOOKS[existing_idx] = entry
    else:
        _RUNBOOKS.append(entry)
    KNOWN_SUBSYSTEMS = _derive_known_subsystems(_RUNBOOKS)
    if persist:
        save_runbooks(path)


def create_runbook_from_resolution(
    event: LogEvent,
    resolution: ProblemResolution,
    persist: bool = False,
    path: Path | str | None = None,
) -> RunbookEntry:
    """Synthesize a new RunbookEntry from an agent ProblemResolution and register it."""
    subsystem = resolve_subsystem(event.subsystem) if event.subsystem else "general"
    citation = (
        resolution.citations_and_reasons[0]
        if resolution.citations_and_reasons
        else f"[Learned SRE Runbook: {subsystem}]"
    )

    kw_candidates: set[str] = set()
    if event.event_type and event.event_type != "*":
        kw_candidates.add(event.event_type.lower())
    if event.flag and str(event.flag) != "-":
        kw_candidates.add(str(event.flag).lower())
    for word in event.message.split():
        cleaned_w = word.strip("=(),.:;\"'[]{}<>").lower()
        if len(cleaned_w) >= 3 and not cleaned_w.isdigit():
            kw_candidates.add(cleaned_w)

    entry = RunbookEntry(
        subsystem=subsystem,
        scenario=resolution.error_summary or f"Incident in {subsystem}",
        root_cause=resolution.root_cause,
        remediation_steps=tuple(resolution.remediation_steps),
        citation=citation,
        keywords=tuple(sorted(list(kw_candidates))[:8]),
        event_type=event.event_type or "*",
    )
    add_runbook_entry(entry, persist=persist, path=path)
    return entry


def resolve_subsystem(subsystem: str) -> str:
    """Normalize and resolve subsystem aliases."""
    cleaned = subsystem.strip().lower()
    return _SUBSYSTEM_ALIASES.get(cleaned, cleaned)


_resolve_subsystem = resolve_subsystem


def _entry_matches_keyword(entry: RunbookEntry, keyword: str) -> bool:
    """Check if a runbook entry matches the given keyword or symptom."""
    kw = keyword.strip().lower()
    if not kw:
        return True

    # Search in all text components
    combined_text = (
        f"{entry.subsystem} {entry.scenario} {entry.root_cause} "
        f"{' '.join(entry.remediation_steps)} {entry.citation} "
        f"{' '.join(entry.keywords)} {entry.event_type}"
    ).lower()

    if kw in combined_text:
        return True

    # Match if all individual tokens appear in the text
    tokens = kw.split()
    if len(tokens) > 1 and all(token in combined_text for token in tokens):
        return True

    return False


def query_subsystem_docs(subsystem: str, keyword: str = "") -> str:
    """Query authoritative LANL HPC subsystem documentation and runbooks.

    Args:
        subsystem: The HPC subsystem name (e.g., 'clusterfilesystem',
            'resourcemgmtdaeomon', 'unix.hw', 'switch_module', 'boot_cmd').
        keyword: Optional search keyword or symptom (e.g., 'panic', 'err = 10',
            'alt0', 'PSU', 'CMF-Port', 'timeout') to narrow results.

    Returns:
        Structured runbook excerpt including root causes, actionable remediation
        steps, and authoritative citations.
    """
    resolved = _resolve_subsystem(subsystem)

    # Filter all entries for the resolved subsystem
    subsystem_entries = [e for e in _RUNBOOKS if e.subsystem == resolved]

    if not subsystem_entries:
        known_list = "\n".join(f"- {s}" for s in KNOWN_SUBSYSTEMS)
        return (
            f"Unknown subsystem '{subsystem}'. Available subsystems in LANL HPC knowledge base:\n"
            f"{known_list}"
        )

    # If keyword is provided, filter matching entries
    if keyword.strip():
        matching = [e for e in subsystem_entries if _entry_matches_keyword(e, keyword)]
        if matching:
            return "\n\n---\n\n".join(e.format_text() for e in matching)
        # Graceful fallback: return all subsystem runbooks with a note
        header = (
            f"[Note: No specific entry matched keyword '{keyword}' in subsystem '{resolved}'. "
            f"Displaying general runbooks for {resolved}:]\n\n"
        )
        return header + "\n\n---\n\n".join(e.format_text() for e in subsystem_entries)

    # No keyword provided: return all entries for the subsystem
    return "\n\n---\n\n".join(e.format_text() for e in subsystem_entries)
