# v0.1 Baseline Experiment

Status: **Not run**

The executable definition lives in `benchmarks/suites/baseline.json`; the generated canonical
result lives under `benchmarks/reports/<run-id>/`. This document is the short human record for
the accepted baseline and should link to that report rather than copying raw request data.
Never record credentials, prompts/responses, private addresses, provider account identifiers,
or access tokens.

## Experiment identity

- Run ID:
- UTC start/end:
- Operator:
- Git commit:
- Suite hash:
- Report path:
- Objective:

## Hardware and platform

- Provider and region:
- Server/SKU:
- CPU and memory:
- GPU model, count, and VRAM:
- GPU interconnect/topology:
- Local storage:
- Network:
- Operating system/kernel:
- NVIDIA driver/CUDA compatibility:
- containerd/Kubernetes/Cilium:
- GPU Operator/DCGM Exporter:
- Prometheus/Grafana/OpenCost:
- AIPerf version and image digest:

## Runtime identity

- Runtime adapter: `vllm`
- Runtime profile: `qwen2.5-7b-instruct`
- Runtime image digest:
- Model repository: `Qwen/Qwen2.5-7B-Instruct`
- Model revision: `a09a35458c702b33eeacc393d103063234e8bc28`
- Served model: `qwen2.5-7b-instruct`
- Worker role: `combined`
- Precision: BF16
- GPU/tensor parallelism: `1` / `1`
- Maximum model length: `8192`
- GPU memory utilization target: `0.90`
- Maximum sequences: `8`

## Pricing identity

- Cost profile:
- Cost-profile SHA-256:
- Currency: `USD`
- Whole-node hourly rate:
- Provider price source:
- Price effective date:
- Physical GPU count:
- Derived GPU hourly rate:
- Power included in rental: `true`

Allocated cost includes node-local idle allocation and is the primary bill-reconciling value.
Usage cost excludes idle allocation and is an efficiency view. DCGM energy is reported
separately and must not be added to the rental when power is already included.

## Fixed workload

- Endpoint: streaming `/v1/chat/completions`
- Prompt source: deterministic synthetic data
- Target input length: 512 tokens
- Requested output length: 128 tokens
- Temperature: `0`
- Seed: `42`
- Ignore EOS: enabled
- Warmup: 100 requests at concurrency 8, excluded
- Measured concurrency: `1, 4, 8, 16, 32`
- Requests per concurrency/trial: 1,000
- Independent trials per concurrency: 5
- Cooldown between trials: 30 seconds
- Total measured requests: 25,000
- AIPerf benchmark-local server/GPU telemetry interval: 333 milliseconds

## Performance and cost summary

The generated report contains per-trial rows, P50/P95/P99 distributions, 95% confidence
intervals, and coefficient of variation. Copy only the accepted aggregate headline here.

| Concurrency | Success % | TTFT P50 | TTFT P95 | TTFT P99 | AIPerf ITL P99 | Server TPOT P99 | E2E P99 | Output tok/s | GPU util % | Allocated USD/request | Usage USD/request | Blended USD/1M tok |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 1 | | | | | | | | | | | | |
| 4 | | | | | | | | | | | | |
| 8 | | | | | | | | | | | | |
| 16 | | | | | | | | | | | | |
| 32 | | | | | | | | | | | | |

Additional accepted totals:

- Total experiment cost, including warmup/cooldown:
- Measured-window allocated cost:
- Measured-window usage cost:
- Input USD/million tokens:
- Output USD/million tokens:
- Requests/USD and tokens/USD:
- Wh/request and kWh/million tokens:
- Peak GPU memory/power/temperature:
- OpenCost audit expected/observed/difference:

The report is invalid if allocated cost differs from `whole-node hourly rate × experiment
elapsed time` by more than the greater of 5% or one minute of node cost.

## Findings

Document the saturation point, throughput/latency/cost frontier, queueing and tail-latency
behaviour, KV-cache pressure, GPU efficiency, trial variability, failures, cost reconciliation,
and any data-quality limitation that affects future comparisons.

## Reproduction

Record the exact command and the generated report path:

```bash
make benchmark SUITE=baseline COST_PROFILE=<profile>
make benchmark-report RUN_ID=<run-id>
make benchmark-serve RUN_ID=<run-id>
```

Only the sanitized report (`index.html`, SVG charts, summaries, and reproducibility manifest)
is eligible for version control. AIPerf exports summary data only. Per-cell summaries are
retained as compressed archives under the node-local benchmark-results volume and copied into
ignored `benchmarks/.work/<run-id>/` for normalization; `make benchmark-clean RETAIN_DAYS=30`
prunes both run locations without deleting the published report.
