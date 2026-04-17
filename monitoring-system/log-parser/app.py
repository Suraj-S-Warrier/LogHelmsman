import json
import logging
import os
import re
import time
import threading
from kafka import KafkaConsumer, KafkaProducer
from kubernetes import client, config

# ── logger ──────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

# ── config ───────────────────────────────────────────────────────────────────
KAFKA_BROKER = os.environ.get("KAFKA_BROKER", "kafka.monitoring-system.svc.cluster.local:9092")
INPUT_TOPIC   = os.environ.get("INPUT_TOPIC", "raw-logs")
OUTPUT_TOPIC  = os.environ.get("OUTPUT_TOPIC", "parsed-logs")

# ── error pattern matching ───────────────────────────────────────────────────
ERROR_PATTERNS = re.compile(
    r'\b(ERROR|WARN|WARNING|FATAL|CRITICAL|Exception|Traceback|Error)\b',
    re.IGNORECASE
)

# ── kubernetes client for restart counts ─────────────────────────────────────
def init_k8s():
    try:
        config.load_incluster_config()
        return client.CoreV1Api()
    except Exception as e:
        log.warning(f"Could not init K8s client — {e}")
        return None

k8s_api = init_k8s()
restart_cache = {}
restart_cache_lock = threading.Lock()

def get_restart_count(pod_name, namespace):
    """Poll K8s API for pod restart count. Cached per pod."""
    try:
        pod = k8s_api.read_namespaced_pod(name=pod_name, namespace=namespace)
        restarts = 0
        for cs in pod.status.container_statuses or []:
            restarts += cs.restart_count
        return restarts
    except Exception as e:
        log.warning(f"Could not get restart count for {pod_name} — {e}")
        return 0

# ── parse a single raw record ────────────────────────────────────────────────
def parse_record(raw):
    try:
        # raw is the full Fluent Bit enriched record
        pod_name       = raw.get("kubernetes", {}).get("pod_name", "unknown")
        namespace      = raw.get("kubernetes", {}).get("namespace_name", "unknown")
        container_name = raw.get("kubernetes", {}).get("container_name", "unknown")
        node           = raw.get("kubernetes", {}).get("host", "unknown")
        timestamp      = raw.get("time", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))

        # the actual log line is a JSON string inside the "log" field
        log_str = raw.get("log", "")
        try:
            log_json = json.loads(log_str)
            level   = log_json.get("level", "INFO")
            message = log_json.get("message", log_str)
            service = log_json.get("service", container_name)
        except (json.JSONDecodeError, TypeError):
            # not JSON — fall back to regex level detection
            level   = "ERROR" if ERROR_PATTERNS.search(log_str) else "INFO"
            message = log_str.strip()
            service = container_name

        # is this an error line?
        is_error = level.upper() in ("ERROR", "WARN", "WARNING", "FATAL", "CRITICAL") \
                   or bool(ERROR_PATTERNS.search(message))

        # restart count from K8s API
        restart_count = 0
        if k8s_api:
            with restart_cache_lock:
                restart_count = get_restart_count(pod_name, namespace)

        return {
            "timestamp":     timestamp,
            "pod":           pod_name,
            "namespace":     namespace,
            "service":       service,
            "container":     container_name,
            "node":          node,
            "level":         level.upper(),
            "message":       message,
            "is_error":      is_error,
            "restart_count": restart_count,
        }

    except Exception as e:
        log.error(f"Failed to parse record — {e} — raw={str(raw)[:200]}")
        return None

# ── main loop ────────────────────────────────────────────────────────────────
def main():
    log.info(f"Log parser starting — {KAFKA_BROKER} | {INPUT_TOPIC} → {OUTPUT_TOPIC}")

    # wait for Kafka to be ready
    while True:
        try:
            consumer = KafkaConsumer(
                INPUT_TOPIC,
                bootstrap_servers=KAFKA_BROKER,
                auto_offset_reset="earliest",
                group_id="log-parser-group",
                value_deserializer=lambda m: json.loads(m.decode("utf-8")),
                consumer_timeout_ms=-1,    # block forever — no timeout
            )
            producer = KafkaProducer(
                bootstrap_servers=KAFKA_BROKER,
                value_serializer=lambda m: json.dumps(m).encode("utf-8"),
            )
            log.info("Connected to Kafka")
            break
        except Exception as e:
            log.warning(f"Kafka not ready yet — retrying in 5s — {e}")
            time.sleep(5)

    processed = 0
    errors    = 0

    for msg in consumer:
        try:
            raw    = msg.value
            parsed = parse_record(raw)

            if parsed is None:
                errors += 1
                continue

            producer.send(OUTPUT_TOPIC, value=parsed)
            processed += 1

            if processed % 100 == 0:
                log.info(f"Processed {processed} records — errors={errors}")

            # log every parsed record at debug level so you can verify
            log.debug(f"Parsed — pod={parsed['pod']} level={parsed['level']} "
                      f"is_error={parsed['is_error']} restarts={parsed['restart_count']}")

        except Exception as e:
            log.error(f"Error processing message — {e}")
            errors += 1

if __name__ == "__main__":
    main()