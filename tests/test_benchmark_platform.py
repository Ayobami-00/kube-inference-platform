"""Focused tests for AIPerf orchestration, cost accounting, and report publication."""

from __future__ import annotations

import base64
import copy
import io
import json
import os
import subprocess
import sys
import tarfile
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT))

import bench.aiperf as aiperf_module  # noqa: E402
import bench.cli as bench_cli  # noqa: E402
from bench.aiperf import (  # noqa: E402
    AIPERF_IMAGE,
    AIPERF_VERSION,
    BenchmarkCell,
    KubernetesJobExecutor,
    _extract_artifact_archive,
    build_cells,
    check_foundation,
    execute_run,
    render_run_plan,
)
from bench.clients import GrafanaClient, OpenCostClient  # noqa: E402
from bench.config import (  # noqa: E402
    Suite,
    ValidationError,
    config_hash,
    load_cost_profile,
    load_suite,
)
from bench.errors import BenchError, ExternalCommandError  # noqa: E402
from bench.pipeline import (  # noqa: E402
    DEFAULT_RUNTIME_IMAGE,
    _grafana_credentials,
    _live_runtime_workload_identity,
    _platform_identity_from_objects,
    _wait_for_observability_convergence,
    collect_telemetry,
    complete_and_publish_run,
    normalize_aiperf_summary,
    normalize_run,
    record_grafana_annotation,
    record_grafana_start_annotation,
)
from bench.report import generate_report, validate_normalized_input  # noqa: E402


def cost_profile_data(name: str = "test-a10") -> dict[str, object]:
    return {
        "schemaVersion": 1,
        "name": name,
        "currency": "USD",
        "nodeHourlyRateUsd": 1.2,
        "provider": "Fixture Cloud",
        "machineType": "single-a10",
        "region": "test-region",
        "source": "https://pricing.example.test/a10",
        "effectiveFrom": "2026-09-01",
        "physicalGpuCount": 1,
        "powerIncludedInRate": True,
    }


def component_versions() -> dict[str, str]:
    return {
        "aiperf": AIPERF_VERSION,
        "kubernetes": "1.36.4",
        "containerd": "2.3.4",
        "helm": "3.21.4",
        "cilium": "1.20.1",
        "gpuOperator": "v26.7.0",
        "nvidiaDriver": "595.91.07",
        "dcgmExporter": "4.6.0-4.8.3-distroless",
        "vllm": "v0.29.0",
        "kubePrometheusStack": "91.4.0",
        "prometheus": "v3.14.0-distroless",
        "grafanaChart": "13.2.4",
        "opencost": "1.121.1",
    }


def runtime_profile_sha256() -> str:
    profile = json.loads(
        (ROOT / "deploy/runtime/vllm/profiles/qwen2.5-7b-instruct.json").read_text()
    )
    return config_hash(profile)


@pytest.fixture
def cost_profile_root(tmp_path: Path) -> Path:
    root = tmp_path / "profiles"
    root.mkdir()
    (root / "test-a10.json").write_text(
        json.dumps(cost_profile_data(), sort_keys=True), encoding="utf-8"
    )
    return root


@pytest.fixture
def cost_profile(cost_profile_root: Path):
    return load_cost_profile("test-a10", root=cost_profile_root)


def normalized_report_data(cost_profile) -> dict[str, object]:
    suite = load_suite("baseline")
    start = datetime(2026, 9, 1, tzinfo=UTC)
    end = start + timedelta(hours=1)
    trials = []
    offset_minutes = 1
    for concurrency in (1, 4, 8, 16, 32):
        for trial_number in range(1, 6):
            trial_start = start + timedelta(minutes=offset_minutes)
            trial_end = trial_start + timedelta(minutes=1)
            offset_minutes += 2
            trials.append(
                {
                    "concurrency": concurrency,
                    "trial": trial_number,
                    "startedAt": trial_start.isoformat().replace("+00:00", "Z"),
                    "endedAt": trial_end.isoformat().replace("+00:00", "Z"),
                    "requests": 1000,
                    "successfulRequests": 999,
                    "failedRequests": 1,
                    "inputTokens": 512000,
                    "outputTokens": 128000,
                    "metrics": {
                        "ttftMs": {
                            "p50": 20.0 + concurrency,
                            "p95": 30.0 + concurrency,
                            "p99": 40.0 + concurrency,
                        },
                        "itlMs": {
                            "p50": 5.0,
                            "p95": 6.0,
                            "p99": 7.0,
                        },
                        "e2eMs": {
                            "p50": 700.0,
                            "p95": 800.0,
                            "p99": 900.0 + concurrency,
                        },
                        "requestThroughputPerSecond": float(concurrency * 2 + trial_number / 10),
                        "outputTokenThroughputPerSecond": float(concurrency * 200),
                        "streamingTimingEvidence": True,
                    },
                    "telemetry": {
                        "gpuUtilizationAveragePct": min(99.0, 30.0 + concurrency * 2),
                        "gpuMemoryUsedAverageBytes": 17_000_000_000.0,
                        "powerAverageWatts": 120.0,
                        "temperatureMaxC": 68.0,
                        "kvCacheAveragePct": min(95.0, 10.0 + concurrency),
                        "requestRunningMax": float(concurrency),
                        "requestQueueMax": float(max(0, concurrency - 8)),
                        "promptTokenRateAveragePerSecond": float(concurrency * 512),
                        "generatedTokenRateAveragePerSecond": float(concurrency * 128),
                        "cachedTokenRateAveragePerSecond": float(concurrency * 64),
                        "serverTtftP50Ms": 19.0 + concurrency,
                        "serverTtftP95Ms": 29.0 + concurrency,
                        "serverTtftP99Ms": 39.0 + concurrency,
                        "serverTpotP50Ms": 4.0,
                        "serverTpotP95Ms": 5.0,
                        "serverTpotP99Ms": 6.0,
                        "serverE2eP50Ms": 690.0,
                        "serverE2eP95Ms": 790.0,
                        "serverE2eP99Ms": 890.0 + concurrency,
                        "gpuPcieTxAverageBytesPerSecond": 104_857_600.0,
                        "gpuPcieRxAverageBytesPerSecond": 52_428_800.0,
                        "gpuSmClockAverageMhz": 1410.0,
                        "gpuMemoryClockAverageMhz": 6251.0,
                        "gpuClockThrottleMaxMask": 0.0,
                        "gpuXidMaxCode": 0.0,
                    },
                    "cost": {
                        "allocatedUsd": 0.04,
                        "usageUsd": 0.03,
                        "inputUsd": 0.02,
                        "outputUsd": 0.02,
                        "energyKwh": 0.002,
                    },
                }
            )
    embedded_profile = dict(cost_profile.data)
    embedded_profile["sha256"] = cost_profile.sha256
    return {
        "schemaVersion": 1,
        "run": {
            "id": "baseline-fixture",
            "startedAt": start.isoformat().replace("+00:00", "Z"),
            "endedAt": end.isoformat().replace("+00:00", "Z"),
            "gitRevision": "0" * 40,
            "gitTreeState": "clean",
            "gitWorkspaceSha256": "1" * 64,
            "aiperfVersion": AIPERF_VERSION,
            "aiperfImage": AIPERF_IMAGE,
            "componentVersions": component_versions(),
            "runtime": "vllm",
            "runtimePodUid": "11111111-2222-3333-4444-555555555555",
            "runtimePodRestartCount": 0,
            "runtimeVersion": "v0.29.0",
            "runtimeImage": "vllm/vllm-openai:v0.29.0@sha256:" + "a" * 64,
            "runtimeProfileSha256": runtime_profile_sha256(),
            "modelProfile": "qwen2.5-7b-instruct",
            "modelName": "qwen2.5-7b-instruct",
            "modelRevision": "a" * 40,
            "gpuModel": "NVIDIA A10",
            "gpuCount": 1,
            "grafanaBaseUrl": "http://localhost:3000",
        },
        "suite": {
            "name": "baseline",
            "sha256": suite.sha256,
            "manifest": suite.data,
            "concurrency": [1, 4, 8, 16, 32],
            "requestsPerCell": 1000,
            "trialsPerCell": 5,
            "confidenceLevel": 0.95,
        },
        "costProfile": embedded_profile,
        "trials": trials,
        "opencost": {
            "windowStart": start.isoformat().replace("+00:00", "Z"),
            "windowEnd": end.isoformat().replace("+00:00", "Z"),
            "queriedAt": (end + timedelta(minutes=1)).isoformat().replace("+00:00", "Z"),
            "allocatedUsd": 1.2,
            "usageUsd": 0.75,
            "inputUsd": 0.5,
            "outputUsd": 0.5,
        },
        "prometheus": {"stepSeconds": 5, "contextSeconds": 120, "seriesComplete": True},
        "dataQuality": {"status": "complete", "warnings": [], "missingSeries": []},
    }


