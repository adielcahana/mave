"""HPC Log Stream Automated Problem Resolution Engine & CLI."""

# ruff: noqa: E402
import argparse
import asyncio
import io
import json
import os
import re
import sys
from pathlib import Path

# Ensure repository root is on sys.path for direct script execution (`python src/main.py`)
_repo_root = Path(__file__).resolve().parent.parent
if str(_repo_root) not in sys.path:
    sys.path.insert(0, str(_repo_root))

# Suppress Pydantic-AI ASCII banner for clean TUI rendering
os.environ["PYDANTIC_AI_NO_BANNER"] = "1"

if sys.platform == "win32" and isinstance(sys.stdout, io.TextIOWrapper):
    sys.stdout.reconfigure(encoding="utf-8")

from rich import box
from rich.console import Console, Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from src.evaluation import evaluate_benchmark
from src.pipeline import LogPipeline, LogProcessingStats, SourceName
from src.rules.engine import RuleTreeEngine

console = Console()


def _format_severity_badge(severity: str) -> Text:
    """Format an incident severity badge with appropriate styling."""
    sev = severity.upper()
    if sev == "CRITICAL":
        return Text(" CRITICAL ", style="bold white on red")
    elif sev == "HIGH":
        return Text(" HIGH ", style="bold black on bright_yellow")
    elif sev == "MEDIUM":
        return Text(" MEDIUM ", style="bold black on yellow")
    elif sev == "LOW":
        return Text(" LOW ", style="bold white on blue")
    return Text(f" {sev} ", style="bold white on cyan")


def _format_tier_badge(status: str) -> Text:
    """Format a routing tier badge (Fast-Path vs SRE Agent LLM)."""
    if status == "FAST_PATH_HIT":
        return Text(" FAST-PATH (AUTOMATED) ", style="bold white on green")
    elif status == "AGENT_RESOLVED":
        return Text(" SRE AGENT (LLM) ", style="bold white on magenta")
    return Text(f" {status} ", style="bold white on blue")


def _clean_str(text: str) -> str:
    """Normalize non-breaking spaces and whitespace for clean terminal rendering."""
    return text.replace("\xa0", " ").strip()


def render_header(subtitle: str = "") -> None:
    """Render the master TUI header panel."""
    title_text = Text()
    title_text.append("⚡ ", style="bold yellow")
    title_text.append("HPC STREAM LOG TRIAGE & AUTOMATED PROBLEM RESOLUTION", style="bold cyan")
    title_text.append(" ⚡", style="bold yellow")

    sub = subtitle or (
        "Two-Tier Streaming Engine • Wire-Speed Filtration • Sub-Millisecond Rule Tree • SRE Agent"
    )
    sub_text = Text(sub, style="dim white")

    header_table = Table.grid(expand=True)
    header_table.add_column(justify="center")
    header_table.add_row(title_text)
    header_table.add_row(sub_text)

    panel = Panel(
        header_table,
        border_style="bright_blue",
        box=box.ROUNDED,
        padding=(1, 2),
    )
    console.print(panel)


def load_log_lines(file_path: str | Path, limit: int | None = None) -> list[str]:
    """Load log lines from a .log, .txt, or .json file."""
    path = Path(file_path)
    if not path.is_file():
        raise FileNotFoundError(f"Log file not found: {path.resolve()}")

    raw_lines: list[str] = []
    if path.suffix.lower() == ".json":
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            for item in data:
                if isinstance(item, str):
                    raw_lines.append(item)
                elif isinstance(item, dict):
                    raw = item.get("raw") or item.get("log") or item.get("message")
                    if raw:
                        raw_lines.append(str(raw))
    else:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                stripped = line.strip()
                if stripped:
                    raw_lines.append(stripped)
                if limit and len(raw_lines) >= limit:
                    break

    if limit and len(raw_lines) > limit:
        raw_lines = raw_lines[:limit]

    return raw_lines


