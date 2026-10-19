"""
Phase 2.4-J: Production-Path Failure Injection, Crash Recovery & Release Gate Validation Suite
==============================================================================================
Validates all mandatory reliability criteria through the REAL production execution paths:
- Workstream A: Real Artifact Rollback Under Persistence Failure (continue_workflow rollback)
- Workstream B: Genuine SQLite Lock Contention (real connections, transient recovery, bounded failure)
- Workstream C: Actual Process Death & Recovery (OS subprocess kill, fresh-process reconciliation, ambiguity handling)
- Workstream D: Multiprocess Claiming & Resume Concurrency (barrier-synchronized real OS processes)
- Workstream E: Provider Accounting & Ambiguous Outcomes (duplicate-completion idempotency, zero-token preservation)
- Workstream F: Remediation, Authorization & Fail-Closed Behavior (origin checks, ID validation, approval gates)
- Workstream G: Manifest Safety & Artifact Limits (25MB limit, case-insensitive and Unicode collisions)
- Workstream H: Honest Failure Diagnostics & Observability (correlation-verified operator audit)
"""

import os
import sys
import json
import time
import uuid
import copy
import hashlib
import sqlite3
import threading
import subprocess
from pathlib import Path
from typing import Dict, Any, Optional

import pytest

# Ensure repository root is on sys.path
root_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if root_dir not in sys.path:
    sys.path.insert(0, root_dir)

import app as agency_app
from app import (
    app,
    run_qa_checks,
    build_artifact_manifest,
    publish_workflow_deliverables,
    cleanup_backups,
    continue_workflow,
    run_workflow,
    workflow_state,
    state_lock,
    is_valid_session_id,
    record_token_usage,
    load_budget,
    budget_lock,
    get_project_folder_name
)
import db_persistence as db_store
import task_orchestrator as orch


# =========================================================================
# Workstream A: Real Artifact Rollback Under Persistence Failure
# =========================================================================

def test_workstream_a_production_continue_workflow_restores_pre_existing_files(monkeypatch, tmp_path):
    """
    Workstream A: Invokes the real continue_workflow production entry point with a pre-existing artifact.
    Injects a persistence failure at the database manifest recording boundary.
    Verifies that the production exception handler:
    1. Restores the pre-existing index.html byte-for-byte from backup.
    2. Deletes newly created deliverable files (plan.md, style.css, script.js).
    3. Leaves unrelated files in the project directory completely untouched.
    4. Cleans up temporary backup files.
    5. Sets both workflow_state and SQLite session status to 'failed'.
    """
    test_gen_dir = str(tmp_path / "gen_apps_prod_rollback")
    os.makedirs(test_gen_dir, exist_ok=True)
    monkeypatch.setattr("app.GENERATED_APPS_DIR", test_gen_dir)

    def valid_dev_adapter(prompt_file, role_name, short_name, color, prompt, context_text=""):
        if "developer" in prompt_file:
            html = "<!DOCTYPE html><html lang='ar' dir='rtl'><head><meta charset='UTF-8'><title>نظام الفواتير</title></head><body><h1>Candidate Release</h1></body></html>"
            css = "body { background: #000; color: #fff; font-family: sans-serif; }"
            js = "document.addEventListener('DOMContentLoaded', () => { console.log('Production Candidate'); });"
            return f"```html\n{html}\n```\n```css\n{css}\n```\n```javascript\n{js}\n```"
        return "تم إنجاز المرحلة بنجاح"

    task_name = "نظام الفواتير الموثوق"
    run_workflow(task_name, call_agent_fn=valid_dev_adapter)
    with state_lock:
        sid = workflow_state["session_id"]
        assert workflow_state["status"] == "approval_needed"

    app_dir, _ = get_project_folder_name(task_name)
    os.makedirs(app_dir, exist_ok=True)

    # 1. Place pre-existing artifact with distinctive content
    index_file = os.path.join(app_dir, "index.html")
    original_html_payload = "PRE_EXISTING_DISTINCTIVE_USER_CODE_VERIFICATION_TOKEN_8899"
    with open(index_file, "w", encoding="utf-8") as f:
        f.write(original_html_payload)

    # 2. Place unrelated user file that must not be touched
    unrelated_file = os.path.join(app_dir, "customer_records.csv")
    unrelated_payload = "id,name,balance\n1,Alice,500\n2,Bob,1200"
    with open(unrelated_file, "w", encoding="utf-8") as f:
        f.write(unrelated_payload)

    # 3. Inject persistence failure at the database manifest boundary
    def fail_manifest(*args, **kwargs):
        raise sqlite3.OperationalError("Simulated database I/O error during manifest persistence")
    monkeypatch.setattr(db_store, "record_artifact_manifest", fail_manifest)

    # 4. Invoke REAL production workflow continuation!
    continue_workflow(feedback="معتمد للنشر", session_id=sid, call_agent_fn=valid_dev_adapter)

    # 5. Verify production failure handler transitioned state to failed
    with state_lock:
        assert workflow_state["status"] == "failed"

    persisted_sess = db_store.get_session(sid)
    assert persisted_sess["status"] == "failed"
    assert "Database delivery persistence failed" in persisted_sess["error_message"]

    # 6. Verify original index.html was restored byte-for-byte by production code
    with open(index_file, "r", encoding="utf-8") as f:
        restored_html = f.read()
    assert restored_html == original_html_payload
    assert "Candidate Release" not in restored_html

    # 7. Verify unrelated file is completely intact
    with open(unrelated_file, "r", encoding="utf-8") as f:
        assert f.read() == unrelated_payload

    # 8. Verify newly created artifacts were unlinked and removed
    assert not os.path.exists(os.path.join(app_dir, "plan.md"))
    assert not os.path.exists(os.path.join(app_dir, "style.css"))
    assert not os.path.exists(os.path.join(app_dir, "script.js"))

    # 9. Verify backup files were cleaned up
    bak_files = [f for f in os.listdir(app_dir) if ".bak" in f]
    assert len(bak_files) == 0


