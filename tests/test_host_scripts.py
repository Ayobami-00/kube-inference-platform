"""Deterministic tests for target-host preparation and validation scripts."""

import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
VALIDATE_SCRIPT = ROOT / "deploy" / "host" / "validate.sh"
SETUP_SCRIPT = ROOT / "deploy" / "host" / "setup.sh"


def write_command(command_dir: Path, name: str, body: str) -> None:
    command = command_dir / name
    command.write_text(f"#!/bin/sh\n{body}\n")
    command.chmod(0o755)


@pytest.fixture
def host_fixture(tmp_path: Path) -> tuple[Path, Path, Path]:
    command_dir = tmp_path / "commands"
    command_dir.mkdir()
    os_release = tmp_path / "os-release"
    os_release.write_text('ID="ubuntu"\nVERSION_ID="24.04"\nPRETTY_NAME="Ubuntu 24.04 LTS"\n')
    command_log = tmp_path / "commands.log"

    write_command(command_dir, "uname", 'printf "%s\\n" "${KIP_TEST_ARCH:-x86_64}"')
    write_command(command_dir, "ps", 'printf "%s\\n" "${KIP_TEST_INIT:-systemd}"')
    write_command(command_dir, "hostname", 'printf "%s\\n" "gpu-node-1"')
    write_command(
        command_dir,
        "lscpu",
        """cat <<'EOF'
Architecture: x86_64
CPU(s): 32
Model name: Test CPU
Flags: RAW_CPU_SENTINEL
EOF""",
    )
    write_command(
        command_dir,
        "free",
        """cat <<'EOF'
               total        used        free
Mem:           256Gi        10Gi       246Gi
EOF""",
    )
    write_command(
        command_dir,
        "lsblk",
        """if [ "${1:-}" = "-dn" ]; then
    printf "%s\\n" "disk" "disk"
else
    printf "%s\\n" "NAME SIZE TYPE" "nvme0n1 1.8T disk" "nvme1n1 1.8T disk"
fi""",
    )
    write_command(
        command_dir,
        "ip",
        """case "${1:-}" in
    link) printf "%s\\n" "1: lo: <LOOPBACK,UP>" "2: eth0: <BROADCAST,UP>" ;;
    -o) printf "%s\\n" "1: lo: <LOOPBACK,UP>" "2: eth0: <BROADCAST,UP>" ;;
    route) printf "%s\\n" "default via 192.0.2.1 dev eth0" ;;
esac""",
    )
    write_command(command_dir, "getent", 'printf "%s\\n" "192.0.2.2 pkgs.k8s.io"')
    write_command(command_dir, "timedatectl", 'printf "%s\\n" "yes"')
    write_command(command_dir, "swapon", "exit 0")
    write_command(command_dir, "lspci", "exit 0")
    write_command(
        command_dir,
        "sudo",
        """if [ "${1:-}" = "-v" ]; then
    exit 0
fi
	printf "sudo %s\\n" "$*" >> "$KIP_TEST_COMMAND_LOG"
""",
    )
    write_command(
        command_dir,
        "dpkg-query",
        """for argument in "$@"; do
    package_name=$argument
done
if [ "${KIP_MISSING_PACKAGE:-}" = "$package_name" ]; then
    exit 1
fi
	printf "%s\\n" "install ok installed"
""",
    )
    write_command(
        command_dir,
        "apt-get",
        'printf "apt-get %s\\n" "$*" >> "$KIP_TEST_COMMAND_LOG"',
    )
    write_command(
        command_dir,
        "env",
        'printf "env %s\\n" "$*" >> "$KIP_TEST_COMMAND_LOG"',
    )

    return command_dir, os_release, command_log


def run_script(
    script: Path,
    host_fixture: tuple[Path, Path, Path],
    **overrides: str,
) -> subprocess.CompletedProcess[str]:
    command_dir, os_release, command_log = host_fixture
    environment = os.environ.copy()
    environment.update(
        {
            "KIP_TESTING": "1",
            "KIP_TEST_COMMAND_DIR": str(command_dir),
            "KIP_TEST_COMMAND_LOG": str(command_log),
            "KIP_OS_RELEASE_FILE": str(os_release),
            "NO_COLOR": "1",
        }
    )
    environment.update(overrides)
    return subprocess.run(
        ["/bin/bash", str(script)],
        cwd=ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )


@pytest.mark.parametrize("version", ["22.04", "24.04"])
def test_supported_ubuntu_versions_pass(
    host_fixture: tuple[Path, Path, Path], version: str
) -> None:
    _, os_release, _ = host_fixture
    os_release.write_text(
        f'ID="ubuntu"\nVERSION_ID="{version}"\nPRETTY_NAME="Ubuntu {version} LTS"\n'
    )

    result = run_script(VALIDATE_SCRIPT, host_fixture)

    assert result.returncode == 0
    assert "Operating system" in result.stdout
    assert "PASS" in result.stdout