async def analyze_log_lines(
    raw_lines: list[str],
    source_name: SourceName = "Stream",
    pipeline: LogPipeline | None = None,
    concurrency_limit: int | None = 5,
    file_name: str = "",
) -> LogProcessingStats:
    """Analyze a list of log lines concurrently via asyncio and return stats and remedies."""
    pipe = pipeline or LogPipeline()
    return await pipe.analyze(
        raw_lines,
        source_name=source_name,
        concurrency_limit=concurrency_limit,
        file_name=file_name,
    )


async def analyze_log_file(
    file_path: str | Path,
    limit: int | None = None,
    pipeline: LogPipeline | None = None,
    concurrency_limit: int | None = 5,
) -> LogProcessingStats:
    """Analyze a log file and return quantitative statistics and remedy recommendations."""
    path = Path(file_path)
    raw_lines = load_log_lines(path, limit=limit)
    return await analyze_log_lines(
        raw_lines,
        source_name="File",
        pipeline=pipeline,
        concurrency_limit=concurrency_limit,
        file_name=path.name,
    )


def render_stats_dashboard(stats: LogProcessingStats) -> None:
    """Render structured stats dashboard and remedy recommendations in rich TUI."""
    # 1. Main Stats Dashboard Table
    stats_table = Table(
        title=f"[bold cyan]HPC Log Processing Statistics — {stats.file_name}[/]",
        box=box.ROUNDED,
        border_style="cyan",
        header_style="bold bright_white on blue",
    )
    stats_table.add_column("Classification Metric", style="bold white", width=32)
    stats_table.add_column("Count", justify="right", width=10)
    stats_table.add_column("Share", justify="right", width=10)
    stats_table.add_column("Pipeline Impact", style="italic white")

    total = stats.total_lines
    benign_pct = (stats.benign_count / total * 100) if total > 0 else 0.0
    error_pct = (stats.error_count / total * 100) if total > 0 else 0.0
    auto_pct = (stats.auto_annotated_count / total * 100) if total > 0 else 0.0
    llm_pct = (stats.llm_needed_count / total * 100) if total > 0 else 0.0

    stats_table.add_row(
        "Total Logs Processed",
        f"[bold]{total}[/]",
        "100.0%",
        "Full ingested stream volume",
    )
    stats_table.add_section()

    stats_table.add_row(
        "Benign Telemetry (Routine)",
        f"[bold cyan]{stats.benign_count}[/]",
        f"[cyan]{benign_pct:.1f}%[/]",
        "[cyan]Filtered at wire speed (<0.1ms, $0)[/]",
    )
    stats_table.add_row(
        "Actionable Errors (Incidents)",
        f"[bold bright_yellow]{stats.error_count}[/]",
        f"[bright_yellow]{error_pct:.1f}%[/]",
        "[bright_yellow]Triaged & remediated[/]",
    )
    stats_table.add_section()

    stats_table.add_row(
        "  ↳ Automatically Annotated",
        f"[bold green]{stats.auto_annotated_count}[/]",
        f"[green]{auto_pct:.1f}%[/]",
        "[green]Fast-path rules & cache (<0.5ms, $0)[/]",
    )
    stats_table.add_row(
        "  ↳ Escalated to LLM (SRE Agent)",
        f"[bold magenta]{stats.llm_needed_count}[/]",
        f"[magenta]{llm_pct:.1f}%[/]",
        f"[magenta]Novel error triage (~${stats.total_cost_usd:.4f})[/]",
    )
    stats_table.add_section()

    bypass_count = stats.benign_count + stats.auto_annotated_count
    bypass_pct = (bypass_count / total * 100) if total > 0 else 0.0
    saved_usd = stats.total_lines * 0.0015 - stats.total_cost_usd
    stats_table.add_row(
        "Zero-Cost LLM Bypass Rate",
        f"[bold green]{bypass_count}[/]",
        f"[bold green]{bypass_pct:.1f}%[/]",
        f"[bold green]Saved estimated ${max(saved_usd, 0.0):.4f} in inference[/]",
    )

    console.print(stats_table)
    console.print()

    # 2. Timing & Throughput Card
    time_str = (
        f"{stats.elapsed_time_s * 1000.0:.2f} ms"
        if stats.elapsed_time_s < 1.0
        else f"{stats.elapsed_time_s:.2f} s"
    )
    avg_latency = (stats.elapsed_time_s / total * 1000.0) if total > 0 else 0.0

    perf_table = Table(box=box.ROUNDED, expand=True, border_style="green")
    perf_table.add_column("⏱ Time to Results", justify="center", style="bold bright_green")
    perf_table.add_column("⚡ Stream Throughput", justify="center", style="bold bright_yellow")
    perf_table.add_column("⚡ Mean Latency per Log", justify="center", style="bold cyan")
    perf_table.add_row(
        time_str, f"{stats.throughput_lines_per_sec:,.0f} logs/sec", f"{avg_latency:.3f} ms"
    )

    console.print(
        Panel(
            perf_table,
            title="[bold bright_green]Operational Performance & Execution Timing[/]",
            border_style="green",
            box=box.ROUNDED,
        )
    )
    console.print()

    # 3. Remedy Recommendations Section
    if stats.unique_errors:
        rec_title = (
            f"ACTIONABLE ERROR REMEDY RECOMMENDATIONS "
            f"({len(stats.unique_errors)} UNIQUE FAILURE MODES)"
        )
        console.print(
            Panel(
                Text(rec_title, style="bold bright_white", justify="center"),
                style="bold yellow",
                box=box.DOUBLE,
            )
        )

        for _root_key, (res, occurrences) in stats.unique_errors.items():
            if not res.resolution:
                continue

            r = res.resolution
            card_content: list[Table | Text] = []

            # Clean display title
            clean_title = re.sub(r"^\[node-[^\]]+\]\s*", "", r.error_summary).strip()
            # If title has raw message in parentheses, clean it up
            if "(" in clean_title and clean_title.endswith(")"):
                clean_title = clean_title[: clean_title.rfind("(")].strip()

            meta_table = Table.grid(padding=(0, 1), expand=True)
            meta_table.add_column()
            meta_table.add_column(justify="right")

            badge_text = Text()
            badge_text.append_text(_format_severity_badge(r.severity))
            badge_text.append(" ")
            badge_text.append_text(_format_tier_badge(res.status))
            badge_text.append(f"  ({occurrences} occurrence{'s' if occurrences != 1 else ''})")
            meta_table.add_row(badge_text, Text(f"Cost: ${res.cost_usd:.4f}", style="bold yellow"))
            card_content.append(meta_table)
            card_content.append(Text())

            remedy_table = Table(box=box.ROUNDED, border_style="dim", expand=True)
            remedy_table.add_column("Field", style="bold bright_cyan", width=14)
            remedy_table.add_column("Actionable Guidance", style="white")

            remedy_table.add_row("Root Cause", Text(_clean_str(r.root_cause), style="italic white"))

            if r.remediation_steps:
                steps_text = Text()
                for idx, step in enumerate(r.remediation_steps, 1):
                    clean_step = _clean_str(step)
                    if clean_step.startswith(f"{idx}."):
                        clean_step = clean_step[len(f"{idx}.") :].strip()
                    steps_text.append(f"{idx}. ", style="bold bright_yellow")
                    steps_text.append(f"{clean_step}\n")
                remedy_table.add_row("Remediation", steps_text)

            if r.citations_and_reasons:
                cites_text = Text()
                for cite in r.citations_and_reasons:
                    cites_text.append("📖 ", style="yellow")
                    cites_text.append(_clean_str(cite) + "\n", style="italic dim bright_white")
                remedy_table.add_row("Citations", cites_text)

            card_content.append(remedy_table)

            panel = Panel(
                Group(*card_content),
                title=f"[bold bright_yellow]Incident: {clean_title}[/]",
                border_style="yellow" if r.severity in ("HIGH", "CRITICAL") else "blue",
                box=box.ROUNDED,
                padding=(1, 2),
            )
            console.print(panel)
    elif stats.benign_count > 0 and stats.error_count == 0:
        console.print(
            Panel(
                Text(
                    "✓ Benign Telemetry: Log classified as routine cluster operation.\n"
                    "Dropped at wire speed (zero incident, zero token cost).",
                    style="bold green",
                    justify="center",
                ),
                border_style="green",
                box=box.ROUNDED,
            )
        )


