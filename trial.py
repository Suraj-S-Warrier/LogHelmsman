import pandas as pd

df = pd.read_csv("burn_in.csv")

# filter out loadgen
df = df[df['service'] != 'loadgen']

print(f"Total rows: {len(df)}")
print(f"\nServices: {df['service'].value_counts().to_dict()}")
print(f"\nFeature stats:")
print(df[['error_rate','log_volume','unique_error_types',
          'cpu_millicores','memory_mi',
          'error_rate_delta','log_volume_delta']].describe())