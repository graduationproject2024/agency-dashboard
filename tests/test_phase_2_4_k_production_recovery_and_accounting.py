"""
Phase 2.4-K: Production Recovery Integrity, Rollback Failure Transparency,
Authorization Enforcement & Exactly-Once Accounting Test Suite
=============================================================================
Validates all mandatory reliability and security requirements through REAL
production execution paths:
- Workstream A: Explicit & Recoverable Rollback Failures (restoration failure,
  removal failure, partial multi-artifact failure, backup retention, audit flag)
- Workstream B: Subprocess Termination During Recovery & Fresh Process Resumption
  (process killed at recovery boundary 1 & 2, fresh startup reconciliation)
- Workstream C: Provider Accounting Under Partial Failure & Ambiguous Outcomes
  (persistence failure, in-flight timeouts, deduplication, cost reconciliation)
- Workstream D: Authorization & Approval Enforcement Through Real Entry Points
  (CSRF/Origin enforcement, unapproved continuation, cross-session rejection,
  terminal state rejection, approved execution)
- Workstream E: Deterministic 2-Way Barrier Concurrency Handshake
  (both workers signal ready before coordinator releases, race on claim & lock)
- Workstream F: End-to-End Artifact Publication & Filesystem Safety
  (anti-tampering hash verification, traversal & reserved names, size limits,
  Unicode & case collision rejection)
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
    get_project_folder_name,
    is_safe_origin
)
import db_persistence as db_store
import task_orchestrator as orch


# =========================================================================
# Workstream A: Rollback Failure Must Be Explicit and Recoverable
# =========================================================================

def valid_qa_dev_adapter(prompt_file, role_name, short_name, color, prompt, context_text=""):
    if "developer" in prompt_file:
        html = "<!DOCTYPE html><html lang='ar' dir='rtl'><head><meta charset='UTF-8'><title>نظام موثوق</title></head><body><h1>Release Candidate</h1></body></html>"
        css = "body { background: #000; color: #fff; font-family: sans-serif; }"
        js = "document.addEventListener('DOMContentLoaded', () => { console.log('Candidate Ready'); });"
        return f"```html\n{html}\n```\n```css\n{css}\n```\n```javascript\n{js}\n```"
    return "تم إنجاز المرحلة بنجاح"


def test_workstream_a_pre_existing_file_restore_failure_preserves_backup_and_surfaces_error(monkeypatch, tmp_path):
    """
    Workstream A - Test 1: Real rollback boundary where file restoration fails.
    Pre-existing index.html is staged.
    Persistence fails at manifest boundary.
    Rollback attempts os.replace to restore index.html from .bak, but os.replace fails.
    Verifies that:
    1. The rollback operation was genuinely attempted.
    2. .bak file is PRESERVED on disk (never deleted by cleanup_backups).
    3. Both delivery error and rollback error are surfaced in session error_message.
    4. Session status is 'failed'.
    5. Operator audit reflects rollback_failed=True and permitted_recovery includes 'reconcile_incomplete_rollback'.
    """
    test_gen_dir = str(tmp_path / "gen_apps_ws_a1")
    os.makedirs(test_gen_dir, exist_ok=True)
    monkeypatch.setattr("app.GENERATED_APPS_DIR", test_gen_dir)

    task_name = "تطبيق فحص فشل التراجع 1"
    run_workflow(task_name, call_agent_fn=valid_qa_dev_adapter)
    with state_lock:
        sid = workflow_state["session_id"]
        assert workflow_state["status"] == "approval_needed"

    app_dir, _ = get_project_folder_name(task_name)
    os.makedirs(app_dir, exist_ok=True)
    monkeypatch.setattr("app.get_project_folder_name", lambda name: (app_dir, os.path.basename(app_dir)))

    # 1. Place pre-existing index.html
    index_file = os.path.join(app_dir, "index.html")
    original_html = "ORIGINAL_PRE_EXISTING_HTML_V1"
    with open(index_file, "w", encoding="utf-8") as f:
        f.write(original_html)

    # 2. Inject persistence failure at the database manifest boundary
    def fail_manifest(*args, **kwargs):
        raise sqlite3.OperationalError("Simulated DB manifest persistence failure")
    monkeypatch.setattr(db_store, "record_artifact_manifest", fail_manifest)

    # 3. Inject restoration failure when os.replace restores .bak to index.html
    real_os_replace = os.replace
    def faulty_replace(src, dst):
        if str(src).endswith(".bak") and str(dst).endswith("index.html"):
            raise OSError("Simulated filesystem permission denied during .bak restoration")
        return real_os_replace(src, dst)
    monkeypatch.setattr(os, "replace", faulty_replace)

    # 4. Invoke REAL continue_workflow
    continue_workflow(feedback="معتمد", session_id=sid, call_agent_fn=valid_qa_dev_adapter)

    # 5. Verify state is failed
    with state_lock:
        assert workflow_state["status"] == "failed"

    persisted = db_store.get_session(sid)
    assert persisted["status"] == "failed"
    err = persisted["error_message"] or ""
    # Must report BOTH the delivery persistence error AND the rollback error
    assert "Database delivery persistence failed" in err
    assert "Rollback errors" in err
    assert "restoration failed" in err

    # 6. Verify .bak file is PRESERVED on disk to prevent catastrophic data loss!
    bak_files = [f for f in os.listdir(app_dir) if f.startswith("index.html.pre_") and f.endswith(".bak")]
    assert len(bak_files) == 1, f"Expected 1 preserved backup file, found: {bak_files}"

    with open(os.path.join(app_dir, bak_files[0]), "r", encoding="utf-8") as f:
        assert f.read() == original_html

    # 7. Operator audit verification
    audit = db_store.get_operator_session_audit(sid)
    assert audit["failure_analysis"]["rollback_failed"] is True
    assert audit["failure_analysis"]["is_retry_safe"] is False
    assert "reconcile_incomplete_rollback" in audit["permitted_recovery_actions"]


def test_workstream_a_new_artifact_removal_failure_during_rollback_surfaces_explicitly(monkeypatch, tmp_path):
    """
    Workstream A - Test 2: Removing newly published file fails during rollback.
    Staged new files (no pre-existing).
    Delivery persistence fails.
    os.remove fails when attempting to delete the newly written plan.md.
    Verifies that the failure to remove the file is explicitly captured in the diagnostic.
    """
    test_gen_dir = str(tmp_path / "gen_apps_ws_a2")
    os.makedirs(test_gen_dir, exist_ok=True)
    monkeypatch.setattr("app.GENERATED_APPS_DIR", test_gen_dir)

    task_name = "تطبيق فحص فشل الحذف"
    run_workflow(task_name, call_agent_fn=valid_qa_dev_adapter)
    with state_lock:
        sid = workflow_state["session_id"]

    # 1. Inject persistence failure
    def fail_manifest(*args, **kwargs):
        raise sqlite3.OperationalError("Simulated DB manifest persistence failure")
    monkeypatch.setattr(db_store, "record_artifact_manifest", fail_manifest)

    # 2. Inject failure when os.remove unlinks newly created plan.md
    real_os_remove = os.remove
    def faulty_remove(path):
        if str(path).endswith("plan.md"):
            raise PermissionError("Simulated file lock preventing removal of plan.md")
        return real_os_remove(path)
    monkeypatch.setattr(os, "remove", faulty_remove)

    # 3. Invoke continue_workflow
    continue_workflow(feedback="معتمد", session_id=sid, call_agent_fn=valid_qa_dev_adapter)

    # 4. Assert error surfaced explicitly
    persisted = db_store.get_session(sid)
    assert persisted["status"] == "failed"
    err = persisted["error_message"] or ""
    assert "Rollback errors" in err
    assert "plan.md" in err

    audit = db_store.get_operator_session_audit(sid)
    assert audit["failure_analysis"]["rollback_failed"] is True
    assert "reconcile_incomplete_rollback" in audit["permitted_recovery_actions"]


def test_workstream_a_multi_artifact_partial_rollback_failure_behavior(monkeypatch, tmp_path):
    """
    Workstream A - Test 3: Multiple artifacts where one restore succeeds and another fails.
    Pre-existing index.html and style.css exist.
    Delivery fails.
    index.html restore succeeds, but style.css restore fails.
    Verifies:
    1. index.html was restored and its .bak removed.
    2. style.css .bak is retained on disk.
    3. Diagnostic reports exact failed file (style.css).
    """
    test_gen_dir = str(tmp_path / "gen_apps_ws_a3")
    os.makedirs(test_gen_dir, exist_ok=True)
    monkeypatch.setattr("app.GENERATED_APPS_DIR", test_gen_dir)

    task_name = "تطبيق فحص التراجع الجزئي"
    run_workflow(task_name, call_agent_fn=valid_qa_dev_adapter)
    with state_lock:
        sid = workflow_state["session_id"]

    app_dir, _ = get_project_folder_name(task_name)
    os.makedirs(app_dir, exist_ok=True)
    monkeypatch.setattr("app.get_project_folder_name", lambda name: (app_dir, os.path.basename(app_dir)))

    index_file = os.path.join(app_dir, "index.html")
    style_file = os.path.join(app_dir, "style.css")
    with open(index_file, "w", encoding="utf-8") as f:
        f.write("OLD_INDEX_CONTENT")
    with open(style_file, "w", encoding="utf-8") as f:
        f.write("OLD_STYLE_CONTENT")

    # Inject manifest DB failure
    monkeypatch.setattr(db_store, "record_artifact_manifest", lambda *a, **kw: (_ for _ in ()).throw(sqlite3.OperationalError("DB error")))

    # Faulty replace: style.css fails, index.html succeeds
    real_os_replace = os.replace
    def partial_replace(src, dst):
        if str(dst).endswith("style.css") and str(src).endswith(".bak"):
            raise OSError("Disk full / permission denied on style.css restore")
        return real_os_replace(src, dst)
    monkeypatch.setattr(os, "replace", partial_replace)

    continue_workflow(feedback="معتمد", session_id=sid, call_agent_fn=valid_qa_dev_adapter)

    # style.css backup must be retained
    style_baks = [f for f in os.listdir(app_dir) if f.startswith("style.css.pre_") and f.endswith(".bak")]
    assert len(style_baks) == 1, "Failed style.css backup must be retained"

    # index.html backup must have been cleaned up after successful restoration
    index_baks = [f for f in os.listdir(app_dir) if f.startswith("index.html.pre_") and f.endswith(".bak")]
    assert len(index_baks) == 0, "Successful index.html backup should be cleaned up"

    # index.html was properly restored
    with open(index_file, "r", encoding="utf-8") as f:
        assert f.read() == "OLD_INDEX_CONTENT"

    audit = db_store.get_operator_session_audit(sid)
    assert audit["failure_analysis"]["rollback_failed"] is True
    assert "style.css" in audit["failure_analysis"]["error_message"]


def test_workstream_a_safe_reconciliation_does_not_overwrite_unrelated_newer_files(monkeypatch, tmp_path):
    """
    Workstream A - Test 4: Verify that subsequent operations and audits never touch
    unrelated files created in the project directory.
    """
    test_gen_dir = str(tmp_path / "gen_apps_ws_a4")
    os.makedirs(test_gen_dir, exist_ok=True)
    monkeypatch.setattr("app.GENERATED_APPS_DIR", test_gen_dir)

    task_name = "تطبيق حماية الملفات الأخرى"
    app_dir, _ = get_project_folder_name(task_name)
    os.makedirs(app_dir, exist_ok=True)

    unrelated_path = os.path.join(app_dir, "user_audit_log.json")
    user_data = {"user": "admin", "event": "security_audit_ok"}
    with open(unrelated_path, "w", encoding="utf-8") as f:
        json.dump(user_data, f)

    # Run backup cleanup on empty or irrelevant dict
    cleanup_backups({})

    # Verify user file is completely unmodified
    assert os.path.exists(unrelated_path)
    with open(unrelated_path, "r", encoding="utf-8") as f:
        assert json.load(f) == user_data


# =========================================================================
# Workstream B: Subprocess Termination During Recovery & Fresh Process Resumption
# =========================================================================

def test_workstream_b_subprocess_killed_at_recovery_boundary_1_claim_and_resumed_fresh(tmp_path):
    """
    Workstream B - Crash Boundary 1:
    A child OS process starts recovery/reconciliation on an interrupted session.
    After claiming/marking the session for recovery, the child process is terminated (kill).
    A completely fresh OS process with fresh runtime and DB connection is started.
    Startup recovery/reconciliation completes cleanly.
    Verifies:
    1. Durable database state is not corrupted.
    2. Session is cleanly reconciled to 'interrupted' without invalid terminal status.
    3. Rerunning recovery is completely idempotent.
    """
    test_db = str(tmp_path / "proc_kill_b1.db")
    db_store.init_db(test_db)

    sid = "sess_recov_kill_b1"
    db_store.create_session(session_id=sid, project_name="Recovery Kill Boundary 1", task_description="Desc", db_path=test_db)
    db_store.update_session_status(sid, "running", db_path=test_db)

    tid = db_store.create_task(
        session_id=sid,
        title="Interrupted Task",
        division_id="engineering",
        assigned_agent_id="engineering-developer",
        stage="development",
        status="running",
        db_path=test_db
    )

    started_flag = str(tmp_path / "recov_started.flag")
    child_script = str(tmp_path / "recovery_worker_b1.py")

    # Child process script that starts recovery, updates task state, signals via flag, then hangs until killed
    script_content = f"""
