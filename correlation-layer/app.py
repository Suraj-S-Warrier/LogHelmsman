import json
import logging
import os
import time
from collections import defaultdict
from kafka import KafkaConsumer, KafkaProducer
import yaml

# ── logger ────────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

# ── config ────────────────────────────────────────────────────────────────────
KAFKA_BROKER      = os.environ.get("KAFKA_BROKER", "kafka.monitoring-system.svc.cluster.local:9092")
INPUT_TOPIC       = os.environ.get("INPUT_TOPIC", "alerts")
OUTPUT_TOPIC      = os.environ.get("OUTPUT_TOPIC", "correlated-alerts")
TAXONOMY_FILE     = os.environ.get("TAXONOMY_FILE", "/app/config/taxonomy.yaml")
HISTORY_WINDOW    = int(os.environ.get("HISTORY_WINDOW", "300"))   # 5 min of alert history per pod
MIN_ANOMALY_COUNT = int(os.environ.get("MIN_ANOMALY_COUNT", "2"))   # min alerts before correlating

# ── load failure taxonomy ─────────────────────────────────────────────────────
def load_taxonomy():
    with open(TAXONOMY_FILE) as f:
        return yaml.safe_load(f)

taxonomy = load_taxonomy()
log.info(f"Loaded taxonomy — {len(taxonomy['failure_classes'])} failure classes")

# ── per-pod alert history ─────────────────────────────────────────────────────
# structure: {pod: [(timestamp, alert), ...]}
pod_history = defaultdict(list)

def evict_old_history():
    cutoff = time.time() - HISTORY_WINDOW
    for pod in list(pod_history.keys()):
        pod_history[pod] = [
            (ts, alert) for ts, alert in pod_history[pod]
            if ts > cutoff
        ]
        if not pod_history[pod]:
            del pod_history[pod]

# ── feature signal extractors ─────────────────────────────────────────────────
def has_cpu_spike(alert):
    return alert.get("features", {}).get("cpu_millicores", 0) > 50

def has_memory_growth(alert):
    return alert.get("features", {}).get("memory_mi", 0) > 60

def has_log_spike(alert):
    return alert.get("features", {}).get("log_volume_delta", 0) > 80

def has_error_spike(alert):
    return alert.get("features", {}).get("error_rate", 0) > 0.2

def is_high_severity(alert):
    return alert.get("severity") in ("high", "medium")

# ── signal extractors map (used by taxonomy matching) ─────────────────────────
SIGNAL_CHECKERS = {
    "cpu_spike":      has_cpu_spike,
    "memory_growth":  has_memory_growth,
    "log_spike":      has_log_spike,
    "error_spike":    has_error_spike,
    "restart_spike":  lambda a: a.get("features", {}).get("restart_delta", 0) > 2,
    "high_severity":  is_high_severity,
}

# ── classify failure based on taxonomy ───────────────────────────────────────
def classify_failure(recent_alerts):
    """
    Check recent alerts for a pod against taxonomy conditions.
    Returns the first matching failure class or None.
    """
    # aggregate signals across recent alerts for this pod
    active_signals = set()
    for _, alert in recent_alerts:
        for signal, checker in SIGNAL_CHECKERS.items():
            if checker(alert):
                active_signals.add(signal)

    log.debug(f"Active signals: {active_signals}")

    for failure_class in taxonomy["failure_classes"]:
        required = set(failure_class["conditions"])
        if required.issubset(active_signals):
            return failure_class["name"], failure_class["severity"]

    return None, None

# ── cross-service correlation ─────────────────────────────────────────────────

EXCLUDED_SERVICES = {"loadgen"}

def check_cascading_failure(producer):
    """
    If multiple services are anomalous simultaneously → cascading failure.
    """
    now = time.time()
    cutoff = now - 120   # last 2 minutes

    # find services with recent alerts
    services_with_alerts = set()
    pods_with_alerts = []
    for pod, history in pod_history.items():
        recent = [(ts, a) for ts, a in history if ts > cutoff]
        if recent:
            service = recent[-1][1].get("service", "unknown")
            if service in EXCLUDED_SERVICES:   
                continue
            services_with_alerts.add(service)
            pods_with_alerts.append(pod)

    if len(services_with_alerts) >= 2:
        alert = {
            "type":             "correlated_alert",
            "failure_class":    "cascading_failure",
            "severity":         "critical",
            "services_affected": list(services_with_alerts),
            "pods_affected":    pods_with_alerts,
            "evidence":         "multiple services anomalous simultaneously",
            "timestamp":        time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        producer.send(OUTPUT_TOPIC, value=alert)
        log.warning(f"CASCADING FAILURE detected — "
                   f"services={services_with_alerts} "
                   f"pods={pods_with_alerts}")
        return True
    return False

last_cascade_check = 0

# ── process incoming alert ─────────────────────────────────────────────────────
def process_alert(alert, producer):
    global last_cascade_check

    pod       = alert.get("pod", "unknown")
    service   = alert.get("service", "unknown")
    severity  = alert.get("severity", "low")
    score     = alert.get("score", 0)

    if service in EXCLUDED_SERVICES:      
        log.debug(f"Ignoring excluded service — {service}")
        return

    # add to history
    pod_history[pod].append((time.time(), alert))

    # evict old records
    evict_old_history()

    # get recent history for this pod
    recent = pod_history[pod]

    log.info(f"Alert received — pod={pod} service={service} "
             f"severity={severity} score={score:.4f} "
             f"history_depth={len(recent)}")

    # need minimum alerts before correlating — filters single-window false positives
    if len(recent) < MIN_ANOMALY_COUNT:
        log.info(f"Not enough history for {pod} — "
                 f"{len(recent)}/{MIN_ANOMALY_COUNT} alerts — skipping correlation")
        return

    # classify against taxonomy
    failure_class, class_severity = classify_failure(recent)

    if failure_class:
        correlated = {
            "type":          "correlated_alert",
            "failure_class": failure_class,
            "severity":      class_severity,
            "pod":           pod,
            "service":       service,
            "evidence":      {
                "alert_count":    len(recent),
                "window_seconds": HISTORY_WINDOW,
                "latest_score":   score,
                "latest_features": alert.get("features", {}),
            },
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        producer.send(OUTPUT_TOPIC, value=correlated)
        log.warning(f"CORRELATED ALERT — class={failure_class} "
                   f"severity={class_severity} pod={pod}")

    # check for cascading failure every 60 seconds
    if time.time() - last_cascade_check > 60:
        check_cascading_failure(producer)
        last_cascade_check = time.time()

# ── main ──────────────────────────────────────────────────────────────────────
def main():
    log.info("Correlation layer starting")

    while True:
        try:
            consumer = KafkaConsumer(
                INPUT_TOPIC,
                bootstrap_servers=KAFKA_BROKER,
                auto_offset_reset="latest",
                group_id="correlation-layer-group",
                value_deserializer=lambda m: json.loads(m.decode("utf-8")),
                consumer_timeout_ms=-1,
            )
            producer = KafkaProducer(
                bootstrap_servers=KAFKA_BROKER,
                value_serializer=lambda m: json.dumps(m).encode("utf-8"),
            )
            log.info("Connected to Kafka")
            break
        except Exception as e:
            log.warning(f"Kafka not ready — retrying in 5s — {e}")
            time.sleep(5)

    for msg in consumer:
        try:
            alert = msg.value
            process_alert(alert, producer)
        except Exception as e:
            log.error(f"Error processing alert — {e}")

if __name__ == "__main__":
    main()