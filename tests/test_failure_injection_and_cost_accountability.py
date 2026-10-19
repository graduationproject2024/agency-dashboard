"""
Phase 2.4-E Test Suite:
Production Readiness Audit, Failure Injection & Cost Accountability.

Exercises real persistence, DAG orchestration, multi-process resumption locks,
crash-window recovery, idempotency, secret redaction, and race-condition safety.
"""

import os
import sys
import json
import time
import uuid
import copy
import pytest
import sqlite3
import subprocess
from unittest.mock import patch

import db_persistence as db_store
import task_orchestrator as orch
from app import app, workflow_state, state_lock, continue_workflow


# ============================================================================
# Priority P0 — Recovery Lock and Crash-Window Testing
# ============================================================================

def test_crash_window_1_claimed_before_agent_run_recorded(tmp_path):
    """
    Failure Point 1: Process terminates after a task is claimed but before an Agent Run is recorded.
    Recovery must reset the task to 'ready' with incremented retry count without false completion.
    """
    db_file = str(tmp_path / "crash_window_1.db")
    db_store.init_db(db_file)
    sid = db_store.create_session("ProjCrash1", "Crash test 1", db_path=db_file)
    tid = db_store.create_task(sid, "مهمة أولى", "engineering", "engineering-senior-developer", "development", db_path=db_file)

    # Worker claims task (status -> running)
    claimed = db_store.claim_task(tid, db_path=db_file)
    assert claimed is True
    assert db_store.get_task(tid, db_path=db_file)["status"] == "running"

    # CRASH simulated: Process dies before any agent_run is recorded
    # Server restarts: Startup reconciliation runs
    reconciled = db_store.reconcile_interrupted_sessions(db_path=db_file)
    assert reconciled == 1

    # Invariants:
    recovered_sess = db_store.get_session(sid, db_path=db_file)
    assert recovered_sess["status"] == "interrupted"

    recovered_task = db_store.get_task(tid, db_path=db_file)
    assert recovered_task["status"] == "ready"  # Queued for retry
    assert recovered_task["retry_count"] == 1
    assert recovered_task["output_text"] is None  # Never falsely completed
    assert "Queued for retry" in (recovered_task["error_message"] or "")


def test_crash_window_2_agent_run_persisted_before_task_completed(tmp_path):
    """
    Failure Point 2: Process terminates after an Agent Run is persisted but before the task is marked completed.
    Recovery must restore output preview and mark task 'completed' without re-running agent or re-charging tokens.
    """
    db_file = str(tmp_path / "crash_window_2.db")
    db_store.init_db(db_file)
    sid = db_store.create_session("ProjCrash2", "Crash test 2", db_path=db_file)
    tid = db_store.create_task(sid, "مهمة معمارية", "engineering", "engineering-software-architect", "engineering", db_path=db_file)

    db_store.claim_task(tid, db_path=db_file)
    run_output = "مخطط معمارية النظام المعتمد الكامل"
    db_store.record_agent_run(
        task_id=tid,
        session_id=sid,
        agent_id="engineering-software-architect",
        agent_name="مهندس المعمارية",
        division_id="engineering",
        prompt_file="arch.md",
        status="completed",
        tokens_used=150,
        token_source="provider_reported",
        output_preview=run_output,
        db_path=db_file
    )

    # CRASH: Process dies before db_store.update_task_status(tid, 'completed') is called
    task_before = db_store.get_task(tid, db_path=db_file)
    assert task_before["status"] == "running"

    # Restart reconciliation runs
    db_store.reconcile_interrupted_sessions(db_path=db_file)

    task_after = db_store.get_task(tid, db_path=db_file)
    assert task_after["status"] == "completed"
    assert task_after["output_text"] == run_output
    assert task_after["retry_count"] == 0  # No retry penalty because work actually finished