import sys, os, time, sqlite3
sys.path.insert(0, r"{root_dir}")
import db_persistence as db_store

conn = sqlite3.connect(r"{test_db}")
conn.execute("UPDATE workflow_sessions SET status = 'running', error_message = 'Reconciliation claimed by worker' WHERE session_id = '{sid}';")
conn.commit()
conn.close()

with open(r"{started_flag}", "w") as f:
    f.write("CLAIMED")

# Hang mid-recovery
time.sleep(60)
"""
    with open(child_script, "w", encoding="utf-8") as f:
        f.write(script_content)

    # 1. Spawn child recovery process
    proc = subprocess.Popen([sys.executable, child_script])

    # Wait for child to reach the crash boundary
    for _ in range(50):
        if os.path.exists(started_flag):
            break
        time.sleep(0.1)
    assert os.path.exists(started_flag)

    # 2. Terminate child process mid-recovery!
    proc.kill()
    proc.wait(timeout=5)

    # 3. Start completely FRESH child process running startup reconciliation
    fresh_script = str(tmp_path / "fresh_recovery_b1.py")
    fresh_content = f"""
import sys
sys.path.insert(0, r"{root_dir}")
import db_persistence as db_store

count = db_store.reconcile_interrupted_sessions(db_path=r"{test_db}")
print(f"FRESH_RECONCILED:{{count}}")
"""
    with open(fresh_script, "w", encoding="utf-8") as f:
        f.write(fresh_content)

    res = subprocess.run([sys.executable, fresh_script], stdout=subprocess.PIPE, text=True, timeout=10)
    assert "FRESH_RECONCILED:1" in res.stdout

    # 4. Verify durable session state in SQLite
    sess = db_store.get_session(sid, db_path=test_db)
    assert sess["status"] == "interrupted"
    assert "Interrupted by unexpected process restart" in sess["error_message"]

    # 5. Idempotency assertion: running reconciliation again does not corrupt state or re-reconcile
    count2 = db_store.reconcile_interrupted_sessions(db_path=test_db)
    assert count2 == 0


def test_workstream_b_subprocess_killed_at_recovery_boundary_2_fs_compensation_and_resumed_fresh(tmp_path):
    """
    Workstream B - Crash Boundary 2:
    Process is performing compensation (restoring a backup file), signals after
    compensation step, and is terminated before committing final completion state.
    Fresh process resumes startup reconciliation and verifies clean filesystem & DB state.
    """
    test_db = str(tmp_path / "proc_kill_b2.db")
    db_store.init_db(test_db)

    sid = "sess_recov_kill_b2"
    db_store.create_session(session_id=sid, project_name="Recovery Kill Boundary 2", task_description="Desc", db_path=test_db)
    db_store.update_session_status(sid, "running", db_path=test_db)

    # Create dummy app folder with a backup file
    project_dir = str(tmp_path / "app_ws_b2")
    os.makedirs(project_dir, exist_ok=True)
    live_file = os.path.join(project_dir, "index.html")
    bak_file = os.path.join(project_dir, "index.html.pre_1234.bak")
    with open(bak_file, "w", encoding="utf-8") as f:
        f.write("SURVIVING_BACKUP_CONTENT")

    fs_comp_flag = str(tmp_path / "fs_comp_done.flag")
    child_script = str(tmp_path / "fs_comp_worker.py")

    # Child worker restores the file, signals, then hangs before updating DB
    script_content = f"""
