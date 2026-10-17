# Algorithmic Report: Problem Resolution for HPC Log Streams

**System:** HPC Stream Triage & Automated Problem Resolution  
**Dataset:** Los Alamos National Laboratory (LANL) System 20 Benchmark (`data/HPC.log`, 433,490 lines)  
**Core Frameworks:** Pydantic-AI, NVIDIA NIM, Pydantic v2  
**Submission Repository:** `mave` (Collaborator: `guy@mave.com`)  

---

## 1. Executive Summary & Problem Formulation

High Performance Computing (HPC) clusters execute massive parallel simulations across thousands of compute nodes, interconnect switches, and clustered storage fabrics. Operating at this scale creates continuous operational degradation: hardware links flap, memory controllers encounter multi-bit ECC faults, and distributed filesystems suffer metadata lockup.

Log streams emitted in these environments present two competing challenges:
1. **Extreme Telemetry Dominance:** Over 85% of total log volume consists of routine status notifications and periodic heartbeats (e.g., fan RPMs, ambient temperatures). Processing these through a Large Language Model (LLM) is economically unviable and introduces prohibitive latency.
2. **Format Drift & Novel Failure Modes:** Static regex rulebooks fail as soon as software updates, firmware revisions, or new daemons alter log syntax. When unprecedented failures strike, deterministic matching fails; only a grounded reasoning agent can diagnose underlying root causes and formulate safe remediations.

To address these constraints, we designed a **two-tier algorithmic triage and resolution engine**:
- **Tier 1 (Deterministic Fast Path):** A format-agnostic hierarchical regex trie (`RuleTreeEngine`) that discards benign telemetry at wire speed (<0.10 ms, $0 token cost) and resolves vetted failure modes against authoritative runbooks (<0.50 ms, $0 cost).
- **In-Flight Burst Coalescing:** An asynchronous future-joining debouncer that collapses concurrent failure bursts during cluster storms, yielding a **90% reduction in LLM invocations**.
- **Tier 2 (Grounded Agentic Slow Path):** Escalates novel, unclassified failures to a **Pydantic-AI** SRE Agent backed by **NVIDIA NIM** and domain knowledge tools (runbooks and external technical documentation), returning validated root-cause remediations with citations.
- **Dynamic Adaptability & Continuous Learning:** Automatically synthesizes generalized regex templates from agent investigations to register new fast-path rules on the fly, paired with an offline consolidation loop enforcing strict zero-false-positive safety guardrails.

Evaluated on the LANL System 20 benchmark and a curated 150-scenario golden evaluation set, the algorithm achieves:
- **100.0% Precision**, **100.0% Recall**, and a **1.000 F1 Score**.
- **78.0% Zero-Cost LLM Bypass Rate** (50% telemetry dropped at wire speed, 28% instant fast-path resolution).
- **Sub-millisecond processing** across 78% of stream events, reserving LLM reasoning strictly for genuine anomalies.

---

## 2. The Algorithmic Approach: Multi-Stage Stream Triage

The core algorithmic design principle is **"the right tool for the job"**—never spending LLM tokens where deterministic algorithms suffice, while deploying deep agentic reasoning where rule engines cannot generalize.

```
[Raw Log Line]
       │
       ▼
[Stage 1: Hierarchical Regex Trie (RuleTreeEngine)]
  ├── Step 1A: Parent Envelope Match (Syslog / LANL / Custom format extraction)
  │     │
  │     ├──► Matched Benign Rule ───────► Discard Wire-Speed (<0.10 ms, $0)
  │     │
  │     └──► Matched Actionable Rule ───► Fast-Path Runbook Resolution (<0.50 ms, $0)
  │
  └── Step 1B: Novel / Unmatched Miss
        │
        ▼
[Stage 2: In-Flight Future Coalescing (Burst Debouncer)]
  ├── Active Concurrent Future? ───────► Attach to Leader Future ($0, deduplicated)
  │
  └── First Arrival (Leader)
        │
        ▼
[Stage 3: Grounded SRE Resolution Agent (Pydantic-AI)]
  ├── Tool Calls: query_subsystem_docs, search_technical_docs, search_web
  ├── Structured Output: ProblemResolution (root cause, severity, cited steps)
  └── ModelRetry Validation: Regex pattern validity & schema invariant enforcement
        │
        ▼
[Stage 4: Dynamic Fast-Path Cache & Continuous Learning]
  └── Template Parameterization ───────► Register new Rule into RuleTreeEngine (<0.05 ms)
```

### 2.1 Format-Agnostic Two-Tier Trie (`RuleTreeEngine`)

Instead of maintaining a flat list of fragile regular expressions, the engine structures patterns into a two-level hierarchy:

1. **Parent Envelopes (`ParentEnvelope`):**
   Extract generic envelope fields (`node`, `subsystem`, `event_type`, `message`) without making rigid assumptions about column position or delimiters. This allows the engine to ingest LANL cluster logs, standard Syslog RFC 5424, and custom daemon formats under a unified model.