def aiperf_summary(
    *, successful: int = 999, start_time: str | None = None, end_time: str | None = None
) -> dict[str, object]:
    metric = lambda **values: values  # noqa: E731 - compact fixture constructor
    summary: dict[str, object] = {
        # This mirrors AIPerf 0.12's real summary-only export shape: metrics are
        # top-level, aggregate counters use ``avg``, and a clean run serializes
        # the error-only counter as JSON null.
        "request_count": metric(avg=successful),
        "error_request_count": None,
        "input_sequence_length": metric(avg=512),
        "output_sequence_length": metric(avg=128),
        "total_isl": metric(avg=successful * 512),
        "total_osl": metric(avg=successful * 128),
        "time_to_first_token": metric(p50=20, p95=30, p99=40),
        "inter_token_latency": metric(p50=5, p95=6, p99=7),
        "request_latency": metric(p50=700, p95=800, p99=900),
        "request_throughput": metric(avg=10),
        "output_token_throughput": metric(avg=1000),
        "input_config": {
            "endpoint": {"streaming": True},
            "datasets": [{"name": "main", "entries": 1000}],
            "phases": [
                {
                    "name": "profiling",
                    "kind": "profiling",
                    "type": "concurrency",
                    "requests": 1000,
                    "concurrency": 8,
                }
            ],
        },
        "error_summary": [{}] if successful == 999 else [],
        "was_cancelled": False,
    }
    if start_time is not None:
        summary["start_time"] = start_time
    if end_time is not None:
        summary["end_time"] = end_time
    return summary


def run_cli(
    *arguments: str, environment: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "bench", *arguments],
        cwd=ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )


def test_baseline_suite_is_exact_high_rigor_matrix() -> None:
    suite = load_suite("baseline")

    assert suite.data["sweep"]["concurrency"] == [1, 4, 8, 16, 32]
    assert suite.data["sweep"]["requestsPerCell"] == 1000
    assert suite.data["sweep"]["trialsPerCell"] == 5
    assert suite.data["sweep"]["cooldownSeconds"] == 30
    assert suite.data["warmup"] == {
        "requests": 100,
        "concurrency": 8,
        "excludeFromMetrics": True,
    }
    cells = build_cells(suite)
    assert len(cells) == 26
    assert sum(cell.requests for cell in cells if not cell.warmup) == 25_000


def test_benchmark_cluster_check_runs_full_platform_preflight(monkeypatch, capsys) -> None:
    calls = []
    monkeypatch.setattr(bench_cli, "check_foundation", lambda cluster: {"cluster": cluster})
    monkeypatch.setattr(bench_cli, "load_suite", lambda _name: object())
    monkeypatch.setattr(bench_cli, "run_platform_preflight", calls.append)

    result = bench_cli._check(  # noqa: SLF001 - exercise the command boundary directly
        bench_cli.argparse.Namespace(cluster=True, cost_profile=None)
    )

    assert result == 0
    assert calls == [""]
    output = json.loads(capsys.readouterr().out)
    assert output["platformPreflight"] == "passed"
    assert output["pricing"]["status"] == "warning"


def test_suite_rejects_unknown_fields_and_path_traversal(tmp_path: Path) -> None:
    suite = json.loads((ROOT / "benchmarks" / "suites" / "baseline.json").read_text())
    suite["unexpected"] = True
    (tmp_path / "invalid.json").write_text(json.dumps(suite))

    with pytest.raises(ValidationError, match="unknown unexpected"):
        load_suite("invalid", root=tmp_path)
    with pytest.raises(ValidationError, match="invalid suite name"):
        load_suite("../baseline")


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("nodeHourlyRateUsd", 0, "nodeHourlyRateUsd"),
        ("currency", "GBP", "must use USD"),
        ("physicalGpuCount", 0, "physicalGpuCount"),
        ("powerIncludedInRate", False, "must include power"),
        ("effectiveFrom", "September", "YYYY-MM-DD"),
        ("effectiveFrom", "2999-01-01", "must not be in the future"),
        ("nodeHourlyRateUsd", 1_000_000, "less than 1000000"),
        ("physicalGpuCount", 1025, "must be <= 1024"),
        ("provider", "REPLACE_ME", "unconfigured placeholder"),
        ("source", "http://pricing.example.test/a10", "public HTTPS"),
        (
            "source",
            "https://billing-user:secret@pricing.example.test/a10",
            "public HTTPS",
        ),
        ("source", "https://pricing.example.test/a10?token=secret", "query"),
        ("source", "https://pricing.example.test/a10#private", "fragment"),
    ],
)
def test_cost_profile_rejects_unpublishable_values(
    tmp_path: Path, field: str, value: object, message: str
) -> None:
    profile = cost_profile_data("invalid")
    profile[field] = value
    (tmp_path / "invalid.json").write_text(json.dumps(profile))

    with pytest.raises(ValidationError, match=message):
        load_cost_profile("invalid", root=tmp_path)


def test_job_manifest_is_cpu_only_pinned_and_ownership_labelled(cost_profile) -> None:
    suite = load_suite("baseline")
    executor = KubernetesJobExecutor()
    cell = BenchmarkCell(concurrency=16, trial=3, requests=1000)

    manifest = executor.render(suite, cost_profile, "baseline-fixture", cell)
    configmap, job = manifest["items"]
    container = job["spec"]["template"]["spec"]["containers"][0]
    command = container["command"]
    aiperf_command = command[4:]

    assert configmap["metadata"]["labels"]["app.kubernetes.io/managed-by"] == (
        "kube-inference-platform"
    )
    assert container["image"] == AIPERF_IMAGE
    assert container["resources"] == {
        "requests": {"cpu": "2", "memory": "4Gi"},
        "limits": {"cpu": "8", "memory": "8Gi"},
    }
    assert "nvidia.com/gpu" not in json.dumps(job)
    assert job["spec"]["template"]["spec"]["automountServiceAccountToken"] is False
    assert container["env"] == [
        {"name": "AIPERF_GPU_COLLECTION_INTERVAL", "value": "0.333"},
        {"name": "AIPERF_SERVER_METRICS_COLLECTION_INTERVAL", "value": "0.333"},
        {"name": "HF_HOME", "value": "/results/.cache/huggingface"},
        {"name": "TZ", "value": "UTC"},
    ]
    assert command[:2] == ["python", "-c"]
    assert "tarfile.open" in command[2]
    assert command[3] == "/results/baseline-fixture/c16-t3"
    assert aiperf_command == [
        "aiperf",
        "profile",
        "--model",
        "qwen2.5-7b-instruct",
        "--tokenizer",
        "Qwen/Qwen2.5-7B-Instruct",
        "--tokenizer-revision",
        "a09a35458c702b33eeacc393d103063234e8bc28",
        "--url",
        "http://vllm-qwen25-7b.kip-runtime.svc.cluster.local:8000",
        "--endpoint-type",
        "chat",
        "--streaming",
        "--apply-chat-template",
        "--synthetic-input-tokens-mean",
        "512",
        "--synthetic-input-tokens-stddev",
        "0",
        "--output-tokens-mean",
        "128",
        "--output-tokens-stddev",
        "0",
        "--extra-inputs",
        "ignore_eos:true",
        "--extra-inputs",
        "temperature:0",
        "--extra-inputs",
        "seed:42",
        "--concurrency",
        "16",
        "--request-count",
        "1000",
        "--num-dataset-entries",
        "1000",
        "--random-seed",
        "42",
        "--gpu-telemetry",
        "http://127.0.0.1:19400/metrics",
        "--export-level",
        "summary",
        "--output-artifact-dir",
        "/results/baseline-fixture/c16-t3",
    ]
    assert "--warmup-request-count" not in aiperf_command
    assert "ThreadingHTTPServer" in command[2]
    assert "DCGM_UPSTREAM" in command[2]
    assert "payload.replace(b'hostname=\"', b'Hostname=\"')" in command[2]


def test_artifact_archive_extraction_is_safe_and_deterministic(tmp_path: Path) -> None:
    producer = tmp_path / "producer" / "c1-t1"
    producer.mkdir(parents=True)
    (producer / "profile_export_aiperf.json").write_text('{"request_count": 1}')
    archive = tmp_path / "c1-t1.tar.gz"
    with tarfile.open(archive, "w:gz") as output:
        output.add(producer, arcname="c1-t1")

    destination = tmp_path / "consumer" / "c1-t1"
    destination.parent.mkdir()
    _extract_artifact_archive(archive, destination)

    assert (destination / "profile_export_aiperf.json").read_text() == '{"request_count": 1}'
    with pytest.raises(ExternalCommandError, match="already exists"):
        _extract_artifact_archive(archive, destination)


def test_artifact_archive_rejects_path_traversal(tmp_path: Path) -> None:
    archive = tmp_path / "unsafe.tar.gz"
    payload = b"unsafe"
    with tarfile.open(archive, "w:gz") as output:
        root = tarfile.TarInfo("c1-t1")
        root.type = tarfile.DIRTYPE
        output.addfile(root)
        member = tarfile.TarInfo("../outside")
        member.size = len(payload)
        output.addfile(member, io.BytesIO(payload))

    with pytest.raises(ExternalCommandError, match="unsafe member"):
        _extract_artifact_archive(archive, tmp_path / "destination" / "c1-t1")
    assert not (tmp_path / "outside").exists()


def test_rendered_run_plan_has_one_warmup_and_25_measured_jobs(cost_profile) -> None:
    plan = render_run_plan(load_suite("baseline"), cost_profile, "baseline-fixture")

    assert plan["executor"] == "kubernetes-job"
    assert plan["aiperfImage"] == AIPERF_IMAGE
    assert len(plan["cells"]) == 26
    assert "warmup" in plan["cells"][0]["items"][1]["metadata"]["name"]
    assert all(
        item["kind"] in {"ConfigMap", "Job"} for cell in plan["cells"] for item in cell["items"]
    )


