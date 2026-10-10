# HPC Log Problem Resolution System Implementation Plan (Lean Scope)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a lean, high-performance problem resolution system for HPC logs that filters telemetry at wire speed, resolves known errors via a fast-path Rule Tree cache, invokes a Pydantic-AI SRE agent for novel errors with subsystem documentation tools, caches new resolutions for future-proofing, and documents the enterprise ingestion and post-mortem consolidation architecture in `report.md`.

**Architecture:** A two-tier pipeline:
1. **Fast Path**: Raw logs are parsed into typed `LogEvent` records. Routine telemetry (`ambient=*`, `normal`, `running`, `Link ok`) is filtered instantly without LLM overhead. Actionable logs query an indexed Rule Tree cache for sub-millisecond, zero-cost resolution.
2. **Slow Path**: Unseen errors route to a Pydantic-AI SRE agent equipped with a cluster subsystem knowledge base tool. The agent produces structured, cited `ProblemResolution` outputs, which are automatically cached into the Rule Tree to ensure future-proofing.
3. **Design-Only Architectures**: Technology-agnostic enterprise ingestion (consume message queue from data lake) and the offline Post-Mortem Rule Consolidation Agent are fully diagrammed and specified in `report.md`.

**Tech Stack:** Python 3.12+, Pydantic-AI, Pydantic v2, AsyncOpenAI (NVIDIA NIM), Pytest, Ruff, Ty.

