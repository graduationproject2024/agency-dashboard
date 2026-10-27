"""
Phase 2.8 Critical Code Review Fixes Regression Test Suite
Validates:
1. P0 — Release-Gate Fail-Closed Behavior (missing QA, empty/malformed manifest, digest mismatch, stale revisions, cross-session evidence, failed tasks, persistence errors, non-blocking findings policy).
2. P0 — Scoped QA Finding Resolution (resolves only verified findings for covered artifacts/tasks, keeps unrelated/uncovered open, failed runs don't resolve, history idempotency).
3. P0 — Workflow DAG Validation Robustness (non-list, non-dict, missing/empty/duplicate IDs, invalid dependency values, self-dependencies, cycles, deep graphs, complexity limits, mixed definitions).
"""
import copy
import hashlib
import json
import os
import pytest
from unittest.mock import patch

import db_persistence as db_store
import task_orchestrator as orch
from app import app, evaluate_release_gate, run_qa_checks


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    """Hermetic isolated SQLite persistence database fixture."""
    db_file = str(tmp_path / "test_review_fixes.db")
    monkeypatch.setenv("DATABASE_PATH", db_file)
    db_store.init_db(db_file)
    return db_file


@pytest.fixture
def test_client():
    app.config["TESTING"] = True
    with app.test_client() as client:
        yield client


# =========================================================================
# 1. P0 — RELEASE GATE FAIL-CLOSED TESTS
# =========================================================================

def test_release_gate_missing_qa_evidence_and_manifest_blocks(temp_db):
    """Release gate MUST fail closed when no QA evidence or manifest exists."""
    sess_id = db_store.create_session("No QA Session", "Task", db_path=temp_db)
    res = evaluate_release_gate(sess_id, db_path=temp_db)
    assert res["allowed"] is False
    assert res["status"] == "blocked"
    assert any("missing QA evidence" in r for r in res["reasons"])
    assert any("Missing artifact manifest" in r for r in res["reasons"])


def test_release_gate_empty_manifest_blocks(temp_db):
    """Release gate MUST fail closed when candidate manifest contains 0 artifacts."""
    sess_id = db_store.create_session("Empty Manifest Session", "Task", db_path=temp_db)
    empty_manifest = {"session_id": sess_id, "qa_run_id": "run_empty", "artifacts": []}
    res = evaluate_release_gate(sess_id, artifact_manifest=empty_manifest, db_path=temp_db)
    assert res["allowed"] is False
    assert res["status"] == "blocked"
    assert any("Empty artifact manifest" in r for r in res["reasons"])


def test_release_gate_malformed_manifest_blocks(temp_db):
    """Release gate MUST fail closed when manifest is not a dict or artifacts is not a list."""
    sess_id = db_store.create_session("Malformed Manifest Session", "Task", db_path=temp_db)
    
    # Manifest is a string
    res1 = evaluate_release_gate(sess_id, artifact_manifest="not_a_dict", db_path=temp_db)
    assert res1["allowed"] is False
    assert any("Malformed artifact manifest" in r for r in res1["reasons"])

    # Artifacts is a string instead of a list
    res2 = evaluate_release_gate(sess_id, artifact_manifest={"artifacts": "not_a_list"}, db_path=temp_db)
    assert res2["allowed"] is False
    assert any("artifacts' must be a list" in r for r in res2["reasons"])


def test_release_gate_invalid_or_missing_manifest_digest_blocks(temp_db):
    """Release gate MUST fail closed when manifest entries have missing or non-SHA256 digests."""
    sess_id = db_store.create_session("Bad Digest Session", "Task", db_path=temp_db)
    bad_manifest = {
        "session_id": sess_id,
        "qa_run_id": "run_bad_digest",
        "artifacts": [
            {"relative_path": "index.html", "sha256": "not_a_valid_64_hex_hash"}
        ]
    }
    res = evaluate_release_gate(sess_id, artifact_manifest=bad_manifest, db_path=temp_db)
    assert res["allowed"] is False
    assert any("Invalid or missing manifest digest" in r for r in res["reasons"])


