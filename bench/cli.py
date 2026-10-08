"""Command-line entry point for AIPerf benchmark orchestration and reporting."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .aiperf import (
    RESULTS_HOST_PATH,
    _results_directory_owned,
    check_foundation,
    execute_run,
    install_foundation,
    run_platform_preflight,
)
from .config import load_cost_profile, load_suite, validate_identifier
from .errors import BenchError, ValidationError
from .paths import reports_root, suites_root, work_root
from .report import generate_report, load_normalized_input


def build_parser() -> argparse.ArgumentParser:
    """Create the public command-line parser."""
    parser = argparse.ArgumentParser(
        prog="python -m bench",
        description=(
            "AIPerf-backed concurrent inference benchmarker for Kube Inference Platform. "
            "It orchestrates load generation and publishes sanitized performance/cost reports."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", metavar="COMMAND")
    subparsers.add_parser("help", help="show this help message")

    validate = subparsers.add_parser("validate-suite", help="validate a committed suite")
    validate.add_argument("--suite", default="baseline", metavar="NAME")

    check = subparsers.add_parser("check", help="check benchmark assets and optional cluster state")
    check.add_argument("--cost-profile", metavar="NAME")
    check.add_argument("--cluster", action="store_true")

    install = subparsers.add_parser(
        "install", help="install benchmark namespace and result storage"
    )
    install.add_argument("--dry-run", action="store_true")

    run = subparsers.add_parser("run", help="run a committed suite using Kubernetes AIPerf Jobs")
    run.add_argument("--suite", default="baseline", metavar="NAME")
    run.add_argument("--cost-profile", required=True, metavar="NAME")
    run.add_argument("--run-id", metavar="ID")
    run.add_argument("--dry-run", action="store_true")
    run.add_argument(
        "--resume",
        action="store_true",
        help="resume an interrupted run after validating its frozen plan and artifacts",
    )

    report = subparsers.add_parser("report", help="publish a sanitized static benchmark report")
    report.add_argument("--run-id", required=True, metavar="ID")
    report.add_argument("--input", type=Path, metavar="PATH")
    report.add_argument("--output", type=Path, metavar="DIR")

    listing = subparsers.add_parser("list", help="list committed suites or benchmark runs")
    listing.add_argument("--kind", choices=("suites", "runs"), default="runs")

    serve = subparsers.add_parser("serve", help="serve one static report on loopback")
    serve.add_argument("--run-id", required=True, metavar="ID")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8008)

    clean = subparsers.add_parser("clean", help="delete expired raw work data, never reports")
    clean.add_argument("--retain-days", type=int, default=30, metavar="DAYS")
    clean.add_argument("--dry-run", action="store_true")
    return parser


def _generated_run_id(suite: str) -> str:
    suffix = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    value = f"{suite}-{suffix}"
    return validate_identifier(value, kind="run ID", run_id=True)


def _json_output(value: object) -> None:
    print(json.dumps(value, sort_keys=True, indent=2))


def _validate_suite(name: str) -> int:
    suite = load_suite(name)
    measured = (
        len(suite.data["sweep"]["concurrency"])
        * suite.data["sweep"]["requestsPerCell"]
        * suite.data["sweep"]["trialsPerCell"]
    )
    _json_output(
        {
            "status": "valid",
            "suite": suite.name,
            "sha256": suite.sha256,
            "measuredRequests": measured,
        }
    )
    return 0


def _check(args: argparse.Namespace) -> int:
    status = check_foundation(cluster=args.cluster)
    load_suite("baseline")
    selected_profile = None
    if args.cost_profile:
        selected_profile = load_cost_profile(args.cost_profile)
        status["pricing"] = {
            "status": "configured",
            "profile": selected_profile.name,
            "sha256": selected_profile.sha256,
            "nodeHourlyRateUsd": selected_profile.node_hourly_rate,
            "gpuHourlyRateUsd": selected_profile.gpu_hourly_rate,
        }
    else:
        status["pricing"] = {
            "status": "warning",
            "detail": "pricing not configured; report publication is blocked",
        }
    if args.cluster:
        # ``benchmark-check`` is the platform-readiness check, not merely a
        # local asset validator. An omitted profile permits the documented
        # unpriced warning; a supplied profile must be the exact active
        # OpenCost profile because the cost check receives its name.
        run_platform_preflight(selected_profile.name if selected_profile else "")
        status["platformPreflight"] = "passed"
    _json_output(status)
    return 0


def _install(args: argparse.Namespace) -> int:
    result = install_foundation(dry_run=args.dry_run)
    _json_output(result)
    return 0


def _run(args: argparse.Namespace) -> int:
    suite = load_suite(args.suite)
    profile = load_cost_profile(args.cost_profile)
    if args.resume and not args.run_id:
        raise ValidationError("--resume requires an explicit --run-id")
    run_id = validate_identifier(
        args.run_id or _generated_run_id(suite.name), kind="run ID", run_id=True
    )
    result = execute_run(suite, profile, run_id, dry_run=args.dry_run, resume=args.resume)
    _json_output(result)
    if not args.dry_run:
        print(f"Benchmark and sanitized report completed for {run_id}.")
    return 0


def _report(args: argparse.Namespace) -> int:
    run_id = validate_identifier(args.run_id, kind="run ID", run_id=True)
    input_path = args.input or work_root() / run_id / "normalized.json"
    output = args.output or reports_root() / run_id
    data = load_normalized_input(input_path)
    embedded = data.get("costProfile")
    if not isinstance(embedded, dict) or not isinstance(embedded.get("name"), str):
        raise ValidationError("normalized input must identify its committed cost profile")
    profile = load_cost_profile(embedded["name"])
    destination = generate_report(
        data,
        run_id=run_id,
        cost_profile=profile,
        output=output,
        replace=True,
    )
    _json_output({"status": "published", "runId": run_id, "path": str(destination)})
    return 0


def _list(args: argparse.Namespace) -> int:
    if args.kind == "suites":
        names = []
        for path in sorted(suites_root().glob("*.json")):
            try:
                suite = load_suite(path.stem)
            except ValidationError:
                continue
            names.append({"name": suite.name, "sha256": suite.sha256})
        _json_output({"suites": names})
        return 0
    names = sorted(
        {
            path.name
            for root in (work_root(), reports_root())
            if root.exists()
            for path in root.iterdir()
        }
    )
    runs = []
    for name in names:
        try:
            validate_identifier(name, kind="run ID", run_id=True)
        except ValidationError:
            continue
        runs.append(
            {
                "id": name,
                "rawWork": (work_root() / name).is_dir(),
                "report": (reports_root() / name / "index.html").is_file(),
            }
        )
    _json_output({"runs": runs})
    return 0


def _serve(args: argparse.Namespace) -> int:
    run_id = validate_identifier(args.run_id, kind="run ID", run_id=True)
    if args.host != "127.0.0.1":
        raise ValidationError("benchmark reports may only bind to 127.0.0.1")
    if args.port < 1 or args.port > 65535:
        raise ValidationError("port must be in the range 1..65535")
    directory = reports_root() / run_id
    if not (directory / "index.html").is_file():
        raise ValidationError(f"published report does not exist: {run_id}")
    handler = partial(SimpleHTTPRequestHandler, directory=str(directory))
    server = ThreadingHTTPServer((args.host, args.port), handler)
    print(f"Serving {run_id} at http://{args.host}:{args.port}/ (Ctrl-C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


def _clean(args: argparse.Namespace) -> int:
    if args.retain_days < 0 or args.retain_days > 3650:
        raise ValidationError("retain-days must be in the range 0..3650")
    cutoff = datetime.now(UTC) - timedelta(days=args.retain_days)
    removed: dict[str, list[str]] = {"local": [], "node": []}

    def clean_root(category: str, root: Path) -> None:
        if not root.is_dir():
            return
        for path in sorted(root.iterdir()):
            if not path.is_dir() or path.is_symlink():
                continue
            try:
                validate_identifier(path.name, kind="run ID", run_id=True)
            except ValidationError:
                continue
            modified = datetime.fromtimestamp(path.stat().st_mtime, UTC)
            if modified <= cutoff:
                removed[category].append(path.name)
                if not args.dry_run:
                    shutil.rmtree(path)

    # Local ignored work data is repository-scoped and independent of the
    # privileged node volume. Never recurse into the latter unless its exact
    # KIP ownership marker is present.
    clean_root("local", work_root())
    if os.path.lexists(RESULTS_HOST_PATH):
        if not _results_directory_owned():
            raise BenchError(
                f"refusing to clean unowned benchmark result path: {RESULTS_HOST_PATH}"
            )
        clean_root("node", RESULTS_HOST_PATH)
    _json_output({"removed": removed, "retainedDays": args.retain_days, "dryRun": args.dry_run})
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Dispatch commands with stable exit codes: success=0, failure=1, usage=2."""
    parser = build_parser()
    arguments = list(argv) if argv is not None else sys.argv[1:]
    if not arguments:
        parser.print_help()
        return 0
    args = parser.parse_args(arguments)
    if args.command == "help":
        parser.print_help()
        return 0
    handlers = {
        "validate-suite": lambda: _validate_suite(args.suite),
        "check": lambda: _check(args),
        "install": lambda: _install(args),
        "run": lambda: _run(args),
        "report": lambda: _report(args),
        "list": lambda: _list(args),
        "serve": lambda: _serve(args),
        "clean": lambda: _clean(args),
    }
    try:
        return handlers[args.command]()
    except BenchError as error:
        print(f"error: {error}", file=sys.stderr)
        return error.exit_code
