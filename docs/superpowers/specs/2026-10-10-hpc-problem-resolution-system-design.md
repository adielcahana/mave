# Design Specification: HPC Log Problem Resolution & Dynamic Rule Learning System

**Date:** 2026-10-10  
**Status:** Approved Specification  
**Target Repository:** `mave`  

---

## 1. Intent & Context

High Performance Computing (HPC) clusters generate high-throughput semi-structured log streams across thousands of compute nodes, service daemons, interconnect switches, and shared cluster filesystems. Failures cause severe job interruptions and cluster downtime. 

The objective is to build an intelligent, future-proof problem resolution system that:
1. Filters benign telemetry at wire speed without burning LLM tokens or introducing latency.
2. Resolves recurring, known failure patterns via an instant fast-path **Rule Engine / Resolution Cache**.
3. Deploys a **Real-Time SRE Agent** (powered by Pydantic-AI and NVIDIA NIM) to investigate novel, unseen errors by querying subsystem documentation and log context.
4. Closes the learning loop with an asynchronous **Post-Mortem Consolidation Agent** that synthesizes generalized rules from novel incidents, verifies them with **hard-coded code guardrails** against historical logs, and prompts for **human operator approval** before activating them.

---

## 2. Goals, Non-Goals & Anti-Goals

### 2.1 Goals
- **Empirical Grounding**: Grounded in the 433,490-line LANL System 20 HPC dataset (`data/HPC.log`), respecting its 44 subsystem/event types and chronological evolution.
- **Two-Tier Architecture**:
  - **Fast Path**: Sub-millisecond deterministic parsing and rule matching.
  - **Slow Path**: Deep agentic reasoning with tool calling (`pydantic-ai`) for novel errors.
- **Two-Agent Role Separation**:
  - **Real-Time SRE Agent**: Immediate operator triage, root-cause explanation, and actionable remediation with citations.
  - **Post-Mortem Rule Agent**: Pattern consolidation across incident clusters, candidate rule generation, and regression validation.
- **Hard-Coded Safety Guardrails**: Zero tolerance for false positives on normal telemetry; strict code-level rejection of dangerous or overly broad patterns before human review.
- **Human-in-the-Loop**: Explicit operator sign-off before committing newly learned rules into the active fast path (with an automated `--auto-approve` flag for headless CI/tests).
- **Incident Burst Debouncing**: In-flight deduplication buffer ensuring rapid bursts of identical novel errors do not flood the LLM API with redundant calls.
- **Strict Code Quality**: Passes Python 3.12+ `ruff check`, `ruff format`, and `ty check`.

### 2.2 Non-Goals
- **Live Distributed Ingestion**: Per `instructions.md`, data ingestion infrastructure (Kafka, Fluentbit, ClickHouse/Snowflake) is diagrammed and architected in the design/report, not deployed as running background infrastructure.
- **Custom DSL Compiler**: No custom parser/AST engine needed; rules are declarative Python/Pydantic schemas with regex and filter guards.
- **Unbounded Auto-Deployment**: No autonomous self-modification of production rules without passing hard-coded regression tests and receiving operator approval.

### 2.3 Anti-Goals
- **LLM-on-Every-Log**: Never send high-volume routine heartbeats (`ambient=36`, `normal`, `Link ok`) to an LLM.
- **Fragile Flat Regexes**: Avoiding monolithic flat regex lists that don't respect the hierarchical `Node -> Subsystem -> Event -> Message` nature of HPC logs.
- **Hallucinated Remediations**: Never output generic ungrounded advice; all agent recommendations must cite specific subsystem docs.

---

## 3. Data Insights & Ground Truth Standard

### 3.1 Dataset Anatomy (`data/HPC.log`)
- **Total Lines**: 433,490 logs spanning July 2003 through April 2006.
- **Schema**: `[LogID] [Entity/Device] [Subsystem] [EventCategory] [Timestamp] [SeverityFlag] [MessageBody]`
- **Subsystem & Event Space**: 44 distinct pairs across 1,129 message templates.
- **Telemetry Dominance**: Over 85% of traffic is normal telemetry (`ambient=*`, `normal`, `running`, `Link ok`).

