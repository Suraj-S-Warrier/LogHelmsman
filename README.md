# LogHelmsman 🔭

> Real-time Kubernetes log anomaly detection pipeline using Kafka, Isolation Forest, and a declarative correlation engine — deployed entirely on-cluster.

![Architecture](https://img.shields.io/badge/architecture-event--driven-blue)
![Kubernetes](https://img.shields.io/badge/kubernetes-1.35-326CE5?logo=kubernetes)
![Kafka](https://img.shields.io/badge/kafka-3.9-231F20?logo=apachekafka)
![Python](https://img.shields.io/badge/python-3.11-3776AB?logo=python)

---

## What is this?

LogHelmsman is a self-hosted observability pipeline that monitors a Kubernetes cluster in real time, detects anomalous pod behavior using an unsupervised ML model, and classifies failures into named categories using a declarative correlation engine.

The pipeline observes another set of services running in the same cluster — making it a system that monitors itself. Both the target workloads and the monitoring stack run on the same 3-node Minikube cluster, isolated via Kubernetes namespaces and enforced by Calico NetworkPolicy.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                    namespace: target-system                      │
│                                                                  │
│  loadgen ──► frontend ──► backend ──► worker                    │
│  (traffic    (Flask API)  (order      (background               │
│   generator)              processor)   job runner)              │
│                                                                  │
│  Each service has ConfigMap-driven fault injection toggles       │
└──────────────────────┬──────────────────────────────────────────┘
                       │ stdout/stderr logs
                       ▼
┌─────────────────────────────────────────────────────────────────┐
│                 Fluent Bit DaemonSet (one per node)              │
│     tails /var/log/containers/ + attaches K8s metadata          │
└──────────────────────┬──────────────────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────────────────┐
│                    Apache Kafka (KRaft mode)                     │
│                                                                  │
│   raw-logs ──► parsed-logs ──► alerts ──► correlated-alerts     │
└──────────────────────┬──────────────────────────────────────────┘
                       │
          ┌────────────┼─────────────┐
          ▼            ▼             ▼
    log-parser    feature-eng    correlation
    (parse +      (60s sliding   (taxonomy-
     enrich)       windows +      driven
                   7 features)    classifier)
                       │
                       ▼
                  ml-service
                  (Isolation
                   Forest +
                   FastAPI)
                       │
                       ▼
                  dashboard
                  (rich CLI,
                   live view)
```

### Namespace isolation

```
monitoring-system  ──can read──►  target-system
target-system      ──BLOCKED──►   monitoring-system
```

Enforced by Calico NetworkPolicy. The monitoring stack can observe the target system but not vice versa.

---

## Key Design Decisions

### Why Kafka?

Kafka is architecturally justified here — not decorative. Multiple pods across multiple nodes produce logs simultaneously. Kafka buffers, orders, and fans out that stream reliably. More importantly, Kafka's log retention enables **replay-based retraining**: the `/retrain` endpoint on the ML service seeks back 2 hours in `parsed-logs` and retrains the Isolation Forest without any external data store. This is a production pattern that a database-backed system cannot replicate.

### Why a DaemonSet for Fluent Bit?

Kubernetes writes container logs to `/var/log/containers/` on each node. A Deployment would collect logs from one node only. A DaemonSet guarantees one collector per node, so no pod's logs are missed regardless of scheduling.

### Why Isolation Forest?

The system has no labeled anomaly data. Isolation Forest is self-supervised — it learns what "normal" looks like during a burn-in phase and flags deviations. `contamination=0.03` means it expects at most 3% of training windows to be slightly anomalous.

### Why a separate correlation layer?

The ML model produces raw anomaly scores — it doesn't know *what kind* of anomaly it found. The correlation layer adds two things the ML model can't: time-window correlation (sustained anomalies are more significant than single-window spikes) and cross-service correlation (backend + frontend both anomalous = cascading failure, not two independent events). This separation of concerns reduces false positives and produces actionable, named alerts.

### Two-phase operation (burn-in vs inference)

The feature engineer starts in burn-in mode, writing feature vectors to a CSV for 2-3 hours of normal operation. This CSV trains the Isolation Forest offline. Once the model is baked into the ML service image, the feature engineer switches to inference mode and calls the ML service for every window. This makes the training data pipeline explicit and auditable.

---

## Fault Injection

Each target-system service reads a fault mode from a ConfigMap-mounted file — meaning faults can be toggled with a single `kubectl apply` and take effect within 60 seconds without pod restarts.

| Fault | Service | What it simulates | Detection signal |
|---|---|---|---|
| `crash` | any | Random process exit → CrashLoopBackOff | Log drought + restart delta |
| `cpu_spike` | backend, worker | Busy loop → runaway CPU | cpu_millicores spike |
| `memory_leak` | worker | Unbounded list growth | memory_mi gradual increase |
| `error_burst` | frontend, backend | Exception flood | error_rate spike |
| `log_spam` | frontend | Stderr flood | log_volume spike |

### Triggering a fault

```bash
kubectl edit configmap fault-config-worker -n target-system
# change FAULT_MODE from "none" to "cpu_spike"
# save — takes effect within ~60 seconds
```

### Reverting

```bash
kubectl edit configmap fault-config-worker -n target-system
# change FAULT_MODE back to "none"
```

---

## Failure Taxonomy

The correlation layer classifies alerts using a declarative YAML taxonomy — configurable without code changes or redeployment.

```yaml
failure_classes:
  - name: crashloop_overload     # cpu_spike + high_severity
  - name: memory_leak            # memory_growth + high_severity
  - name: error_cascade          # error_spike + high_severity
  - name: log_flood              # log_spike + high_severity
  - name: resource_pressure      # cpu_spike + memory_growth
  - name: pod_crash_restart      # log_drought + high_severity
  - name: crash_recovery         # log_spike_recovery + high_severity
  - name: crashloop              # restart_spike + high_severity
  - name: cascading_failure      # 2+ services anomalous simultaneously (medium+)
```

---

## ML Service

**Endpoint:** `POST /predict`

Accepts a feature vector, returns an anomaly score and severity classification.

```bash
curl -X POST http://ml-service:8000/predict \
  -H "Content-Type: application/json" \
  -d '{
    "pod": "target-system-worker-xxx",
    "namespace": "target-system",
    "service": "worker",
    "window_end": "2026-04-18T10:00:00Z",
    "log_volume": 65,
    "cpu_millicores": 1.5,
    "memory_mi": 32.0,
    "log_volume_delta": 5.0
  }'
```

```json
{
  "pod": "target-system-worker-xxx",
  "service": "worker",
  "anomaly": false,
  "score": 0.1565,
  "severity": "normal",
  "features": {...}
}
```

**Endpoint:** `POST /retrain`

Triggers a background retraining task that replays the last 2 hours from the `parsed-logs` Kafka topic and hot-swaps the model without downtime.

---

## Feature Engineering

Seven features computed per pod per 60-second sliding window:

| Feature | Description | Catches |
|---|---|---|
| `error_rate` | errors / total log lines | error bursts |
| `log_volume` | total log lines in window | log spam, crashes |
| `unique_error_types` | distinct error messages | error diversity |
| `cpu_millicores` | CPU usage from metrics-server | CPU spikes |
| `memory_mi` | Memory usage from metrics-server | memory leaks |
| `error_rate_delta` | change in error_rate vs prev window | sudden error onset |
| `log_volume_delta` | change in log_volume vs prev window | sudden log changes |

---

## Dashboard

Live CLI dashboard powered by Python `rich`:

```
╭──────────────────────────────────────────────────────────────────╮
│ LogHelmsman — K8s Anomaly Detection Pipeline  Last update: ...   │
╰──────────────────────────────────────────────────────────────────╯
                        Anomalous Pods
╭──────────────┬─────────┬────────┬──────────┬────────┬──────────╮
│ Pod          │ Service │  Score │ Severity │ CPU(m) │ Mem(Mi)  │
├──────────────┼─────────┼────────┼──────────┼────────┼──────────┤
│ worker-xxx   │ worker  │-0.2172 │   HIGH   │ 309.46 │    47.34 │
╰──────────────┴─────────┴────────┴──────────┴────────┴──────────╯
                       Correlated Alerts
╭──────────┬──────────────────────┬──────────┬─────────────────╮
│ Time     │ Failure Class        │ Severity │ Service         │
├──────────┼──────────────────────┼──────────┼─────────────────┤
│ 09:35:50 │ 💥 crashloop_overload│ CRITICAL │ worker          │
╰──────────┴──────────────────────┴──────────┴─────────────────╯
```

Attach to view:
```bash
kubectl attach -it -n monitoring-system deployment/dashboard
```

---

## Kafka Topics

| Topic | Producer | Consumer | Purpose |
|---|---|---|---|
| `raw-logs` | Fluent Bit | log-parser | Raw enriched container logs |
| `parsed-logs` | log-parser | feature-engineer | Structured parsed log records |
| `alerts` | ml-service | correlation-layer, dashboard | Raw ML anomaly alerts |
| `correlated-alerts` | correlation-layer | dashboard | Named, classified failure alerts |

---

## HPA — Autoscaling

The log-parser scales automatically under high log volume:

```yaml
minReplicas: 1
maxReplicas: 4
targetCPUUtilizationPercentage: 50
```

Trigger load to observe scaling:
```bash
# flood logs from all services
kubectl edit configmap fault-config-frontend -n target-system  # log_spam
kubectl edit configmap fault-config-backend -n target-system   # log_spam
kubectl edit configmap fault-config-worker -n target-system    # log_spam

# watch pods scale
kubectl get hpa -n monitoring-system -w
kubectl get pods -n monitoring-system -w
```

---

## Local Setup

### Prerequisites

- Docker Desktop
- Minikube v1.38+
- kubectl
- Helm
- Python 3.11+

### Start the cluster

```bash
minikube start --nodes 3 --cpus 2 --memory 4096 --cni calico --driver=docker
minikube addons enable metrics-server
```

### Create namespaces and apply network policy

```bash
kubectl apply -f namespaces/
```

### Deploy target system

```bash
# build images
docker build -t target-frontend:v1 target-system/frontend/
docker build -t target-backend:v1 target-system/backend/
docker build -t target-worker:v1 target-system/worker/
docker build -t target-loadgen:v1 target-system/loadgen/

# load into minikube
minikube image load target-frontend:v1
minikube image load target-backend:v1
minikube image load target-worker:v1
minikube image load target-loadgen:v1

# deploy
kubectl apply -f target-system/manifests/
```

### Deploy monitoring stack

```bash
# deploy kafka
kubectl apply -f monitoring-system/manifests/kafka.yaml

# wait for kafka, then create topics
kubectl exec -it -n monitoring-system deployment/kafka -- \
  /opt/kafka/bin/kafka-topics.sh --create --topic raw-logs --bootstrap-server localhost:9092
kubectl exec -it -n monitoring-system deployment/kafka -- \
  /opt/kafka/bin/kafka-topics.sh --create --topic parsed-logs --bootstrap-server localhost:9092
kubectl exec -it -n monitoring-system deployment/kafka -- \
  /opt/kafka/bin/kafka-topics.sh --create --topic alerts --bootstrap-server localhost:9092
kubectl exec -it -n monitoring-system deployment/kafka -- \
  /opt/kafka/bin/kafka-topics.sh --create --topic correlated-alerts --bootstrap-server localhost:9092

# deploy fluent bit
kubectl apply -f monitoring-system/manifests/fluent-bit.yaml

# build and deploy pipeline services
docker build -t log-parser:v1 monitoring-system/log-parser/
docker build -t feature-engineer:v1 monitoring-system/feature-engineer/
docker build -t correlation-layer:v1 monitoring-system/correlation-layer/
docker build -t dashboard:v1 monitoring-system/dashboard/

minikube image load log-parser:v1
minikube image load feature-engineer:v1
minikube image load correlation-layer:v1
minikube image load dashboard:v1

kubectl apply -f monitoring-system/manifests/
```

### Burn-in (collect training data)

```bash
# feature engineer starts in BURN_IN_MODE=true
# let it run for 2-3 hours, then copy the CSV
kubectl cp monitoring-system/<feature-engineer-pod>:/data/burn_in.csv ./burn_in.csv
```

### Train the model

```bash
pip install scikit-learn pandas joblib
python monitoring-system/ml-service/train.py
```

### Deploy ML service

```bash
docker build -t ml-service:v1 monitoring-system/ml-service/
minikube image load ml-service:v1
kubectl apply -f monitoring-system/manifests/ml-service.yaml

# switch feature engineer to inference mode
kubectl set env deployment/feature-engineer -n monitoring-system BURN_IN_MODE=false
```

### View dashboard

```bash
kubectl attach -it -n monitoring-system deployment/dashboard
```

---

## Observed Detection Results

| Fault | Detection Latency | Failure Class | Notes |
|---|---|---|---|
| cpu_spike | ~2 windows (~2 min) | `crashloop_overload` | Both replicas caught simultaneously |
| memory_leak | ~3-5 windows (~5 min) | `memory_leak` | Gradual memory growth detected |
| crash | ~2 windows (~2 min) | `pod_crash_restart` | Log drought pattern detected |
| error_burst | ~1-2 windows | `error_cascade` | High error_rate spike |
| multi-service | ~2-3 windows | `cascading_failure` | Cross-service correlation |

---

## Tech Stack

| Component | Technology |
|---|---|
| Container orchestration | Kubernetes 1.35 (Minikube) |
| CNI / Network Policy | Calico |
| Message broker | Apache Kafka 3.9 (KRaft) |
| Log collector | Fluent Bit 3.2 (DaemonSet) |
| ML model | Isolation Forest (scikit-learn) |
| ML serving | FastAPI + Uvicorn |
| Feature engineering | Python, kafka-python, kubernetes client |
| Dashboard | Python rich |
| Target services | Flask, Python |
| Autoscaling | Kubernetes HPA |
| Storage | Minikube hostPath PVC (Kafka, feature CSV) |