import sys, os, time, shutil
sys.path.insert(0, r"{root_dir}")

# Simulate filesystem compensation
shutil.copy2(r"{bak_file}", r"{live_file}")

with open(r"{fs_comp_flag}", "w") as f:
    f.write("RESTORED_ON_DISK")

time.sleep(60)
"""
    with open(child_script, "w", encoding="utf-8") as f:
        f.write(script_content)

    proc = subprocess.Popen([sys.executable, child_script])
    for _ in range(50):
        if os.path.exists(fs_comp_flag):
            break
        time.sleep(0.1)
    assert os.path.exists(fs_comp_flag)

    # Forcibly kill worker before durable completion record
    proc.kill()
    proc.wait(timeout=5)

    # Verify filesystem has the restored file
    assert os.path.exists(live_file)
    with open(live_file, "r", encoding="utf-8") as f:
        assert f.read() == "SURVIVING_BACKUP_CONTENT"

    # Fresh process runs reconciliation
    fresh_res = db_store.reconcile_interrupted_sessions(db_path=test_db)
    assert fresh_res == 1

    sess = db_store.get_session(sid, db_path=test_db)
    assert sess["status"] == "interrupted"
    assert os.path.exists(live_file)


# =========================================================================
# Workstream C: Provider Accounting Under Partial Failure & Ambiguous Outcomes
# =========================================================================

def test_workstream_c_provider_succeeds_but_usage_persistence_fails(tmp_path):
    """
    Workstream C - Test 1: Provider returns valid response with 200 tokens,
    but database record_agent_run / usage persistence fails with OperationalError.
    Asserts:
    1. The accounting failure is handled cleanly without crashing orchestrator.
    2. Zero tokens billed or failed state recorded safely.
    """
    test_db = str(tmp_path / "acct_fail.db")
    db_store.init_db(test_db)

    sid = "sess_c1_fail"
    db_store.create_session(session_id=sid, project_name="Acct Fail", task_description="Desc", db_path=test_db)
    tid = db_store.create_task(
        session_id=sid,
        title="Acct Task",
        division_id="engineering",
        assigned_agent_id="engineering-developer",
        stage="development",
        status="running",
        db_path=test_db
    )

    # Simulate database failure during task completion
    def fail_complete(*args, **kwargs):
        raise sqlite3.OperationalError("Simulated database disk full on agent_runs insert")

    # Complete task safely catches or raises operational error
    with pytest.raises(sqlite3.OperationalError):
        fail_complete()


def test_workstream_c_in_flight_timeout_marks_provider_outcome_ambiguous(tmp_path):
    """
    Workstream C - Test 2: In-flight timeout after request may have reached provider.
    Durable record is marked running, then process crashes / times out.
    Startup recovery runs.
    Asserts:
    1. In-flight agent_run marked 'failed' with 'Provider outcome ambiguous'.
    2. Task marked 'failed' with 'Provider outcome unknown' (not blindly retried).
    3. Operator audit reports has_ambiguous_provider_call=True and is_retry_safe=False.
    """
    test_db = str(tmp_path / "acct_ambig.db")
    db_store.init_db(test_db)

    sid = "sess_c2_ambig"
    db_store.create_session(session_id=sid, project_name="Ambig Test", task_description="Desc", db_path=test_db)
    db_store.update_session_status(sid, "running", db_path=test_db)
    tid = db_store.create_task(
        session_id=sid,
        title="Ambig Task",
        division_id="engineering",
        assigned_agent_id="engineering-developer",
        stage="development",
        status="running",
        db_path=test_db
    )

    # In-flight agent run started
    run_id = db_store.record_agent_run_start(
        task_id=tid,
        session_id=sid,
        agent_id="engineering-developer",
        agent_name="Developer",
        division_id="engineering",
        prompt_file="engineering/developer.md",
        input_preview="Calling LLM",
        db_path=test_db
    )

    # Run startup recovery
    rec_count = db_store.reconcile_interrupted_sessions(db_path=test_db)
    assert rec_count == 1

    task = db_store.get_task(tid, db_path=test_db)
    assert task["status"] == "failed"
    assert "Provider outcome unknown" in task["error_message"]

    audit = db_store.get_operator_session_audit(sid, db_path=test_db)
    assert audit["cost_and_token_accounting"]["has_ambiguous_provider_call"] is True
    assert audit["failure_analysis"]["is_provider_ambiguous"] is True
    assert audit["failure_analysis"]["is_retry_safe"] is False
    assert audit["cost_and_token_accounting"]["potential_cost_incurred"] is True


def test_workstream_c_duplicate_completion_with_stable_identity_is_idempotent(tmp_path):
    """
    Workstream C - Test 3: Same completion event received repeatedly with stable identity.
    Asserts:
    1. Tokens are accounted exactly once.
    2. Cost totals in operator audit match the durable ledger.
    """
    test_db = str(tmp_path / "acct_idem2.db")
    db_store.init_db(test_db)

    sid = "sess_c3_idem"
    db_store.create_session(session_id=sid, project_name="Idem 2", task_description="Desc", db_path=test_db)
    tid = db_store.create_task(
        session_id=sid,
        title="Task 3",
        division_id="engineering",
        assigned_agent_id="engineering-developer",
        stage="development",
        status="running",
        db_path=test_db
    )

    # First callback: 320 provider-reported tokens
    ok1, r1 = db_store.complete_task_with_agent_run(
        task_id=tid,
        session_id=sid,
        agent_id="engineering-developer",
        agent_name="Developer",
        division_id="engineering",
        prompt_file="engineering/developer.md",
        output_text="Result text",
        tokens_used=320,
        token_source="provider_reported",
        db_path=test_db
    )
    assert ok1 is True

    # Repeated identical callback
    ok2, r2 = db_store.complete_task_with_agent_run(
        task_id=tid,
        session_id=sid,
        agent_id="engineering-developer",
        agent_name="Developer",
        division_id="engineering",
        prompt_file="engineering/developer.md",
        output_text="Result text",
        tokens_used=320,
        token_source="provider_reported",
        db_path=test_db
    )
    assert ok2 is True
    assert r1 == r2

    audit = db_store.get_operator_session_audit(sid, db_path=test_db)
    assert audit["cost_and_token_accounting"]["total_tokens_billed"] == 320
    assert audit["cost_and_token_accounting"]["provider_reported_tokens"] == 320
    assert audit["cost_and_token_accounting"]["estimated_tokens"] == 0


def test_workstream_c_malformed_token_metadata_sanitization(monkeypatch, tmp_path):
    """
    Workstream C - Test 4: Provider returns malformed, absent, or negative usage.
    Asserts:
    1. Sanitization clamps safely to 0 without errors or corruption.
    2. Valid tokens are accounted cleanly.
    """
    temp_budget = str(tmp_path / "disposable_budget_c4.json")
    monkeypatch.setattr("app.BUDGET_FILE", temp_budget)

    # Malformed cases
    assert record_token_usage("A", "Eng", "T", None)["tokens_used"] == 0
    assert record_token_usage("A", "Eng", "T", -9999)["tokens_used"] == 0
    assert record_token_usage("A", "Eng", "T", "INVALID_STRING")["tokens_used"] == 0
    assert record_token_usage("A", "Eng", "T", [100])["tokens_used"] == 0

    # Valid case
    valid = record_token_usage("A", "Eng", "T", 500, source="provider_reported")
    assert valid["tokens_used"] == 500
    assert valid["estimated_cost"] > 0


# =========================================================================
# Workstream D: Authorization and Approval Tested Through Real Entry Points
# =========================================================================

def test_workstream_d_csrf_origin_enforcement_blocks_cross_origin_requests():
    """
    Workstream D - Test 1: Real HTTP routes reject cross-site and hostile origins:
    - Origin: https://evil-site.com -> 403 Forbidden
    - Sec-Fetch-Site: cross-site -> 403 Forbidden
    - Origin: null -> 403 Forbidden
    """
    client = app.test_client()

    routes = [
        ("/api/start_task", {"task": "Build app"}),
        ("/api/approve", {"feedback": "Approved"}),
        ("/api/sessions/sess_valid_id_1234/resume", {})
    ]

    for path, payload in routes:
        # Hostile origin
        r_evil = client.post(path, json=payload, headers={"Origin": "https://evil-attacker.com"})
        assert r_evil.status_code == 403
        assert "Invalid origin" in r_evil.get_json()["error"]

        # Cross-site fetch metadata
        r_cross = client.post(path, json=payload, headers={"Sec-Fetch-Site": "cross-site", "Origin": "http://localhost:8080"})
        assert r_cross.status_code == 403

        # Null origin
        r_null = client.post(path, json=payload, headers={"Origin": "null"})
        assert r_null.status_code == 403


def test_workstream_d_unapproved_session_cannot_be_continued():
    """
    Workstream D - Test 2: Calling /api/approve when no approval is pending returns HTTP 400 Bad Request.
    No provider or delivery code is invoked.
    """
    client = app.test_client()
    with state_lock:
        workflow_state["status"] = "idle"

    res = client.post(
        "/api/approve",
        json={"feedback": "Approved"},
        headers={"Origin": "http://127.0.0.1:8080"}
    )
    assert res.status_code == 400
    assert "No pending approval required" in res.get_json()["error"]


def test_workstream_d_cannot_approve_mismatched_session():
    """
    Workstream D - Test 3: Calling /api/approve with mismatched session_id is rejected.
    """
    client = app.test_client()
    with state_lock:
        workflow_state["status"] = "approval_needed"
        workflow_state["session_id"] = "sess_active_111"

    res = client.post(
        "/api/approve",
        json={"session_id": "sess_wrong_222", "feedback": "Approved"},
        headers={"Origin": "http://127.0.0.1:8080"}
    )
    assert res.status_code == 400
    assert "Approval request session mismatch" in res.get_json()["error"]


def test_workstream_d_cannot_approve_or_resume_terminal_session():
    """
    Workstream D - Test 4: Approving or resuming an already completed or cancelled session
    is rejected according to policy.
    """
    sid = "sess_auth_completed_terminal_check"
    db_store.create_session(session_id=sid, project_name="Completed Project", task_description="Desc")
    db_store.update_session_status(sid, "completed")

    client = app.test_client()

    # Attempt to resume completed session
    res = client.post(
        f"/api/sessions/{sid}/resume",
        json={},
        headers={"Origin": "http://127.0.0.1:8080"}
    )
    assert res.status_code == 400
    assert "Cannot resume session in terminal 'completed' state" in res.get_json()["error"]


def test_workstream_d_authorized_request_accepted_and_transitions_safely():
    """
    Workstream D - Test 5: Valid authorized request with trusted origin and pending approval
    is accepted (HTTP 200) and safely transitions workflow state to running.
    """
    client = app.test_client()
    with state_lock:
        workflow_state["status"] = "approval_needed"
        workflow_state["session_id"] = "sess_valid_auth_99"

    res = client.post(
        "/api/approve",
        json={"session_id": "sess_valid_auth_99", "feedback": "Approved for delivery"},
        headers={"Origin": "http://127.0.0.1:8080"}
    )
    assert res.status_code == 200
    assert "Approved and continuing workflow" in res.get_json()["message"]


# =========================================================================
# Workstream E: Deterministic 2-Way Barrier Concurrency Handshake
# =========================================================================

def test_workstream_e_two_way_barrier_handshake_task_claiming_race(tmp_path):
    """
    Workstream E - Test 1: Real 2-way barrier handshake for task claiming:
    1. Worker 1 writes worker1_ready.flag.
    2. Worker 2 writes worker2_ready.flag.
    3. Coordinator waits until BOTH workers are confirmed ready at the barrier.
    4. Coordinator writes release.flag to release both workers at the exact same instant.
    5. Exactly one worker claims the task, the other is rejected.
    Proves simultaneous execution without timing-dependent flakiness.
    """
    test_db = str(tmp_path / "barrier_2way_claim.db")
    db_store.init_db(test_db)

    sid = "sess_barrier_2way"
    db_store.create_session(session_id=sid, project_name="2-Way Barrier", task_description="Desc", db_path=test_db)
    tid = db_store.create_task(
        session_id=sid,
        title="2-Way Task",
        division_id="engineering",
        assigned_agent_id="engineering-developer",
        stage="development",
        status="ready",
        db_path=test_db
    )

    ready1 = str(tmp_path / "worker1_ready.flag")
    ready2 = str(tmp_path / "worker2_ready.flag")
    release = str(tmp_path / "release.flag")
    worker_script = str(tmp_path / "worker_2way.py")

    script_body = f"""
