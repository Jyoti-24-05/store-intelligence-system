"""Terminal dashboard — polls API every N seconds and renders with rich.live.

Usage
-----
    python dashboard/terminal_dashboard.py
    python dashboard/terminal_dashboard.py --store ST1076
    API_URL=http://localhost:8000 python dashboard/terminal_dashboard.py --store ST1008

CLI arguments
-------------
    --store      Store ID to display (overrides STORE_ID env var)
    --api-url    API base URL (overrides API_URL env var)
    --interval   Poll interval in seconds (overrides POLL_INTERVAL_SECONDS env var)

Environment variables (fallbacks)
----------------------------------
    API_URL                  default: http://localhost:8000
    STORE_ID                 default: ST1008
    POLL_INTERVAL_SECONDS    default: 5
"""
from __future__ import annotations

import argparse
import os
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
from rich.console import Group

# ── CLI args (override env vars) ──────────────────────────────────────────────
parser = argparse.ArgumentParser(description="Store Intelligence terminal dashboard")
parser.add_argument("--store",    default=None, help="Store ID e.g. ST1008 or ST1076")
parser.add_argument("--api-url",  default=None, help="API base URL e.g. http://localhost:8000")
parser.add_argument("--interval", default=None, type=int, help="Poll interval in seconds")
args, _ = parser.parse_known_args()

API_URL       = args.api_url  or os.getenv("API_URL",                    "http://localhost:8000")
STORE_ID      = args.store    or os.getenv("STORE_ID",                   "ST1008")
POLL_INTERVAL = args.interval or int(os.getenv("POLL_INTERVAL_SECONDS",  "5"))

console = Console()

SEVERITY_COLORS = {
    "CRITICAL": "bold red",
    "WARN":     "bold yellow",
    "INFO":     "bold cyan",
}