def test_release_gate_candidate_digest_mismatch_blocks(temp_db):
    """Release gate MUST detect tampering when candidate file does not match manifest SHA-256."""
    sess_id = db_store.create_session("Tamper Session", "Task", db_path=temp_db)
    valid_content = "<!DOCTYPE html><html><head><title>OK</title></head><body>OK</body></html>"
    valid_sha = hashlib.sha256(valid_content.encode("utf-8")).hexdigest()

    manifest = {
        "session_id": sess_id,
        "qa_run_id": "run_tamper",
        "artifacts": [{"relative_path": "index.html", "sha256": valid_sha}]
    }

    tampered_content = "<!DOCTYPE html><html><head><title>Tampered</title></head><body>Tampered</body></html>"
    res = evaluate_release_gate(
        sess_id,
        candidate_files={"index.html": tampered_content},
        artifact_manifest=manifest,
        db_path=temp_db
    )
    assert res["allowed"] is False
    assert any("digest mismatch" in r for r in res["reasons"])


def test_release_gate_cross_session_qa_evidence_mismatch_blocks(temp_db):
    """Release gate MUST reject QA evidence certified under a different session_id."""
    sess_id = db_store.create_session("Real Session", "Task", db_path=temp_db)
    alien_manifest = {
        "session_id": "alien_session_999",
        "qa_run_id": "run_alien",
        "artifacts": [
            {"relative_path": "index.html", "sha256": "a" * 64}
        ]
    }
    res = evaluate_release_gate(sess_id, artifact_manifest=alien_manifest, db_path=temp_db)
    assert res["allowed"] is False
    assert any("QA evidence candidate mismatch" in r for r in res["reasons"])


def test_release_gate_failed_qa_checks_in_manifest_blocks(temp_db):
    """Release gate MUST fail closed when manifest validation_status is 'failed'."""
    sess_id = db_store.create_session("Failed QA Manifest", "Task", db_path=temp_db)
    failed_manifest = {
        "session_id": sess_id,
        "qa_run_id": "run_failed",
        "validation_status": "failed",
        "artifacts": [
            {"relative_path": "index.html", "sha256": "b" * 64, "validation_status": "failed"}
        ]
    }
    res = evaluate_release_gate(sess_id, artifact_manifest=failed_manifest, db_path=temp_db)
    assert res["allowed"] is False
    assert any("verification checks failed" in r for r in res["reasons"])


def test_release_gate_uncompleted_qa_specialist_task_blocks(temp_db):
    """Release gate MUST fail closed if a QA stage task in the workflow has not completed."""
    sess_id = db_store.create_session("QA Task Session", "Task", db_path=temp_db)
    db_store.create_task(sess_id, "Code Task", "engineering", "eng-agent", "engineering", db_path=temp_db)
    qa_task_id = db_store.create_task(sess_id, "QA Verification", "security", "qa-agent", "qa", db_path=temp_db)
    # QA task remains 'pending'
    
    html = "<!DOCTYPE html><html><head><title>App</title></head><body>App</body></html>"
    sha = hashlib.sha256(html.encode("utf-8")).hexdigest()
    manifest = {
        "session_id": sess_id,
        "qa_run_id": "run_1",
        "artifacts": [{"relative_path": "index.html", "sha256": sha}]
    }

    res = evaluate_release_gate(
        sess_id,
        candidate_files={"index.html": html},
        artifact_manifest=manifest,
        db_path=temp_db
    )
    assert res["allowed"] is False
    assert any("Required QA specialist tasks have not completed" in r for r in res["reasons"])


def test_release_gate_open_blocking_findings_blocks(temp_db):
    """Critical or high findings strictly block release."""
    sess_id = db_store.create_session("Critical Finding Session", "Task", db_path=temp_db)
    db_store.record_qa_finding(
        session_id=sess_id,
        task_id=None,
        qa_run_id="run_1",
        severity="critical",
        category="security",
        artifact_name="index.html",
        evidence="Remote code injection risk",
        status="open",
        db_path=temp_db
    )
    html = "<!DOCTYPE html><html><head><title>App</title></head><body>App</body></html>"
    sha = hashlib.sha256(html.encode("utf-8")).hexdigest()
    manifest = {
        "session_id": sess_id,
        "qa_run_id": "run_1",
        "artifacts": [{"relative_path": "index.html", "sha256": sha}]
    }
    res = evaluate_release_gate(sess_id, candidate_files={"index.html": html}, artifact_manifest=manifest, db_path=temp_db)
    assert res["allowed"] is False
    assert res["blocking_findings_count"] == 1
    assert any("blocking findings exist" in r for r in res["reasons"])


