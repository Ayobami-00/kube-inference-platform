# Kube Inference Platform architecture

v0.1 status: **Host-to-inference validated on an NVIDIA A10 and again on a Vast RTX 4090;
live observability and a priced, reconciled short benchmark report are validated on the RTX 4090.
The five-trial baseline experiment remains pending.**

The v0.1 milestone proves a measurable and reproducible serving path on one SSH-accessible
bare-metal NVIDIA GPU server. The platform composes Kubernetes, NVIDIA's Kubernetes
integration, vLLM, Prometheus/Grafana, OpenCost, and AIPerf rather than replacing any of their
schedulers or accounting models.

## Eventual platform (planned)

The long-term goal is a portable, Kubernetes-native inference platform that turns one or more
supported, SSH-accessible bare-metal GPU servers into a model-serving cluster. A user should be
able to declare a model, runtime, GPU and parallelism needs, replica range, and latency target;
the platform should handle deployment, placement, request routing, scaling, health, measurement,
and cost. The same interface should work from one GPU through multi-node distributed replicas,
without tying the control plane to a particular server provider.

```mermaid
flowchart TB
    subgraph Interfaces[User and workload interfaces]
        Spec["inferctl / declarative API / ModelDeployment CRD"]
        Client[OpenAI-compatible API clients]
        Suite[Reviewed AIPerf benchmark suites]
    end

    subgraph Control[Future KIP control plane]
        Controller[Focused ModelDeployment controller]
        Planner[Topology-aware placement and capacity policy]
        Scaler[Future SLO-aware autoscaler]
        Cache[Model cache policy]
    end

    subgraph Serving[Inference request path]
        Gateway[Envoy AI Gateway: trusted identity, admission, streaming]
        Router[llm-d InferencePool and endpoint picker: load, queue, KV affinity]
        Replicas[vLLM or SGLang replicas: combined or prefill/decode]
    end

    subgraph Foundation[Portable Kubernetes and GPU foundation]
        Nodes[One or more bare-metal GPU nodes]
        Bootstrap[Kubernetes, Cilium, GPU and optional Network Operators]
        Topology[GPU, NVLink, NUMA, NIC, and RDMA topology]
        GPUs[GPU capacity across nodes]
    end

    subgraph Evidence[Observability, cost, and experiments]
        Telemetry[Runtime, Kubernetes, and DCGM telemetry]
        Metrics[Prometheus and future OpenTelemetry]
        Dashboards[Grafana dashboards and alerts]
        Costs[OpenCost allocation and inference cost]
        Reports[Sanitized benchmark reports]
    end

    Spec --> Controller
    Controller --> Planner
    Controller --> Scaler
    Controller --> Cache
    Planner --> Replicas
    Scaler --> Replicas
    Cache --> Replicas

    Client --> Gateway
    Suite --> Gateway
    Gateway --> Router --> Replicas --> GPUs
    Nodes --> Bootstrap --> GPUs
    Topology --> Planner
    Nodes --> Topology

    Replicas --> Telemetry
    GPUs --> Telemetry
    Telemetry --> Metrics
    Metrics --> Dashboards
    Metrics --> Costs
    Suite --> Reports
    Metrics --> Reports
    Costs --> Reports
    Reports -.->|measured evidence for future policies| Planner
```

The future control plane decides *what* should run and *where*: a focused
`ModelDeployment` controller can translate reviewed profiles into Kubernetes workloads and
placement policy. For the first multi-tenant pilot, KIP remains the sole owner of vLLM
Deployments, while Envoy AI Gateway owns the proxy/request path and llm-d owns the
`InferencePool` endpoint picker. This avoids implementing a second Go reverse proxy or two
controllers for the same Deployment. The gateway authenticates and admits a request; llm-d
chooses a replica. vLLM retains batching and token scheduling *inside* that replica.

Horizontal replicas and distributed execution solve different problems. The platform should
route among independent replicas for capacity and availability, while supporting tensor,
pipeline, and eventually disaggregated prefill/decode work within a replica when one model
needs multiple GPUs or nodes. Kubernetes-native workload primitives should manage distributed
replica lifecycles; the platform supplies topology and runtime configuration.

The measurement loop is a core platform feature: AIPerf exercises the same serving path as
clients, while Prometheus, DCGM, OpenCost, and future tracing provide the latency, throughput,
GPU, energy, and cost evidence used to compare routing, placement, and scaling policies. The
diagram is a destination, not a claim that its Go control plane, gateway, autoscaler, SGLang
adapter, or multi-node execution exists in v0.1.

