# Baseline Experiment

Status: **Not run**

This document becomes the reproducible record of the v0.1 baseline inference experiment. Replace
the placeholders with observed values; do not record credentials, private keys, or access
tokens.

## Experiment metadata

- Date:
- Operator:
- Git commit:
- Objective:

## Hardware

- Provider:
- Server type:
- CPU and memory:
- GPU model, count, and VRAM:
- GPU interconnect/topology:
- Local storage:
- Network:

## Software versions

- Operating system:
- NVIDIA driver:
- CUDA compatibility:
- containerd:
- Kubernetes:
- CNI:
- GPU Operator:
- vLLM:
- Model revision:

## Runtime configuration

- Model:
- Precision/quantisation:
- GPU count:
- Tensor parallelism:
- Relevant vLLM arguments:

## Workload

- Prompt/input length:
- Requested output length:
- Requests per run:
- Concurrency levels:
- Streaming enabled:

## Results

Commit compact results as `results.csv` and `results.json`. Store high-volume request traces in
the ignored `raw/` directory.

| Concurrency | TTFT P50 | TTFT P95 | TTFT P99 | TPOT P99 | E2E P99 | Output tok/s | GPU util |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| | | | | | | | |

## Findings

Document observed throughput, saturation, queueing, tail-latency behaviour, failures, and any
limitations that affect comparison with future experiments.

## Reproduction

Record the exact benchmark command and references to the committed runtime and workload
configuration used for the experiment.
