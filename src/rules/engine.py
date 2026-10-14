"""Fast-path Rule Tree engine, dynamic resolution cache, and persistent rule registry."""

import asyncio
import json
import re
from collections import OrderedDict
from pathlib import Path
from typing import Any

from src.rules.models import LogEvent, ParentEnvelope, ProblemResolution, Rule
from src.rules.normalizer import parse_log_line


def extract_template(message: str) -> str:
    """Normalize variable tokens to extract a canonical recurring template."""
    s = message.strip()
    # Normalize quoted literals or escaped-quote strings (e.g. "param", \042token\042)
    s = re.sub(r'\\042[^\\]*\\042|"[^"]*"', '"<PARAM>"', s)
    # Normalize hex addresses (e.g., 0xdeadbeef, 0x12)
    s = re.sub(r"\b0x[0-9a-fA-F]+\b", "<HEX>", s)
    # Normalize IP addresses
    s = re.sub(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b", "<IP>", s)
    # Normalize hardware IDs (e.g., HWID=1328)
    s = re.sub(r"\bHWID=\d+\b", "HWID=<ID>", s)
    # Normalize node references (e.g., node-25, node-104)
    s = re.sub(r"\bnode-\w+\b", "<NODE>", s)
    # Normalize storage/domain names with numbers (e.g., storage1034 -> storage<NUM>)
    s = re.sub(r"\b(storage|domain|vol)\d+\b", r"\1<NUM>", s, flags=re.IGNORECASE)
    # Normalize generic numbers (integers or error codes)
    s = re.sub(r"\b\d+\b", "<NUM>", s)
    # Collapse multiple whitespaces
    s = re.sub(r"\s+", " ", s).strip()
    return s


def template_to_regex(template: str) -> str:
    """Convert a normalized template with <NUM>, <HEX>, etc. into a safe regex pattern."""
    tokens = {
        "<HEX>": r"0x[0-9a-fA-F]+",
        "<IP>": r"\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}",
        "<ID>": r"\d+",
        "<NODE>": r"node-\w+",
        "<NUM>": r"\d+",
        "<PARAM>": r'[^"\\]+',
    }
    parts = template.split()
    rx_parts: list[str] = []
    for part in parts:
        if part in tokens:
            rx_parts.append(tokens[part])
        else:
            sub = re.escape(part)
            for tok, rx in tokens.items():
                sub = sub.replace(re.escape(tok), rx)
            sub = sub.replace('"', r'(?:\\042|")')
            rx_parts.append(sub)
    return r"\s+".join(rx_parts)


def get_default_benign_cases(benchmark_path: str = "data/golden_eval.json") -> list[str]:
    """Return collection of known benign HPC telemetry cases for regression validation."""
    cases: list[str] = [
        "Fan speeds ( 3552 3534 3375 3500 3515 3479 )",
        "ambient = 28",
        "ambient=30",
        "link ok",
        "status running",
        "normal",
        "responding",
    ]
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


DEFAULT_RULES_PATHS: list[Path] = [
    Path("resources/rules.json"),
    Path(__file__).resolve().parent.parent.parent / "resources" / "rules.json",
    Path("rules/rules.json"),
    Path(__file__).resolve().parent.parent.parent / "rules" / "rules.json",
]

DEFAULT_ENVELOPE_PATHS: list[Path] = [
    Path("resources/log_templates.json"),
    Path(__file__).resolve().parent.parent.parent / "resources" / "log_templates.json",
]


def get_default_envelopes(
    path: Path | str | None = "resources/log_templates.json",
) -> list[ParentEnvelope]:
    """Return standard parent envelope templates from log_templates.json."""
    candidate_paths: list[Path] = []
    if path:
        candidate_paths.append(Path(path))
    for default_p in DEFAULT_ENVELOPE_PATHS:
        if default_p not in candidate_paths:
            candidate_paths.append(default_p)

    for p in candidate_paths:
        if p.is_file():
            try:
                with open(p, encoding="utf-8") as f:
                    data = json.load(f)
                return [
                    ParentEnvelope(
                        parent_id=item["template_id"],
                        envelope_regex=item["regex"],
                        description=item.get("description", ""),
                    )
                    for item in data
                ]
            except Exception:
                pass
    return []


def get_default_rules(path: Path | str | None = "rules/rules.json") -> list[Rule]:
    """Return standard LANL HPC failure resolution rules from rules file."""
    candidate_paths: list[Path] = []
    if path:
        candidate_paths.append(Path(path))
    for default_p in DEFAULT_RULES_PATHS:
        if default_p not in candidate_paths:
            candidate_paths.append(default_p)

    for p in candidate_paths:
        if p.is_file():
            try:
                with open(p, encoding="utf-8") as f:
                    data = json.load(f)
                return [Rule.model_validate(item) for item in data]
            except Exception:
                pass
    return []


class RuleTreeEngine:
    """Fast-path Regex Tree engine (Parent Envelope -> Child Message Rules) and dynamic cache."""

    def __init__(
        self,
        rules_path: Path | str | None = None,
        load_defaults: bool = False,
        max_cache_size: int = 10_000,
        benign_cases: list[str] | None = None,
        envelopes_path: Path | str | None = "resources/log_templates.json",
    ) -> None:
        self.rules_path: Path | None = Path(rules_path) if rules_path else None
        self.envelopes_path: Path | None = Path(envelopes_path) if envelopes_path else None
        self._max_cache_size: int = max_cache_size
        self._benign_cases: list[str] | None = benign_cases

        # Parent envelopes (Regex Tree root nodes)
        self._envelopes: list[ParentEnvelope] = []
        # Primary registry of all known rules: rule_id -> Rule
        self._rules: dict[str, Rule] = {}
        # Common / universal rules that match across any envelope
        self._common_rules: list[Rule] = []
        # Active tree index for (subsystem, event_type) fallback compatibility
        self._tree: dict[tuple[str, str], list[Rule]] = {}
        # Dynamic exact cache: (subsystem, message) -> ProblemResolution
        self._exact_cache: OrderedDict[tuple[str, str], ProblemResolution] = OrderedDict()
        # Dynamic template cache: (subsystem, template) -> ProblemResolution
        self._template_cache: OrderedDict[tuple[str, str], ProblemResolution] = OrderedDict()
        # Message-only fallback cache for logs with empty/missing subsystems
        self._exact_msg_cache: OrderedDict[str, ProblemResolution] = OrderedDict()
        self._template_msg_cache: OrderedDict[str, ProblemResolution] = OrderedDict()

        # In-flight async validation tasks: rule_id -> asyncio.Task[bool]
        self._validation_tasks: dict[str, asyncio.Task[bool]] = {}

        # Load standard parent envelopes
        self.load_default_envelopes(self.envelopes_path)

        if self.rules_path and self.rules_path.is_file():
            self.load_rules_from_file(self.rules_path)
        elif load_defaults:
            self.load_default_rules()

    @classmethod
    def with_defaults(
        cls,
        rules_path: Path | str | None = "rules/rules.json",
        max_cache_size: int = 10_000,
        benign_cases: list[str] | None = None,
        envelopes_path: Path | str | None = "resources/log_templates.json",
    ) -> "RuleTreeEngine":
        """Instantiate RuleTreeEngine pre-loaded with persistent or default rules."""
        engine = cls(
            rules_path=rules_path,
            max_cache_size=max_cache_size,
            benign_cases=benign_cases,
            envelopes_path=envelopes_path,
        )
        if not engine._rules:
            engine.load_default_rules()
            if engine.rules_path:
                engine.save_rules_to_file()
        return engine

    @property
    def rule_count(self) -> int:
        """Return total number of rules currently indexed in the tree."""
        return sum(len(rules) for rules in self._tree.values())

    @property
    def total_registered_rules(self) -> int:
        """Return total number of registered rules in the persistent registry."""
        return len(self._rules)

    @property
    def rules(self) -> list[Rule]:
        """Return list of all rules in the persistent registry."""
        return list(self._rules.values())

    @property
    def envelopes(self) -> list[ParentEnvelope]:
        """Return list of all registered parent envelopes."""
        return list(self._envelopes)

    def load_default_envelopes(self, path: Path | str | None = None) -> list[ParentEnvelope]:
        """Load standard cluster parent envelopes into the Regex Tree."""
        target = path or self.envelopes_path
        envelopes = get_default_envelopes(target)
        for env in envelopes:
            self.add_envelope(env)
        return envelopes

    def add_envelope(self, envelope: ParentEnvelope) -> None:
        """Register a parent envelope into the Regex Tree."""
        existing_idx = next(
            (i for i, e in enumerate(self._envelopes) if e.parent_id == envelope.parent_id),
            None,
        )
        if existing_idx is not None:
            self._envelopes[existing_idx] = envelope
        else:
            self._envelopes.append(envelope)

    @property
    def cache_size(self) -> int:
        """Return total number of exact cached resolutions."""
        return len(self._exact_cache)

    @property
    def template_cache_size(self) -> int:
        """Return total number of template cached resolutions."""
        return len(self._template_cache)

    def get_rule(self, rule_id: str) -> Rule | None:
        """Fetch registered rule by its unique ID."""
        return self._rules.get(rule_id)

    def load_rules_from_file(self, path: Path | str) -> list[Rule]:
        """Load rules from a JSON file into registry and tree index."""
        file_path = Path(path)
        if not file_path.is_file():
            return []
        with open(file_path, encoding="utf-8") as f:
            data = json.load(f)
        rules = [Rule.model_validate(item) for item in data]
        for rule in rules:
            self.add_rule(rule, persist=False)
        return rules

    def save_rules_to_file(self, target_path: Path | str | None = None) -> None:
        """Persist registered rules to the JSON file."""
        dest = Path(target_path) if target_path else self.rules_path
        if not dest:
            return
        dest.parent.mkdir(parents=True, exist_ok=True)
        data = [rule.model_dump(mode="json") for rule in self._rules.values()]
        with open(dest, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

    def load_default_rules(self) -> None:
        """Load standard LANL HPC failure rules into the tree."""
        rules = get_default_rules(self.rules_path)
        for rule in rules:
            self.add_rule(rule, persist=False)

    def clear_rules(self) -> None:
        """Clear all indexed and registered rules."""
        for env in self._envelopes:
            env.children.clear()
        self._common_rules.clear()
        self._tree.clear()
        self._rules.clear()

    def clear_cache(self) -> None:
        """Clear all dynamic resolution caches."""
        self._exact_cache.clear()
        self._template_cache.clear()
        self._exact_msg_cache.clear()
        self._template_msg_cache.clear()

    def _store_cache_entry(
        self,
        cache: OrderedDict[Any, ProblemResolution],
        key: Any,
        value: ProblemResolution,
    ) -> None:
        """Store key-value pair in LRU cache with capacity ceiling."""
        if key in cache:
            cache.move_to_end(key)
        cache[key] = value
        if len(cache) > self._max_cache_size:
            cache.popitem(last=False)

    def _get_cache_entry(
        self,
        cache: OrderedDict[Any, ProblemResolution],
        key: Any,
    ) -> ProblemResolution | None:
        """Retrieve key from LRU cache, updating LRU order if found."""
        if key in cache:
            cache.move_to_end(key)
            return cache[key]
        return None

    def add_rule(self, rule: Rule, persist: bool = True) -> None:
        """Register a rule into the registry and attach to its parent envelope or common pool."""
        self._rules[rule.rule_id] = rule

        # Only index in matching tree if not REJECTED
        if rule.validation_status != "REJECTED":
            # 1. Attach to parent envelope if specified
            if rule.parent_id and rule.parent_id != "*":
                env = next((e for e in self._envelopes if e.parent_id == rule.parent_id), None)
                if env is not None:
                    existing_idx = next(
                        (i for i, r in enumerate(env.children) if r.rule_id == rule.rule_id),
                        None,
                    )
                    if existing_idx is not None:
                        env.children[existing_idx] = rule
                    else:
                        env.children.append(rule)
                else:
                    self._add_to_common(rule)
            else:
                self._add_to_common(rule)

            # 2. Also index in (subsystem, event_type) for backwards-compatible candidate lookups
            sub = rule.subsystem.strip().lower()
            ev_type = rule.event_type.strip().lower()
            key = (sub, ev_type)
            if key not in self._tree:
                self._tree[key] = []
            existing_idx = next(
                (i for i, r in enumerate(self._tree[key]) if r.rule_id == rule.rule_id),
                None,
            )
            if existing_idx is not None:
                self._tree[key][existing_idx] = rule
            else:
                self._tree[key].append(rule)

        if persist and self.rules_path:
            self.save_rules_to_file()

    def _add_to_common(self, rule: Rule) -> None:
        """Add rule to common rules pool matching across envelopes."""
        existing_idx = next(
            (i for i, r in enumerate(self._common_rules) if r.rule_id == rule.rule_id),
            None,
        )
        if existing_idx is not None:
            self._common_rules[existing_idx] = rule
        else:
            self._common_rules.append(rule)

    def add_rules(self, rules: list[Rule], persist: bool = True) -> None:
        """Register multiple rules into registry and tree index."""
        for rule in rules:
            self.add_rule(rule, persist=False)
        if persist and self.rules_path:
            self.save_rules_to_file()

    def remove_rule_from_tree(self, rule_id: str) -> None:
        """Remove a rule from active matching tree (e.g., when validation rejects it)."""
        for env in self._envelopes:
            env.children = [r for r in env.children if r.rule_id != rule_id]
        self._common_rules = [r for r in self._common_rules if r.rule_id != rule_id]
        for key in list(self._tree.keys()):
            self._tree[key] = [r for r in self._tree[key] if r.rule_id != rule_id]

    def _evict_from_caches(self, rule: Rule) -> None:
        """Evict cached resolutions associated with a rejected rule."""
        for k, res in list(self._template_cache.items()):
            if res == rule.resolution:
                self._template_cache.pop(k, None)
        for k, res in list(self._exact_cache.items()):
            if res == rule.resolution:
                self._exact_cache.pop(k, None)
        for k, res in list(self._template_msg_cache.items()):
            if res == rule.resolution:
                self._template_msg_cache.pop(k, None)
        for k, res in list(self._exact_msg_cache.items()):
            if res == rule.resolution:
                self._exact_msg_cache.pop(k, None)

    def validate_rule_against_benign(
        self,
        rule: Rule,
        benign_cases: list[str] | None = None,
    ) -> bool:
        """Deterministically check that the rule pattern does not match benign telemetry.

        Returns True if zero false positives (safe), False if pattern matches a benign case.
        """
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
            try:
                parsed = parse_log_line(case)
                if parsed.message and rule.matches(parsed.message):
                    return False
            except Exception:
                pass
        return True

    async def validate_rule_async(
        self,
        rule: Rule,
        benign_cases: list[str] | None = None,
    ) -> bool:
        """Asynchronously validate rule against benign cases and update status non-blockingly."""
        await asyncio.sleep(0)  # yield control so calling coroutine continues immediately
        is_valid = self.validate_rule_against_benign(rule, benign_cases)
        if is_valid:
            rule.validation_status = "VALIDATED"
        else:
            rule.validation_status = "REJECTED"
            self.remove_rule_from_tree(rule.rule_id)
            self._evict_from_caches(rule)

        if self.rules_path:
            self.save_rules_to_file()

        return is_valid

    def match_raw(self, raw_line: str) -> tuple[LogEvent, Rule | None]:
        """Process a raw log line through the Regex Tree (Parent Envelope -> Child Rule)."""
        line = raw_line.strip()

        for env in self._envelopes:
            m = env.match(line)
            if m:
                data = m.groupdict()
                msg = data.get("message", "").strip()

                ts = 0
                if "timestamp" in data:
                    try:
                        ts = int(data["timestamp"])
                    except (ValueError, TypeError):
                        ts = 0
                flag = 0
                if "flag" in data:
                    try:
                        flag = int(data["flag"])
                    except (ValueError, TypeError):
                        flag = 0

                event = LogEvent(
                    log_id=str(data.get("log_id", "")),
                    node=str(data.get("node", "")),
                    subsystem=str(data.get("subsystem", "")),
                    event_type=str(data.get("event_type", "")),
                    timestamp=ts,
                    flag=flag,
                    message=msg,
                    raw=raw_line,
                    is_unparsed=False,
                    **{
                        k: v
                        for k, v in data.items()
                        if k
                        not in {
                            "log_id",
                            "node",
                            "subsystem",
                            "event_type",
                            "timestamp",
                            "flag",
                            "message",
                        }
                    },
                )

                # 1. Match child rules under this parent envelope
                matched_child = env.match_child(msg)
                if matched_child is not None:
                    return event, matched_child

                # 2. Match common rules across any envelope
                for rule in self._common_rules:
                    if rule.validation_status != "REJECTED" and rule.matches(msg):
                        return event, rule

                # Envelope matched, but message is novel
                return event, None

        # No parent envelope matched: completely novel envelope
        fallback_event = parse_log_line(raw_line)
        fallback_event.is_unparsed = True
        return fallback_event, None

    def create_rule_from_resolution(
        self,
        event: LogEvent | str,
        resolution: ProblemResolution,
        rule_id: str | None = None,
        auto_validate_async: bool = True,
        parent_id: str | None = None,
    ) -> Rule:
        """Create a new rule from a diagnosed event with PENDING validation state and persist."""
        if isinstance(event, str):
            event = parse_log_line(event)

        sub = event.subsystem.strip().lower()
        ev_type = event.event_type.strip().lower()
        msg = event.message.strip()
        raw = event.raw.strip() if hasattr(event, "raw") and event.raw else ""

        pattern = ""
        suggested = (resolution.suggested_log_template_regex or "").strip()
        if suggested:
            try:
                compiled = re.compile(suggested, re.IGNORECASE)
                if (msg and compiled.search(msg)) or (raw and compiled.search(raw)):
                    pattern = suggested
            except re.error:
                pattern = ""

        if not pattern:
            template = extract_template(msg)
            pattern = template_to_regex(template)
        else:
            template = extract_template(msg)

        target_parent = parent_id or "*"
        # If event was unparsed and agent provided a full-line regex with named groups:
        if event.is_unparsed and suggested and "(?P<message>" in suggested:
            new_env_id = f"ENV-{abs(hash(suggested)) % 1_000_000:06d}"
            env = ParentEnvelope(
                parent_id=new_env_id,
                envelope_regex=suggested,
                description=f"Auto-generated envelope for {sub or 'unparsed'}",
            )
            self.add_envelope(env)
            target_parent = new_env_id

        rid = rule_id or f"RULE-AUTO-{abs(hash(pattern)) % 1_000_000:06d}"
        rule = Rule(
            rule_id=rid,
            parent_id=target_parent,
            subsystem=sub if sub else "*",
            event_type=ev_type if ev_type else "*",
            pattern=pattern,
            rule_type="ACTIONABLE_ERROR",
            resolution=resolution,
            human_validated=False,
            validation_status="PENDING",
        )

        # Register rule and auto-dump to file
        self.add_rule(rule, persist=True)

        # Also populate in-memory template/exact cache for instant lookup
        self.cache_resolution(event, resolution)

        # Launch async benign validation in background without blocking
        if auto_validate_async:
            try:
                loop = asyncio.get_running_loop()
                task = loop.create_task(self.validate_rule_async(rule))
                self._validation_tasks[rule.rule_id] = task
            except RuntimeError:
                pass

        return rule

    async def wait_for_validation(self, rule_id: str, timeout: float = 5.0) -> bool:
        """Wait for background async validation task of a specific rule to complete."""
        task = self._validation_tasks.get(rule_id)
        if task is None:
            rule = self.get_rule(rule_id)
            return rule.validation_status == "VALIDATED" if rule else False
        return await asyncio.wait_for(task, timeout=timeout)

    async def wait_for_all_validations(self, timeout: float = 5.0) -> None:
        """Wait for all pending background validation tasks to complete."""
        tasks = [t for t in self._validation_tasks.values() if not t.done()]
        if tasks:
            await asyncio.wait_for(
                asyncio.gather(*tasks, return_exceptions=True),
                timeout=timeout,
            )

    def cache_resolution(
        self,
        event: LogEvent | str,
        resolution: ProblemResolution,
    ) -> None:
        """Dynamically cache a resolution for future occurrences to bypass LLM."""
        if isinstance(event, str):
            event = parse_log_line(event)

        sub = event.subsystem.strip().lower()
        msg = event.message.strip()
        if not msg:
            return

        template = extract_template(msg)

        # Store in exact cache
        self._store_cache_entry(self._exact_cache, (sub, msg), resolution)
        self._store_cache_entry(self._exact_msg_cache, msg, resolution)

        # Store in template cache
        self._store_cache_entry(self._template_cache, (sub, template), resolution)
        self._store_cache_entry(self._template_msg_cache, template, resolution)

        # If subsystem was unspecified or wildcard, cache under wildcard
        if not sub or sub == "*":
            self._store_cache_entry(self._exact_cache, ("*", msg), resolution)
            self._store_cache_entry(self._template_cache, ("*", template), resolution)

    def _get_candidate_rules(self, sub: str, ev_type: str) -> list[Rule]:
        """Fetch candidate rules in order of specificity."""
        candidates: list[Rule] = []

        # 1. Exact (subsystem, event_type)
        if (sub, ev_type) in self._tree:
            candidates.extend(self._tree[(sub, ev_type)])

        # 2. Subsystem wildcard event_type (subsystem, "*")
        if ev_type != "*" and (sub, "*") in self._tree:
            candidates.extend(self._tree[(sub, "*")])

        # 3. Global subsystem, exact event_type ("*", ev_type)
        if sub != "*" and ("*", ev_type) in self._tree:
            candidates.extend(self._tree[("*", ev_type)])

        # 4. Universal wildcard ("*", "*")
        if (sub != "*" or ev_type != "*") and ("*", "*") in self._tree:
            candidates.extend(self._tree[("*", "*")])

        return candidates

    def match_rule(self, event: LogEvent | str) -> Rule | None:
        """Match event against indexed rules, skipping REJECTED rules.

        Returns matching Rule (which may be VALIDATED or PENDING), or None.
        """
        if isinstance(event, str):
            evt, rule = self.match_raw(event)
            if rule is not None:
                return rule
            event = evt

        msg = event.message.strip()
        raw = event.raw.strip() if hasattr(event, "raw") and event.raw else ""
        if not msg and not raw:
            return None

        # 1. Check parent envelopes' children
        for env in self._envelopes:
            if msg:
                matched = env.match_child(msg)
                if matched is not None:
                    return matched
            if raw:
                matched = env.match_child(raw)
                if matched is not None:
                    return matched

        # 2. Check common rules
        for rule in self._common_rules:
            if rule.validation_status != "REJECTED":
                if (msg and rule.matches(msg)) or (raw and rule.matches(raw)):
                    return rule

        sub = event.subsystem.strip().lower()
        ev_type = event.event_type.strip().lower()

        # 3. Evaluate pre-configured rules via candidate tree
        candidates = self._get_candidate_rules(sub, ev_type)
        for rule in candidates:
            if rule.validation_status != "REJECTED" and (
                rule.matches(msg) or (bool(raw) and rule.matches(raw))
            ):
                return rule

        # Fallback search across all rules if subsystem is empty
        if not sub:
            for rules in self._tree.values():
                for rule in rules:
                    if rule.validation_status != "REJECTED" and (
                        rule.matches(msg) or (bool(raw) and rule.matches(raw))
                    ):
                        return rule

        return None

    def match(self, event: LogEvent | str) -> ProblemResolution | None:
        """Resolve an event via pre-configured rules or dynamic resolution cache.

        Returns ProblemResolution if matched, or None if novel/unrecognized.
        """
        rule = self.match_rule(event)
        if rule is not None:
            return rule.resolution

        if isinstance(event, str):
            event = parse_log_line(event)

        msg = event.message.strip()
        if not msg:
            return None

        sub = event.subsystem.strip().lower()

        # 2. Check exact dynamic cache (subsystem + message)
        res = self._get_cache_entry(self._exact_cache, (sub, msg))
        if res is not None:
            return res

        res = self._get_cache_entry(self._exact_cache, ("*", msg))
        if res is not None:
            return res

        # 3. Check template dynamic cache (subsystem + template)
        template = extract_template(msg)
        res = self._get_cache_entry(self._template_cache, (sub, template))
        if res is not None:
            return res

        res = self._get_cache_entry(self._template_cache, ("*", template))
        if res is not None:
            return res

        # 4. Message-only fallback caches (only evaluated when subsystem is empty/missing)
        if not sub:
            res = self._get_cache_entry(self._exact_msg_cache, msg)
            if res is not None:
                return res

            res = self._get_cache_entry(self._template_msg_cache, template)
            if res is not None:
                return res

        return None