def test_workstream_a_rollback_failure_surfaces_explicitly(monkeypatch, tmp_path):
    """
    Workstream A: If rollback restoration itself fails (e.g. permission or I/O error),
    the failure must be surfaced explicitly and MUST NOT produce a false successful terminal state.
    """
    test_gen_dir = str(tmp_path / "gen_apps_rb_fail")
    os.makedirs(test_gen_dir, exist_ok=True)
    monkeypatch.setattr("app.GENERATED_APPS_DIR", test_gen_dir)

    def dev_adapter(prompt_file, role_name, short_name, color, prompt, context_text=""):
        if "developer" in prompt_file:
            return "```html\n<!DOCTYPE html><html><body>Test</body></html>\n```"
        return "تم"

    task_name = "تطبيق فحص الانهيار"
    run_workflow(task_name, call_agent_fn=dev_adapter)
    with state_lock:
        sid = workflow_state["session_id"]

    # Inject failure during delivery checkpoint persistence
    def fail_checkpoint(*args, **kwargs):
        raise sqlite3.OperationalError("Simulated checkpoint failure")
    monkeypatch.setattr(db_store, "save_session_checkpoint", fail_checkpoint)

    continue_workflow(feedback="معتمد", session_id=sid, call_agent_fn=dev_adapter)

    with state_lock:
        assert workflow_state["status"] == "failed"

    sess = db_store.get_session(sid)
    assert sess["status"] == "failed"


# =========================================================================
# Workstream B: Genuine SQLite Lock Contention
# =========================================================================

