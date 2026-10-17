#!/usr/bin/env python3
"""Reconcile one reviewed llm-d large-pool flow-control comparison.

This changes only the endpoint-picker concurrency detector for the large
model. It does not alter the vLLM Deployment, image, model, tenant identities,
Gateway, or the interactive pool. Rollout time is excluded from benchmarks.
"""

from __future__ import annotations

import argparse
import os
import runpy
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LLMD = ROOT / "deploy/lambda/llmd-install.py"
POOL = "qwen3-next-80b-a3b-instruct"
RELEASE = "kip-qwen3-next"
VARIANTS = {"baseline": 4, "large-flow6": 6, "large-flow8": 8}
ANNOTATION = "kube-inference-platform.io/flow-variant"


def desired_values(module: dict, control: str, variant: str) -> dict:
    if variant not in VARIANTS:
        raise ValueError("flow-control variant is not reviewed")
    values = module["pool_values"](
        POOL, control, flow=True, large_max_concurrency=VARIANTS[variant]
    )
    if variant != "baseline":
        values["router"]["epp"]["podAnnotations"][ANNOTATION] = variant
    return values


def reconcile(variant: str) -> None:
    if variant not in VARIANTS or os.geteuid() != 0:
        raise ValueError("run as root with a reviewed flow-control variant")
    os.environ["KUBECONFIG"] = "/etc/kubernetes/admin.conf"
    module = runpy.run_path(str(LLMD))
    control = module["run"](["hostname"]).strip()
    if not module["CONTROL"].fullmatch(control):
        raise ValueError("run only on the task-owned Lambda east control plane")
    module["require_models"]()
    if not module["flow_enabled"]():
        raise ValueError("trusted tenant flow control is not installed")
    desired = desired_values(module, control, variant)
    previous = [desired_values(module, control, other) for other in VARIANTS if other != variant]
    inference = module["LOCK"]["inference"]
    with tempfile.TemporaryDirectory(prefix="kip-lambda-flow-variant-") as folder:
        module["chart_release"](
            RELEASE,
            "kip-inference",
            inference["routerGatewayChart"],
            inference["routerGatewayChartDigest"],
            desired,
            Path(folder),
            previous_values=previous,
        )
    print(f"PASS: {RELEASE} endpoint picker is Ready with reviewed {variant} flow control.")


def main() -> int:
    parser = argparse.ArgumentParser(description="Set reviewed llm-d large-pool flow variant")
    parser.add_argument("variant", choices=tuple(VARIANTS))
    args = parser.parse_args()
    try:
        reconcile(args.variant)
    except Exception as error:  # Includes the dynamically loaded installer's RoutingError.
        print(f"Lambda flow variant failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
