"""Fast-path Rule Tree engine, dynamic resolution cache, and persistent rule registry."""

import asyncio
import json
import re
from collections import OrderedDict
from pathlib import Path
from typing import Any

from src.rules.models import LogEvent, ProblemResolution, Rule
from src.rules.normalizer import parse_log_line


def extract_template(message: str) -> str:
    """Normalize variable tokens to extract a canonical recurring template."""
    s = message.strip()
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


def _get_fallback_default_rules() -> list[Rule]:
    """Hard-coded fallback rules if rules.json is missing or corrupted."""
    return [
        Rule(
            rule_id="RULE-PSU-01",
            subsystem="switch_module",
            event_type="psu",
            pattern=r"PSU status \( (?:off on|on off) \)",
            human_validated=True,
            validation_status="VALIDATED",
            resolution=ProblemResolution(
                error_summary="PSU failure detected",
                root_cause="Power supply unit degraded or powered off on switch module",
                severity="HIGH",
                remediation_steps=[
                    "Inspect physical switch power module indicator LEDs",
                    "Replace failed PSU unit or reconnect power feed",
                ],
                citations_and_reasons=[
                    "Hardware manual section 4.2: Redundant PSU Operations",
                ],
            ),
        ),
        Rule(
            rule_id="RULE-FS-FULL-01",
            subsystem="clusterfilesystem",
            event_type="fdmn.full",
            pattern=r"ServerFileSystem: ServerFileSystem domain \S+ is full",
            human_validated=True,
            validation_status="VALIDATED",
            resolution=ProblemResolution(
                error_summary="Cluster filesystem domain full",
                root_cause="Storage domain exceeded 100% capacity",
                severity="HIGH",
                remediation_steps=[
                    "Purge expired scratch directories and temporary dumps",
                    "Expand volume quota or allocate additional storage domains",
                ],
                citations_and_reasons=[
                    "LANL Storage Architecture Guide: Sec 8.3 Filesystem Quotas",
                ],
            ),
        ),
        Rule(
            rule_id="RULE-FS-PANIC-01",
            subsystem="clusterfilesystem",
            event_type="fdmn.panic",
            pattern=r"ServerFileSystem: ServerFileSystem domain \S+ is panicked",
            human_validated=True,
            validation_status="VALIDATED",
            resolution=ProblemResolution(
                error_summary="Cluster filesystem domain panic",
                root_cause="Severe filesystem metadata inconsistency or I/O barrier timeout",
                severity="CRITICAL",
                remediation_steps=[
                    "Quarantine and unmount the affected storage domain immediately",
                    "Execute filesystem consistency check (advfsck)",
                    "Restore corrupted metadata from snapshot if unrecoverable",
                ],
                citations_and_reasons=[
                    "LANL Storage Architecture Guide: Sec 8.1 Panic Recovery",
                ],
            ),
        ),
        Rule(
            rule_id="RULE-NET-ALT0-01",
            subsystem="unix.hw",
            event_type="*",
            pattern=r"(?:interface alt0 is down|Component \S*alt0\S* is in the unavailable state)",
            human_validated=True,
            validation_status="VALIDATED",
            resolution=ProblemResolution(
                error_summary="Network interface alt0 unavailable",
                root_cause="Physical network link loss or transceiver failure on alt0",
                severity="HIGH",
                remediation_steps=[
                    "Verify physical Ethernet/InfiniBand patch cord and switch port",
                    "Restart interface via `ifconfig alt0 down && ifconfig alt0 up`",
                    "Reseat optical transceiver if link does not recover",
                ],
                citations_and_reasons=[
                    "LANL Unix Hardware Runbook: Sec 3.1 Interface Diagnostics",
                ],
            ),
        ),
        Rule(
            rule_id="RULE-RESMGMT-ERR10-01",
            subsystem="resourcemgmtdaeomon",
            event_type="*",
            pattern=r"failed to configure resourcemgmt subsystem err = 10",
            human_validated=True,
            validation_status="VALIDATED",
            resolution=ProblemResolution(
                error_summary="Resource manager daemon initialization failure",
                root_cause="Subsystem configuration error code 10 during daemon startup",
                severity="CRITICAL",
                remediation_steps=[
                    "Check node controller connectivity and verify daemon configuration file",
                    "Restart resourcemgmtdaeomon service",
                ],
                citations_and_reasons=[
                    "LANL Resource Management Architecture Manual: Sec 5.2",
                ],
            ),
        ),
    ]


