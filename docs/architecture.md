# v0.1 Architecture

Status: **Host, single-node cluster, and GPU path validated on target hardware; vLLM runtime tooling implemented, with target inference validation pending**

The v0.1 milestone proves the smallest complete serving path on one SSH-accessible bare-metal NVIDIA GPU
server. The platform composes Kubernetes, NVIDIA's Kubernetes integration, and vLLM rather than
replacing them.

Before cluster bootstrap, `make host-setup` validates the supported Ubuntu/x86-64 contract and
installs only the tools required to inspect the machine. It does not install Kubernetes or
NVIDIA software. The same target-local entry point will later be reused by multi-node remote
orchestration.

The cluster bootstrap installs a pinned containerd runtime and creates one kubeadm node that
serves as both control plane and worker. kube-proxy provides the initial Kubernetes Service
implementation, while Cilium provides IPv4 pod networking through Kubernetes PodCIDR IPAM and
a VXLAN overlay. This conservative baseline keeps kube-proxy replacement, Hubble, multi-node
routing, and high availability as later measured changes rather than initial variables.

Bootstrap treats the following as one ordered dependency chain:

```mermaid
flowchart LR
    Host[Validated Ubuntu host] --> Runtime[containerd with systemd cgroups]
    Runtime --> ControlPlane[kubeadm control plane]
    ControlPlane --> CNI[Cilium CNI]
    CNI --> Ready[Ready and schedulable node]
    Ready --> GPUOperator[NVIDIA GPU Operator]
    GPUOperator --> GPUReady[Whole nvidia.com/gpu resource]
    GPUReady --> Smoke[Temporary nvidia-smi Pod]
    Smoke --> Dispatcher[runtime dispatcher and committed profile]
    Dispatcher --> VLLMRuntime[vLLM adapter - target validation pending]
```

```mermaid
flowchart LR
    Bench[Async benchmark client] -->|OpenAI-compatible HTTP/SSE| Service[Kubernetes Service]
    Service --> VLLM[vLLM Pod]
    VLLM --> GPU[NVIDIA GPU]

    VLLM -->|Runtime metrics| Prometheus[Prometheus]
    GPU --> DCGM[DCGM Exporter]
    DCGM -->|GPU metrics| Prometheus
    Prometheus --> Grafana[Grafana]
```

## Request path

The benchmark client sends OpenAI-compatible requests to a Kubernetes Service. The Service
selects the single vLLM replica, which performs continuous batching and executes inference on
the GPU. Streaming Server-Sent Events return generated content incrementally to the client.

The stable `runtime-*` commands resolve a committed runtime adapter and model profile. The
profile owns Kubernetes-generic identity, model, storage, and resource settings; the vLLM
adapter owns engine arguments, rendering, health checks, and OpenAI-compatible smoke tests.
This boundary keeps the public workflow stable without inventing shared abstractions before a
second runtime provides concrete reuse requirements.

The v0.1 profile deploys one BF16 Qwen2.5-7B-Instruct replica, requests one whole GPU, and
exposes only an internal ClusterIP Service. A node-local hostPath cache survives Pod restarts,
while the `Recreate` strategy prevents two full-GPU replicas from competing during an update.
Runtime completion is a hard checkpoint: health, exact model discovery, non-streaming chat and
legacy completion, and demonstrably incremental SSE must all pass before benchmarking begins.

## Telemetry path

Prometheus collects application-level metrics from vLLM and hardware-level metrics from DCGM
Exporter. Grafana visualises both perspectives. The benchmark client independently measures
time to first token, time per output token, end-to-end latency, and throughput.

## Platform boundary

The repository owns deployment configuration, experiment tooling, measurements, and the later
orchestration layer. Kubernetes owns workload scheduling, vLLM owns within-replica inference
scheduling and continuous batching, and NVIDIA's operators expose and monitor GPU hardware.

## GPU enablement boundary

The v0.1 GPU layer uses GPU Operator's classic `ClusterPolicy` and Kubernetes Device Plugin
path. Operator-managed drivers are the default; a host driver is accepted only through the
explicit preinstalled mode. CDI supplies devices to normal Pods without a runtime class.

The layer intentionally exposes whole physical GPUs only. A hard checkpoint compares
`nvidia.com/gpu` capacity and allocatable resources with NVIDIA display-class PCI devices,
then schedules a digest-pinned CUDA Pod requesting exactly one GPU. vLLM remains blocked until
that Pod runs `nvidia-smi` successfully and the cluster remains healthy afterward. DCGM
Exporter belongs to the observability slice, while MIG, time-slicing, DRA, GPUDirect, and
multi-node topology are deferred measured extensions.

## Deferred components

Additional runtime adapters, arbitrary runtime overrides, the Go operator and gateway,
authentication, rate limiting, multi-replica routing, autoscaling, topology-aware placement,
and multi-node distributed inference are intentionally outside the v0.1 architecture. They
will build on the measured baseline instead of being designed in the absence of a working
serving path.
