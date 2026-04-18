import json
import logging
import os
import time
from typing import Optional
import joblib
import numpy as np
from fastapi import FastAPI, HTTPException, BackgroundTasks
from pydantic import BaseModel
from kafka import KafkaConsumer, KafkaProducer

# ── logger ────────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

# ── config ────────────────────────────────────────────────────────────────────
MODEL_DIR      = os.environ.get("MODEL_DIR", "/app/model")
KAFKA_BROKER   = os.environ.get("KAFKA_BROKER", "kafka.monitoring-system.svc.cluster.local:9092")
PARSED_TOPIC   = os.environ.get("PARSED_TOPIC", "parsed-logs")
ALERTS_TOPIC   = os.environ.get("ALERTS_TOPIC", "alerts")

# ── load model ────────────────────────────────────────────────────────────────
with open(f"{MODEL_DIR}/features.json") as f:
    FEATURES = json.load(f)

model   = joblib.load(f"{MODEL_DIR}/isolation_forest.pkl")
scaler  = joblib.load(f"{MODEL_DIR}/scaler.pkl")

log.info(f"Model loaded — features={FEATURES}")

# ── kafka producer (for alerts) ───────────────────────────────────────────────
def get_producer():
    while True:
        try:
            producer = KafkaProducer(
                bootstrap_servers=KAFKA_BROKER,
                value_serializer=lambda m: json.dumps(m).encode("utf-8"),
            )
            log.info("Kafka producer connected")
            return producer
        except Exception as e:
            log.warning(f"Kafka not ready — retrying in 5s — {e}")
            time.sleep(5)

producer = get_producer()

# ── FastAPI app ───────────────────────────────────────────────────────────────
app = FastAPI(title="ML Anomaly Detection Service")

class FeatureVector(BaseModel):
    pod:               str
    namespace:         str
    service:           str
    window_end:        str
    log_volume:        float
    cpu_millicores:    float
    memory_mi:         float
    log_volume_delta:  float
    error_rate:        Optional[float] = 0.0
    unique_error_types: Optional[int] = 0
    error_rate_delta:  Optional[float] = 0.0

class PredictionResult(BaseModel):
    pod:      str
    service:  str
    anomaly:  bool
    score:    float
    severity: str
    features: dict

def score_to_severity(score: float) -> str:
    if score > 0:
        return "normal"
    elif score > -0.10:
        return "low"
    elif score > -0.15:
        return "medium"
    else:
        return "high"

@app.get("/health")
def health():
    return {"status": "ok", "features": FEATURES}

ANOMALY_THRESHOLD = -0.05 

@app.post("/predict", response_model=PredictionResult)
def predict(vector: FeatureVector):
    try:
        # extract features in correct order
        values = [[getattr(vector, f) for f in FEATURES]]
        scaled = scaler.transform(values)

        score     = float(model.decision_function(scaled)[0])
        is_anomaly = score < ANOMALY_THRESHOLD
        severity  = score_to_severity(score)

        result = PredictionResult(
            pod=vector.pod,
            service=vector.service,
            anomaly=is_anomaly,
            score=round(score, 4),
            severity=severity,
            features={f: getattr(vector, f) for f in FEATURES}
        )

        # publish to alerts topic if anomalous
        if is_anomaly:
            alert = {
                "type":       "ml_anomaly",
                "pod":        vector.pod,
                "namespace":  vector.namespace,
                "service":    vector.service,
                "window_end": vector.window_end,
                "score":      round(score, 4),
                "severity":   severity,
                "features":   result.features,
                "timestamp":  time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }
            producer.send(ALERTS_TOPIC, value=alert)
            log.warning(f"ANOMALY detected — pod={vector.pod} "
                       f"score={score:.4f} severity={severity}")
        else:
            log.debug(f"Normal — pod={vector.pod} score={score:.4f}")

        return result

    except Exception as e:
        log.error(f"Prediction error — {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/retrain")
def retrain(background_tasks: BackgroundTasks):
    """Retrain model from last 2 hours of parsed-logs Kafka topic."""
    background_tasks.add_task(retrain_task)
    return {"status": "retraining started"}

def retrain_task():
    log.info("Retrain task started — replaying last 2h from parsed-logs")
    try:
        from sklearn.ensemble import IsolationForest
        from sklearn.preprocessing import StandardScaler

        # seek to 2 hours ago
        two_hours_ago = int((time.time() - 7200) * 1000)  # ms timestamp

        consumer = KafkaConsumer(
            bootstrap_servers=KAFKA_BROKER,
            auto_offset_reset="earliest",
            consumer_timeout_ms=30000,
            value_deserializer=lambda m: json.loads(m.decode("utf-8")),
        )

        # assign and seek to timestamp
        from kafka import TopicPartition
        from kafka.structs import OffsetAndTimestamp

        tp = TopicPartition(PARSED_TOPIC, 0)
        consumer.assign([tp])
        offsets = consumer.offsets_for_times({tp: two_hours_ago})

        if offsets[tp]:
            consumer.seek(tp, offsets[tp].offset)
        else:
            consumer.seek_to_beginning(tp)

        # collect records and compute simple features
        records = []
        for msg in consumer:
            r = msg.value
            records.append({
                "log_volume":       1,
                "cpu_millicores":   0,
                "memory_mi":        0,
                "log_volume_delta": 0,
            })
            if len(records) >= 5000:
                break

        if len(records) < 100:
            log.warning(f"Not enough data to retrain — only {len(records)} records")
            return

        import pandas as pd
        df = pd.DataFrame(records)
        X  = scaler.fit_transform(df[FEATURES])

        new_model = IsolationForest(
            n_estimators=200,
            contamination=0.03,
            random_state=42
        )
        new_model.fit(X)

        # hot-swap the model
        global model
        model = new_model
        joblib.dump(model, f"{MODEL_DIR}/isolation_forest.pkl")

        log.info(f"Retrain complete — {len(records)} records used")

    except Exception as e:
        log.error(f"Retrain failed — {e}")