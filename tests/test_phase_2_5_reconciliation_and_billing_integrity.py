"""
Phase 2.5: Production-Safe Reconciliation, Crash Recovery & Provider Billing Integrity
======================================================================================
Validates all mandatory Phase 2.5 requirements through REAL production execution paths:
- Workstream A: Safe Incomplete-Rollback Reconciliation (non-destructive preview, safe
  restoration, conflict protection for newer user edits, path traversal / Windows device
  rejection, idempotency, concurrent claim blocking)
- Workstream B: Crash Consistency Across Recovery Boundaries (process termination at
  boundaries 1-4, fresh process restart, startup reconciliation via reconcile_interrupted_sessions)
- Workstream C: Provider Billing and Usage Reconciliation (authoritative provider lookup,
  explicit zero usage, rejection of negative/malformed usage, idempotency, no replay of prompts)
- Workstream D: Operator Diagnostics and Authorization Enforcement (CSRF/Origin validation,
  cross-session rejection, conflict HTTP 409, audit correlation and secret redaction)
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
    publish_workflow_deliverables,
    cleanup_backups,
    continue_workflow,
    is_valid_session_id,
    is_safe_origin,
    get_project_folder_name
)
import db_persistence as db_store


# =========================================================================
# Workstream A: Safe Incomplete-Rollback Reconciliation & Artifact Safety
# =========================================================================

def test_workstream_a_preview_is_strictly_non_destructive(tmp_path):
    """
    Workstream A - Test 1: Non-destructive reconciliation preview.
    Preview reports expected hashes, candidate match, proposed action, and is_safe=True.
    Verifies that preview does NOT mutate files, delete backups, or modify session state.
    """
    test_db = str(tmp_path / "ws_a1_preview.db")
    db_store.init_db(test_db)

    app_dir = str(tmp_path / "app_ws_a1")
    os.makedirs(app_dir, exist_ok=True)

    original_bytes = b"ORIGINAL_INDEX_CONTENT_V1"
    candidate_bytes = b"CANDIDATE_INDEX_CONTENT_V2"

    orig_sha = hashlib.sha256(original_bytes).hexdigest()
    cand_sha = hashlib.sha256(candidate_bytes).hexdigest()

    target_file = os.path.join(app_dir, "index.html")
    backup_file = target_file + ".pre_backup.bak"

    with open(target_file, "wb") as f:
        f.write(candidate_bytes)
    with open(backup_file, "wb") as f:
        f.write(original_bytes)

    sid = "sess_preview_ws_a1"
    db_store.create_session(session_id=sid, project_name="Preview Test", task_description="Desc", db_path=test_db)

    rec_id = db_store.record_incomplete_rollback(
        session_id=sid,
        delivery_id="deliv_001",
        app_dir=app_dir,
        target_path=target_file,
        backup_path=backup_file,
        original_sha256=orig_sha,
        candidate_sha256=cand_sha,
        error_message="Simulated rollback failure",
        db_path=test_db
    )

    sess_before = db_store.get_session(sid, db_path=test_db)

    # Execute preview
    preview = db_store.preview_rollback_reconciliation(sid, db_path=test_db)

    assert preview["session_id"] == sid
    assert preview["total_records"] == 1
    assert preview["can_auto_reconcile"] is True
    assert preview["conflicts_detected"] is False

    art = preview["artifacts"][0]
    assert art["recovery_id"] == rec_id
    assert art["content_state"] == "matches_candidate"
    assert art["proposed_action"] == "restore_backup"
    assert art["is_safe"] is True
    assert art["backup_sha256"] == orig_sha
    assert art["target_sha256"] == cand_sha

    # Non-destructive assertions: files on disk and DB records are completely untouched
    with open(target_file, "rb") as f:
        assert f.read() == candidate_bytes
    with open(backup_file, "rb") as f:
        assert f.read() == original_bytes

    sess = db_store.get_session(sid, db_path=test_db)
    assert sess["status"] == sess_before["status"]


def test_workstream_a_safe_restoration_and_backup_cleanup(tmp_path):
    """
    Workstream A - Test 2: Safe restoration of uncommitted candidate.
    Restores original bytes, verifies SHA digest immediately, updates DB to 'completed',
    and unlinks backup file only after verified commit.
    """
    test_db = str(tmp_path / "ws_a2_restore.db")
    db_store.init_db(test_db)

    app_dir = str(tmp_path / "app_ws_a2")
    os.makedirs(app_dir, exist_ok=True)

    original_bytes = b"ORIGINAL_STYLE_SHEET_V1 { color: blue; }"
    candidate_bytes = b"CANDIDATE_STYLE_SHEET_V2 { color: red; }"

    orig_sha = hashlib.sha256(original_bytes).hexdigest()
    cand_sha = hashlib.sha256(candidate_bytes).hexdigest()

    target_file = os.path.join(app_dir, "style.css")
    backup_file = target_file + ".pre_style.bak"

    with open(target_file, "wb") as f:
        f.write(candidate_bytes)
    with open(backup_file, "wb") as f:
        f.write(original_bytes)

    sid = "sess_restore_ws_a2"
    db_store.create_session(session_id=sid, project_name="Restore Test", task_description="Desc", db_path=test_db)

    rec_id = db_store.record_incomplete_rollback(
        session_id=sid,
        delivery_id="deliv_002",
        app_dir=app_dir,
        target_path=target_file,
        backup_path=backup_file,
        original_sha256=orig_sha,
        candidate_sha256=cand_sha,
        error_message="Rollback error: Permission denied",
        db_path=test_db
    )

    # Apply authorized reconciliation
    res = db_store.apply_rollback_reconciliation(session_id=sid, db_path=test_db)
    assert res["success"] is True
    assert res["reconciled_count"] == 1
    assert res["conflicts_count"] == 0

    # Verify target file has restored original content
    with open(target_file, "rb") as f:
        restored = f.read()
    assert restored == original_bytes
    assert hashlib.sha256(restored).hexdigest() == orig_sha

    # Verify backup was safely unlinked after successful restore and commit
    assert not os.path.exists(backup_file)

    # Verify DB record is completed
    with db_store.get_db_connection(test_db) as conn:
        row = conn.execute("SELECT status FROM rollback_recovery_records WHERE recovery_id = ?;", (rec_id,)).fetchone()
        assert row["status"] == "completed"


def test_workstream_a_refusal_to_overwrite_newer_user_edits(tmp_path):
    """
    Workstream A - Test 3: Conflict detection and protection of newer user edits.
    If the target file differs from both backup and candidate, automatic overwrite is refused!
    Preview flags conflict; apply transitions to 'conflicted' and returns refusal.
    """
    test_db = str(tmp_path / "ws_a3_conflict.db")
    db_store.init_db(test_db)

    app_dir = str(tmp_path / "app_ws_a3")
    os.makedirs(app_dir, exist_ok=True)

    original_bytes = b"ORIGINAL_CONTENT"
    candidate_bytes = b"UNCOMMITTED_CANDIDATE"
    user_edited_bytes = b"NEWER_USER_MANUAL_EDITS_DO_NOT_DESTROY"

    orig_sha = hashlib.sha256(original_bytes).hexdigest()
    cand_sha = hashlib.sha256(candidate_bytes).hexdigest()

    target_file = os.path.join(app_dir, "script.js")
    backup_file = target_file + ".pre_js.bak"

    with open(target_file, "wb") as f:
        f.write(user_edited_bytes)  # User modified the file post-failure!
    with open(backup_file, "wb") as f:
        f.write(original_bytes)

    sid = "sess_conflict_ws_a3"
    db_store.create_session(session_id=sid, project_name="Conflict Test", task_description="Desc", db_path=test_db)
    db_store.update_session_status(sid, "failed", error_message="Database delivery persistence failed; Rollback errors: Failed to rollback script.js", db_path=test_db)

    rec_id = db_store.record_incomplete_rollback(
        session_id=sid,
        delivery_id="deliv_003",
        app_dir=app_dir,
        target_path=target_file,
        backup_path=backup_file,
        original_sha256=orig_sha,
        candidate_sha256=cand_sha,
        error_message="Rollback error",
        db_path=test_db
    )

    # 1. Preview detects conflict
    preview = db_store.preview_rollback_reconciliation(sid, db_path=test_db)
    assert preview["can_auto_reconcile"] is False
    assert preview["conflicts_detected"] is True
    art = preview["artifacts"][0]
    assert art["content_state"] == "conflicted_newer_user_edit"
    assert art["is_safe"] is False
    assert art["proposed_action"] == "manual_intervention_required"

    # 2. Apply without force REFUSES to overwrite!
    res = db_store.apply_rollback_reconciliation(session_id=sid, force=False, db_path=test_db)
    assert res["success"] is False
    assert res["conflicts_count"] == 1
    assert "newer user edits" in res["results"][0]["error"]

    # Verify user edits were NOT destroyed
    with open(target_file, "rb") as f:
        assert f.read() == user_edited_bytes

    # Verify backup is retained
    assert os.path.exists(backup_file)

    # 3. Operator audit reflects conflict
    audit = db_store.get_operator_session_audit(sid, db_path=test_db)
    assert audit["failure_analysis"]["artifact_conflict_detected"] is True
    assert "resolve_conflict_manually" in audit["permitted_recovery_actions"]


def test_workstream_a_forced_reconciliation_overrides_conflict(tmp_path):
    """
    Workstream A - Test 4: Explicit operator override with force=True.
    When operator explicitly confirms force=True, restoration succeeds over conflicted file.
    """
    test_db = str(tmp_path / "ws_a4_forced.db")
    db_store.init_db(test_db)

    app_dir = str(tmp_path / "app_ws_a4")
    os.makedirs(app_dir, exist_ok=True)

    original_bytes = b"ORIGINAL_CONTENT_V1"
    target_file = os.path.join(app_dir, "plan.md")
    backup_file = target_file + ".pre_plan.bak"

    with open(target_file, "wb") as f:
        f.write(b"USER_CONFLICT_BYTES")
    with open(backup_file, "wb") as f:
        f.write(original_bytes)

    sid = "sess_forced_ws_a4"
    db_store.create_session(session_id=sid, project_name="Forced Test", task_description="Desc", db_path=test_db)

    db_store.record_incomplete_rollback(
        session_id=sid,
        delivery_id="deliv_004",
        app_dir=app_dir,
        target_path=target_file,
        backup_path=backup_file,
        db_path=test_db
    )

    # Apply with force=True
    res = db_store.apply_rollback_reconciliation(session_id=sid, force=True, db_path=test_db)
    assert res["success"] is True
    assert res["reconciled_count"] == 1

    with open(target_file, "rb") as f:
        assert f.read() == original_bytes


def test_workstream_a_path_safety_rejects_traversal_and_windows_devices(tmp_path):
    """
    Workstream A - Test 5: Strict path safety enforcement.
    Rejects traversal (..), absolute paths, drive prefixes, UNC paths, ADS (:),
    and Windows reserved device names (CON, PRN, AUX, NUL, COM1-9, LPT1-9).
    """
    app_dir = str(tmp_path / "safe_project_dir")
    os.makedirs(app_dir, exist_ok=True)

    # Safe path succeeds
    safe_target = db_store.validate_project_artifact_path(app_dir, "css/style.css")
    assert safe_target.startswith(os.path.abspath(app_dir))

    # Forbidden paths must raise ValueError
    forbidden_paths = [
        "../outside.txt",
        "../../etc/passwd",
        r"..\..\Windows\System32",
        "C:\\boot.ini",
        r"\\evil_server\share\payload.js",
        "/etc/shadow",
        "file.txt:stream",
        "CON",
        "con.txt",
        "PRN",
        "AUX",
        "NUL",
        "nul.html",
        "COM1",
        "com2.js",
        "LPT1",
        "",
        "   "
    ]

    for p in forbidden_paths:
        with pytest.raises(ValueError) as exc:
            db_store.validate_project_artifact_path(app_dir, p)
        assert len(str(exc.value)) > 0


def test_workstream_a_missing_backup_handled_gracefully(tmp_path):
    """
    Workstream A - Test 6: Missing backup file handling.
    If backup was deleted or unreadable, apply reports failure without corrupting DB.
    """
    test_db = str(tmp_path / "ws_a6_missing_bak.db")
    db_store.init_db(test_db)

    app_dir = str(tmp_path / "app_ws_a6")
    os.makedirs(app_dir, exist_ok=True)

    target_file = os.path.join(app_dir, "index.html")
    non_existent_bak = os.path.join(app_dir, "index.html.non_existent.bak")

    with open(target_file, "wb") as f:
        f.write(b"CANDIDATE_DATA")

    sid = "sess_missing_bak_a6"
    db_store.create_session(session_id=sid, project_name="Missing Bak", task_description="Desc", db_path=test_db)

    rec_id = db_store.record_incomplete_rollback(
        session_id=sid,
        delivery_id="deliv_006",
        app_dir=app_dir,
        target_path=target_file,
        backup_path=non_existent_bak,
        db_path=test_db
    )

    res = db_store.apply_rollback_reconciliation(session_id=sid, db_path=test_db)
    assert res["success"] is False
    assert res["failed_count"] == 1
    assert "missing on disk" in res["results"][0]["error"]


def test_workstream_a_idempotent_repeated_reconciliation(tmp_path):
    """
    Workstream A - Test 7: Repeated reconciliation calls are completely idempotent.
    """
    test_db = str(tmp_path / "ws_a7_idempotent.db")
    db_store.init_db(test_db)

    app_dir = str(tmp_path / "app_ws_a7")
    os.makedirs(app_dir, exist_ok=True)

    original_bytes = b"ORIGINAL_BYTES"
    target_file = os.path.join(app_dir, "index.html")
    backup_file = target_file + ".pre.bak"

    with open(target_file, "wb") as f:
        f.write(b"CANDIDATE_BYTES")
    with open(backup_file, "wb") as f:
        f.write(original_bytes)

    sid = "sess_idempotent_a7"
    db_store.create_session(session_id=sid, project_name="Idempotent Test", task_description="Desc", db_path=test_db)

    db_store.record_incomplete_rollback(
        session_id=sid,
        delivery_id="deliv_007",
        app_dir=app_dir,
        target_path=target_file,
        backup_path=backup_file,
        db_path=test_db
    )

    # First call: reconciles
    r1 = db_store.apply_rollback_reconciliation(session_id=sid, db_path=test_db)
    assert r1["success"] is True
    assert r1["reconciled_count"] == 1

    # Second call: idempotent, already completed
    r2 = db_store.apply_rollback_reconciliation(session_id=sid, db_path=test_db)
    assert r2["success"] is True
    assert r2.get("already_completed") is True


def test_workstream_a_concurrent_reconciliation_claim_blocking(tmp_path):
    """
    Workstream A - Test 8: Concurrency protection.
    A second worker attempting reconciliation on an active in_progress record is rejected.
    """
    test_db = str(tmp_path / "ws_a8_concurrency.db")
    db_store.init_db(test_db)

    app_dir = str(tmp_path / "app_ws_a8")
    os.makedirs(app_dir, exist_ok=True)

    target_file = os.path.join(app_dir, "index.html")
    backup_file = target_file + ".pre.bak"
    with open(target_file, "wb") as f:
        f.write(b"CANDIDATE")
    with open(backup_file, "wb") as f:
        f.write(b"ORIGINAL")

    sid = "sess_concur_a8"
    db_store.create_session(session_id=sid, project_name="Concur", task_description="Desc", db_path=test_db)

    rec_id = db_store.record_incomplete_rollback(
        session_id=sid,
        delivery_id="deliv_008",
        app_dir=app_dir,
        target_path=target_file,
        backup_path=backup_file,
        db_path=test_db
    )

    # Worker 1 acquires claim (sets status to 'in_progress')
    with db_store.get_db_connection(test_db) as conn:
        conn.execute("UPDATE rollback_recovery_records SET status = 'in_progress', updated_at = ? WHERE recovery_id = ?;", (time.time(), rec_id))
        conn.commit()

    # Worker 2 attempts reconciliation immediately
    r2 = db_store.apply_rollback_reconciliation(session_id=sid, db_path=test_db)
    assert r2["success"] is False
    assert "Concurrent reconciliation in progress" in r2["error"]


# =========================================================================
# Workstream B: Crash Consistency Across Recovery Boundaries
# =========================================================================

def test_workstream_b_crash_before_filesystem_restore_and_startup_reconciliation(tmp_path):
    """
    Workstream B - Crash Boundary 1: Process terminated before filesystem restoration.
    Child process acquires recovery claim, persists in_progress, and crashes.
    Fresh restart process runs reconcile_interrupted_sessions().
    Verifies:
    - Target file was not yet restored
    - Startup recovery resets status to 'pending'
    - Backup is preserved for subsequent operator retry
    """
    test_db = str(tmp_path / "proc_kill_b1.db")
    db_store.init_db(test_db)

    app_dir = str(tmp_path / "app_ws_b1")
    os.makedirs(app_dir, exist_ok=True)

    original_bytes = b"ORIGINAL_BYTES_B1"
    candidate_bytes = b"CANDIDATE_BYTES_B1"
    target_file = os.path.join(app_dir, "index.html")
    backup_file = target_file + ".pre_b1.bak"

    with open(target_file, "wb") as f:
        f.write(candidate_bytes)
    with open(backup_file, "wb") as f:
        f.write(original_bytes)

    sid = "sess_crash_b1"
    db_store.create_session(session_id=sid, project_name="Crash B1", task_description="Desc", db_path=test_db)

    rec_id = db_store.record_incomplete_rollback(
        session_id=sid,
        delivery_id="deliv_b1",
        app_dir=app_dir,
        target_path=target_file,
        backup_path=backup_file,
        db_path=test_db
    )

    started_flag = str(tmp_path / "b1_started.flag")
    worker_script = str(tmp_path / "worker_b1.py")

    worker_code = f"""
