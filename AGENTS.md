# AGENTS.md — HPC Log Stream Triage & Resolution System

## Problem Statement

High Performance Computing (HPC) clusters generate large-scale, semi-structured log streams across thousands of compute and service nodes. The objective of this project is to build an intelligent, low-latency, and cost-effective system for HPC log analysis and automated problem resolution that:
- **Wire-speed telemetry filtering**: Discards routine benign heartbeats and state notifications with zero token cost (<0.10 ms).
- **Fast-path deterministic resolution**: Resolves vetted failure modes and discards benign heartbeats in sub-milliseconds (<0.10 ms) using a format-agnostic, two-tier `RuleTreeEngine` (Parent Envelope Regex -> Child Message Regex).
- **Grounded LLM-powered incident diagnosis**: Escalates novel, unclassified failures to a Pydantic-AI SRE agent backed by NVIDIA NIM and authoritative LANL subsystem runbooks, generating cited, reproducible root-cause remediations.
- **Dynamic rule learning & caching**: Synthesizes regex envelope/message templates and registers dynamic fast-path tree entries from agent investigations to prevent repeat LLM invocations.

---

## Technical Stack & Frameworks

| Domain | Framework / Tool | Purpose |
| :--- | :--- | :--- |
| **Agent Framework** | [Pydantic-AI](https://ai.pydantic.dev/) | Agent orchestration, tool integration, dependency injection, and schema validation. |
| **Configuration** | [Pydantic-Settings](https://docs.pydantic.dev/latest/concepts/pydantic_settings/) | Strongly-typed environment variables and `.env` loading. |
| **LLM Provider** | [NVIDIA NIM](https://build.nvidia.com) | Enterprise inference API via OpenAI-compatible endpoint. |
| **Data Validation** | [Pydantic v2](https://docs.pydantic.dev/latest/) | Immutability, strict typing, and validation for log events, rules, and resolutions. |
| **Code Formatter & Linter** | [Ruff](https://docs.astral.sh/ruff/) | Formatting and linting enforcing Python 3.12+ standards. |
| **Static Type Checking** | [Ty](https://github.com/astral-sh/ty) | Static type analysis for strict type correctness. |
| **Test Runner** | [Pytest](https://docs.pytest.org/) | Comprehensive unit and integration test suite with async support (`anyio`). |
| **Package Management** | [uv](https://docs.astral.sh/uv/) | Fast virtual environment and dependency management. |

---

## Architecture Overview

```
[Incoming HPC Log Stream]
        │
        ▼
[Tier 1: Format-Agnostic Regex Tree]
   ├── Parent Envelope Regex (Syslog, K8s, LANL, Custom Daemons)
   │     │
   │     ├──► Benign Child Rule ───────► Discard telemetry (<0.05ms, $0)
   │     │
   │     └──► Actionable Child Rule ───► Fast-path resolution (<0.05ms, $0)
   │
   └── Novel / Unmatched Message
         │ (Incident escalation)
         ▼
[Tier 2: SRE Resolution Agent] ────────► Pydantic-AI + NVIDIA NIM + Domain Runbooks
         │                               Synthesizes Parent / Child Regex & Remediation
         ▼
[Dynamic Fast-Path Cache & Tree] ──────► Registers rule into Regex Tree (<0.05ms, $0)
```

### Core Modules (`src/`)

- `src/agent.py`: Production Pydantic-AI SRE agent with runbook search tools, prompt grounding, and `ModelRetry` regex output validation.
- `src/config.py`: Application settings (`Settings`) with `.env` loading and cached singleton access.
- `src/main.py`: Unified streaming pipeline CLI & benchmark evaluation entry point.
- `src/pipeline.py`: Two-tier streaming orchestrator (`LogPipeline`, `LogProcessingStats`, `PipelineResult`, `SourceName`) connecting ingestion, regex tree filtering/rules, and agent escalation.
- `src/evaluation.py`: Quantitative benchmark evaluation suite measuring classification accuracy, LLM bypass rate, and latency SLAs.
- `src/split_data.py`: Deterministic dataset partitioning utility.
- `src/knowledge/docs_db.py`: In-memory knowledge base indexing cluster runbooks with dynamic learning and persistence.
- `src/knowledge/search.py`: External technical documentation retrieval (async DuckDuckGo web search and local Markdown/text/JSON docs scanner).
- `src/rules/engine.py`: Single-pass two-tier Regex Tree matching engine (`RuleTreeEngine`) with dynamic parent envelope and child rule synthesis and post-facto deduplication.
- `src/rules/models.py`: Strongly-typed domain models (`LogEvent`, `ParentEnvelope`, `Rule`, `ProblemResolution`, `RuleType`, `ValidationStatus`).

---

## Repository Structure

```
mave/
├── .env                  # Environment variables (BASE_URL, API_KEY, MODEL_NAME) - gitignored
├── .env.example          # Sample environment configuration
├── .gitignore            # Git ignore specification
├── AGENTS.md             # Project architecture, real repo state, and agent workflows (THIS FILE)
├── instructions.md       # Problem statement and requirements
├── pyproject.toml        # Dependencies, tool configurations (Ruff, Ty, Pytest)
├── README.md             # Public overview, quickstart, and CLI instructions
├── report.md             # Architectural submission report with deep dives
├── uv.lock               # Deterministic dependency lockfile
├── data/
│   ├── golden_eval.json  # 150-scenario curated evaluation benchmark
│   ├── HPC.log           # LANL System 20 HPC log dataset (433,490 lines)
│   ├── README.md         # Benchmark citation details
│   └── splits/           # Train/Val/Test partitions (generated, gitignored)
├── resources/
│   ├── rules.json        # Dynamic Regex Tree registry (clean initial state)
│   └── runbooks.json     # Dynamic failure recovery runbook registry (clean initial state)
├── src/
│   ├── __init__.py
│   ├── agent.py          # Pydantic-AI SRE agent & output validator
│   ├── config.py         # Pydantic-settings configuration singleton
│   ├── evaluation.py     # Evaluation metrics and SLA benchmark runner
│   ├── main.py           # Unified streaming pipeline CLI & benchmark evaluation entry point
│   ├── pipeline.py       # Two-tier streaming pipeline orchestrator
│   ├── split_data.py     # Train/Val/Test data splitter
│   ├── knowledge/
│   │   ├── __init__.py
│   │   ├── docs_db.py    # Runbooks & knowledge base search
│   │   └── search.py     # Async web search & local docs retrieval tools
│   └── rules/
│       ├── __init__.py
│       ├── engine.py     # Unified RuleTreeEngine (ParentEnvelope -> Child Rule)
│       └── models.py     # Generic key-value LogEvent & Regex Tree domain models
└── tests/
    ├── fixtures/           # Mock verification rules and runbook fixtures for test suites
    ├── test_agent.py       # SRE agent tool-calling & regex output validation tests
    ├── test_docs_db.py     # Knowledge base lookup & search tests
    ├── test_evaluation.py  # Benchmark metrics & scoring tests
    ├── test_pipeline.py    # Stream pipeline & dynamic rule learning tests
    ├── test_rule_engine.py # Regex tree matching, persistence & validation tests
    └── test_search.py      # Async external web search & local docs retrieval tests
```

---

## Development & Code Quality Standards

Always verify the codebase after any modification using Astral's tooling suite:

```bash
# 1. Run all unit and integration tests (59 tests passing)
uv run pytest

# 2. Check static typing (Astral Ty)
uv run ty check

# 3. Code formatting and linting (Astral Ruff)
uv run ruff check .
uv run ruff format .

# 4. Pipeline execution / demo
uv run python src/main.py
uv run python src/main.py data/HPC.log --limit 500
uv run python src/main.py --line "130000 Interconnect-1T00 switch_module psu 1074220000 1 PSU status ( off on )"
```

---

## Agent Instructions & Maintenance Protocol

To maintain consistency and long-term project health, all contributing AI agents and developers must strictly follow these rules:

### 1. Synchronize `AGENTS.md` Regularly
- **Mandatory Update Trigger**: Whenever files or modules are created, renamed, moved, or deleted; whenever dependencies change in `pyproject.toml`; or whenever architecture/dataflow changes, update `AGENTS.md` immediately in the same commit.
- **Accuracy Check**: Ensure that the "Repository Structure" and "Core Modules" sections accurately reflect existing disk paths and roles. Do not document planned or deleted files.

### 2. Git Discipline & Commit Standards
- **Conventional Commits**: Every commit message must follow conventional commit semantics:
  - `feat(...)`: New functionality, modules, or features.
  - `fix(...)`: Bug fixes, race condition patches, error handling.
  - `refactor(...)`: Restructuring code without changing external behavior.
  - `docs(...)`: Documentation updates (`AGENTS.md`, `README.md`, docstrings).
  - `test(...)`: Adding or updating test suites.
  - `chore(...)`: Dependency updates, tooling, git configuration.
- **Atomic Commits**: Keep commits focused and logically grouped. Do not mix unrelated refactors with new features.
- **Verification Before Commit**: Never commit failing code. Always run `uv run pytest`, `uv run ty check`, and `uv run ruff check .` before committing.
- **Clean Working Tree**: Ensure no untracked scratch files, temporary logs, or `.env` credential files are committed to git. Always inspect `git status` before finishing a task.
