# Kube Inference Platform

Kube Inference Platform is a portable, Kubernetes-native LLM inference platform for
turning bare-metal NVIDIA GPU servers into single- or multi-node inference clusters. The
long-term platform will provide declarative model deployment, topology-aware placement,
distributed execution, routing, autoscaling, SLO management, observability, and cost
optimisation.

> [!IMPORTANT]
> The repository currently contains the v0.1 foundation and portable host-preparation
> tooling. The host tooling is tested with deterministic fixtures but has not yet been
> validated on target hardware. Kubernetes manifests, GPU integration, vLLM serving,
> observability, and real benchmark traffic are not yet implemented or validated.

## v0.1 objective

The v0.1 milestone establishes one complete, production-shaped vertical slice:

```text
bare-metal NVIDIA GPU
        ↓
single-node Kubernetes
        ↓
NVIDIA GPU Operator
        ↓
vLLM model server
        ↓
OpenAI-compatible streaming API
        ↓
concurrent benchmark client
        ↓
latency, throughput, and GPU telemetry
```

The goal is not to build the full control plane. The goal is to prove that a real model can
serve real requests on a real GPU and that the complete request path can be measured.

See [the architecture document](docs/architecture.md) for component boundaries and data
flow.

## Repository layout

```text
.
├── bench/                  Python benchmark CLI
├── deploy/                 Host, cluster, GPU, runtime, and observability assets
├── docs/                   Architecture documentation
├── experiments/baseline/   Reproducible baseline experiment record
├── tests/                  Benchmark tests
├── Makefile                Local development commands
└── pyproject.toml          Python project and tool configuration
```

Operational instructions remain in this README. A separate runbook will only be introduced
if multi-node operations, upgrades, or recovery procedures make this document unwieldy.

## Local development

### Prerequisites

- Python 3.12 for the project development environment (the code supports Python 3.11 or newer)
- [uv](https://docs.astral.sh/uv/)
- Make

Create the development environment:

```bash
make setup
```

Run all local checks:

```bash
make check
```

Inspect the initial benchmark command:

```bash
python -m bench --help
```

The CLI is intentionally limited to a runnable help command at this stage. HTTP/SSE traffic,
concurrency, token accounting, and metrics are added during the benchmark implementation
step.

## Host preparation

The v0.1 host tooling runs directly on a target machine rather than orchestrating it remotely.
From a repository checkout on the host, run:

```bash
make host-setup
```

The setup command installs only missing inspection prerequisites and then validates the host.
It supports root execution or a non-root user with `sudo`, and it is safe to repeat. It does
not install or configure containerd, Kubernetes, NVIDIA software, swap, kernel modules,
sysctls, or networking.

The v0.1 target contract is:

- Ubuntu 22.04 or 24.04
- x86-64
- systemd
- `apt` package management
- Root access or working `sudo`
- A usable non-loopback network interface

Run validation without changing the host:

```bash
make host-check
```

Display the complete output from hardware inspection commands:

```bash
make host-check VERBOSE=1
```

Before GPU Operator installation, unavailable NVIDIA hardware and driver checks are reported
as `DEFERRED`. After GPU Operator installation, require all GPU checks to pass:

```bash
make host-check GPU_REQUIRED=1
```

Both `VERBOSE` and `GPU_REQUIRED` accept only `0` or `1`. Terminal colors are enabled only for
interactive output and can be disabled explicitly with `NO_COLOR=1`. Host details are printed
to the terminal and are never persisted automatically.

The later multi-node milestone will add inventory-driven remote orchestration while reusing
these same target-local scripts.

## v0.1 implementation milestones

- [x] Create the repository foundation and runnable benchmark skeleton.
- [x] Implement portable host preparation and validation tooling.
- [ ] Implement single-node Kubernetes bootstrap and CNI installation.
- [ ] Implement NVIDIA GPU Operator installation and CUDA validation.
- [ ] Provision the target host and validate the host, cluster, and GPU slices.
- [ ] Deploy one vLLM model replica and expose it through a Kubernetes Service.
- [ ] Verify non-streaming and streaming OpenAI-compatible requests.
- [ ] Implement the asynchronous benchmark client and inference metrics.
- [ ] Install Prometheus, Grafana, and DCGM Exporter.
- [ ] Run and document the baseline concurrency experiment.

Deployment assets are added in that order. Each command documented here must be verified on
the real machine before its checklist item is marked complete.

## Security and local state

Never commit SSH keys, API tokens, kubeconfig files, real machine inventories, model-provider
credentials, or Kubernetes Secret values. Use local ignored files and environment variables,
and commit only clearly marked examples with placeholder values.

Raw benchmark traces and model caches remain local. Compact experiment results such as CSV
and JSON summaries should be committed with the configuration that produced them.

## v0.1 Definition of Done

The v0.1 milestone is complete when a real model runs on a real NVIDIA GPU inside bare-metal
Kubernetes, serves streaming OpenAI-compatible requests, and can be concurrently benchmarked
while application and GPU telemetry are collected.

The Go operator, custom gateway, authentication, rate limiting, autoscaling, multi-replica
routing, and multi-node inference are deliberately deferred until this baseline is proven.
