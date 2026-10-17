"""Pure Regex Tree engine (Parent Envelope -> Child Message Rules) and dynamic rule learning."""

import asyncio
import hashlib
import json
import logging
import re
from pathlib import Path
from typing import Any

from src.rules.models import LogEvent, ParentEnvelope, ProblemResolution, Rule, RuleType

logger = logging.getLogger(__name__)

DEFAULT_RULES_PATH = Path("resources/rules.json")


def extract_group_patterns(regex: str) -> dict[str, str]:
    """Extract mapping of named capture groups to their inner pattern from a regex string."""
    results: dict[str, str] = {}
    i = 0
    n = len(regex)
    while i < n:
        if regex[i : i + 4] == "(?P<":
            end_name = regex.find(">", i + 4)
            if end_name != -1:
                name = regex[i + 4 : end_name]
                depth = 1
                j = end_name + 1
                in_escape = False
                in_char_class = False
                while j < n and depth > 0:
                    ch = regex[j]
                    if in_escape:
                        in_escape = False
                    elif ch == "\\":
                        in_escape = True
                    elif ch == "[" and not in_char_class:
                        in_char_class = True
                    elif ch == "]" and in_char_class:
                        in_char_class = False
                    elif not in_char_class:
                        if ch == "(":
                            depth += 1
                        elif ch == ")":
                            depth -= 1
                    j += 1
                if depth == 0:
                    inner_pattern = regex[end_name + 1 : j - 1]
                    results[name] = inner_pattern
                    i = j
                    continue
        i += 1
    return results


def derive_format_string(pattern: re.Pattern[Any], sample_raw_log: str) -> str:
    """Derive a format template reconstructing sample_raw_log from named capture groups."""
    match = pattern.search(sample_raw_log)
    if not match or not pattern.groupindex:
        return sample_raw_log

    spans: list[tuple[int, int, str]] = []
    for name, idx in pattern.groupindex.items():
        span = match.span(idx)
        if span != (-1, -1):
            spans.append((span[0], span[1], name))

    spans.sort(key=lambda x: (x[0], -x[1]))

    filtered_spans: list[tuple[int, int, str]] = []
    last_end = -1
    for start, end, name in spans:
        if start >= last_end:
            filtered_spans.append((start, end, name))
            last_end = end

    res_parts: list[str] = []
    curr = 0
    for start, end, name in filtered_spans:
        res_parts.append(sample_raw_log[curr:start])
        res_parts.append(f"{{{name}}}")
        curr = end
    res_parts.append(sample_raw_log[curr:])

    return "".join(res_parts)


def sanitize_regex(regex: str, sample: str) -> str:
    """Repair common LLM JSON double-escaping in regex patterns so they match sample."""
    cleaned = regex.strip()
    if not cleaned:
        return ""
    try:
        if re.compile(cleaned, re.IGNORECASE).search(sample):
            return cleaned
    except re.error:
        pass

    candidate = re.sub(r"\\\\([dDsSwWbB().+*?\[\]^$|])", r"\\\1", cleaned)
    candidate = candidate.replace(r"\\\\042", r"\\042").replace(r"\\\.042", r"\\042")
    if r"\042" in sample:
        candidate = candidate.replace(r"[^\\042]+", r".+?")
        if r"\"" in candidate or '"' in candidate:
            candidate = (
                candidate.replace(r"[^\"\\s]+", r"\S+?")
                .replace(r"[^\"]+", r".+?")
                .replace(r"\"", r"\\042")
                .replace('"', r"\\042")
            )
    try:
        if re.compile(candidate, re.IGNORECASE).search(sample):
            return candidate
    except re.error:
        pass
    return cleaned


def get_default_benign_cases(benchmark_path: str = "data/golden_eval.json") -> list[str]:
    """Load known benign HPC telemetry cases dynamically from benchmark dataset."""
    cases: list[str] = []
    p = Path(benchmark_path)
    if p.is_file():
        try:
            with open(p, encoding="utf-8") as f:
                items = json.load(f)
            for item in items:
                if not item.get("is_actionable", True):
                    raw = item.get("raw", "")
                    if raw and raw not in cases:
                        cases.append(raw)
        except Exception:
            pass
    return cases


