"""Smoke tests for the benchmark command-line interface."""

import subprocess
import sys


def test_bench_package_is_importable() -> None:
    result = subprocess.run(
        [sys.executable, "-c", "import bench"],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0


def test_bench_help_identifies_the_inference_benchmarker() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "bench", "--help"],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert "inference benchmarker" in result.stdout.lower()


def test_bench_without_arguments_displays_help() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "bench"],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert result.stdout.startswith("usage: python -m bench")