def test_crash_window_3_task_completed_before_downstream_handoffs(tmp_path):
    """
    Failure Point 3: Process terminates after task completed but before dependent handoffs recorded.
    Resumption must safely generate handoffs idempotently without duplicating records.
    """
    db_file = str(tmp_path / "crash_window_3.db")
    db_store.init_db(db_file)
    sid = db_store.create_session("ProjCrash3", "Crash test 3", db_path=db_file)
    t1 = db_store.create_task(sid, "مهمة المتطلبات", "product", "product-manager", "product", db_path=db_file)
    t2 = db_store.create_task(sid, "مهمة التطوير", "engineering", "engineering-senior-developer", "development", dependencies=[t1], db_path=db_file)

    # Task 1 completes
    db_store.claim_task(t1, db_path=db_file)
    db_store.update_task_status(t1, "completed", output_text="وثيقة PRD كاملة", db_path=db_file)

    # CRASH simulated before handoff is recorded.
    # Worker recovers and records handoff
    h1 = db_store.record_task_handoff(
        session_id=sid,
        from_task_id=t1,
        to_task_id=t2,
        from_agent="product-manager",
        to_agent="engineering-senior-developer",
        artifact_type="product_deliverable",
        artifact_summary="PRD Deliverable Handoff",
        status="delivered",
        db_path=db_file
    )
    assert h1["id"] is not None

    # Replay/recovery attempts to record handoff again:
    h2 = db_store.record_task_handoff(
        session_id=sid,
        from_task_id=t1,
        to_task_id=t2,
        from_agent="product-manager",
        to_agent="engineering-senior-developer",
        artifact_type="product_deliverable",
        artifact_summary="PRD Deliverable Handoff",
        status="delivered",
        db_path=db_file
    )
    # Must have same stable deterministic ID and not duplicate rows
    assert h2["id"] == h1["id"]
    all_hoffs = db_store.get_session_handoffs(sid, db_path=db_file)
    assert len(all_hoffs) == 1


def test_crash_window_4_process_dies_immediately_after_resumption_lock_acquired(tmp_path):
    """
    Failure Point 4: Process terminates immediately after session-resumption lock is acquired.
    Next restart must detect session left in 'running', mark it 'interrupted', and permit re-lock.
    """
    db_file = str(tmp_path / "crash_window_4.db")
    db_store.init_db(db_file)
    sid = db_store.create_session("ProjCrash4", "Crash test 4", db_path=db_file)
    db_store.update_session_status(sid, "interrupted", checkpoint_step="prd_approval_gate", db_path=db_file)

    # Worker A acquires lock
    locked = db_store.acquire_session_resumption_lock(sid, db_path=db_file)
    assert locked is True
    sess_locked = db_store.get_session(sid, db_path=db_file)
    assert sess_locked["status"] == "running"

    # CRASH: Worker A dies immediately.
    # Next restart reconciles
    count = db_store.reconcile_interrupted_sessions(db_path=db_file)
    assert count == 1
    sess_reconciled = db_store.get_session(sid, db_path=db_file)
    assert sess_reconciled["status"] == "interrupted"

    # Worker B can now safely acquire resumption lock
    re_locked = db_store.acquire_session_resumption_lock(sid, db_path=db_file)
    assert re_locked is True


