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
            "service": "backend",
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
    # values: none | crash | error_burst | cpu_spike

WORKER_URL = os.environ.get("WORKER_URL", "http://worker:5000")

# ── fake inventory check ────────────────────────────────────────────────────
INVENTORY = {"book": 100, "phone": 50, "laptop": 20, "unknown": 10}

def check_inventory(item, quantity):
    stock = INVENTORY.get(item, 0)
    if stock < quantity:
        raise ValueError(f"Insufficient stock for {item} — available={stock} requested={quantity}")
    return stock

def apply_discount(item, quantity):
    discount = 0.1 if quantity > 3 else 0.0
    base_prices = {"book": 15, "phone": 699, "laptop": 1299, "unknown": 9}
    price = base_prices.get(item, 9)
    total = price * quantity * (1 - discount)
    return round(total, 2)

# ── cpu spike helper ────────────────────────────────────────────────────────
def burn_cpu(seconds=2):
    end = time.time() + seconds
    while time.time() < end:
        pass  # busy loop

# ── routes ──────────────────────────────────────────────────────────────────
@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"}), 200


@app.route("/process", methods=["POST"])
def process_order():
    fault = get_fault()

    # crash mode
    if fault == "crash" and random.random() < 0.15:
        log.error("Simulated crash — exiting process")
        os._exit(1)

    # cpu spike mode
    if fault == "cpu_spike":
        log.warning("CPU spike triggered — running busy loop")
        burn_cpu(seconds=2)

    data = request.get_json(silent=True) or {}
    item = data.get("item", "unknown")
    quantity = data.get("quantity", 1)

    log.info(f"Processing order — item={item} quantity={quantity}")

    # error burst mode
    if fault == "error_burst":
        log.error(f"Simulated processing failure — item={item}")
        return jsonify({"error": "processing failed"}), 500

    try:
        stock = check_inventory(item, quantity)
        log.info(f"Inventory check passed — item={item} stock={stock}")

        total = apply_discount(item, quantity)
        log.info(f"Order priced — item={item} quantity={quantity} total={total}")

        # enqueue follow-up task in worker
        try:
            requests.post(
                f"{WORKER_URL}/enqueue",
                json={"item": item, "quantity": quantity, "total": total},
                timeout=3
            )
            log.info(f"Task enqueued in worker — item={item}")
        except Exception as e:
            # worker being down shouldn't fail the order
            log.warning(f"Worker enqueue failed (non-fatal) — {str(e)}")

        return jsonify({
            "status": "processed",
            "item": item,
            "quantity": quantity,
            "total": total
        }), 200

    except ValueError as e:
        log.error(f"Inventory error — {str(e)}")
        return jsonify({"error": str(e)}), 400

    except Exception as e:
        log.error(f"Unexpected processing error — {str(e)}")
        return jsonify({"error": "internal error"}), 500


if __name__ == "__main__":
    log.info("Backend service starting")
    app.run(host="0.0.0.0", port=5000)