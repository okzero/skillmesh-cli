#!/usr/bin/env python3
"""Validate private L4 run evidence and generate a sanitized PR summary."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional


REQUIRED_FILES = (
    "run.json", "plan.md", "commands.jsonl", "tests.json", "review.md",
    "final.json",
)
REQUIRED_PR_HEADINGS = (
    "## L4 Delivery Evidence", "### Quality Gates",
    "### Review and Risks", "### Human Acceptance",
)
READY = "ready_for_acceptance"


class GateError(Exception):
    pass


def _read_json(path: Path) -> Dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise GateError(f"invalid JSON {path.name}: {exc}") from exc
    if not isinstance(value, dict):
        raise GateError(f"{path.name} must contain a JSON object")
    return value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_commands(path: Path) -> int:
    count = 0
    for line_number, line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), 1
    ):
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError as exc:
            raise GateError(f"commands.jsonl:{line_number}: {exc}") from exc
        if not isinstance(item, dict) or not item.get("command"):
            raise GateError(f"commands.jsonl:{line_number}: missing command")
        if item.get("exit_code") is None:
            raise GateError(f"commands.jsonl:{line_number}: missing exit_code")
        count += 1
    if count == 0:
        raise GateError("commands.jsonl must record at least one command")
    return count


def validate_run(run_dir: Path, spec_path: Path) -> Dict[str, Any]:
    if not spec_path.is_file():
        raise GateError(f"spec does not exist: {spec_path}")
    missing = [name for name in REQUIRED_FILES if not (run_dir / name).is_file()]
    if missing:
        raise GateError(f"missing run evidence: {', '.join(missing)}")

    run = _read_json(run_dir / "run.json")
    tests = _read_json(run_dir / "tests.json")
    final = _read_json(run_dir / "final.json")
    expected_hash = _sha256(spec_path)
    if run.get("spec_sha256") != expected_hash:
        raise GateError("spec hash does not match run.json")
    if run.get("human_interventions") != 0:
        raise GateError("human_interventions must be exactly 0")
    if run.get("status") != READY or final.get("result") != READY:
        raise GateError("run and final result must be ready_for_acceptance")
    if not run.get("run_id") or run.get("run_id") != run_dir.name:
        raise GateError("run_id must match the run directory name")
    if not isinstance(run.get("files_changed"), list):
        raise GateError("run.json files_changed must be an array")
    if not (run_dir / "plan.md").read_text(encoding="utf-8").strip():
        raise GateError("plan.md must not be empty")

    review = (run_dir / "review.md").read_text(encoding="utf-8")
    if not re.search(r"(?im)^verdict:\s*PASS\s*$", review):
        raise GateError("review.md must contain 'verdict: PASS'")

    checks = tests.get("checks")
    if not isinstance(checks, list) or not checks:
        raise GateError("tests.json checks must be a non-empty array")
    failed = [
        str(item.get("name", "unnamed"))
        for item in checks
        if not isinstance(item, dict) or item.get("status") != "passed"
    ]
    if failed:
        raise GateError(f"quality gates not passed: {', '.join(failed)}")
    command_count = _validate_commands(run_dir / "commands.jsonl")
    risks = final.get("risks", [])
    if not isinstance(risks, list):
        raise GateError("final.json risks must be an array")

    return {
        "run_id": run["run_id"], "spec_sha256": expected_hash,
        "files_changed": run["files_changed"], "checks": checks,
        "command_count": command_count, "risks": risks,
        "human_interventions": 0, "result": READY,
    }


def render_summary(evidence: Dict[str, Any]) -> str:
    files = evidence["files_changed"] or ["None"]
    risks = evidence["risks"] or ["None identified"]
    lines: List[str] = [
        "## L4 Delivery Evidence", "",
        f"- Run ID: `{evidence['run_id']}`",
        f"- Spec SHA-256: `{evidence['spec_sha256']}`",
        f"- Human interventions: `{evidence['human_interventions']}`",
        f"- Recorded commands: `{evidence['command_count']}`",
        "- Changed files: " + ", ".join(
            f"`{Path(str(item)).name}`" for item in files
        ), "", "### Quality Gates", "",
    ]
    lines.extend(f"- [x] {item['name']}" for item in evidence["checks"])
    lines.extend(["", "### Review and Risks", ""])
    lines.extend(f"- {str(item)}" for item in risks)
    lines.extend([
        "", "### Human Acceptance", "", "- [ ] Final diff reviewed",
        "- [ ] Approve or reject this pull request", "",
    ])
    return "\n".join(lines)


def check_pr_body(body: str) -> None:
    missing = [heading for heading in REQUIRED_PR_HEADINGS if heading not in body]
    if missing:
        raise GateError(f"PR body missing headings: {', '.join(missing)}")
    if not re.search(r"Human interventions:\s*`0`", body):
        raise GateError("PR body must declare Human interventions: `0`")
    if not re.search(r"Spec SHA-256:\s*`[0-9a-f]{64}`", body):
        raise GateError("PR body must contain a valid spec SHA-256")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    validate_parser = subparsers.add_parser("validate")
    validate_parser.add_argument("--run-dir", type=Path, required=True)
    validate_parser.add_argument("--spec", type=Path, required=True)
    validate_parser.add_argument("--summary-out", type=Path)
    pr_parser = subparsers.add_parser("check-pr")
    pr_parser.add_argument("--event", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "validate":
            evidence = validate_run(args.run_dir, args.spec)
            summary = render_summary(evidence)
            if args.summary_out:
                args.summary_out.parent.mkdir(parents=True, exist_ok=True)
                args.summary_out.write_text(summary, encoding="utf-8")
            print(summary)
        else:
            event = _read_json(args.event)
            body = event.get("pull_request", {}).get("body") or ""
            check_pr_body(body)
            print("L4 PR evidence: valid")
    except GateError as exc:
        print(f"L4 gate failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
