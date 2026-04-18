import json
import logging
import os
import time
import threading
from collections import defaultdict
from datetime import datetime
from kafka import KafkaConsumer
from rich.console import Console
from rich.table import Table
from rich.live import Live
from rich.panel import Panel
from rich.columns import Columns
from rich.text import Text
from rich import box
from rich.console import Group

# ── config ────────────────────────────────────────────────────────────────────
KAFKA_BROKER = os.environ.get("KAFKA_BROKER", "kafka.monitoring-system.svc.cluster.local:9092")
CORRELATED_TOPIC = os.environ.get("CORRELATED_TOPIC", "correlated-alerts")
ML_TOPIC = os.environ.get("ML_TOPIC", "alerts")
REFRESH_RATE = float(os.environ.get("REFRESH_RATE", "2.0"))

console = Console()

# ── shared state ──────────────────────────────────────────────────────────────
state = {
    "correlated_alerts": [],
    "ml_alerts": [],
    "pod_status": {},
    "alert_counts": defaultdict(int),
    "last_update": None,
}
state_lock = threading.Lock()

SEVERITY_COLORS = {
    "normal": "green", "low": "yellow", "medium": "orange3", "high": "red", "critical": "bold red",
}
FAILURE_CLASS_ICONS = {
    "crashloop_overload": "💥", "memory_leak": "🧠", "error_cascade": "⚠️ ",
    "log_flood": "📋", "resource_pressure": "🔥", "cascading_failure": "🚨",
}

# ── helper for local time ─────────────────────────────────────────────────────
def get_local_now():
    """Returns current system time formatted as HH:MM:SS."""
    return datetime.now().strftime("%H:%M:%S")

def format_to_local(timestamp_str):
    """Parses ISO timestamp (UTC) and converts to local HH:MM:SS."""
    try:
        # Handles 'Z' or '+00:00' suffixes
        dt = datetime.fromisoformat(timestamp_str.replace("Z", "+00:00"))
        return dt.astimezone().strftime("%H:%M:%S")
    except Exception:
        return timestamp_str[-8:] # Fallback to string slicing

# ── kafka consumer thread ─────────────────────────────────────────────────────
def kafka_consumer_thread():
    while True:
        try:
            consumer = KafkaConsumer(
                CORRELATED_TOPIC, ML_TOPIC,
                bootstrap_servers=KAFKA_BROKER,
                auto_offset_reset="latest",
                group_id="dashboard-group",
                value_deserializer=lambda m: json.loads(m.decode("utf-8")),
                consumer_timeout_ms=-1,
            )
            logging.getLogger("kafka").setLevel(logging.WARNING)
            
            for msg in consumer:
                alert = msg.value
                topic = msg.topic
                
                with state_lock:
                    if topic == CORRELATED_TOPIC:
                        state["correlated_alerts"].insert(0, alert)
                        state["correlated_alerts"] = state["correlated_alerts"][:20]
                        fc = alert.get("failure_class", "unknown")
                        state["alert_counts"][fc] += 1
                    
                    elif topic == ML_TOPIC:
                        state["ml_alerts"].insert(0, alert)
                        state["ml_alerts"] = state["ml_alerts"][:50]
                        
                        pod = alert.get("pod", "unknown")
                        state["pod_status"][pod] = {
                            "service": alert.get("service", "unknown"),
                            "score": alert.get("score", 0),
                            "severity": alert.get("severity", "low"),
                            "features": alert.get("features", {}),
                            "last_seen": get_local_now(), # UPDATED: Local Time
                        }
                    
                    state["last_update"] = get_local_now() # UPDATED: Local Time
        except Exception as e:
            time.sleep(5)

# ── render functions ──────────────────────────────────────────────────────────
def render_header():
    last = state.get("last_update") or "waiting..."
    total = sum(state["alert_counts"].values())
    return Panel(
        f"[bold cyan]LogHelmsman[/bold cyan] — K8s Anomaly Detection Pipeline "
        f"[dim]Last update: {last} (Local) | Total alerts: {total}[/dim]",
        style="cyan",
    )

def render_pod_health():
    table = Table(title="Anomalous Pods", box=box.ROUNDED, expand=True, header_style="bold cyan")
    table.add_column("Pod", style="dim")
    table.add_column("Service", style="cyan")
    table.add_column("Score", justify="right")
    table.add_column("Severity", justify="center")
    table.add_column("CPU (m)", justify="right")
    table.add_column("Mem (Mi)", justify="right")
    table.add_column("Last Seen", justify="right", style="dim")

    with state_lock:
        pod_status = dict(state["pod_status"])
        if not pod_status:
            table.add_row("[dim]No anomalous pods detected[/dim]", *[""]*6)
            return table

        for pod, info in sorted(pod_status.items(), key=lambda x: x[1]["score"]):
            severity = info["severity"]
            color = SEVERITY_COLORS.get(severity, "white")
            features = info["features"]
            short_pod = "-".join(pod.split("-")[-2:]) if "-" in pod else pod
            
            table.add_row(
                short_pod, info["service"], f"{info['score']:.4f}",
                f"[{color}]{severity.upper()}[/{color}]",
                str(features.get("cpu_millicores", "—")),
                str(features.get("memory_mi", "—")),
                info["last_seen"]
            )
    return table

def render_correlated_alerts():
    table = Table(title="Correlated Alerts", box=box.ROUNDED, expand=True, header_style="bold magenta")
    table.add_column("Time", style="dim")
    table.add_column("Failure Class", style="bold")
    table.add_column("Severity", justify="center")
    table.add_column("Service", style="cyan")
    table.add_column("Evidence", style="dim")

    with state_lock:
        alerts = list(state["correlated_alerts"])
        if not alerts:
            table.add_row("[dim]No correlated alerts yet[/dim]", *[""]*4)
            return table

        for alert in alerts[:10]:
            severity = alert.get("severity", "low")
            color = SEVERITY_COLORS.get(severity, "white")
            failure_class = alert.get("failure_class", "unknown")
            icon = FAILURE_CLASS_ICONS.get(failure_class, "•")
            
            # UPDATED: Parse the incoming Kafka UTC timestamp to Local
            timestamp = format_to_local(alert.get("timestamp", ""))
            
            evidence = alert.get("evidence", {})
            ev_str = f"{evidence.get('alert_count', '?')} anomalous windows" if isinstance(evidence, dict) else str(evidence)[:50]
            service = alert.get("service") or ", ".join(alert.get("services_affected", []))
            
            table.add_row(timestamp, f"{icon} {failure_class}", f"[{color}]{severity.upper()}[/{color}]", service, ev_str)
    return table

def render_alert_summary():
    table = Table(title="Alert Summary", box=box.SIMPLE, header_style="bold")
    table.add_column("Failure Class")
    table.add_column("Count", justify="right")
    with state_lock:
        counts = dict(state["alert_counts"])
        for fc, count in sorted(counts.items(), key=lambda x: -x[1]):
            table.add_row(f"{FAILURE_CLASS_ICONS.get(fc, '•')} {fc}", str(count))
    return table

def render_layout():
    return Group(render_header(), render_pod_health(), render_correlated_alerts(), render_alert_summary())

def main():
    threading.Thread(target=kafka_consumer_thread, daemon=True).start()
    console.print("[cyan]LogHelmsman Dashboard starting...[/cyan]")
    time.sleep(2)
    with Live(render_layout(), console=console, refresh_per_second=1/REFRESH_RATE, screen=True) as live:
        while True:
            live.update(render_layout())
            time.sleep(REFRESH_RATE)

if __name__ == "__main__":
    main()
