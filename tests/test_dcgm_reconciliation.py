"""Ownership and idempotence checks for the GPU-to-observability handoff."""

import json
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
DCGM_LIBRARY = ROOT / "deploy" / "observability" / "lib" / "dcgm.sh"


def run_reconciliation(
    tmp_path: Path, *, initial: str, safe: bool = True, mode: str = "operator"
) -> tuple[subprocess.CompletedProcess[str], str, str]:
    state = tmp_path / "dcgm-state"
    config_state = tmp_path / "config-state"
    mutations = tmp_path / "mutations"
    state.write_text(initial)
    if initial == "true":
        config_state.write_text("exact")
    script = f'''
set -euo pipefail
source "{DCGM_LIBRARY}"
kip_gpu_release_exists() {{ return 0; }}
kip_gpu_recorded_mode() {{ printf '%s' "$KIP_TEST_MODE"; }}
kip_gpu_validate_driver_mode() {{
    case "$1" in operator|preinstalled) return 0 ;; *) return 1 ;; esac
}}
kip_gpu_release_is_exact() {{ [ "$KIP_TEST_SAFE" = 1 ]; }}
kip_gpu_dcgm_exporter_state() {{ command cat "$KIP_TEST_STATE"; }}
kip_gpu_reconcile_operator() {{
    printf '%s\n' "$KIP_GPU_DRIVER_MODE:$1" >> "$KIP_TEST_MUTATIONS"
    printf '%s' "$1" > "$KIP_TEST_STATE"
}}
kip_gpu_release_is_exact_for_dcgm_state() {{
    [ "$KIP_TEST_SAFE" = 1 ] && [ "$(cat "$KIP_TEST_STATE")" = "$1" ]
}}
kip_observability_dcgm_config_safe() {{ [ "$KIP_TEST_SAFE" = 1 ]; }}
kip_observability_apply_dcgm_config() {{
    printf '%s\n' config >> "$KIP_TEST_MUTATIONS"
    printf exact > "$KIP_TEST_CONFIG_STATE"
}}
kip_observability_dcgm_config_exact() {{
    [ "$KIP_TEST_SAFE" = 1 ] && [ -e "$KIP_TEST_CONFIG_STATE" ]
}}
kip_observability_dcgm_live_state_safe() {{ [ "$KIP_TEST_SAFE" = 1 ]; }}
kip_observability_dcgm_live_state_exact() {{
    [ "$KIP_TEST_SAFE" = 1 ] && [ "$(cat "$KIP_TEST_STATE")" = true ]
}}
kip_observability_wait_for_dcgm_rollout() {{ return 0; }}
kip_observability_reconcile_dcgm_exporter
printf '%s' "$KIP_GPU_DRIVER_MODE"
'''
    completed = subprocess.run(
        ["/bin/bash", "-c", script],
        cwd=ROOT,
        env={
            **os.environ,
            "KIP_TEST_MODE": mode,
            "KIP_TEST_MUTATIONS": str(mutations),
            "KIP_TEST_CONFIG_STATE": str(config_state),
            "KIP_TEST_SAFE": "1" if safe else "0",
            "KIP_TEST_STATE": str(state),
        },
        check=False,
        capture_output=True,
        text=True,
    )
    mutation_log = mutations.read_text() if mutations.exists() else ""
    return completed, state.read_text(), mutation_log


def live_cluster_policy(*, image: str = "dcgm-exporter") -> dict[str, object]:
    return {
        "metadata": {
            "uid": "cluster-policy-uid",
            "labels": {"app.kubernetes.io/managed-by": "Helm"},
            "annotations": {
                "meta.helm.sh/release-name": "gpu-operator",
                "meta.helm.sh/release-namespace": "gpu-operator",
            },
        },
        "spec": {
            "dcgmExporter": {
                "enabled": True,
                "repository": "nvcr.io/nvidia/k8s",
                "image": image,
                "version": (
                    "4.6.0-4.8.3-distroless@sha256:"
                    "b4df763de9558e5b3f1f1d79bc65b772fcf65b8a9c3664ea7173e47153112b4a"
                ),
                "serviceMonitor": {"enabled": False},
                "config": {"name": "kip-dcgm-metrics"},
                "env": [
                    {"name": "DCGM_EXPORTER_INTERVAL", "value": "1000"},
                    {
                        "name": "DCGM_EXPORTER_COLLECTORS",
                        "value": "/etc/dcgm-exporter/dcgm-metrics.csv",
                    },
                ],
            }
        },
    }


