import os
import time
import random
import logging
import json
import threading
from flask import Flask, request, jsonify

app = Flask(__name__)

# ── structured JSON logger ──────────────────────────────────────────────────
class JsonFormatter(logging.Formatter):
    def format(self, record):
        return json.dumps({
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "level": record.levelname,
            "service": "worker",
            "message": record.getMessage(),
        })

handler = logging.StreamHandler()
handler.setFormatter(JsonFormatter())
logging.getLogger().addHandler(handler)
logging.getLogger().setLevel(logging.INFO)
log = logging.getLogger(__name__)

def get_fault():
    try:
        with open("/etc/config/FAULT_MODE", "r") as f:
            return f.read().strip()
    except:
        return "none"
    # values: none | crash | cpu_spike | memory_leak | error_burst

# ── in-memory task queue and leak bucket ───────────────────────────────────
task_queue = []
leak_bucket = []   # grows forever under memory_leak fault
queue_lock = threading.Lock()

# ── task processing loop (runs in background thread) ───────────────────────
def process_loop():
    while True:
        fault = get_fault()

        # crash mode
        if fault == "crash" and random.random() < 0.1:
            log.error("Simulated worker crash — exiting process")
            os._exit(1)

        # memory leak mode — keep appending to leak_bucket, never clear it
        if fault == "memory_leak":
            leak_bucket.append("x" * 100_000)  # ~100KB per iteration
            log.warning(f"Memory leak active — leak_bucket size={len(leak_bucket)}")

        # cpu spike mode — busy loop on the processing thread
        if fault == "cpu_spike":
            log.warning("CPU spike active — burning cycles")
            end = time.time() + 1.5
            while time.time() < end:
                pass

        with queue_lock:
            if task_queue:
                task = task_queue.pop(0)
            else:
                task = None

        if task:
            try:
                # error burst mode — fail all task processing
                if fault == "error_burst":
                    raise Exception(f"Simulated task failure — item={task.get('item')}")

                # normal processing — fake some work
                process_time = random.uniform(0.1, 0.5)
                time.sleep(process_time)

                log.info(
                    f"Task completed — item={task.get('item')} "
                    f"quantity={task.get('quantity')} "
                    f"total={task.get('total')} "
                    f"duration={process_time:.2f}s"
                )

            except Exception as e:
                log.error(f"Task processing error — {str(e)}")

        else:
            # nothing in queue — idle heartbeat so logs keep flowing
            log.info(f"Worker idle — queue_size={len(task_queue)} fault={fault}")
            time.sleep(2)


# ── routes ──────────────────────────────────────────────────────────────────
@app.route("/health", methods=["GET"])
def health():
    return jsonify({
        "status": "ok",
        "queue_size": len(task_queue),
        "leak_bucket_size": len(leak_bucket)
    }), 200


@app.route("/enqueue", methods=["POST"])
def enqueue():
    data = request.get_json(silent=True) or {}
    with queue_lock:
        task_queue.append(data)
    log.info(f"Task enqueued — item={data.get('item')} queue_size={len(task_queue)}")
    return jsonify({"status": "enqueued", "queue_size": len(task_queue)}), 202


if __name__ == "__main__":
    log.info("Worker service starting")

    # start the background processing loop in a daemon thread
    t = threading.Thread(target=process_loop, daemon=True)
    t.start()

    app.run(host="0.0.0.0", port=5000)