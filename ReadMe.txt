This project aims to build a logs monitoring system, that monitors the logs of a target K8s cluster, and figure out "Is something going wrong with my K8s cluster rn?". The monitoring system will be monitoring the following stats:
pod crashes
restart bursts
CPU/memory spikes
error-rate spikes
scheduling failures

What have Been Implemented in the project:
- 2 separate namespaces: target-system, monitoring-system
- A network policy which enables monitoring-system to communicate to target-system but not vice-versa (and also allows intra namespace communication)
- Target-system:
  - contains 4 deployments: A loadgen, a frontend which gets polled by the loadgen, a backend which processes the requests from frontend, and a worker which does the tasks for the requests.
  - This is meant to simulate a simple real world application, with 4 microservices.
  - The deployments are configured to read configMaps which are mounted as volume, so that i can toggle the fault modes in the microservices. 
  - Various fault mode options are provided for each microservice, all of which affects other microservices too.
-Monitoring-system:
  - Kakfa deployment with persistent volume. Has 3 topics created - raw-logs, parsed-logs,and alerts
  - A Fluent Bit DaemonSet to the pods' metadata and logs to the raw-logs topic of kafka.
  - A log-parser pod and a feature engineer pod to refine the raw-logs and convert it into the required format for our csv file.
  - An Isolation Forest model (trained on the normal behaviour, detects any anomalous behaviour). Its by design a little too sensitive to any anomalous behaviour, bringing in false positives but ensuring that no anomalies go missing. The false positives are handled for in the correlation layer. 
  - Implemented correlation layer and iteratively tuned the correlation thresholds based on observed false positive patterns during testing.



The "Y I Did What I Did"s of my project:
- Why not Docker Desktop? While Docker Desktop provides a nice little single node cluster, doesn't provide me with the multi node setup required for demonstrating HPA or DaemonSet (for FluentD).
-ConfigMaps are mounted as volume and not EnvFrom? Because if i do the latter, I cannot simply change the ConfigMap values to create faults and irregular activity, and expect it to get reflected in the pods, since the pods load the env variables statically during startup and i need to restart the pods for the fault mode to get reflected. Instead if i mount a volume, then the pods check the volume everytime for finding the value of the variable.
-why fluentbit in daemonset and not deployment? Since i need one fluentbit pod per node since every container writes its logs into its corresponding node, and i need to retrieve "those" logs.



Starting the project in minikube with the command:
minikube start --nodes 3 --cpus 2 --memory 4096 --cni calico   #calico required since we need cni compatible software for NetworkPolicy to work.

applied memory_leak to worker at 15:35
got the worker pods in anomalous in 15:36
got memory_leak in 15:37,15:38