## Three-node llm-d pilot (partially validated)

The separate pilot validated a private request path on one non-HA control plane and
two cross-region RTX 4090 workers; the three task-owned VMs have since been
released. It is not yet an accepted SLO or failure-recovery campaign. In that
deployment KIP owned both pinned, same-revision vLLM Deployments; llm-d owned
the `InferencePool` and endpoint picker; Envoy AI Gateway owned its private
Gateway and route.

```mermaid
flowchart LR
    Clients[Seeded AIPerf tenants A/B/C] --> Gateway[Private Envoy AI Gateway]
    Gateway -.->|authorize and inject trusted identity| Auth[Fail-closed tenant/class authorization]
    Gateway --> EPP[llm-d endpoint picker and flow control]
    EPP --> Pool[InferencePool]
    Pool --> W1[vLLM Qwen replica / GPU worker 1]
    Pool --> W2[vLLM Qwen replica / GPU worker 2]
    W1 --> Metrics[Prometheus / DCGM / OpenCost]
    W2 --> Metrics
    Gateway --> Metrics
    EPP --> Metrics
    Metrics --> Grafana[Private Grafana]
    Metrics --> Reports[Sanitized local pilot reports]
    Ledger[Verified Vast node-rate ledger] --> Reports
```

WireGuard supplies the private node and Kubernetes API network. The first mixed text
smoke reached both workers and reconciled AIPerf, Gateway, and endpoint-picker events;
it had too few samples for SLO acceptance. A subsequent intentionally bursty trial
exposed 429 load-shedding and unacceptable chat/agentic TTFT. The batch arrival
generator was corrected to honor its reviewed offered rate. A later, genuinely
restarted weighted-random EPP completed a 367-request, zero-error text trial with
near-even worker selection, but chat and agentic TTFT still missed proposed SLOs.
A subsequent 61-request smoke covered all nine tenant/class combinations and
reconciled its verified node-rate audit with OpenCost. It is not an adequately
sampled SLO campaign. A controlled KIP-owned model-Pod loss then completed
11/11 streamed turns on the surviving GPU worker; after the replacement was
Ready, a second 11/11 trace reached both workers. These are small recovery
checkpoints, not evidence of in-flight continuity or sustained fault tolerance.
The initial cross-region placement of Gateway/EPP on the Japanese worker added
measurable pre-dispatch latency for a Romanian control-node client. Both CPU
request-path Pods are now pinned to the control node with exact control-plane
taint tolerations; the two model Pods remain on separate GPU workers. On the
same seeded isolated chat trace, client P95 TTFT improved from 1.84 to 0.88 seconds,
but one passing isolated trial is not nominal mixed-load acceptance.
At the earlier mixed offered rates, the colocated path still missed chat and
agentic-style TTFT targets and rejected four of 100 batch requests at llm-d's
60-second queue TTL. Actual flow-control queue depth reached 19; the report
counts the 363 completions and four rejections separately and marks the run
partial. AIPerf's zero-duration error placeholders are not treated as measured
latency: rejection terminal times are reconstructed from matching Envoy
start/duration evidence and explicitly disclosed. Lower-load calibration is
required; colocation alone does not establish capacity or fairness.
A later lower-offered-rate mix completed 345/345 streamed turns without queue
rejections, and its infrastructure cost reconciled to OpenCost within 0.1%.
Nevertheless chat P95 TTFT was 3.75 seconds and agentic-style P95 TTFT was
2.01 seconds, both above their proposed targets. The actual flow-control queue
still peaked at 13. That single calibration trial is also partial. Its sanitized
report was copied locally before the exact task-owned VMs were released; VM-local
raw traces and model caches were not retained.
Nominal nine-cohort trials, voice, and clean rebuild remain acceptance work.

## Bootstrap dependency chain

`make bootstrap` preserves the hard checkpoints between layers:

```mermaid
flowchart LR
    Host[Validated Ubuntu host] --> Runtime[containerd with systemd cgroups]
    Runtime --> ControlPlane[kubeadm control plane]
    ControlPlane --> CNI[Cilium CNI]
    CNI --> Ready[Ready and schedulable node]
    Ready --> GPUOperator[NVIDIA GPU Operator]
    GPUOperator --> GPUReady[Whole nvidia.com/gpu resource]
    GPUReady --> GPUSmoke[Temporary nvidia-smi Pod]
    GPUSmoke --> Dispatcher[Runtime dispatcher and committed profile]
    Dispatcher --> VLLM[vLLM Service]
    VLLM --> InferenceSmoke[OpenAI and streaming checkpoint]
    InferenceSmoke --> Observe[Prometheus, Grafana, DCGM, OpenCost]
    Observe --> AIPerf[AIPerf Kubernetes Job capability]
```

