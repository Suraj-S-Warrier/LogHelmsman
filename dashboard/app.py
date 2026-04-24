import json
import logging
import os
import time
import threading
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from kafka import KafkaConsumer
from rich.console import Console
from rich.table import Table
from rich.live import Live
from rich.panel import Panel
from rich.text import Text
from rich import box
from rich.console import Group

# ── config ────────────────────────────────────────────────────────────────────
KAFKA_BROKER = os.environ.get("KAFKA_BROKER", "kafka.monitoring-system.svc.cluster.local:9092")
CORRELATED_TOPIC = os.environ.get("CORRELATED_TOPIC", "correlated-alerts")
ML_TOPIC = os.environ.get("ML_TOPIC", "alerts")
REFRESH_RATE = float(os.environ.get("REFRESH_RATE", "2.0"))

ALERT_TTL = 90  # seconds (controls how long alerts stay visible)

IST = timezone(timedelta(hours=5, minutes=30))

console = Console()

# ── shared state ──────────────────────────────────────────────────────────────
state = {
    "correlated_alerts": [],
    "pod_status": {},
    "alert_counts": defaultdict(int),
    "last_update": None,
}
state_lock = threading.Lock()

SEVERITY_COLORS = {
    "normal": "green",
    "low": "yellow",
    "medium": "orange3",
    "high": "red",
    "critical": "bold red",
}

FAILURE_CLASS_ICONS = {
    "crashloop_overload": "💥",
    "memory_leak": "🧠",
    "error_cascade": "⚠️",
    "log_flood": "📋",
    "resource_pressure": "🔥",
    "cascading_failure": "🚨",
}

# ── time helpers ──────────────────────────────────────────────────────────────
def now_ist():
    return datetime.now(IST)

def format_to_ist(timestamp_str):
    try:
        dt = datetime.fromisoformat(timestamp_str.replace("Z", "+00:00"))
        return dt.astimezone(IST)
    except:
        return now_ist()

def format_time(dt):
    return dt.strftime("%H:%M:%S")

# ── kafka consumer ────────────────────────────────────────────────────────────
def kafka_consumer_thread():
    while True:
        try:
            consumer = KafkaConsumer(
                CORRELATED_TOPIC,
                ML_TOPIC,
                bootstrap_servers=KAFKA_BROKER,
                auto_offset_reset="latest",
                group_id="dashboard-group",
                value_deserializer=lambda m: json.loads(m.decode("utf-8")),
            )

            logging.getLogger("kafka").setLevel(logging.WARNING)

            for msg in consumer:
                alert = msg.value
                topic = msg.topic
                now = now_ist()

                with state_lock:

                    # ── Correlated alerts ──
                    if topic == CORRELATED_TOPIC:
                        ts = format_to_ist(alert.get("timestamp", ""))
                        alert["parsed_time"] = ts

                        state["correlated_alerts"].append(alert)

                        fc = alert.get("failure_class", "unknown")
                        state["alert_counts"][fc] += 1

                    # ── ML alerts (pod status) ──
                    elif topic == ML_TOPIC:
                        pod = alert.get("pod", "unknown")

                        state["pod_status"][pod] = {
                            "service": alert.get("service", "unknown"),
                            "score": alert.get("score", 0),
                            "severity": alert.get("severity", "low"),
                            "features": alert.get("features", {}),
                            "last_seen": now,
                        }

                    # ── CLEANUP OLD DATA ──
                    # remove stale correlated alerts
                    state["correlated_alerts"] = [
                        a for a in state["correlated_alerts"]
                        if (now - a["parsed_time"]).total_seconds() < ALERT_TTL
                    ]

                    # remove stale pod anomalies
                    state["pod_status"] = {
                        pod: info for pod, info in state["pod_status"].items()
                        if (now - info["last_seen"]).total_seconds() < ALERT_TTL
                    }

                    state["last_update"] = now

        except Exception:
            time.sleep(5)

# ── render functions ──────────────────────────────────────────────────────────
def render_header():
    last = format_time(state["last_update"]) if state["last_update"] else "waiting..."
    total = sum(state["alert_counts"].values())

    return Panel(
        f"[bold cyan]LogHelmsman[/bold cyan] — K8s Anomaly Detection Pipeline "
        f"[dim]Last update: {last} IST | Total alerts: {total}[/dim]",
        style="cyan",
    )

def render_pod_health():
    table = Table(title="Active Anomalous Pods", box=box.ROUNDED, expand=True)
    table.add_column("Pod")
    table.add_column("Service")
    table.add_column("Score", justify="right")
    table.add_column("Severity", justify="center")
    table.add_column("CPU")
    table.add_column("Mem")
    table.add_column("Last Seen")

    with state_lock:
        if not state["pod_status"]:
            table.add_row("[dim]No active anomalies[/dim]", *[""]*6)
            return table

        for pod, info in sorted(state["pod_status"].items(), key=lambda x: x[1]["score"]):
            color = SEVERITY_COLORS.get(info["severity"], "white")
            features = info["features"]

            table.add_row(
                pod.split("-")[-1],
                info["service"],
                f"{info['score']:.4f}",
                f"[{color}]{info['severity'].upper()}[/{color}]",
                str(features.get("cpu_millicores", "—")),
                str(features.get("memory_mi", "—")),
                format_time(info["last_seen"])
            )
    return table

def render_correlated_alerts():
    table = Table(title="Active Correlated Alerts", box=box.ROUNDED, expand=True)
    table.add_column("Time")
    table.add_column("Failure")
    table.add_column("Severity")
    table.add_column("Service")
    table.add_column("Evidence")

    with state_lock:
        if not state["correlated_alerts"]:
            table.add_row("[dim]No active alerts[/dim]", *[""]*4)
            return table

        for alert in state["correlated_alerts"][-10:]:
            color = SEVERITY_COLORS.get(alert.get("severity", "low"), "white")
            fc = alert.get("failure_class", "unknown")
            icon = FAILURE_CLASS_ICONS.get(fc, "•")

            evidence = alert.get("evidence", {})
            ev_str = str(evidence.get("alert_count", "?"))

            service = alert.get("service") or ", ".join(alert.get("services_affected", []))

            table.add_row(
                format_time(alert["parsed_time"]),
                f"{icon} {fc}",
                f"[{color}]{alert.get('severity','low').upper()}[/{color}]",
                service,
                ev_str
            )
    return table

def render_layout():
    return Group(
        render_header(),
        render_pod_health(),
        render_correlated_alerts(),
    )

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