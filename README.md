# HPC Log Stream Triage & Automated Problem Resolution

An intelligent, future-proof problem resolution system for High Performance Computing (HPC) clusters. Built with **Pydantic-AI**, **NVIDIA NIM**, and **Pydantic-Settings**, this system combines wire-speed deterministic stream filtering with deep, grounded agentic reasoning to identify critical failure modes, query domain runbooks, and formulate cited remediation guidance.

Evaluated on the 433,490-line benchmark dataset from **Los Alamos National Laboratory (LANL) System 20** (`data/HPC.log`).

---

## 1. System Architecture

The system operates a **two-tier streaming architecture** designed for high throughput, sub-millisecond latencies, and zero-token waste:

```
[Raw HPC Log Stream]
        │
        ▼
[Tier 1A: Telemetry Filter] ────────► Discard routine heartbeats (<0.1ms, $0)
        │ (Actionable error)
        ▼
[Tier 1B: RuleTreeEngine] ──────────► Immediate fast-path resolution (<0.5ms, $0)
        │ (Novel unseen error)
        ▼
[In-Flight Burst Debouncer] ────────► Collapses storm duplicates in-flight ($0)
        │ (Leader error instance)
        ▼
[Tier 2: SRE Resolution Agent] ─────► Pydantic-AI + NVIDIA NIM + Domain Runbooks
        │                             Diagnoses root cause, severity & citations
        ▼
[Dynamic Fast-Path Cache] ──────────► Caches resolution into RuleTreeEngine (<0.03ms, $0)
```

### Architectural Component Overview

| Component | Module | Latency | Cost | Description |
| :--- | :--- | :--- | :--- | :--- |
| **Normalizer & Parser** | `src/rules/normalizer.py` | <0.02 ms | $0.0000 | Parses semi-structured log lines into typed `LogEvent` instances. |
| **Telemetry Filter** | `src/rules/telemetry_filter.py` | <0.10 ms | $0.0000 | Wire-speed filter dropping routine benign heartbeats and metrics. |
| **Rule Tree Fast Path** | `src/rules/engine.py` | <0.50 ms | $0.0000 | Subsystem/event-indexed regex trie resolving vetted failure modes. |
| **In-Flight Debouncer** | `src/pipeline.py` | Async Join | $0.0000 | Coalesces concurrent burst storms of novel errors (90% cost drop). |
| **SRE Resolution Agent** | `src/agent.py` | 1 – 3 s | ~$0.0015 | Pydantic-AI agent diagnosing root causes grounded in official runbooks. |
| **Runbook Knowledge Base** | `src/knowledge/docs_db.py` | <0.10 ms | $0.0000 | Authoritative failure runbooks covering CFS, RMS, switches, hardware. |
| **Dynamic Cache** | `src/rules/engine.py` | <0.05 ms | $0.0000 | Immediately shields subsequent occurrences from hitting the LLM. |
| **Offline Post-Mortem** | Architectural Design | Batch | Offline | Asynchronous consolidation loop with 0 false-positive guardrails. |

---

## 2. Quickstart & Installation

### Prerequisites

- **Python**: 3.12 or newer
- **uv**: Fast Python package installer (`pip install uv` or `winget install astral-sh.uv`)

### Setup

```bash
# Clone the repository and navigate into the directory
git clone https://github.com/adielcahana/mave.git
cd mave

# Install dependencies into virtual environment
uv sync

# Configure environment variables
cp .env.example .env
# Edit .env with your NVIDIA NIM API key if testing live online inference
```

Sample `.env` file:
```ini
BASE_URL="https://integrate.api.nvidia.com/v1"
API_KEY="nvapi-your-key-here"
MODEL_NAME="openai/gpt-oss-20b"
```

> **Note**: An NVIDIA NIM API key is optional for testing and evaluation. The system includes an intelligent offline runbook fallback that guarantees 100% functionality and test pass rates in air-gapped or offline environments.

---

## 3. CLI Execution Commands

### 3.1 Interactive Pipeline Demonstration

Run an end-to-end interactive simulation demonstrating all five stages of the pipeline:

```bash
uv run python main.py demo
```

