# Deployment Assets

This directory contains the declarative assets required for the v0.1 vertical slice.
Operational commands and their verified order remain in the repository's root `README.md`.

| Directory | Responsibility | Completion gate |
| --- | --- | --- |
| `host/` | Provider-neutral host preparation and validation | Required host checks pass |
| `cluster/` | Pinned containerd, kubeadm, and Cilium bootstrap | The node and all core system pods are healthy |
| `gpu/` | Pinned GPU Operator install, read-only validation, CUDA smoke test, and the Operator-owned DCGM configuration | A whole-GPU Pod can consume `nvidia.com/gpu` and run `nvidia-smi`; DCGM remains deferred until observability |
| `runtime/` | Stable runtime dispatcher, committed profiles, and the vLLM adapter | Health, model discovery, non-streaming generation, and incremental SSE pass |
| `observability/` | Prometheus/Grafana/Alertmanager, ownership-safe DCGM enablement and discovery, OpenCost, stable recording rules, dashboards, and alerts | Runtime, GPU, platform, energy, and cost metrics are queryable |
| `fleet/` | Separate immutable artifact lock for the proposed three-node llm-d pilot | Compatibility and live GPU/network gates remain unvalidated |

Benchmark orchestration lives under `bench/` and `benchmarks/` because it is a platform
capability rather than another long-running serving layer. Its installation owns the
`kip-benchmark` namespace, scoped ServiceAccount, contract ConfigMap, and 20-GiB local PV/PVC.
Individual AIPerf cells create a receipt ConfigMap and ordinary CPU-only Job behind the
benchmark executor abstraction; successful run resources are removed after collection.

`deploy/destroy.sh` is the guarded, target-local inverse of the complete v0.1 stack. It removes
only recognized KIP-managed benchmark, observability/cost, runtime, GPU, and cluster state,
including generated benchmark work data and model cache. Reviewed pricing profiles and
sanitized benchmark reports remain available after teardown so results are reproducible.
