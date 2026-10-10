"""Fast-path Rule Tree engine and dynamic resolution cache."""

import re

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


def get_default_rules() -> list[Rule]:
    """Return standard pre-configured LANL HPC failure resolution rules."""
    return [
        Rule(
            rule_id="RULE-PSU-01",
            subsystem="switch_module",
            event_type="psu",
            pattern=r"PSU status \( (?:off on|on off) \)",
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


class RuleTreeEngine:
    """Fast-path hierarchical rule tree engine and dynamic resolution cache."""

    def __init__(self, load_defaults: bool = False) -> None:
        # Hierarchical tree index: (subsystem, event_type) -> list[Rule]
        self._tree: dict[tuple[str, str], list[Rule]] = {}
        # Dynamic exact cache: (subsystem, message) -> ProblemResolution
        self._exact_cache: dict[tuple[str, str], ProblemResolution] = {}
        # Dynamic template cache: (subsystem, template) -> ProblemResolution
        self._template_cache: dict[tuple[str, str], ProblemResolution] = {}
        # Message-only fallback cache for logs with empty/missing subsystems
        self._exact_msg_cache: dict[str, ProblemResolution] = {}
        self._template_msg_cache: dict[str, ProblemResolution] = {}

        if load_defaults:
            self.load_default_rules()

    @classmethod
    def with_defaults(cls) -> "RuleTreeEngine":
        """Instantiate RuleTreeEngine pre-loaded with standard LANL HPC failure rules."""
        return cls(load_defaults=True)

    @property
    def rule_count(self) -> int:
        """Return total number of rules currently indexed in the tree."""
        return sum(len(rules) for rules in self._tree.values())

    @property
    def cache_size(self) -> int:
        """Return total number of exact cached resolutions."""
        return len(self._exact_cache)

    @property
    def template_cache_size(self) -> int:
        """Return total number of template cached resolutions."""
        return len(self._template_cache)

    def load_default_rules(self) -> None:
        """Load standard LANL HPC failure rules into the tree."""
        for rule in get_default_rules():
            self.add_rule(rule)

    def clear_rules(self) -> None:
        """Clear all indexed rules."""
        self._tree.clear()

    def clear_cache(self) -> None:
        """Clear all dynamic resolution caches."""
        self._exact_cache.clear()
        self._template_cache.clear()
        self._exact_msg_cache.clear()
        self._template_msg_cache.clear()

    def add_rule(self, rule: Rule) -> None:
        """Register a pre-configured rule into the hierarchical index."""
        sub = rule.subsystem.strip().lower()
        ev_type = rule.event_type.strip().lower()
        key = (sub, ev_type)
        if key not in self._tree:
            self._tree[key] = []
        self._tree[key].append(rule)

    def add_rules(self, rules: list[Rule]) -> None:
        """Register multiple rules into the hierarchical index."""
        for rule in rules:
            self.add_rule(rule)

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
        self._exact_cache[(sub, msg)] = resolution
        self._exact_msg_cache[msg] = resolution

        # Store in template cache
        self._template_cache[(sub, template)] = resolution
        self._template_msg_cache[template] = resolution

        # If subsystem was unspecified or wildcard, cache under wildcard
        if not sub or sub == "*":
            self._exact_cache[("*", msg)] = resolution
            self._template_cache[("*", template)] = resolution

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

    def match(self, event: LogEvent | str) -> ProblemResolution | None:
        """Resolve an event via pre-configured rules or dynamic resolution cache.

        Returns ProblemResolution if matched, or None if novel/unrecognized.
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
            if rule.matches(msg):
                return rule.resolution

        # Fallback search across all rules if subsystem is empty
        if not sub:
            for rules in self._tree.values():
                for rule in rules:
                    if rule.matches(msg):
                        return rule.resolution

        # 2. Check exact dynamic cache (subsystem + message)
        if (sub, msg) in self._exact_cache:
            return self._exact_cache[(sub, msg)]

        if ("*", msg) in self._exact_cache:
            return self._exact_cache[("*", msg)]

        # 3. Check template dynamic cache (subsystem + template)
        template = extract_template(msg)
        if (sub, template) in self._template_cache:
            return self._template_cache[(sub, template)]

        if ("*", template) in self._template_cache:
            return self._template_cache[("*", template)]

        # 4. Message-only fallback caches
        if msg in self._exact_msg_cache:
            return self._exact_msg_cache[msg]

        if template in self._template_msg_cache:
            return self._template_msg_cache[template]

        return None