def run_evaluate(benchmark_path: str = "data/golden_eval.json") -> None:
    """Run benchmark evaluation for test backwards compatibility."""
    path = Path(benchmark_path)
    if not path.is_file():
        console.print(Panel(f"Error: Golden evaluation file not found at {path.resolve()}"))
        sys.exit(1)

    rule_engine = RuleTreeEngine.with_defaults(rules_path=None)
    metrics = evaluate_benchmark(path, rule_engine=rule_engine)

    filt_pct = (metrics.telemetry_filtered / metrics.total * 100) if metrics.total > 0 else 0.0
    fast_pct = (metrics.fast_path_hits / metrics.total * 100) if metrics.total > 0 else 0.0
    fall_pct = (metrics.agent_fallbacks / metrics.total * 100) if metrics.total > 0 else 0.0

    print("HPC Golden Evaluation Metrics")
    print(f"Total: {metrics.total}, Accuracy: {metrics.accuracy:.1%}")
    print("HPC OPERATIONAL SLA BREAKDOWN")
    print("Wire-Speed Telemetry Dropping SLA")
    print(f"Volume: {metrics.telemetry_filtered} ({filt_pct:.1f}%)")
    print("Fast-Path Rule Tree Resolution SLA")
    print(f"Volume: {metrics.fast_path_hits} ({fast_pct:.1f}%)")
    print(f"Slow-Path Volume: {metrics.agent_fallbacks} ({fall_pct:.1f}%)")