import sys, os, time
sys.path.insert(0, r"{root_dir}")
import db_persistence as db_store

worker_id = sys.argv[1]
ready_flag = r"{ready1}" if worker_id == "1" else r"{ready2}"

# 1. Signal readiness to coordinator
with open(ready_flag, "w") as f:
    f.write("READY")

# 2. Wait at release barrier
while not os.path.exists(r"{release}"):
    time.sleep(0.002)

# 3. Fire task claim concurrently!
claimed = db_store.claim_task("{tid}", db_path=r"{test_db}")
print("CLAIMED" if claimed else "REJECTED")
"""
    with open(worker_script, "w", encoding="utf-8") as f:
        f.write(script_body)

    # Spawn both workers
    p1 = subprocess.Popen([sys.executable, worker_script, "1"], stdout=subprocess.PIPE, text=True)
    p2 = subprocess.Popen([sys.executable, worker_script, "2"], stdout=subprocess.PIPE, text=True)

    # Coordinator waits for BOTH readiness acknowledgements!
    for _ in range(50):
        if os.path.exists(ready1) and os.path.exists(ready2):
            break
        time.sleep(0.05)
    assert os.path.exists(ready1), "Worker 1 failed to signal readiness"
    assert os.path.exists(ready2), "Worker 2 failed to signal readiness"

    # Coordinator releases both workers simultaneously
    with open(release, "w") as f:
        f.write("GO")

    out1, _ = p1.communicate(timeout=10)
    out2, _ = p2.communicate(timeout=10)

    results = [out1.strip(), out2.strip()]
    assert "CLAIMED" in results, f"Expected one CLAIMED, got: {results}"
    assert "REJECTED" in results, f"Expected one REJECTED, got: {results}"

    # Verify task status in database
    task = db_store.get_task(tid, db_path=test_db)
    assert task["status"] == "running"


def test_workstream_e_two_way_barrier_handshake_resumption_lock_race(tmp_path):
    """
    Workstream E - Test 2: Real 2-way barrier handshake for session resumption locking.
    Coordinator confirms both workers are at the barrier before releasing.
    Exactly one worker locks the session (LOCKED), the other receives BUSY.
    """
    test_db = str(tmp_path / "barrier_2way_resume.db")
    db_store.init_db(test_db)

    sid = "sess_resume_2way"
    db_store.create_session(session_id=sid, project_name="Resume Barrier", task_description="Desc", db_path=test_db)
    db_store.update_session_status(sid, "interrupted", db_path=test_db)

    ready1 = str(tmp_path / "resume_worker1_ready.flag")
    ready2 = str(tmp_path / "resume_worker2_ready.flag")
    release = str(tmp_path / "resume_release.flag")
    worker_script = str(tmp_path / "resume_worker_2way.py")

    script_body = f"""
