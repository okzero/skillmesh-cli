"""Tests for the auditable L4 delivery evidence gate."""
import hashlib
import json
from pathlib import Path

import pytest

from scripts import l4_gate


def _write_run(tmp_path: Path, *, interventions=0, check_status="passed"):
    spec = tmp_path / "private-spec.md"
    spec.write_text("internal requirement\n", encoding="utf-8")
    run_dir = tmp_path / "run-001"
    run_dir.mkdir()
    spec_hash = hashlib.sha256(spec.read_bytes()).hexdigest()
    (run_dir / "run.json").write_text(json.dumps({
        "run_id": "run-001", "spec_sha256": spec_hash,
        "human_interventions": interventions,
        "status": "ready_for_acceptance",
        "files_changed": ["skillmesh/config.py"],
    }), encoding="utf-8")
    (run_dir / "plan.md").write_text("# Plan\n", encoding="utf-8")
    (run_dir / "commands.jsonl").write_text(
        json.dumps({"command": "python3 -m pytest", "exit_code": 0}) + "\n",
        encoding="utf-8",
    )
    (run_dir / "tests.json").write_text(json.dumps({
        "checks": [{"name": "pytest", "status": check_status}],
    }), encoding="utf-8")
    (run_dir / "review.md").write_text(
        "# Review\n\nverdict: PASS\n", encoding="utf-8"
    )
    (run_dir / "final.json").write_text(json.dumps({
        "result": "ready_for_acceptance", "risks": [],
    }), encoding="utf-8")
    return run_dir, spec


def test_validate_run_and_render_sanitized_summary(tmp_path):
    run_dir, spec = _write_run(tmp_path)
    evidence = l4_gate.validate_run(run_dir, spec)
    summary = l4_gate.render_summary(evidence)
    assert evidence["result"] == "ready_for_acceptance"
    assert "Human interventions: `0`" in summary
    assert str(spec) not in summary
    assert "`config.py`" in summary


def test_gate_rejects_human_intervention(tmp_path):
    run_dir, spec = _write_run(tmp_path, interventions=1)
    with pytest.raises(l4_gate.GateError, match="human_interventions"):
        l4_gate.validate_run(run_dir, spec)


def test_gate_rejects_failed_check(tmp_path):
    run_dir, spec = _write_run(tmp_path, check_status="failed")
    with pytest.raises(l4_gate.GateError, match="quality gates"):
        l4_gate.validate_run(run_dir, spec)


def test_gate_rejects_changed_spec(tmp_path):
    run_dir, spec = _write_run(tmp_path)
    spec.write_text("changed requirement\n", encoding="utf-8")
    with pytest.raises(l4_gate.GateError, match="spec hash"):
        l4_gate.validate_run(run_dir, spec)


def test_gate_requires_pass_review(tmp_path):
    run_dir, spec = _write_run(tmp_path)
    (run_dir / "review.md").write_text("verdict: FAIL\n", encoding="utf-8")
    with pytest.raises(l4_gate.GateError, match="verdict: PASS"):
        l4_gate.validate_run(run_dir, spec)


def test_pr_body_contract():
    body = """## L4 Delivery Evidence
- Spec SHA-256: `aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa`
- Human interventions: `0`
### Quality Gates
- [x] pytest
### Review and Risks
- None
### Human Acceptance
- [ ] Approve
"""
    l4_gate.check_pr_body(body)


def test_pr_body_rejects_missing_evidence():
    with pytest.raises(l4_gate.GateError, match="missing headings"):
        l4_gate.check_pr_body("ordinary pull request")