**Spec:** [`docs/superpowers/specs/2026-10-10-hpc-problem-resolution-system-design.md`](file:///c:/Code/mave/docs/superpowers/specs/2026-10-10-hpc-problem-resolution-system-design.md)

---

## Deliverable Separation Matrix

| Component | Nature | Destination | Description |
| :--- | :--- | :--- | :--- |
| **Ingestion Pipeline** | **Design Only** | `report.md` | Tech-stack agnostic streaming ingestion architecture diagram (consume message queue from data lake) as explicitly instructed ("don't implement, just diagram"). |
| **Post-Mortem Consolidation Agent** | **Design Only** | `report.md` | Offline multi-agent consolidation loop, cross-incident clustering, and human approval workflow diagrammed and documented in the submission report. |
| **Log Normalizer & Telemetry Filter** | **Actual Code** | `src/rules/normalizer.py`, `src/rules/telemetry_filter.py` | Wire-speed parser separating routine telemetry from actionable events. |
| **Rule Tree & Resolution Cache** | **Actual Code** | `src/rules/models.py`, `src/rules/engine.py` | Hierarchical `(subsystem, event)` fast-path matcher and dynamic resolution cache. |
| **Subsystem Knowledge Base** | **Actual Code** | `src/knowledge/docs_db.py` | Domain runbooks covering the 44 cluster subsystem failure modes (from the Los Alamos National Laboratory benchmark). |
| **Production SRE Agent** | **Actual Code** | `src/agent.py` | Pydantic-AI agent querying domain documentation and returning cited `ProblemResolution`. |
| **Pipeline & In-Flight Burst Suppressor** | **Actual Code** | `src/pipeline.py` | Two-tier orchestrator with duplicate suppression for bursting error storms. |
| **Golden Benchmark & Evaluation** | **Actual Code & Data** | `data/golden_eval.json`, `src/evaluation.py` | 150 diverse labeled logs measuring Precision, Recall, F1, and fast-path vs agent breakdown. |
| **Unified CLI (`demo` & `evaluate`)** | **Actual Code** | `main.py` | End-to-end interactive demo walkthrough and automated evaluation runner. |
| **Submission Report & Run Guide** | **Actual Docs** | `report.md`, `README.md` | Formal submission documentation with architecture diagrams, thought process, and run commands. |

---

## Global Constraints

- **Python Version**: Python 3.12+ with strict typing (`uv run ty check`) and formatting (`uv run ruff check .` and `uv run ruff format .`).
- **Telemetry Invariance**: Zero false alarms permitted on normal cluster telemetry (`ambient=*`, `normal`, `running`, `Link ok`).
- **Token Efficiency**: 0 LLM calls for routine telemetry and known/cached errors; novel errors trigger exactly 1 SRE agent call per burst.
- **Reproducibility**: All unit tests run via `uv run pytest`; benchmark metrics compute deterministically in seconds.

---

## Review Focus

1. **Missing or Corrupted Fields**: Parser must handle empty nodes, missing timestamps, and escaped backslashes (`\ `) gracefully (Task 1).
2. **Telemetry Leakage**: Normal heartbeat logs must never leak into the SRE agent (Task 1 & Task 6).
3. **Burst Deduplication**: Repetitive novel errors occurring consecutive times (e.g. 25x `err = 10`) must trigger only 1 LLM query (Task 2 & Task 6).
4. **Citation Grounding**: Every SRE agent output must cite specific documentation and reasons (Task 4).
5. **Dynamic Cache Verification**: The second arrival of an unseen error must hit the fast path without re-invoking the LLM (Task 2 & Task 6).

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


def test_parse_edge_case_empty_node():
    raw = "48124  clusterfilesystem fdmn.full 1076621893 1 ServerFileSystem: ServerFileSystem domain root_domain is full"
    event = parse_log_line(raw)
    assert event.log_id == "48124"
    assert event.subsystem == "clusterfilesystem"
    assert event.event_type == "fdmn.full"


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
Expected: FAIL with ModuleNotFoundError.

- [ ] **Step 3: Implement `LogEvent`, `parse_log_line`, and `TelemetryFilter`**

In `src/rules/normalizer.py`, parse fields with positional awareness for varying headers. In `src/rules/telemetry_filter.py`, match benign keywords (`ambient=`, `normal`, `Link ok`, `Fan speeds`, `status running`, `responding`) to drop telemetry.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_normalizer.py -v`  
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/rules/models.py src/rules/normalizer.py src/rules/telemetry_filter.py tests/test_normalizer.py
git commit -m "feat(rules): add LogEvent normalizer and wire-speed telemetry filter"
```

---

### Task 2: Fast-Path Rule Tree Engine & Resolution Cache (Actual Code)

**Files:**
- Create: `src/rules/engine.py`
- Modify: `src/rules/models.py`
- Test: `tests/test_rule_engine.py`

**Interfaces:**
- Consumes: `LogEvent` from `src/rules/models.py`
- Produces:
  - `Rule(rule_id: str, subsystem: str, event_type: str, pattern: str, resolution: ProblemResolution)`
  - `RuleTreeEngine.match(event: LogEvent) -> ProblemResolution | None`
  - `RuleTreeEngine.add_rule(rule: Rule) -> None`
  - `RuleTreeEngine.cache_resolution(event: LogEvent, resolution: ProblemResolution) -> None`

- [ ] **Step 1: Write failing tests for RuleTreeEngine and dynamic caching**

```python
# tests/test_rule_engine.py
from src.agent import ProblemResolution
from src.rules.engine import RuleTreeEngine
from src.rules.models import Rule
from src.rules.normalizer import parse_log_line


def test_rule_tree_match_preconfigured_rule():
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


def test_rule_tree_dynamic_cache_resolution():
    engine = RuleTreeEngine()
    event = parse_log_line(
        "460903 resourcemgmtdaeomon node-25 server subsys 1145552216 1 failed to configure resourcemgmt subsystem err = 10"
    )
    assert engine.match(event) is None

    res = ProblemResolution(
        error_summary="Resource daemon init failed",
        root_cause="Daemon unable to configure subsystem",
        severity="CRITICAL",
        remediation_steps=["Restart daemon", "Check node controller"],
        citations_and_reasons=["Resourcemgmt docs"],
    )
    engine.cache_resolution(event, res)

    # Next occurrence hits the cache
    matched = engine.match(event)
    assert matched is not None
    assert matched.error_summary == "Resource daemon init failed"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_rule_engine.py -v`  
Expected: FAIL with missing classes.

- [ ] **Step 3: Implement `RuleTreeEngine` with hierarchical index and dynamic cache**

In `src/rules/engine.py`, store rules in a nested dictionary `_tree[(subsystem, event_type)]` and support parameterized regex matching alongside an exact template hash cache.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_rule_engine.py -v`  
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/rules/engine.py src/rules/models.py tests/test_rule_engine.py
git commit -m "feat(rules): implement RuleTreeEngine with dynamic resolution caching"
```

---

### Task 3: Subsystem Knowledge Base & Runbooks (Actual Code)

**Files:**
- Create: `src/knowledge/docs_db.py`
- Test: `tests/test_docs_db.py`

**Interfaces:**
- Produces:
  - `query_subsystem_docs(subsystem: str, keyword: str = "") -> str`
  - `list_known_subsystems() -> list[str]`

- [ ] **Step 1: Write failing tests for Subsystem Knowledge Base**

```python
# tests/test_docs_db.py
from src.knowledge.docs_db import list_known_subsystems, query_subsystem_docs


def test_knowledge_base_covers_key_subsystems():
    subsystems = list_known_subsystems()
    assert "clusterfilesystem" in subsystems
    assert "resourcemgmtdaeomon" in subsystems
    assert "unix.hw" in subsystems
    assert "switch_module" in subsystems


def test_query_subsystem_returns_actionable_runbook():
    doc = query_subsystem_docs("clusterfilesystem", "panic")
    assert "ServerFileSystem domain panic" in doc
    assert "Remediation" in doc
    assert "Citation" in doc
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_docs_db.py -v`  
Expected: FAIL with ModuleNotFoundError.

- [ ] **Step 3: Implement `src/knowledge/docs_db.py` with LANL HPC Runbooks**

Create comprehensive, domain-accurate documentation for LANL HPC subsystems:
- `resourcemgmtdaeomon`: Config error 10, node controller timeout.
- `clusterfilesystem`: `fdmn.panic`, `fdmn.full`, unserved storage domains.
- `unix.hw`: `alt0` network interface down, hardware state change unavailable.
- `switch_module`: PSU failover, temperature warnings, link flapping.
- `boot_cmd`: `CMF-Port` disconnection, node state determination errors, console busy.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_docs_db.py -v`  
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/knowledge/docs_db.py tests/test_docs_db.py
git commit -m "feat(knowledge): implement LANL HPC subsystem knowledge base and runbooks"
```

---

### Task 4: Production SRE Resolution Agent (Actual Code)

**Files:**
- Modify: `src/agent.py`
- Test: `tests/test_agent.py`

**Interfaces:**
- Consumes: `query_subsystem_docs` from `src/knowledge/docs_db.py`, `Settings` from `src/config.py`
- Produces:
  - `ProblemResolution(error_summary: str, root_cause: str, severity: str, remediation_steps: list[str], citations_and_reasons: list[str])`
  - `create_agent(settings: Settings | None) -> Agent[Settings, ProblemResolution]`
  - `async analyze_log(log_event: LogEvent, agent: Agent, settings: Settings) -> ProblemResolution`

- [ ] **Step 1: Write failing test for SRE Agent tool invocation**

```python
# tests/test_agent.py
import pytest
from src.agent import ProblemResolution, create_agent
from src.rules.normalizer import parse_log_line


def test_agent_tool_registry():
    agent = create_agent()
    # Check query_subsystem_docs is registered as a tool
    tool_names = [t.name for t in agent._function_tools.values()]
    assert "query_subsystem_docs" in tool_names


def test_problem_resolution_schema_validation():
    res = ProblemResolution(
        error_summary="Interface alt0 down",
        root_cause="NIFF detected network loss",
        severity="HIGH",
        remediation_steps=["Check cable", "Restart alt0"],
        citations_and_reasons=["LANL Unix.hw Runbook Sec 3.1"],
    )
    assert res.severity == "HIGH"
    assert len(res.remediation_steps) == 2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_agent.py -v`  
Expected: FAIL if tools or helpers are missing.

- [ ] **Step 3: Update `src/agent.py` with enhanced tools and system prompt**

Wire `query_subsystem_docs` directly from `src/knowledge/docs_db.py` into the Pydantic-AI agent. Provide helper `analyze_log(event, agent, settings)` that constructs a rich incident prompt with log fields (`node`, `subsystem`, `message`).

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_agent.py -v`  
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/agent.py tests/test_agent.py
git commit -m "feat(agent): wire production SRE agent with rich knowledge tools"
```

---

### Task 5: Curated Golden Evaluation Benchmark & Evaluation Harness (Actual Code & Data)

**Files:**
- Create: `data/golden_eval.json`
- Create: `src/evaluation.py`
- Test: `tests/test_evaluation.py`

**Interfaces:**
- Produces:
  - `data/golden_eval.json`: 150 labeled log lines (`is_error: bool`, `expected_severity: str`, `category: str`).
  - `evaluate_benchmark(benchmark_path: Path, rule_engine: RuleTreeEngine) -> BenchmarkMetrics`
  - `BenchmarkMetrics(total: int, precision: float, recall: float, f1: float, fast_path_hits: int, telemetry_filtered: int)`

- [ ] **Step 1: Write failing tests for Evaluation Harness**

```python
# tests/test_evaluation.py
from pathlib import Path
from src.evaluation import evaluate_benchmark
from src.rules.engine import RuleTreeEngine


def test_evaluation_benchmark_executes():
    engine = RuleTreeEngine()
    metrics = evaluate_benchmark(Path("data/golden_eval.json"), engine)
    assert metrics.total > 50
    assert 0.0 <= metrics.precision <= 1.0
    assert 0.0 <= metrics.recall <= 1.0
    assert metrics.telemetry_filtered > 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_evaluation.py -v`  
Expected: FAIL with missing file/module.

- [ ] **Step 3: Generate `data/golden_eval.json` and implement `src/evaluation.py`**

Construct `data/golden_eval.json` containing 150 diverse labeled log instances across normal telemetry and failures (PSU, filesystem panic, interface down, daemon error 10, temperature warnings). Implement evaluation metrics calculation in `src/evaluation.py`.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_evaluation.py -v`  
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add data/golden_eval.json src/evaluation.py tests/test_evaluation.py
git commit -m "feat(eval): add golden evaluation benchmark and metrics harness"
```

---

### Task 6: Stream Pipeline Orchestrator & Unified CLI (Actual Code)

**Files:**
- Create: `src/pipeline.py`
- Modify: `main.py`
- Test: `tests/test_pipeline.py`

**Interfaces:**
- Produces:
  - `LogPipeline(rule_engine: RuleTreeEngine, agent: Agent, settings: Settings)`
  - `async LogPipeline.process_log(raw_line: str) -> PipelineResult(status: str, resolution: ProblemResolution | None, cost_usd: float)`
  - CLI: `main.py demo` (walks through telemetry -> fast path -> agent -> cache hit)
  - CLI: `main.py evaluate` (computes precision/recall and prints benchmark report)

- [ ] **Step 1: Write failing tests for LogPipeline**

```python
# tests/test_pipeline.py
import pytest
from src.agent import ProblemResolution, create_agent
from src.config import get_settings
from src.pipeline import LogPipeline
from src.rules.engine import RuleTreeEngine


@pytest.mark.asyncio
async def test_pipeline_filters_telemetry():
    engine = RuleTreeEngine()
    pipeline = LogPipeline(rule_engine=engine, agent=None)
    result = await pipeline.process_log("2560243 gige3 gige temperature 1073198499 1 normal")
    assert result.status == "TELEMETRY_FILTERED"
    assert result.resolution is None


@pytest.mark.asyncio
async def test_pipeline_hits_fast_path():
    engine = RuleTreeEngine()
    event_raw = "98745 Interconnect-0N00 switch_module psu 1110481498 1 PSU status ( off on )"
    # Seed rule
    engine.cache_resolution(
        event_raw,
        ProblemResolution(
            error_summary="PSU error",
            root_cause="PSU off",
            severity="HIGH",
            remediation_steps=["Swap"],
            citations_and_reasons=["Manual"],
        ),
    )
    pipeline = LogPipeline(rule_engine=engine, agent=None)
    result = await pipeline.process_log(event_raw)
    assert result.status == "FAST_PATH_HIT"
    assert result.resolution.severity == "HIGH"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_pipeline.py -v`  
Expected: FAIL with ModuleNotFoundError.

- [ ] **Step 3: Implement `src/pipeline.py` and update `main.py`**

In `src/pipeline.py`, wire the two-tier triage:
1. `TelemetryFilter`: returns `TELEMETRY_FILTERED`.
2. `RuleTreeEngine.match`: returns `FAST_PATH_HIT`.
3. In-flight debounce check: if duplicate novel error, returns `IN_FLIGHT_ATTACHED`.
4. Fallback: calls `agent.run()`, caches result in `RuleTreeEngine`, returns `AGENT_RESOLVED`.
In `main.py`, implement `demo` and `evaluate` CLI options.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_pipeline.py -v`  
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/pipeline.py main.py tests/test_pipeline.py
git commit -m "feat(pipeline): implement stream pipeline and unified demo/eval CLI"
```

---

### Task 7: Architectural Submission Report & Build Documentation (Design & Docs Only)

**Files:**
- Create: `report.md`
- Create: `README.md`

**Deliverables:**
- **`report.md`**:
  - **Data Ingestion Architecture Diagram & Writeup**: Tech-stack agnostic streaming ingestion architecture diagram (cluster log collectors $\rightarrow$ message queue $\rightarrow$ data lake / analytical storage) as explicitly instructed ("don't implement, just diagram").
  - **Post-Mortem Consolidation Agent Architecture Diagram & Writeup**: Offline multi-agent consolidation loop, cross-incident clustering, and human approval workflow diagrammed and documented in the report.
  - **System Thought Process & Two-Tier Trade-Offs**: Analysis of wire-speed triage vs deep reasoning, token savings, and latency guarantees.
  - **Dataset Analysis Findings**: System 20 HPC cluster dataset analysis (44 subsystem pairs, lack of official labels, chronological drift).
  - **Quantitative Evaluation Results**: Precision, Recall, F1, and cost breakdown from `main.py evaluate`.
  - **Future Improvements**: Distributed vector stores, cross-node temporal anomaly detection.
- **`README.md`**:
  - Exact setup, verification, and run commands (`uv run python main.py demo`, `uv run python main.py evaluate`, `uv run pytest`).

- [ ] **Step 1: Write `report.md` with complete diagrams, analysis, and benchmark outcomes**
- [ ] **Step 2: Write `README.md` with exact instructions to execute and test**
- [ ] **Step 3: Run complete static check and test suite**

Run: `uv run ruff check . && uv run ruff format . && uv run ty check && uv run pytest`  
Expected: All checks and tests pass with 0 errors.

- [ ] **Step 4: Commit**

```bash
git add report.md README.md
git commit -m "docs: add comprehensive submission report and build instructions"
```
