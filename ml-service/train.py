import pandas as pd
import numpy as np
from sklearn.ensemble import IsolationForest
from sklearn.preprocessing import StandardScaler
import joblib
import json
import os

# ── load and clean data ───────────────────────────────────────────────────────
df = pd.read_csv("burn_in.csv")
df = df[df['service'] != 'loadgen']

# drop zero-variance features
FEATURES = ['log_volume', 'cpu_millicores', 'memory_mi', 'log_volume_delta']
df = df[FEATURES].dropna()

print(f"Training on {len(df)} rows with features: {FEATURES}")
print(f"\nFeature stats:")
print(df.describe())

# ── scale features ────────────────────────────────────────────────────────────
scaler = StandardScaler()
X = scaler.fit_transform(df)

# ── train isolation forest ────────────────────────────────────────────────────
model = IsolationForest(
    n_estimators=200,       # more trees = more stable scores
    contamination=0.03,     # assume 3% of training data might be slightly anomalous
    max_samples='auto',
    random_state=42,
    verbose=1
)
model.fit(X)

# ── quick sanity check ────────────────────────────────────────────────────────
scores = model.decision_function(X)
predictions = model.predict(X)
anomaly_count = (predictions == -1).sum()

print(f"\nTraining complete:")
print(f"  Anomalies flagged in training data: {anomaly_count} ({anomaly_count/len(df)*100:.1f}%)")
print(f"  Score range: {scores.min():.3f} to {scores.max():.3f}")
print(f"  Score mean: {scores.mean():.3f}")
print(f"  Score std: {scores.std():.3f}")

# ── save model and scaler ─────────────────────────────────────────────────────
os.makedirs("ml-service/model", exist_ok=True)
joblib.dump(model, "ml-service/model/isolation_forest.pkl")
joblib.dump(scaler, "ml-service/model/scaler.pkl")

# save feature list so the service knows what to expect
with open("ml-service/model/features.json", "w") as f:
    json.dump(FEATURES, f)

print(f"\nModel saved to ml-service/model/")
print(f"Features: {FEATURES}")