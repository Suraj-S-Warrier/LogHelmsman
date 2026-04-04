import random
import requests
import time
import os
FRONTEND_URL = os.environ.get("FRONTEND_URL", "http://frontend:5000")
while True:
    requests.post(f"{FRONTEND_URL}/order", json={
        "item": random.choice(["book", "phone", "laptop"]),
        "quantity": random.randint(1, 5)
    })
    time.sleep(random.uniform(0.5, 3.0))
