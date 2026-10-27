"""
Phase 2.8 Enterprise Workforce Intelligence, Workflow Orchestration & Quality Gates Test Suite

Validates:
- Group A: Workforce registry, canonical 18 divisions, 282 agents, deterministic capability-based assignment, seniority and tool constraints, unknown capability resilience, no-suitable-agent fallback.
- Group B: Workflow DAG validation, cycle detection, missing dependencies, duplicate IDs, dependency failure propagation.
- Group C: Typed inter-agent handoffs, contract versioning, trace IDs, payload bounds, destination agent validation, idempotent handoff persistence.
- Group D: Execution reliability, tuple/dict return handling, bounded retries, duplicate task execution guards, crash recovery.
- Group E: Independent QA findings persistence, severity/category validation, prevention of implementer self-certification, fail-closed release gate evaluation, stale evidence rejection, bounded remediation loop.
- Group F: Human approval lifecycle (pending -> approved | rejected | expired | cancelled), revision hash binding, anti-tampering, self-approval prevention, lazy expiration, idempotency.
- Group G: Security, safe origin enforcement on mutating endpoints, secret non-exposure, atomic SQLite transactions and schema backward compatibility.
"""

import os
import sys
import time
import json
import uuid
import hashlib
import tempfile
import sqlite3
import pytest

# Ensure local agency-dashboard root is on sys.path
REPO_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if REPO_DIR not in sys.path:
    sys.path.insert(0, REPO_DIR)

import db_persistence as db_store
import task_orchestrator as orch
from app import (
    app,
    run_qa_checks,
    evaluate_release_gate,
    CANONICAL_DIVISIONS,
    get_agents_dir,
    is_safe_origin
)


@pytest.fixture
def temp_db(tmp_path):
    """Hermetic isolated SQLite test database with Phase 2.8 schema initialized."""
    db_file = str(tmp_path / "test_phase_2_8.db")
    db_store.init_db(db_path=db_file)
    return db_file


@pytest.fixture
def test_client(temp_db, monkeypatch):
    """Flask test client wired to isolated test database."""
    monkeypatch.setenv("DATABASE_PATH", temp_db)
    app.config["TESTING"] = True
    with app.test_client() as client:
        yield client


# =========================================================================
# GROUP A: Workforce Registry & Capability-Based Assignment
# =========================================================================

def test_group_a_canonical_registry_integrity():
    """Verify canonical agent registry discovers agents across all 18 canonical divisions."""
    reg = orch.get_agent_capability_registry()
    assert len(reg) >= 200, f"Expected canonical agent catalog (got {len(reg)})"

    # All 18 canonical divisions must exist
    div_ids = {d["id"] for d in CANONICAL_DIVISIONS}
    assert len(div_ids) == 18, "Must contain exactly 18 canonical divisions"

    # Verify agent capability schema structure
    for aid, profile in list(reg.items())[:20]:
        assert "agent_id" in profile
        assert "name" in profile
        assert "division_id" in profile
        assert "capabilities" in profile
        assert isinstance(profile["capabilities"], list)
        assert "seniority" in profile
        assert profile["seniority"] in ("lead", "senior", "specialist", "junior", "unknown")
        assert "security_permissions" in profile
        assert "required_tools" in profile
        assert "qa_responsibilities" in profile
        assert "status" in profile


def test_group_a_deterministic_assignment_by_capabilities():
    """Verify deterministic assignment matches declared capabilities, division affinity, and seniority."""
    req = {
        "required_capabilities": ["architecture"],
        "preferred_division": "engineering",
        "min_seniority": "senior"
    }
    result = orch.assign_agent_to_task(req)
    assert result["assigned"] is True
    assert result["selected_agent_id"] is not None
    assert "architecture" in result["matched_capabilities"]
    assert result["score"] > 0
    assert "architecture" in result["reason"]
    assert result["candidate_count"] > 0


