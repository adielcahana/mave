"""Template registry for schema-driven HPC log parsing.

Provides combined regex disjunction (DFA) compilation for single-pass O(M) log parsing
with fallback to wire-speed normalizer.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

from src.rules.models import LogEvent
from src.rules.normalizer import parse_log_line

logger = logging.getLogger(__name__)

DEFAULT_LOG_TEMPLATES_PATH = Path("resources/log_templates.json")


class LogTemplateRegistry:
    """Registry of log templates compiled into a combined regex disjunction.

    Pros and Cons of the Combined Regex Disjunction:
    Pros:
      - Single-pass O(M) regex scan through DFA; avoids O(N) sequential regex tests.
      - Scales efficiently across dozens of evolving templates without linear scanning latency.
      - Named branch matches template instantly, mapping tokens directly to LogEvent attributes.
    Cons:
      - Large combined compiled pattern can be complex to inspect or debug.
      - Branch order precedence requires placing specific patterns before general/catch-all ones.
      - Recompilation of combined regex needed on template addition (amortized cost).
    """

    def __init__(self, templates_path: str | Path | None = None) -> None:
        """Initialize the template registry.

        Args:
            templates_path: Path to the log templates JSON file. If None or not found,
                initializes with default path or empty list.
        """
        self.templates_path: Path = (
            Path(templates_path) if templates_path else DEFAULT_LOG_TEMPLATES_PATH
        )
        self.templates: list[dict[str, Any]] = []
        self._compiled_regex: re.Pattern[str] | None = None
        self._branch_map: dict[str, dict[str, Any]] = {}
        self.load_templates()

    def load_templates(self) -> None:
        """Load templates from configured templates_path and compile disjunction."""
        if self.templates_path.is_file():
            try:
                with open(self.templates_path, encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, list):
                    self.templates = data
            except Exception as e:
                logger.warning("Failed to load log templates from %s: %s", self.templates_path, e)
                self.templates = []
        else:
            self.templates = []

        self._compile_disjunction()

    def _compile_disjunction(self) -> None:
        """Compile all registered templates into a single combined regex disjunction."""
        if not self.templates:
            self._compiled_regex = None
            self._branch_map = {}
            return

        branches: list[str] = []
        self._branch_map = {}

        for idx, item in enumerate(self.templates):
            branch_id = f"T_{idx}"
            raw_regex = item.get("regex", "").strip()
            if not raw_regex:
                continue

            # In Python regex, you cannot have duplicate group names across branches
            # in (?P<name>...). To allow each branch template to have its own (?P<subsystem>...),
            # we rewrite group names by prefixing with branch_id:
            # e.g. (?P<subsystem> -> (?P<T_0__subsystem>
            # Then at parse time, we extract keys starting with f"{branch_id}__".
            def _prefix_group(match: re.Match[str], b_id: str = branch_id) -> str:
                grp_name = match.group(1)
                return f"(?P<{b_id}__{grp_name}>"

            prefixed_regex = re.sub(r"\(\?P<([a-zA-Z_][a-zA-Z0-9_]*)>", _prefix_group, raw_regex)
            branches.append(f"(?P<{branch_id}>{prefixed_regex})")
            self._branch_map[branch_id] = item

        if not branches:
            self._compiled_regex = None
            return

        combined_pattern = "|".join(branches)
        self._compiled_regex = re.compile(combined_pattern)

    def parse(self, raw_line: str) -> LogEvent:
        """Parse a raw log line using combined regex disjunction or fallback.

        Tries combined regex first. If matched, populates LogEvent fields with is_unparsed=False.
        If no template matches, calls parse_log_line from normalizer.py as fallback and sets
        event.is_unparsed=True.
        """
        if self._compiled_regex is not None:
            match = self._compiled_regex.match(raw_line)
            if match is not None:
                # Find which branch matched
                matched_branch_id: str | None = None
                for branch_id in self._branch_map:
                    if match.group(matched_branch_id or branch_id) is not None:
                        matched_branch_id = branch_id
                        break

                if matched_branch_id is not None:
                    prefix = f"{matched_branch_id}__"
                    extracted: dict[str, Any] = {}
                    for group_name, val in match.groupdict().items():
                        if group_name.startswith(prefix) and val is not None:
                            field_name = group_name[len(prefix) :]
                            extracted[field_name] = val

                    # Cast numeric fields
                    timestamp = 0
                    if "timestamp" in extracted:
                        try:
                            timestamp = int(float(extracted["timestamp"]))
                        except ValueError:
                            timestamp = 0

                    flag = 0
                    if "flag" in extracted:
                        try:
                            flag = int(extracted["flag"])
                        except ValueError:
                            flag = 0

                    return LogEvent(
                        log_id=str(extracted.get("log_id", "")),
                        node=str(extracted.get("node", "")),
                        subsystem=str(extracted.get("subsystem", "")),
                        event_type=str(extracted.get("event_type", "")),
                        timestamp=timestamp,
                        flag=flag,
                        message=str(extracted.get("message", "")),
                        raw=raw_line,
                        is_unparsed=False,
                    )

        # Fallback to wire-speed normalizer
        event = parse_log_line(raw_line)
        event.is_unparsed = True
        return event

    def register_template(self, template_id: str, regex: str, persist: bool = True) -> None:
        """Register a new template dynamically and immediately recompile.

        Appends new template to in-memory list, immediately recompiles the combined
        regex so it is available immediately, and optionally writes to the json file.
        """
        new_entry = {
            "template_id": template_id,
            "regex": regex,
        }
        # Prepend or append? The user requirements say:
        # "Appends new template to in-memory list, immediately recompiles the combined regex"
        # However, if it's more specific or added dynamically, let's append it as requested.
        self.templates.append(new_entry)
        self._compile_disjunction()

        if persist:
            try:
                self.templates_path.parent.mkdir(parents=True, exist_ok=True)
                with open(self.templates_path, "w", encoding="utf-8") as f:
                    json.dump(self.templates, f, indent=2)
            except Exception as e:
                logger.warning("Failed to persist templates to %s: %s", self.templates_path, e)
