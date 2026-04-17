import json
import logging
import os
import time
import threading
from collections import defaultdict, deque
from kafka import KafkaConsumer, KafkaProducer
from kubernetes import client, config
import requests as http_requests

# ── logger ───────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

# ── config ───────────────────────────────────────────────────────────────────
KAFKA_BROKER   = os.environ.get("KAFKA_BROKER", "kafka.monitoring-system.svc.cluster.local:9092")
INPUT_TOPIC    = os.environ.get("INPUT_TOPIC", "parsed-logs")
OUTPUT_TOPIC   = os.environ.get("OUTPUT_TOPIC", "feature-vectors")
WINDOW_SECONDS = int(os.environ.get("WINDOW_SECONDS", "60"))
BURN_IN_MODE   = os.environ.get("BURN_IN_MODE", "true").lower() == "true"
BURN_IN_FILE   = os.environ.get("BURN_IN_FILE", "/data/burn_in.csv")
ML_SERVICE_URL = os.environ.get("ML_SERVICE_URL", "http://ml-service:8000/predict")

# ── kubernetes client for metrics ────────────────────────────────────────────
def init_k8s():
    try:
        config.load_incluster_config()
        return client.CustomObjectsApi(), client.CoreV1Api()
    except Exception as e:
        log.warning(f"Could not init K8s client — {e}")
        return None, None

metrics_api, core_api = init_k8s()

def get_pod_metrics(pod_name, namespace):
    """Fetch CPU and memory usage from metrics-server."""
    try:
        metrics = metrics_api.get_namespaced_custom_object(
            group="metrics.k8s.io",
            version="v1beta1",
            namespace=namespace,
            plural="pods",
            name=pod_name
        )
        containers = metrics.get("containers", [])
        total_cpu_m = 0
        total_mem_mi = 0
        for c in containers:
            usage = c.get("usage", {})
            cpu = usage.get("cpu", "0m")
            mem = usage.get("memory", "0Ki")
            # parse cpu: "125m" → 125 millicores
            if cpu.endswith("m"):
                total_cpu_m += int(cpu[:-1])
            elif cpu.endswith("n"):
                total_cpu_m += int(cpu[:-1]) / 1_000_000
            # parse memory: "64Mi" → 64, "65536Ki" → 64
            if mem.endswith("Ki"):
                total_mem_mi += int(mem[:-2]) / 1024
            elif mem.endswith("Mi"):
                total_mem_mi += int(mem[:-2])
            elif mem.endswith("Gi"):
                total_mem_mi += int(mem[:-2]) * 1024
        return round(total_cpu_m, 2), round(total_mem_mi, 2)
    except Exception as e:
        log.warning(f"Could not get metrics for {pod_name} — {e}")
        return 0.0, 0.0

# ── per-pod window state ──────────────────────────────────────────────────────
class PodWindow:
    def __init__(self, pod, namespace, service):
        self.pod       = pod
        self.namespace = namespace
        self.service   = service
        self.logs      = deque()   # (timestamp, is_error, message)
        self.last_restart_count  = 0
        self.prev_error_rate     = 0.0
        self.prev_log_volume     = 0
        self.lock = threading.Lock()

    def add_log(self, timestamp, is_error, message, restart_count):
        with self.lock:
            self.logs.append((timestamp, is_error, message))
            self.last_restart_count = restart_count

    def evict_old(self, cutoff_time):
        """Remove logs older than the window."""
        with self.lock:
            while self.logs and self.logs[0][0] < cutoff_time:
                self.logs.popleft()

    def compute_features(self, cpu_m, mem_mi):
        with self.lock:
            logs = list(self.logs)

        total      = len(logs)
        errors     = sum(1 for _, is_error, _ in logs if is_error)
        error_msgs = set(msg for _, is_error, msg in logs if is_error)

        error_rate        = round(errors / total, 4) if total > 0 else 0.0
        log_volume        = total
        unique_error_types = len(error_msgs)

        # delta features — rate of change vs previous window
        error_rate_delta  = round(error_rate - self.prev_error_rate, 4)
        log_volume_delta  = log_volume - self.prev_log_volume

        # update previous window values
        self.prev_error_rate  = error_rate
        self.prev_log_volume  = log_volume

        return {
            "pod":               self.pod,
            "namespace":         self.namespace,
            "service":           self.service,
            "window_end":        time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "error_rate":        error_rate,
            "log_volume":        log_volume,
            "unique_error_types": unique_error_types,
            "cpu_millicores":    cpu_m,
            "memory_mi":         mem_mi,
            "error_rate_delta":  error_rate_delta,
            "log_volume_delta":  log_volume_delta,
        }