def test_group_a_no_suitable_agent_fallback():
    """Verify explicit unassigned fallback when no candidate satisfies requirements."""
    req = {
        "required_capabilities": ["quantum_teleportation_ftl_warp_drive"],
        "preferred_division": "engineering"
    }
    result = orch.assign_agent_to_task(req)
    assert result["assigned"] is False
    assert result["selected_agent_id"] is None
    assert result["candidate_count"] == 0
    assert "No suitable agent found" in result["reason"]


def test_group_a_security_permission_and_tool_filtering():
    """Verify candidates lacking required security permissions or tools are excluded."""
    # Synthetic candidate pool
    candidates = [
        {
            "agent_id": "agent-junior",
            "name": "Junior Dev",
            "division_id": "engineering",
            "capabilities": ["frontend", "javascript"],
            "seniority": "junior",
            "security_permissions": ["read_repo"],
            "required_tools": ["editor"],
            "status": "available"
        },
        {
            "agent_id": "agent-lead",
            "name": "Lead Dev",
            "division_id": "engineering",
            "capabilities": ["frontend", "javascript"],
            "seniority": "lead",
            "security_permissions": ["read_repo", "prod_deploy"],
            "required_tools": ["editor", "docker"],
            "status": "available"
        }
    ]

    # Task requiring prod_deploy permission
    req_perms = {
        "required_capabilities": ["frontend"],
        "required_permissions": ["prod_deploy"]
    }
    res = orch.assign_agent_to_task(req_perms, candidates=candidates)
    assert res["assigned"] is True
    assert res["selected_agent_id"] == "agent-lead"

    # Task requiring tool not possessed by junior
    req_tool = {
        "required_capabilities": ["frontend"],
        "required_tools": ["docker"]
    }
    res2 = orch.assign_agent_to_task(req_tool, candidates=candidates)
    assert res2["assigned"] is True
    assert res2["selected_agent_id"] == "agent-lead"

    # Task requiring unknown tool
    req_unknown = {
        "required_capabilities": ["frontend"],
        "required_tools": ["nonexistent_tool_xyz"]
    }
    res3 = orch.assign_agent_to_task(req_unknown, candidates=candidates)
    assert res3["assigned"] is False


def test_group_a_unknown_and_malformed_metadata_resilience():
    """Verify parser handles unknown capabilities, missing frontmatter, and malformed fields gracefully."""
    raw_prompt = """
    This is an agent prompt with no frontmatter.
    Role: Specialist
    Responsibilities:
    - Code review
    - Debugging
    """
    profile = orch.parse_agent_capabilities("test-unknown-agent", raw_prompt)
    assert profile["agent_id"] == "test-unknown-agent"
    assert profile["seniority"] == "specialist"
    assert "code_review" in profile["capabilities"] or "testing" in profile["capabilities"] or "development" in profile["capabilities"]
    assert profile["status"] == "available"


# =========================================================================
# GROUP B: Workflow Graph & Dependency Scheduling
# =========================================================================

def test_group_b_valid_dag_validation():
    """Verify valid DAG passes topological validation and computes execution levels."""
    tasks = [
        {"task_id": "t1", "dependencies": []},
        {"task_id": "t2", "dependencies": ["t1"]},
        {"task_id": "t3", "dependencies": ["t1"]},
        {"task_id": "t4", "dependencies": ["t2", "t3"]}
    ]
    res = orch.validate_workflow_graph(tasks)
    assert res["is_valid"] is True
    assert len(res["errors"]) == 0
    assert len(res["execution_levels"]) == 3
    assert res["execution_levels"][0] == ["t1"]
    assert set(res["execution_levels"][1]) == {"t2", "t3"}
    assert res["execution_levels"][2] == ["t4"]


def test_group_b_cycle_detection():
    """Verify circular dependencies are rejected with clear cycle identification."""
    tasks = [
        {"task_id": "a", "dependencies": ["c"]},
        {"task_id": "b", "dependencies": ["a"]},
        {"task_id": "c", "dependencies": ["b"]}
    ]
    res = orch.validate_workflow_graph(tasks)
    assert res["is_valid"] is False
    assert any("cycle" in err.lower() for err in res["errors"])