def live_daemonsets(*, image: str | None = None, owned: bool = True) -> dict[str, object]:
    image = image or (
        "nvcr.io/nvidia/k8s/dcgm-exporter:4.6.0-4.8.3-distroless@sha256:"
        "b4df763de9558e5b3f1f1d79bc65b772fcf65b8a9c3664ea7173e47153112b4a"
    )
    owner_references = []
    if owned:
        owner_references.append(
            {
                "apiVersion": "nvidia.com/v1",
                "kind": "ClusterPolicy",
                "name": "cluster-policy",
                "controller": True,
                "uid": "cluster-policy-uid",
            }
        )
    return {
        "items": [
            {
                "metadata": {
                    "name": "nvidia-dcgm-exporter",
                    "labels": {"app": "nvidia-dcgm-exporter"},
                    "ownerReferences": owner_references,
                },
                "spec": {
                    "template": {
                        "spec": {
                            "containers": [
                                {
                                    "name": "nvidia-dcgm-exporter",
                                    "image": image,
                                    "env": [
                                        {
                                            "name": "DCGM_EXPORTER_INTERVAL",
                                            "value": "1000",
                                        },
                                        {
                                            "name": "DCGM_EXPORTER_COLLECTORS",
                                            "value": ("/etc/dcgm-exporter/dcgm-metrics.csv"),
                                        },
                                    ],
                                    "volumeMounts": [
                                        {
                                            "name": "metrics",
                                            "mountPath": "/etc/dcgm-exporter",
                                        }
                                    ],
                                }
                            ],
                            "volumes": [
                                {
                                    "name": "metrics",
                                    "configMap": {"name": "kip-dcgm-metrics"},
                                }
                            ],
                        }
                    }
                },
            }
        ]
    }


def run_live_check(
    tmp_path: Path,
    cluster_policy: dict[str, object],
    daemonsets: dict[str, object],
    function: str,
) -> subprocess.CompletedProcess[str]:
    cluster_policy_file = tmp_path / "cluster-policy.json"
    daemonsets_file = tmp_path / "daemonsets.json"
    cluster_policy_file.write_text(json.dumps(cluster_policy))
    daemonsets_file.write_text(json.dumps(daemonsets))
    script = f'''
set -euo pipefail
source "{ROOT / "deploy" / "gpu" / "versions.env"}"
KIP_GPU_NAMESPACE=gpu-operator
KIP_GPU_RELEASE=gpu-operator
kip_exec() {{ command "$@"; }}
kip_kubectl() {{
    case "$*" in
        *'get clusterpolicy cluster-policy'*) command cat "$KIP_TEST_POLICY" ;;
        *'get daemonsets -o json'*) command cat "$KIP_TEST_DAEMONSETS" ;;
        *) return 1 ;;
    esac
}}
source "{DCGM_LIBRARY}"
{function}
'''
    return subprocess.run(
        ["/bin/bash", "-c", script],
        cwd=ROOT,
        env={
            **os.environ,
            "KIP_TEST_POLICY": str(cluster_policy_file),
            "KIP_TEST_DAEMONSETS": str(daemonsets_file),
        },
        check=False,
        capture_output=True,
        text=True,
    )


@pytest.mark.parametrize("mode", ["operator", "preinstalled"])
def test_disabled_exporter_is_enabled_without_changing_driver_mode(
    tmp_path: Path, mode: str
) -> None:
    completed, state, mutations = run_reconciliation(tmp_path, initial="false", mode=mode)

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout == mode
    assert state == "true"
    assert mutations == f"config\n{mode}:true\n"


def test_exact_enabled_exporter_rerun_is_non_mutating(tmp_path: Path) -> None:
    completed, state, mutations = run_reconciliation(tmp_path, initial="true")

    assert completed.returncode == 0, completed.stderr
    assert state == "true"
    assert mutations == ""


def test_unmanaged_or_drifted_operator_is_refused_before_mutation(tmp_path: Path) -> None:
    completed, state, mutations = run_reconciliation(tmp_path, initial="false", safe=False)

    assert completed.returncode == 1
    assert state == "false"
    assert mutations == ""