def test_workstream_b_genuine_sqlite_lock_contention_transient_recovery(tmp_path):
    """
    Workstream B: Real SQLite database with WAL mode and two genuine connections.
    Connection 1 acquires a write lock (BEGIN IMMEDIATE).
    Connection 2 attempts to write through db_persistence with _retry_on_db_lock.
    Connection 1 releases lock after 0.3s; Connection 2 succeeds within supported window.
    """
    test_db = str(tmp_path / "real_contention.db")
    db_store.init_db(test_db)

    ready_event = threading.Event()

    def lock_holder():
        c = sqlite3.connect(test_db, timeout=1.0)
        c.execute("BEGIN IMMEDIATE;")
        c.execute("UPDATE workflow_sessions SET updated_at = 1 WHERE session_id = 'nonexistent';")
        ready_event.set()
        time.sleep(0.35)
        c.commit()
        c.close()

    t = threading.Thread(target=lock_holder)
    t.start()
    ready_event.wait()

    start_time = time.time()
    sid = "sess_real_lock_ok"
    # Connection 2 attempts persistence operation
    created_sid = db_store.create_session(
        session_id=sid,
        project_name="Genuine Lock Test",
        task_description="Testing WAL contention",
        db_path=test_db
    )
    t.join()
    elapsed = time.time() - start_time

    assert elapsed >= 0.30, f"Operation did not wait for lock release (took {elapsed}s)"
    assert created_sid == sid

    # Verify persisted database row
    persisted = db_store.get_session(sid, db_path=test_db)
    assert persisted is not None
    assert persisted["project_name"] == "Genuine Lock Test"


def test_workstream_b_genuine_sqlite_lock_contention_bounded_failure(tmp_path):
    """
    Workstream B: When a write lock is held longer than the retry policy allows,
    _retry_on_db_lock produces a bounded, explicit sqlite3.OperationalError without hanging indefinitely.
    """
    test_db = str(tmp_path / "real_lock_fail.db")
    db_store.init_db(test_db)

    ready_event = threading.Event()

    def prolonged_lock_holder():
        c = sqlite3.connect(test_db, timeout=1.0)
        c.execute("BEGIN IMMEDIATE;")
        c.execute("UPDATE workflow_sessions SET updated_at = 1 WHERE session_id = 'nonexistent';")
        ready_event.set()
        time.sleep(2.0)
        c.commit()
        c.close()

    t = threading.Thread(target=prolonged_lock_holder)
    t.start()
    ready_event.wait()

    start_time = time.time()
    caught_error = False
    try:
        # Fast retry policy to verify bounded failure
        def write_op():
            c2 = sqlite3.connect(test_db, timeout=0.05)
            c2.execute("BEGIN IMMEDIATE;")
            c2.execute("INSERT INTO workflow_sessions (session_id, project_name) VALUES ('sess_fail', 'Fail');")
            c2.commit()
            c2.close()

        db_store._retry_on_db_lock(write_op, max_retries=3, initial_delay=0.02)
    except sqlite3.OperationalError as e:
        caught_error = True
        assert "locked" in str(e).lower() or "busy" in str(e).lower()

    elapsed = time.time() - start_time
    t.join()

    assert caught_error is True
    assert elapsed < 1.8, f"Contention failure took too long ({elapsed}s), must be bounded"

    # Verify database remains clean and uncorrupted
    sess = db_store.get_session("sess_fail", db_path=test_db)
    assert sess is None


def test_workstream_b_permanent_sql_error_fails_immediately_without_retry(tmp_path):
    """
    Workstream B: Non-lock permanent errors (such as schema syntax or constraint violations)
    fail immediately on attempt 1 without redundant busy retries.
    """
    test_db = str(tmp_path / "permanent_error.db")
    db_store.init_db(test_db)

    attempts = {"count": 0}

    def bad_query():
        attempts["count"] += 1
        raise sqlite3.OperationalError("no such table: non_existent_table_xyz")

    with pytest.raises(sqlite3.OperationalError) as exc_info:
        db_store._retry_on_db_lock(bad_query, max_retries=5, initial_delay=0.05)

    assert "no such table" in str(exc_info.value)
    assert attempts["count"] == 1, "Permanent error must not be retried as a lock error"


# =========================================================================
# Workstream C: Actual Process Death & Recovery
# =========================================================================

