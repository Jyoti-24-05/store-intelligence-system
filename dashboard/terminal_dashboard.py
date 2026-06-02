"""Terminal dashboard — polls API every N seconds and renders with rich.live.

Usage
-----
    python dashboard/terminal_dashboard.py
    API_URL=http://localhost:8000 STORE_ID=STORE_BLR_002 python dashboard/terminal_dashboard.py

Environment variables
---------------------
    API_URL                  default: http://localhost:8000
    STORE_ID                 default: ST1008
    POLL_INTERVAL_SECONDS    default: 5
"""
from __future__ import annotations

import os
import sys
import time
from datetime import datetime

import httpx
from rich.columns import Columns
from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich import box

API_URL             = os.getenv("API_URL",             "http://localhost:8000")
STORE_ID            = os.getenv("STORE_ID",            "ST1008")
POLL_INTERVAL       = int(os.getenv("POLL_INTERVAL_SECONDS", "5"))

console = Console()

SEVERITY_COLORS = {
    "CRITICAL": "bold red",
    "WARN":     "bold yellow",
    "INFO":     "bold cyan",
}


# ─────────────────────────────────────────────
# Data fetchers
# ─────────────────────────────────────────────

def fetch(client: httpx.Client, path: str) -> dict | None:
    try:
        r = client.get(f"{API_URL}{path}", timeout=4)
        r.raise_for_status()
        return r.json()
    except Exception:
        return None


# ─────────────────────────────────────────────
# Renderers
# ─────────────────────────────────────────────

def _queue_color(depth: int) -> str:
    if depth < 3:   return "green"
    if depth <= 6:  return "yellow"
    return "red"


def _pct_bar(value: float, width: int = 20) -> str:
    filled = round(value * width)
    return f"[green]{'█' * filled}[/green]{'░' * (width - filled)}"


def build_metrics_panel(metrics: dict | None) -> Panel:
    if metrics is None:
        return Panel("[red]API unreachable[/red]", title="Metrics", border_style="red")

    t = Table(box=box.SIMPLE, show_header=False, padding=(0, 1))
    t.add_column("Metric", style="cyan", min_width=22)
    t.add_column("Value",  min_width=30)

    uv    = metrics["unique_visitors"]
    cr    = metrics["conversion_rate"]
    qd    = metrics["current_queue_depth"]
    ar    = metrics["abandonment_rate"]
    qcol  = _queue_color(qd)

    t.add_row("Unique Visitors",   f"[bold white]{uv}[/bold white]")
    t.add_row("Conversion Rate",   f"{_pct_bar(cr)}  [bold]{cr*100:.1f}%[/bold]")
    t.add_row("Queue Depth",       f"[{qcol}]{qd}[/{qcol}]")
    t.add_row("Abandonment Rate",  f"[bold]{ar*100:.1f}%[/bold]")

    return Panel(t, title=f"[bold cyan]Metrics — {STORE_ID}[/bold cyan]", border_style="cyan")


def build_anomaly_panel(anomaly_data: dict | None) -> Panel:
    if anomaly_data is None:
        return Panel("[red]API unreachable[/red]", title="Anomalies", border_style="red")

    anomalies = anomaly_data.get("anomalies", [])
    if not anomalies:
        return Panel(
            "[green]✓ No active anomalies[/green]",
            title="[bold green]Anomalies[/bold green]",
            border_style="green",
        )

    t = Table(box=box.SIMPLE, show_header=True, padding=(0, 1))
    t.add_column("Severity", style="bold", min_width=10)
    t.add_column("Type",     style="cyan", min_width=24)
    t.add_column("Description", min_width=40)

    for a in anomalies:
        col = SEVERITY_COLORS.get(a["severity"], "white")
        t.add_row(
            f"[{col}]{a['severity']}[/{col}]",
            a["anomaly_type"],
            a["description"][:60] + ("…" if len(a["description"]) > 60 else ""),
        )

    border = "red" if any(a["severity"] == "CRITICAL" for a in anomalies) else "yellow"
    return Panel(t, title=f"[bold {border}]Anomalies ({len(anomalies)} active)[/bold {border}]",
                 border_style=border)


def build_funnel_panel(funnel: dict | None) -> Panel:
    if funnel is None:
        return Panel("[red]API unreachable[/red]", title="Funnel", border_style="red")

    t = Table(box=box.SIMPLE, show_header=True, padding=(0, 1))
    t.add_column("Stage",      style="cyan", min_width=16)
    t.add_column("Sessions",   justify="right", min_width=10)
    t.add_column("Drop-off",   justify="right", min_width=10)

    for s in funnel.get("stages", []):
        drop = s["drop_off_pct"]
        drop_col = "red" if drop > 50 else "yellow" if drop > 20 else "green"
        t.add_row(
            s["stage"].replace("_", " ").title(),
            str(s["count"]),
            f"[{drop_col}]{drop:.1f}%[/{drop_col}]" if s["stage"] != "entry" else "—",
        )

    return Panel(t, title="[bold cyan]Funnel[/bold cyan]", border_style="cyan")


def build_header() -> Text:
    now = datetime.now().strftime("%H:%M:%S")
    txt = Text()
    txt.append("  Store Intelligence", style="bold magenta")
    txt.append(f"  —  {STORE_ID}", style="bold white")
    txt.append(f"  —  {now}", style="dim")
    txt.append(f"  —  polling every {POLL_INTERVAL}s", style="dim")
    return txt


def render(client: httpx.Client):
    metrics  = fetch(client, f"/stores/{STORE_ID}/metrics")
    anomalies = fetch(client, f"/stores/{STORE_ID}/anomalies")
    funnel   = fetch(client, f"/stores/{STORE_ID}/funnel")

    from rich.layout import Layout
    from rich.console import Group

    return Group(
        build_header(),
        Columns([build_metrics_panel(metrics), build_funnel_panel(funnel)],
                equal=True, expand=True),
        build_anomaly_panel(anomalies),
    )


# ─────────────────────────────────────────────
# Main loop
# ─────────────────────────────────────────────

def main():
    console.print(f"\n[bold cyan]Store Intelligence Dashboard[/bold cyan]")
    console.print(f"API: [yellow]{API_URL}[/yellow]   Store: [yellow]{STORE_ID}[/yellow]")
    console.print("Press [bold]Ctrl+C[/bold] to exit\n")

    with httpx.Client() as client:
        with Live(render(client), console=console, refresh_per_second=1,
                  screen=True) as live:
            try:
                while True:
                    time.sleep(POLL_INTERVAL)
                    live.update(render(client))
            except KeyboardInterrupt:
                pass

    console.print("\n[dim]Dashboard stopped.[/dim]")


if __name__ == "__main__":
    main()