The composed command installs the AIPerf capability but does not start the exhaustive
baseline. A missing cost profile also does not prevent platform installation; it leaves the
stack explicitly unpriced and prevents benchmark publication.

Before cluster bootstrap, `make host-setup` validates the supported Ubuntu/x86-64 contract and
installs only inspection tools. Cluster bootstrap then installs pinned containerd/Kubernetes
components and creates one control-plane/worker node. kube-proxy supplies Service routing,
while Cilium provides IPv4 Pod networking through Kubernetes PodCIDR IPAM and VXLAN. Hubble,
kube-proxy replacement, multi-node routing, and high availability remain later changes.

## Serving and benchmark paths

```mermaid
flowchart LR
    Suite[Reviewed benchmark suite] --> Executor[Benchmark executor]
    Executor --> Workers[CPU-only AIPerf Jobs]
    Workers -->|OpenAI-compatible HTTP and SSE| Service[Internal Kubernetes Service]
    Service --> Combined[vLLM combined worker]
    Combined --> GPU[NVIDIA GPU]

    Workers --> Archive[Node-local compressed summary archives]
    Archive --> Work[Ignored normalization workspace]
    Work --> Builder[Report builder]
    Prometheus --> Builder
    OpenCost --> Builder
    Builder --> Report[Sanitized HTML, JSON, CSV, SVG]
```

The stable `runtime-*` commands resolve a committed runtime adapter and model profile. The
profile owns Kubernetes-generic identity, model, storage, and resources; the vLLM adapter owns
engine arguments, rendering, health, and OpenAI-compatible smoke tests. This avoids inventing a
premature common runtime implementation while keeping the public interface ready for a second
adapter.

The baseline profile deploys one BF16 Qwen2.5-7B-Instruct replica on one whole GPU and exposes
only a ClusterIP Service. A node-local hostPath cache survives Pod restarts; `Recreate` prevents
two full-GPU replicas from competing during updates. Health, exact model discovery,
non-streaming chat/legacy completion, and demonstrably incremental SSE are hard gates before
benchmarking.

AIPerf is the only traffic generator. KIP validates reviewed suites, creates ownership-safe
Kubernetes Jobs through an executor boundary, captures exact UTC run windows, normalizes
upstream results, fetches correlated telemetry and cost, and creates the report. It does not
duplicate AIPerf's async client or request-latency calculations. AIPerf 0.12 does not publish a
Kubernetes operator/CRDs, so v0.1 uses ordinary Jobs; a future operator can become another
executor without changing suite or report contracts.

## Stable telemetry contract

```mermaid
flowchart TB
    subgraph Workloads[Current and future inference topology]
        Router[router]
        Prefill[prefill workers]
        Decode[decode workers]
        Combined[combined vLLM worker]
    end

    Workloads -->|runtime metrics| Adapter[Version-specific recording rules]
    DCGM[GPU Operator DCGM Exporter<br/>pinned collector contract] -->|GPU metrics| Adapter
    Kube[Kubernetes, node, and Cilium exporters] --> Adapter
    Adapter -->|stable kip metrics| Prometheus[Prometheus]
    Prometheus --> Grafana[Provisioned Grafana dashboards]
    Prometheus --> Rules[Alerts and SLO templates]
    Prometheus --> OpenCost[OpenCost allocation and inference cost]
```

Runtime Pods identify `runtime`, `deployment`, `model_profile`, `served_model`,
`model_revision`, `worker_role`, replica, tensor-rank, and pipeline-rank. The Service is only
the discovery/routing boundary; Prometheus copies per-worker identity from the backing Pod so a
shared Service cannot falsely stamp every worker with rank zero. Prometheus enrichment also adds
cluster, namespace, Pod, node, and GPU UUID. The current Pod is `worker_role=combined`; future
`router`, `prefill`, and `decode` workloads supply their true per-Pod role and rank labels.

Dashboards query stable `kip:` recording rules rather than raw vLLM/DCGM metric names. The
adapter layer calculates request/error and token rates; server TTFT, TPOT, end-to-end, queue,
prefill, and decode percentiles; running/waiting requests; KV/prefix-cache state;
preemptions; readiness; GPU utilization/memory/power/thermal/clock/PCIe/XID state; and node
health. Dashboard variables filter these dimensions instead of hardcoding the v0.1 model or
Deployment. DCGM PCIe profiling is optional on GPUs that do not expose its profiling counters;
reports disclose missing series and render `N/A`, never a synthetic zero.

