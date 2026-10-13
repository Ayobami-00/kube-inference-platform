#!/usr/bin/env python3
"""Compare the exact, merged KIP fleet Helm values with a live release."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml


def merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    result = base.copy()
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = merge(result[key], value)
        else:
            result[key] = value
    return result


def read_yaml(path: Path) -> dict[str, Any]:
    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a mapping")
    return value


def main() -> int:
    if len(sys.argv) != 5:
        print("usage: helm-values-check.py RELEASE NAMESPACE BASE OVERLAY", file=sys.stderr)
        return 2
    release, namespace, base_path, overlay_path = sys.argv[1:]
    desired = merge(read_yaml(Path(base_path)), read_yaml(Path(overlay_path)))
    try:
        actual = subprocess.run(
            ["helm", "get", "values", release, "--namespace", namespace, "--output", "json"],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        recorded = json.loads(actual.stdout)
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as error:
        print(f"could not inspect Helm release values: {error}", file=sys.stderr)
        return 1
    if recorded != desired:
        print("Helm release values differ from the committed fleet configuration", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