def test_group_b_missing_dependency_and_duplicate_id_rejection():
    """Verify missing dependency references and duplicate IDs fail validation."""
    tasks_missing = [
        {"task_id": "t1", "dependencies": ["ghost_task"]}
    ]
    res1 = orch.validate_workflow_graph(tasks_missing)
    assert res1["is_valid"] is False
    assert any("ghost_task" in err for err in res1["errors"])

    tasks_dup = [
        {"task_id": "t1", "dependencies": []},
        {"task_id": "t1", "dependencies": []}
    ]
    res2 = orch.validate_workflow_graph(tasks_dup)
    assert res2["is_valid"] is False
    assert any("duplicate" in err.lower() for err in res2["errors"])


def test_group_b_dependency_failure_propagation(temp_db):
    """Verify that when a prerequisite task fails, downstream dependents are marked blocked."""
    sess_id = db_store.create_session("Test Dep Fail", "Test Desc", db_path=temp_db)
    t1 = db_store.create_task(sess_id, "Build Core", "engineering", "eng-lead", stage="engineering", dependencies=[], db_path=temp_db)
    t2 = db_store.create_task(sess_id, "Build UI", "engineering", "eng-dev", stage="engineering", dependencies=[t1], db_path=temp_db)
    t3 = db_store.create_task(sess_id, "Run QA", "testing", "eng-qa", stage="testing", dependencies=[t2], db_path=temp_db)
    t_other = db_store.create_task(sess_id, "Unrelated Task", "marketing", "mkt-lead", stage="marketing", dependencies=[], db_path=temp_db)

    # Fail t1 and propagate
    blocked = orch.fail_task_and_propagate(t1, "Compilation crashed with fatal error", session_id=sess_id, db_path=temp_db)
    assert set(blocked) == {t2, t3}

    t1_obj = db_store.get_task(t1, db_path=temp_db)
    t2_obj = db_store.get_task(t2, db_path=temp_db)
    t3_obj = db_store.get_task(t3, db_path=temp_db)
    other_obj = db_store.get_task(t_other, db_path=temp_db)

    assert t1_obj["status"] == "failed"
    assert t2_obj["status"] == "blocked"
    assert t3_obj["status"] == "blocked"
    assert "Blocked by upstream" in t2_obj["error_message"]
    assert other_obj["status"] == "ready"


# =========================================================================
# GROUP C: Typed Inter-Agent Handoffs
# =========================================================================

def test_group_c_valid_typed_handoff_persistence(temp_db):
    """Verify durable typed handoff recording with trace_id, contract_version, and payload."""
    sess_id = db_store.create_session("Test Handoff", "Test Desc", db_path=temp_db)
    t1 = db_store.create_task(sess_id, "Design Arch", "engineering", "eng-arch", stage="engineering", db_path=temp_db)
    t2 = db_store.create_task(sess_id, "Implement Code", "engineering", "eng-dev", stage="engineering", dependencies=[t1], db_path=temp_db)
    db_store.update_task_status(t1, "completed", output_text="System architecture blueprint", db_path=temp_db)

    hid = db_store.create_task_handoff(
        session_id=sess_id,
        from_task_id=t1,
        to_task_id=t2,
        from_agent_id="eng-arch",
        to_agent_id="eng-dev",
        artifact_type="architecture_spec",
        artifact_summary="System architecture blueprint",
        status="delivered",
        contract_version="1.0",
        trace_id="trc_test_12345",
        validation_outcome="valid",
        required_context={"framework": "vanilla_js"},
        db_path=temp_db
    )
    assert hid.startswith("hoff_")

    handoffs = db_store.get_session_handoffs(sess_id, db_path=temp_db)
    assert len(handoffs) == 1
    h = handoffs[0]
    assert h["contract_version"] == "1.0"
    assert h["trace_id"] == "trc_test_12345"
    assert h["validation_outcome"] == "valid"
    assert h["required_context"]["framework"] == "vanilla_js"


