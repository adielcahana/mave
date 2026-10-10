# AGENTS.md — Project Setup & Architecture

## Problem Statement

High Performance Computing (HPC) clusters generate large-scale, semi-structured log streams across numerous compute and service nodes. The objective of this project is to build an intelligent, future-proof system for HPC log analysis and automated problem resolution that:
- Identifies critical failure modes and errors within evolving log formats.
- Connects to domain knowledge and subsystem documentation.
- Formulates grounded and cited remediation guidance to reduce cluster downtime.

---

## Technical Stack & Frameworks

| Domain | Framework / Tool | Purpose |
| :--- | :--- | :--- |
| **Agent Framework** | [Pydantic-AI](https://ai.pydantic.dev/) | Agent orchestration, tool integration, dependency injection, and schema validation. |
| **Configuration** | [Pydantic-Settings](https://docs.pydantic.dev/latest/concepts/pydantic_settings/) | Strongly-typed environment variables and `.env` loading. |
| **LLM Provider** | [NVIDIA NIM](https://build.nvidia.com) | Enterprise inference API via OpenAI-compatible endpoint. |
| **Code Formatter & Linter**| [Ruff](https://docs.astral.sh/ruff/) | Formatting and linting enforcing Python 3.12+ standards. |
| **Static Type Checking** | [Ty](https://github.com/astral-sh/ty) | Static type analysis for strict type correctness. |
| **Package Management** | [uv](https://docs.astral.sh/uv/) | Fast virtual environment and dependency management. |

---

## Agent Setup

The project provides a starter Pydantic-AI agent connecting to NVIDIA NIM:

- **Module**: `src/agent.py`
- **Configuration**: Loaded via `src/config.py` using `pydantic-settings` from `.env`.
- **Model Client**: `AsyncOpenAI` pointing to the NVIDIA NIM endpoint (`BASE_URL` and `API_KEY`).
- **Entry Point**: `main.py` verifies connectivity to the NVIDIA NIM provider.

---

## Project Structure

```
mave/
├── .env                  # NVIDIA credentials (BASE_URL, API_KEY, MODEL_NAME)
├── .env.example          # Sample environment configuration
├── AGENTS.md             # Project problem statement, frameworks, and setup overview
├── instructions.md       # Assignment specification and guidelines
├── pyproject.toml        # Dependencies, tool configurations (Ruff & Ty)
├── main.py               # Minimal entry point testing connection to NVIDIA NIM
├── data/
│   ├── HPC.log           # Los Alamos National Labs HPC log dataset
│   └── README.md         # Citation details for loghub benchmark
└── src/
    ├── __init__.py
    ├── agent.py          # Minimal Pydantic-AI agent connecting to NVIDIA NIM
    └── config.py         # Pydantic-settings configuration loading
```

---

## Development & Code Quality Standards

- **Type Checking**: `uv run ty check`
- **Linting & Formatting**: `uv run ruff format .` and `uv run ruff check .`
- **Execution**: `uv run python main.py`