def test_failed_job_is_detected_immediately_and_preserved(
    cost_profile, tmp_path: Path, monkeypatch
) -> None:
    commands: list[list[str]] = []

    def fake_run(arguments, **_kwargs):
        commands.append(arguments)
        if "get" in arguments and "job" in arguments:
            status = {"status": {"conditions": [{"type": "Failed", "status": "True"}]}}
            return subprocess.CompletedProcess(arguments, 0, json.dumps(status), "")
        return subprocess.CompletedProcess(arguments, 0, "diagnostic\n", "")

    monkeypatch.setattr("bench.aiperf._run_command", fake_run)
    destination = tmp_path / "failed"
    with pytest.raises(ExternalCommandError, match="failed; diagnostics preserved"):
        KubernetesJobExecutor().execute(
            load_suite("baseline"),
            cost_profile,
            "baseline-fixture",
            BenchmarkCell(concurrency=1, trial=1, requests=1000),
            destination,
            21_600,
        )

    assert (destination / "diagnostics.txt").is_file()
    assert not any("wait" in command for command in commands)
    assert any("delete" in command and "configmap" in command for command in commands)
    assert not any("delete" in command and "job,configmap" in command for command in commands)


def test_completed_job_collects_archive_from_target_local_results_host_path(
    cost_profile, tmp_path: Path, monkeypatch
) -> None:
    commands: list[list[str]] = []
    results = tmp_path / "node-results"
    run_id = "baseline-fixture"
    archive_source = results / run_id / "c1-t1.tar.gz"
    archive_source.parent.mkdir(parents=True)
    producer = tmp_path / "producer" / "c1-t1"
    producer.mkdir(parents=True)
    (producer / "profile_export_aiperf.json").write_text('{"request_count": 1000}')
    with tarfile.open(archive_source, "w:gz") as output:
        output.add(producer, arcname="c1-t1")

    def fake_run(arguments, **_kwargs):
        commands.append(arguments)
        if "get" in arguments and "job" in arguments:
            status = {"status": {"conditions": [{"type": "Complete", "status": "True"}]}}
            return subprocess.CompletedProcess(arguments, 0, json.dumps(status), "")
        return subprocess.CompletedProcess(arguments, 0, "", "")

    monkeypatch.setattr("bench.aiperf.RESULTS_HOST_PATH", results)
    monkeypatch.setattr("bench.aiperf._run_command", fake_run)
    destination = tmp_path / "work" / "c1-t1"

    result = KubernetesJobExecutor().execute(
        load_suite("baseline"),
        cost_profile,
        run_id,
        BenchmarkCell(concurrency=1, trial=1, requests=1000),
        destination,
        21_600,
    )

    assert result["status"] == "succeeded"
    assert (destination / "profile_export_aiperf.json").read_text() == ('{"request_count": 1000}')
    assert not any("cp" in command for command in commands)
    assert any("delete" in command and "job,configmap" in command for command in commands)


@pytest.mark.parametrize("outcome", ["timeout", "interrupt"])
def test_timeout_or_interrupt_stops_the_load_job(
    cost_profile, tmp_path: Path, monkeypatch, outcome: str
) -> None:
    commands: list[list[str]] = []

    def fake_run(arguments, **_kwargs):
        commands.append(arguments)
        return subprocess.CompletedProcess(arguments, 0, "diagnostic\n", "")

    executor = KubernetesJobExecutor()
    monkeypatch.setattr("bench.aiperf._run_command", fake_run)
    if outcome == "timeout":
        monkeypatch.setattr(executor, "_wait_for_terminal", lambda *_args: "timeout")
        expected_error = ExternalCommandError
    else:

        def interrupt(*_args):
            raise KeyboardInterrupt

        monkeypatch.setattr(executor, "_wait_for_terminal", interrupt)
        expected_error = KeyboardInterrupt

    with pytest.raises(expected_error):
        executor.execute(
            load_suite("baseline"),
            cost_profile,
            "baseline-fixture",
            BenchmarkCell(concurrency=1, trial=1, requests=1000),
            tmp_path / outcome,
            21_600,
        )

    assert any("delete" in command and "job,configmap" in command for command in commands)


def test_aiperf_normalizer_extracts_only_aggregated_metrics() -> None:
    normalized = normalize_aiperf_summary(aiperf_summary(), 1000)

    assert normalized["successfulRequests"] == 999
    assert normalized["failedRequests"] == 1
    assert normalized["inputTokens"] == 999 * 512
    assert normalized["outputTokens"] == 999 * 128
    assert normalized["metrics"]["ttftMs"] == {"p50": 20.0, "p95": 30.0, "p99": 40.0}
    assert normalized["metrics"]["itlMs"] == {"p50": 5.0, "p95": 6.0, "p99": 7.0}
    assert "tpotMs" not in normalized["metrics"]
    assert normalized["metrics"]["streamingTimingEvidence"] is True


def test_aiperf_normalizer_accepts_real_v012_summary_shape() -> None:
    summary = json.loads((ROOT / "tests" / "fixtures" / "aiperf-v0.12-summary.json").read_text())

    normalized = normalize_aiperf_summary(summary, 64)

    assert normalized["successfulRequests"] == 64
    assert normalized["failedRequests"] == 0
    assert normalized["inputTokens"] == 6400
    assert normalized["outputTokens"] == 12799
    assert normalized["metrics"]["ttftMs"]["p95"] == pytest.approx(33.0437491)
    assert normalized["metrics"]["itlMs"]["p95"] == pytest.approx(18.883067175879397)


def test_aiperf_normalizer_supports_top_level_schema_and_explicit_error_count() -> None:
    summary = aiperf_summary(successful=997)
    summary["error_request_count"] = {"avg": 3}
    summary["error_summary"] = [{"type": "HTTPError", "count": 3}]

    normalized = normalize_aiperf_summary(summary, 1000)

    assert normalized["successfulRequests"] == 997
    assert normalized["failedRequests"] == 3


@pytest.mark.parametrize("metric", ["time_to_first_token", "inter_token_latency"])
def test_aiperf_normalizer_requires_positive_streaming_timing(metric: str) -> None:
    summary = aiperf_summary()
    summary[metric]["p95"] = 0

    with pytest.raises(BenchError, match="streaming timing evidence"):
        normalize_aiperf_summary(summary, 1000)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("not-streaming", "configured streaming request run"),
        ("wrong-request-count", "configured streaming request run"),
        ("missing-input-config", "streaming input configuration"),
    ],
)
def test_aiperf_normalizer_requires_matching_streaming_configuration(
    mutation: str, message: str
) -> None:
    summary = aiperf_summary()
    if mutation == "not-streaming":
        summary["input_config"]["endpoint"]["streaming"] = False
    elif mutation == "wrong-request-count":
        summary["input_config"]["phases"][0]["requests"] = 999
    else:
        summary.pop("input_config")

    with pytest.raises(BenchError, match=message):
        normalize_aiperf_summary(summary, 1000)


def test_aiperf_normalizer_rejects_cancelled_summary() -> None:
    summary = aiperf_summary()
    summary["was_cancelled"] = True

    with pytest.raises(BenchError, match="was cancelled"):
        normalize_aiperf_summary(summary, 1000)


def test_opencost_client_uses_outward_rounded_whole_second_window() -> None:
    calls: list[tuple[str, dict[str, object]]] = []

    class HttpFixture:
        def get(self, path, query):
            calls.append((path, query))
            return {"code": 200, "data": {}}

    client = OpenCostClient("http://127.0.0.1:9003")
    client._http = HttpFixture()
    client.inference_cost(
        start="2026-09-22T12:16:40.586704Z",
        end="2026-09-22T12:17:48.405725Z",
        cost_basis="allocation",
        model_name="qwen2.5-7b-instruct",
    )

    assert calls[0][0] == "/inferenceCost/total"
    assert calls[0][1]["window"] == "2026-09-22T12:16:40Z,2026-09-22T12:17:49Z"