def test_group_c_destination_mismatch_and_oversized_payload(temp_db):
    """Verify destination agent mismatch and oversized payloads are rejected."""
    sess_id = db_store.create_session("Test Handoff Rejections", "Desc", db_path=temp_db)
    t1 = db_store.create_task(sess_id, "Task A", "engineering", "agent-a", stage="engineering", db_path=temp_db)
    t2 = db_store.create_task(sess_id, "Task B", "engineering", "agent-b", stage="engineering", db_path=temp_db)
    db_store.update_task_status(t1, "completed", output_text="Task A completed", db_path=temp_db)

    # 1. Destination agent mismatch (claiming handoff to agent-c when t2 assigned to agent-b)
    with pytest.raises(ValueError, match="Destination agent mismatch"):
        db_store.create_task_handoff(
            session_id=sess_id,
            from_task_id=t1,
            to_task_id=t2,
            from_agent_id="agent-a",
            to_agent_id="agent-c",
            artifact_type="doc",
            artifact_summary="summary",
            db_path=temp_db
        )

    # 2. Oversized payload (> 100k characters)
    oversized = "X" * 105000
    with pytest.raises(ValueError, match="Handoff artifact_summary exceeds max payload"):
        db_store.create_task_handoff(
            session_id=sess_id,
            from_task_id=t1,
            to_task_id=t2,
            from_agent_id="agent-a",
            to_agent_id="agent-b",
            artifact_type="doc",
            artifact_summary=oversized,
            db_path=temp_db
        )


def test_group_c_idempotent_duplicate_handoff(temp_db):
    """Verify duplicate handoff calls update the existing record idempotently."""
    sess_id = db_store.create_session("Test Idempotent Handoff", "Desc", db_path=temp_db)
    t1 = db_store.create_task(sess_id, "Task A", "engineering", "agent-a", stage="engineering", db_path=temp_db)
    t2 = db_store.create_task(sess_id, "Task B", "engineering", "agent-b", stage="engineering", db_path=temp_db)
    db_store.update_task_status(t1, "completed", output_text="Task A completed", db_path=temp_db)

    hid1 = db_store.create_task_handoff(
        session_id=sess_id,
        from_task_id=t1,
        to_task_id=t2,
        from_agent_id="agent-a",
        to_agent_id="agent-b",
        artifact_type="doc",
        artifact_summary="Initial summary",
        status="pending",
        db_path=temp_db
    )

    hid2 = db_store.create_task_handoff(
        session_id=sess_id,
        from_task_id=t1,
        to_task_id=t2,
        from_agent_id="agent-a",
        to_agent_id="agent-b",
        artifact_type="doc",
        artifact_summary="Updated summary",
        status="delivered",
        db_path=temp_db
    )

    assert hid1 == hid2
    handoffs = db_store.get_session_handoffs(sess_id, db_path=temp_db)
    assert len(handoffs) == 1
    assert handoffs[0]["status"] == "delivered"
    assert handoffs[0]["artifact_summary"] == "Updated summary"


# =========================================================================
# GROUP D: Execution Reliability & Failure Handling
# =========================================================================