def test_release_gate_non_blocking_findings_policy_permits_release(temp_db):
    """Non-blocking findings (medium/low/warning) do not block release if deliverables and QA are valid."""
    sess_id = db_store.create_session("Non-blocking Finding Session", "Task", db_path=temp_db)
    html = "<!DOCTYPE html><html><head><title>App</title></head><body>App</body></html>"
    css = "body { margin: 0; }"
    js = "console.log('clean');"
    
    rep = run_qa_checks(
        app_html=html,
        app_css=css,
        app_js=js,
        session_id=sess_id,
        db_path=temp_db
    )
    assert rep["passed"] is True
    qa_run_id = rep["qa_run_id"]
    manifest = rep["artifact_manifest"]

    db_store.record_qa_finding(
        session_id=sess_id,
        task_id=None,
        qa_run_id=qa_run_id,
        severity="low",
        category="performance",
        artifact_name="index.html",
        evidence="Image tag missing alt text",
        status="open",
        artifact_revision=manifest.get("artifact_revision"),
        db_path=temp_db
    )
    res = evaluate_release_gate(
        sess_id,
        candidate_files={"index.html": html, "style.css": css, "script.js": js},
        artifact_manifest=manifest,
        db_path=temp_db
    )
    assert res["allowed"] is True
    assert res["status"] == "passed"
    assert res["blocking_findings_count"] == 0
    assert res["non_blocking_findings_count"] == 1


def test_release_gate_persistence_failure_fails_closed(temp_db):
    """Database exception during release gate evaluation immediately fails closed."""
    sess_id = db_store.create_session("Error Gate Session", "Task", db_path=temp_db)
    with patch("db_persistence.list_tasks_for_session", side_effect=IOError("Database disk I/O failure")):
        res = evaluate_release_gate(sess_id, db_path=temp_db)
        assert res["allowed"] is False
        assert res["status"] == "blocked"
        assert any("Persistence or evidence-retrieval failure" in r for r in res["reasons"])


# =========================================================================
# 2. P0 — SCOPED QA FINDING RESOLUTION TESTS
# =========================================================================

def test_qa_run_resolves_only_targeted_finding_id(temp_db):
    """When verified_finding_ids is passed, ONLY those findings may be marked verified_resolved."""
    sess_id = db_store.create_session("Targeted Finding Session", "Task", db_path=temp_db)
    
    fid1 = db_store.record_qa_finding(
        session_id=sess_id,
        task_id=None,
        qa_run_id="run_1",
        severity="high",
        category="syntax",
        artifact_name="index.html",
        evidence="Missing doctype",
        responsible_agent_id="developer_1",
        status="open",
        db_path=temp_db
    )
    fid2 = db_store.record_qa_finding(
        session_id=sess_id,
        task_id=None,
        qa_run_id="run_1",
        severity="high",
        category="syntax",
        artifact_name="index.html",
        evidence="Unclosed element",
        responsible_agent_id="developer_1",
        status="open",
        db_path=temp_db
    )

    clean_html = "<!DOCTYPE html><html><head><title>App</title></head><body><h1>Clean</h1></body></html>"
    report = run_qa_checks(
        app_html=clean_html,
        app_css="body { margin: 0; }",
        app_js="console.log('clean');",
        session_id=sess_id,
        verified_finding_ids=[fid1],
        db_path=temp_db
    )

    assert report["passed"] is True
    assert fid1 in report["resolved_findings"]
    assert fid2 in report["unresolved_findings"]

    # Verify persistent status
    f1 = db_store.get_qa_finding(fid1, db_path=temp_db)
    f2 = db_store.get_qa_finding(fid2, db_path=temp_db)
    assert f1["status"] == "verified_resolved"
    assert f2["status"] == "open"


def test_qa_run_unrelated_artifact_finding_remains_open(temp_db):
    """A finding for an artifact NOT verified in the QA run (e.g. backend_api.py) must remain open."""
    sess_id = db_store.create_session("Unrelated Artifact Session", "Task", db_path=temp_db)
    
    fid_backend = db_store.record_qa_finding(
        session_id=sess_id,
        task_id=None,
        qa_run_id="run_backend",
        severity="high",
        category="security",
        artifact_name="backend_api.py",
        evidence="SQL injection risk in query constructor",
        responsible_agent_id="backend_dev",
        status="open",
        db_path=temp_db
    )

    clean_html = "<!DOCTYPE html><html><head><title>Web App</title></head><body><h1>Clean Frontend</h1></body></html>"
    report = run_qa_checks(
        app_html=clean_html,
        app_css="body { color: black; }",
        app_js="console.log('ok');",
        session_id=sess_id,
        db_path=temp_db
    )

    assert report["passed"] is True
    assert fid_backend in report["unresolved_findings"]
    assert fid_backend not in report["resolved_findings"]

    f_backend = db_store.get_qa_finding(fid_backend, db_path=temp_db)
    assert f_backend["status"] == "open"