def test_live_operator_resources_match_all_pinned_fields(tmp_path: Path) -> None:
    completed = run_live_check(
        tmp_path,
        live_cluster_policy(),
        live_daemonsets(),
        "kip_observability_dcgm_live_state_exact",
    )

    assert completed.returncode == 0, completed.stderr


@pytest.mark.parametrize(
    ("cluster_policy", "daemonsets"),
    [
        (live_cluster_policy(image="unexpected-exporter"), live_daemonsets()),
        (
            live_cluster_policy(),
            live_daemonsets(image="registry.example/unpinned:latest"),
        ),
    ],
)
def test_live_image_drift_is_detected(
    tmp_path: Path,
    cluster_policy: dict[str, object],
    daemonsets: dict[str, object],
) -> None:
    completed = run_live_check(
        tmp_path,
        cluster_policy,
        daemonsets,
        "kip_observability_dcgm_live_state_exact",
    )

    assert completed.returncode == 1


@pytest.mark.parametrize("drift", ["policy-env", "daemonset-env", "config-mount"])
def test_live_environment_and_config_mount_drift_is_detected(tmp_path: Path, drift: str) -> None:
    cluster_policy = live_cluster_policy()
    daemonsets = live_daemonsets()
    if drift == "policy-env":
        cluster_policy["spec"]["dcgmExporter"]["env"][0]["value"] = "5000"
    elif drift == "daemonset-env":
        daemonsets["items"][0]["spec"]["template"]["spec"]["containers"][0]["env"][0]["value"] = (
            "5000"
        )
    else:
        daemonsets["items"][0]["spec"]["template"]["spec"]["volumes"][0]["configMap"]["name"] = (
            "unmanaged-metrics"
        )
    completed = run_live_check(
        tmp_path,
        cluster_policy,
        daemonsets,
        "kip_observability_dcgm_live_state_exact",
    )

    assert completed.returncode == 1


def test_unowned_exporter_daemonset_is_not_safe_to_reconcile(tmp_path: Path) -> None:
    completed = run_live_check(
        tmp_path,
        live_cluster_policy(),
        live_daemonsets(owned=False),
        "kip_observability_dcgm_live_state_safe",
    )

    assert completed.returncode == 1


def test_duplicate_exporter_daemonsets_are_not_safe_to_reconcile(tmp_path: Path) -> None:
    daemonsets = live_daemonsets()
    duplicate = json.loads(json.dumps(daemonsets["items"][0]))
    duplicate["metadata"]["name"] = "unexpected-dcgm-exporter"
    daemonsets["items"].append(duplicate)
    completed = run_live_check(
        tmp_path,
        live_cluster_policy(),
        daemonsets,
        "kip_observability_dcgm_live_state_safe",
    )

    assert completed.returncode == 1


