# Deployment Assets

This directory will contain the declarative assets required for the v0.1 vertical slice.
Operational commands and their verified order remain in the repository's root `README.md`.

| Directory | Responsibility | Completion gate |
| --- | --- | --- |
| `cluster/` | containerd, kubeadm, and CNI configuration | The Kubernetes node is `Ready` |
| `gpu/` | NVIDIA GPU Operator values and CUDA validation | A Pod can consume `nvidia.com/gpu` |
| `runtime/` | The initial vLLM Deployment and Service | Streaming inference returns tokens |
| `observability/` | Prometheus, Grafana, and DCGM integration | Runtime and GPU metrics are queryable |

The `.gitkeep` file in each directory is temporary and must be removed when the first real
configuration file is added.
