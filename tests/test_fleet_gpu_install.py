"""Fail-closed fleet GPU and cross-node smoke entry points."""

from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GPU_INSTALL = ROOT / "deploy/fleet/gpu-install.sh"
NETWORK_SMOKE = ROOT / "deploy/fleet/network-smoke.sh"
LARGE_IMAGE_PULL = ROOT / "deploy/fleet/large-image-pull.sh"


def test_invalid_fleet_gpu_count_exits_before_cluster_mutation() -> None:
    result = subprocess.run(
        ["bash", str(GPU_INSTALL)],
        cwd=ROOT,
        env={"PATH": "/usr/bin:/bin", "KIP_FLEET_GPU_COUNT": "many"},
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 2
    assert "FLEET_GPU_COUNT must be 1 or 2" in result.stderr


def test_fleet_gpu_checkpoint_is_node_pinned_and_uses_immutable_image() -> None:
    script = GPU_INSTALL.read_text(encoding="utf-8")
    assert "nvidia.com/gpu.deploy.operands=false" in script
    assert 'nodeSelector: {"kubernetes.io/hostname": $node}' in script
    assert '"nvidia.com/gpu": "1"' in script
    assert "image: $image" in script
    assert "kip_gpu_verify_chart" in script
    assert "kip_fleet_gpu_capacity_exact" in script


def test_network_smoke_refuses_arguments_without_creating_resources() -> None:
    result = subprocess.run(
        ["bash", str(NETWORK_SMOKE), "unexpected"],
        cwd=ROOT,
        env={"PATH": "/usr/bin:/bin"},
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 2
    assert "accepts no arguments" in result.stderr


def test_network_smoke_rejects_invalid_worker_count_before_cluster_access() -> None:
    result = subprocess.run(
        ["bash", str(NETWORK_SMOKE)],
        cwd=ROOT,
        env={"PATH": "/usr/bin:/bin", "KIP_FLEET_GPU_COUNT": "3"},
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 2
    assert "FLEET_GPU_COUNT must be 1 or 2" in result.stderr


def test_network_smoke_covers_every_cross_node_pair() -> None:
    script = NETWORK_SMOKE.read_text(encoding="utf-8")
    assert "nodes=(kip-control kip-gpu-1)" in script
    assert "nodes+=(kip-gpu-2)" in script
    assert 'for source_node in "${nodes[@]}"' in script
    assert 'for destination_node in "${nodes[@]}"' in script
    assert '"http://$destination_ip:8080/"' in script
    assert '"http://$destination_pod.$namespace.svc.cluster.local:8080/"' in script


def test_large_image_pull_override_is_bounded_and_owned() -> None:
    script = LARGE_IMAGE_PULL.read_text(encoding="utf-8")
    assert "kip-fleet-large-images.toml" in script
    assert 'image_pull_progress_timeout = "30m0s"' in script
    assert "dpkg-query -W -f='${Version}' containerd.io" in script
    assert "crictl ps --name vllm" in script
    assert "cmp -s" in script
    assert "systemctl restart containerd" in script
    result = subprocess.run(
        ["bash", str(LARGE_IMAGE_PULL), "unexpected"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 2