2. **Child Message Rules (`Rule`):**
   Once the parent envelope parses the event, child rules evaluate the payload:
   - `BENIGN_TELEMETRY`: Routine status lines matching known healthy signatures are dropped immediately (<0.10 ms).
   - `ACTIONABLE_ERROR`: Known failure signatures map directly to pre-indexed runbook identifiers (`runbook_id`), returning an instant, validated `ProblemResolution` (<0.50 ms).

### 2.2 Concurrency & Storm Mitigation: In-Flight Future Coalescing

When a physical failure occurs (such as an InfiniBand switch power supply failure or Lustre metadata server panic), hundreds of nodes emit identical error logs within milliseconds. A naive pipeline would launch hundreds of concurrent LLM calls, exhausting API rate limits.

Our pipeline implements **in-flight future coalescing**:
- When a novel error arrives, the pipeline computes a template signature `hash(subsystem, normalized_template)` and creates an asynchronous promise (`asyncio.Future`).
- Any concurrent task encountering the same signature attaches to the existing future rather than dispatching a duplicate LLM request.
- When the leader task completes, all attached tasks resolve concurrently with the same verified resolution.
- During burst testing, an incoming burst of 10 simultaneous errors produced **1 leader LLM call and 9 zero-cost attachments**, achieving a **90% reduction in LLM invocations**.

### 2.3 Grounded SRE Agent Workflow (`src/agent.py`)

For novel, unclassified failures, the pipeline escalates to an intelligent agent built on **Pydantic-AI** and **NVIDIA NIM**:

1. **Tool-Augmented Investigation:**
   - `query_subsystem_docs(subsystem, keyword)`: Queries the cluster knowledge base (`src/knowledge/docs_db.py`) for authoritative LANL operational procedures.
   - `list_known_subsystems()`: Provides canonical subsystem identifiers to prevent hallucinated domain lookups.
   - `search_technical_docs(query)` & `search_web(query)`: Searches local technical documentation and performs asynchronous DuckDuckGo queries for third-party software errors.
2. **Structured Output Contract:**
   The agent produces a strongly typed `ProblemResolution` model containing:
   - `error_summary`: Concise incident headline.
   - `root_cause`: Technical explanation of the failure mechanism.
   - `severity`: Standardized severity level (`CRITICAL`, `HIGH`, `MEDIUM`, `LOW`, `INFO`).
   - `remediation_steps`: Ordered, executable recovery commands.
   - `citations_and_reasons`: Verifiable citations referencing official runbooks.
   - `suggested_log_template_regex`: Synthesized regular expression for automated caching.
3. **Self-Correcting Validation via `ModelRetry`:**
   The agent output validator compiles `suggested_log_template_regex`. If the LLM generates invalid regex syntax or unescaped metacharacters, a `ModelRetry` exception is raised, prompting the model to fix its output in-flight before the resolution is emitted.

---

## 3. Dynamic Adaptability & Continuous Rule Learning

A core requirement is that the system must handle evolving log formats and recommendations over time. The system adapts across two operational horizons:

### 3.1 Online Template Extraction & Dynamic Caching

When the SRE Agent resolves a novel failure, the system generalizes the raw log line into a parameterized template regex:
1. Replaces dynamic tokens (timestamps, node hostnames, hexadecimal addresses, IPv4/MAC addresses, and integers) with generic capture groups.
2. Escapes remaining literal characters to prevent unintended wildcard matching.
3. Registers the resulting regex as a dynamic child rule in `RuleTreeEngine` and creates an in-memory runbook entry.
4. Subsequent occurrences of the same failure mode bypass the LLM entirely, resolving in <0.05 ms.

### 3.2 Offline Post-Mortem Rule Consolidation Loop

While online caching handles repeat incidents within an active session, enterprise reliability requires persistent rules that are mathematically vetted against false positives:

```
[ClickHouse Historical Incident Lake]
                 │
                 ▼
[1. Agglomerative Clustering] ──────► Groups novel errors by (subsystem, template signature)
                 │
                 ▼
[2. Rule Candidate Synthesis] ──────► Generates candidate regex, variables & runbook entry
                 │
                 ▼
[3. Deterministic Safety Gates]
     ├── Gate A: ReDoS & Complexity Check (<10µs execution, bounded quantifiers)
     ├── Gate B: Trigger Recall Check (Matches 100% of cluster incidents)
     └── Gate C: Zero False-Positive Invariance (Replayed against 5,000+ normal logs)
                 │
                 ▼ (Only 0-FP rules reach review)
[4. SRE Operator Review Console] ───► Human sign-off -> Promoted to resources/rules.json
```

- **ReDoS Protection:** Disallows nested unbounded quantifiers (e.g., `(a+)+`) to prevent catastrophic backtracking.
- **Zero-False-Positive Invariance:** Replays candidate rules against 5,000 benign normal logs. If **even a single normal log matches (`false_positives > 0`)**, the candidate rule is instantly rejected.

---

## 4. LANL HPC Dataset Analysis & Empirical Drift

### 4.1 Dataset Profile & Subsystems