def get_default_envelopes(path: Path | str | None = DEFAULT_RULES_PATH) -> list[ParentEnvelope]:
    """Load hierarchical ParentEnvelope tree (with nested child rules) from resources/rules.json."""
    candidate_paths: list[Path] = []
    if path:
        candidate_paths.append(Path(path))
    pkg_default = Path(__file__).resolve().parent.parent.parent / "resources" / "rules.json"
    for default_p in (DEFAULT_RULES_PATH, pkg_default):
        if default_p not in candidate_paths:
            candidate_paths.append(default_p)

    for p in candidate_paths:
        if p.is_file():
            try:
                with open(p, encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, list):
                    return [ParentEnvelope.model_validate(item) for item in data]
            except Exception as e:
                logger.warning("Failed to load regex tree from %s: %s", p, e)
    return []


class RuleTreeEngine:
    """Two-tier Regex Tree engine (Parent Envelope -> Child Message Rules)."""

    def __init__(
        self,
        rules_path: Path | str | None = None,
        load_defaults: bool = False,
        benign_cases: list[str] | None = None,
    ) -> None:
        self.rules_path: Path | None = Path(rules_path) if rules_path else None
        self._benign_cases: list[str] | None = benign_cases

        self._envelopes: list[ParentEnvelope] = []
        self._compiled_disjunction: re.Pattern[str] | None = None
        self._branch_map: dict[str, ParentEnvelope] = {}
        self._group_patterns: dict[str, str] = {}
        self._group_examples: dict[str, str] = {}
        self._envelope_samples: dict[str, str] = {}
        self._rule_samples: dict[str, str] = {}
        self._validation_tasks: dict[str, asyncio.Task[bool]] = {}

        if self.rules_path and self.rules_path.is_file():
            self.load_from_file(self.rules_path)
        elif load_defaults:
            self.load_defaults()

    @classmethod
    def with_defaults(
        cls,
        rules_path: Path | str | None = DEFAULT_RULES_PATH,
        benign_cases: list[str] | None = None,
    ) -> "RuleTreeEngine":
        """Instantiate RuleTreeEngine pre-loaded with the hierarchical regex tree."""
        engine = cls(rules_path=rules_path, benign_cases=benign_cases)
        if not engine._envelopes:
            engine.load_defaults()
            if engine.rules_path:
                engine.save_to_file()
        return engine

    @property
    def envelopes(self) -> list[ParentEnvelope]:
        """Return all registered parent envelopes."""
        return list(self._envelopes)

    @property
    def rules(self) -> list[Rule]:
        """Return all child rules across all registered parent envelopes."""
        return [rule for env in self._envelopes for rule in env.children]

    @property
    def rule_count(self) -> int:
        """Return total number of active (non-rejected) child rules in the tree."""
        return sum(
            1 for env in self._envelopes for r in env.children if r.validation_status != "REJECTED"
        )

    def _compile_disjunction(self) -> None:
        """Compile all registered parent envelopes into a single-pass regex disjunction."""
        if not self._envelopes:
            self._compiled_disjunction = None
            self._branch_map = {}
            return

        branches: list[str] = []
        self._branch_map = {}

        for idx, env in enumerate(self._envelopes):
            branch_id = f"T_{idx}"
            raw_regex = env.envelope_regex.strip()
            if not raw_regex:
                continue

            patterns = extract_group_patterns(raw_regex)
            for grp, pat in patterns.items():
                if grp not in self._group_patterns or len(pat) > len(self._group_patterns[grp]):
                    self._group_patterns[grp] = pat

            def _prefix_group(match: re.Match[str], b_id: str = branch_id) -> str:
                grp_name = match.group(1)
                return f"(?P<{b_id}__{grp_name}>"

            prefixed_regex = re.sub(r"\(\?P<([a-zA-Z_][a-zA-Z0-9_]*)>", _prefix_group, raw_regex)
            branches.append(f"(?P<{branch_id}>{prefixed_regex})")
            self._branch_map[branch_id] = env

        if not branches:
            self._compiled_disjunction = None
            return

        self._compiled_disjunction = re.compile("|".join(branches))

    def update_group_examples(self, values: dict[str, Any]) -> None:
        """Update representative example values for known capture groups from incoming logs."""
        for k, v in values.items():
            if v is not None and str(v).strip():
                self._group_examples[k] = str(v).strip()

    def get_known_groups_info(self) -> dict[str, dict[str, str]]:
        """Return dictionary of known named groups with their regex pattern and latest example."""
        return {
            key: {
                "pattern": self._group_patterns.get(key, r"\S+"),
                "example": self._group_examples.get(key, ""),
            }
            for key in sorted(self._group_patterns.keys())
        }

    def load_defaults(self) -> list[ParentEnvelope]:
        """Load default hierarchical regex tree from resources/rules.json."""
        envelopes = get_default_envelopes(self.rules_path)
        for env in envelopes:
            self.add_envelope(env, persist=False)
        return envelopes

    def load_from_file(self, path: Path | str) -> list[ParentEnvelope]:
        """Load hierarchical regex tree from a JSON file."""
        file_path = Path(path)
        if not file_path.is_file():
            return []
        with open(file_path, encoding="utf-8") as f:
            data = json.load(f)
        envelopes = [ParentEnvelope.model_validate(item) for item in data]
        for env in envelopes:
            self.add_envelope(env, persist=False)
        return envelopes

    def save_to_file(self, target_path: Path | str | None = None) -> None:
        """Persist the full hierarchical regex tree (envelopes and child rules) to JSON."""
        dest = Path(target_path) if target_path else self.rules_path
        if not dest:
            return
        dest.parent.mkdir(parents=True, exist_ok=True)
        data = [env.model_dump(mode="json") for env in self._envelopes]
        with open(dest, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

    def add_envelope(self, envelope: ParentEnvelope, persist: bool = False) -> None:
        """Register or update a ParentEnvelope in the Regex Tree and recompile disjunction."""
        existing_idx = next(
            (i for i, e in enumerate(self._envelopes) if e.parent_id == envelope.parent_id),
            None,
        )
        if existing_idx is not None:
            self._envelopes[existing_idx] = envelope
        else:
            self._envelopes.append(envelope)

        self._compile_disjunction()
        if persist and self.rules_path:
            self.save_to_file()

    def register_template(
        self,
        template_id: str,
        regex: str,
        format_string: str = "",
        sample_raw_log: str = "",
        persist: bool = True,
    ) -> ParentEnvelope:
        """Register a new parent envelope template dynamically and immediately recompile."""
        if not format_string and sample_raw_log:
            try:
                rx = re.compile(regex)
                format_string = derive_format_string(rx, sample_raw_log)
            except Exception:
                pass

        if sample_raw_log:
            try:
                rx = re.compile(regex)
                m = rx.search(sample_raw_log)
                if m:
                    self.update_group_examples(m.groupdict())
            except Exception:
                pass

        env = ParentEnvelope(
            parent_id=template_id,
            envelope_regex=regex,
            format_string=format_string,
        )
        self.add_envelope(env, persist=persist)
        return env

    def get_envelope(self, parent_id: str) -> ParentEnvelope | None:
        """Fetch a registered ParentEnvelope by its parent_id."""
        return next((e for e in self._envelopes if e.parent_id == parent_id), None)

    def get_rule(self, rule_id: str) -> Rule | None:
        """Fetch a child rule by its rule_id across all envelopes."""
        for env in self._envelopes:
            for rule in env.children:
                if rule.rule_id == rule_id:
                    return rule
        return None

    def add_rule(self, parent_id: str, rule: Rule, persist: bool = True) -> None:
        """Attach a child Rule under the specified ParentEnvelope."""
        env = self.get_envelope(parent_id)
        if env is None:
            raise ValueError(f"ParentEnvelope '{parent_id}' not found in RuleTreeEngine")

        existing_idx = next(
            (i for i, r in enumerate(env.children) if r.rule_id == rule.rule_id),
            None,
        )
        if existing_idx is not None:
            env.children[existing_idx] = rule
        else:
            env.children.append(rule)

        if persist and self.rules_path:
            self.save_to_file()

    def validate_rule_against_benign(
        self,
        rule: Rule,
        benign_cases: list[str] | None = None,
    ) -> bool:
        """Deterministically verify that an actionable rule does not match benign telemetry."""
        if rule.rule_type == "BENIGN_TELEMETRY":
            return True

        cases = (
            benign_cases
            if benign_cases is not None
            else (
                self._benign_cases if self._benign_cases is not None else get_default_benign_cases()
            )
        )
        for case in cases:
            if rule.matches(case):
                return False
            parsed_event, _ = self.match(case)
            if parsed_event.message and rule.matches(parsed_event.message):
                return False
        return True

    async def validate_rule_async(
        self,
        rule: Rule,
        benign_cases: list[str] | None = None,
    ) -> bool:
        """Asynchronously validate rule against benign cases and update validation_status."""
        await asyncio.sleep(0)
        is_valid = self.validate_rule_against_benign(rule, benign_cases)
        rule.validation_status = "VALIDATED" if is_valid else "REJECTED"

        if self.rules_path:
            self.save_to_file()

        return is_valid

    def match(self, raw_line: str) -> tuple[LogEvent, Rule | None]:
        """Parse a raw log line via Parent Envelope disjunction and match Child Rules."""
        line = raw_line.strip()
        if not line:
            return LogEvent(raw=raw_line, message="", is_unparsed=True), None

        if self._compiled_disjunction is not None:
            m = self._compiled_disjunction.search(line)
            if m is not None:
                matched_branch_id: str | None = None
                if m.lastgroup:
                    candidate = m.lastgroup.split("__")[0]
                    if candidate in self._branch_map:
                        matched_branch_id = candidate
                if matched_branch_id is None:
                    for b_id in self._branch_map:
                        if m.group(b_id) is not None:
                            matched_branch_id = b_id
                            break

                if matched_branch_id is not None:
                    env = self._branch_map[matched_branch_id]
                    prefix = f"{matched_branch_id}__"
                    extracted: dict[str, Any] = {
                        group_name[len(prefix) :]: val
                        for group_name, val in m.groupdict().items()
                        if group_name.startswith(prefix) and val is not None
                    }

                    self.update_group_examples(extracted)

                    format_str = env.format_string
                    if not format_str:
                        try:
                            format_str = derive_format_string(env.get_compiled_pattern(), line)
                        except Exception:
                            format_str = ""

                    msg = str(extracted.pop("message", "")).strip() or line
                    extra_fields = {
                        k: v
                        for k, v in extracted.items()
                        if k not in {"raw", "message", "format_string", "is_unparsed", "parent_id"}
                    }

                    event = LogEvent(
                        raw=raw_line,
                        message=msg,
                        format_string=format_str,
                        is_unparsed=False,
                        parent_id=env.parent_id,
                        **extra_fields,
                    )

                    matched_child = env.match_child(msg)
                    return event, matched_child

        return LogEvent(raw=raw_line, message=line, is_unparsed=True), None

    def create_rule_from_resolution(
        self,
        event: LogEvent | str,
        resolution: ProblemResolution,
        rule_id: str | None = None,
        auto_validate_async: bool = True,
        parent_id: str | None = None,
        runbook_id: str | None = None,
    ) -> Rule:
        """Synthesize and deduplicate ParentEnvelope and Child Rule from an agent resolution."""
        if isinstance(event, str):
            event, _ = self.match(event)

        raw = event.raw.strip() or event.message.strip()
        msg = event.message.strip() or raw
        suggested = sanitize_regex(resolution.suggested_log_template_regex or "", raw)

        # Post-facto deduplication: re-match raw against the current tree in case a concurrent
        # worker already registered a matching ParentEnvelope and/or Child Rule.
        current_event, current_rule = self.match(raw)
        if parent_id is None and not current_event.is_unparsed and current_event.parent_id:
            target_parent_id = current_event.parent_id
            msg = current_event.message.strip() or raw
            self._envelope_samples.setdefault(target_parent_id, raw)
            if rule_id is None and current_rule is not None:
                self._rule_samples.setdefault(current_rule.rule_id, msg)
                return current_rule
        else:
            target_parent_id = parent_id or event.parent_id

        if not target_parent_id:
            env_regex = ""
            if suggested:
                try:
                    compiled_env = re.compile(suggested)
                    if compiled_env.search(raw):
                        env_regex = suggested
                except re.error:
                    env_regex = ""

            if not env_regex:
                env_regex = f"^(?P<message>{re.escape(raw)})$"

            env_digest = hashlib.sha256(env_regex.encode()).hexdigest()[:8]
            target_parent_id = parent_id or f"ENV-AUTO-{env_digest}"
            compiled_env = re.compile(env_regex)
            m = compiled_env.search(raw)
            if m:
                self.update_group_examples(m.groupdict())
                if "message" in m.groupdict() and m.group("message") is not None:
                    msg = m.group("message").strip()

            fmt_str = resolution.format_string
            if not fmt_str:
                try:
                    fmt_str = derive_format_string(compiled_env, raw)
                except Exception:
                    fmt_str = ""

            existing_env = self.get_envelope(target_parent_id)
            if existing_env is None:
                # Deduplicate any narrower auto-generated envelopes subsumed by compiled_env
                migrated_children: list[Rule] = []
                remaining_envelopes: list[ParentEnvelope] = []
                for old_env in self._envelopes:
                    old_sample = self._envelope_samples.get(old_env.parent_id)
                    if (
                        old_env.parent_id.startswith("ENV-AUTO-")
                        and old_sample is not None
                        and compiled_env.search(old_sample)
                    ):
                        for child in old_env.children:
                            if all(c.rule_id != child.rule_id for c in migrated_children):
                                migrated_children.append(child)
                        self._envelope_samples.pop(old_env.parent_id, None)
                    else:
                        remaining_envelopes.append(old_env)
                self._envelopes = remaining_envelopes

                new_env = ParentEnvelope(
                    parent_id=target_parent_id,
                    envelope_regex=env_regex,
                    format_string=fmt_str,
                    description="Auto-generated parent envelope",
                    children=migrated_children,
                )
                self.add_envelope(new_env, persist=False)
            self._envelope_samples[target_parent_id] = raw

        # Determine child message regex pattern under target_parent_id
        pattern = ""
        suggested_msg_rx = sanitize_regex(resolution.suggested_message_regex or "", msg)
        if suggested_msg_rx:
            try:
                if re.compile(suggested_msg_rx, re.IGNORECASE).search(msg):
                    pattern = suggested_msg_rx
                    resolution.suggested_message_regex = suggested_msg_rx
            except re.error:
                pattern = ""

        if not pattern and not event.is_unparsed:
            sanitized_msg_rx = sanitize_regex(resolution.suggested_log_template_regex or "", msg)
            if sanitized_msg_rx:
                try:
                    if re.compile(sanitized_msg_rx, re.IGNORECASE).search(msg):
                        pattern = sanitized_msg_rx
                        resolution.suggested_log_template_regex = sanitized_msg_rx
                except re.error:
                    pattern = ""

        if not pattern:
            group_pats = extract_group_patterns(suggested)
            msg_group_pat = group_pats.get("message", "").strip()
            if msg_group_pat and msg_group_pat not in {".*", ".+", r"\S+"}:
                try:
                    if re.compile(msg_group_pat, re.IGNORECASE).search(msg):
                        pattern = msg_group_pat
                except re.error:
                    pass
            elif "message" not in group_pats and suggested:
                try:
                    if re.compile(suggested, re.IGNORECASE).search(msg):
                        pattern = suggested
                except re.error:
                    pass
            if not pattern:
                pattern = re.sub(r"\d+", r"\\d+", re.escape(msg))

        rule_type: RuleType = "BENIGN_TELEMETRY" if resolution.is_benign else "ACTIONABLE_ERROR"
        rule_res = None if resolution.is_benign else resolution
        rule_digest = hashlib.sha256(f"{target_parent_id}:{pattern}".encode()).hexdigest()[:8]
        rid = rule_id or f"RULE-AUTO-{rule_digest}"

        rule = Rule(
            rule_id=rid,
            pattern=pattern,
            rule_type=rule_type,
            runbook_id=None if resolution.is_benign else runbook_id,
            resolution=rule_res,
            human_validated=False,
            validation_status="PENDING",
        )

        # Post-facto child rule deduplication:
        # 1. If an existing child rule already matches msg, reuse it.
        # 2. If the new rule is more general and matches an older auto-rule's sample, replace it.
        env = self.get_envelope(target_parent_id)
        if env is not None:
            if rule_id is None:
                existing_child = env.match_child(msg)
                if existing_child is not None:
                    self._rule_samples.setdefault(existing_child.rule_id, msg)
                    if self.rules_path:
                        self.save_to_file()
                    return existing_child

            retained_children: list[Rule] = []
            for old_r in env.children:
                old_msg = self._rule_samples.get(old_r.rule_id)
                if (
                    old_r.rule_id.startswith("RULE-AUTO-")
                    and old_r.rule_type == rule.rule_type
                    and old_msg is not None
                    and rule.matches(old_msg)
                ):
                    self._rule_samples.pop(old_r.rule_id, None)
                else:
                    retained_children.append(old_r)
            env.children = retained_children

        self._rule_samples[rule.rule_id] = msg
        self.add_rule(target_parent_id, rule, persist=True)

        if auto_validate_async:
            try:
                loop = asyncio.get_running_loop()
                task = loop.create_task(self.validate_rule_async(rule))
                self._validation_tasks[rule.rule_id] = task
                task.add_done_callback(
                    lambda _t, r_id=rule.rule_id: self._validation_tasks.pop(r_id, None)
                )
            except RuntimeError:
                pass

        return rule

    async def wait_for_validation(self, rule_id: str, timeout: float = 5.0) -> bool:
        """Wait for background async validation task of a specific rule to complete."""
        task = self._validation_tasks.get(rule_id)
        if task is not None:
            return await asyncio.wait_for(task, timeout=timeout)
        rule = self.get_rule(rule_id)
        return rule.validation_status == "VALIDATED" if rule else False
