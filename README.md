# HPC Log Stream Triage & Automated Problem Resolution

A problem resolution system for High Performance Computing (HPC) clusters. Built with **Pydantic-AI** and **NVIDIA NIM**, this system combines deterministic stream filtering with grounded agentic reasoning to identify critical failure modes, query domain runbooks, and formulate cited remediation guidance.

Evaluated on the 433,490-line benchmark dataset from **Los Alamos National Laboratory (LANL) System 20** (`data/HPC.log`).

---

## 1. System Architecture

The system operates a **two-tier architecture** designed for high throughput, sub-millisecond latencies, and zero-token waste when processing cluster logs:

```
[Incoming HPC Log Stream]
        │
        ▼
[Tier 1A: Telemetry Filter] ────────► Discard routine heartbeats (<0.1ms, $0)
        │ (Actionable error)
        ▼
[Tier 1B: RuleTreeEngine] ──────────► Immediate fast-path resolution (<0.5ms, $0)
        │ (Novel unseen error)
        ▼
[Circuit Breaker / Debouncer] ──────► Guardrail shielding against API outages & storms
        │ (Incident investigation)
        ▼
[Tier 2: SRE Resolution Agent] ─────► Pydantic-AI + NVIDIA NIM + Domain Runbooks
        │                             Diagnoses root cause, severity & citations
        ▼
[Dynamic Fast-Path Cache] ──────────► Caches resolution into RuleTreeEngine (<0.05ms, $0)
```

### Architectural Component Overview

| Component | Module | Latency | Cost | Description |
| :--- | :--- | :--- | :--- | :--- |
| **Normalizer & Parser** | `src/rules/normalizer.py` | <0.02 ms | $0.0000 | Parses semi-structured log lines into typed `LogEvent` instances. |
| **Telemetry Filter** | `src/rules/telemetry_filter.py` | <0.10 ms | $0.0000 | Wire-speed filter dropping routine benign heartbeats and metrics. |
| **Rule Tree Fast Path** | `src/rules/engine.py` | <0.50 ms | $0.0000 | Subsystem/event-indexed regex trie resolving vetted failure modes. |
| **Circuit Breaker** | `src/agent.py` | <0.01 ms | $0.0000 | Fails fast to local runbooks during API outages or air-gapped runs. |
| **SRE Resolution Agent** | `src/agent.py` | 1 – 3 s | ~$0.0015 | Pydantic-AI agent diagnosing root causes grounded in official runbooks. |
| **Runbook Knowledge Base** | `src/knowledge/docs_db.py` | <0.10 ms | $0.0000 | Authoritative failure runbooks covering CFS, RMS, switches, hardware. |
| **Dynamic Cache** | `src/rules/engine.py` | <0.05 ms | $0.0000 | Immediately shields subsequent occurrences from hitting the LLM. |

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

> **Note**: An NVIDIA NIM API key is optional. The system includes an autonomous runbook fallback and circuit breaker that guarantee 100% functionality and test pass rates in air-gapped or offline environments.

---

## 3. CLI Execution

Ingest any HPC log file (`.log`, `.txt`, or `.json`) to generate quantitative classification statistics, throughput metrics, and grounded remedy recommendations:

```bash
# Ingest default benchmark file (data/golden_eval.json)
uv run python main.py

# Ingest raw cluster logs with an optional line limit
uv run python main.py data/HPC.log --limit 500

# Ingest any custom log stream
uv run python main.py path/to/cluster.log
```

### Output Provided

1. **Classification Statistics Dashboard**:
   - **Total Logs Processed**: Ingested stream count.
   - **Benign Telemetry**: Routine node telemetry and heartbeats filtered at wire speed (<0.1ms, $0 cost).
   - **Actionable Errors**: Total incident count requiring diagnosis and remediation.
   - **Automatically Annotated**: Resolved via sub-millisecond Fast-Path Rule Tree and dynamic cache (<0.5ms, $0 cost).
   - **Escalated to LLM**: Novel failure modes diagnosed via Pydantic-AI SRE Agent with runbook grounding.
   - **Zero-Cost LLM Bypass Rate**: Aggregate percentage of logs resolved without token expenditure.

2. **Execution Timing & Throughput Card**:
   - **Time to Results**: Total wall-clock elapsed time (e.g. `0.38 ms` for routine telemetry, `23.05 s` for full benchmark).
   - **Stream Throughput**: Processing speed in **logs/sec** (up to 50,000+ logs/sec).
   - **Mean Latency**: Per-log average processing time.

3. **Actionable Remedy Recommendations**:
   - Distinct cards for each unique failure mode across the stream (both Fast-Path rules and LLM-generated).
   - **Severity Badge**: `CRITICAL`, `HIGH`, `MEDIUM`, `LOW`, `INFO`.
   - **Routing Tier**: `FAST-PATH (AUTOMATED)` vs. `SRE AGENT (LLM)`.
   - **Occurrences Count**: Aggregated across duplicate events in the stream.
   - **Identified Root Cause**: Technical explanation of underlying component failure.
   - **Ordered Remediation Procedures**: Step-by-step procedures to recover cluster nodes safely.
   - **Official Runbook Citations**: Authoritative references to LANL cluster documentation.

---

## 4. Development & Code Quality Standards

All source code enforces strict typing, modern formatting, and rigorous test coverage:

```bash
# Run unit and integration tests (63/63 passing)
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
├── main.py               # Stream log analysis & automated problem resolution CLI
├── rules/
│   └── rules.json        # Persistent, vetted rule registry
├── data/
│   ├── HPC.log           # 433,490-line LANL System 20 HPC dataset
│   ├── golden_eval.json  # 150-scenario curated evaluation benchmark
│   ├── splits/           # Train/Val/Test dataset partitions
│   └── README.md         # Citation details for loghub benchmark
├── src/
│   ├── __init__.py
│   ├── agent.py          # Production Pydantic-AI SRE Agent & circuit breaker
│   ├── config.py         # Strongly-typed Pydantic-Settings configuration
│   ├── evaluation.py     # Benchmark evaluation metrics and SLA engine
│   ├── pipeline.py       # Two-tier streaming pipeline orchestrator
│   ├── knowledge/
│   │   ├── __init__.py
│   │   └── docs_db.py    # Authoritative LANL HPC runbooks and search tools
│   └── rules/
│       ├── __init__.py
│       ├── engine.py     # RuleTreeEngine with template extraction & cache
│       ├── models.py     # Strongly-typed schemas (LogEvent, ProblemResolution, Rule)
│       ├── normalizer.py # Semi-structured log parsing and header extraction
│       └── telemetry_filter.py # Wire-speed benign telemetry filter
└── tests/
    ├── test_agent.py        # SRE agent tool-calling and fallback tests
    ├── test_docs_db.py      # Knowledge base query and search tests
    ├── test_evaluation.py   # Golden evaluation and metrics tests
    ├── test_normalizer.py   # Log parser and header extractor tests
    ├── test_pipeline.py     # Streaming pipeline, cache, and debouncer tests
    └── test_rule_engine.py  # Rule matching, persistent rules, and trie tests
```

---

## 6. Comprehensive Architectural Report

For full architectural diagrams (including distributed message bus and ingestion data lake specs), the offline post-mortem consolidation loop with zero-false-positive safety guardrails, dataset analysis findings, and future improvements, refer to:

👉 **[Read the Full Architectural Report (`report.md`)](report.md)**