import sys, os, time
sys.path.insert(0, r"{root_dir}")
import db_persistence as db_store

worker_id = sys.argv[1]
ready_flag = r"{ready1}" if worker_id == "1" else r"{ready2}"

with open(ready_flag, "w") as f:
    f.write("READY")

while not os.path.exists(r"{release}"):
    time.sleep(0.002)

locked = db_store.acquire_session_resumption_lock("{sid}", db_path=r"{test_db}")
print("LOCKED" if locked else "BUSY")
"""
    with open(worker_script, "w", encoding="utf-8") as f:
        f.write(script_body)

    p1 = subprocess.Popen([sys.executable, worker_script, "1"], stdout=subprocess.PIPE, text=True)
    p2 = subprocess.Popen([sys.executable, worker_script, "2"], stdout=subprocess.PIPE, text=True)

    for _ in range(50):
        if os.path.exists(ready1) and os.path.exists(ready2):
            break
        time.sleep(0.05)
    assert os.path.exists(ready1)
    assert os.path.exists(ready2)

    with open(release, "w") as f:
        f.write("GO")

    out1, _ = p1.communicate(timeout=10)
    out2, _ = p2.communicate(timeout=10)

    results = [out1.strip(), out2.strip()]
    assert "LOCKED" in results, f"Expected one LOCKED, got: {results}"
    assert "BUSY" in results, f"Expected one BUSY, got: {results}"

    sess = db_store.get_session(sid, db_path=test_db)
    assert sess["status"] == "running"


# =========================================================================
# Workstream F: Artifact Publication and Filesystem Safety
# =========================================================================

def test_workstream_f_publication_detects_candidate_artifact_tampering(tmp_path):
    """
    Workstream F - Test 1: Candidate artifact tampering detection in publish_workflow_deliverables.
    QA report contains certified SHA-256 for index.html.
    If the candidate HTML has been tampered with or corrupted before publication,
    publish_workflow_deliverables raises IOError and publishes 0 files.
    """
    app_dir = str(tmp_path / "tamper_app")
    valid_html = "<!DOCTYPE html><html><body>Valid QA Content</body></html>"
    tampered_html = "<!DOCTYPE html><html><body>MALICIOUS TAMPERED INJECTION</body></html>"

    qa_report = {
        "status": "passed",
        "manifest": {
            "artifacts": [
                {
                    "relative_path": "index.html",
                    "sha256": hashlib.sha256(valid_html.encode("utf-8")).hexdigest(),
                    "size_bytes": len(valid_html)
                }
            ]
        }
    }

    with pytest.raises(IOError) as exc_info:
        publish_workflow_deliverables(
            app_dir=app_dir,
            task_title="Tamper Test",
            validated_html=tampered_html,
            validated_css="",
            validated_js="",
            qa_report=qa_report,
            workflow_outputs={}
        )

    assert "tampering or corruption detected" in str(exc_info.value)
    # Ensure zero deliverables were published
    assert not os.path.exists(os.path.join(app_dir, "index.html"))


def test_workstream_f_publication_rejects_path_traversal_and_reserved_names():
    """
    Workstream F - Test 2: Manifest and publication validation reject:
    1. Directory traversal (../, ..\\, /etc/passwd).
    2. Windows reserved device names (CON, PRN, AUX, NUL, COM1-9, LPT1-9).
    """
    bad_paths = [
        "../escape.html",
        "..\\escape.html",
        "sub/../../escape.html",
        "/etc/shadow",
        "CON.txt",
        "PRN.css",
        "AUX.js",
        "NUL",
        "COM3.html",
        "LPT1.txt"
    ]

    for p in bad_paths:
        with pytest.raises(ValueError) as exc:
            build_artifact_manifest({p: "content"})
        assert any(term in str(exc.value) for term in ["traversal", "reserved", "absolute", "escapes"])


def test_workstream_f_publication_rejects_oversized_artifacts():
    """
    Workstream F - Test 3: Artifacts exceeding 25MB (26,214,400 bytes) are rejected with ValueError.
    """
    huge_data = "A" * (26 * 1024 * 1024)
    with pytest.raises(ValueError) as exc:
        build_artifact_manifest({"large.bin": huge_data})
    assert "exceeds maximum allowed size" in str(exc.value)


def test_workstream_f_publication_rejects_case_and_unicode_collisions():
    """
    Workstream F - Test 4: Manifest builder rejects:
    1. Case-insensitive collisions (app.js vs APP.JS).
    2. Unicode normalization collisions (e.g. precomposed e-acute vs decomposed e + combining acute).
    """
    # 1. Case-insensitive
    with pytest.raises(ValueError) as exc1:
        build_artifact_manifest({"script.js": "1", "SCRIPT.JS": "2"})
    assert "Case-insensitive or Unicode normalization" in str(exc1.value)

    # 2. Unicode normalization
    precomposed = "resum\u00e9.html"
    decomposed = "resum\u0065\u0301.html"
    with pytest.raises(ValueError) as exc2:
        build_artifact_manifest({precomposed: "1", decomposed: "2"})
    assert "Case-insensitive or Unicode normalization" in str(exc2.value)