def test_collect_telemetry_excludes_neighboring_windows() -> None:
    trial_start = "2026-09-01T00:00:00.000600Z"
    trial_end = "2026-09-01T00:01:00.001099Z"

    class PrometheusFixture:
        def __init__(self) -> None:
            self.queries: list[tuple[str, str, str, str]] = []

        def query_range(self, expression, *, start, end, step):
            self.queries.append((expression, start, end, step))
            end_epoch = datetime.fromisoformat(end.replace("Z", "+00:00")).timestamp()
            if "increase(" in expression:
                return [{"metric": {}, "values": [[end_epoch, "4"]]}]
            return [
                {
                    "metric": {},
                    "values": [
                        [
                            datetime.fromisoformat(start.replace("Z", "+00:00")).timestamp(),
                            "99",
                        ],
                        [
                            datetime.fromisoformat(trial_start.replace("Z", "+00:00")).timestamp(),
                            "1",
                        ],
                        [
                            datetime.fromisoformat(trial_end.replace("Z", "+00:00")).timestamp(),
                            "3",
                        ],
                        [end_epoch, "99"],
                    ],
                }
            ]

    prometheus = PrometheusFixture()
    telemetry = collect_telemetry(
        prometheus,
        start=trial_start,
        end=trial_end,
        context_seconds=120,
        model_name="qwen2.5-7b-instruct",
        runtime="vllm",
        model_profile="qwen2.5-7b-instruct",
    )

    # The 99-valued context samples represent neighboring cells and must not
    # influence reductions over the trial's own 1/3-valued gauge samples.
    assert telemetry["gpuUtilizationAveragePct"] == 200
    assert telemetry["temperatureMaxC"] == 3
    exact = [query for query in prometheus.queries if "increase(" in query[0]]
    assert exact
    assert all("[60000ms]" in expression for expression, *_rest in exact)
    assert all(
        start == end == "2026-09-01T00:01:00.001000Z" for _expression, start, end, _step in exact
    )
    assert all(step == "1s" for _expression, _start, _end, step in exact)
    assert not any("[5m]" in expression or "rate5m" in expression for expression, *_ in exact)