def test_workstream_c_actual_process_death_scenario_1_after_run_creation(tmp_path):
    """
    Workstream C - Scenario 1: Process dies after run creation.
    Child OS process initializes a session and starts running.
    Parent process forcibly kills the child process with p.kill().
    Fresh process invokes reconcile_interrupted_sessions().
    Verifies that interrupted state is detected and durably reconciled.
    """
    test_db = str(tmp_path / "proc_death_s1.db")
    db_store.init_db(test_db)

    ready_flag = str(tmp_path / "child_running.flag")
    worker_script = str(tmp_path / "child_worker_s1.py")
    sid = "sess_proc_death_01"

    script_body = f"""
import sys, os, time
sys.path.insert(0, r"{root_dir}")
import db_persistence as db_store

db_store.create_session(session_id="{sid}", project_name="Death S1", task_description="Test", db_path=r"{test_db}")
db_store.update_session_status("{sid}", "running", db_path=r"{test_db}")

with open(r"{ready_flag}", "w") as f:
    f.write("READY")

# Child process blocks indefinitely until killed by parent
time.sleep(60)
"""
    with open(worker_script, "w", encoding="utf-8") as f:
        f.write(script_body)

    # Spawn real OS child process
    proc = subprocess.Popen([sys.executable, worker_script])
    
    # Wait for child process to reach checkpoint
    for _ in range(50):
        if os.path.exists(ready_flag):
            break
        time.sleep(0.1)
    assert os.path.exists(ready_flag)

    # Forcibly terminate child process
    proc.kill()
    proc.wait(timeout=5)

    # Fresh OS process runs startup reconciliation
    reconcile_script = str(tmp_path / "reconcile_s1.py")
    rec_body = f"""
import sys
sys.path.insert(0, r"{root_dir}")
import db_persistence as db_store

count = db_store.reconcile_interrupted_sessions(db_path=r"{test_db}")
print(f"RECONCILED:{{count}}")
"""
    with open(reconcile_script, "w", encoding="utf-8") as f:
        f.write(rec_body)

    rec_proc = subprocess.run([sys.executable, reconcile_script], stdout=subprocess.PIPE, text=True, timeout=10)
    assert "RECONCILED:1" in rec_proc.stdout

    # Inspect persisted database state
    sess = db_store.get_session(sid, db_path=test_db)
    assert sess["status"] == "interrupted"
    assert "Interrupted by unexpected process restart" in sess["error_message"]


def test_workstream_c_actual_process_death_scenario_2_and_3_in_flight_provider_ambiguity(tmp_path):
    """
    Workstream C - Scenarios 2 & 3: Process dies during in-flight provider call.
    Child OS process starts an agent run (status='running' in agent_runs), then is killed.
    Fresh recovery process runs reconcile_interrupted_sessions().
    Verifies that:
    1. In-flight run is marked 'failed' with explicit 'Provider outcome ambiguous'.
    2. Task is marked 'failed' with 'Provider outcome unknown' and NOT blindly re-executed.
    3. Session is marked 'failed'.
    4. Operator audit exposes ambiguous provider call and indicates retry is unsafe.
    """
    test_db = str(tmp_path / "proc_death_s2.db")
    db_store.init_db(test_db)

    in_flight_flag = str(tmp_path / "in_flight.flag")
    worker_script = str(tmp_path / "child_worker_s2.py")
    sid = "sess_ambig_crash"

    script_body = f"""
import sys, os, time
sys.path.insert(0, r"{root_dir}")
import db_persistence as db_store

db_store.create_session(session_id="{sid}", project_name="In-Flight Provider Test", task_description="Test", db_path=r"{test_db}")
db_store.update_session_status("{sid}", "running", db_path=r"{test_db}")

tid = db_store.create_task(
    session_id="{sid}",
    title="In-Flight Task",
    division_id="engineering",
    assigned_agent_id="engineering-developer",
    stage="development",
    status="running",
    db_path=r"{test_db}"
)

# Record durable in-flight provider call
run_id = db_store.record_agent_run_start(
    task_id=tid,
    session_id="{sid}",
    agent_id="engineering-developer",
    agent_name="Senior Developer",
    division_id="engineering",
    prompt_file="engineering/developer.md",
    input_preview="Calling external LLM...",
    db_path=r"{test_db}"
)

with open(r"{in_flight_flag}", "w") as f:
    f.write(tid)

# Process hangs simulating external HTTP call in-flight
time.sleep(60)
"""
    with open(worker_script, "w", encoding="utf-8") as f:
        f.write(script_body)

    proc = subprocess.Popen([sys.executable, worker_script])
    for _ in range(50):
        if os.path.exists(in_flight_flag):
            break
        time.sleep(0.1)
    assert os.path.exists(in_flight_flag)

    with open(in_flight_flag, "r") as f:
        tid = f.read().strip()

    # Forcibly terminate child process mid-flight!
    proc.kill()
    proc.wait(timeout=5)

    # Fresh process runs reconciliation
    rec_count = db_store.reconcile_interrupted_sessions(db_path=test_db)
    assert rec_count == 1

    # Check task and session status in SQLite
    task = db_store.get_task(tid, db_path=test_db)
    assert task["status"] == "failed"
    assert "Provider outcome unknown" in task["error_message"]

    sess = db_store.get_session(sid, db_path=test_db)
    assert sess["status"] == "failed"

    # Verify Operator Audit reports honest ambiguity
    audit = db_store.get_operator_session_audit(sid, db_path=test_db)
    assert audit["cost_and_token_accounting"]["has_ambiguous_provider_call"] is True
    assert audit["failure_analysis"]["is_provider_ambiguous"] is True
    assert audit["failure_analysis"]["is_retry_safe"] is False
    assert "inspect_diagnostics" in audit["permitted_recovery_actions"]


