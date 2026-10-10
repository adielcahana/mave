# AGENTS.md — HPC Log Problem Resolution System Architecture

## Problem Statement

High Performance Computing (HPC) clusters generate massive, evolving semi-structured log streams across thousands of compute and service nodes. Site Reliability Engineers (SREs) face recurring challenges:
- High volume of telemetry noise masking critical hardware and daemon failures.
- Drifting and updating log formats that break brittle regexes.
- The need for rapid, grounded, and cited remediation advice without hallucinated diagnostic commands.

This project addresses these challenges by combining fast deterministic log ingestion and filtering with an agentic problem-resolution layer that queries official subsystem knowledge bases and generates validated, actionable remediation steps.

---

## Technical Stack & Frameworks

| Domain | Framework / Tool | Purpose |
| :--- | :--- | :--- |
| **Agent Framework** | [Pydantic-AI](https://ai.pydantic.dev/) | Agent orchestration, tool binding, retries, and strict schema validation. |
| **Configuration** | [Pydantic-Settings](https://docs.pydantic.dev/latest/concepts/pydantic_settings/) | Strongly-typed environment variables and `.env` file management. |
| **LLM Provider** | [NVIDIA NIM](https://build.nvidia.com) | Enterprise inference API using OpenAI-compatible client (`openai/gpt-oss-20b`). |
| **Data Validation** | [Pydantic v2](https://docs.pydantic.dev/) | Rust-backed validation of inputs, dependencies, and structured outputs. |
| **Code Formatter & Linter**| [Ruff](https://docs.astral.sh/ruff/) | Blazing fast formatting and linting enforcing Python 3.12+ standards. |
| **Static Type Checking** | [Ty](https://github.com/astral-sh/ty) | Static type analysis for strict type correctness. |
| **Package Management** | [uv](https://docs.astral.sh/uv/) | Fast virtual environment and dependency management. |

---

## Architecture & Workflow

```
+-------------------------------------------------------------+
|                      HPC Log Stream                         |
|  (e.g., node-25 server: failed to configure subsystem 10)   |
+------------------------------+------------------------------+
                               |
                               v
+-------------------------------------------------------------+
|                Log Ingestion & Preprocessing                |
|  - Loghub HPC dataset parser                                |
|  - Error filtering & template extraction                    |
+------------------------------+------------------------------+
                               |
                               v
+-------------------------------------------------------------+
|                 Pydantic-AI Agent Harness                   |
|  Model: NVIDIA NIM (openai/gpt-oss-20b)                     |
|  Config: Pydantic-Settings via .env                         |
+---------------+-----------------------------+---------------+
                |                             |
                v (Tool Call)                 v (Output Validation)
+-------------------------------+  +--------------------------+
|      Knowledge & Tools        |  |    Structured Output     |
| - Subsystem Doc Resolver      |  |  (ProblemResolution)    |
| - Error code mappings         |  | - Error summary          |
| - Historical cluster telemetry|  | - Root cause analysis    |
+-------------------------------+  | - Severity rating        |
                                   | - Remediation steps      |
                                   | - Citations & references |
                                   +--------------------------+
```

---

## Agent Specifications

### `HPCProblemResolutionAgent` (`src/agent.py`)
- **Harness Framework**: Pydantic-AI
- **Provider**: NVIDIA NIM API (`https://integrate.api.nvidia.com/v1`) via `AsyncOpenAI` client.
- **Config Provider**: `pydantic-settings` (`Settings` model reading `.env`).
- **Default LLM Model**: `openai/gpt-oss-20b` (compatible with structured output and tool calling).

#### Tools Available to the Agent
- `query_subsystem_docs(subsystem: str) -> str`:
  Queries knowledge bases for HPC subsystem documentation, known error codes (e.g., `resourcemgmtdaeomon`, Slurm, PBS, cluster node controllers), configuration paths, and troubleshooting procedures.

#### Structured Output Schema (`ProblemResolution`)
Each analysis produces a validated Pydantic model:
- `error_summary: str`: High-level summary of the detected incident.
- `root_cause: str`: In-depth explanation of the technical cause.
- `severity: str`: Categorization (`CRITICAL`, `HIGH`, `MEDIUM`, `LOW`).
- `remediation_steps: list[str]`: Ordered, actionable steps for engineers to remediate the failure.
- `citations_and_reasons: list[str]`: References to official manuals, error codes, and reasoning behind the advice.

---

## Project Structure

```
mave/
├── .env                  # NVIDIA credentials (BASE_URL, API_KEY, MODEL_NAME)
├── .env.example          # Sample environment configuration
├── AGENTS.md             # Architecture, problem statement, frameworks, and agent specs
├── instructions.md       # Assignment specification and guidelines
├── pyproject.toml        # Dependencies, tool configurations (Ruff & Ty)
├── main.py               # Entry point running end-to-end agent diagnostics
├── data/
│   ├── HPC.log           # Los Alamos National Labs HPC log dataset
│   └── README.md         # Citation details for loghub benchmark
└── src/
    ├── __init__.py
    ├── agent.py          # Pydantic-AI agent harness, tools, and schema
    └── config.py         # Pydantic-settings configuration loading
```

---

## Development & Code Quality Standards

The project strictly follows modern Python standards:
- **Type Checking**: Verified with `ty` (`uv run ty check`)
- **Linting & Formatting**: Enforced with `ruff` (`uv run ruff format .` and `uv run ruff check .`)
- **Package Management**: Powered by `uv` for reproducible virtual environments.
