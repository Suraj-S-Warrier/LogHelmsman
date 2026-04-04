import os
import time
import random
import logging
import json
import requests
from flask import Flask, request, jsonify

app = Flask(__name__)

# ── structured JSON logger ──────────────────────────────────────────────────
class JsonFormatter(logging.Formatter):
    def format(self, record):
        return json.dumps({
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "level": record.levelname,
            "service": "frontend",
            "message": record.getMessage(),
        })

handler = logging.StreamHandler()
handler.setFormatter(JsonFormatter())
logging.getLogger().addHandler(handler)
logging.getLogger().setLevel(logging.INFO)
log = logging.getLogger(__name__)

# ── fault injection config (read from env, set via ConfigMap) ───────────────
def get_fault():
    try:
        with open("/etc/config/FAULT_MODE", "r") as f:
            return f.read().strip()
    except:
        return "none"

BACKEND_URL = os.environ.get("BACKEND_URL", "http://backend:5000")

# ── routes ──────────────────────────────────────────────────────────────────
@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"}), 200


@app.route("/order", methods=["POST"])
def create_order():
    fault = get_fault()

    # crash mode — random exit, causes CrashLoopBackOff
    if fault == "crash" and random.random() < 0.15:
        log.error("Simulated crash — exiting process")
        os._exit(1)

    # log spam mode
    if fault == "log_spam":
        for _ in range(50):
            log.error("ERROR flood — simulated log spam event")

    data = request.get_json(silent=True) or {}
    item = data.get("item", "unknown")
    quantity = data.get("quantity", 1)

    log.info(f"Order received — item={item} quantity={quantity}")

    # error burst mode — don't even call backend
    if fault == "error_burst":
        log.error(f"Simulated error burst — dropping order item={item}")
        return jsonify({"error": "service degraded"}), 503

    # normal path — forward to backend
    try:
        resp = requests.post(
            f"{BACKEND_URL}/process",
            json={"item": item, "quantity": quantity},
            timeout=5
        )
        resp.raise_for_status()
        log.info(f"Order forwarded to backend — item={item} status={resp.status_code}")
        return jsonify(resp.json()), resp.status_code

    except requests.exceptions.Timeout:
        log.error(f"Backend timeout — item={item}")
        return jsonify({"error": "backend timeout"}), 504

    except requests.exceptions.ConnectionError:
        log.error(f"Backend unreachable — item={item}")
        return jsonify({"error": "backend unreachable"}), 502

    except Exception as e:
        log.error(f"Unexpected error — {str(e)}")
        return jsonify({"error": "internal error"}), 500


if __name__ == "__main__":
    log.info("Frontend service starting")
    app.run(host="0.0.0.0", port=5000)