def main() -> None:
    """CLI entry point accepting a log file or single log line and returning stats and remedies."""
    parser = argparse.ArgumentParser(
        description="HPC Automated Problem Resolution Engine — Ingest Log Stream & Return Remedies",
    )
    parser.add_argument(
        "log_file",
        nargs="?",
        default=None,
        help="Path to HPC log file (.log, .txt, or .json). Default: data/golden_eval.json",
    )
    parser.add_argument(
        "--file",
        "-f",
        dest="flag_file",
        help="Explicit log file path",
    )
    parser.add_argument(
        "--line",
        "-l",
        "--log",
        dest="log_line",
        help="Process a single log line directly from CLI",
    )
    parser.add_argument(
        "--limit",
        "-n",
        type=int,
        default=None,
        help="Maximum number of log lines to process (default: all)",
    )
    parser.add_argument(
        "--concurrency",
        "-c",
        type=int,
        default=5,
        help="Maximum concurrent asyncio tasks for log processing (default: 5)",
    )
    parser.add_argument(
        "--evaluate",
        "--eval",
        dest="evaluate",
        action="store_true",
        help="Run golden evaluation benchmark breakdown",
    )

    args = parser.parse_args()

    if args.evaluate or (args.log_file and args.log_file in ("evaluate", "eval")):
        run_evaluate("data/golden_eval.json")
        return

    if args.log_line is not None:
        log_line = args.log_line.strip()
        if not log_line:
            console.print("[bold red]Error:[/] Provided log line is empty.")
            sys.exit(1)

        render_header(subtitle="Processing single log line from CLI")
        console.print(
            Panel(
                Text(log_line, style="bright_white"),
                title="[bold cyan]Input Log Line[/]",
                border_style="cyan",
                box=box.ROUNDED,
            )
        )
        console.print()

        with console.status(
            "[bold cyan]Processing log line...[/]",
            spinner="dots",
        ):
            stats = asyncio.run(analyze_log_lines([log_line], source_name="CLI Input"))

        render_stats_dashboard(stats)
        return

    target_file = args.flag_file or args.log_file or "data/golden_eval.json"

    render_header(subtitle=f"Ingesting log stream from: {target_file}")

    with console.status(
        f"[bold cyan]Processing log stream from {target_file}...[/]",
        spinner="dots",
    ):
        stats = asyncio.run(
            analyze_log_file(
                target_file,
                limit=args.limit,
                concurrency_limit=args.concurrency,
            )
        )

    render_stats_dashboard(stats)


if __name__ == "__main__":
    main()
