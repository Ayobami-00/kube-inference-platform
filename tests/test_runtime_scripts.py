"""Deterministic subprocess tests for the pluggable runtime layer."""

# ruff: noqa: E501

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
DISPATCH_SCRIPT = ROOT / "deploy" / "runtime" / "dispatch.sh"
DEPLOY_SCRIPT = ROOT / "deploy" / "runtime" / "vllm" / "deploy.sh"
VALIDATE_SCRIPT = ROOT / "deploy" / "runtime" / "vllm" / "validate.sh"
SMOKE_SCRIPT = ROOT / "deploy" / "runtime" / "vllm" / "smoke.sh"
PROFILE = ROOT / "deploy" / "runtime" / "vllm" / "profiles" / "qwen2.5-7b-instruct.json"
IMAGE_DIGEST = "sha256:3e10e8189823e0f7ae4620c271bcdaaf64127ec7d0edc351591a508498b7684a"
MODEL_REVISION = "a09a35458c702b33eeacc393d103063234e8bc28"


def write_command(command_dir: Path, name: str, body: str) -> None:
    command = command_dir / name
    command.write_text(f"#!/bin/sh\n{body}\n")
    command.chmod(0o755)


def profile_hash() -> str:
    canonical = json.dumps(json.loads(PROFILE.read_text()), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(f"{canonical}\n".encode()).hexdigest()


@pytest.fixture
def runtime_fixture(tmp_path: Path) -> dict[str, Path]:
    command_dir = tmp_path / "commands"
    command_dir.mkdir()
    command_log = tmp_path / "commands.log"
    namespace_state = tmp_path / "namespace"
    deployment_state = tmp_path / "deployment"
    service_state = tmp_path / "service"
    smoke_state = tmp_path / "smoke"
    config_state = tmp_path / "smoke-config"
    var_root = tmp_path / "var"
    (var_root / "lib" / "containerd").mkdir(parents=True)
    kubeconfig = tmp_path / "admin.conf"
    kubeconfig.write_text("fixture\n")
    cluster_check = tmp_path / "cluster-check"
    gpu_check = tmp_path / "gpu-check"
    for script, marker in ((cluster_check, "cluster"), (gpu_check, "gpu")):
        script.write_text(
            "#!/bin/sh\n"
            f"printf '{marker}-check\\n' >> \"$KIP_TEST_COMMAND_LOG\"\n"
            f'[ "${{KIP_TEST_{marker.upper()}_FAIL:-0}}" != 1 ]\n'
        )
        script.chmod(0o755)

    jq_path = shutil.which("jq")
    sha_path = shutil.which("sha256sum") or shutil.which("shasum")
    assert jq_path is not None
    assert sha_path is not None
    write_command(command_dir, "jq", f'exec "{jq_path}" "$@"')
    if Path(sha_path).name == "shasum":
        write_command(command_dir, "sha256sum", f'exec "{sha_path}" -a 256 "$@"')
    else:
        write_command(command_dir, "sha256sum", f'exec "{sha_path}" "$@"')
    write_command(
        command_dir,
        "df",
        "printf '%s\\n' 'Filesystem 1024-blocks Used Available Capacity Mounted on' "
        "'/dev/test 200000000 1 199999999 1% /'",
    )
    write_command(
        command_dir,
        "sudo",
        """printf 'sudo %s\\n' "$*" >> "$KIP_TEST_COMMAND_LOG"
case "$*" in '-n true') exit 0 ;; esac
command_name=$1
shift
if [ -x "$KIP_TEST_COMMAND_DIR/$command_name" ]; then
    exec "$KIP_TEST_COMMAND_DIR/$command_name" "$@"
fi
exec "$command_name" "$@""",
    )
    write_command(
        command_dir,
        "curl",
        """printf 'curl %s\\n' "$*" >> "$KIP_TEST_COMMAND_LOG"
case "$*" in
    *'/v1/models') printf '%s\\n' '{"data":[{"id":"qwen2.5-7b-instruct"}]}' ;;
    *'/health') printf '%s' '' ;;
    *) exit 1 ;;
esac""",
    )
    write_command(
        command_dir,
        "kubectl",
        r"""args="$*"
printf 'kubectl %s\n' "$args" >> "$KIP_TEST_COMMAND_LOG"
case "$args" in
    *'get nodes -o json')
        printf '%s\n' '{"items":[{"metadata":{"name":"gpu-node-1"},"spec":{"unschedulable":false},"status":{"conditions":[{"type":"Ready","status":"True"}],"allocatable":{"nvidia.com/gpu":"1"}}}]}'
        ;;
    *'get node gpu-node-1 -o json')
        printf '%s\n' '{"status":{"allocatable":{"nvidia.com/gpu":"1"}}}' ;;
    *'get namespace kip-runtime -o jsonpath={.metadata.name}'*)
        [ -e "$KIP_TEST_NAMESPACE_STATE" ] && printf '%s' kip-runtime || exit 1 ;;
    *'get namespace kip-runtime -o jsonpath={.metadata.labels.app\.kubernetes\.io/managed-by}'*)
        [ -e "$KIP_TEST_NAMESPACE_STATE" ] && printf '%s' "${KIP_TEST_NAMESPACE_OWNER:-kube-inference-platform}" || exit 1 ;;
    *'get namespace kip-runtime -o jsonpath={.metadata.annotations.kube-inference-platform/profile-sha256}'*)
        [ -e "$KIP_TEST_NAMESPACE_STATE" ] && printf '%s' "${KIP_TEST_RECORDED_PROFILE_HASH:-$KIP_TEST_PROFILE_HASH}" || exit 1 ;;
    *'get namespace kip-runtime -o jsonpath={.metadata.annotations.kube-inference-platform/profile}'*)
        [ -e "$KIP_TEST_NAMESPACE_STATE" ] && printf '%s' qwen2.5-7b-instruct || exit 1 ;;
    *'get namespace kip-runtime'*) [ -e "$KIP_TEST_NAMESPACE_STATE" ] ;;
    *'get deployment,service -o json')
        if [ "${KIP_TEST_EXTRA_RUNTIME:-0}" = 1 ]; then
            printf '%s\n' '{"items":[{"kind":"Deployment","metadata":{"name":"unmanaged-runtime","labels":{}}}]}'
        else
            printf '%s\n' '{"items":[{"kind":"Deployment","metadata":{"name":"vllm-qwen25-7b","labels":{"app.kubernetes.io/managed-by":"kube-inference-platform"}}},{"kind":"Service","metadata":{"name":"vllm-qwen25-7b","labels":{"app.kubernetes.io/managed-by":"kube-inference-platform"}}}]}'
        fi
        ;;
    *'get deployment vllm-qwen25-7b -o jsonpath={.metadata.name}'*)
        [ -e "$KIP_TEST_DEPLOYMENT_STATE" ] && printf '%s' vllm-qwen25-7b || exit 1 ;;
    *'get service vllm-qwen25-7b -o jsonpath={.metadata.name}'*)
        [ -e "$KIP_TEST_SERVICE_STATE" ] && printf '%s' vllm-qwen25-7b || exit 1 ;;
    *'get deployment vllm-qwen25-7b -o jsonpath={.metadata.labels.app\.kubernetes\.io/managed-by}'*|*'get service vllm-qwen25-7b -o jsonpath={.metadata.labels.app\.kubernetes\.io/managed-by}'*)
        printf '%s' "${KIP_TEST_RESOURCE_OWNER:-kube-inference-platform}" ;;
    *'get deployment vllm-qwen25-7b -o jsonpath={.metadata.annotations.kube-inference-platform/profile-sha256}'*|*'get service vllm-qwen25-7b -o jsonpath={.metadata.annotations.kube-inference-platform/profile-sha256}'*)
        printf '%s' "${KIP_TEST_RECORDED_PROFILE_HASH:-$KIP_TEST_PROFILE_HASH}" ;;
    *'get deployment vllm-qwen25-7b -o jsonpath={.metadata.annotations.kube-inference-platform/profile}'*|*'get service vllm-qwen25-7b -o jsonpath={.metadata.annotations.kube-inference-platform/profile}'*)
        printf '%s' qwen2.5-7b-instruct ;;
    *'get deployment vllm-qwen25-7b -o jsonpath={.metadata.annotations.kube-inference-platform/runtime-version}'*|*'get service vllm-qwen25-7b -o jsonpath={.metadata.annotations.kube-inference-platform/runtime-version}'*)
        printf '%s' v0.29.0 ;;
    *'get deployment vllm-qwen25-7b -o jsonpath={.metadata.annotations.kube-inference-platform/model-revision}'*|*'get service vllm-qwen25-7b -o jsonpath={.metadata.annotations.kube-inference-platform/model-revision}'*)
        printf '%s' "$KIP_TEST_MODEL_REVISION" ;;
    *'get deployment vllm-qwen25-7b -o jsonpath={.metadata.annotations.kube-inference-platform/image-digest}'*|*'get service vllm-qwen25-7b -o jsonpath={.metadata.annotations.kube-inference-platform/image-digest}'*)
        printf '%s' "$KIP_TEST_IMAGE_DIGEST" ;;
    *'get deployment vllm-qwen25-7b -o json')
        live_image=${KIP_TEST_LIVE_IMAGE:-vllm/vllm-openai:v0.29.0-x86_64-cu129@$KIP_TEST_IMAGE_DIGEST}
        printf '%s\n' "{\"metadata\":{\"labels\":{\"app.kubernetes.io/managed-by\":\"kube-inference-platform\"},\"annotations\":{\"kube-inference-platform/profile-sha256\":\"$KIP_TEST_PROFILE_HASH\"}},\"spec\":{\"replicas\":1,\"strategy\":{\"type\":\"Recreate\"},\"progressDeadlineSeconds\":2700,\"template\":{\"spec\":{\"enableServiceLinks\":false,\"terminationGracePeriodSeconds\":120,\"securityContext\":{\"seccompProfile\":{\"type\":\"RuntimeDefault\"}},\"containers\":[{\"image\":\"$live_image\",\"command\":[\"vllm\",\"serve\"],\"args\":[\"Qwen/Qwen2.5-7B-Instruct\",\"--revision\",\"$KIP_TEST_MODEL_REVISION\",\"--tokenizer-revision\",\"$KIP_TEST_MODEL_REVISION\",\"--served-model-name\",\"qwen2.5-7b-instruct\",\"--host\",\"0.0.0.0\",\"--port\",\"8000\",\"--dtype\",\"bfloat16\",\"--tensor-parallel-size\",\"1\",\"--max-model-len\",\"8192\",\"--gpu-memory-utilization\",\"0.9\",\"--max-num-seqs\",\"8\",\"--seed\",\"0\",\"--no-enable-prefix-caching\"],\"env\":[{\"name\":\"HF_HOME\",\"value\":\"/var/cache/kip/huggingface\"}],\"resources\":{\"requests\":{\"cpu\":\"4\",\"memory\":\"24Gi\",\"ephemeral-storage\":\"10Gi\",\"nvidia.com/gpu\":\"1\"},\"limits\":{\"cpu\":\"12\",\"memory\":\"48Gi\",\"ephemeral-storage\":\"20Gi\",\"nvidia.com/gpu\":\"1\"}},\"securityContext\":{\"allowPrivilegeEscalation\":false,\"capabilities\":{\"drop\":[\"ALL\"]}},\"startupProbe\":{\"httpGet\":{\"path\":\"/health\"},\"periodSeconds\":10,\"timeoutSeconds\":5,\"failureThreshold\":180},\"readinessProbe\":{\"httpGet\":{\"path\":\"/health\"},\"periodSeconds\":5,\"timeoutSeconds\":3,\"failureThreshold\":3},\"livenessProbe\":{\"httpGet\":{\"path\":\"/health\"},\"periodSeconds\":15,\"timeoutSeconds\":3,\"failureThreshold\":4},\"volumeMounts\":[{\"name\":\"model-cache\",\"mountPath\":\"/var/cache/kip\"},{\"name\":\"shared-memory\",\"mountPath\":\"/dev/shm\"}]}],\"volumes\":[{\"name\":\"model-cache\",\"hostPath\":{\"path\":\"$KIP_VAR_ROOT/lib/kube-inference-platform/model-cache/vllm\",\"type\":\"Directory\"}},{\"name\":\"shared-memory\",\"emptyDir\":{\"medium\":\"Memory\",\"sizeLimit\":\"4Gi\"}}]}}},\"status\":{\"availableReplicas\":1,\"readyReplicas\":1}}"
        ;;
    *'get pods -l '*' -o json')
        printf '%s\n' '{"items":[{"spec":{"nodeName":"gpu-node-1"},"status":{"phase":"Running","conditions":[{"type":"Ready","status":"True"}],"containerStatuses":[{"ready":true,"restartCount":'"${KIP_TEST_RESTARTS:-0}"',"state":{"running":{}}}]}}]}'
        ;;
    *'get service vllm-qwen25-7b -o json')
        printf '%s\n' '{"metadata":{"labels":{"app.kubernetes.io/managed-by":"kube-inference-platform"}},"spec":{"type":"ClusterIP","selector":{"app.kubernetes.io/name":"vllm","app.kubernetes.io/instance":"qwen2.5-7b-instruct"},"ports":[{"port":8000,"targetPort":"http"}]}}'
        ;;
    *'get deployment vllm-qwen25-7b'*) [ -e "$KIP_TEST_DEPLOYMENT_STATE" ] ;;
    *'get service vllm-qwen25-7b'*) [ -e "$KIP_TEST_SERVICE_STATE" ] ;;
    *'get endpointslice -l kubernetes.io/service-name=vllm-qwen25-7b -o json')
        printf '%s\n' '{"items":[{"endpoints":[{"addresses":["10.0.0.2"],"conditions":{"ready":true}}]}]}' ;;
    *'apply -f '*'namespace.yaml'*) touch "$KIP_TEST_NAMESPACE_STATE" ;;
    *'apply -f '*'service.yaml'*) touch "$KIP_TEST_SERVICE_STATE" ;;
    *'apply -f '*'deployment.yaml'*) touch "$KIP_TEST_DEPLOYMENT_STATE" ;;
    *'rollout status deployment/vllm-qwen25-7b'*) [ "${KIP_TEST_ROLLOUT_FAIL:-0}" != 1 ] ;;
    *'create configmap kip-runtime-smoke-client'*) printf '%s\n' 'apiVersion: v1' 'kind: ConfigMap' ;;
    *'apply -f -'*) touch "$KIP_TEST_CONFIG_STATE" ;;
    *'apply -f '*'smoke-pod.yaml'*) touch "$KIP_TEST_SMOKE_STATE" ;;
    *'wait --for=jsonpath={.status.phase}=Succeeded pod/kip-runtime-smoke'*) [ "${KIP_TEST_SMOKE_FAIL:-0}" != 1 ] ;;
    *'logs kip-runtime-smoke --all-containers'*) printf '%s\n' 'diagnostic logs' ;;
    *'logs kip-runtime-smoke'*) printf '%s\n' 'PASS health HTTP 200' 'PASS models qwen2.5-7b-instruct' 'PASS chat completion prompt_tokens=5 completion_tokens=2' 'PASS legacy completion' 'PASS streaming deltas=20 span_ms=800.0' 'RUNTIME_SMOKE_OK' ;;
    *'delete pod kip-runtime-smoke'*) rm -f "$KIP_TEST_SMOKE_STATE" ;;
    *'delete configmap kip-runtime-smoke-client'*) rm -f "$KIP_TEST_CONFIG_STATE" ;;
    *'describe '*|*'get events '*|*'get pods -o wide'*) printf '%s\n' diagnostic ;;
esac""",
    )

    return {
        "command_dir": command_dir,
        "command_log": command_log,
        "namespace_state": namespace_state,
        "deployment_state": deployment_state,
        "service_state": service_state,
        "smoke_state": smoke_state,
        "config_state": config_state,
        "var_root": var_root,
        "kubeconfig": kubeconfig,
        "cluster_check": cluster_check,
        "gpu_check": gpu_check,
    }


def runtime_environment(fixture: dict[str, Path], **overrides: str) -> dict[str, str]:
    environment = os.environ.copy()
    environment.update(
        {
            "KIP_TESTING": "1",
            "KIP_TEST_COMMAND_DIR": str(fixture["command_dir"]),
            "KIP_TEST_COMMAND_LOG": str(fixture["command_log"]),
            "KIP_TEST_NAMESPACE_STATE": str(fixture["namespace_state"]),
            "KIP_TEST_DEPLOYMENT_STATE": str(fixture["deployment_state"]),
            "KIP_TEST_SERVICE_STATE": str(fixture["service_state"]),
            "KIP_TEST_SMOKE_STATE": str(fixture["smoke_state"]),
            "KIP_TEST_CONFIG_STATE": str(fixture["config_state"]),
            "KIP_TEST_PROFILE_HASH": profile_hash(),
            "KIP_TEST_IMAGE_DIGEST": IMAGE_DIGEST,
            "KIP_TEST_MODEL_REVISION": MODEL_REVISION,
            "KIP_TEST_CACHE_FREE_KIB": str(80 * 1024 * 1024),
            "KIP_CLUSTER_CHECK_SCRIPT": str(fixture["cluster_check"]),
            "KIP_GPU_CHECK_SCRIPT": str(fixture["gpu_check"]),
            "KIP_KUBECONFIG": str(fixture["kubeconfig"]),
            "KIP_VAR_ROOT": str(fixture["var_root"]),
            "KIP_TEST_EUID": "0",
            "NO_COLOR": "1",
        }
    )
    environment.update(overrides)
    return environment


def run_runtime_script(
    script: Path, fixture: dict[str, Path], *args: str, **overrides: str
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["/bin/bash", str(script), *args],
        cwd=ROOT,
        env=runtime_environment(fixture, **overrides),
        check=False,
        capture_output=True,
        text=True,
    )


def mark_runtime_installed(fixture: dict[str, Path]) -> None:
    fixture["namespace_state"].touch()
    fixture["deployment_state"].touch()
    fixture["service_state"].touch()


def validate_profile_file(
    profile_path: Path, fixture: dict[str, Path]
) -> subprocess.CompletedProcess[str]:
    script = (
        f'source "{ROOT / "deploy" / "cluster" / "lib" / "common.sh"}"; '
        f'source "{ROOT / "deploy" / "runtime" / "vllm" / "lib" / "common.sh"}"; '
        'kip_runtime_validate_profile_schema "$1"'
    )
    return subprocess.run(
        ["/bin/bash", "-c", script, "profile-validation", str(profile_path)],
        cwd=ROOT,
        env=runtime_environment(fixture),
        check=False,
        capture_output=True,
        text=True,
    )


def test_runtime_artifacts_pin_safe_single_gpu_baseline() -> None:
    versions = (ROOT / "deploy" / "runtime" / "vllm" / "versions.env").read_text()
    deployment = (
        ROOT / "deploy" / "runtime" / "vllm" / "manifests" / "deployment.yaml.tpl"
    ).read_text()
    service = (ROOT / "deploy" / "runtime" / "vllm" / "manifests" / "service.yaml.tpl").read_text()
    smoke_pod = (
        ROOT / "deploy" / "runtime" / "vllm" / "manifests" / "smoke-pod.yaml.tpl"
    ).read_text()
    profile = json.loads(PROFILE.read_text())

    assert 'KIP_VLLM_VERSION="v0.29.0"' in versions
    assert IMAGE_DIGEST in versions
    assert profile["model"]["revision"] == MODEL_REVISION
    assert profile["vllm"]["maxModelLength"] == 8192
    assert profile["vllm"]["gpuMemoryUtilization"] == 0.9
    assert profile["vllm"]["quantization"] is None
    assert "type: Recreate" in deployment
    assert 'nvidia.com/gpu: "__GPU_REQUEST__"' in deployment
    assert "allowPrivilegeEscalation: false" in deployment
    assert 'drop: ["ALL"]' in deployment
    assert "type: RuntimeDefault" in deployment
    assert "enableServiceLinks: false" in deployment
    assert "failureThreshold: 180" in deployment
    assert "sizeLimit: __SHARED_MEMORY_SIZE__" in deployment
    assert "type: Directory" in deployment
    assert "runtimeClassName" not in deployment
    assert "hostNetwork" not in deployment
    assert "--trust-remote-code" not in deployment
    assert "--no-enable-prefix-caching" in deployment
    assert "type: ClusterIP" in service
    assert "NodePort" not in service
    assert "nvidia.com/gpu" not in smoke_pod
    render = (ROOT / "deploy" / "runtime" / "vllm" / "lib" / "render.sh").read_text()
    assert "-v namespace=" not in render


@pytest.mark.parametrize(
    "mutation",
    [
        "unknown-field",
        "multiple-replicas",
        "zero-gpu",
        "quantization",
        "cpu-offload",
        "remote-code",
        "invalid-name",
    ],
)
def test_profile_schema_rejects_unsafe_or_unknown_configuration(
    runtime_fixture: dict[str, Path], tmp_path: Path, mutation: str
) -> None:
    profile = json.loads(PROFILE.read_text())
    if mutation == "unknown-field":
        profile["unexpected"] = True
    elif mutation == "multiple-replicas":
        profile["kubernetes"]["replicas"] = 2
    elif mutation == "zero-gpu":
        profile["kubernetes"]["resources"]["limits"]["gpu"] = 0
    elif mutation == "quantization":
        profile["vllm"]["quantization"] = "awq"
    elif mutation == "cpu-offload":
        profile["vllm"]["cpuOffloadGiB"] = 4
    elif mutation == "remote-code":
        profile["vllm"]["trustRemoteCode"] = True
    else:
        profile["resourceName"] = "INVALID_NAME"
    profile_path = tmp_path / f"{mutation}.json"
    profile_path.write_text(json.dumps(profile))

    result = validate_profile_file(profile_path, runtime_fixture)

    assert result.returncode != 0


def test_dispatch_rejects_unknown_runtime_and_profile(runtime_fixture: dict[str, Path]) -> None:
    unknown_runtime = run_runtime_script(
        DISPATCH_SCRIPT, runtime_fixture, "check", KIP_RUNTIME="../other"
    )
    unknown_profile = run_runtime_script(
        DISPATCH_SCRIPT, runtime_fixture, "check", KIP_MODEL_PROFILE="../../secret"
    )

    assert unknown_runtime.returncode == 2
    assert "RUNTIME must identify" in unknown_runtime.stderr
    assert unknown_profile.returncode == 2
    assert "MODEL_PROFILE must identify" in unknown_profile.stderr


def test_fresh_runtime_deploy_reaches_inference_checkpoint(
    runtime_fixture: dict[str, Path],
) -> None:
    result = run_runtime_script(DEPLOY_SCRIPT, runtime_fixture)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "10 passed" in result.stdout
    assert "Hard checkpoint passed" in result.stdout
    commands = runtime_fixture["command_log"].read_text()
    assert "rollout status deployment/vllm-qwen25-7b --timeout=45m" in commands
    assert "create configmap kip-runtime-smoke-client" in commands
    assert commands.count("delete pod kip-runtime-smoke") >= 2
    assert not runtime_fixture["smoke_state"].exists()
    assert not runtime_fixture["config_state"].exists()


def test_matching_runtime_rerun_reconciles_and_retests(runtime_fixture: dict[str, Path]) -> None:
    mark_runtime_installed(runtime_fixture)

    result = run_runtime_script(DEPLOY_SCRIPT, runtime_fixture)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "matching managed runtime installation" in result.stdout
    commands = runtime_fixture["command_log"].read_text()
    assert "rollout status deployment/vllm-qwen25-7b --timeout=45m" in commands
    assert "create configmap kip-runtime-smoke-client" in commands


def test_partial_managed_runtime_is_reconciled(runtime_fixture: dict[str, Path]) -> None:
    runtime_fixture["namespace_state"].touch()

    result = run_runtime_script(DEPLOY_SCRIPT, runtime_fixture)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "partial managed runtime installation" in result.stdout
    assert runtime_fixture["deployment_state"].exists()
    assert runtime_fixture["service_state"].exists()


def test_runtime_check_is_read_only(runtime_fixture: dict[str, Path]) -> None:
    mark_runtime_installed(runtime_fixture)

    result = run_runtime_script(VALIDATE_SCRIPT, runtime_fixture)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "7 passed" in result.stdout
    commands = runtime_fixture["command_log"].read_text()
    assert "apply -f" not in commands
    assert "delete " not in commands
    assert "/v1/chat/completions" not in commands
    assert "/v1/completions" not in commands


def test_runtime_check_warns_for_healthy_previous_restart(runtime_fixture: dict[str, Path]) -> None:
    mark_runtime_installed(runtime_fixture)

    result = run_runtime_script(VALIDATE_SCRIPT, runtime_fixture, KIP_TEST_RESTARTS="1")

    assert result.returncode == 0
    assert "1 warned" in result.stdout


@pytest.mark.parametrize(
    "overrides",
    [
        {"KIP_TEST_CLUSTER_FAIL": "1"},
        {"KIP_TEST_GPU_FAIL": "1"},
        {"KIP_TEST_CACHE_FREE_KIB": "100"},
    ],
)
def test_runtime_preflight_failures_block_mutation(
    runtime_fixture: dict[str, Path], overrides: dict[str, str]
) -> None:
    result = run_runtime_script(DEPLOY_SCRIPT, runtime_fixture, **overrides)

    assert result.returncode == 1
    commands = runtime_fixture["command_log"].read_text()
    assert "apply -f" not in commands


def test_runtime_drift_is_refused(runtime_fixture: dict[str, Path]) -> None:
    mark_runtime_installed(runtime_fixture)

    result = run_runtime_script(
        DEPLOY_SCRIPT, runtime_fixture, KIP_TEST_RECORDED_PROFILE_HASH="drift"
    )

    assert result.returncode == 1
    assert "drifted" in result.stdout
    assert "apply -f" not in runtime_fixture["command_log"].read_text()


def test_altered_managed_deployment_is_refused(runtime_fixture: dict[str, Path]) -> None:
    mark_runtime_installed(runtime_fixture)

    result = run_runtime_script(
        DEPLOY_SCRIPT,
        runtime_fixture,
        KIP_TEST_LIVE_IMAGE="example.invalid/changed:latest",
    )

    assert result.returncode == 1
    assert "Deployment identity or pinned configuration has drifted" in result.stdout
    assert "apply -f" not in runtime_fixture["command_log"].read_text()


def test_unmanaged_workload_in_runtime_namespace_is_refused(
    runtime_fixture: dict[str, Path],
) -> None:
    mark_runtime_installed(runtime_fixture)

    result = run_runtime_script(
        DEPLOY_SCRIPT,
        runtime_fixture,
        KIP_TEST_EXTRA_RUNTIME="1",
    )

    assert result.returncode == 1
    assert "namespace ownership or profile has drifted" in result.stdout
    assert "apply -f" not in runtime_fixture["command_log"].read_text()


def test_rollout_failure_reports_diagnostics(runtime_fixture: dict[str, Path]) -> None:
    result = run_runtime_script(DEPLOY_SCRIPT, runtime_fixture, KIP_TEST_ROLLOUT_FAIL="1")

    assert result.returncode == 1
    assert "Runtime diagnostics" in result.stderr


def test_runtime_smoke_cleans_up_after_failure(runtime_fixture: dict[str, Path]) -> None:
    mark_runtime_installed(runtime_fixture)

    result = run_runtime_script(SMOKE_SCRIPT, runtime_fixture, KIP_TEST_SMOKE_FAIL="1")

    assert result.returncode == 1
    commands = runtime_fixture["command_log"].read_text()
    assert commands.count("delete pod kip-runtime-smoke") >= 2
    assert not runtime_fixture["smoke_state"].exists()
    assert not runtime_fixture["config_state"].exists()


def test_invalid_make_runtime_values_exit_two(runtime_fixture: dict[str, Path]) -> None:
    result = subprocess.run(
        ["make", "runtime-check", "RUNTIME=unknown"],
        cwd=ROOT,
        env=runtime_environment(runtime_fixture),
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 2
    assert "RUNTIME must identify" in result.stderr

    verbose = subprocess.run(
        ["make", "runtime-check", "VERBOSE=sometimes"],
        cwd=ROOT,
        env=runtime_environment(runtime_fixture),
        check=False,
        capture_output=True,
        text=True,
    )

    assert verbose.returncode == 2
    assert "VERBOSE must be 0 or 1" in verbose.stderr


@pytest.mark.parametrize("preexisting", [False, True])
def test_model_cache_requires_or_creates_exact_marker(tmp_path: Path, preexisting: bool) -> None:
    var_root = tmp_path / "var"
    cache_root = var_root / "lib" / "kube-inference-platform" / "model-cache"
    cache_path = cache_root / "vllm"
    work = tmp_path / "work"
    work.mkdir()
    if preexisting:
        cache_root.mkdir(parents=True)
        (cache_root / "foreign-model.bin").write_text("preserve\n")
    script = f'''
source "{ROOT / "deploy" / "lib" / "ui.sh"}"
source "{ROOT / "deploy" / "cluster" / "lib" / "common.sh"}"
KIP_VAR_ROOT="{var_root}"
KIP_WORK_DIR="{work}"
KIP_RUNTIME_CACHE_PATH="{cache_path}"
source "{ROOT / "deploy" / "runtime" / "vllm" / "lib" / "preflight.sh"}"
KIP_USE_SUDO=0
kip_runtime_cache_storage_ready
'''
    completed = subprocess.run(
        ["/bin/bash", "-c", script], cwd=ROOT, check=False, capture_output=True, text=True
    )
    if preexisting:
        assert completed.returncode == 1
        assert (cache_root / "foreign-model.bin").read_text() == "preserve\n"
        assert not (cache_root / ".kip-managed-v1").exists()
    else:
        assert completed.returncode == 0, completed.stderr
        assert (cache_root / ".kip-managed-v1").read_text() == (
            "kube-inference-platform model cache v1\n"
        )
        assert cache_path.is_dir()