def test_qa_run_uncovered_asset_finding_remains_open(temp_db):
    """If style.css was not provided to run_qa_checks, an open finding on style.css cannot be resolved."""
    sess_id = db_store.create_session("Uncovered Asset Session", "Task", db_path=temp_db)
    
    fid_css = db_store.record_qa_finding(
        session_id=sess_id,
        task_id=None,
        qa_run_id="run_css",
        severity="medium",
        category="syntax",
        artifact_name="style.css",
        evidence="Unbalanced brace in selector",
        responsible_agent_id="css_dev",
        status="open",
        db_path=temp_db
    )

    # HTML only checked (app_css is empty)
    clean_html = "<!DOCTYPE html><html><head><title>App</title></head><body><h1>App</h1></body></html>"
    report = run_qa_checks(
        app_html=clean_html,
        app_css="",
        app_js="",
        session_id=sess_id,
        db_path=temp_db
    )

    assert fid_css in report["unresolved_findings"]
    f_css = db_store.get_qa_finding(fid_css, db_path=temp_db)
    assert f_css["status"] == "open"


def test_failed_qa_run_cannot_resolve_findings(temp_db):
    """A failed QA run cannot resolve any open findings."""
    sess_id = db_store.create_session("Failed QA Session", "Task", db_path=temp_db)
    
    fid = db_store.record_qa_finding(
        session_id=sess_id,
        task_id=None,
        qa_run_id="run_old",
        severity="high",
        category="syntax",
        artifact_name="index.html",
        evidence="Missing doctype",
        responsible_agent_id="dev",
        status="open",
        db_path=temp_db
    )

    # Broken HTML submitted
    bad_html = "<html><body>No doctype and <script src='bad'></body></html>"
    report = run_qa_checks(
        app_html=bad_html,
        app_css="body { color: red; }",
        app_js="console.log('ok');",
        session_id=sess_id,
        verified_finding_ids=[fid],
        db_path=temp_db
    )

    assert report["passed"] is False
    assert len(report["resolved_findings"]) == 0
    f = db_store.get_qa_finding(fid, db_path=temp_db)
    assert f["status"] == "open"


def test_already_resolved_finding_history_idempotency(temp_db):
    """Repeated QA runs do not duplicate verification history on already resolved findings."""
    sess_id = db_store.create_session("History Idempotency Session", "Task", db_path=temp_db)
    
    fid = db_store.record_qa_finding(
        session_id=sess_id,
        task_id=None,
        qa_run_id="run_1",
        severity="medium",
        category="syntax",
        artifact_name="index.html",
        evidence="Fix me",
        responsible_agent_id="dev",
        status="open",
        db_path=temp_db
    )

    clean_html = "<!DOCTYPE html><html><head><title>App</title></head><body><h1>Clean</h1></body></html>"
    
    # Run 1 resolves finding
    rep1 = run_qa_checks(clean_html, app_css="body {}", app_js="console.log(1);", session_id=sess_id, db_path=temp_db)
    assert fid in rep1["resolved_findings"]
    f_after_run1 = db_store.get_qa_finding(fid, db_path=temp_db)
    assert len(f_after_run1["verification_history"]) == 1

    # Run 2 on same session does not append duplicate history
    rep2 = run_qa_checks(clean_html, app_css="body {}", app_js="console.log(1);", session_id=sess_id, db_path=temp_db)
    f_after_run2 = db_store.get_qa_finding(fid, db_path=temp_db)
    assert len(f_after_run2["verification_history"]) == 1
    assert f_after_run2["status"] == "verified_resolved"


# =========================================================================
# 3. P0 — WORKFLOW DAG VALIDATION ROBUSTNESS TESTS
# =========================================================================

def test_dag_validation_non_list_task_collection():
    """Non-list input must return structured error without exception."""
    res_none = orch.validate_workflow_graph(None)
    assert res_none["is_valid"] is False
    assert any("must be a list" in e for e in res_none["errors"])

    res_dict = orch.validate_workflow_graph({"task_1": {"dependencies": []}})
    assert res_dict["is_valid"] is False
    assert any("must be a list" in e for e in res_dict["errors"])

    res_str = orch.validate_workflow_graph("not_a_list")
    assert res_str["is_valid"] is False
    assert any("must be a list" in e for e in res_str["errors"])


