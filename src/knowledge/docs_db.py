"""Authoritative cluster knowledge base and runbooks."""

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
    "load_runbooks",
    "save_runbooks",
]


@dataclass(frozen=True)
class RunbookEntry:
    """Individual failure mode runbook entry for an HPC cluster."""

    scenario: str
    root_cause: str
    remediation_steps: tuple[str, ...]
    citation: str
    keywords: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.remediation_steps, tuple):
            object.__setattr__(self, "remediation_steps", tuple(self.remediation_steps))
        if not isinstance(self.keywords, tuple):
            object.__setattr__(self, "keywords", tuple(self.keywords))

    def to_dict(self) -> dict[str, Any]:
        """Convert entry to JSON-serializable dictionary."""
        return {
            "scenario": self.scenario,
            "root_cause": self.root_cause,
            "remediation_steps": list(self.remediation_steps),
            "citation": self.citation,
            "keywords": list(self.keywords),
        }

    def format_text(self) -> str:
        """Render the runbook entry into standard structured markdown."""
        steps_formatted = "\n".join(
            f"{i + 1}. {step}" for i, step in enumerate(self.remediation_steps)
        )
        return (
            f"### {self.scenario}\n"
            f"**Root Cause:** {self.root_cause}\n"
            f"**Remediation Steps:**\n{steps_formatted}\n"
            f"**Citation:** {self.citation}"
        )


def _resolve_runbooks_path(custom_path: Path | str | None = None) -> Path:
    """Resolve path to runbooks.json, prioritizing explicit path, package root, and cwd."""
    if custom_path is not None:
        return Path(custom_path)

    pkg_path = Path(__file__).resolve().parent.parent.parent / "resources" / "runbooks.json"
    if pkg_path.is_file():
        return pkg_path

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
            scenario=item["scenario"],
            root_cause=item["root_cause"],
            remediation_steps=tuple(item.get("remediation_steps", ())),
            citation=item["citation"],
            keywords=tuple(item.get("keywords", ())),
        )
        for item in data
    ]


_RUNBOOKS: list[RunbookEntry] = load_runbooks()


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
    existing_idx = next(
        (
            i
            for i, e in enumerate(_RUNBOOKS)
            if e.scenario == entry.scenario or e.root_cause == entry.root_cause
        ),
        None,
    )
    if existing_idx is not None:
        _RUNBOOKS[existing_idx] = entry
    else:
        _RUNBOOKS.append(entry)
    if persist:
        save_runbooks(path)


def create_runbook_from_resolution(
    event: LogEvent,
    resolution: ProblemResolution,
    persist: bool = False,
    path: Path | str | None = None,
) -> RunbookEntry:
    """Synthesize a new RunbookEntry from an agent ProblemResolution and register it."""
    citation = (
        resolution.citations_and_reasons[0]
        if resolution.citations_and_reasons
        else "[Learned SRE Runbook]"
    )

    kw_candidates: set[str] = set()
    for val in event.fields.values():
        s_val = str(val).strip()
        if len(s_val) >= 3 and not s_val.isdigit():
            kw_candidates.add(s_val.lower())
    for word in event.message.split():
        cleaned_w = word.strip("=(),.:;\"'[]{}<>").lower()
        if len(cleaned_w) >= 3 and not cleaned_w.isdigit():
            kw_candidates.add(cleaned_w)

    entry = RunbookEntry(
        scenario=resolution.error_summary or "Cluster Incident",
        root_cause=resolution.root_cause,
        remediation_steps=tuple(resolution.remediation_steps),
        citation=citation,
        keywords=tuple(sorted(list(kw_candidates))[:8]),
    )
    add_runbook_entry(entry, persist=persist, path=path)
    return entry
