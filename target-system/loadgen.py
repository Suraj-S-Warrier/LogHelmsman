import random
import requests
import time
import os
import logging

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

FRONTEND_URL = os.environ.get("FRONTEND_URL", "http://frontend:5000")
log.info(f"Loadgen starting — targeting {FRONTEND_URL}")

while True:
    try:
        item = random.choice(["book", "phone", "laptop"])
        quantity = random.randint(1, 5)
        resp = requests.post(
            f"{FRONTEND_URL}/order",
            json={"item": item, "quantity": quantity},
            timeout=5
        )
        log.info(f"Order sent — item={item} quantity={quantity} status={resp.status_code}")
    except Exception as e:
        log.error(f"Request failed — {str(e)}")
    time.sleep(random.uniform(0.5, 3.0))