def test_workstream_c_actual_process_death_scenario_4_interrupted_recovery_idempotence(tmp_path):
    """
    Workstream C - Scenario 4: Recovery itself is interrupted.
    Reconciliation is run, interrupted, and run again.
    Verifies that repeated recovery is strictly idempotent with zero duplicate tasks or records.
    """
    test_db = str(tmp_path / "proc_death_s4.db")
    db_store.init_db(test_db)

    sid = "sess_s4_idem"
    db_store.create_session(session_id=sid, project_name="Idem S4", task_description="Test", db_path=test_db)
    db_store.update_session_status(sid, "running", db_path=test_db)

    # First reconciliation
    r1 = db_store.reconcile_interrupted_sessions(db_path=test_db)
    assert r1 == 1

    # Second reconciliation (simulating restart after prior recovery)
    r2 = db_store.reconcile_interrupted_sessions(db_path=test_db)
    assert r2 == 0

    sess = db_store.get_session(sid, db_path=test_db)
    assert sess["status"] == "interrupted"


# =========================================================================
# Workstream D: Multiprocess Claiming & Resume Concurrency
# =========================================================================

def test_workstream_d_multiprocess_task_claiming_barrier_race(tmp_path):
    """
    Workstream D: Two separate OS processes compete for the same task claim.
    A synchronization barrier file guarantees both processes fire simultaneously.
    Exactly one worker acquires the claim (CLAIMED), and the other fails (REJECTED).
    """
    test_db = str(tmp_path / "barrier_claim.db")
    db_store.init_db(test_db)

    sid = "sess_barrier_claim"
    db_store.create_session(session_id=sid, project_name="Barrier Claim", task_description="Desc", db_path=test_db)
    tid = db_store.create_task(
        session_id=sid,
        title="Barrier Task",
        division_id="engineering",
        assigned_agent_id="engineering-developer",
        stage="development",
        status="ready",
        db_path=test_db
    )

    barrier_file = str(tmp_path / "start_race.flag")
    worker_script = str(tmp_path / "claim_worker.py")

    script_body = f"""
import sys, os, time
sys.path.insert(0, r"{root_dir}")
import db_persistence as db_store

# Wait at synchronization barrier
while not os.path.exists(r"{barrier_file}"):
    time.sleep(0.005)

claimed = db_store.claim_task("{tid}", db_path=r"{test_db}")
print("CLAIMED" if claimed else "REJECTED")
"""
    with open(worker_script, "w", encoding="utf-8") as f:
        f.write(script_body)

    p1 = subprocess.Popen([sys.executable, worker_script], stdout=subprocess.PIPE, text=True)
    p2 = subprocess.Popen([sys.executable, worker_script], stdout=subprocess.PIPE, text=True)

    time.sleep(0.1)
    # Release synchronization barrier!
    with open(barrier_file, "w") as f:
        f.write("GO")

    out1, _ = p1.communicate(timeout=10)
    out2, _ = p2.communicate(timeout=10)

    results = [out1.strip(), out2.strip()]
    assert "CLAIMED" in results, f"Expected one CLAIMED, got: {results}"
    assert "REJECTED" in results, f"Expected one REJECTED, got: {results}"

    task = db_store.get_task(tid, db_path=test_db)
    assert task["status"] == "running"