### 3.2 Chronological Splits (`data/splits/`)
Generated via [`src/split_data.py`](file:///c:/Code/mave/src/split_data.py):
- **Train (70%)** (`data/splits/train.log`): 303,443 lines (2003-07-31 to 2005-04-18), 1,094 templates. Used to build baseline rule sets and normal telemetry invariance baselines.
- **Validation (15%)** (`data/splits/val.log`): 65,023 lines (2005-04-18 to 2005-11-20), contains recurring known logs + 24 brand-new unseen error/command templates (e.g. CMF-Port connection errors, SCSI WWID dropouts).
- **Test (15%)** (`data/splits/test.log`): 65,024 lines (2005-11-20 to 2006-04-30), contains recurring known logs + 53 brand-new unseen error templates (e.g. `ServerFileSystem domain panic`, `resourcemgmtdaeomon err = 10`, `Console-Busy`).

### 3.3 Evaluation & Ground Truth Benchmark
Because `HPC.log` is officially unlabeled in Loghub (`N.A.` for anomalies), the system uses a **Curated Golden Evaluation Benchmark** (`data/golden_eval.json`):
- A curated suite of ~150 representative failure scenarios (`CRITICAL`, `HIGH`, `WARNING`) vs benign baselines (`NORMAL`).
- Precision, Recall, and F1 measured deterministically on error triage in `main.py evaluate`.
- Invariance verification: 0 false positives on baseline telemetry.
- Fast-path vs Slow-path throughput and hit rate telemetry.

---

## 4. System Architecture & Components

```
+---------------------------------------------------------------------------------+
|                       1. Ingestion Architecture (Design)                        |
|   HPC Nodes/Switches -> Fluentbit/Vector -> Kafka Buffer -> Analytical Store    |
+---------------------------------------------------------------------------------+
                                       |
                                       v
+---------------------------------------------------------------------------------+
|                       2. Stream Triage & Rule Engine (Fast Path)                |
|   Stream Normalizer -> Telemetry Filter (Discards benign logs)                  |
|   -> Rule Engine Tree: Matches Subsystem/Event/Regex Cache                      |
|       |--> [Hit]  -> Immediate Vetted Resolution (Sub-ms, $0)                   |
|       |--> [Miss] -> Debounce & In-Flight Buffer -> Hand off to SRE Agent       |
+---------------------------------------------------------------------------------+
                                       |
                                       v
+---------------------------------------------------------------------------------+
|                       3. Real-Time SRE Agent (Online Slow Path)                 |
|   Pydantic-AI Agent (NVIDIA NIM) + Subsystem Documentation Tools                |
|   -> Queries daemon manuals, runbooks, error code lookup                        |
|   -> Produces ProblemResolution: Summary, Root Cause, Severity, Steps, Citations|
|   -> Delivers remediation to Operator & logs to Incident Audit Store            |
+---------------------------------------------------------------------------------+
                                       |
                                       v
+---------------------------------------------------------------------------------+
|               4. Post-Mortem Rule Consolidation Agent (Offline Loop)            |
|   Pydantic-AI Post-Mortem Agent                                                 |
|   -> Tool: inspect_incident_cluster (analyzes unresolved cases)                 |
|   -> Tool: propose_rule (synthesizes structured RuleCandidate)                 |
|   -> Tool: test_rule_against_dataset (Dry-runs rule vs historical logs)         |
|   -> Hard-Coded Guardrails: ReDoS check, 0 False Positives on normal telemetry  |
|   -> Human-in-the-Loop Gate: Prompts Operator for Approval (or --auto-approve)  |
|   -> Tool: promote_rule (Registers vetted rule into active Rule Engine)         |
+---------------------------------------------------------------------------------+
```

### 4.1 Log Normalizer, Telemetry Filter & Rule Tree (`src/rules/`)
- **`LogEvent` model**: Strongly typed representation of parsed log fields (`log_id`, `node`, `subsystem`, `event_type`, `timestamp`, `flag`, `message`).
- **`TelemetryFilter`**: High-speed deterministic regex/keyword gate identifying benign telemetry (`ambient=`, `normal`, `running`, `Link ok`, `success`) to bypass LLM analysis.
- **`IncidentDebouncer`**: State buffer tracking currently in-flight unresolved errors. If 20 identical lines burst within seconds during an incident storm, only one LLM query is triggered; subsequent occurrences attach to the pending investigation.
- **`Rule` model**:
  - `rule_id`: Unique identifier (e.g., `RULE-FS-PANIC-01`).
  - `subsystem`: Target subsystem guard (e.g., `clusterfilesystem`).
  - `event_type`: Target event type guard (e.g., `fdmn.panic`).
  - `pattern`: Compiled regex for variable extraction.
  - `resolution`: Structured `ProblemResolution` returned on match.
  - `provenance`: Triggering log sample, author/agent tag, creation timestamp.
- **`RuleTreeEngine`**: Hierarchical dispatch indexing rules by `(subsystem, event_type)` with fallback pattern matching.

### 4.2 Real-Time SRE Agent (`src/sre_agent.py`)
- Built using **Pydantic-AI** and typed dependencies (`Settings`).
- **Tools**:
  - `query_subsystem_docs(subsystem, error_code)`: Enriched knowledge base covering actual HPC subsystems (`resourcemgmtdaeomon`, `clusterfilesystem`, `unix.hw`, `switch_module`, `slurm`, `boot_cmd`).
  - `query_historical_frequency(subsystem, event_type)`: Inquires about past event volume.
- **Output Schema**: Structured `ProblemResolution` (`error_summary`, `root_cause`, `severity`, `remediation_steps`, `citations_and_reasons`).

### 4.3 Post-Mortem Rule Consolidation Agent (`src/post_mortem_agent.py`)
- Asynchronous agent invoked on batches of novel incidents logged by the SRE agent.
- **Tools**:
  - `inspect_unresolved_incidents()`: Gathers recent un-cached incident logs and agent resolutions.
  - `propose_rule_candidate(subsystem, event_type, regex_pattern, resolution)`: Formulates a structured candidate rule.
  - `dry_run_rule(candidate, dataset_split)`: Executes test runs against train/val splits.
  - `promote_rule(candidate)`: Inserts rule into the active Rule Engine store.
- **Hard-Coded Code Safety Guardrails** (`src/guardrails.py`):
  - *Telemetry Anti-Match Check*: Evaluates the regex against a golden set of 5,000 normal telemetry logs. If `false_positives > 0`, the tool raises an immediate rejection error.
  - *Trigger Recall Check*: Verifies candidate regex matches the specific triggering incident logs (`true_positives > 0`).
  - *ReDoS & Complexity Validation*: Validates regex syntax and bounded execution time.
- **Human Approval Gate**:
  - Interactive CLI confirmation (`[A]pprove / [R]eject`) presenting rule details and regression test metrics before promotion.
  - Non-interactive headless support via `--auto-approve` flag for automated CI/pytest runs.

### 4.4 End-to-End Orchestrator & CLI (`main.py`)
- Supported CLI commands:
  - `demo`: Streams a curated progression of logs (telemetry -> known error -> novel unseen error -> SRE research -> post-mortem rule synthesis -> operator approval -> stream replay demonstrating fast-path cache hit).
  - `evaluate`: Computes quantitative metrics (Precision, Recall, F1 on `data/golden_eval.json`, and stream triage throughput on test split).

---

## 5. Builder's Decisions (Explicit Engineering Calls)

The following architectural decisions have been settled as senior engineering calls:
1. **Benchmark Scope**: `evaluate` mode benchmarks against `data/golden_eval.json` (~150 diverse labeled scenarios) for deterministic Precision/Recall/F1 in seconds, while reporting streaming ingestion throughput over `test.log`.
2. **Incident Storm Debounce**: An in-flight debounce buffer (`IncidentDebouncer`) collapses repetitive bursts of the same error into a single incident, preventing API rate limits (HTTP 429).
3. **Approval Gate Strategy**: Interactive prompt by default, with an `--auto-approve` flag for non-interactive test and CI runs.
4. **Demo Execution Flow**: The `demo` mode runs an end-to-end interactive simulation displaying live telemetry filtering, fast-path hits, SRE diagnosis of an unseen failure, post-mortem rule synthesis, and the subsequent fast-path hit.

---

## 6. Verification & Quality Plan

1. **Static Typing & Code Style**:
   - `uv run ruff check .`
   - `uv run ruff format .`
   - `uv run ty check`
2. **Automated Test Suite (`uv run pytest`)**:
   - `test_normalizer.py`: Parsing valid and malformed log lines.
   - `test_guardrails.py`: Hard-coded false positive and negative rejection tests.
   - `test_rule_engine.py`: Fast-path matching, hierarchical dispatch, and cache hit rates.
   - `test_sre_agent.py`: Agent execution and mock tool resolution.
   - `test_post_mortem.py`: Rule synthesis, validation dry-run, and promotion lifecycle.
3. **End-to-End Validation**:
   - Run end-to-end stream replay verifying that unseen errors from `val` and `test` splits are investigated, synthesized into rules, and subsequently resolved via the fast path.