def test_unsupported_operating_system_fails(host_fixture: tuple[Path, Path, Path]) -> None:
    _, os_release, _ = host_fixture
    os_release.write_text('ID="debian"\nVERSION_ID="13"\nPRETTY_NAME="Debian 13"\n')

    result = run_script(VALIDATE_SCRIPT, host_fixture)

    assert result.returncode == 1
    assert "Ubuntu 22.04/24.04 required" in result.stdout


def test_unsupported_architecture_fails(host_fixture: tuple[Path, Path, Path]) -> None:
    result = run_script(VALIDATE_SCRIPT, host_fixture, KIP_TEST_ARCH="aarch64")

    assert result.returncode == 1
    assert "x86_64 required; detected aarch64" in result.stdout


def test_missing_gpu_is_deferred_by_default(host_fixture: tuple[Path, Path, Path]) -> None:
    result = run_script(VALIDATE_SCRIPT, host_fixture)

    assert result.returncode == 0
    assert result.stdout.count("DEFERRED") == 3
    assert "3 deferred" in result.stdout


def test_missing_gpu_fails_when_required(host_fixture: tuple[Path, Path, Path]) -> None:
    result = run_script(VALIDATE_SCRIPT, host_fixture, KIP_GPU_REQUIRED="1")

    assert result.returncode == 1
    assert "NVIDIA hardware" in result.stdout
    assert "3 failed" in result.stdout


def test_healthy_gpu_and_topology_pass(host_fixture: tuple[Path, Path, Path]) -> None:
    command_dir, _, _ = host_fixture
    write_command(command_dir, "lspci", 'printf "%s\\n" "0000:01:00.0 NVIDIA Corporation"')
    write_command(
        command_dir,
        "nvidia-smi",
        """if [ "${1:-}" = "topo" ]; then
    printf "%s\\n" "GPU0 X"
else
    printf "%s\\n" "NVIDIA-SMI test output"
fi""",
    )

    result = run_script(VALIDATE_SCRIPT, host_fixture, KIP_GPU_REQUIRED="1")

    assert result.returncode == 0
    assert "nvidia-smi completed successfully" in result.stdout
    assert "nvidia-smi topo -m completed successfully" in result.stdout


def test_verbose_mode_includes_raw_command_output(host_fixture: tuple[Path, Path, Path]) -> None:
    concise = run_script(VALIDATE_SCRIPT, host_fixture)
    verbose = run_script(VALIDATE_SCRIPT, host_fixture, KIP_VERBOSE="1")

    assert "RAW_CPU_SENTINEL" not in concise.stdout
    assert "RAW_CPU_SENTINEL" in verbose.stdout
    assert "--- lscpu ---" in verbose.stdout


def test_no_color_overrides_forced_color(host_fixture: tuple[Path, Path, Path]) -> None:
    no_color = run_script(VALIDATE_SCRIPT, host_fixture, KIP_FORCE_COLOR="1")

    assert "\x1b[" not in no_color.stdout


def test_invalid_boolean_configuration_exits_two(
    host_fixture: tuple[Path, Path, Path],
) -> None:
    result = run_script(VALIDATE_SCRIPT, host_fixture, KIP_VERBOSE="sometimes")

    assert result.returncode == 2
    assert "VERBOSE must be 0 or 1" in result.stderr


def test_missing_required_command_has_remediation(
    host_fixture: tuple[Path, Path, Path],
) -> None:
    command_dir, _, _ = host_fixture
    (command_dir / "lscpu").unlink()

    result = run_script(VALIDATE_SCRIPT, host_fixture)

    assert result.returncode == 1
    assert "Missing: lscpu. Run make host-setup" in result.stdout


def test_setup_skips_packages_that_are_already_installed(
    host_fixture: tuple[Path, Path, Path],
) -> None:
    _, _, command_log = host_fixture

    result = run_script(SETUP_SCRIPT, host_fixture)

    assert result.returncode == 0
    assert "All required packages are already installed" in result.stdout
    assert not command_log.exists() or "apt-get" not in command_log.read_text()


def test_setup_rejects_unsupported_platform_before_package_commands(
    host_fixture: tuple[Path, Path, Path],
) -> None:
    _, os_release, command_log = host_fixture
    os_release.write_text('ID="debian"\nVERSION_ID="13"\nPRETTY_NAME="Debian 13"\n')

    result = run_script(SETUP_SCRIPT, host_fixture)

    assert result.returncode == 1
    assert "Ubuntu 22.04/24.04 x86_64 required" in result.stdout
    assert not command_log.exists()


def test_setup_installs_only_missing_packages_without_upgrade(
    host_fixture: tuple[Path, Path, Path],
) -> None:
    _, _, command_log = host_fixture

    result = run_script(SETUP_SCRIPT, host_fixture, KIP_MISSING_PACKAGE="jq")

    assert result.returncode == 0
    commands = command_log.read_text()
    assert "apt-get update" in commands
    assert "apt-get install -y --no-install-recommends jq" in commands
    assert "upgrade" not in commands