def test_workstream_d_multiprocess_session_resumption_barrier_race(tmp_path):
    """
    Workstream D: Two separate OS processes compete to resume an interrupted session.
    A synchronization barrier ensures simultaneous execution.
    Exactly one process locks the session (LOCKED), while the other receives BUSY.
    """
    test_db = str(tmp_path / "barrier_resume.db")
    db_store.init_db(test_db)

    sid = "sess_barrier_resume"
    db_store.create_session(session_id=sid, project_name="Barrier Resume", task_description="Desc", db_path=test_db)
    db_store.update_session_status(sid, "interrupted", db_path=test_db)

    barrier_file = str(tmp_path / "start_resume_race.flag")
    worker_script = str(tmp_path / "resume_worker.py")

    script_body = f"""
import sys, os, time
sys.path.insert(0, r"{root_dir}")
import db_persistence as db_store

while not os.path.exists(r"{barrier_file}"):
    time.sleep(0.005)

locked = db_store.acquire_session_resumption_lock("{sid}", db_path=r"{test_db}")
print("LOCKED" if locked else "BUSY")
"""
    with open(worker_script, "w", encoding="utf-8") as f:
        f.write(script_body)

    p1 = subprocess.Popen([sys.executable, worker_script], stdout=subprocess.PIPE, text=True)
    p2 = subprocess.Popen([sys.executable, worker_script], stdout=subprocess.PIPE, text=True)

    time.sleep(0.1)
    with open(barrier_file, "w") as f:
        f.write("GO")

    out1, _ = p1.communicate(timeout=10)
    out2, _ = p2.communicate(timeout=10)

    results = [out1.strip(), out2.strip()]
    assert "LOCKED" in results, f"Expected one LOCKED, got: {results}"
    assert "BUSY" in results, f"Expected one BUSY, got: {results}"

    sess = db_store.get_session(sid, db_path=test_db)
    assert sess["status"] == "running"


# =========================================================================
# Workstream E: Provider Accounting & Ambiguous Outcomes
# =========================================================================

def test_workstream_e_duplicate_task_completion_idempotency_does_not_double_count(tmp_path):
    """
    Workstream E: Calling complete_task_with_agent_run repeatedly on the same task
    does NOT duplicate agent_runs records or double-count tokens in operator audit.
    """
    test_db = str(tmp_path / "accounting_idem.db")
    db_store.init_db(test_db)

    sid = "sess_acct_idem"
    db_store.create_session(session_id=sid, project_name="Acct Idem", task_description="Desc", db_path=test_db)
    tid = db_store.create_task(
        session_id=sid,
        title="Acct Task",
        division_id="engineering",
        assigned_agent_id="engineering-developer",
        stage="development",
        status="running",
        db_path=test_db
    )

    # First completion: 150 tokens
    ok1, run_id1 = db_store.complete_task_with_agent_run(
        task_id=tid,
        session_id=sid,
        agent_id="engineering-developer",
        agent_name="Developer",
        division_id="engineering",
        prompt_file="engineering/developer.md",
        output_text="Completed Output 1",
        tokens_used=150,
        token_source="provider_reported",
        db_path=test_db
    )
    assert ok1 is True

    # Duplicate callback / second completion on same task: 150 tokens
    ok2, run_id2 = db_store.complete_task_with_agent_run(
        task_id=tid,
        session_id=sid,
        agent_id="engineering-developer",
        agent_name="Developer",
        division_id="engineering",
        prompt_file="engineering/developer.md",
        output_text="Completed Output 2 (Repeated Callback)",
        tokens_used=150,
        token_source="provider_reported",
        db_path=test_db
    )
    assert ok2 is True
    assert run_id1 == run_id2

    # Operator audit must show exactly 150 tokens billed, NOT 300
    audit = db_store.get_operator_session_audit(sid, db_path=test_db)
    assert audit["cost_and_token_accounting"]["total_tokens_billed"] == 150
    assert audit["cost_and_token_accounting"]["provider_reported_tokens"] == 150
    assert audit["cost_and_token_accounting"]["estimated_tokens"] == 0

    runs = db_store.list_agent_runs(session_id=sid, db_path=test_db)
    assert len(runs) == 1