# ── global pod windows ────────────────────────────────────────────────────────
pod_windows = {}
pod_windows_lock = threading.Lock()

def get_or_create_window(pod, namespace, service):
    with pod_windows_lock:
        if pod not in pod_windows:
            pod_windows[pod] = PodWindow(pod, namespace, service)
        return pod_windows[pod]

# ── burn-in CSV writer ────────────────────────────────────────────────────────
CSV_HEADERS = [
    "pod", "namespace", "service", "window_end",
    "error_rate", "log_volume", "unique_error_types",
    "cpu_millicores", "memory_mi",
    "error_rate_delta", "log_volume_delta"
]

def init_csv():
    os.makedirs(os.path.dirname(BURN_IN_FILE), exist_ok=True)
    if not os.path.exists(BURN_IN_FILE):
        with open(BURN_IN_FILE, "w") as f:
            f.write(",".join(CSV_HEADERS) + "\n")
        log.info(f"Burn-in CSV initialized at {BURN_IN_FILE}")

def write_to_csv(features):
    with open(BURN_IN_FILE, "a") as f:
        row = ",".join(str(features.get(h, "")) for h in CSV_HEADERS)
        f.write(row + "\n")

# ── emit feature vector ───────────────────────────────────────────────────────
def emit_features(features, producer):
    if BURN_IN_MODE:
        write_to_csv(features)
        log.info(f"[BURN-IN] {features['pod']} | "
                 f"err_rate={features['error_rate']} "
                 f"vol={features['log_volume']} "
                 f"cpu={features['cpu_millicores']}m "
                 f"mem={features['memory_mi']}Mi")
    else:
        # send to ML service
        try:
            resp = http_requests.post(ML_SERVICE_URL, json=features, timeout=3)
            result = resp.json()
            log.info(f"[INFERENCE] {features['pod']} | "
                     f"anomaly={result.get('anomaly')} "
                     f"score={result.get('score')}")
            # also publish feature vector to Kafka for the correlation layer
            producer.send(OUTPUT_TOPIC, value={**features, **result})
        except Exception as e:
            log.error(f"ML service call failed — {e}")

# ── window emission loop (runs every WINDOW_SECONDS) ─────────────────────────
def emission_loop(producer):
    while True:
        time.sleep(WINDOW_SECONDS)
        cutoff = time.time() - WINDOW_SECONDS

        with pod_windows_lock:
            pods = list(pod_windows.values())

        for window in pods:
            window.evict_old(cutoff)
            cpu_m, mem_mi = get_pod_metrics(window.pod, window.namespace)
            features = window.compute_features(cpu_m, mem_mi)

            # skip pods with no logs in this window
            if features["log_volume"] == 0:
                continue

            emit_features(features, producer)

# ── consumer loop ─────────────────────────────────────────────────────────────
def main():
    log.info(f"Feature engineer starting — BURN_IN_MODE={BURN_IN_MODE}")

    if BURN_IN_MODE:
        init_csv()

    # wait for Kafka
    while True:
        try:
            consumer = KafkaConsumer(
                INPUT_TOPIC,
                bootstrap_servers=KAFKA_BROKER,
                auto_offset_reset="earliest",
                group_id="feature-engineer-group",
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

    # start emission loop in background thread
    t = threading.Thread(target=emission_loop, args=(producer,), daemon=True)
    t.start()
    log.info(f"Emission loop started — window={WINDOW_SECONDS}s")

    # consume parsed-logs and populate windows
    for msg in consumer:
        try:
            record = msg.value
            pod       = record.get("pod", "unknown")
            namespace = record.get("namespace", "unknown")
            service   = record.get("service", "unknown")
            is_error  = record.get("is_error", False)
            message   = record.get("message", "")
            restarts  = record.get("restart_count", 0)

            try:
                ts = time.mktime(
                    time.strptime(
                        record.get("timestamp", "")[:19],
                        "%Y-%m-%dT%H:%M:%S"
                    )
                )
            except Exception:
                ts = time.time()

            window = get_or_create_window(pod, namespace, service)
            window.add_log(ts, is_error, message, restarts)

        except Exception as e:
            log.error(f"Error consuming record — {e}")

if __name__ == "__main__":
    main()