def test_normalize_run_joins_aiperf_prometheus_and_opencost_exact_windows(
    cost_profile, tmp_path: Path, monkeypatch
) -> None:
    suite = load_suite("baseline")
    start = datetime(2026, 9, 1, tzinfo=UTC)
    timeline = []
    for index, cell in enumerate(build_cells(suite)[1:]):
        cell_start = start + timedelta(minutes=index * 2 + 1)
        cell_end = cell_start + timedelta(minutes=1)
        timeline.append(
            {
                "cell": cell.identifier,
                "concurrency": cell.concurrency,
                "trial": cell.trial,
                "warmup": False,
                "startedAt": cell_start.isoformat().replace("+00:00", "Z"),
                "endedAt": cell_end.isoformat().replace("+00:00", "Z"),
                "status": "succeeded",
            }
        )
        artifact = tmp_path / "aiperf" / cell.identifier
        artifact.mkdir(parents=True)
        (artifact / "profile_export_aiperf.json").write_text(
            json.dumps(
                aiperf_summary(
                    start_time=cell_start.replace(tzinfo=None).isoformat(),
                    end_time=cell_end.replace(tzinfo=None).isoformat(),
                )
            )
        )
    receipt = {
        "runId": "baseline-fixture",
        "startedAt": start.isoformat().replace("+00:00", "Z"),
        "endedAt": (start + timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
        "repository": {
            "revision": "0" * 40,
            "treeState": "clean",
            "workspaceSha256": "1" * 64,
        },
        "environment": {
            "componentVersions": component_versions(),
            "platformIdentity": {
                "runtimeVersion": "v0.29.0",
                "runtimeImage": DEFAULT_RUNTIME_IMAGE,
                "runtimeProfileSha256": runtime_profile_sha256(),
                "modelProfile": "qwen2.5-7b-instruct",
                "modelName": "qwen2.5-7b-instruct",
                "modelRevision": "a09a35458c702b33eeacc393d103063234e8bc28",
                "gpuModel": "NVIDIA-A10",
                "gpuCount": 1,
            },
            "runtimeWorkload": {
                "podUid": "11111111-2222-3333-4444-555555555555",
                "restartCount": 0,
            },
        },
        "timeline": timeline,
    }

    class PrometheusFixture:
        def __init__(self) -> None:
            self.queries: list[tuple[str, str, str]] = []

        def query_range(self, expression, *, start, end, step):
            self.queries.append((expression, start, end))
            if "utilization_ratio" in expression or "usage_ratio" in expression:
                value = "0.5"
            elif "memory_used" in expression:
                value = "17000000000"
            elif "power_watts" in expression:
                value = "120"
            elif "temperature" in expression:
                value = "68"
            else:
                value = "3"
            range_start = datetime.fromisoformat(start.replace("Z", "+00:00"))
            range_end = datetime.fromisoformat(end.replace("Z", "+00:00"))
            midpoint = range_start + (range_end - range_start) / 2
            return [{"metric": {}, "values": [[midpoint.timestamp(), value]]}]

    class OpenCostFixture:
        def __init__(self) -> None:
            self.inference_calls: list[tuple[str, str, str, str]] = []

        def inference_cost(self, *, start, end, cost_basis, model_name):
            self.inference_calls.append((start, end, cost_basis, model_name))
            total = 0.04 if cost_basis == "allocation" else 0.03
            return {"data": [{"totalCost": total, "inputCost": 0.02, "outputCost": 0.02}]}

        def allocation(
            self,
            *,
            start,
            end,
            aggregate="namespace,controllerKind,controller",
            share_idle=True,
        ):
            assert aggregate == "namespace,controllerKind,controller"
            assert start == receipt["startedAt"]
            assert end == receipt["endedAt"]
            assert share_idle is True
            return {"data": [{"totalCost": 1.2}]}

    prometheus = PrometheusFixture()
    opencost = OpenCostFixture()
    monkeypatch.setattr(
        "bench.pipeline._live_platform_identity",
        lambda *_args: {
            "runtimeVersion": "v0.29.0",
            "runtimeImage": DEFAULT_RUNTIME_IMAGE,
            "runtimeProfileSha256": runtime_profile_sha256(),
            "modelProfile": "qwen2.5-7b-instruct",
            "modelName": "qwen2.5-7b-instruct",
            "modelRevision": "a09a35458c702b33eeacc393d103063234e8bc28",
            "gpuModel": "NVIDIA-A10",
            "gpuCount": 1,
        },
    )
    normalized = normalize_run(
        suite,
        cost_profile,
        receipt,
        tmp_path,
        prometheus=prometheus,
        opencost=opencost,
    )

    assert len(normalized["trials"]) == 25
    assert normalized["trials"][0]["telemetry"]["gpuUtilizationAveragePct"] == 50
    assert normalized["trials"][0]["telemetry"]["kvCacheAveragePct"] == 50
    assert normalized["trials"][0]["telemetry"]["serverTtftP95Ms"] == 3000
    assert normalized["trials"][0]["telemetry"]["gpuPcieTxAverageBytesPerSecond"] == 3
    assert normalized["trials"][0]["cost"]["energyKwh"] == pytest.approx(0.002)
    assert normalized["opencost"]["allocatedUsd"] == 1.2
    assert normalized["dataQuality"]["status"] == "warning"
    first_start = datetime.fromisoformat(prometheus.queries[0][1].replace("Z", "+00:00"))
    first_end = datetime.fromisoformat(prometheus.queries[0][2].replace("Z", "+00:00"))
    assert first_start == start - timedelta(minutes=1)
    assert first_end == start + timedelta(minutes=4)
    assert all(
        'model_name="qwen2.5-7b-instruct"' in expression
        for expression, _start, _end in prometheus.queries
    )
    assert all('runtime="vllm"' in expression for expression, _start, _end in prometheus.queries)
    assert all(
        'model_profile="qwen2.5-7b-instruct"' in expression
        for expression, _start, _end in prometheus.queries
    )
    assert any("kip:gpu:runtime_utilization_ratio" in query[0] for query in prometheus.queries)
    assert any("kip:gpu:runtime_power_watts" in query[0] for query in prometheus.queries)
    assert any("increase(vllm:prompt_tokens_total" in query[0] for query in prometheus.queries)
    assert any("increase(vllm:generation_tokens_total" in query[0] for query in prometheus.queries)
    assert any("increase(vllm:prefix_cache_hits_total" in query[0] for query in prometheus.queries)
    assert any(
        "increase(vllm:time_to_first_token_seconds_bucket" in query[0]
        and "histogram_quantile(0.95" in query[0]
        for query in prometheus.queries
    )
    assert any(
        "increase(vllm:request_time_per_output_token_seconds_bucket" in query[0]
        and "histogram_quantile(0.95" in query[0]
        for query in prometheus.queries
    )
    assert any(
        "increase(vllm:e2e_request_latency_seconds_bucket" in query[0]
        and "histogram_quantile(0.95" in query[0]
        for query in prometheus.queries
    )
    exact_queries = [query for query in prometheus.queries if "increase(" in query[0]]
    assert exact_queries
    assert all("[60000ms]" in expression for expression, _start, _end in exact_queries)
    assert all(query_start == query_end for _expression, query_start, query_end in exact_queries)
    assert not any("rate5m" in expression for expression, _start, _end in prometheus.queries)
    assert not any("kip:latency:" in expression for expression, _start, _end in prometheus.queries)
    assert any(
        "kip:gpu:runtime_pcie_tx_bytes_per_second" in query[0] for query in prometheus.queries
    )
    assert any("kip:gpu:runtime_sm_clock_mhz" in query[0] for query in prometheus.queries)
    assert any("kip:gpu:runtime_clock_throttle" in query[0] for query in prometheus.queries)
    assert any("kip:gpu:runtime_xid_errors" in query[0] for query in prometheus.queries)
    assert opencost.inference_calls[0] == (
        timeline[0]["startedAt"],
        timeline[0]["endedAt"],
        "allocation",
        "qwen2.5-7b-instruct",
    )
    validate_normalized_input(normalized, run_id="baseline-fixture", cost_profile=cost_profile)


def test_platform_identity_is_captured_from_live_owned_runtime_and_gpu_node(cost_profile) -> None:
    suite = load_suite("baseline")
    profile_document = json.loads(
        (ROOT / "deploy/runtime/vllm/profiles/qwen2.5-7b-instruct.json").read_text()
    )
    profile_hash = config_hash(profile_document)
    revision = profile_document["model"]["revision"]
    labels = {
        "app.kubernetes.io/managed-by": "kube-inference-platform",
        "kube-inference-platform.io/runtime": "vllm",
        "kube-inference-platform.io/model-profile": "qwen2.5-7b-instruct",
        "kube-inference-platform.io/served-model": "qwen2.5-7b-instruct",
        "kube-inference-platform.io/model-revision": revision,
    }
    deployment = {
        "metadata": {
            "name": "vllm-qwen25-7b",
            "namespace": "kip-runtime",
            "labels": labels,
            "annotations": {
                "kube-inference-platform/runtime-version": "v0.29.0",
                "kube-inference-platform/profile": "qwen2.5-7b-instruct",
                "kube-inference-platform/profile-sha256": profile_hash,
                "kube-inference-platform/model-revision": revision,
                "kube-inference-platform/image-digest": DEFAULT_RUNTIME_IMAGE.partition("@")[2],
                "kube-inference-platform.io/model-id": "Qwen/Qwen2.5-7B-Instruct",
            },
        },
        "spec": {
            "template": {
                "metadata": {"labels": labels},
                "spec": {"containers": [{"name": "vllm", "image": DEFAULT_RUNTIME_IMAGE}]},
            }
        },
    }
    nodes = {
        "items": [
            {
                "metadata": {"labels": {"nvidia.com/gpu.product": "NVIDIA-A10"}},
                "status": {
                    "capacity": {"nvidia.com/gpu": "1"},
                    "allocatable": {"nvidia.com/gpu": "1"},
                },
            }
        ]
    }

    identity = _platform_identity_from_objects(
        suite, cost_profile, component_versions(), deployment, nodes
    )

    assert identity == {
        "runtimeVersion": "v0.29.0",
        "runtimeImage": DEFAULT_RUNTIME_IMAGE,
        "runtimeProfileSha256": profile_hash,
        "modelProfile": "qwen2.5-7b-instruct",
        "modelName": "qwen2.5-7b-instruct",
        "modelRevision": revision,
        "gpuModel": "NVIDIA-A10",
        "gpuCount": 1,
    }

    deployment["metadata"]["annotations"]["kube-inference-platform/profile-sha256"] = "0" * 64
    with pytest.raises(BenchError, match="profile-sha256"):
        _platform_identity_from_objects(
            suite, cost_profile, component_versions(), deployment, nodes
        )


def test_runtime_workload_identity_captures_pod_uid_and_restart_count(monkeypatch) -> None:
    captured = {}

    def fake_kubectl(arguments, description):
        captured["arguments"] = arguments
        captured["description"] = description
        return {
            "items": [
                {
                    "metadata": {"uid": "11111111-2222-3333-4444-555555555555"},
                    "status": {
                        "phase": "Running",
                        "containerStatuses": [{"name": "vllm", "ready": True, "restartCount": 2}],
                    },
                }
            ]
        }

    monkeypatch.setattr("bench.pipeline._kubectl_json", fake_kubectl)
    identity = _live_runtime_workload_identity(load_suite("baseline"))

    assert identity == {
        "podUid": "11111111-2222-3333-4444-555555555555",
        "restartCount": 2,
    }
    assert captured["arguments"] == [
        "-n",
        "kip-runtime",
        "get",
        "pods",
        "-l",
        "app.kubernetes.io/name=vllm,app.kubernetes.io/instance=qwen2.5-7b-instruct",
        "-o",
        "json",
    ]


def test_observability_convergence_requires_fresh_metrics_and_token_costs() -> None:
    ended_at = "2026-09-01T01:00:00Z"
    ended_epoch = datetime.fromisoformat(ended_at.replace("Z", "+00:00")).timestamp()

    class PrometheusFixture:
        def query_range(self, expression, *, start, end, step):
            assert "generation_tokens_total" in expression
            assert start == ended_at
            assert step == "5s"
            return [{"metric": {}, "values": [[ended_epoch + 5, str(ended_epoch + 5)]]}]

    class OpenCostFixture:
        def allocation(self, **_kwargs):
            return {"data": [{"totalCost": 1.2}]}

        def inference_cost(self, **_kwargs):
            return {"data": [{"totalCost": 1.0, "inputCost": 0.4, "outputCost": 0.6}]}

    _wait_for_observability_convergence(
        PrometheusFixture(),
        OpenCostFixture(),
        {"startedAt": "2026-09-01T00:00:00Z", "endedAt": ended_at},
        "qwen2.5-7b-instruct",
        timeout_seconds=0,
    )


def test_observability_convergence_times_out_on_stale_scrape() -> None:
    ended_at = "2026-09-01T01:00:00Z"
    ended_epoch = datetime.fromisoformat(ended_at.replace("Z", "+00:00")).timestamp()

    class PrometheusFixture:
        def query_range(self, *_args, **_kwargs):
            return [{"metric": {}, "values": [[ended_epoch, str(ended_epoch - 30)]]}]

    class OpenCostFixture:
        def allocation(self, **_kwargs):
            pytest.fail("stale Prometheus data must block OpenCost collection")

    with pytest.raises(ExternalCommandError, match="did not converge"):
        _wait_for_observability_convergence(
            PrometheusFixture(),
            OpenCostFixture(),
            {"startedAt": "2026-09-01T00:00:00Z", "endedAt": ended_at},
            "qwen2.5-7b-instruct",
            timeout_seconds=0,
        )


def test_grafana_client_posts_region_with_redacted_credentials(monkeypatch) -> None:
    captured = {}

    class Response(io.BytesIO):
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            self.close()

    def fake_urlopen(request, timeout):
        captured["request"] = request
        captured["timeout"] = timeout
        return Response(b'{"id":42,"message":"Annotation added"}')

    monkeypatch.setattr("bench.clients.urlopen", fake_urlopen)
    client = GrafanaClient("http://127.0.0.1:13000", "admin", "super-secret")
    annotation_id = client.create_region_annotation(
        start_milliseconds=1_000,
        end_milliseconds=2_000,
        tags=["kip-benchmark", "run:baseline-fixture"],
        text="KIP benchmark baseline-fixture",
    )

    assert annotation_id == 42
    assert "super-secret" not in repr(client)
    assert json.loads(captured["request"].data) == {
        "time": 1_000,
        "timeEnd": 2_000,
        "isRegion": True,
        "tags": ["kip-benchmark", "run:baseline-fixture"],
        "text": "KIP benchmark baseline-fixture",
    }
    authorization = captured["request"].get_header("Authorization")
    assert authorization == "Basic " + base64.b64encode(b"admin:super-secret").decode()


def test_grafana_credentials_are_read_only_from_managed_secret(monkeypatch) -> None:
    secret = {
        "data": {
            "admin-user": base64.b64encode(b"admin").decode(),
            "admin-password": base64.b64encode(b"ephemeral-password").decode(),
        }
    }
    captured = {}

    def fake_run(arguments, **kwargs):
        captured["arguments"] = arguments
        captured["kwargs"] = kwargs
        return subprocess.CompletedProcess(arguments, 0, json.dumps(secret), "")

    monkeypatch.setattr("bench.pipeline.subprocess.run", fake_run)
    assert _grafana_credentials() == ("admin", "ephemeral-password")
    assert captured["arguments"][-5:] == [
        "get",
        "secret",
        "kip-grafana-admin",
        "-o",
        "json",
    ]
    assert captured["kwargs"]["capture_output"] is True


def test_benchmark_annotation_uses_exact_experiment_window_and_tags(cost_profile) -> None:
    captured = {}

    class GrafanaFixture:
        def create_region_annotation(self, **kwargs):
            captured.update(kwargs)
            return 73

    receipt = {
        "runId": "baseline-fixture",
        "startedAt": "2026-09-01T00:00:00Z",
        "endedAt": "2026-09-01T01:00:00Z",
    }
    annotation_id = record_grafana_annotation(
        receipt,
        load_suite("baseline"),
        cost_profile,
        client=GrafanaFixture(),
    )

    assert annotation_id == 73
    assert captured["start_milliseconds"] == 1_788_220_800_000
    assert captured["end_milliseconds"] == 1_788_224_400_000
    assert captured["tags"] == [
        "kip-benchmark",
        "run:baseline-fixture",
        "suite:baseline",
        "profile:qwen2.5-7b-instruct",
        "cost-profile:test-a10",
    ]


def test_benchmark_start_annotation_precedes_load_generation(cost_profile) -> None:
    captured = {}

    class GrafanaFixture:
        def create_point_annotation(self, **kwargs):
            captured.update(kwargs)
            return 74

    suite = load_suite("baseline")
    annotation_id = record_grafana_start_annotation(
        "baseline-fixture",
        "2026-09-01T12:00:00Z",
        suite,
        cost_profile,
        client=GrafanaFixture(),
    )

    assert annotation_id == 74
    assert captured["timestamp_milliseconds"] == 1_788_264_000_000
    assert "benchmark-start" in captured["tags"]
    assert "run:baseline-fixture" in captured["tags"]


def test_annotation_failure_blocks_report_publication(
    cost_profile, tmp_path: Path, monkeypatch
) -> None:
    normalized = normalized_report_data(cost_profile)

    @contextmanager
    def fake_clients():
        yield object(), object()

    monkeypatch.setattr("bench.pipeline.observability_clients", fake_clients)
    monkeypatch.setattr("bench.pipeline._wait_for_observability_convergence", lambda *_args: None)
    monkeypatch.setattr("bench.pipeline.normalize_run", lambda *_args, **_kwargs: normalized)

    def annotation_failure(*_args, **_kwargs):
        raise ExternalCommandError("Grafana annotation unavailable")

    monkeypatch.setattr("bench.pipeline.record_grafana_annotation", annotation_failure)

    def unexpected_report(*_args, **_kwargs):
        pytest.fail("report generation must not run after annotation failure")

    monkeypatch.setattr("bench.pipeline.generate_report", unexpected_report)
    with pytest.raises(ExternalCommandError, match="annotation unavailable"):
        complete_and_publish_run(
            load_suite("baseline"),
            cost_profile,
            {"runId": "baseline-fixture"},
            tmp_path,
        )
    assert not (tmp_path / "normalized.json").exists()


def test_cli_dry_run_does_not_require_kubectl(cost_profile_root: Path) -> None:
    environment = os.environ.copy()
    environment["KIP_COST_PROFILES_ROOT"] = str(cost_profile_root)
    environment["KIP_KUBECTL"] = "definitely-not-installed"

    result = run_cli(
        "run",
        "--suite",
        "baseline",
        "--cost-profile",
        "test-a10",
        "--run-id",
        "baseline-fixture",
        "--dry-run",
        environment=environment,
    )

    assert result.returncode == 0, result.stderr
    output = json.loads(result.stdout)
    assert output["runId"] == "baseline-fixture"
    assert len(output["cells"]) == 26


def test_execute_run_invokes_publication_finalizer_automatically(
    cost_profile, tmp_path: Path, monkeypatch
) -> None:
    baseline = load_suite("baseline")
    data = copy.deepcopy(baseline.data)
    data["warmup"]["requests"] = 1
    data["warmup"]["concurrency"] = 1
    data["sweep"]["concurrency"] = [1]
    data["sweep"]["requestsPerCell"] = 2
    data["sweep"]["trialsPerCell"] = 1
    data["sweep"]["cooldownSeconds"] = 0
    suite = Suite("baseline", data, baseline.path, config_hash(data))

    cell_timeouts = []

    class ExecutorFixture:
        def render(self, suite, profile, run_id, cell):
            return {"cell": cell.identifier}

        def execute(self, suite, profile, run_id, cell, destination, timeout_seconds):
            cell_timeouts.append(timeout_seconds)
            destination.mkdir(parents=True)
            (destination / "profile_export_aiperf.json").write_text("{}")
            started = datetime.now(UTC)
            return {
                "cell": cell.identifier,
                "concurrency": cell.concurrency,
                "trial": cell.trial,
                "warmup": cell.warmup,
                "startedAt": started.isoformat().replace("+00:00", "Z"),
                "endedAt": (started + timedelta(seconds=1)).isoformat().replace("+00:00", "Z"),
                "status": "succeeded",
            }

    finalized = {}

    def finalizer(suite, profile, receipt, destination):
        finalized["receipt"] = receipt
        assert (destination / "aiperf" / "warmup" / "profile_export_aiperf.json").is_file()
        assert (destination / "aiperf" / "c1-t1" / "profile_export_aiperf.json").is_file()
        return {"report": "published"}

    monkeypatch.setenv("KIP_BENCH_WORK_ROOT", str(tmp_path / "work"))
    monkeypatch.setattr("bench.aiperf.run_platform_preflight", lambda profile: None)
    monkeypatch.setattr("bench.aiperf._require_loaded_inputs_current", lambda *_args: None)
    identity_calls = []

    def fixed_identity():
        identity_calls.append("captured")
        return {
            "revision": "0" * 40,
            "treeState": "dirty",
            "workspaceSha256": "1" * 64,
        }

    monkeypatch.setattr(
        "bench.aiperf.capture_repository_identity",
        fixed_identity,
    )
    monkeypatch.setattr("bench.aiperf.check_foundation", lambda cluster: {})
    monkeypatch.setattr("bench.aiperf.require_no_active_benchmark", lambda: None)
    environment_calls = []

    def fixed_environment(*_args):
        environment_calls.append("captured")
        return {
            "componentVersions": component_versions(),
            "platformIdentity": {
                "runtimeVersion": "v0.29.0",
                "runtimeImage": DEFAULT_RUNTIME_IMAGE,
                "runtimeProfileSha256": runtime_profile_sha256(),
                "modelProfile": "qwen2.5-7b-instruct",
                "modelName": "qwen2.5-7b-instruct",
                "modelRevision": "a09a35458c702b33eeacc393d103063234e8bc28",
                "gpuModel": "NVIDIA-A10",
                "gpuCount": 1,
            },
            "runtimeWorkload": {
                "podUid": "11111111-2222-3333-4444-555555555555",
                "restartCount": 0,
            },
        }

    monkeypatch.setattr("bench.pipeline.capture_benchmark_environment", fixed_environment)
    monkeypatch.setattr(
        "bench.pipeline.record_grafana_start_annotation", lambda *_args, **_kwargs: 91
    )
    result = execute_run(
        suite,
        cost_profile,
        "baseline-small",
        executor=ExecutorFixture(),
        finalizer=finalizer,
    )

    assert result["publication"] == {"report": "published"}
    assert len(finalized["receipt"]["timeline"]) == 2
    assert finalized["receipt"]["grafanaStartAnnotationId"] == 91
    assert finalized["receipt"]["repository"] == {
        "revision": "0" * 40,
        "treeState": "dirty",
        "workspaceSha256": "1" * 64,
    }
    assert identity_calls == ["captured", "captured"]
    assert environment_calls == ["captured", "captured"]
    assert cell_timeouts == [21600, 21600]


def test_resume_requires_explicit_run_id(cost_profile_root: Path) -> None:
    environment = os.environ.copy()
    environment["KIP_COST_PROFILES_ROOT"] = str(cost_profile_root)

    result = run_cli(
        "run",
        "--suite",
        "baseline",
        "--cost-profile",
        "test-a10",
        "--resume",
        environment=environment,
    )

    assert result.returncode == 2
    assert "--resume requires an explicit --run-id" in result.stderr


def test_repository_identity_freezes_dirty_untracked_content(tmp_path: Path, monkeypatch) -> None:
    untracked = tmp_path / "benchmark-input.json"
    untracked.write_bytes(b'{"value":1}\n')
    monkeypatch.setenv("KIP_REPOSITORY_ROOT", str(tmp_path))

    def fake_git(arguments, root):
        assert root == tmp_path
        if arguments[:2] == ["rev-parse", "--verify"]:
            return ("2" * 40 + "\n").encode()
        if arguments[0] == "status":
            return b"? benchmark-input.json\0"
        if arguments[0] == "diff":
            return b"tracked patch\n"
        if arguments[0] == "ls-files":
            return b"benchmark-input.json\0"
        raise AssertionError(arguments)

    monkeypatch.setattr(aiperf_module, "_run_git_bytes", fake_git)
    first = aiperf_module.capture_repository_identity()
    untracked.write_bytes(b'{"value":2}\n')
    second = aiperf_module.capture_repository_identity()

    assert first["revision"] == "2" * 40
    assert first["treeState"] == "dirty"
    assert first["workspaceSha256"] != second["workspaceSha256"]


def test_cli_rejects_path_traversal_with_exit_two(cost_profile_root: Path) -> None:
    environment = os.environ.copy()
    environment["KIP_COST_PROFILES_ROOT"] = str(cost_profile_root)

    result = run_cli(
        "run",
        "--suite",
        "../baseline",
        "--cost-profile",
        "test-a10",
        "--dry-run",
        environment=environment,
    )

    assert result.returncode == 2
    assert "invalid suite name" in result.stderr


def test_foundation_is_owned_internal_persistent_storage() -> None:
    status = check_foundation(cluster=False)
    foundation = json.loads((ROOT / "benchmarks" / "kubernetes" / "foundation.json").read_text())
    resources = {(item["kind"], item["metadata"]["name"]): item for item in foundation["items"]}

    assert status["executor"] == "kubernetes-job"
    assert ("Namespace", "kip-benchmark") in resources
    pv = resources[("PersistentVolume", "kip-benchmark-results")]
    assert pv["spec"]["capacity"]["storage"] == "20Gi"
    assert pv["spec"]["persistentVolumeReclaimPolicy"] == "Retain"
    assert pv["spec"]["hostPath"] == {
        "path": "/var/lib/kube-inference-platform/benchmark-results",
        "type": "Directory",
    }
    assert "Service" not in {item["kind"] for item in foundation["items"]}


def _live_foundation_objects() -> dict[tuple[str, str, str | None], dict[str, object]]:
    foundation = json.loads((ROOT / "benchmarks" / "kubernetes" / "foundation.json").read_text())
    resources = {}
    for item in foundation["items"]:
        live = copy.deepcopy(item)
        live["metadata"]["resourceVersion"] = "1234"
        live["metadata"]["uid"] = "11111111-2222-3333-4444-555555555555"
        if live["kind"] == "Namespace":
            live["metadata"]["labels"]["kubernetes.io/metadata.name"] = "kip-benchmark"
        if live["kind"] == "PersistentVolumeClaim":
            live["status"] = {"phase": "Bound", "capacity": {"storage": "20Gi"}}
        identity = (
            live["kind"],
            live["metadata"]["name"],
            live["metadata"].get("namespace"),
        )
        resources[identity] = live
    return resources


def _set_nested(value: dict[str, object], path: tuple[str, ...], replacement: object) -> None:
    current = value
    for part in path[:-1]:
        child = current[part]
        assert isinstance(child, dict)
        current = child
    current[path[-1]] = replacement


def _patch_live_foundation(monkeypatch, resources) -> None:
    monkeypatch.setattr(aiperf_module, "_results_directory_ready", lambda: True)
    monkeypatch.setattr(
        aiperf_module,
        "_get_foundation_resource",
        lambda kind, name, *, namespace=None: resources.get((kind, name, namespace)),
    )


def test_foundation_cluster_check_accepts_exact_canonical_live_objects(monkeypatch) -> None:
    resources = _live_foundation_objects()
    _patch_live_foundation(monkeypatch, resources)

    status = check_foundation(cluster=True)

    assert status["clusterChecked"] is True


@pytest.mark.parametrize(
    ("identity", "path", "replacement"),
    [
        (
            ("Namespace", "kip-benchmark", None),
            ("metadata", "labels", "pod-security.kubernetes.io/enforce"),
            "privileged",
        ),
        (
            ("ServiceAccount", "kip-benchmark", "kip-benchmark"),
            ("automountServiceAccountToken",),
            True,
        ),
        (
            ("ConfigMap", "kip-benchmark-contract", "kip-benchmark"),
            ("data", "aiperfImage"),
            "untrusted.example/aiperf:latest",
        ),
        (
            ("PersistentVolume", "kip-benchmark-results", None),
            ("spec", "hostPath", "path"),
            "/var/lib/foreign-results",
        ),
        (
            ("PersistentVolume", "kip-benchmark-results", None),
            ("spec", "capacity", "storage"),
            "1Gi",
        ),
        (
            ("PersistentVolume", "kip-benchmark-results", None),
            ("spec", "persistentVolumeReclaimPolicy"),
            "Delete",
        ),
        (
            ("PersistentVolume", "kip-benchmark-results", None),
            ("spec", "storageClassName"),
            "foreign-dynamic",
        ),
        (
            ("PersistentVolume", "kip-benchmark-results", None),
            ("spec", "accessModes"),
            ["ReadWriteMany"],
        ),
        (
            ("PersistentVolume", "kip-benchmark-results", None),
            ("spec", "nodeAffinity"),
            {
                "required": {
                    "nodeSelectorTerms": [
                        {
                            "matchExpressions": [
                                {
                                    "key": "kubernetes.io/hostname",
                                    "operator": "In",
                                    "values": ["foreign-node"],
                                }
                            ]
                        }
                    ]
                }
            },
        ),
        (
            ("PersistentVolumeClaim", "kip-benchmark-results", "kip-benchmark"),
            ("spec", "volumeName"),
            "foreign-results",
        ),
        (
            ("PersistentVolumeClaim", "kip-benchmark-results", "kip-benchmark"),
            ("spec", "resources", "requests", "storage"),
            "1Gi",
        ),
        (
            ("PersistentVolumeClaim", "kip-benchmark-results", "kip-benchmark"),
            ("spec", "storageClassName"),
            "foreign-dynamic",
        ),
        (
            ("PersistentVolumeClaim", "kip-benchmark-results", "kip-benchmark"),
            ("spec", "accessModes"),
            ["ReadWriteMany"],
        ),
        (
            ("PersistentVolumeClaim", "kip-benchmark-results", "kip-benchmark"),
            ("status", "capacity", "storage"),
            "1Gi",
        ),
    ],
)
def test_foundation_cluster_check_rejects_live_contract_drift(
    monkeypatch, identity, path, replacement
) -> None:
    resources = _live_foundation_objects()
    _set_nested(resources[identity], path, replacement)
    _patch_live_foundation(monkeypatch, resources)

    with pytest.raises(BenchError, match="foundation drift"):
        check_foundation(cluster=True)


def test_foundation_rejects_bound_pvc_redirected_to_foreign_pv_before_host_mutation(
    monkeypatch,
) -> None:
    resources = _live_foundation_objects()
    claim = resources[("PersistentVolumeClaim", "kip-benchmark-results", "kip-benchmark")]
    claim["spec"]["volumeName"] = "foreign-results"
    resources[("PersistentVolume", "foreign-results", None)] = {
        "apiVersion": "v1",
        "kind": "PersistentVolume",
        "metadata": {"name": "foreign-results"},
        "spec": {"hostPath": {"path": "/srv/foreign-user-data", "type": "Directory"}},
    }
    _patch_live_foundation(monkeypatch, resources)
    host_mutated = False

    def prepare() -> None:
        nonlocal host_mutated
        host_mutated = True

    monkeypatch.setattr(aiperf_module, "_prepare_results_directory", prepare)

    with pytest.raises(BenchError, match="spec.volumeName"):
        aiperf_module.install_foundation()

    assert host_mutated is False


def test_foundation_rejects_weakened_committed_contract_before_cluster_or_host_mutation(
    monkeypatch,
) -> None:
    manifest = json.loads((ROOT / "benchmarks" / "kubernetes" / "foundation.json").read_text())
    persistent_volume = next(
        item for item in manifest["items"] if item["kind"] == "PersistentVolume"
    )
    persistent_volume["spec"]["hostPath"]["path"] = "/srv/unreviewed-results"
    host_mutated = False

    def prepare() -> None:
        nonlocal host_mutated
        host_mutated = True

    monkeypatch.setattr(aiperf_module, "load_foundation_manifest", lambda: manifest)
    monkeypatch.setattr(aiperf_module, "_prepare_results_directory", prepare)
    monkeypatch.setattr(
        aiperf_module,
        "_get_foundation_resource",
        lambda *_args, **_kwargs: pytest.fail("invalid committed input must not contact cluster"),
    )

    with pytest.raises(BenchError, match="required local security/storage contract"):
        aiperf_module.install_foundation()

    assert host_mutated is False


def test_foundation_checks_all_cluster_collisions_before_host_mutation(monkeypatch) -> None:
    inspected: list[tuple[str, str]] = []
    host_mutated = False
    live_namespace = _live_foundation_objects()[("Namespace", "kip-benchmark", None)]

    def inspect(kind: str, name: str, *, namespace: str | None = None):
        del namespace
        inspected.append((kind, name))
        if kind == "Namespace":
            return live_namespace
        if kind == "ConfigMap":
            return {
                "apiVersion": "v1",
                "kind": "ConfigMap",
                "metadata": {"name": name, "labels": {}},
            }
        return None

    def prepare() -> None:
        nonlocal host_mutated
        host_mutated = True

    monkeypatch.setattr(aiperf_module, "_get_foundation_resource", inspect)
    monkeypatch.setattr(aiperf_module, "_prepare_results_directory", prepare)

    with pytest.raises(BenchError, match="unmanaged ConfigMap"):
        aiperf_module.install_foundation()

    assert inspected == [
        ("Namespace", "kip-benchmark"),
        ("ServiceAccount", "kip-benchmark"),
        ("ConfigMap", "kip-benchmark-contract"),
    ]
    assert host_mutated is False


def test_foundation_refuses_unmarked_existing_host_directory(tmp_path: Path, monkeypatch) -> None:
    results = tmp_path / "benchmark-results"
    results.mkdir()
    results.chmod(0o770)
    (results / "user-data.txt").write_text("not owned by KIP\n")
    monkeypatch.setattr(aiperf_module, "RESULTS_HOST_PATH", results)

    with pytest.raises(BenchError, match="existing unowned benchmark result path"):
        aiperf_module._prepare_results_directory()

    assert (results / "user-data.txt").is_file()


def test_foundation_accepts_exact_host_ownership_marker(tmp_path: Path, monkeypatch) -> None:
    results = tmp_path / "benchmark-results"
    results.mkdir()
    results.chmod(0o770)
    (results / aiperf_module.RESULTS_OWNERSHIP_MARKER).write_text(
        aiperf_module.RESULTS_OWNERSHIP_CONTENT
    )
    monkeypatch.setattr(aiperf_module, "RESULTS_HOST_PATH", results)
    monkeypatch.setattr(aiperf_module, "RESULTS_UID", results.stat().st_uid)
    monkeypatch.setattr(aiperf_module, "RESULTS_GID", results.stat().st_gid)
    monkeypatch.setattr(
        aiperf_module,
        "_run_command",
        lambda *_args, **_kwargs: pytest.fail("owned rerun must not mutate host storage"),
    )

    aiperf_module._prepare_results_directory()


def test_benchmark_storage_sudo_preflight_is_noninteractive() -> None:
    source = (ROOT / "bench" / "aiperf.py").read_text()
    assert '["sudo", "-n", "true"]' in source
    assert '["sudo", "-v"]' not in source


def test_report_generation_is_complete_deterministic_and_sanitized(
    cost_profile, tmp_path: Path
) -> None:
    data = normalized_report_data(cost_profile)
    first = generate_report(
        data, run_id="baseline-fixture", cost_profile=cost_profile, output=tmp_path / "first"
    )
    second = generate_report(
        data, run_id="baseline-fixture", cost_profile=cost_profile, output=tmp_path / "second"
    )

    expected_files = {
        "index.html",
        "summary.json",
        "summary.csv",
        "reproducibility.json",
        "charts/latency.svg",
        "charts/throughput.svg",
        "charts/server-token-rates.svg",
        "charts/server-latency.svg",
        "charts/cost.svg",
        "charts/gpu.svg",
        "charts/memory.svg",
        "charts/power-temperature.svg",
        "charts/gpu-pcie.svg",
        "charts/gpu-clocks.svg",
        "charts/gpu-health.svg",
        "charts/runtime-pressure.svg",
        "charts/energy.svg",
        "charts/frontier.svg",
    }
    first_files = {str(path.relative_to(first)) for path in first.rglob("*") if path.is_file()}
    assert first_files == expected_files
    for relative in expected_files:
        assert (first / relative).read_bytes() == (second / relative).read_bytes()
    summary = json.loads((first / "summary.json").read_text())
    assert summary["cost"]["experimentAllocatedUsd"] == 1.2
    assert summary["cost"]["measuredAllocatedUsd"] == pytest.approx(1.0)
    assert len(summary["concurrencyResults"]) == 5
    assert len(summary["trialResults"]) == 25
    assert summary["trialResults"][0]["concurrency"] == 1
    assert summary["trialResults"][0]["trial"] == 1
    assert summary["trialResults"][0]["allocatedCostUsd"] == pytest.approx(0.04)
    assert summary["trialResults"][0]["usageCostUsd"] == pytest.approx(0.03)
    assert summary["trialResults"][0]["allocatedCostPerRequestUsd"] == pytest.approx(0.04 / 999)
    assert summary["trialResults"][0]["usageCostPerRequestUsd"] == pytest.approx(0.03 / 999)
    assert summary["cost"]["measuredAllocatedCostPerSuccessfulRequestUsd"] == pytest.approx(
        1.0 / (999 * 25)
    )
    assert summary["cost"]["measuredUsageCostPerSuccessfulRequestUsd"] == pytest.approx(
        0.75 / (999 * 25)
    )
    first_concurrency = summary["concurrencyResults"][0]
    assert first_concurrency["promptTokenRateAveragePerSecond"] == 512
    assert first_concurrency["generatedTokenRateAveragePerSecond"] == 128
    assert first_concurrency["cachedTokenRateAveragePerSecond"] == 64
    assert first_concurrency["serverTtftP95Ms"] == 30
    assert first_concurrency["serverTpotP95Ms"] == 5
    assert first_concurrency["serverE2eP95Ms"] == 790
    assert first_concurrency["gpuPcieTxAverageMiBPerSecond"] == 100
    assert first_concurrency["gpuPcieRxAverageMiBPerSecond"] == 50
    assert first_concurrency["gpuSmClockAverageMhz"] == 1410
    assert first_concurrency["gpuMemoryClockAverageMhz"] == 6251
    assert first_concurrency["gpuClockThrottleMaxMask"] == 0
    assert first_concurrency["gpuXidMaxCode"] == 0
    assert first_concurrency["allocatedCostPerRequestUsd"] == pytest.approx(0.2 / (999 * 5))
    assert first_concurrency["usageCostPerRequestUsd"] == pytest.approx(0.15 / (999 * 5))
    for metric in (
        "ttftP95Ms",
        "ttftP99Ms",
        "itlP95Ms",
        "itlP99Ms",
        "e2eP95Ms",
        "e2eP99Ms",
        "requestThroughputPerSecond",
        "outputTokenThroughputPerSecond",
    ):
        assert f"{metric}Ci95" in first_concurrency
        assert f"{metric}Cv" in first_concurrency
    reproducibility = json.loads((first / "reproducibility.json").read_text())
    assert reproducibility["aiperfVersion"] == AIPERF_VERSION
    assert reproducibility["aiperfImage"] == AIPERF_IMAGE
    assert reproducibility["componentVersions"] == component_versions()
    assert reproducibility["suite"]["manifest"] == load_suite("baseline").data
    assert summary["suite"]["manifest"]["workload"]["inputTokens"] == 512
    assert summary["suite"]["manifest"]["workload"]["outputTokens"] == 128
    index = (first / "index.html").read_text()
    assert "var-model_name=qwen2.5-7b-instruct" in index
    assert "var-run_id=" not in index
    assert "Trial variability (95% confidence)" in index
    assert "Per-trial cost accounting" in index
    assert "Streaming evidence boundary" in index
    assert "Suite and workload manifest" in index
    assert "sampleIntervalMilliseconds" in index
    assert "cannot prove terminal" in index
    assert "ITL is not labeled as server TPOT" in index
    assert "Allocated USD/request" in index
    assert "Usage USD/request" in index
    assert "Prometheus server token rates" in index
    assert "Server TTFT P95 ms" in index
    assert "Throttle mask max" in index
    published = "\n".join(path.read_text() for path in first.rglob("*") if path.is_file())
    assert "rawPrompt" not in published
    assert "163.192.48.81" not in published


def test_report_rebuild_atomically_replaces_only_the_same_run(cost_profile, tmp_path: Path) -> None:
    data = normalized_report_data(cost_profile)
    output = tmp_path / "baseline-fixture"
    generate_report(data, run_id="baseline-fixture", cost_profile=cost_profile, output=output)

    generate_report(
        data,
        run_id="baseline-fixture",
        cost_profile=cost_profile,
        output=output,
        replace=True,
    )

    assert (output / "index.html").is_file()
    assert not (tmp_path / ".baseline-fixture.previous").exists()


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("sensitive", "prohibited sensitive field"),
        ("ip", "contains an IP address"),
        ("reconciliation", "does not reconcile"),
        ("streaming", "streaming timing evidence"),
        ("missing-trial", "trials do not match"),
        ("missing-metrics", "Prometheus series are incomplete"),
        ("missing-telemetry", "trial.telemetry fields mismatch"),
        ("missing-inference-cost", "inference token attribution must be positive"),
        ("suite-manifest", "suite.manifest does not match"),
    ],
)
def test_report_boundary_rejects_invalid_or_unsafe_publication(
    cost_profile, mutation: str, message: str
) -> None:
    data = copy.deepcopy(normalized_report_data(cost_profile))
    if mutation == "sensitive":
        data["rawPrompt"] = "do not publish me"
    elif mutation == "ip":
        data["run"]["gpuModel"] = "node 163.192.48.81"
    elif mutation == "reconciliation":
        data["opencost"]["allocatedUsd"] = 0.1
    elif mutation == "streaming":
        data["trials"][0]["metrics"]["streamingTimingEvidence"] = False
    elif mutation == "missing-trial":
        data["trials"].pop()
    elif mutation == "missing-inference-cost":
        data["opencost"]["inputUsd"] = 0
    elif mutation == "missing-telemetry":
        data["trials"][0]["telemetry"].pop("gpuXidMaxCode")
    elif mutation == "suite-manifest":
        data["suite"]["manifest"]["workload"]["inputTokens"] = 1024
    else:
        data["prometheus"]["seriesComplete"] = False

    with pytest.raises(ValidationError, match=message):
        validate_normalized_input(data, run_id="baseline-fixture", cost_profile=cost_profile)


