This project aims to build a logs monitoring system, that monitors the logs of a target K8s cluster, and figure out "Is something going wrong with my K8s cluster rn?". The monitoring system will be monitoring the following stats:
pod crashes
restart bursts
CPU/memory spikes
error-rate spikes
scheduling failures

The "Y I Did What I Did"s of my project:
- Why not Docker Desktop? While Docker Desktop provides a nice little single node cluster, doesn't provide me with the multi node setup required for demonstrating HPA or DaemonSet (for FluentD).