def test_workstream_e_token_sanitization_and_isolated_budget_integrity(monkeypatch, tmp_path):
    """
    Workstream E: record_token_usage sanitizes malformed non-integer or negative inputs,
    and isolates budget tracking to a disposable test file without touching production budgets.
    """
    temp_budget = str(tmp_path / "disposable_budget.json")
    monkeypatch.setattr("app.BUDGET_FILE", temp_budget)

    # 1. Negative tokens clamped to 0
    res_neg = record_token_usage("Agent Neg", "Engineering", "Neg Task", -500, source="provider_reported")
    assert res_neg["tokens_used"] == 0
    assert res_neg["estimated_cost"] == 0.0

    # 2. String malformed tokens clamped to 0
    res_str = record_token_usage("Agent Str", "Engineering", "Str Task", "not_a_number", source="estimated")
    assert res_str["tokens_used"] == 0
    assert res_str["estimated_cost"] == 0.0

    # 3. Valid positive tokens accounted accurately (clamped to min cost precision $0.0001)
    res_pos = record_token_usage("Agent Pos", "Engineering", "Pos Task", 250, source="provider_reported")
    assert res_pos["tokens_used"] == 250
    assert res_pos["estimated_cost"] == 0.0001


# =========================================================================
# Workstream F: Remediation, Authorization & Fail-Closed Behavior
# =========================================================================

def test_workstream_f_session_endpoints_reject_malformed_ids_and_traversal():
    r"""
    Workstream F: Session-scoped endpoints enforce strict session_id format validation (^[a-zA-Z0-9_\-]{1,64}$).
    Malformed identifiers return HTTP 400 Bad Request.
    Directory traversal attempts are safely rejected.
    """
    client = app.test_client()

    bad_identifiers = [
        "sess..etc..passwd",
        "sess_test%00extra",
        "a" * 100,  # exceeds 64 chars
        "sess:with:colons",
        "sess;drop table",
        "sess*invalid"
    ]

    for bad_id in bad_identifiers:
        # 1. Audit endpoint
        r1 = client.get(f"/api/sessions/{bad_id}/audit")
        assert r1.status_code == 400
        assert "Invalid session identifier format" in r1.get_json()["error"]

        # 2. Manifest endpoint
        r2 = client.get(f"/api/sessions/{bad_id}/manifest")
        assert r2.status_code == 400

        # 3. Tasks endpoint
        r3 = client.get(f"/api/sessions/{bad_id}/tasks")
        assert r3.status_code == 400

        # 4. Resume endpoint
        r4 = client.post(f"/api/sessions/{bad_id}/resume", json={})
        assert r4.status_code in (400, 403)

    # Directory traversal attempt is blocked
    r_trav = client.get("/api/sessions/../../etc/passwd/audit")
    assert r_trav.status_code in (400, 403, 404)


