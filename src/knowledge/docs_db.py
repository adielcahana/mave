"""Authoritative LANL HPC subsystem knowledge base and runbooks."""

from dataclasses import dataclass

__all__ = [
    "RunbookEntry",
    "get_all_runbooks",
    "list_known_subsystems",
    "query_subsystem_docs",
    "resolve_subsystem",
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


# Canonical list of documented LANL HPC subsystems
KNOWN_SUBSYSTEMS: list[str] = [
    "clusterfilesystem",
    "resourcemgmtdaeomon",
    "unix.hw",
    "switch_module",
    "boot_cmd",
]

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

# Authoritative runbook database
_RUNBOOKS: list[RunbookEntry] = [
    # =========================================================================
    # Subsystem: resourcemgmtdaeomon
    # =========================================================================
    RunbookEntry(
        subsystem="resourcemgmtdaeomon",
        scenario="Resource Management Daemon Subsystem Configuration Error (err = 10)",
        root_cause=(
            "The resource management daemon failed to parse or apply cluster configuration "
            "parameters (error code 10) during subsystem initialization. Common causes "
            "include corrupt /etc/resourcemgmt.conf, permission issues, or mismatched node maps."
        ),
        remediation_steps=(
            "Verify syntax and permissions of /etc/resourcemgmt.conf and node allocation tables.",
            "Verify node controller socket connectivity and resource daemon read access.",
            "Restart resourcemgmtdaeomon service via `systemctl restart resourcemgmtdaeomon`.",
            (
                "If error persists, rebuild local configuration cache and "
                "re-synchronize with master controller."
            ),
        ),
        citation="[LANL HPC Runbook: resourcemgmt-config-err10-v1.4]",
        keywords=("err = 10", "error 10", "config", "configure", "subsystem err", "err=10"),
        event_type="server",
    ),
    RunbookEntry(
        subsystem="resourcemgmtdaeomon",
        scenario="Resource Management Node Controller Communication Timeout",
        root_cause=(
            "The master resource manager daemon timed out waiting for heartbeat or control "
            "responses from the node controller daemon. Caused by compute node kernel freeze, "
            "network interface congestion, or hung controller threads."
        ),
        remediation_steps=(
            "Ping target node controller over management network to verify IP reachability.",
            "Check node controller daemon status and CPU/memory load on target compute node.",
            "Restart hung node controller process and verify socket responsiveness.",
            "Re-register node controller with master resource manager daemon to restore heartbeat.",
        ),
        citation="[LANL HPC Runbook: resourcemgmt-controller-timeout-v1.2]",
        keywords=("timeout", "controller", "node controller", "heartbeat", "unresponsive", "hung"),
        event_type="controller",
    ),
    RunbookEntry(
        subsystem="resourcemgmtdaeomon",
        scenario="Resource Management Daemon Startup Failure",
        root_cause=(
            "Daemon bootstrap aborted due to an unreleased port binding, stale IPC lockfile, "
            "exhausted system file descriptors, or missing shared memory segment."
        ),
        remediation_steps=(
            "Inspect /var/log/resourcemgmt.log for socket bind errors or permission failures.",
            (
                "Clear stale lockfiles (/var/run/resourcemgmt.pid) and unreleased IPC "
                "shared memory segments (`ipcrm`)."
            ),
            "Verify TCP/UDP port availability for resource management communication ports.",
            "Relaunch daemon service under supervisor and monitor startup log stream.",
        ),
        citation="[LANL HPC Runbook: resourcemgmt-daemon-startup-v1.1]",
        keywords=(
            "startup",
            "bootstrap",
            "daemon startup",
            "bind",
            "initialization failure",
            "abort",
        ),
        event_type="startup",
    ),
    # =========================================================================
    # Subsystem: clusterfilesystem
    # =========================================================================
    RunbookEntry(
        subsystem="clusterfilesystem",
        scenario="ServerFileSystem domain panic (fdmn.panic)",
        root_cause=(
            "Critical filesystem metadata inconsistency, I/O barrier timeout, or "
            "unrecoverable disk block corruption triggering an emergency domain panic "
            "to protect data integrity."
        ),
        remediation_steps=(
            "Immediately quarantine and unmount affected storage domain to halt write cascading.",
            "Inspect storage fabric, SAN switches, and controller target connectivity.",
            "Run filesystem consistency repair utility (`advfsck -r <domain>`) offline.",
            (
                "If metadata corruption is irreparable, restore domain metadata from latest "
                "snapshot or tape backup."
            ),
        ),
        citation="[LANL HPC Runbook: clusterfilesystem-fdmn-panic-v2.1]",
        keywords=("panic", "fdmn.panic", "domain panic", "panicked", "emergency panic"),
        event_type="fdmn.panic",
    ),
    RunbookEntry(
        subsystem="clusterfilesystem",
        scenario="ServerFileSystem domain full (fdmn.full)",
        root_cause=(
            "Storage domain reached 100% capacity limit, preventing new block allocations and "
            "causing active HPC job I/O halts across compute clients."
        ),
        remediation_steps=(
            "Identify large consumer accounts and active jobs writing to the affected domain.",
            "Purge expired scratch files, temporary core dumps, and staging caches.",
            "Expand storage domain volume capacity using `addvol <domain> <device>`.",
            "Rebalance volume striping and re-enable cluster write jobs.",
        ),
        citation="[LANL HPC Runbook: clusterfilesystem-fdmn-full-v1.8]",
        keywords=("full", "fdmn.full", "domain full", "capacity", "quota", "root_domain"),
        event_type="fdmn.full",
    ),
    RunbookEntry(
        subsystem="clusterfilesystem",
        scenario="ClusterFileSystem Unserved Storage Domain (no server)",
        root_cause=(
            "Cluster filesystem cannot locate an active serving host or metadata server for the "
            "requested storage domain; primary metadata server node is down or SAN disconnected."
        ),
        remediation_steps=(
            "Identify designated primary and secondary metadata server nodes for the domain.",
            "Verify cluster filesystem daemon status and SAN interconnect on serving nodes.",
            "Force domain server failover to secondary node if primary server is unresponsive.",
            "Remount storage domain across client nodes once server export is re-established.",
        ),
        citation="[LANL HPC Runbook: clusterfilesystem-unserved-domain-v2.0]",
        keywords=(
            "no server",
            "no_server",
            "unserved",
            "server for serverfilesystem",
            "missing server",
            "clusterfilesystem.no_server",
        ),
        event_type="clusterfilesystem.no_server",
    ),
    RunbookEntry(
        subsystem="clusterfilesystem",
        scenario="ClusterFileSystem Storage Domain Metadata Corruption",
        root_cause=(
            "Corrupted superblock, invalid inode B-tree pointers, or torn writes following "
            "an unexpected storage node power interruption or controller cache dump failure."
        ),
        remediation_steps=(
            "Set storage domain to read-only mode to prevent write amplification of corruption.",
            "Extract filesystem event journal logs to locate damaged directory blocks.",
            "Execute offline metadata reconstruction and salvage tools (`advfsck`).",
            "Validate directory hierarchy and restore damaged inodes from snapshot.",
        ),
        citation="[LANL HPC Runbook: clusterfilesystem-metadata-corruption-v1.3]",
        keywords=(
            "metadata",
            "corruption",
            "corrupt",
            "inode",
            "superblock",
            "torn write",
            "btree",
        ),
        event_type="metadata",
    ),
    # =========================================================================
    # Subsystem: unix.hw
    # =========================================================================
    RunbookEntry(
        subsystem="unix.hw",
        scenario="Network Interface alt0 Down / Link Failure",
        root_cause=(
            "Physical Ethernet or InfiniBand link loss, optical transceiver failure, or carrier "
            "loss detected on primary cluster communication interface alt0."
        ),
        remediation_steps=(
            "Verify physical patch cable seating and check link LEDs on switch port and host NIC.",
            "Restart network interface: `ifconfig alt0 down && ifconfig alt0 up`.",
            "Inspect kernel dmesg for PCIe AER error reports or optical transceiver fault codes.",
            "Reseat optical transceiver module or failover cluster traffic to backup interface.",
        ),
        citation="[LANL HPC Runbook: unix-hw-alt0-down-v2.2]",
        keywords=(
            "alt0",
            "interface down",
            "link down",
            "carrier",
            "nic",
            "interface alt0 is down",
        ),
        event_type="net.down",
    ),
    RunbookEntry(
        subsystem="unix.hw",
        scenario="Hardware Component State Change Unavailable",
        root_cause=(
            "Hardware inventory monitor detected a cluster component (PCIe adapter, SCSI disk, "
            "or controller) transitioned to unavailable state due to bus timeout or hardware fault."
        ),
        remediation_steps=(
            "Query hardware inventory with component HWID to identify the failing physical device.",
            "Check IPMI system event logs (SEL) and sensor health for power/thermal anomalies.",
            "Execute hardware bus rescan (`rescan-scsi-bus` or PCIe hotplug reset).",
            "Schedule node maintenance for hardware component reseat or field replacement.",
        ),
        citation="[LANL HPC Runbook: unix-hw-state-change-v1.5]",
        keywords=(
            "unavailable",
            "state change",
            "state_change.unavailable",
            "hwid",
            "component",
            "unavailable state",
        ),
        event_type="state_change.unavailable",
    ),
    RunbookEntry(
        subsystem="unix.hw",
        scenario="Memory Subsystem ECC Corrections / Multi-Bit Faults",
        root_cause=(
            "Compute node memory controller detected repeated single-bit ECC corrections "
            "exceeding rate threshold, or an uncorrectable multi-bit ECC error indicating physical "
            "DIMM degradation."
        ),
        remediation_steps=(
            "Extract memory controller EDAC logs (`edac-util -v`) to isolate failing DIMM socket.",
            (
                "Assess error frequency: if correctable error rate exceeds threshold, schedule "
                "preventative replacement."
            ),
            "If uncorrectable multi-bit error occurred, drain active jobs and halt node.",
            "Replace defective memory module during scheduled maintenance window.",
        ),
        citation="[LANL HPC Runbook: unix-hw-memory-ecc-v1.1]",
        keywords=(
            "ecc",
            "memory ecc",
            "dimm",
            "single-bit",
            "multi-bit",
            "edac",
            "memory correction",
        ),
        event_type="ecc",
    ),
    # =========================================================================
    # Subsystem: switch_module
    # =========================================================================
    RunbookEntry(
        subsystem="switch_module",
        scenario="Interconnect Switch Power Supply Unit (PSU) Failover / Degraded",
        root_cause=(
            "Switch chassis power supply unit lost AC input feed or suffered internal "
            "rectifier fault, operating in degraded single-feed redundancy (off on / on off)."
        ),
        remediation_steps=(
            "Inspect switch chassis rear PSU status LEDs and verify AC power cable connection.",
            "Confirm redundant PSU successfully maintained switch backplane power without loss.",
            "Hot-swap failed power supply unit with compatible spare module.",
            "Verify dual-redundant PSU operational status is restored (`PSU status ( on on )`).",
        ),
        citation="[LANL HPC Runbook: switch-module-psu-failover-v2.0]",
        keywords=(
            "psu",
            "power supply",
            "off on",
            "on off",
            "degraded power",
            "rectifier",
            "psu status",
        ),
        event_type="psu",
    ),
    RunbookEntry(
        subsystem="switch_module",
        scenario="Switch Module Temperature Threshold Exceeded / Overheat",
        root_cause=(
            "Internal switch chassis temperature exceeded thermal threshold due to cooling fan "
            "degradation, filter blockage, or rack airflow stagnation."
        ),
        remediation_steps=(
            "Check switch chassis fan RPM telemetry (`Fan speeds`) to isolate stalled fans.",
            "Inspect rack air baffles and clean perforated intake filters to restore airflow.",
            "Reroute high-bandwidth MPI traffic away from overheating switch spine/leaf.",
            "Replace degraded fan tray assembly if RPM values remain below operating range.",
        ),
        citation="[LANL HPC Runbook: switch-module-temperature-overheat-v1.7]",
        keywords=(
            "temperature",
            "temphigh",
            "overheat",
            "thermal",
            "cooling",
            "fan speeds",
            "warning threshold",
            "exceeds warning threshold",
        ),
        event_type="temphigh",
    ),
    RunbookEntry(
        subsystem="switch_module",
        scenario="Switch Module Link Flapping and Broadcast Link Errors",
        root_cause=(
            "Interconnect switch port experiencing physical signal attenuation, bad optical "
            "termination, or repeated reset cycles on broadcast tree."
        ),
        remediation_steps=(
            "Identify the flapping port or broadcast tree ID (e.g. `Interconnect-0T00:00:2:1`).",
            "Inspect fiber optic connectors for dirt; clean and reseat transceiver optics.",
            "Query switch port diagnostics for symbol errors, CRC packet drops, and reset counts.",
            "Administratively disable flapping link or reconfigure fabric routing across backup.",
        ),
        citation="[LANL HPC Runbook: switch-module-link-flapping-v1.6]",
        keywords=(
            "flapping",
            "link error",
            "bcast-error",
            "link reset",
            "broadcast tree",
            "link in reset",
            "link",
        ),
        event_type="bcast-error",
    ),
    # =========================================================================
    # Subsystem: boot_cmd
    # =========================================================================
    RunbookEntry(
        subsystem="boot_cmd",
        scenario="Boot Command Error: CMF-Port Disconnected",
        root_cause=(
            "Boot command rejected because the Connection Management Facility (CMF) management "
            "port for the target node is disconnected or unresponsive."
        ),
        remediation_steps=(
            "Verify physical CMF management cabling between compute node and console server.",
            "Inspect terminal server switch port activity LED and CMF management link status.",
            "Restart CMF terminal proxy daemon on cluster management head node.",
            "Reseat node CMF management mezzanine adapter if connection remains down.",
        ),
        citation="[LANL HPC Runbook: boot-cmd-cmf-port-disconnect-v2.1]",
        keywords=(
            "cmf-port",
            "cmf",
            "port is not connected",
            "port disconnected",
            "management port",
            "cmf-port this node",
        ),
        event_type="error",
    ),
    RunbookEntry(
        subsystem="boot_cmd",
        scenario="Boot Command Aborted / Node State Determination Error",
        root_cause=(
            "Boot management controller could not determine target node hardware/firmware state, "
            "triggering automated boot command abort across target partitions."
        ),
        remediation_steps=(
            "Query out-of-band interface (BMC/IPMI) for node power status and POST code.",
            "Issue explicit halt command to reset node state into deterministic SRM prompt.",
            "Re-execute boot sequence targeting the specific compute partition.",
            "If state determination continues to fail, trigger hard node power cycle via BMC.",
        ),
        citation="[LANL HPC Runbook: boot-cmd-state-determination-v1.4]",
        keywords=(
            "abort",
            "command failed",
            "state determination",
            "command has been aborted",
            "node state",
            "aborted",
        ),
        event_type="abort",
    ),
    RunbookEntry(
        subsystem="boot_cmd",
        scenario="Boot Command Failed: Console Busy / Timeout Waiting for SRM Prompt",
        root_cause=(
            "Node serial console is locked by another operator session or firmware hung "
            "before yielding the SRM console prompt during halt/boot cycle."
        ),
        remediation_steps=(
            "Identify and terminate orphaned console connections attached to the target node.",
            "Transmit console break sequence (~. or break signal) to regain console control.",
            "Check node serial baud rate and console redirection settings in firmware.",
            "Issue hardware NMI or chassis power cycle if console remains locked.",
        ),
        citation="[LANL HPC Runbook: boot-cmd-console-busy-v1.3]",
        keywords=(
            "console busy",
            "srm prompt",
            "timeout waiting for srm",
            "console",
            "prompt timeout",
        ),
        event_type="error",
    ),
]


def list_known_subsystems() -> list[str]:
    """Return the list of supported and documented LANL HPC subsystems."""
    return list(KNOWN_SUBSYSTEMS)


def get_all_runbooks() -> list[RunbookEntry]:
    """Return all documented runbook entries in the knowledge base."""
    return list(_RUNBOOKS)


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
