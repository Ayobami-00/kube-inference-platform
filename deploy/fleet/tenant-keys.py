#!/usr/bin/env python3
"""Generate pilot credentials once into an exclusive, owner-only local file."""

from __future__ import annotations

import json
import os
import secrets
import sys
from pathlib import Path

TENANTS = ("tenant-a", "tenant-b", "tenant-c")
CLASSES = ("chat", "agentic", "batch", "voice")


def create_credentials(path: Path) -> bool:
    if path.exists():
        if not path.is_file() or path.stat().st_mode & 0o077:
            raise ValueError("existing credential file is not a private regular file")
        return False
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.parent.stat().st_mode & 0o077:
        raise ValueError("credential directory is not owner-only")
    records = {
        tenant: {workload_class: secrets.token_hex(32) for workload_class in CLASSES}
        for tenant in TENANTS
    }
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(records, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return True


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: tenant-keys.py PRIVATE_PATH")
    create_credentials(Path(sys.argv[1]))
