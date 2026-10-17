"""Authoritative cluster knowledge base and runbooks."""

import hashlib
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
    "get_runbook_by_id",
    "load_runbooks",
    "save_runbooks",
]


def _make_runbook_id(scenario: str) -> str:
    """Generate a deterministic runbook identifier from scenario text."""
    digest = hashlib.sha256(scenario.strip().lower().encode("utf-8")).hexdigest()[:8]
    return f"RB-{digest}"


@dataclass(frozen=True)
class RunbookEntry:
    """Individual failure mode runbook entry for an HPC cluster."""

    scenario: str
    root_cause: str
    remediation_steps: tuple[str, ...]
    citation: str
    keywords: tuple[str, ...] = ()
    runbook_id: str = ""
    severity: str = "HIGH"

    def __post_init__(self) -> None:
        if not isinstance(self.remediation_steps, tuple):
            object.__setattr__(self, "remediation_steps", tuple(self.remediation_steps))
        if not isinstance(self.keywords, tuple):
            object.__setattr__(self, "keywords", tuple(self.keywords))
        if not self.runbook_id:
            object.__setattr__(self, "runbook_id", _make_runbook_id(self.scenario))

    def to_dict(self) -> dict[str, Any]:
        """Convert entry to JSON-serializable dictionary."""
        return {
            "runbook_id": self.runbook_id,
            "scenario": self.scenario,
            "root_cause": self.root_cause,
            "severity": self.severity,
            "remediation_steps": list(self.remediation_steps),
            "citation": self.citation,
            "keywords": list(self.keywords),
        }

    def to_problem_resolution(self) -> ProblemResolution:
        """Build a ProblemResolution instance from this runbook entry."""
        return ProblemResolution(
            error_summary=self.scenario,
            root_cause=self.root_cause,
            severity=self.severity,
            is_benign=False,
            remediation_steps=list(self.remediation_steps),
            citations_and_reasons=[self.citation] if self.citation else [],
        )

    def format_text(self) -> str:
        """Render the runbook entry into standard structured markdown."""
        steps_formatted = "\n".join(
            f"{i + 1}. {step}" for i, step in enumerate(self.remediation_steps)
        )
        return (
            f"### {self.scenario}\n"
            f"**Runbook ID:** {self.runbook_id}\n"
            f"**Root Cause:** {self.root_cause}\n"
            f"**Severity:** {self.severity}\n"
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
            runbook_id=item.get("runbook_id", ""),
            scenario=item["scenario"],
            root_cause=item["root_cause"],
            severity=item.get("severity", "HIGH"),
            remediation_steps=tuple(item.get("remediation_steps", ())),
            citation=item["citation"],
            keywords=tuple(item.get("keywords", ())),
        )
        for item in data
    ]


_RUNBOOKS: list[RunbookEntry] = load_runbooks()
_EPHEMERAL_RUNBOOKS: dict[str, RunbookEntry] = {}


def get_all_runbooks() -> list[RunbookEntry]:
    """Return all documented runbook entries in the knowledge base."""
    return list(_RUNBOOKS)


def get_runbook_by_id(
    runbook_id: str,
    path: Path | str | None = None,
) -> RunbookEntry | None:
    """Look up a RunbookEntry by its runbook_id in memory or from disk."""
    if not runbook_id:
        return None
    for entry in _RUNBOOKS:
        if entry.runbook_id == runbook_id:
            return entry
    if runbook_id in _EPHEMERAL_RUNBOOKS:
        return _EPHEMERAL_RUNBOOKS[runbook_id]
    try:
        for entry in load_runbooks(path):
            if entry.runbook_id == runbook_id:
                return entry
    except Exception:
        pass
    return None


def save_runbooks(
    path: Path | str | None = None,
    entries: list[RunbookEntry] | None = None,
) -> None:
    """Serialize and save runbooks to a JSON file."""
    runbooks_path = _resolve_runbooks_path(path)
    source = entries if entries is not None else _RUNBOOKS
    data = [entry.to_dict() for entry in source]
    runbooks_path.parent.mkdir(parents=True, exist_ok=True)
    with open(runbooks_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def _upsert_into_list(target_list: list[RunbookEntry], entry: RunbookEntry) -> RunbookEntry:
    """Insert or update a RunbookEntry in target_list preserving existing runbook_id."""
    existing_idx = next(
        (
            i
            for i, e in enumerate(target_list)
            if e.runbook_id == entry.runbook_id
            or e.scenario == entry.scenario
            or e.root_cause == entry.root_cause
        ),
        None,
    )
    if existing_idx is not None:
        preserved_id = target_list[existing_idx].runbook_id
        if entry.runbook_id != preserved_id:
            entry = RunbookEntry(
                runbook_id=preserved_id,
                scenario=entry.scenario,
                root_cause=entry.root_cause,
                severity=entry.severity,
                remediation_steps=entry.remediation_steps,
                citation=entry.citation,
                keywords=entry.keywords,
            )
        target_list[existing_idx] = entry
    else:
        target_list.append(entry)
    return entry


def add_runbook_entry(
    entry: RunbookEntry,
    persist: bool = True,
    path: Path | str | None = None,
) -> RunbookEntry:
    """Add a new runbook entry to the knowledge base and persist to disk by default."""
    default_path = _resolve_runbooks_path(None).resolve()
    target_path = _resolve_runbooks_path(path).resolve()

    if not persist:
        _EPHEMERAL_RUNBOOKS[entry.runbook_id] = entry
        return entry

    if target_path != default_path:
        base_entries = load_runbooks(target_path) if target_path.is_file() else list(_RUNBOOKS)
        entry = _upsert_into_list(base_entries, entry)
        _EPHEMERAL_RUNBOOKS[entry.runbook_id] = entry
        save_runbooks(target_path, entries=base_entries)
        return entry

    entry = _upsert_into_list(_RUNBOOKS, entry)
    _EPHEMERAL_RUNBOOKS[entry.runbook_id] = entry
    save_runbooks(default_path, entries=_RUNBOOKS)
    return entry


def create_runbook_from_resolution(
    event: LogEvent,
    resolution: ProblemResolution,
    persist: bool = True,
    path: Path | str | None = None,
) -> RunbookEntry:
    """Synthesize or match a RunbookEntry from an agent ProblemResolution and register it."""
    citation = (
        resolution.citations_and_reasons[0]
        if resolution.citations_and_reasons
        else "[Learned SRE Runbook]"
    )

    for existing in _RUNBOOKS:
        if (
            existing.runbook_id in citation
            or (existing.citation and existing.citation in citation)
            or existing.scenario.lower() == resolution.error_summary.strip().lower()
            or existing.root_cause.lower() == resolution.root_cause.strip().lower()
        ):
            return existing

    kw_candidates: set[str] = set()
    for val in event.fields.values():
        s_val = str(val).strip()
        if len(s_val) >= 3 and not s_val.isdigit():
            kw_candidates.add(s_val.lower())
    for word in event.message.split():
        cleaned_w = word.strip("=(),.:;\"'[]{}<>").lower()
        if len(cleaned_w) >= 3 and not cleaned_w.isdigit():
            kw_candidates.add(cleaned_w)

    scenario = resolution.error_summary or "Cluster Incident"
    entry = RunbookEntry(
        runbook_id=_make_runbook_id(scenario),
        scenario=scenario,
        root_cause=resolution.root_cause,
        severity=resolution.severity or "HIGH",
        remediation_steps=tuple(resolution.remediation_steps),
        citation=citation,
        keywords=tuple(sorted(list(kw_candidates))[:8]),
    )
    return add_runbook_entry(entry, persist=persist, path=path)