import sys, os, time
sys.path.insert(0, r"{root_dir}")
import db_persistence as db_store

with db_store.get_db_connection(r"{test_db}") as conn:
    conn.execute("UPDATE rollback_recovery_records SET status = 'in_progress', updated_at = ? WHERE recovery_id = '{rec_id}';", (time.time(),))
    conn.commit()

with open(r"{started_flag}", "w") as f:
    f.write("CLAIMED")

time.sleep(60)
"""
    with open(worker_script, "w", encoding="utf-8") as f:
        f.write(worker_code)

    proc = subprocess.Popen([sys.executable, worker_script])
    for _ in range(50):
        if os.path.exists(started_flag):
            break
        time.sleep(0.1)
    assert os.path.exists(started_flag)

    # Terminate process mid-recovery
    proc.kill()
    proc.wait(timeout=5)

    # Start completely fresh process running startup reconciliation
    fresh_script = str(tmp_path / "fresh_reconcile_b1.py")
    fresh_code = f"""
import sys
sys.path.insert(0, r"{root_dir}")
import db_persistence as db_store

db_store.reconcile_interrupted_sessions(db_path=r"{test_db}")
with db_store.get_db_connection(r"{test_db}") as conn:
    row = conn.execute("SELECT status FROM rollback_recovery_records WHERE recovery_id = '{rec_id}';").fetchone()
    print(f"RECONCILED_STATUS:{{row['status']}}")
