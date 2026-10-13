#!/usr/bin/env python3
"""Read internal EPP flow counters with a short-lived service-account token."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from urllib.request import Request, urlopen

FAIRNESS_RE = re.compile(r'fairness_id="(tenant-[abc])"')
PRIORITY_RE = re.compile(r'priority="(-?\d+)"')


def read_metrics(url: str) -> dict[str, object]:
    completed = subprocess.run(
        [
            "kubectl",
            "--kubeconfig",
            "/etc/kubernetes/admin.conf",
            "-n",
            "kip-inference",
            "create",
            "token",
            "kip-metrics-reader",
            "--duration=600s",
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=20,
    )
    token = completed.stdout.strip()
    if not token:
        raise ValueError("short-lived metrics reader token was empty")
    request = Request(url, headers={"Authorization": f"Bearer {token}"})
    with urlopen(request, timeout=20) as response:
        if response.status != 200:
            raise ValueError(f"EPP metrics returned HTTP {response.status}")
        payload = response.read().decode("utf-8")
    lines = [
        line
        for line in payload.splitlines()
        if line.startswith("llm_d_epp_flow_control_") and not line.startswith("#")
    ]
    cohorts = set()
    for line in lines:
        fairness = FAIRNESS_RE.search(line)
        priority = PRIORITY_RE.search(line)
        if fairness and priority:
            cohorts.add((fairness.group(1), int(priority.group(1))))
    return {
        "flowSeries": len(lines),
        "tenantPriorityPairs": [
            {"tenant": tenant, "priority": priority} for tenant, priority in sorted(cohorts)
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Read KIP internal llm-d flow metrics")
    parser.add_argument(
        "--url", default="http://kip-qwen25-pilot-epp.kip-inference.svc:9090/metrics"
    )
    args = parser.parse_args()
    try:
        print(json.dumps(read_metrics(args.url), sort_keys=True))
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print(f"flow metrics failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