Collection has three deliberate resolutions: 5-second Prometheus samples for vLLM/DCGM,
15–30-second platform samples, and 333-millisecond AIPerf-local telemetry during an active
experiment. The fastest stream remains benchmark-local so long-term Prometheus storage does
not become a raw trace system.

Benchmark publication clips model-scoped stable runtime gauges and raw five-second DCGM GPU
gauges joined to the runtime Pod identity to each exact trial window for
queue, KV-cache, and GPU utilization, memory, power, temperature, PCIe,
clocks, throttle reasons, and XID state. It computes prompt/generated/cached token rates and
server TTFT/TPOT/end-to-end percentiles from the pinned vLLM counter and histogram increases
over that trial alone. The five-minute recording rules remain useful for dashboards but cannot
contaminate a benchmark cell with warmup, cooldown, or adjacent-cell traffic. AIPerf
summary-only output supplies reconciled success/error
counts plus client-observed TTFT and inter-token-latency (ITL) coverage. AIPerf ITL is kept
distinct from vLLM's server TPOT, which comes from
`vllm:request_time_per_output_token_seconds_bucket`. This is reported as streaming timing
evidence, not an SSE protocol proof. The runtime smoke test remains responsible for
incremental arrival and terminal `[DONE]` validation.

## Cost model

The pricing source is a reviewed profile under `costs/profiles/`, not cloud credentials. A
v0.1 profile declares a positive USD whole-node hourly price, provider/SKU/region, source,
effective date, physical GPU count, and that power is included. Its canonical hash is carried
into the report.

For a dedicated homogeneous node:

```text
OpenCost GPU hourly rate = whole-node hourly rate / physical GPU count
CPU, RAM, storage, network, and load-balancer rates = 0
```

This avoids charging the same rental twice. The **allocated** view shares node-local idle cost
and is the bill-reconciling primary measure. The **usage** view excludes idle allocation and
shows active efficiency. DCGM power integration reports Wh/request and kWh/million tokens but
does not add an electricity charge already included in the rental.

For each benchmark, `hourly rate × experiment elapsed time` is the audit total. OpenCost
attributes runtime, idle, and future shared infrastructure by model/workload labels. A report
is valid only when allocated cost reconciles within the greater of 5% or one minute of node
cost and when inference token attribution is present. These numbers are transparent estimates,
not provider invoices.

## Persistence and trust boundaries

Prometheus, Grafana, Alertmanager, and AIPerf results use project-owned node-local storage.
OpenCost remains an internal API over Prometheus data. All platform services are ClusterIP and
normal administration occurs through SSH plus loopback-only `kubectl port-forward` sessions.

Benchmark Jobs run in `kip-benchmark` through a scoped ServiceAccount and request no GPU. The
20-GiB PV maps the fixed node path `/var/lib/kube-inference-platform/benchmark-results`.
AIPerf exports aggregate summaries only, and each successful cell is retained there as a
compressed archive before being copied into ignored `benchmarks/.work/` for normalization.
Failure diagnostics also remain ignored. Only sanitized HTML, SVG, JSON, CSV, and a
reproducibility manifest under `benchmarks/reports/` are eligible for version control.

The repository owns deployment configuration, stable metric adapters, reviewed suites,
pricing provenance, orchestration, and reports. Kubernetes owns workload scheduling, vLLM
owns continuous batching, NVIDIA components expose GPU capacity/telemetry, AIPerf owns load
generation and request metrics, Prometheus owns time-series collection, and OpenCost owns
allocation/inference-cost attribution.

## GPU boundary and deferred components

GPU Operator uses the classic ClusterPolicy/Device Plugin path. CDI supplies whole devices to
ordinary Pods without a runtime class. Operator-managed drivers are the default; host drivers
are accepted only through explicit preinstalled mode. Capacity must match physical display
PCI devices and a temporary digest-pinned CUDA Pod must execute `nvidia-smi` before runtime
deployment.

MIG, time sharing, DRA, GPUDirect, heterogeneous node pricing, component-level price models,
automatic provider billing ingestion, currency conversion, carbon accounting, Loki, Tempo,
OpenTelemetry tracing, Hubble, remote metrics storage, high-availability Prometheus, public
report hosting, Grafana image rendering, benchmark scheduling, accuracy evaluation, arbitrary
runtime overrides, the Go operator/gateway, authentication, autoscaling, and multi-node
distributed inference remain outside v0.1.
