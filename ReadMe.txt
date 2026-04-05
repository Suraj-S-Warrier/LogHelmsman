This project aims to build a logs monitoring system, that monitors the logs of a target K8s cluster, and figure out "Is something going wrong with my K8s cluster rn?". The monitoring system will be monitoring the following stats:
pod crashes
restart bursts
CPU/memory spikes
error-rate spikes
scheduling failures

What have Been Implemented in the project:
2 separate namespaces: target-system, monitoring-system
A network policy which enables monitoring-system to communicate to target-system but not vice-versa

The "Y I Did What I Did"s of my project:
- Why not Docker Desktop? While Docker Desktop provides a nice little single node cluster, doesn't provide me with the multi node setup required for demonstrating HPA or DaemonSet (for FluentD).
-ConfigMaps are mounted as volume and not EnvFrom? Because if i do the latter, I cannot simply change the ConfigMap values to create faults and irregular activity, and expect it to get reflected in the pods, since the pods load the env variables statically during startup and i need to restart the pods for the fault mode to get reflected. Instead if i mount a volume, then the pods check the volume everytime for finding the value of the variable.



Starting the project in minikube with the command:
minikube start --nodes 3 --cpus 2 --memory 4096 --cni calico   #calico required since we need cni compatible software for NetworkPolicy to work.

