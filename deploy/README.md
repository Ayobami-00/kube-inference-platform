# Deployment Assets

This directory contains the declarative assets required for the v0.1 vertical slice.
Operational commands and their verified order remain in the repository's root `README.md`.

| Directory | Responsibility | Completion gate |
| --- | --- | --- |
| `host/` | Provider-neutral host preparation and validation | Required host checks pass |
| `cluster/` | Pinned containerd, kubeadm, and Cilium bootstrap | The node and all core system pods are healthy |
| `gpu/` | Pinned GPU Operator install, read-only validation, and CUDA smoke test | A whole-GPU Pod can consume `nvidia.com/gpu` and run `nvidia-smi` |
| `runtime/` | Stable runtime dispatcher, committed profiles, and the vLLM adapter | Health, model discovery, non-streaming generation, and incremental SSE pass |
| `observability/` | Prometheus, Grafana, and DCGM integration | Runtime and GPU metrics are queryable |

`deploy/destroy.sh` is the guarded, target-local inverse of the implemented runtime, GPU, and
cluster stages. It removes only recognized KIP-managed state, including the model cache, so a
fully fresh bootstrap can be exercised again.

The `.gitkeep` file in the remaining empty deployment directory is temporary and must be
removed when the first real configuration file is added.