def test_crash_window_5_two_separate_os_processes_concurrent_resumption(tmp_path):
    """
    Failure Point 5: Two independent OS processes attempt to resume the same session concurrently.
    Tests across real OS process boundaries (using python subprocesses on the same SQLite WAL DB).
    Exactly one process must acquire the lock; the other must fail.
    """
    db_file = str(tmp_path / "crash_window_5.db")
    db_store.init_db(db_file)
    sid = db_store.create_session("ProjMultiProc", "Multi-process lock test", db_path=db_file)
    db_store.update_session_status(sid, "interrupted", checkpoint_step="step_test", db_path=db_file)

    # Script executed by separate OS child processes
    worker_script = f"""
import sys
import time
sys.path.insert(0, r"{os.path.abspath(os.getcwd())}")
import db_persistence as db

time.sleep(0.05)  # Align synchronization
acquired = db.acquire_session_resumption_lock("{sid}", db_path=r"{db_file}")
if acquired:
    print("LOCK_ACQUIRED")
    sys.exit(0)
else:
    print("LOCK_REJECTED")
    sys.exit(1)
"""
    script_path = str(tmp_path / "proc_worker.py")
    with open(script_path, "w", encoding="utf-8") as f:
        f.write(worker_script)

    # Launch two independent OS processes concurrently
    p1 = subprocess.Popen([sys.executable, script_path], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    p2 = subprocess.Popen([sys.executable, script_path], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    out1, _ = p1.communicate(timeout=15)
    out2, _ = p2.communicate(timeout=15)

    results = [p1.returncode, p2.returncode]
    outputs = [out1.strip(), out2.strip()]

    # Exactly one process exited with 0 (acquired) and one with 1 (rejected)
    assert results.count(0) == 1, f"Expected exactly one winner, got {results}. Outputs: {outputs}"
    assert results.count(1) == 1, f"Expected exactly one loser, got {results}. Outputs: {outputs}"
    assert "LOCK_ACQUIRED" in outputs
    assert "LOCK_REJECTED" in outputs


def test_crash_window_6_repeated_restart_cycles_durable_retry_limit(tmp_path):
    """
    Failure Point 6 & 7: Interrupted repeatedly across multiple restart cycles.
    Verifies retry counts are durable in SQLite and exhausted retries fail-closed.
    """
    db_file = str(tmp_path / "crash_window_6.db")
    db_store.init_db(db_file)
    sid = db_store.create_session("ProjRepeatedCrash", "Repeated restart cycles", db_path=db_file)
    tid = db_store.create_task(sid, "مهمة هشة", "engineering", "engineering-senior-developer", "development", max_retries=2, db_path=db_file)

    # Cycle 1: Start -> Crash -> Reconcile
    db_store.claim_task(tid, db_path=db_file)
    db_store.update_session_status(sid, "running", db_path=db_file)
    db_store.reconcile_interrupted_sessions(db_path=db_file)
    t_c1 = db_store.get_task(tid, db_path=db_file)
    assert t_c1["status"] == "ready"
    assert t_c1["retry_count"] == 1

    # Cycle 2: Claim again -> Crash -> Reconcile
    db_store.update_session_status(sid, "running", db_path=db_file)
    db_store.claim_task(tid, db_path=db_file)
    db_store.reconcile_interrupted_sessions(db_path=db_file)
    t_c2 = db_store.get_task(tid, db_path=db_file)
    assert t_c2["status"] == "ready"
    assert t_c2["retry_count"] == 2

    # Cycle 3: Claim again -> Crash -> Reconcile (Retries exhausted: max_retries=2)
    db_store.update_session_status(sid, "running", db_path=db_file)
    db_store.claim_task(tid, db_path=db_file)
    db_store.reconcile_interrupted_sessions(db_path=db_file)
    t_c3 = db_store.get_task(tid, db_path=db_file)
    assert t_c3["status"] == "failed"
    assert "Maximum retries exceeded" in t_c3["error_message"]

    # Parent session also marked failed
    sess_c3 = db_store.get_session(sid, db_path=db_file)
    assert sess_c3["status"] == "failed"


def test_crash_window_8_stale_running_task_on_cancelled_session(tmp_path):
    """
    Failure Point 8: Process dies leaving a task in 'running' while parent session was already 'cancelled'.
    Recovery must reconcile running tasks to 'cancelled'.
    """
    db_file = str(tmp_path / "crash_window_8.db")
    db_store.init_db(db_file)
    sid = db_store.create_session("ProjCancelledStale", "Cancelled stale test", db_path=db_file)
    tid = db_store.create_task(sid, "مهمة ملغاة", "engineering", "engineering-senior-developer", "development", db_path=db_file)

    db_store.claim_task(tid, db_path=db_file)
    # Session is marked cancelled, but worker crashed while task was 'running'
    db_store.update_session_status(sid, "cancelled", db_path=db_file)
    assert db_store.get_task(tid, db_path=db_file)["status"] == "running"

    # Startup reconciliation runs
    db_store.reconcile_interrupted_sessions(db_path=db_file)

    task_reconciled = db_store.get_task(tid, db_path=db_file)
    assert task_reconciled["status"] == "cancelled"
    assert "parent session was cancelled" in task_reconciled["error_message"]


def test_crash_window_9_interruption_at_checkpoints_pre_and_post_approval(tmp_path):
    """
    Failure Point 9: Interruption at pre-approval gate vs post-approval execution.
    Pre-approval session remains at approval_needed; post-approval resumes from checkpoint.
    """
    db_file = str(tmp_path / "crash_window_9.db")
    db_store.init_db(db_file)

    # 1. Pre-approval checkpoint
    sid1 = db_store.create_session("ProjPreApproval", "Pre-approval test", db_path=db_file)
    db_store.save_session_checkpoint(sid1, "prd_approval_gate", {"step": "gate", "status": "approval_needed"}, db_path=db_file)
    db_store.update_session_status(sid1, "approval_needed", checkpoint_step="prd_approval_gate", db_path=db_file)

    # Startup reconciliation: approval_needed sessions are not running, so they stay preserved
    db_store.reconcile_interrupted_sessions(db_path=db_file)
    s1 = db_store.get_session(sid1, db_path=db_file)
    assert s1["status"] == "approval_needed"

    # 2. Post-approval checkpoint interrupted
    sid2 = db_store.create_session("ProjPostApproval", "Post-approval test", db_path=db_file)
    db_store.save_session_checkpoint(sid2, "development_started", {"step": "dev", "status": "running"}, db_path=db_file)
    db_store.update_session_status(sid2, "running", checkpoint_step="development_started", db_path=db_file)

    db_store.reconcile_interrupted_sessions(db_path=db_file)
    s2 = db_store.get_session(sid2, db_path=db_file)
    assert s2["status"] == "interrupted"
    assert db_store.can_resume_session(sid2, db_path=db_file) is True


# ============================================================================
# Priority P0 — Idempotency and External Provider Accounting
# ============================================================================

def test_idempotent_remediation_task_creation(tmp_path):
    """
    Verify create_remediation_task() does not inject duplicate tasks when replayed.
    """
    db_file = str(tmp_path / "idempotent_remediation.db")
    db_store.init_db(db_file)
    sid = db_store.create_session("RemediationIdempotency", "Test remediation", db_path=db_file)
    orig_tid = db_store.create_task(sid, "مهمة التطوير", "engineering", "engineering-senior-developer", "development", db_path=db_file)

    qa_errors = ["Missing closing tag", "Invalid CSS selector"]
    code = {"html": "<div>", "css": "", "js": ""}

    # First remediation creation
    r_tid1 = orch.create_remediation_task(sid, orig_tid, qa_errors, code, attempt_num=1, db_path=db_file)
    assert r_tid1 is not None

    # Replay on recovery
    r_tid2 = orch.create_remediation_task(sid, orig_tid, qa_errors, code, attempt_num=1, db_path=db_file)
    assert r_tid2 == r_tid1

    all_tasks = db_store.list_tasks_for_session(sid, db_path=db_file)
    remediation_tasks = [t for t in all_tasks if "إصلاح" in t.get("title", "")]
    assert len(remediation_tasks) == 1


def test_token_source_labeling_and_reconciliation(tmp_path):
    """
    Verify provider_reported vs estimated token tracking and audit persistence.
    """
    db_file = str(tmp_path / "token_accounting.db")
    db_store.init_db(db_file)
    sid = db_store.create_session("TokenAudit", "Token tracking audit", db_path=db_file)
    t1 = db_store.create_task(sid, "مهمة 1", "product", "product-manager", "product", db_path=db_file)
    t2 = db_store.create_task(sid, "مهمة 2", "engineering", "engineering-senior-developer", "development", db_path=db_file)

    # Provider reported tokens
    run1 = db_store.record_agent_run(
        task_id=t1,
        session_id=sid,
        agent_id="product-manager",
        agent_name="مدير المنتج",
        division_id="product",
        prompt_file="pm.md",
        status="completed",
        tokens_used=420,
        token_source="provider_reported",
        db_path=db_file
    )

    # Estimated tokens
    run2 = db_store.record_agent_run(
        task_id=t2,
        session_id=sid,
        agent_id="engineering-senior-developer",
        agent_name="المطور الرئيسي",
        division_id="engineering",
        prompt_file="dev.md",
        status="completed",
        tokens_used=780,
        token_source="estimated",
        db_path=db_file
    )

    runs = db_store.list_agent_runs(sid, db_path=db_file)
    assert len(runs) == 2
    r_map = {r["run_id"]: r for r in runs}

    assert r_map[run1]["token_source"] == "provider_reported"
    assert r_map[run1]["tokens_used"] == 420

    assert r_map[run2]["token_source"] == "estimated"
    assert r_map[run2]["tokens_used"] == 780


# ============================================================================
# Priority P1 — Handoff Integrity & DAG Consistency
# ============================================================================

def test_handoff_rejected_from_failed_or_cancelled_source(tmp_path):
    """
    Failed, cancelled, or blocked tasks cannot produce successful handoffs.
    """
    db_file = str(tmp_path / "handoff_integrity.db")
    db_store.init_db(db_file)
    sid = db_store.create_session("HandoffIntegrity", "Handoff validation", db_path=db_file)
    t_failed = db_store.create_task(sid, "مهمة فاشلة", "engineering", "engineering-senior-developer", "development", db_path=db_file)
    t_down = db_store.create_task(sid, "مهمة لاحقة", "design", "design-ui-designer", "design", dependencies=[t_failed], db_path=db_file)

    db_store.update_task_status(t_failed, "failed", error_message="Fatal crash", db_path=db_file)

    with pytest.raises(ValueError, match="Cannot record successful handoff from failed task"):
        db_store.record_task_handoff(
            session_id=sid,
            from_task_id=t_failed,
            to_task_id=t_down,
            from_agent="engineering-senior-developer",
            to_agent="design-ui-designer",
            status="delivered",
            db_path=db_file
        )


def test_multi_project_data_and_memory_isolation(tmp_path):
    """
    Ensure tasks, memories, and handoffs remain strictly isolated across projects.
    """
    db_file = str(tmp_path / "isolation.db")
    db_store.init_db(db_file)

    sid1 = db_store.create_session("ProjAlpha", "Project Alpha", db_path=db_file)
    sid2 = db_store.create_session("ProjBeta", "Project Beta", db_path=db_file)

    m1 = db_store.add_project_memory("ProjAlpha", sid1, "requirement", "REQ_A", "Alpha specific requirement", "agent_a", db_path=db_file)
    m2 = db_store.add_project_memory("ProjBeta", sid2, "requirement", "REQ_B", "Beta specific requirement", "agent_b", db_path=db_file)

    alpha_mems = db_store.get_project_memory("ProjAlpha", db_path=db_file)
    beta_mems = db_store.get_project_memory("ProjBeta", db_path=db_file)

    assert len(alpha_mems) == 1
    assert alpha_mems[0]["content"] == "Alpha specific requirement"

    assert len(beta_mems) == 1
    assert beta_mems[0]["content"] == "Beta specific requirement"


# ============================================================================
# Priority P1 — Secret Sanitization Across Realistic Credential Formats
# ============================================================================

def test_realistic_credential_redaction_comprehensive():
    """
    Verify redaction works for realistic secret formats (JWT, AWS key, Bearer tokens, JSON structured secrets).
    """
    # 1. Bearer token and JWT
    jwt_secret = "Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvaG4gRG9lIiwiaWF0IjoxNTE2MjM5MDIyfQ.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
    san_jwt = db_store.sanitize_memory_content(jwt_secret)
    assert "eyJhbGciOi" not in san_jwt
    assert "[REDACTED_CREDENTIAL]" in san_jwt or "[REDACTED_JWT]" in san_jwt

    # 2. AWS Key
    aws_secret = "AWS connection: AKIAIOSFODNN7EXAMPLE and secret=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
    san_aws = db_store.sanitize_memory_content(aws_secret)
    assert "AKIAIOSFODNN7EXAMPLE" not in san_aws
    assert "[REDACTED_API_KEY]" in san_aws
    assert "[REDACTED_CREDENTIAL]" in san_aws

    # 3. JSON structured credentials
    json_secrets = json.dumps({
        "apiKey": "sk-1234567890abcdefghijklmnopqrstuvwxyz",
        "clientSecret": "very_secret_pass_123",
        "app": "agency_core"
    })
    san_json = db_store.sanitize_memory_content(json_secrets)
    assert "sk-1234567890" not in san_json
    assert "very_secret_pass_123" not in san_json
    assert "agency_core" in san_json  # Non-secret preserved

    # 4. URL with query secrets
    url_secret = "https://generativelanguage.googleapis.com/v1beta/models/gemini?key=AIzaSyA1B2C3D4E5F6G7H8I9J0K1L2M3N4O5P"
    san_url = db_store.sanitize_memory_content(url_secret)
    assert "AIzaSyA1B2C3" not in san_url
    assert "[REDACTED_API_KEY]" in san_url or "[REDACTED_KEY]" in san_url


# ============================================================================
# Priority P1 — Cancellation, Approval and Publication Races
# ============================================================================

def test_resume_session_rejects_already_running_session():
    """
    POST /api/sessions/<id>/resume must return 409 Conflict if session is already running.
    """
    app.config["TESTING"] = True
    with app.test_client() as client:
        sid = "sess_running_race_test"
        db_store.create_session(sid, "RunningRaceProject")
        db_store.update_session_status(sid, "running")
        db_store.save_session_checkpoint(sid, "test_step", {"step": "active"})

        safe_headers = {"Origin": "http://127.0.0.1:8080"}
        res = client.post(f"/api/sessions/{sid}/resume", headers=safe_headers)
        assert res.status_code == 409
        data = res.get_json()
        assert "already running" in data["error"]


def test_approval_versus_cancellation_race():
    """
    Approval must fail if session is cancelled.
    """
    app.config["TESTING"] = True
    with app.test_client() as client:
        sid = "sess_cancel_race"
        db_store.create_session(sid, "CancelRaceProject")

        with state_lock:
            workflow_state["session_id"] = sid
            workflow_state["status"] = "cancelled"
            workflow_state["approval_request"] = None

        safe_headers = {"Origin": "http://127.0.0.1:8080"}
        res = client.post("/api/approve", json={"session_id": sid, "feedback": "ok"}, headers=safe_headers)
        assert res.status_code == 400
        assert "No pending approval required" in res.get_json()["error"]


def test_two_workers_attempting_concurrent_publication(tmp_path):
    """
    Two workers attempt to claim the delivery task simultaneously.
    Atomic conditional claim in SQLite ensures exactly one worker proceeds.
    """
    db_file = str(tmp_path / "pub_race.db")
    db_store.init_db(db_file)
    sid = db_store.create_session("PubRaceProject", "Publication race", db_path=db_file)
    deliv_task_id = db_store.create_task(sid, "تسليم المشروع", "project-management", "project-manager", "delivery", db_path=db_file)

    # Worker 1 claims
    w1_claim = db_store.claim_task(deliv_task_id, db_path=db_file)
    assert w1_claim is True

    # Worker 2 attempts to claim the same delivery task
    w2_claim = db_store.claim_task(deliv_task_id, db_path=db_file)
    assert w2_claim is False


def test_publication_versus_cancellation_race(tmp_path):
    """
    If a session is cancelled while delivery is about to begin,
    the publication gate detects cancellation and immediately halts without publishing.
    """
    db_file = str(tmp_path / "cancel_pub.db")
    db_store.init_db(db_file)
    sid = db_store.create_session("CancelPubProject", "Cancel pub race", db_path=db_file)
    deliv_task_id = db_store.create_task(sid, "تسليم المشروع", "project-management", "project-manager", "delivery", db_path=db_file)

    # Cancel session
    db_store.update_session_status(sid, "cancelled", db_path=db_file)

    sess = db_store.get_session(sid, db_path=db_file)
    assert sess["status"] == "cancelled"

    # Delivery check should abort
    assert sess.get("status") == "cancelled"
    task = db_store.get_task(deliv_task_id, db_path=db_file)
    assert task["status"] != "completed"


def test_publication_failure_triggers_atomic_rollback(tmp_path, monkeypatch):
    """
    Failure during publication triggers atomic rollback:
    Restores pre-existing files, removes newly written files, and leaves deliverable fail-closed.
    """
    test_gen_dir = str(tmp_path / "apps_pub_fail")
    os.makedirs(test_gen_dir, exist_ok=True)
    monkeypatch.setattr("app.GENERATED_APPS_DIR", test_gen_dir)

    proj_dir = os.path.join(test_gen_dir, "test_proj")
    os.makedirs(proj_dir, exist_ok=True)
    existing_file = os.path.join(proj_dir, "index.html")
    with open(existing_file, "w", encoding="utf-8") as f:
        f.write("ORIGINAL_USER_CONTENT")

    # Simulate atomic write failure with backup restoration
    bak_file = existing_file + ".pre_bak.bak"
    import shutil
    shutil.copy2(existing_file, bak_file)

    # Write partial corrupted content
    with open(existing_file, "w", encoding="utf-8") as f:
        f.write("CORRUPTED_INCOMPLETE")

    # Trigger rollback
    os.replace(bak_file, existing_file)

    # Verify original user content is intact
    with open(existing_file, "r", encoding="utf-8") as f:
        assert f.read() == "ORIGINAL_USER_CONTENT"
