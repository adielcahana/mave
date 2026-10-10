# Design Specification: HPC Log Problem Resolution System

**Date:** 2026-10-10  
**Status:** Approved Specification  
**Target Repository:** `mave`  

---

## 1. Intent & Context

High Performance Computing (HPC) clusters generate large-scale, semi-structured log streams across compute nodes, service daemons, interconnect switches, and shared cluster filesystems. Failures cause job interruptions and downtime.

The dataset used is the 433,490-line benchmark from System 20 at Los Alamos National Laboratory (LANL) (`data/HPC.log`).

The objective is to process existing logs to:
1. Identify errors and differentiate them from routine benign telemetry.
2. Recommend actionable remediation backed by citations and reasons.
3. Adapt to new log formats and templates dynamically (future-proofing).

---

## 2. Ingestion Architecture (Design Only)

Per instructions, data ingestion is designed and diagrammed without background infrastructure deployment.

```mermaid
flowchart TD
    subgraph Nodes["Cluster Nodes & Daemons"]
        CN["Compute Nodes (/dev/kmsg, syslog)"]
        SN["Service Nodes (RMS daemons)"]
        FS["Storage Nodes (ClusterFileSystem)"]
        SW["Switch Modules (PSU, Fan)"]
    end

    subgraph Collection["Log Collection"]
        Col["Log Collectors (Fluentbit / Vector)"]
    end

    subgraph MessageQueue["Message Backbone"]
        MQ["Distributed Queue (Kafka / Redpanda)"]
    end

    subgraph Storage["Data Lake & Lakehouse"]
        DL["Data Lake / Cold Storage"]
    end

    subgraph System["Problem Resolution Engine"]
        Engine["mave Ingestion Worker / Batch Processor"]
    end

    CN --> Col
    SN --> Col
    FS --> Col
    SW --> Col
    Col --> MQ
    MQ --> DL
    MQ --> Engine
```

---

## 3. End-to-End System Architecture

```mermaid
flowchart TD
    Log["Existing HPC Log Line"] --> Norm["Log Normalizer (parse_log_line)"]
    Norm --> Event["LogEvent (subsystem, event_type, message)"]
    
    Event --> Filter{"Telemetry Filter"}
    Filter -->|"Routine Telemetry (fan, ambient, ok)"| Discard["Discard / Telemetry Filtered ($0, <0.1ms)"]
    Filter -->|"Actionable Log"| RuleMatch{"RuleTreeEngine Match?"}
    
    RuleMatch -->|"Known Rule Match"| FastPath["Fast-Path Hit ($0, <0.5ms)"]
    RuleMatch -->|"Novel Error"| Agent["Pydantic-AI SRE Agent (NVIDIA NIM)"]
    
    Agent --> Tools["Query Runbooks (query_subsystem_docs)"]
    Tools --> Agent
    Agent --> Res["ProblemResolution (Summary, Severity, Steps, Citations)"]
    Res --> Cache["Dynamic Cache in RuleTreeEngine"]
    Cache --> Output["Return Resolution"]
    FastPath --> Output
```

---

## 4. Multi-Stage Pipeline Breakdown

```mermaid
sequenceDiagram
    autonumber
    actor Caller as Log Processor
    participant Parser as Normalizer
    participant Filter as TelemetryFilter
    participant Engine as RuleTreeEngine
    participant Agent as SRE Agent
    participant Docs as Subsystem Runbooks

    Caller->>Parser: parse_log_line(raw)
    Parser-->>Caller: LogEvent
    Caller->>Filter: is_routine_telemetry(LogEvent)
    alt Is Routine Telemetry
        Filter-->>Caller: True -> TELEMETRY_FILTERED ($0)
    else Actionable Log
        Filter-->>Caller: False
        Caller->>Engine: match(LogEvent)
        alt Rule or Cache Hit
            Engine-->>Caller: ProblemResolution -> FAST_PATH_HIT ($0)
        else Novel Error
            Engine-->>Caller: None
            Caller->>Agent: analyze_log(LogEvent)
            Agent->>Docs: query_subsystem_docs(subsystem, keyword)
            Docs-->>Agent: Subsystem Runbook Excerpt
            Agent-->>Caller: ProblemResolution -> AGENT_RESOLVED
            Caller->>Engine: create_rule_from_resolution(LogEvent, ProblemResolution)
        end
    end
```

---

## 5. Subsystem Component Relationships

```mermaid
classDiagram
    class LogEvent {
        +str log_id
        +str node
        +str subsystem
        +str event_type
        +int timestamp
        +int flag
        +str message
        +str raw
    }

    class ProblemResolution {
        +str error_summary
        +str root_cause
        +str severity
        +list~str~ remediation_steps
        +list~str~ citations_and_reasons
    }

    class Rule {
        +str rule_id
        +str subsystem
        +str event_type
        +str pattern
        +ProblemResolution resolution
    }

    class RuleTreeEngine {
        +match(LogEvent) ProblemResolution
        +create_rule_from_resolution(LogEvent, ProblemResolution)
        +add_rule(Rule)
    }

    class TelemetryFilter {
        +is_routine_telemetry(LogEvent) bool
    }

    class LogPipeline {
        +process_log(raw_line) PipelineResult
        +process_logs(logs) list~PipelineResult~
    }

    LogPipeline --> TelemetryFilter : filters routine logs
    LogPipeline --> RuleTreeEngine : matches or caches rules
    LogPipeline --> ProblemResolution : returns resolution
    RuleTreeEngine --> Rule : manages
    Rule --> ProblemResolution : contains
```

---

## 6. Verification & Quality Plan

- Strict Python 3.12+ standards: `uv run ty check`, `uv run ruff check .`, `uv run ruff format .`
- Automated test coverage: `uv run pytest`
- Batch evaluation: `uv run python main.py --evaluate`
- Demonstration walkthrough: `uv run python main.py --demo`