def test_clean_removes_only_expired_work_and_never_published_reports(tmp_path: Path) -> None:
    work = tmp_path / "work"
    reports = tmp_path / "reports"
    expired = work / "expired-run"
    published = reports / "expired-run"
    expired.mkdir(parents=True)
    published.mkdir(parents=True)
    (published / "index.html").write_text("published")
    environment = os.environ.copy()
    environment["KIP_BENCH_WORK_ROOT"] = str(work)
    environment["KIP_BENCH_REPORTS_ROOT"] = str(reports)

    result = run_cli("clean", "--retain-days", "0", environment=environment)

    assert result.returncode == 0, result.stderr
    assert not expired.exists()
    assert (published / "index.html").read_text() == "published"


def test_clean_keeps_local_pruning_separate_and_refuses_unowned_node_path(
    tmp_path: Path, monkeypatch
) -> None:
    work = tmp_path / "work"
    local_run = work / "expired-run"
    local_run.mkdir(parents=True)
    node = tmp_path / "node-results"
    node_run = node / "expired-run"
    node_run.mkdir(parents=True)
    (node_run / "raw.json").write_text("private\n")
    monkeypatch.setattr(bench_cli, "work_root", lambda: work)
    monkeypatch.setattr(bench_cli, "RESULTS_HOST_PATH", node)
    monkeypatch.setattr(bench_cli, "_results_directory_owned", lambda: False)

    with pytest.raises(BenchError, match="unowned benchmark result path"):
        bench_cli._clean(SimpleNamespace(retain_days=0, dry_run=False))

    assert not local_run.exists()
    assert (node_run / "raw.json").read_text() == "private\n"
