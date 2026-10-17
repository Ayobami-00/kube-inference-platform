#!/usr/bin/env python3
"""Derive the two-model Lambda pilot dashboard from the reviewed fleet view."""

from __future__ import annotations

import json
import sys
from pathlib import Path


def render(source: Path) -> dict:
    dashboard = json.loads(source.read_text(encoding="utf-8"))
    if dashboard.get("uid") != "kip-llmd-fleet-pilot" or len(dashboard.get("panels", [])) != 16:
        raise ValueError("reviewed fleet dashboard schema drifted")
    # Cost must not be shown as zero or inferred from Vast-only recording rules.
    dashboard["panels"] = [
        panel for panel in dashboard["panels"] if panel.get("id") not in {14, 15}
    ]
    dashboard["uid"] = "kip-llmd-lambda-campaign"
    dashboard["title"] = "KIP / Lambda Multi-model llm-d Campaign"
    encoded = json.dumps(dashboard, sort_keys=True)
    encoded = encoded.replace("kip:fleet:", "kip:lambda:")
    encoded = encoded.replace("tokens_generated_rate5m", "generated_tokens_rate5m")
    encoded = encoded.replace("kip-pilot", "kip-lambda")
    return json.loads(encoded)


def main() -> int:
    if len(sys.argv) != 3:
        print("usage: render-dashboard.py REVIEWED_SOURCE OUTPUT", file=sys.stderr)
        return 2
    output = render(Path(sys.argv[1]))
    Path(sys.argv[2]).write_text(json.dumps(output, sort_keys=True, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