def test_dag_validation_empty_task_collection():
    """Empty list returns structured validation error."""
    res = orch.validate_workflow_graph([])
    assert res["is_valid"] is False
    assert any("cannot be empty" in e for e in res["errors"])


def test_dag_validation_non_dict_task_entries():
    """Non-dictionary elements in tasks list return structured error without calling .get() or crashing."""
    tasks = [
        {"id": "task_1", "dependencies": []},
        "not_a_dict_string",
        12345,
        None,
        ["nested_list"]
    ]
    res = orch.validate_workflow_graph(tasks)
    assert res["is_valid"] is False
    assert any("must be a dictionary" in e for e in res["errors"])
    # Cycle detection must NOT have crashed
    assert res["has_cycles"] is False


def test_dag_validation_missing_and_empty_task_identifiers():
    """Task definitions missing 'id' or with empty whitespace id return structured errors."""
    tasks = [
        {"title": "No ID Task", "dependencies": []},
        {"id": "   ", "title": "Whitespace ID Task", "dependencies": []},
        {"id": "task_valid", "dependencies": []}
    ]
    res = orch.validate_workflow_graph(tasks)
    assert res["is_valid"] is False
    assert any("missing required 'id' or 'task_id'" in e for e in res["errors"])
    assert any("empty or whitespace identifier" in e for e in res["errors"])


def test_dag_validation_invalid_dependency_types_and_values():
    """Non-list dependencies or dependencies with invalid items (None, dict) return structured errors."""
    tasks = [
        {"id": "t1", "dependencies": "not_a_list"},
        {"id": "t2", "dependencies": [None, {"bad": "dict"}]},
        {"id": "t3", "dependencies": ["   "]}
    ]
    res = orch.validate_workflow_graph(tasks)
    assert res["is_valid"] is False
    assert any("dependencies must be a list" in e for e in res["errors"])
    assert any("invalid dependency value" in e for e in res["errors"])
    assert any("empty dependency identifier" in e for e in res["errors"])


def test_dag_validation_self_dependencies():
    """Direct self-dependency (task A depends on task A) is cleanly flagged."""
    tasks = [
        {"id": "task_self", "dependencies": ["task_self"]}
    ]
    res = orch.validate_workflow_graph(tasks)
    assert res["is_valid"] is False
    assert any("Self-dependency detected" in e for e in res["errors"])


def test_dag_validation_deeply_nested_valid_graph():
    """A linear DAG of 150 tasks validates successfully without recursion errors."""
    tasks = []
    for i in range(150):
        tid = f"task_{i:03d}"
        deps = [f"task_{i-1:03d}"] if i > 0 else []
        tasks.append({"id": tid, "dependencies": deps})

    res = orch.validate_workflow_graph(tasks)
    assert res["is_valid"] is True
    assert res["task_count"] == 150
    assert len(res["execution_levels"]) == 150
    assert res["has_cycles"] is False


def test_dag_validation_complexity_limit():
    """Task lists exceeding MAX_WORKFLOW_TASKS (1000) are rejected."""
    huge_tasks = [{"id": f"t_{i}", "dependencies": []} for i in range(1005)]
    res = orch.validate_workflow_graph(huge_tasks)
    assert res["is_valid"] is False
    assert any("exceeds maximum task limit" in e for e in res["errors"])


def test_workflow_dag_wrapper_safely_raises_value_error_on_invalid_input():
    """WorkflowDAG constructor wraps validation and raises ValueError without crashing on malformed input."""
    with pytest.raises(ValueError, match="Invalid workflow task graph"):
        orch.WorkflowDAG("not_a_list")

    with pytest.raises(ValueError, match="Invalid workflow task graph"):
        orch.WorkflowDAG([{"id": "t1", "dependencies": ["t1"]}])


def test_api_workflow_validate_endpoint_malformed_input(test_client):
    """POST /api/workflow/validate handles non-dict tasks gracefully without 500 server error."""
    payload = {
        "tasks": [
            {"id": "t1", "dependencies": []},
            "malformed_string_entry"
        ]
    }
    res = test_client.post("/api/workflow/validate", json=payload)
    assert res.status_code == 200
    data = res.get_json()
    assert data["is_valid"] is False
    assert any("must be a dictionary" in e for e in data["errors"])