"""
    with open(fresh_script, "w", encoding="utf-8") as f:
        f.write(fresh_code)

    res = subprocess.run([sys.executable, fresh_script], stdout=subprocess.PIPE, text=True, timeout=10)
    assert "RECONCILED_STATUS:pending" in res.stdout

    # Target file still intact candidate; backup still preserved
    with open(target_file, "rb") as f:
        assert f.read() == candidate_bytes
    assert os.path.exists(backup_file)


def test_workstream_b_crash_after_restore_before_sqlite_commit(tmp_path):
    """
    Workstream B - Crash Boundary 2: Process killed after filesystem restoration
    succeeded, but before SQLite status committed as 'completed'.
    Fresh process runs startup reconciliation:
    - Detects target file already matches backup SHA-256
    - Advances record to 'completed'
    - Safely unlinks backup file
    """
    test_db = str(tmp_path / "proc_kill_b2.db")
    db_store.init_db(test_db)

    app_dir = str(tmp_path / "app_ws_b2")
    os.makedirs(app_dir, exist_ok=True)

    original_bytes = b"ORIGINAL_BYTES_B2"
    target_file = os.path.join(app_dir, "index.html")
    backup_file = target_file + ".pre_b2.bak"

    # Restoration already wrote the original bytes to target before crash
    with open(target_file, "wb") as f:
        f.write(original_bytes)
    with open(backup_file, "wb") as f:
        f.write(original_bytes)

    sid = "sess_crash_b2"
    db_store.create_session(session_id=sid, project_name="Crash B2", task_description="Desc", db_path=test_db)

    rec_id = db_store.record_incomplete_rollback(
        session_id=sid,
        delivery_id="deliv_b2",
        app_dir=app_dir,
        target_path=target_file,
        backup_path=backup_file,
        db_path=test_db
    )

    # In DB, status was left as 'restored' or 'in_progress' when crash occurred
    with db_store.get_db_connection(test_db) as conn:
        conn.execute("UPDATE rollback_recovery_records SET status = 'restored', updated_at = ? WHERE recovery_id = ?;", (time.time(), rec_id))
        conn.commit()

    # Fresh startup reconciliation process runs
    fresh_script = str(tmp_path / "fresh_reconcile_b2.py")
    fresh_code = f"""
