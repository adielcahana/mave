# HPC Log Problem Resolution & Dynamic Rule Learning Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build an end-to-end HPC log problem resolution system combining wire-speed telemetry filtering, a fast-path Rule Tree engine, an enriched Pydantic-AI SRE resolution agent, and a post-mortem rule consolidation agent with hard-coded code guardrails.

**Architecture:** A two-tier problem resolution pipeline where incoming logs are parsed and filtered for routine telemetry; recurring errors hit a fast-path rule cache for sub-millisecond, zero-cost resolution; novel errors route to a real-time Pydantic-AI SRE agent querying subsystem documentation; and an offline post-mortem agent synthesizes generalized rules verified against hard-coded telemetry guardrails and operator approval.

**Tech Stack:** Python 3.12+, Pydantic-AI, Pydantic v2, AsyncOpenAI (NVIDIA NIM), Pytest, Ruff, Ty.

**Spec:** [`docs/superpowers/specs/2026-10-10-hpc-problem-resolution-system-design.md`](file:///c:/Code/mave/docs/superpowers/specs/2026-10-10-hpc-problem-resolution-system-design.md)

---

## Deliverable Separation Matrix

| Component | Nature | Destination | Description |
| :--- | :--- | :--- | :--- |
| **Ingestion Pipeline** | **Design Only** | `report.md`, Architecture Diagrams | End-to-end Kafka, Fluentbit, and ClickHouse/Snowflake streaming architecture as explicitly directed in assignment instructions ("don't implement, just diagram"). |
| **Log Normalizer & Telemetry Filter** | **Actual Code** | `src/rules/normalizer.py`, `src/rules/telemetry_filter.py` | High-speed regex/string parser separating benign telemetry (`ambient=*`, `normal`, `Link ok`) from actionable events. |
| **Rule Engine & In-Flight Debouncer** | **Actual Code** | `src/rules/models.py`, `src/rules/engine.py`, `src/rules/debouncer.py` | Hierarchical `(subsystem, event)` Rule Tree cache and incident debouncer preventing redundant LLM calls during error storms. |
| **Hard-Coded Safety Guardrails** | **Actual Code** | `src/guardrails.py` | Programmatic rejection of candidate rules causing false positives on baseline telemetry, ReDoS patterns, or missing incident recall. |
| **Real-Time SRE Agent** | **Actual Code** | `src/sre_agent.py`, `src/knowledge/docs_db.py` | Pydantic-AI agent querying rich LANL HPC subsystem documentation to explain root causes and provide cited remediations. |
| **Post-Mortem Consolidation Agent** | **Actual Code** | `src/post_mortem_agent.py` | Asynchronous agent with tools to inspect incident clusters, synthesize candidate rules, dry-run against historical logs, and request operator approval. |
| **Evaluation Suite & Golden Benchmark** | **Actual Code & Data** | `data/golden_eval.json`, `tests/`, `main.py evaluate` | Curated evaluation dataset across failure and normal categories, computing Precision, Recall, and F1. |
| **Interactive CLI & Unified Demo** | **Actual Code** | `main.py` | Unified CLI supporting interactive walkthrough (`demo`) and automated metrics benchmark (`evaluate`). |
| **Submission Report & Run Instructions**| **Actual Docs** | `report.md`, `README.md` | Formal assignment submission report with design diagrams, thought process, and exact build/run instructions. |

---

## Global Constraints

- **Python Version**: Python 3.12+ enforcing strict types (`uv run ty check`) and formatting (`uv run ruff check .` and `uv run ruff format .`).
- **Frameworks**: Pydantic-AI agent harness with OpenAI-compatible endpoint (`AsyncOpenAI`) configured via `pydantic-settings` (`Settings`).
- **Telemetry Invariance**: Zero false positives permitted on normal cluster telemetry (`ambient=*`, `normal`, `running`, `Link ok`).
- **Operator Authority**: No rule can be promoted without explicit operator approval (`InteractiveCLIOperator` or `--auto-approve` policy in headless mode).
- **Incident Burst Protection**: Identical unresolved errors within a debounce window must not trigger redundant LLM calls.

---

## Review Focus

1. **Empty/Malformed Log Lines**: Parser must handle missing fields, malformed timestamps, or unexpected line lengths gracefully without crashing (Task 1).
2. **ReDoS Vulnerability in Candidate Regex**: Synthesized rules with catastrophic backtracking must be blocked programmatically by the guardrail timeout (Task 3).
3. **Bursting Error Storms**: Repetitive novel errors occurring consecutive times (e.g. 25x `err = 10`) must be collapsed into a single SRE incident (Task 2).
4. **False Positive Poisoning**: Any candidate rule matching normal telemetry words must be rejected immediately by hard-coded guardrails before reaching operator review (Task 3).
5. **Headless Execution Compatibility**: Automated evaluation and CI test suites must run without hanging on interactive stdin (Task 6).

---

## Task Decomposition

### Task 1: Log Normalizer, Telemetry Filter, and LogEvent Model (Actual Code)

**Files:**
- Create: `src/rules/models.py`
- Create: `src/rules/normalizer.py`
- Create: `src/rules/telemetry_filter.py`
- Test: `tests/test_normalizer.py`

**Interfaces:**
- Produces:
  - `LogEvent(log_id: str, node: str, subsystem: str, event_type: str, timestamp: int, flag: int, message: str, raw: str)`
  - `parse_log_line(raw: str) -> LogEvent`
  - `TelemetryFilter.is_routine_telemetry(event: LogEvent) -> bool`

- [ ] **Step 1: Write failing tests for log parsing and telemetry filtering**

```python
# tests/test_normalizer.py
from src.rules.normalizer import parse_log_line
from src.rules.telemetry_filter import TelemetryFilter


def test_parse_standard_hpc_log():
    raw = "460903 resourcemgmtdaeomon node-25 server subsys 1145552216 1 failed to configure resourcemgmt subsystem err = 10"
    event = parse_log_line(raw)
    assert event.log_id == "460903"
    assert event.subsystem in ("resourcemgmtdaeomon", "server")
    assert event.timestamp == 1145552216
    assert "err = 10" in event.message


def test_telemetry_filter_identifies_normal():
    raw = "2560243 gige3 gige temperature 1073198499 1 normal"
    event = parse_log_line(raw)
    assert TelemetryFilter.is_routine_telemetry(event) is True


def test_telemetry_filter_retains_error():
    raw = "460903 resourcemgmtdaeomon node-25 server subsys 1145552216 1 failed to configure resourcemgmt subsystem err = 10"
    event = parse_log_line(raw)
    assert TelemetryFilter.is_routine_telemetry(event) is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_normalizer.py -v`  
Expected: FAIL with ModuleNotFoundError or import errors.

- [ ] **Step 3: Implement `LogEvent`, `parse_log_line`, and `TelemetryFilter`**

Implement robust line splitting and regex parsing in `src/rules/normalizer.py`, and implement routine keyword/pattern matching (`ambient=`, `normal`, `Link ok`, `Fan speeds`, `status running`) in `src/rules/telemetry_filter.py`.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_normalizer.py -v`  
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/rules/ tests/test_normalizer.py
git commit -m "feat(rules): add LogEvent normalizer and fast telemetry filter"
```

---

### Task 2: Rule Schema, RuleTreeEngine, and Incident Debouncer (Actual Code)

**Files:**
- Create: `src/rules/engine.py`
- Create: `src/rules/debouncer.py`
- Modify: `src/rules/models.py`
- Test: `tests/test_rule_engine.py`

**Interfaces:**
- Consumes: `LogEvent` from `src/rules/models.py`
- Produces:
  - `Rule(rule_id: str, subsystem: str, event_type: str, pattern: str, resolution: ProblemResolution, provenance: dict)`
  - `RuleTreeEngine.match(event: LogEvent) -> ProblemResolution | None`
  - `RuleTreeEngine.add_rule(rule: Rule) -> None`
  - `IncidentDebouncer.should_dispatch(event: LogEvent) -> bool`

- [ ] **Step 1: Write failing tests for RuleTreeEngine and IncidentDebouncer**

```python
# tests/test_rule_engine.py
from src.agent import ProblemResolution
from src.rules.debouncer import IncidentDebouncer
from src.rules.engine import RuleTreeEngine
from src.rules.models import Rule
from src.rules.normalizer import parse_log_line


def test_rule_tree_match_and_hit():
    engine = RuleTreeEngine()
    resolution = ProblemResolution(
        error_summary="PSU failure",
        root_cause="Power supply unit degraded",
        severity="HIGH",
        remediation_steps=["Replace PSU module"],
        citations_and_reasons=["Hardware manual section 4.2"],
    )
    rule = Rule(
        rule_id="RULE-PSU-01",
        subsystem="switch_module",
        event_type="psu",
        pattern=r"PSU status \( (?:off on|on off) \)",
        resolution=resolution,
    )
    engine.add_rule(rule)

    event = parse_log_line(
        "98745 Interconnect-0N00 switch_module psu 1110481498 1 PSU status ( off on )"
    )
    matched = engine.match(event)
    assert matched is not None
    assert matched.severity == "HIGH"


def test_debouncer_collapses_burst():
    debouncer = IncidentDebouncer(window_seconds=60)
    event1 = parse_log_line(
        "460903 resourcemgmtdaeomon node-25 server subsys 1145552216 1 failed to configure resourcemgmt subsystem err = 10"
    )
    event2 = parse_log_line(
        "460919 resourcemgmtdaeomon node-25 server subsys 1145552221 1 failed to configure resourcemgmt subsystem err = 10"
    )
    assert debouncer.should_dispatch(event1) is True
    assert debouncer.should_dispatch(event2) is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_rule_engine.py -v`  
Expected: FAIL with missing classes.

- [ ] **Step 3: Implement `RuleTreeEngine` and `IncidentDebouncer`**

Implement indexed dictionary lookup `self._tree[(subsystem, event_type)]` in `src/rules/engine.py` and timestamped in-flight tracking with TTL in `src/rules/debouncer.py`.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_rule_engine.py -v`  
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/rules/engine.py src/rules/debouncer.py tests/test_rule_engine.py
git commit -m "feat(rules): implement RuleTreeEngine and incident storm debouncer"
```

---

### Task 3: Hard-Coded Code Safety Guardrails (Actual Code)

**Files:**
- Create: `src/guardrails.py`
- Test: `tests/test_guardrails.py`

**Interfaces:**
- Consumes: `Rule` from `src/rules/models.py`, `LogEvent` from `src/rules/models.py`
- Produces:
  - `GuardrailValidator.validate_rule(rule: Rule, trigger_samples: list[LogEvent], telemetry_samples: list[LogEvent]) -> GuardrailResult`

- [ ] **Step 1: Write failing tests for GuardrailValidator**

```python
# tests/test_guardrails.py
import pytest
from src.agent import ProblemResolution
from src.guardrails import GuardrailError, GuardrailValidator
from src.rules.models import Rule
from src.rules.normalizer import parse_log_line


def test_guardrail_rejects_telemetry_false_positive():
    telemetry_sample = [
        parse_log_line("2560243 gige3 gige temperature 1073198499 1 normal"),
        parse_log_line("106391 node-226 node temperature 15.571 1 ambient=24"),
    ]
    trigger_sample = [
        parse_log_line(
            "460903 resourcemgmtdaeomon node-25 server subsys 1145552216 1 failed to configure resourcemgmt subsystem err = 10"
        )
    ]
    # Overly broad pattern that accidentally matches any digit or word
    broad_rule = Rule(
        rule_id="RULE-BAD-01",
        subsystem="server",
        event_type="subsys",
        pattern=r".*",  # Dangerous wildcard!
        resolution=ProblemResolution(
            error_summary="bad",
            root_cause="bad",
            severity="LOW",
            remediation_steps=[],
            citations_and_reasons=[],
        ),
    )
    with pytest.raises(GuardrailError, match="false positive"):
        GuardrailValidator.validate_rule(
            broad_rule,
            trigger_samples=trigger_sample,
            telemetry_samples=telemetry_sample,
        )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_guardrails.py -v`  
Expected: FAIL with ModuleNotFoundError.

- [ ] **Step 3: Implement `GuardrailValidator` and `GuardrailError` in `src/guardrails.py`**

Enforce strict programmatic checks:
1. ReDoS timeout & syntax validity via regex compile test.
2. Positive match verification (`match_count > 0` on `trigger_samples`).
3. Telemetry anti-match verification: immediate failure if any `telemetry_samples` match.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_guardrails.py -v`  
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/guardrails.py tests/test_guardrails.py
git commit -m "feat(guardrails): implement deterministic code safety guardrails"
```

---

### Task 4: Subsystem Knowledge Base & Real-Time SRE Agent (Actual Code)

**Files:**
- Create: `src/knowledge/docs_db.py`
- Modify: `src/agent.py` -> `src/sre_agent.py`
- Test: `tests/test_sre_agent.py`

**Interfaces:**
- Produces:
  - `query_subsystem_docs(subsystem: str, keyword: str = "") -> str`
  - `create_sre_agent(settings: Settings) -> Agent[Settings, ProblemResolution]`
  - `resolve_incident(event: LogEvent, agent: Agent, settings: Settings) -> ProblemResolution`

- [ ] **Step 1: Write failing tests for Subsystem Knowledge Base & SRE Agent**

```python
# tests/test_sre_agent.py
from src.knowledge.docs_db import query_subsystem_docs
from src.rules.normalizer import parse_log_line
from src.sre_agent import create_sre_agent


def test_knowledge_base_covers_lanl_subsystems():
    res = query_subsystem_docs("clusterfilesystem", "panic")
    assert "ServerFileSystem domain panic" in res
    assert "Remediation" in res


def test_sre_agent_creation():
    agent = create_sre_agent()
    assert agent is not None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_sre_agent.py -v`  
Expected: FAIL with ModuleNotFoundError.

- [ ] **Step 3: Implement `src/knowledge/docs_db.py` and `src/sre_agent.py`**

Populate comprehensive runbooks in `docs_db.py` covering all 44 LANL HPC categories (`resourcemgmtdaeomon`, `clusterfilesystem`, `unix.hw`, `switch_module`, `boot_cmd`, `node`), and attach the tool to the Pydantic-AI agent.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_sre_agent.py -v`  
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/knowledge/ src/sre_agent.py tests/test_sre_agent.py
git commit -m "feat(agent): enrich subsystem knowledge base and configure SRE agent"
```

---

### Task 5: Post-Mortem Consolidation Agent & Approval Gate (Actual Code)

**Files:**
- Create: `src/post_mortem_agent.py`
- Test: `tests/test_post_mortem.py`

**Interfaces:**
- Consumes: `RuleTreeEngine` from `src/rules/engine.py`, `GuardrailValidator` from `src/guardrails.py`
- Produces:
  - `RuleCandidate(subsystem: str, event_type: str, pattern: str, resolution: ProblemResolution)`
  - `OperatorApprovalGate.request_approval(candidate: RuleCandidate, metrics: dict) -> bool`
  - `PostMortemConsolidationEngine.process_incident(event: LogEvent, resolution: ProblemResolution) -> Rule | None`

- [ ] **Step 1: Write failing tests for PostMortemConsolidationEngine**

```python
# tests/test_post_mortem.py
from src.agent import ProblemResolution
from src.post_mortem_agent import OperatorApprovalGate, PostMortemConsolidationEngine
from src.rules.engine import RuleTreeEngine
from src.rules.models import RuleCandidate
from src.rules.normalizer import parse_log_line


def test_post_mortem_auto_approval():
    engine = RuleTreeEngine()
    pm = PostMortemConsolidationEngine(rule_engine=engine, auto_approve=True)
    event = parse_log_line(
        "70000 node-D0 clusterfilesystem fdmn.panic 1091728051 1 ServerFileSystem: An ServerFileSystem domain panic has occurred on storage421"
    )
    resolution = ProblemResolution(
        error_summary="Filesystem domain panic",
        root_cause="Storage domain panic",
        severity="CRITICAL",
        remediation_steps=["Check storage controller", "Run domain recovery"],
        citations_and_reasons=["ClusterFS Manual Section 8"],
    )
    new_rule = pm.process_incident(event, resolution)
    assert new_rule is not None
    assert engine.match(event) is not None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_post_mortem.py -v`  
Expected: FAIL with ModuleNotFoundError.

- [ ] **Step 3: Implement `PostMortemConsolidationEngine` and `OperatorApprovalGate`**

Implement candidate rule synthesis, wire to `GuardrailValidator`, prompt operator in interactive mode (or pass if `auto_approve=True`), and register newly approved rules into `RuleTreeEngine`.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_post_mortem.py -v`  
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/post_mortem_agent.py tests/test_post_mortem.py
git commit -m "feat(consolidation): implement post-mortem rule consolidation and approval gate"
```

---

### Task 6: Curated Golden Evaluation Benchmark & Evaluation Suite (Actual Code & Data)

**Files:**
- Create: `data/golden_eval.json`
- Create: `src/evaluation.py`
- Test: `tests/test_evaluation.py`

**Interfaces:**
- Produces:
  - `EvaluationBenchmark(dataset_path: Path)`
  - `evaluate_system(benchmark_path: Path, rule_engine: RuleTreeEngine) -> EvaluationMetrics`
  - `EvaluationMetrics(precision: float, recall: float, f1: float, fast_path_hit_rate: float)`

- [ ] **Step 1: Write failing test for Evaluation Benchmark**

```python
# tests/test_evaluation.py
from pathlib import Path
from src.evaluation import evaluate_system
from src.rules.engine import RuleTreeEngine


def test_evaluation_benchmark_runs():
    engine = RuleTreeEngine()
    metrics = evaluate_system(Path("data/golden_eval.json"), engine)
    assert metrics.precision >= 0.0
    assert metrics.recall >= 0.0
    assert 0.0 <= metrics.f1 <= 1.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_evaluation.py -v`  
Expected: FAIL with FileNotFoundError or missing module.

- [ ] **Step 3: Generate `data/golden_eval.json` and implement `src/evaluation.py`**

Construct `data/golden_eval.json` containing 150 diverse labeled log instances (normal telemetry vs critical errors) from the train, val, and test splits. Implement evaluation harness calculating Precision, Recall, F1, and fast-path vs agent breakdown.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_evaluation.py -v`  
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add data/golden_eval.json src/evaluation.py tests/test_evaluation.py
git commit -m "feat(eval): add golden evaluation dataset and evaluation harness"
```

---

### Task 7: Unified CLI & Interactive Demo Runner (Actual Code)

**Files:**
- Modify: `main.py`
- Test: `tests/test_cli.py`

**Interfaces:**
- Produces:
  - CLI command `main.py demo`: runs the end-to-end interactive workflow.
  - CLI command `main.py evaluate`: outputs quantitative precision/recall benchmark.

- [ ] **Step 1: Write failing CLI test**

```python
# tests/test_cli.py
import pytest
from main import run_evaluation_cli


def test_evaluation_cli_headless():
    exit_code = run_evaluation_cli(auto_approve=True)
    assert exit_code == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_cli.py -v`  
Expected: FAIL with AttributeError.

- [ ] **Step 3: Update `main.py` with `demo` and `evaluate` CLI modes**

Wire the complete lifecycle: parsing, telemetry filtering, rule engine lookup, SRE agent fallback, post-mortem rule synthesis, and benchmark reporting.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_cli.py -v`  
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add main.py tests/test_cli.py
git commit -m "feat(cli): wire unified demo and evaluation CLI entry points"
```

---

### Task 8: Ingestion Architecture Design & Submission Report (Design & Documentation Only)

**Files:**
- Create: `report.md`
- Create: `README.md`

**Deliverables:**
- **`report.md`**:
  - Detailed Data Ingestion Architecture Diagram (Kafka, Fluentbit, ClickHouse/Snowflake) as requested by instructions ("don't implement, just diagram").
  - System Architecture & Thought Process: Explaining the two-tier fast/slow path, the two-agent separation of concerns, and future-proofing.
  - Dataset analysis findings (LANL System 20, chronological drift, lack of official labels).
  - Quantitative benchmark results (Precision, Recall, F1, cost/latency savings).
  - Suggested future improvements (e.g. streaming window correlation, distributed vector store).
- **`README.md`**:
  - Exact prerequisites and execution steps (`uv run python main.py demo`, `uv run python main.py evaluate`, `uv run pytest`).

- [ ] **Step 1: Create `report.md` with complete ingestion architecture diagram and analysis**
- [ ] **Step 2: Update `README.md` with clear instructions to run and test**
- [ ] **Step 3: Run complete static check and test suite**

Run: `uv run ruff check . && uv run ruff format . && uv run ty check && uv run pytest`  
Expected: All checks and tests pass with 0 warnings/errors.

- [ ] **Step 4: Commit**

```bash
git add report.md README.md
git commit -m "docs: complete submission report and build instructions"
```