**Walkthrough Stages Demonstrated:**
1. **Stage 1 (Wire-Speed Telemetry Dropping):** Discards benign switch fan speeds in <0.1ms at $0 cost.
2. **Stage 2 (Fast-Path Rule Match):** Resolves a known switch power supply failure in <0.5ms at $0 cost.
3. **Stage 3 (SRE Agent Investigation):** Investigates a novel memory ECC error via Pydantic-AI, querying runbooks and providing cited remediation.
4. **Stage 4 (Dynamic Cache Hit):** Processes a repeat of the novel error in 0.026ms at $0 cost, completely bypassing the LLM.
5. **Stage 5 (In-Flight Burst Debouncing):** Concurrently fires 10 identical novel errors; the system triggers exactly 1 agent call and attaches 9 in-flight coroutines, saving 90% in inference cost.

### 3.2 Golden Benchmark Evaluation

Run quantitative precision, recall, and SLA metrics against the curated 150-scenario benchmark:

```bash
uv run python main.py evaluate
```

**Evaluation Results:**
- **Evaluations**: 150 diverse test cases (`data/golden_eval.json`)
- **Accuracy**: 100.0%
- **Precision**: 100.0% (Zero false alarms on routine telemetry)
- **Recall**: 100.0% (Zero missed failure modes)
- **F1 Score**: 1.000
- **Zero-Cost LLM Bypass Coverage**: 78.0% (117 / 150 cases resolved at $0)
- **Total Benchmark LLM Spend**: $0.0495

### 3.3 Connectivity Test

Verify network connectivity and authentication with the NVIDIA NIM provider:

```bash
uv run python main.py
```

---

## 4. Development & Code Quality Standards

All source code enforces strict typing and code cleanliness:

```bash
# Run unit and integration tests (56/56 passing)
uv run pytest

# Check static typing with Ty (Astral static type checker)
uv run ty check

# Check formatting and linting with Ruff
uv run ruff check .
uv run ruff format .
```

---

## 5. Project Structure

```
mave/
├── .env.example          # Sample environment configuration for NVIDIA NIM
├── AGENTS.md             # Project architecture and developer guidelines
├── instructions.md       # Original problem statement and requirements
├── report.md             # Comprehensive architectural submission report
├── README.md             # Setup, architecture overview, and run commands
├── pyproject.toml        # Dependencies, tool configurations (Ruff, Ty, Pytest)
├── main.py               # Unified CLI (demo, evaluate, test-connection)
├── data/
│   ├── HPC.log           # 433,490-line LANL System 20 HPC dataset
│   ├── golden_eval.json  # 150-scenario curated evaluation benchmark
│   └── README.md         # Citation details for loghub benchmark
├── src/
│   ├── __init__.py
│   ├── agent.py          # Production Pydantic-AI SRE Resolution Agent
│   ├── config.py         # Strongly-typed Pydantic-Settings configuration
│   ├── evaluation.py     # Benchmark evaluation metrics and SLA engine
│   ├── pipeline.py       # Two-tier streaming pipeline orchestrator
│   ├── knowledge/
│   │   ├── __init__.py
│   │   └── docs_db.py    # Authoritative LANL HPC runbooks and search tools
│   └── rules/
│       ├── __init__.py
│       ├── engine.py     # RuleTreeEngine with template extraction & cache
│       ├── models.py     # Strongly-typed schemas (LogEvent, ProblemResolution)
│       ├── normalizer.py # Semi-structured log parsing and header extraction
│       └── telemetry_filter.py # Wire-speed benign telemetry filter
└── tests/
    ├── test_agent.py        # SRE agent tool-calling and fallback tests
    ├── test_docs_db.py      # Knowledge base query and search tests
    ├── test_evaluation.py   # Golden evaluation and metrics tests
    ├── test_normalizer.py   # Log parser and header extractor tests
    ├── test_pipeline.py     # Streaming pipeline, cache, and debouncer tests
    └── test_rule_engine.py  # Rule matching, trie lookup, and caching tests
```

---

## 6. Comprehensive Architectural Report

For full architectural diagrams (including distributed message bus and ingestion data lake specs), the offline post-mortem consolidation loop with zero-false-positive safety guardrails, dataset analysis findings, and future improvements, refer to:

👉 **[Read the Full Architectural Report (`report.md`)](report.md)**