def test_enabled_live_drift_is_reconciled_through_gpu_operator(tmp_path: Path) -> None:
    state = tmp_path / "state"
    mutations = tmp_path / "mutations"
    state.write_text("drifted")
    script = f'''
set -euo pipefail
source "{DCGM_LIBRARY}"
kip_observability_gpu_operator_safe() {{ return 0; }}
kip_observability_recover_dcgm_config() {{ return 0; }}
kip_observability_dcgm_live_state_safe() {{ return 0; }}
kip_gpu_dcgm_exporter_state() {{ printf true; }}
kip_observability_dcgm_live_state_exact() {{
    [ "$(cat "$KIP_TEST_STATE")" = exact ]
}}
kip_gpu_reconcile_operator() {{
    printf '%s\n' "$1" >> "$KIP_TEST_MUTATIONS"
    printf exact > "$KIP_TEST_STATE"
}}
kip_gpu_release_is_exact_for_dcgm_state() {{ [ "$1" = true ]; }}
kip_observability_dcgm_config_exact() {{ return 0; }}
kip_observability_wait_for_dcgm_rollout() {{ return 0; }}
kip_observability_reconcile_dcgm_exporter
'''
    completed = subprocess.run(
        ["/bin/bash", "-c", script],
        cwd=ROOT,
        env={
            **os.environ,
            "KIP_TEST_STATE": str(state),
            "KIP_TEST_MUTATIONS": str(mutations),
        },
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert state.read_text() == "exact"
    assert mutations.read_text() == "true\n"


def test_missing_config_is_restored_but_drift_is_refused(tmp_path: Path) -> None:
    state = tmp_path / "config-state"
    mutations = tmp_path / "mutations"
    script = f'''
set -euo pipefail
source "{DCGM_LIBRARY}"
kip_observability_dcgm_config_exact() {{ [ -e "$KIP_TEST_STATE" ]; }}
kip_observability_dcgm_config_safe() {{ [ "$KIP_TEST_SAFE" = 1 ]; }}
kip_observability_apply_dcgm_config() {{
    printf applied >> "$KIP_TEST_MUTATIONS"
    touch "$KIP_TEST_STATE"
}}
kip_observability_recover_dcgm_config
'''
    env = {
        **os.environ,
        "KIP_TEST_STATE": str(state),
        "KIP_TEST_MUTATIONS": str(mutations),
        "KIP_TEST_SAFE": "1",
    }
    restored = subprocess.run(
        ["/bin/bash", "-c", script],
        cwd=ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert restored.returncode == 0, restored.stderr
    assert mutations.read_text() == "applied"

    state.unlink()
    mutations.unlink()
    env["KIP_TEST_SAFE"] = "0"
    refused = subprocess.run(
        ["/bin/bash", "-c", script],
        cwd=ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert refused.returncode == 1
    assert not mutations.exists()


def test_preflight_recovery_repairs_enabled_live_drift_before_gpu_check(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    mutations = tmp_path / "mutations"
    state.write_text("drifted")
    script = f'''
set -euo pipefail
source "{DCGM_LIBRARY}"
kip_gpu_release_exists() {{ return 0; }}
kip_gpu_recorded_mode() {{ printf operator; }}
kip_gpu_release_is_exact() {{ [ "$KIP_GPU_DRIVER_MODE" = operator ]; }}
kip_observability_recover_dcgm_config() {{ return 0; }}
kip_gpu_dcgm_exporter_state() {{ printf true; }}
kip_observability_dcgm_live_state_safe() {{ return 0; }}
kip_observability_dcgm_live_state_exact() {{
    [ "$(cat "$KIP_TEST_STATE")" = exact ]
}}
kip_gpu_reconcile_operator() {{
    printf '%s\n' "$KIP_GPU_DRIVER_MODE:$1" >> "$KIP_TEST_MUTATIONS"
    printf exact > "$KIP_TEST_STATE"
}}
kip_observability_wait_for_dcgm_rollout() {{
    printf rollout >> "$KIP_TEST_MUTATIONS"
}}
kip_observability_recover_gpu_operator
'''
    completed = subprocess.run(
        ["/bin/bash", "-c", script],
        cwd=ROOT,
        env={
            **os.environ,
            "KIP_TEST_STATE": str(state),
            "KIP_TEST_MUTATIONS": str(mutations),
        },
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert state.read_text() == "exact"
    assert mutations.read_text() == "operator:true\nrollout"


def test_reconciliation_uses_operator_owned_exact_pins() -> None:
    gpu_values = (ROOT / "deploy" / "gpu" / "values.yaml").read_text()
    operator = (ROOT / "deploy" / "gpu" / "lib" / "operator.sh").read_text()
    install = (ROOT / "deploy" / "observability" / "install.sh").read_text()

    assert "dcgmExporter:\n" in gpu_values
    assert "  enabled: false" in gpu_values
    assert "dcgmExporter.enabled=$dcgm_enabled" in operator
    assert "dcgmExporter.serviceMonitor.enabled=false" in operator
    assert "dcgmExporter.env[0].value=1000" in operator
    assert "dcgmExporter.env[1].name=DCGM_EXPORTER_COLLECTORS" in operator
    assert "dcgmExporter.config.name=$KIP_DCGM_METRICS_CONFIG_NAME" in operator
    assert (
        "DCGM_FI_DEV_CLOCKS_EVENT_REASONS"
        in (ROOT / "deploy" / "observability" / "assets" / "dcgm-metrics.csv").read_text()
    )
    assert (
        "DCGM_FI_PROF_PCIE_TX_BYTES"
        in (ROOT / "deploy" / "observability" / "assets" / "dcgm-metrics.csv").read_text()
    )
    assert "kip.dcgmExporterEnabled=$dcgm_enabled" in operator
    assert install.index("kip_observability_recover_gpu_operator") < install.index(
        "kip_observability_platform_ready"
    )
    assert "kip_observability_reconcile_dcgm_exporter" in install
    assert install.index("kip_observability_reconcile_dcgm_exporter") < install.index(
        "manifests/service-monitors.yaml"
    )