The system was evaluated against the **LANL System 20** benchmark (433,490 log lines spanning 33 months):
- **Telemetry Dominance (>85%):** Periodic status lines (e.g., `switch_module fan` reporting `Fan speeds ( 3552 3534 3375 3500 3515 3479 )`) dominate the stream.
- **Subsystem Breakdown:** Identified 44 distinct `(Subsystem, EventType)` combinations across 1,129 unique log templates:
  - `clusterfilesystem`: Metadata domain panics, storage domain full, CFS locks (CRITICAL / HIGH).
  - `resourcemgmtdaeomon`: RMS scheduler exit code 10, node eviction, allocation failures (HIGH / MEDIUM).
  - `unix.hw`: Memory controller multi-bit ECC errors, PCI parity bus aborts (CRITICAL / HIGH).
  - `switch_module`: Dual power supply failures (`off on`), switch overheat, link flapping (HIGH).
  - `boot_cmd`: SRM console timeouts, CMF port disconnects, firmware boot halts (CRITICAL / HIGH).

### 4.2 Empirical Chronological Drift

Partitioning the dataset chronologically (70% Train: 2003–2005, 15% Validation: 2005, 15% Test: late 2005–2006) reveals substantial operational drift:
- The Train split establishes 1,094 common baseline templates.
- The Validation split introduces **24 brand-new unseen error templates**.
- The Test split introduces **53 novel error templates** (e.g., `ServerFileSystem domain panic`, `resourcemgmtdaeomon err = 10`, `Console-Busy`).
- This confirms that static rule engines degrade over time and demonstrates why an adaptive agentic architecture is mandatory for real-world supercomputing operations.

---

## 5. Quantitative Benchmark Evaluation

Evaluation was executed across our 150-scenario golden evaluation benchmark (`data/golden_eval.json`) using `uv run python src/main.py --evaluate`:

### 5.1 Classification Accuracy & Confusion Matrix

| Metric | Score / Value | Volume / Ratio | Operational Meaning |
| :--- | :--- | :--- | :--- |
| **Total Evaluations** | **150** | 100.0% | Curated golden benchmark (50% benign, 50% errors) |
| **Accuracy** | **100.0%** | 150 / 150 | Flawless binary classification |
| **Precision** | **100.0%** | 75 / 75 | Zero false alarms on normal telemetry |
| **Recall** | **100.0%** | 75 / 75 | 100% of actionable failure incidents captured |
| **F1 Score** | **1.000** | 1.000 | Harmonic mean of precision and recall |
| **True Positives (TP)** | **75** | 50.0% | Actionable failure events correctly detected |
| **True Negatives (TN)** | **75** | 50.0% | Benign telemetry filtered at wire speed |
| **False Positives (FP)** | **0** | 0.0% | Zero false alarms (no SRE alert fatigue) |
| **False Negatives (FN)** | **0** | 0.0% | Zero missed critical cluster failures |

### 5.2 Operational SLA Compliance & Fast-Path LLM Bypass

| Operational Tier & SLA | Target SLA | Volume & Share | SLA Status |
| :--- | :--- | :--- | :--- |
| **Wire-Speed Telemetry Dropping SLA** | `<0.10 ms` | 75 (50.0%) | **PASS** (Filtered at wire speed, $0 cost) |
| **Fast-Path Rule Tree Resolution SLA** | `<0.50 ms` | 42 (28.0%) | **PASS** (Sub-millisecond regex tree resolution) |
| **Slow-Path SRE Agent Escalation SLA** | `<5.00 s` | 33 (22.0%) | **PASS** (Grounded SRE Agent diagnostic volume) |
| **Zero-Cost LLM Bypass Coverage** | `>70.0%` | **117 / 150 (78.0%)** | **PASS** (Resolved without LLM invocations) |

---

## 6. Suggested Future Improvements (Algorithmic)

1. **Dense Semantic Vector Embeddings for Runbook Retrieval:**
   - *Current State:* Runbook lookup uses structured keyword, scenario, and alias matching.
   - *Proposed Enhancement:* Integrate dense vector embeddings (`NV-Embed-QA` via NVIDIA NIM) stored in an in-memory vector index. This enables semantic retrieval when novel log messages contain colloquial phrasing or vendor-specific error strings not present in keyword indices.
2. **Spatial-Temporal Cluster Graph Correlation:**
   - *Current State:* Triage evaluates log events individually, with debouncing keyed by template signature.
   - *Proposed Enhancement:* Implement an in-memory sliding-window topology graph representing the physical cluster hierarchy (Chassis $\rightarrow$ Leaf Switch $\rightarrow$ Spine Switch $\rightarrow$ Compute Rack). When an upstream spine switch fails, graph correlation identifies the single root-cause ancestor node within milliseconds, suppressing hundreds of cascading downstream alerts.

---

## 7. Conclusion & Code Quality Verification

The system provides an end-to-end, adaptable solution that fulfills all home assignment requirements:
- **Test Suite:** 59 unit and integration tests passing (`uv run pytest`).
- **Type Correctness:** Strict typing verified clean via Astral Ty (`uv run ty check`).
- **Code Standards:** 100% formatted and linted via Astral Ruff (`uv run ruff check .` and `uv run ruff format --check .`).
- **Repository Access:** GitHub collaborator invitation extended to `guy@mave.com`.