EVENT_TYPE_COLORS = {
    "ENTRY":                 "bright_blue",
    "EXIT":                  "white",
    "ZONE_ENTER":            "cyan",
    "ZONE_EXIT":             "bright_black",
    "ZONE_DWELL":            "yellow",
    "BILLING_QUEUE_JOIN":    "bright_yellow",
    "BILLING_QUEUE_ABANDON": "red",
    "REENTRY":               "magenta",
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
    if depth < 3:  return "green"
    if depth <= 6: return "yellow"
    return "red"


def _pct_bar(value: float, width: int = 20) -> str:
    filled = round(value * width)
    return f"[green]{'█' * filled}[/green]{'░' * (width - filled)}"


def build_header(store_health: dict | None) -> Text:
    now    = datetime.now().strftime("%H:%M:%S")
    status = store_health["status"] if store_health else "UNKNOWN"
    status_col = "green" if status == "OK" else "red"

    last_evt_raw = store_health.get("last_event_at") if store_health else None
    if last_evt_raw and last_evt_raw != "—":
        try:
            last_evt = last_evt_raw[11:19]          # extract HH:MM:SS from ISO string
        except Exception:
            last_evt = last_evt_raw
    else:
        last_evt = "—"

    txt = Text()
    txt.append("  Store Intelligence", style="bold magenta")
    txt.append(f"  —  {STORE_ID}",     style="bold white")
    txt.append(f"  —  {now}",          style="dim")
    txt.append(f"  —  feed: ",         style="dim")
    txt.append(status,                 style=f"bold {status_col}")
    txt.append(f"  —  last event: {last_evt}", style="dim")
    txt.append(f"  —  polling every {POLL_INTERVAL}s", style="dim")
    return txt


def build_health_panel(health: dict | None, store_health: dict | None) -> Panel:
    if health is None:
        return Panel("[red]Health endpoint unreachable[/red]",
                     title="Health", border_style="red")

    t = Table(box=box.SIMPLE, show_header=False, padding=(0, 1))
    t.add_column("Key",   style="cyan",  min_width=22)
    t.add_column("Value", min_width=30)

    db_col    = "green" if health.get("db_connected") else "red"
    db_val    = "connected" if health.get("db_connected") else "disconnected"
    feed_col  = "green" if (store_health or {}).get("status") == "OK" else "red"
    feed_val  = (store_health or {}).get("status", "UNKNOWN")

    last_raw  = (store_health or {}).get("last_event_at")
    last_disp = last_raw[11:19] if last_raw else "—"

    t.add_row("DB",          f"[{db_col}]{db_val}[/{db_col}]")
    t.add_row("Feed status", f"[{feed_col}]{feed_val}[/{feed_col}]")
    t.add_row("Last event",  last_disp)

    border = "green" if feed_val == "OK" else "red"
    return Panel(t, title="[bold]Health[/bold]", border_style=border)


def build_metrics_panel(metrics: dict | None) -> Panel:
    if metrics is None:
        return Panel("[red]API unreachable[/red]", title="Metrics", border_style="red")

    t = Table(box=box.SIMPLE, show_header=False, padding=(0, 1))
    t.add_column("Metric", style="cyan", min_width=22)
    t.add_column("Value",  min_width=30)

    uv   = metrics["unique_visitors"]
    cr   = metrics["conversion_rate"]
    qd   = metrics["current_queue_depth"]
    ar   = metrics["abandonment_rate"]
    qcol = _queue_color(qd)

    queue_row = f"[{qcol}]{qd}[/{qcol}]"
    if qd >= 5:
        queue_row += "  [bold yellow]⚠ open counter[/bold yellow]"

    t.add_row("Unique Visitors",   f"[bold white]{uv}[/bold white]")
    t.add_row("Conversion Rate",   f"{_pct_bar(cr)}  [bold]{cr*100:.1f}%[/bold]")
    t.add_row("Queue Depth",       queue_row)
    t.add_row("Abandonment Rate",  f"[bold]{ar*100:.1f}%[/bold]")

    return Panel(t, title=f"[bold cyan]Metrics — {STORE_ID}[/bold cyan]",
                 border_style="cyan")


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
    t.add_column("Severity",    style="bold",  min_width=10)
    t.add_column("Type",        style="cyan",  min_width=26)
    t.add_column("Description", min_width=38)
    t.add_column("Action",      style="dim",   min_width=30)

    for a in anomalies:
        col = SEVERITY_COLORS.get(a["severity"], "white")
        t.add_row(
            f"[{col}]{a['severity']}[/{col}]",
            a["anomaly_type"],
            a["description"][:50] + ("…" if len(a["description"]) > 50 else ""),
            a.get("suggested_action", "")[:35],
        )

    border = "red" if any(a["severity"] == "CRITICAL" for a in anomalies) else "yellow"
    return Panel(
        t,
        title=f"[bold {border}]Anomalies ({len(anomalies)} active)[/bold {border}]",
        border_style=border,
    )


def build_funnel_panel(funnel: dict | None) -> Panel:
    if funnel is None:
        return Panel("[red]API unreachable[/red]", title="Funnel", border_style="red")

    t = Table(box=box.SIMPLE, show_header=True, padding=(0, 1))
    t.add_column("Stage",    style="cyan",  min_width=16)
    t.add_column("Sessions", justify="right", min_width=10)
    t.add_column("Drop-off", justify="right", min_width=10)

    for s in funnel.get("stages", []):
        drop     = s["drop_off_pct"]
        drop_col = "red" if drop > 50 else "yellow" if drop > 20 else "green"
        t.add_row(
            s["stage"].replace("_", " ").title(),
            str(s["count"]),
            f"[{drop_col}]{drop:.1f}%[/{drop_col}]" if s["stage"] != "entry" else "—",
        )

    session_count = funnel.get("session_count", 0)
    return Panel(
        t,
        title=f"[bold cyan]Funnel — {session_count} sessions[/bold cyan]",
        border_style="cyan",
    )


def build_breakdown_panel(breakdown: dict | None) -> Panel:
    """Event type breakdown — customer vs staff counts per event type."""
    if breakdown is None:
        return Panel("[dim]No breakdown data[/dim]",
                     title="Event Breakdown", border_style="bright_black")

    t = Table(box=box.SIMPLE, show_header=True, padding=(0, 1))
    t.add_column("Event Type", style="cyan",  min_width=26)
    t.add_column("Count",      justify="right", min_width=8)

    by_type = breakdown.get("by_type", {})
    total   = breakdown.get("total", 0)

    for etype, count in sorted(by_type.items(), key=lambda x: -x[1]):
        col = EVENT_TYPE_COLORS.get(etype, "white")
        t.add_row(
            f"[{col}]{etype}[/{col}]",
            str(count),
        )

    cust  = breakdown.get("customer_events", 0)
    staff = breakdown.get("staff_events",    0)
    title = (
        f"[bold]Events — [white]{total}[/white] total  "
        f"[cyan]{cust}[/cyan] customer  "
        f"[bright_black]{staff}[/bright_black] staff[/bold]"
    )
    return Panel(t, title=title, border_style="bright_black")


# ─────────────────────────────────────────────
# Render — assembles all panels each tick
# ─────────────────────────────────────────────

def render(client: httpx.Client) -> Group:
    metrics   = fetch(client, f"/stores/{STORE_ID}/metrics")
    anomalies = fetch(client, f"/stores/{STORE_ID}/anomalies")
    funnel    = fetch(client, f"/stores/{STORE_ID}/funnel")
    health    = fetch(client, "/health")
    breakdown = fetch(client, f"/stores/{STORE_ID}/events/breakdown")

    store_health = next(
        (s for s in (health or {}).get("stores", []) if s["store_id"] == STORE_ID),
        None,
    )

    return Group(
        build_header(store_health),
        Columns(
            [
                build_metrics_panel(metrics),
                build_funnel_panel(funnel),
            ],
            equal=True, expand=True,
        ),
        Columns(
            [
                build_anomaly_panel(anomalies),
                build_breakdown_panel(breakdown),
            ],
            equal=True, expand=True,
        ),
        build_health_panel(health, store_health),
    )


# ─────────────────────────────────────────────
# Main loop
# ─────────────────────────────────────────────

def main() -> None:
    console.print(f"\n[bold cyan]Store Intelligence Dashboard[/bold cyan]")
    console.print(f"API:   [yellow]{API_URL}[/yellow]")
    console.print(f"Store: [yellow]{STORE_ID}[/yellow]  "
                  f"(switch with [dim]--store ST1076[/dim])")
    console.print("Press [bold]Ctrl+C[/bold] to exit\n")

    with httpx.Client() as client:
        with Live(
            render(client),
            console=console,
            refresh_per_second=1,
            screen=True,
        ) as live:
            try:
                while True:
                    time.sleep(POLL_INTERVAL)
                    live.update(render(client))
            except KeyboardInterrupt:
                pass

    console.print("\n[dim]Dashboard stopped.[/dim]")


if __name__ == "__main__":
    main()