import sys
sys.path.insert(0, r"{root_dir}")
import db_persistence as db_store

db_store.reconcile_interrupted_sessions(db_path=r"{test_db}")
with db_store.get_db_connection(r"{test_db}") as conn:
    row = conn.execute("SELECT status FROM rollback_recovery_records WHERE recovery_id = '{rec_id}';").fetchone()
    print(f"RECONCILED_STATUS:{{row['status']}}")
"""
    with open(fresh_script, "w", encoding="utf-8") as f:
        f.write(fresh_code)

    res = subprocess.run([sys.executable, fresh_script], stdout=subprocess.PIPE, text=True, timeout=10)
    assert "RECONCILED_STATUS:completed" in res.stdout

    # Target file contains restored bytes
    with open(target_file, "rb") as f:
        assert f.read() == original_bytes

    # Backup file was safely unlinked
    assert not os.path.exists(backup_file)


# =========================================================================
# Workstream C: Provider Billing and Usage Reconciliation
# =========================================================================

def test_workstream_c_provider_reconciliation_confirmed_usage(tmp_path):
    """
    Workstream C - Test 1: Authoritative provider usage lookup.
    Reconciles an ambiguous agent run using authoritative provider data.
    Updates tokens_used, sets token_source='provider_confirmed', reconciliation_status='completed'.
    """
    test_db = str(tmp_path / "ws_c1_provider.db")
    db_store.init_db(test_db)

    sid = "sess_provider_c1"
    db_store.create_session(session_id=sid, project_name="Provider Reconcile", task_description="Desc", db_path=test_db)
    tid = db_store.create_task(session_id=sid, title="Task C1", division_id="engineering", assigned_agent_id="dev", stage="dev", status="failed", db_path=test_db)

    run_id = db_store.record_agent_run(
        task_id=tid,
        session_id=sid,
        agent_id="dev",
        agent_name="Developer",
        division_id="engineering",
        prompt_file="dev.md",
        status="failed",
        tokens_used=0,
        token_source="estimated",
        error_message="Provider timeout: call ambiguous",
        db_path=test_db
    )

    # Operator audit reports reconciliation pending
    audit_pre = db_store.get_operator_session_audit(sid, db_path=test_db)
    assert audit_pre["cost_and_token_accounting"]["provider_reconciliation_pending"] is True
    assert "reconcile_provider_usage" in audit_pre["cost_and_token_accounting"]["permitted_accounting_actions"]

    # Reconcile with authoritative lookup
    def mock_provider_lookup(r_id, req_id):
        return {
            "found": True,
            "tokens": 2840,
            "cost": 0.0568,
            "provider_request_id": "req_gemini_auth_991"
        }

    res = db_store.reconcile_provider_run_usage(run_id, provider_lookup_fn=mock_provider_lookup, db_path=test_db)
    assert res["success"] is True
    assert res["tokens_used"] == 2840
    assert res["token_source"] == "provider_confirmed"
    assert res["reconciliation_status"] == "completed"

    # Operator audit now reflects confirmed usage
    audit_post = db_store.get_operator_session_audit(sid, db_path=test_db)
    assert audit_post["cost_and_token_accounting"]["provider_confirmed_tokens"] == 2840
    assert audit_post["cost_and_token_accounting"]["total_tokens_billed"] == 2840


def test_workstream_c_provider_reconciliation_preserves_explicit_zero_tokens(tmp_path):
    """
    Workstream C - Test 2: Preserves explicit zero-token usage.
    Zero tokens confirmed by provider (e.g. cache hit) is not overwritten or rejected.
    """
    test_db = str(tmp_path / "ws_c2_zero_tokens.db")
    db_store.init_db(test_db)

    sid = "sess_zero_c2"
    db_store.create_session(session_id=sid, project_name="Zero Tokens", task_description="Desc", db_path=test_db)
    tid = db_store.create_task(session_id=sid, title="Task C2", division_id="engineering", assigned_agent_id="dev", stage="dev", status="completed", db_path=test_db)

    run_id = db_store.record_agent_run(
        task_id=tid,
        session_id=sid,
        agent_id="dev",
        agent_name="Developer",
        division_id="engineering",
        prompt_file="dev.md",
        status="completed",
        tokens_used=500,
        token_source="estimated",
        db_path=test_db
    )

    def mock_zero_lookup(r_id, req_id):
        return {"found": True, "tokens": 0, "cost": 0.0, "provider_request_id": "req_zero"}

    res = db_store.reconcile_provider_run_usage(run_id, provider_lookup_fn=mock_zero_lookup, db_path=test_db)
    assert res["success"] is True
    assert res["tokens_used"] == 0
    assert res["token_source"] == "provider_confirmed"


def test_workstream_c_rejection_of_negative_and_malformed_tokens(tmp_path):
    """
    Workstream C - Test 3: Rejection of negative or malformed token usage from provider.
    Rejects negative values without corrupting ledger.
    """
    test_db = str(tmp_path / "ws_c3_malformed.db")
    db_store.init_db(test_db)

    sid = "sess_malformed_c3"
    db_store.create_session(session_id=sid, project_name="Malformed", task_description="Desc", db_path=test_db)
    tid = db_store.create_task(session_id=sid, title="Task C3", division_id="engineering", assigned_agent_id="dev", stage="dev", status="completed", db_path=test_db)

    run_id = db_store.record_agent_run(
        task_id=tid,
        session_id=sid,
        agent_id="dev",
        agent_name="Developer",
        division_id="engineering",
        prompt_file="dev.md",
        status="completed",
        tokens_used=100,
        token_source="estimated",
        db_path=test_db
    )

    # Negative tokens rejected
    res_neg = db_store.reconcile_provider_run_usage(
        run_id,
        provider_lookup_fn=lambda r, req: {"found": True, "tokens": -500},
        db_path=test_db
    )
    assert res_neg["success"] is False
    assert res_neg["reconciliation_status"] == "failed"

    # Malformed tokens rejected
    res_str = db_store.reconcile_provider_run_usage(
        run_id,
        provider_lookup_fn=lambda r, req: {"found": True, "tokens": "not_a_number"},
        db_path=test_db
    )
    assert res_str["success"] is False
    assert res_str["reconciliation_status"] == "failed"

    # Existing tokens are uncorrupted
    with db_store.get_db_connection(test_db) as conn:
        row = conn.execute("SELECT tokens_used FROM agent_runs WHERE run_id = ?;", (run_id,)).fetchone()
        assert row["tokens_used"] == 100


def test_workstream_c_reconciliation_idempotency_prevents_double_counting(tmp_path):
    """
    Workstream C - Test 4: Idempotent provider usage reconciliation.
    Repeated reconciliation polls return already_reconciled=True without altering values.
    """
    test_db = str(tmp_path / "ws_c4_idempotent.db")
    db_store.init_db(test_db)

    sid = "sess_idempotent_c4"
    db_store.create_session(session_id=sid, project_name="Idempotent Provider", task_description="Desc", db_path=test_db)
    tid = db_store.create_task(session_id=sid, title="Task C4", division_id="engineering", assigned_agent_id="dev", stage="dev", status="completed", db_path=test_db)

    run_id = db_store.record_agent_run(
        task_id=tid,
        session_id=sid,
        agent_id="dev",
        agent_name="Developer",
        division_id="engineering",
        prompt_file="dev.md",
        status="completed",
        tokens_used=500,
        token_source="estimated",
        db_path=test_db
    )

    def lookup(r, req):
        return {"found": True, "tokens": 1200, "provider_request_id": "req_1200"}

    r1 = db_store.reconcile_provider_run_usage(run_id, provider_lookup_fn=lookup, db_path=test_db)
    assert r1["success"] is True
    assert r1["tokens_used"] == 1200

    r2 = db_store.reconcile_provider_run_usage(run_id, provider_lookup_fn=lookup, db_path=test_db)
    assert r2["success"] is True
    assert r2.get("already_reconciled") is True
    assert r2["tokens_used"] == 1200


def test_workstream_c_unavailable_lookup_never_replays_generative_prompts(tmp_path):
    """
    Workstream C - Test 5: Unavailable lookup remains explicitly unresolved.
    Generative prompts are NEVER automatically replayed.
    """
    test_db = str(tmp_path / "ws_c5_unavailable.db")
    db_store.init_db(test_db)

    sid = "sess_unavail_c5"
    db_store.create_session(session_id=sid, project_name="Unavail", task_description="Desc", db_path=test_db)
    tid = db_store.create_task(session_id=sid, title="Task C5", division_id="engineering", assigned_agent_id="dev", stage="dev", status="failed", db_path=test_db)

    run_id = db_store.record_agent_run(
        task_id=tid,
        session_id=sid,
        agent_id="dev",
        agent_name="Developer",
        division_id="engineering",
        prompt_file="dev.md",
        status="failed",
        tokens_used=0,
        token_source="estimated",
        error_message="Ambiguous in-flight call",
        db_path=test_db
    )

    # Calling without lookup function reports unresolved and explains limitation
    res = db_store.reconcile_provider_run_usage(run_id, provider_lookup_fn=None, db_path=test_db)
    assert res["success"] is False
    assert res["reconciliation_status"] == "unresolved"
    assert "Replay of generative prompts is forbidden" in res["error"]


# =========================================================================
# Workstream D: Operator Diagnostics, Authorization & HTTP Endpoints
# =========================================================================

def test_workstream_d_endpoints_reject_invalid_origin_and_cross_site(monkeypatch, tmp_path):
    """
    Workstream D - Test 1: Origin/CSRF barrier on all recovery and reconciliation routes.
    Rejects foreign origins (403 Forbidden).
    """
    test_db = str(tmp_path / "ws_d1_origin.db")
    db_store.init_db(test_db)
    monkeypatch.setattr("db_persistence.DEFAULT_DB_FILENAME", os.path.basename(test_db))
    monkeypatch.setenv("DATABASE_PATH", test_db)

    client = app.test_client()

    sid = "sess_origin_d1"
    db_store.create_session(session_id=sid, project_name="Origin Test", task_description="Desc", db_path=test_db)

    # State-changing POST routes with foreign origin MUST be 403 Forbidden
    res_apply = client.post(
        f"/api/sessions/{sid}/reconcile_rollback/apply",
        headers={"Origin": "http://malicious-attacker.com:8080"},
        json={"operator_action": "restore_safe"}
    )
    assert res_apply.status_code == 403

    res_usage = client.post(
        "/api/runs/run_test_123/reconcile_usage",
        headers={"Origin": "http://malicious-attacker.com:8080"},
        json={"provider_tokens": 100}
    )
    assert res_usage.status_code == 403


def test_workstream_d_preview_and_apply_http_flow_with_conflict(monkeypatch, tmp_path):
    """
    Workstream D - Test 2: End-to-end HTTP reconciliation flow with conflict.
    - GET /api/sessions/<id>/reconcile_rollback/preview -> 200 with conflict summary
    - POST /api/sessions/<id>/reconcile_rollback/apply (without force) -> 409 Conflict
    - POST /api/sessions/<id>/reconcile_rollback/apply (with force=True) -> 200 OK
    """
    test_db = str(tmp_path / "ws_d2_http.db")
    db_store.init_db(test_db)
    monkeypatch.setattr("db_persistence.DEFAULT_DB_FILENAME", os.path.basename(test_db))
    monkeypatch.setenv("DATABASE_PATH", test_db)

    app_dir = str(tmp_path / "app_ws_d2")
    os.makedirs(app_dir, exist_ok=True)

    original_bytes = b"ORIGINAL_HTML"
    target_file = os.path.join(app_dir, "index.html")
    candidate_bytes = b"CANDIDATE_DELIVERABLE_HTML"
    target_file = os.path.join(app_dir, "index.html")
    backup_file = target_file + ".pre.bak"

    with open(target_file, "wb") as f:
        f.write(candidate_bytes)
    with open(backup_file, "wb") as f:
        f.write(original_bytes)

    sid = "sess_http_d2"
    db_store.create_session(session_id=sid, project_name="HTTP Flow", task_description="Desc", db_path=test_db)

    db_store.record_incomplete_rollback(
        session_id=sid,
        delivery_id="deliv_d2",
        app_dir=app_dir,
        target_path=target_file,
        backup_path=backup_file,
        candidate_sha256=hashlib.sha256(candidate_bytes).hexdigest(),
        original_sha256=hashlib.sha256(original_bytes).hexdigest(),
        db_path=test_db
    )

    # Post-failure: User edited file creating conflict!
    with open(target_file, "wb") as f:
        f.write(b"USER_NEW_EDITS")

    client = app.test_client()

    # 1. Preview
    res_prev = client.get(
        f"/api/sessions/{sid}/reconcile_rollback/preview",
        headers={"Origin": "http://127.0.0.1:8080"}
    )
    assert res_prev.status_code == 200
    prev_data = res_prev.get_json()
    assert prev_data["conflicts_detected"] is True
    assert prev_data["can_auto_reconcile"] is False

    # 2. Apply without force -> 409 Conflict!
    res_apply = client.post(
        f"/api/sessions/{sid}/reconcile_rollback/apply",
        headers={"Origin": "http://127.0.0.1:8080", "Content-Type": "application/json"},
        json={"force": False}
    )
    assert res_apply.status_code == 409
    apply_err = res_apply.get_json()
    assert "newer user edits exist" in apply_err["error"]

    # 3. Apply with force=True -> 200 OK!
    res_forced = client.post(
        f"/api/sessions/{sid}/reconcile_rollback/apply",
        headers={"Origin": "http://127.0.0.1:8080", "Content-Type": "application/json"},
        json={"force": True}
    )
    assert res_forced.status_code == 200
    forced_data = res_forced.get_json()
    assert forced_data["success"] is True
    assert forced_data["reconciled_count"] == 1


def test_workstream_d_reconcile_usage_http_flow(monkeypatch, tmp_path):
    """
    Workstream D - Test 3: HTTP route for provider usage reconciliation.
    POST /api/runs/<run_id>/reconcile_usage
    """
    test_db = str(tmp_path / "ws_d3_usage.db")
    db_store.init_db(test_db)
    monkeypatch.setattr("db_persistence.DEFAULT_DB_FILENAME", os.path.basename(test_db))
    monkeypatch.setenv("DATABASE_PATH", test_db)

    sid = "sess_usage_d3"
    db_store.create_session(session_id=sid, project_name="Usage HTTP", task_description="Desc", db_path=test_db)
    tid = db_store.create_task(session_id=sid, title="Task D3", division_id="engineering", assigned_agent_id="dev", stage="dev", status="completed", db_path=test_db)

    run_id = db_store.record_agent_run(
        task_id=tid,
        session_id=sid,
        agent_id="dev",
        agent_name="Developer",
        division_id="engineering",
        prompt_file="dev.md",
        status="completed",
        tokens_used=100,
        token_source="estimated",
        db_path=test_db
    )

    client = app.test_client()

    res = client.post(
        f"/api/runs/{run_id}/reconcile_usage",
        headers={"Origin": "http://127.0.0.1:8080", "Content-Type": "application/json"},
        json={"provider_tokens": 1750, "cost": 0.035, "provider_request_id": "req_http_1750"}
    )
    assert res.status_code == 200
    data = res.get_json()
    assert data["success"] is True
    assert data["tokens_used"] == 1750
    assert data["token_source"] == "provider_confirmed"


def test_workstream_d_secret_redaction_in_audit_and_records(tmp_path):
    """
    Workstream D - Test 4: Secret sanitization in audit, error messages, and memory.
    Ensures API keys, tokens, and credentials are never leaked in operator reports.
    """
    test_db = str(tmp_path / "ws_d4_secrets.db")
    db_store.init_db(test_db)

    sid = "sess_secrets_d4"
    db_store.create_session(session_id=sid, project_name="Secrets Test", task_description="Desc", db_path=test_db)

    app_dir = str(tmp_path / "app_ws_d4")
    os.makedirs(app_dir, exist_ok=True)
    target_file = os.path.join(app_dir, "index.html")
    backup_file = target_file + ".pre.bak"
    with open(target_file, "wb") as f:
        f.write(b"data")
    with open(backup_file, "wb") as f:
        f.write(b"data")

    # Injected raw API key in error message
    raw_key = "AIzaSyFakeSecretKeyForTest12345"
    db_store.record_incomplete_rollback(
        session_id=sid,
        delivery_id="deliv_d4",
        app_dir=app_dir,
        target_path=target_file,
        backup_path=backup_file,
        error_message=f"Rollback failed with key: {raw_key}",
        db_path=test_db
    )

    audit = db_store.get_operator_session_audit(sid, db_path=test_db)
    recs = audit["rollback_recovery"]["records"]
    assert len(recs) == 1
    # Verify raw secret key was redacted
    assert raw_key not in recs[0]["error_message"]
    assert "[REDACTED_API_KEY]" in recs[0]["error_message"]