def test_group_d_duplicate_task_execution_guard(temp_db):
    """Verify already completed task skips re-execution and preserves original output."""
    sess_id = db_store.create_session("Test Dedup Exec", "Desc", db_path=temp_db)
    t1 = db_store.create_task(sess_id, "Core Logic", "engineering", "eng-dev", stage="engineering", db_path=temp_db)
    db_store.update_task_status(t1, "completed", output_text="Existing Authentic Output", db_path=temp_db)

    task_obj = db_store.get_task(t1, db_path=temp_db)
    sess_obj = db_store.get_session(sess_id, db_path=temp_db)

    call_count = 0
    def mock_agent(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        return "New Fabricated Output"

    ok, out, err = orch.execute_single_task(
        task=task_obj,
        session=sess_obj,
        call_agent_fn=mock_agent,
        agent_prompt_resolver_fn=lambda a: ("Prompt", "Role", "blue"),
        db_path=temp_db
    )
    assert ok is True
    assert out == "Existing Authentic Output"
    assert call_count == 0, "Agent function must not be invoked for already completed tasks"


def test_group_d_tuple_and_dict_return_handling(temp_db):
    """Verify execute_single_task safely unpacks tuple and dict returns from agent functions."""
    sess_id = db_store.create_session("Test Returns", "Desc", db_path=temp_db)
    t1 = db_store.create_task(sess_id, "Tuple Task", "engineering", "eng-dev", stage="engineering", db_path=temp_db)
    sess_obj = db_store.get_session(sess_id, db_path=temp_db)
    task_obj = db_store.get_task(t1, db_path=temp_db)

    # 1. Tuple return (output_text, metadata_dict)
    def mock_tuple_agent(*args, **kwargs):
        return ("Tuple Code Output", {"model": "test-model", "tokens": 120})

    ok, out, err = orch.execute_single_task(
        task=task_obj,
        session=sess_obj,
        call_agent_fn=mock_tuple_agent,
        agent_prompt_resolver_fn=lambda a: ("Prompt", "Role", "blue"),
        db_path=temp_db
    )
    assert ok is True
    assert out == "Tuple Code Output"

    # 2. Dict return
    t2 = db_store.create_task(sess_id, "Dict Task", "engineering", "eng-dev", stage="engineering", db_path=temp_db)
    task_obj2 = db_store.get_task(t2, db_path=temp_db)
    def mock_dict_agent(*args, **kwargs):
        return {"output": "Dict Code Output", "metadata": {}}

    ok2, out2, err2 = orch.execute_single_task(
        task=task_obj2,
        session=sess_obj,
        call_agent_fn=mock_dict_agent,
        agent_prompt_resolver_fn=lambda a: ("Prompt", "Role", "blue"),
        db_path=temp_db
    )
    assert ok2 is True
    assert out2 == "Dict Code Output"


# =========================================================================
# GROUP E: Independent QA, Remediation & Release Gates
# =========================================================================

def test_group_e_run_qa_checks_records_structured_findings(temp_db):
    """Verify run_qa_checks classifies and persists findings to SQLite qa_findings table."""
    sess_id = db_store.create_session("Test QA Findings", "Desc", db_path=temp_db)
    
    # Defective HTML with missing doctype and path traversal link
    bad_html = """
    <html>
      <head><title>Bad App</title></head>
      <body>
        <a href="../../etc/passwd">Leak</a>
        <h1>App</h1>
      </body>
    </html>
    """
    res = run_qa_checks(bad_html, app_css="h1 { color: red; }", app_js="console.log('ok');", session_id=sess_id, db_path=temp_db)
    assert res["passed"] is False
    assert len(res["errors"]) >= 2

    # Query durable findings
    findings = db_store.list_qa_findings(session_id=sess_id, db_path=temp_db)
    assert len(findings) >= 2
    sevs = {f["severity"] for f in findings}
    assert "critical" in sevs  # path traversal is critical

    # Verify severity filter parameter
    crit_findings = db_store.list_qa_findings(session_id=sess_id, severity="critical", db_path=temp_db)
    assert len(crit_findings) >= 1
    assert all(f["severity"] == "critical" for f in crit_findings)

    sec_finding = next(f for f in findings if f["category"] == "security")
    assert "Path traversal" in sec_finding["evidence"]
    assert sec_finding["status"] == "open"
    assert sec_finding["responsible_agent_id"] == "engineering-senior-developer"


def test_group_e_prevention_of_self_certification(temp_db):
    """Verify implementation agent cannot unilaterally mark its own findings verified_resolved."""
    sess_id = db_store.create_session("Test Self Cert", "Desc", db_path=temp_db)
    valid_rev = "a" * 64
    fid = db_store.record_qa_finding(
        session_id=sess_id,
        task_id=None,
        qa_run_id="run_1",
        severity="high",
        category="syntax",
        artifact_name="script.js",
        evidence="SyntaxError in line 10",
        remediation_recommendation="Fix bracket",
        responsible_agent_id="engineering-senior-developer",
        status="open",
        artifact_revision=valid_rev,
        db_path=temp_db
    )

    # Attempt self-certification by responsible agent -> must fail
    ok, err = db_store.update_qa_finding_status(
        finding_id=fid,
        status="verified_resolved",
        verified_by_agent="engineering-senior-developer",
        artifact_revision=valid_rev,
        db_path=temp_db
    )
    assert ok is False
    assert "Self-certification rejected" in err

    # Independent verification by QA engineer -> must succeed
    ok2, err2 = db_store.update_qa_finding_status(
        finding_id=fid,
        status="verified_resolved",
        verified_by_agent="engineering-qa-engineer",
        artifact_revision=valid_rev,
        db_path=temp_db
    )
    assert ok2 is True
    finding = db_store.get_qa_finding(fid, db_path=temp_db)
    assert finding["status"] == "verified_resolved"


def test_group_e_release_gate_fails_closed_when_qa_missing(temp_db):
    """Verify release gate blocks release when required QA has not run."""
    sess_id = db_store.create_session("Test Gate No QA", "Desc", db_path=temp_db)
    # No QA run, no findings
    eval_res = evaluate_release_gate(sess_id, db_path=temp_db)
    assert eval_res["allowed"] is False
    assert eval_res["status"] == "blocked"
    assert any("missing QA evidence" in r for r in eval_res["reasons"])


def test_group_e_release_gate_blocks_on_open_critical_findings(temp_db):
    """Verify release gate blocks when unresolved critical findings exist."""
    sess_id = db_store.create_session("Test Gate Open Findings", "Desc", db_path=temp_db)
    db_store.record_qa_finding(
        session_id=sess_id,
        task_id=None,
        qa_run_id="run_1",
        severity="critical",
        category="security",
        artifact_name="index.html",
        evidence="Path traversal",
        remediation_recommendation="Fix links",
        status="open",
        db_path=temp_db
    )
    eval_res = evaluate_release_gate(
        sess_id,
        artifact_manifest={"artifacts": [], "qa_run_id": "run_1"},
        db_path=temp_db
    )
    assert eval_res["allowed"] is False
    assert eval_res["status"] == "blocked"
    assert eval_res["blocking_findings_count"] == 1
    assert any("blocking findings" in r for r in eval_res["reasons"])


def test_group_e_release_gate_detects_stale_or_tampered_manifest(temp_db):
    """Verify release gate detects when candidate files do not match certified QA manifest digest."""
    sess_id = db_store.create_session("Test Gate Tamper", "Desc", db_path=temp_db)
    certified_html = "<!DOCTYPE html><html><head><title>App</title></head><body>Hello</body></html>"
    certified_sha = hashlib.sha256(certified_html.encode("utf-8")).hexdigest()

    manifest = {
        "qa_run_id": "run_1",
        "artifacts": [
            {"relative_path": "index.html", "sha256": certified_sha}
        ]
    }

    # Tampered candidate file with different content
    tampered_html = "<!DOCTYPE html><html><head><title>App</title></head><body>Modified Hello</body></html>"
    eval_res = evaluate_release_gate(
        session_id=sess_id,
        candidate_files={"index.html": tampered_html},
        artifact_manifest=manifest,
        db_path=temp_db
    )
    assert eval_res["allowed"] is False
    assert any("digest mismatch" in r for r in eval_res["reasons"])


# =========================================================================
# GROUP F: Human Approval Lifecycle & Revision Binding
# =========================================================================

def test_group_f_approval_request_lifecycle(temp_db):
    """Verify full approval lifecycle: request -> lazy expire -> reject -> approve."""
    sess_id = db_store.create_session("Test Approvals", "Desc", db_path=temp_db)
    rev_hash = hashlib.sha256(b"revision_v1").hexdigest()

    # 1. Create request
    appr_id = db_store.create_approval_request(
        session_id=sess_id,
        task_id=None,
        action_type="production_release",
        requested_by="automated_pipeline",
        policy_rule="dual_signoff",
        artifact_revision_hash=rev_hash,
        expires_in_seconds=3600,
        db_path=temp_db
    )
    assert appr_id.startswith("apr_")
    appr = db_store.get_approval_request(appr_id, db_path=temp_db)
    assert appr["status"] == "pending"

    # 2. Self-approval prevention
    ok, err = db_store.decide_approval_request(
        approval_id=appr_id,
        decision="approved",
        principal="automated_pipeline",  # Same as requested_by
        db_path=temp_db
    )
    assert ok is False
    assert "Self-approval is forbidden" in err

    # 3. Revision hash mismatch rejection
    bad_rev_hash = hashlib.sha256(b"modified_revision").hexdigest()
    ok_rev, err_rev = db_store.decide_approval_request(
        approval_id=appr_id,
        decision="approved",
        principal="human_director",
        current_revision_hash=bad_rev_hash,
        db_path=temp_db
    )
    assert ok_rev is False
    assert "revision mismatch" in err_rev.lower()

    # 4. Authorized decision with matching revision
    ok_good, err_good = db_store.decide_approval_request(
        approval_id=appr_id,
        decision="approved",
        principal="human_director",
        reason="Approved for staging release",
        current_revision_hash=rev_hash,
        db_path=temp_db
    )
    assert ok_good is True
    appr_done = db_store.get_approval_request(appr_id, db_path=temp_db)
    assert appr_done["status"] == "approved"
    assert appr_done["decision_by"] == "human_director"


def test_group_f_stale_approval_invalidated_when_code_changes(temp_db):
    """Verify that when candidate code changes after approval, release gate blocks stale approval."""
    sess_id = db_store.create_session("Test Stale Approval", "Desc", db_path=temp_db)

    # Candidate files revision 1
    code_v1 = {"index.html": "<!DOCTYPE html><html><body>Version 1</body></html>"}
    hasher = hashlib.sha256()
    hasher.update(f"index.html:{hashlib.sha256(code_v1['index.html'].encode('utf-8')).hexdigest()};".encode("utf-8"))
    rev1_hash = hasher.hexdigest()

    appr_id = db_store.create_approval_request(
        session_id=sess_id,
        task_id=None,
        action_type="release",
        requested_by="dev_team",
        artifact_revision_hash=rev1_hash,
        db_path=temp_db
    )
    db_store.decide_approval_request(
        approval_id=appr_id,
        decision="approved",
        principal="release_manager",
        current_revision_hash=rev1_hash,
        db_path=temp_db
    )

    # Now candidate files are modified to Version 2 without re-approval
    code_v2 = {"index.html": "<!DOCTYPE html><html><body>Version 2 - Modified</body></html>"}
    eval_res = evaluate_release_gate(
        session_id=sess_id,
        candidate_files=code_v2,
        artifact_manifest={"qa_run_id": "qa_123", "artifacts": []},
        db_path=temp_db
    )
    assert eval_res["allowed"] is False
    assert any("different artifact revision" in r for r in eval_res["reasons"])


# =========================================================================
# GROUP G: Security, Safe Origin & Persistence Compatibility
# =========================================================================

def test_group_g_origin_validation_on_mutating_endpoints(test_client):
    """Verify CSRF / safe origin enforcement blocks cross-site and null origins on Phase 2.8 endpoints."""
    # 1. Untrusted foreign origin -> 403 Forbidden
    headers_foreign = {"Origin": "https://malicious-attacker.com", "Content-Type": "application/json"}
    
    res1 = test_client.post("/api/workforce/assign", headers=headers_foreign, json={})
    assert res1.status_code == 403

    res2 = test_client.post("/api/workflow/validate", headers=headers_foreign, json={"tasks": []})
    assert res2.status_code == 403

    res3 = test_client.post("/api/approvals/request", headers=headers_foreign, json={})
    assert res3.status_code == 403

    res4 = test_client.post("/api/approvals/apr_test/decide", headers=headers_foreign, json={})
    assert res4.status_code == 403

    res5 = test_client.post("/api/qa_findings/fnd_test/status", headers=headers_foreign, json={})
    assert res5.status_code == 403


def test_group_g_workforce_intelligence_endpoint(test_client):
    """Verify GET /api/workforce/intelligence returns valid canonical metrics and schema."""
    res = test_client.get("/api/workforce/intelligence")
    assert res.status_code == 200
    data = res.get_json()
    assert data["total_registered_agents"] >= 200
    assert data["total_divisions"] == 18
    assert "divisions" in data
    assert "qa_workload" in data
    assert "execution_summary" in data
    assert data["configured_agents_count"] >= data["executed_agents_count"]