def get_default_rules(path: Path | str | None = "rules/rules.json") -> list[Rule]:
    """Return standard LANL HPC failure resolution rules from rules file, or fallback list."""
    if path:
        p = Path(path)
        if p.is_file():
            try:
                with open(p, encoding="utf-8") as f:
                    data = json.load(f)
                return [Rule.model_validate(item) for item in data]
            except Exception:
                pass
    return _get_fallback_default_rules()


class RuleTreeEngine:
    """Fast-path hierarchical rule tree engine, dynamic cache, and persistent rule registry."""

    def __init__(
        self,
        rules_path: Path | str | None = None,
        load_defaults: bool = False,
        max_cache_size: int = 10_000,
        benign_cases: list[str] | None = None,
    ) -> None:
        self.rules_path: Path | None = Path(rules_path) if rules_path else None
        self._max_cache_size: int = max_cache_size
        self._benign_cases: list[str] | None = benign_cases

        # Primary registry of all known rules: rule_id -> Rule
        self._rules: dict[str, Rule] = {}
        # Active tree index for fast-path: (subsystem, event_type) -> list[Rule]
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
    ) -> "RuleTreeEngine":
        """Instantiate RuleTreeEngine pre-loaded with persistent or default rules."""
        engine = cls(
            rules_path=rules_path,
            max_cache_size=max_cache_size,
            benign_cases=benign_cases,
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
        rules = (
            get_default_rules(self.rules_path) if self.rules_path else _get_fallback_default_rules()
        )
        for rule in rules:
            self.add_rule(rule, persist=False)

    def clear_rules(self) -> None:
        """Clear all indexed and registered rules."""
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
        """Register a rule into the registry and tree index (unless REJECTED)."""
        self._rules[rule.rule_id] = rule

        # Only index in matching tree if not REJECTED
        if rule.validation_status != "REJECTED":
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

    def add_rules(self, rules: list[Rule], persist: bool = True) -> None:
        """Register multiple rules into registry and tree index."""
        for rule in rules:
            self.add_rule(rule, persist=False)
        if persist and self.rules_path:
            self.save_rules_to_file()

    def remove_rule_from_tree(self, rule_id: str) -> None:
        """Remove a rule from active matching tree (e.g., when validation rejects it)."""
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

    def create_rule_from_resolution(
        self,
        event: LogEvent | str,
        resolution: ProblemResolution,
        rule_id: str | None = None,
        auto_validate_async: bool = True,
    ) -> Rule:
        """Create a new rule from a diagnosed event with PENDING validation state and persist."""
        if isinstance(event, str):
            event = parse_log_line(event)

        sub = event.subsystem.strip().lower()
        ev_type = event.event_type.strip().lower()
        msg = event.message.strip()
        template = extract_template(msg)
        pattern = template_to_regex(template)

        rid = rule_id or f"RULE-AUTO-{abs(hash(template)) % 1_000_000:06d}"
        rule = Rule(
            rule_id=rid,
            subsystem=sub if sub else "*",
            event_type=ev_type if ev_type else "*",
            pattern=pattern,
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
                # No running event loop in current context
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
            event = parse_log_line(event)

        msg = event.message.strip()
        if not msg:
            return None

        sub = event.subsystem.strip().lower()
        ev_type = event.event_type.strip().lower()

        # 1. Evaluate pre-configured rules via hierarchical tree
        candidates = self._get_candidate_rules(sub, ev_type)
        for rule in candidates:
            if rule.validation_status != "REJECTED" and rule.matches(msg):
                return rule

        # Fallback search across all rules if subsystem is empty
        if not sub:
            for rules in self._tree.values():
                for rule in rules:
                    if rule.validation_status != "REJECTED" and rule.matches(msg):
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