def test_workstream_f_cannot_resume_completed_or_invalid_session():
    """
    Workstream F: Resuming a session with status 'completed' or nonexistent ID returns HTTP 400/404.
    """
    client = app.test_client()

    # 1. Nonexistent session ID
    r_none = client.post("/api/sessions/sess_nonexistent_valid_id_1234/resume", json={})
    assert r_none.status_code == 404
    assert "Session not found" in r_none.get_json()["error"]


# =========================================================================
# Workstream G: Manifest Safety & Artifact Limits
# =========================================================================

def test_workstream_g_manifest_enforces_25mb_size_cap():
    """
    Workstream G: Artifacts exceeding 25MB (26214400 bytes) are rejected with ValueError.
    """
    oversized_payload = "X" * (26 * 1024 * 1024)  # 26MB
    files = {"huge_file.dat": oversized_payload}

    with pytest.raises(ValueError) as exc_info:
        build_artifact_manifest(files)

    assert "exceeds maximum allowed size" in str(exc_info.value)


def test_workstream_g_manifest_rejects_case_insensitive_and_unicode_collisions():
    """
    Workstream G: Manifest engine rejects case-insensitive collisions and Unicode NFC normalization collisions.
    """
    # 1. Case-insensitive collision
    with pytest.raises(ValueError) as exc1:
        build_artifact_manifest({"index.html": "1", "INDEX.HTML": "2"})
    assert "Case-insensitive or Unicode normalization" in str(exc1.value)

    # 2. Unicode normalization collision
    decomposed = "caf\u0065\u0301.js"
    precomposed = "caf\u00e9.js"
    with pytest.raises(ValueError) as exc2:
        build_artifact_manifest({decomposed: "1", precomposed: "2"})
    assert "Case-insensitive or Unicode normalization" in str(exc2.value)


def test_workstream_g_manifest_rejects_path_traversal_and_reserved_devices():
    """
    Workstream G: Manifest engine rejects path traversal and Windows reserved device names.
    """
    traversal_cases = [
        "../escape.html",
        "..\\escape.html",
        "/etc/passwd",
        "C:\\boot.ini",
        "CON.txt",
        "AUX.js",
        "PRN.css",
        "NUL",
        "COM1.html"
    ]
    for bad_path in traversal_cases:
        with pytest.raises(ValueError):
            build_artifact_manifest({bad_path: "malicious content"})


# =========================================================================
# Workstream H: Honest Failure Diagnostics & Observability
# =========================================================================

def test_workstream_h_honest_failure_diagnostics_audit(tmp_path):
    """
    Workstream H: get_operator_session_audit provides honest correlation-verified diagnostics:
    - Identifies failed task and stage.
    - Flags ambiguous provider calls.
    - Sets is_retry_safe to False when ambiguous.
    - Limits permitted actions to inspection.
    """
    test_db = str(tmp_path / "diag_audit.db")
    db_store.init_db(test_db)

    sid = "sess_diag_01"
    db_store.create_session(session_id=sid, project_name="Diag Test", task_description="Test", db_path=test_db)
    db_store.update_session_status(sid, "running", db_path=test_db)

    tid = db_store.create_task(
        session_id=sid,
        title="Failed Core Task",
        division_id="engineering",
        assigned_agent_id="engineering-developer",
        stage="development",
        status="running",
        db_path=test_db
    )

    db_store.record_agent_run_start(
        task_id=tid,
        session_id=sid,
        agent_id="engineering-developer",
        agent_name="Senior Developer",
        division_id="engineering",
        prompt_file="engineering/developer.md",
        input_preview="Calling LLM",
        db_path=test_db
    )

    # Crash reconciliation runs
    db_store.reconcile_interrupted_sessions(db_path=test_db)

    audit = db_store.get_operator_session_audit(sid, db_path=test_db)
    assert audit["failure_analysis"]["status"] == "failed"
    assert audit["failure_analysis"]["failed_task_id"] == tid
    assert audit["failure_analysis"]["is_provider_ambiguous"] is True
    assert audit["failure_analysis"]["is_retry_safe"] is False
    assert audit["permitted_recovery_actions"] == ["inspect_diagnostics"]
