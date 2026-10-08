#!/usr/bin/env python3
"""Fail-closed validation for committed platform artifacts.

The default invocation requires the pinned ``promtool`` and Helm toolchain plus
the exact cached upstream chart archives. It never downloads dependencies.
Developers may explicitly select ``--structural-only`` for the fast subset, but
that mode is not used by the required ``make check`` quality gate.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path, PurePosixPath
from typing import Any

import yaml


class ArtifactError(RuntimeError):
    """A committed artifact violates the offline platform contract."""


_DASHBOARD_DIMENSIONS = {
    "cluster",
    "namespace",
    "deployment",
    "runtime",
    "model_profile",
    "model_name",
    "worker_role",
    "node",
    "pod",
    "gpu_uuid",
}
_DURATION = re.compile(r"^[1-9][0-9]*(?:ms|s|m|h|d|w|y)$")
_METRIC_NAME = re.compile(r"^[A-Za-z_:][A-Za-z0-9_:]*$")
_ALERT_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_PROFILE_NAME = re.compile(r"^[a-z0-9](?:[-a-z0-9]*[a-z0-9])?$")
_IPV4 = re.compile(
    rb"(?<![0-9])(?:25[0-5]|2[0-4][0-9]|1?[0-9]{1,2})"
    rb"(?:\.(?:25[0-5]|2[0-4][0-9]|1?[0-9]{1,2})){3}(?![0-9])"
)
_SECRET_PATTERNS = (
    re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(rb"AKIA[0-9A-Z]{16}"),
    re.compile(rb"gh" + rb"[pousr]_[A-Za-z0-9]{30,}"),
    re.compile(rb"hf" + rb"_[A-Za-z0-9]{30,}"),
    re.compile(rb"xox" + rb"[abprs]-[A-Za-z0-9-]{20,}"),
)
_REPORT_SENSITIVE_KEYS = re.compile(
    rb'"(?:rawPrompt|rawResponse|requestBody|responseBody|requestTrace|credentials|apiKey)"\s*:'
)


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ArtifactError(f"{path}: invalid JSON: {error}") from error


def _require_mapping(value: Any, context: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise ArtifactError(f"{context}: expected an object")
    return value


def _require_keys(value: Mapping[str, Any], required: set[str], context: str) -> None:
    missing = sorted(required - value.keys())
    if missing:
        raise ArtifactError(f"{context}: missing keys: {', '.join(missing)}")


def _validate_grid_position(value: Any, context: str) -> None:
    grid = _require_mapping(value, f"{context}.gridPos")
    _require_keys(grid, {"x", "y", "w", "h"}, f"{context}.gridPos")
    if any(isinstance(grid[key], bool) or not isinstance(grid[key], int) for key in grid):
        raise ArtifactError(f"{context}.gridPos: coordinates must be integers")
    if not (0 <= grid["x"] < 24 and 1 <= grid["w"] <= 24):
        raise ArtifactError(f"{context}.gridPos: x/w are outside Grafana's 24-column grid")
    if grid["x"] + grid["w"] > 24 or grid["y"] < 0 or grid["h"] <= 0:
        raise ArtifactError(f"{context}.gridPos: invalid panel bounds")


def _validate_balanced_promql(expression: str, context: str) -> None:
    """Catch malformed committed expressions without pretending to replace promtool."""
    if not expression.strip():
        raise ArtifactError(f"{context}: PromQL expression is empty")
    stack: list[str] = []
    pairs = {")": "(", "]": "[", "}": "{"}
    quoted = False
    escaped = False
    for character in expression:
        if quoted:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                quoted = False
            continue
        if character == '"':
            quoted = True
        elif character in "([{":
            stack.append(character)
        elif character in ")]}" and (not stack or stack.pop() != pairs[character]):
            raise ArtifactError(f"{context}: unbalanced PromQL delimiters")
    if quoted or stack:
        raise ArtifactError(f"{context}: unterminated string or PromQL delimiter")


def validate_dashboards(root: Path) -> int:
    paths = sorted((root / "deploy/observability/dashboards").glob("*.json"))
    if not paths:
        raise ArtifactError("no Grafana dashboards were found")
    seen_uids: set[str] = set()
    for path in paths:
        dashboard = _require_mapping(_load_json(path), str(path))
        _require_keys(
            dashboard,
            {"uid", "title", "schemaVersion", "editable", "timezone", "panels", "templating"},
            str(path),
        )
        uid = dashboard["uid"]
        if not isinstance(uid, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", uid):
            raise ArtifactError(f"{path}: uid is not Grafana-safe")
        if uid in seen_uids:
            raise ArtifactError(f"{path}: duplicate dashboard uid {uid!r}")
        seen_uids.add(uid)
        if dashboard["editable"] is not False or dashboard["timezone"] != "utc":
            raise ArtifactError(f"{path}: provisioned dashboards must be immutable and UTC")
        if not isinstance(dashboard["schemaVersion"], int) or dashboard["schemaVersion"] <= 0:
            raise ArtifactError(f"{path}: invalid Grafana schemaVersion")

        panels = dashboard["panels"]
        if not isinstance(panels, list) or not panels:
            raise ArtifactError(f"{path}: panels must be a non-empty array")
        panel_ids: set[int] = set()
        for index, panel_value in enumerate(panels):
            context = f"{path}: panels[{index}]"
            panel = _require_mapping(panel_value, context)
            _require_keys(panel, {"id", "title", "type", "gridPos"}, context)
            panel_id = panel["id"]
            if isinstance(panel_id, bool) or not isinstance(panel_id, int) or panel_id <= 0:
                raise ArtifactError(f"{context}: id must be a positive integer")
            if panel_id in panel_ids:
                raise ArtifactError(f"{context}: duplicate panel id {panel_id}")
            panel_ids.add(panel_id)
            if not isinstance(panel["title"], str) or not panel["title"].strip():
                raise ArtifactError(f"{context}: title must be non-empty")
            _validate_grid_position(panel["gridPos"], context)
            targets = panel.get("targets", [])
            if panel["type"] not in {"row", "text"} and not targets:
                raise ArtifactError(f"{context}: data panel has no query targets")
            if not isinstance(targets, list):
                raise ArtifactError(f"{context}: targets must be an array")
            for target_index, target_value in enumerate(targets):
                target = _require_mapping(target_value, f"{context}.targets[{target_index}]")
                expression = target.get("expr")
                if not isinstance(expression, str):
                    raise ArtifactError(f"{context}.targets[{target_index}]: missing PromQL expr")
                _validate_balanced_promql(expression, f"{context}.targets[{target_index}]")

        templating = _require_mapping(dashboard["templating"], f"{path}: templating")
        variables = templating.get("list")
        if not isinstance(variables, list):
            raise ArtifactError(f"{path}: templating.list must be an array")
        names = {value.get("name") for value in variables if isinstance(value, dict)}
        missing_dimensions = sorted(_DASHBOARD_DIMENSIONS - names)
        if missing_dimensions:
            raise ArtifactError(
                f"{path}: missing dashboard dimensions: {', '.join(missing_dimensions)}"
            )
    return len(paths)


def _validate_schema_scalar(value: Any, schema: Mapping[str, Any], context: str) -> None:
    if "const" in schema and value != schema["const"]:
        raise ArtifactError(f"{context}: expected constant {schema['const']!r}")
    value_type = schema.get("type")
    if value_type == "string" and not isinstance(value, str):
        raise ArtifactError(f"{context}: expected a string")
    if value_type == "number" and (isinstance(value, bool) or not isinstance(value, int | float)):
        raise ArtifactError(f"{context}: expected a number")
    if value_type == "integer" and (isinstance(value, bool) or not isinstance(value, int)):
        raise ArtifactError(f"{context}: expected an integer")
    if isinstance(value, str):
        if len(value) < schema.get("minLength", 0) or len(value) > schema.get(
            "maxLength", sys.maxsize
        ):
            raise ArtifactError(f"{context}: string length is outside the schema bounds")
        if (pattern := schema.get("pattern")) and re.fullmatch(pattern, value) is None:
            raise ArtifactError(f"{context}: string does not match the schema pattern")
    if isinstance(value, int | float) and not isinstance(value, bool):
        if "exclusiveMinimum" in schema and value <= schema["exclusiveMinimum"]:
            raise ArtifactError(f"{context}: value is below the exclusive minimum")
        if "exclusiveMaximum" in schema and value >= schema["exclusiveMaximum"]:
            raise ArtifactError(f"{context}: value is above the exclusive maximum")
        if "minimum" in schema and value < schema["minimum"]:
            raise ArtifactError(f"{context}: value is below the minimum")
        if "maximum" in schema and value > schema["maximum"]:
            raise ArtifactError(f"{context}: value is above the maximum")


def _validate_cost_instance(
    value: Any, schema: Mapping[str, Any], context: str, *, template: bool
) -> None:
    profile = _require_mapping(value, context)
    required = set(schema.get("required", []))
    properties = _require_mapping(schema.get("properties"), f"{context}: schema.properties")
    if set(profile) != required or set(properties) != required:
        raise ArtifactError(f"{context}: fields do not exactly match the cost-profile schema")
    for key, property_schema in properties.items():
        if template and key == "nodeHourlyRateUsd" and profile[key] is None:
            continue
        if template and key in {"provider", "region", "source", "effectiveFrom"}:
            if not isinstance(profile[key], str) or not profile[key]:
                raise ArtifactError(f"{context}.{key}: template placeholder must be non-empty")
            continue
        _validate_schema_scalar(profile[key], property_schema, f"{context}.{key}")


def validate_profiles(root: Path) -> int:
    schema_path = root / "costs/schema-v1.json"
    schema = _require_mapping(_load_json(schema_path), str(schema_path))
    if schema.get("$schema") != "https://json-schema.org/draft/2020-12/schema":
        raise ArtifactError(f"{schema_path}: expected JSON Schema draft 2020-12")
    if schema.get("additionalProperties") is not False:
        raise ArtifactError(f"{schema_path}: additional properties must be rejected")

    count = 0
    for path in sorted((root / "costs/profiles").glob("*.json")):
        template = path.name.endswith(".template.json")
        profile = _load_json(path)
        _validate_cost_instance(profile, schema, str(path), template=template)
        if not template:
            if path.stem != profile["name"] or not _PROFILE_NAME.fullmatch(profile["name"]):
                raise ArtifactError(f"{path}: filename and profile name must match")
            if "REPLACE_" in json.dumps(profile):
                raise ArtifactError(f"{path}: published profile contains a placeholder")
        count += 1

    for path in sorted((root / "deploy/runtime/vllm/profiles").glob("*.json")):
        profile = _require_mapping(_load_json(path), str(path))
        _require_keys(profile, {"schemaVersion", "name", "model", "kubernetes", "vllm"}, str(path))
        if profile["schemaVersion"] != 1 or profile["name"] != path.stem:
            raise ArtifactError(f"{path}: runtime profile identity/schema version mismatch")
        model = _require_mapping(profile["model"], f"{path}: model")
        if not re.fullmatch(r"[0-9a-f]{40}", str(model.get("revision", ""))):
            raise ArtifactError(f"{path}: model revision must be an immutable commit SHA")
        count += 1

    suites = root / "benchmarks/suites"
    sys.path.insert(0, str(root))
    try:
        from bench.config import load_suite

        for path in sorted(suites.glob("*.json")):
            load_suite(path.stem, root=suites)
            count += 1
    finally:
        sys.path.pop(0)
    return count


def _load_prometheus_rule(root: Path) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    path = root / "deploy/observability/manifests/prometheus-rules.yaml"
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        raise ArtifactError(f"{path}: invalid YAML: {error}") from error
    resource = _require_mapping(document, str(path))
    if (
        resource.get("apiVersion") != "monitoring.coreos.com/v1"
        or resource.get("kind") != "PrometheusRule"
    ):
        raise ArtifactError(f"{path}: expected a monitoring.coreos.com/v1 PrometheusRule")
    spec = _require_mapping(resource.get("spec"), f"{path}: spec")
    return resource, spec


def validate_prometheus_rules(root: Path) -> tuple[int, Mapping[str, Any]]:
    _, spec = _load_prometheus_rule(root)
    groups = spec.get("groups")
    if not isinstance(groups, list) or not groups:
        raise ArtifactError("PrometheusRule spec.groups must be a non-empty array")
    group_names: set[str] = set()
    rule_names: set[tuple[str, str]] = set()
    count = 0
    for group_index, group_value in enumerate(groups):
        context = f"PrometheusRule groups[{group_index}]"
        group = _require_mapping(group_value, context)
        name = group.get("name")
        if not isinstance(name, str) or not name or name in group_names:
            raise ArtifactError(f"{context}: group name is missing or duplicated")
        group_names.add(name)
        interval = group.get("interval")
        if interval is not None and (
            not isinstance(interval, str) or _DURATION.fullmatch(interval) is None
        ):
            raise ArtifactError(f"{context}: invalid evaluation interval")
        rules = group.get("rules")
        if not isinstance(rules, list) or not rules:
            raise ArtifactError(f"{context}: rules must be a non-empty array")
        for rule_index, rule_value in enumerate(rules):
            rule_context = f"{context}.rules[{rule_index}]"
            rule = _require_mapping(rule_value, rule_context)
            is_record = "record" in rule
            is_alert = "alert" in rule
            if is_record == is_alert:
                raise ArtifactError(f"{rule_context}: expected exactly one of record or alert")
            kind = "record" if is_record else "alert"
            rule_name = rule[kind]
            pattern = _METRIC_NAME if is_record else _ALERT_NAME
            if not isinstance(rule_name, str) or pattern.fullmatch(rule_name) is None:
                raise ArtifactError(f"{rule_context}: invalid {kind} name")
            identity = (kind, rule_name)
            if identity in rule_names:
                raise ArtifactError(f"{rule_context}: duplicate {kind} name {rule_name!r}")
            rule_names.add(identity)
            expression = rule.get("expr")
            if not isinstance(expression, str):
                raise ArtifactError(f"{rule_context}: expr must be a string")
            _validate_balanced_promql(expression, rule_context)
            duration = rule.get("for")
            if duration is not None and (
                not isinstance(duration, str) or _DURATION.fullmatch(duration) is None
            ):
                raise ArtifactError(f"{rule_context}: invalid alert duration")
            for field in ("labels", "annotations"):
                if field in rule and not isinstance(rule[field], dict):
                    raise ArtifactError(f"{rule_context}: {field} must be an object")
            count += 1
    return count, {"groups": groups}


def validate_yaml_and_helm_values(root: Path) -> int:
    candidates = [
        root / "deploy/observability/values/kube-prometheus-stack.yaml",
        root / "deploy/observability/values/opencost-unpriced.yaml",
        root / "deploy/observability/manifests/namespace.yaml",
        root / "deploy/observability/manifests/service-monitors.yaml",
    ]
    documents = 0
    for path in candidates:
        try:
            loaded = list(yaml.safe_load_all(path.read_text(encoding="utf-8")))
        except (OSError, yaml.YAMLError) as error:
            raise ArtifactError(f"{path}: invalid YAML/Helm values: {error}") from error
        if not loaded or any(
            document is not None and not isinstance(document, dict) for document in loaded
        ):
            raise ArtifactError(f"{path}: expected YAML mapping documents")
        documents += sum(document is not None for document in loaded)
    return documents


def _command_path(env_name: str, default: str) -> str | None:
    configured = os.environ.get(env_name, "").strip()
    if configured:
        path = Path(configured).expanduser()
        return str(path) if path.is_file() and os.access(path, os.X_OK) else None
    return shutil.which(default)


def validate_with_promtool(root: Path, native_rules: Mapping[str, Any], *, strict: bool) -> bool:
    promtool = _command_path("PROMTOOL", "promtool")
    if promtool is None:
        if strict:
            raise ArtifactError(
                "strict validation requires the pinned promtool; run "
                "`make artifact-validator-setup` or set PROMTOOL explicitly"
            )
        return False
    version_result = subprocess.run(
        [promtool, "--version"], check=False, capture_output=True, text=True
    )
    version_output = f"{version_result.stdout}\n{version_result.stderr}"
    version_match = re.search(r"\bpromtool, version ([^\s]+)", version_output)
    expected_version = _version_pin(root, "KIP_PROMETHEUS_VERSION")
    expected_version = expected_version.removeprefix("v").removesuffix("-distroless")
    if (
        version_result.returncode != 0
        or version_match is None
        or version_match.group(1) != expected_version
    ):
        actual = version_match.group(1) if version_match is not None else "unknown"
        raise ArtifactError(f"promtool version mismatch: expected {expected_version}, got {actual}")
    with tempfile.TemporaryDirectory(prefix="kip-promtool-") as directory:
        path = Path(directory) / "rules.yaml"
        path.write_text(yaml.safe_dump(dict(native_rules), sort_keys=False), encoding="utf-8")
        completed = subprocess.run(
            [promtool, "check", "rules", str(path)],
            check=False,
            capture_output=True,
            text=True,
        )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise ArtifactError(f"promtool rejected the recording/alert rules: {detail}")
    return True


def _pin_from_file(path: Path, name: str) -> str:
    pattern = re.compile(rf'^{re.escape(name)}="([^"]+)"$', re.MULTILINE)
    versions = path.read_text(encoding="utf-8")
    match = pattern.search(versions)
    if match is None:
        raise ArtifactError(f"{path} does not define {name}")
    return match.group(1)


def _version_pin(root: Path, name: str) -> str:
    return _pin_from_file(root / "deploy/observability/versions.env", name)


def _cluster_version_pin(root: Path, name: str) -> str:
    return _pin_from_file(root / "deploy/cluster/versions.env", name)


def _validator_checksum_pin(root: Path, name: str) -> str:
    return _pin_from_file(root / "scripts/artifact-validator-checksums.env", name)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
    except OSError as error:
        raise ArtifactError(f"cannot hash {path}: {error}") from error
    return digest.hexdigest()


def _run_helm_chart(
    helm: str,
    archive: Path,
    *,
    expected_name: str,
    expected_version: str,
    expected_sha256: str,
    release: str,
    values: Path,
) -> None:
    if not archive.is_file():
        raise ArtifactError(f"cached Helm chart does not exist: {archive}")
    actual_sha256 = _sha256(archive)
    if actual_sha256 != expected_sha256:
        raise ArtifactError(
            f"{archive}: chart archive checksum mismatch: expected {expected_sha256}, "
            f"got {actual_sha256}"
        )
    shown = subprocess.run(
        [helm, "show", "chart", str(archive)],
        check=False,
        capture_output=True,
        text=True,
    )
    if shown.returncode != 0:
        raise ArtifactError(f"helm show chart failed for {archive}: {shown.stderr.strip()}")
    metadata = _require_mapping(yaml.safe_load(shown.stdout), f"{archive}: Chart.yaml")
    if metadata.get("name") != expected_name or str(metadata.get("version")) != expected_version:
        raise ArtifactError(
            f"{archive}: expected {expected_name} {expected_version}, got "
            f"{metadata.get('name')} {metadata.get('version')}"
        )
    common = [
        "--namespace",
        "kip-observability",
        "--values",
        str(values),
    ]
    linted = subprocess.run(
        [helm, "lint", str(archive), *common], check=False, capture_output=True, text=True
    )
    if linted.returncode != 0:
        raise ArtifactError(f"helm lint failed for {archive}: {linted.stdout}{linted.stderr}")
    rendered = subprocess.run(
        [helm, "template", release, str(archive), *common],
        check=False,
        capture_output=True,
        text=True,
    )
    if rendered.returncode != 0:
        raise ArtifactError(f"helm template failed for {archive}: {rendered.stderr.strip()}")
    try:
        resources = [item for item in yaml.safe_load_all(rendered.stdout) if item is not None]
    except yaml.YAMLError as error:
        raise ArtifactError(f"helm produced invalid YAML for {archive}: {error}") from error
    if not resources or any(not isinstance(item, dict) for item in resources):
        raise ArtifactError(f"helm produced no Kubernetes resources for {archive}")


def validate_with_helm(root: Path, *, strict: bool) -> bool:
    helm = _command_path("HELM", "helm")
    monitoring_value = os.environ.get("KIP_KUBE_PROMETHEUS_STACK_CHART_ARCHIVE", "").strip()
    opencost_value = os.environ.get("KIP_OPENCOST_CHART_ARCHIVE", "").strip()
    if helm is None or not monitoring_value or not opencost_value:
        if strict:
            raise ArtifactError(
                "strict validation requires pinned Helm and both exact chart archives; run "
                "`make artifact-validator-setup` or configure HELM, "
                "KIP_KUBE_PROMETHEUS_STACK_CHART_ARCHIVE, and "
                "KIP_OPENCOST_CHART_ARCHIVE explicitly"
            )
        return False
    helm_version_result = subprocess.run(
        [helm, "version", "--template", "{{.Version}}"],
        check=False,
        capture_output=True,
        text=True,
    )
    actual_helm_version = helm_version_result.stdout.strip()
    expected_helm_version = f"v{_cluster_version_pin(root, 'KIP_HELM_VERSION')}"
    if helm_version_result.returncode != 0 or actual_helm_version != expected_helm_version:
        raise ArtifactError(
            f"Helm version mismatch: expected {expected_helm_version}, "
            f"got {actual_helm_version or 'unknown'}"
        )
    _run_helm_chart(
        helm,
        Path(monitoring_value).expanduser(),
        expected_name="kube-prometheus-stack",
        expected_version=_version_pin(root, "KIP_KUBE_PROMETHEUS_STACK_VERSION"),
        expected_sha256=_validator_checksum_pin(
            root, "KIP_VALIDATOR_KUBE_PROMETHEUS_STACK_ARCHIVE_SHA256"
        ),
        release="kip-monitoring",
        values=root / "deploy/observability/values/kube-prometheus-stack.yaml",
    )
    _run_helm_chart(
        helm,
        Path(opencost_value).expanduser(),
        expected_name="opencost",
        expected_version=_version_pin(root, "KIP_OPENCOST_CHART_VERSION"),
        expected_sha256=_validator_checksum_pin(root, "KIP_VALIDATOR_OPENCOST_ARCHIVE_SHA256"),
        release="kip-opencost",
        values=root / "deploy/observability/values/opencost-unpriced.yaml",
    )
    return True


def _git_eligible_paths(root: Path) -> list[Path]:
    completed = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=root,
        check=False,
        capture_output=True,
    )
    if completed.returncode != 0:
        raise ArtifactError("git ls-files failed while scanning publishable artifacts")
    return [root / value.decode() for value in completed.stdout.split(b"\0") if value]


def _path_is_prohibited(relative: PurePosixPath) -> bool:
    parts = relative.parts
    name = relative.name
    if any(part in {"credentials", "secrets"} for part in parts):
        return True
    if name == ".env" or (name.startswith(".env.") and name != ".env.example"):
        return True
    if name == "kubeconfig" or name.startswith("kubeconfig."):
        return True
    if relative.suffix.lower() in {".key", ".pem", ".trace", ".jsonl", ".har", ".ndjson"}:
        return True
    if parts[:2] == ("benchmarks", ".work"):
        return True
    if "raw" in parts and parts[:2] == ("benchmarks", "reports"):
        return True
    return name.startswith("inventory.") and name.endswith(".local.yaml")


def _validate_report_path(relative: PurePosixPath) -> None:
    parts = relative.parts
    if parts == ("benchmarks", "reports", ".gitkeep"):
        return
    tail = parts[3:]
    if len(parts) < 4 or not tail:
        raise ArtifactError(f"{relative}: invalid published benchmark report path")
    allowed_root = {"index.html", "summary.json", "summary.csv", "reproducibility.json"}
    if len(tail) == 1 and tail[0] in allowed_root:
        return
    if len(tail) == 2 and tail[0] == "charts" and tail[1].endswith(".svg"):
        return
    raise ArtifactError(f"{relative}: file type is not allowed in a published benchmark report")


def validate_candidate_paths(root: Path, paths: Iterable[Path]) -> int:
    count = 0
    for path in paths:
        try:
            relative = PurePosixPath(path.relative_to(root).as_posix())
        except ValueError as error:
            raise ArtifactError(f"artifact escapes repository root: {path}") from error
        if _path_is_prohibited(relative):
            raise ArtifactError(
                f"{relative}: prohibited raw, credential, or machine-local artifact"
            )
        if relative.parts[:2] == ("benchmarks", "reports"):
            _validate_report_path(relative)
        if not path.is_file() or path.is_symlink():
            continue
        try:
            content = path.read_bytes()
        except OSError as error:
            raise ArtifactError(f"cannot inspect {relative}: {error}") from error
        for pattern in _SECRET_PATTERNS:
            if pattern.search(content):
                raise ArtifactError(f"{relative}: content matches a credential signature")
        if relative.parts[:2] == ("benchmarks", "reports"):
            if _IPV4.search(content):
                raise ArtifactError(f"{relative}: published report contains an IPv4 address")
            if _REPORT_SENSITIVE_KEYS.search(content):
                raise ArtifactError(f"{relative}: published report contains a sensitive raw field")
        count += 1
    return count


def validate_repository(root: Path, *, strict_external: bool) -> list[str]:
    messages = []
    dashboards = validate_dashboards(root)
    messages.append(f"PASS dashboard schemas ({dashboards} dashboards)")
    profiles = validate_profiles(root)
    messages.append(f"PASS profile schemas ({profiles} profiles/suites)")
    rules, native_rules = validate_prometheus_rules(root)
    messages.append(f"PASS Prometheus rule structure ({rules} rules)")
    yaml_documents = validate_yaml_and_helm_values(root)
    messages.append(f"PASS Helm values and manifest YAML ({yaml_documents} documents)")
    if validate_with_promtool(root, native_rules, strict=strict_external):
        messages.append("PASS promtool semantic rule validation")
    else:
        messages.append("NOT RUN promtool semantic validation (explicit structural-only mode)")
    if validate_with_helm(root, strict=strict_external):
        messages.append("PASS exact cached upstream Helm rendering")
    else:
        messages.append("NOT RUN upstream Helm rendering (explicit structural-only mode)")
    eligible = validate_candidate_paths(root, _git_eligible_paths(root))
    messages.append(f"PASS prohibited artifact/credential scan ({eligible} Git-eligible files)")
    return messages


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--strict-external", action="store_true")
    mode.add_argument(
        "--structural-only",
        action="store_true",
        help="explicitly omit promtool and upstream Helm rendering (never used by make check)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    strict = not arguments.structural_only
    if arguments.strict_external or os.environ.get("KIP_REQUIRE_EXTERNAL_VALIDATORS") == "1":
        strict = True
    try:
        messages = validate_repository(arguments.root.resolve(), strict_external=strict)
    except ArtifactError as error:
        print(f"FAIL {error}", file=sys.stderr)
        return 1
    for message in messages:
        print(message)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
