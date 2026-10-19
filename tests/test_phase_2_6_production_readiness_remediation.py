"""
Phase 2.6 Production Readiness Remediation Test Suite
Validates:
- Phase 2.6-A1: Provider request ID capture and persistence (Gemini, OpenRouter, DB).
- Phase 2.6-A2: Crash-safe, durable, idempotent financial reconciliation (SQLite outbox,
                budget.json projection, crash failpoints, startup recovery, concurrency).
- Phase 2.6-B1: Safe cleanup of abandoned .restoring_* files without deleting active/user work.
- Phase 2.6-B2: Strict run_id validation boundary.
- Phase 2.6-C:  Recovery authentication, authorization, force=True elevated privilege,
                and durable SQLite audit logging.
"""

import os
import sys
import time
import json
import uuid
import hashlib
import threading
from unittest.mock import patch, MagicMock
import pytest

import db_persistence as db_store
from app import (
    app,
    set_last_agent_execution,
    get_last_agent_execution,
    call_agent_gemini,
    call_agent,
    is_valid_run_id,
    authenticate_operator
)


@pytest.fixture(autouse=True)
def isolate_test_budget(tmp_path, monkeypatch):
    """Ensure all tests operate against an isolated dummy budget file by default."""
    temp_budget = str(tmp_path / "isolated_p26_budget.json")
    with open(temp_budget, "w", encoding="utf-8") as f:
        json.dump({"tokens_used": 1000, "estimated_cost": 0.001, "budget_limit": 50.0, "transactions": []}, f)
    monkeypatch.setenv("BUDGET_FILE", temp_budget)
    monkeypatch.setenv("BUDGET_FILE_PATH", temp_budget)
    monkeypatch.setattr("app.BUDGET_FILE", temp_budget, raising=False)


# =========================================================================
# Phase 2.6-A1 Tests: Provider Request ID Capture & Persistence
# =========================================================================

def test_p26_a1_gemini_request_id_capture(monkeypatch):
    """Verify Gemini adapter extracts x-goog-request-id and propagates to execution context."""
    fake_resp = MagicMock()
    fake_resp.headers = {"x-goog-request-id": "req_goog_gemini_777"}
    fake_resp.json.return_value = {
        "candidates": [{
            "content": {"parts": [{"text": "مرحباً بالعالم"}]}
        }],
        "usageMetadata": {"totalTokenCount": 350}
    }

    with patch("requests.post", return_value=fake_resp):
        reply, tokens, ok = call_agent_gemini(
            system_prompt="Test sys",
            full_prompt="Test task",
            api_key="test_api_key_123",
            role_name="مبرمج",
            preferred_model="gemini-3.5-flash-lite"
        )
        assert ok is True
        assert tokens == 350
        meta = get_last_agent_execution()
        assert meta is not None
        assert meta["tokens_used"] == 350
        assert meta["token_source"] == "provider_reported"
        assert meta["provider"] == "gemini"
        assert meta["provider_request_id"] == "req_goog_gemini_777"


def test_p26_a1_openrouter_request_id_capture(monkeypatch):
    """Verify OpenRouter adapter extracts response body id and propagates to execution context."""
    fake_resp = MagicMock()
    fake_resp.headers = {"x-request-id": "hdr_or_999"}
    fake_resp.json.return_value = {
        "id": "gen_openrouter_alpha_888",
        "choices": [{
            "message": {"content": "كود بايثون متقدم"}
        }],
        "usage": {"total_tokens": 420}
    }

    monkeypatch.setattr("app.load_settings", lambda: {"provider": "openrouter", "openrouter_model": "test-model"})
    monkeypatch.setattr("app.get_api_key", lambda p: "test_openrouter_key")

    with patch("requests.post", return_value=fake_resp):
        reply = call_agent(
            agent_file="developer.md",
            role_name="مبرمج",
            short_name="DEV",
            color="#2ecc71",
            prompt="اكتب كود",
            return_metadata=False
        )
        assert "كود بايثون" in reply
        meta = get_last_agent_execution()
        assert meta is not None
        assert meta["tokens_used"] == 420
        assert meta["token_source"] == "provider_reported"
        assert meta["provider"] == "openrouter"
        assert meta["provider_request_id"] == "gen_openrouter_alpha_888"


def test_p26_a1_missing_request_id_not_fabricated():
    """Verify responses lacking request IDs default to None and are never fabricated."""
    set_last_agent_execution(tokens_used=100, token_source="estimated", provider="test", provider_request_id=None)
    meta = get_last_agent_execution()
    assert meta["provider_request_id"] is None


def test_p26_a1_db_persistence_and_retrieval(tmp_path):
    """Verify provider_request_id is durably stored in agent_runs and retrievable."""
    test_db = str(tmp_path / "req_id_test.db")
    db_store.init_db(test_db)

    sid = "sess_p26_req_1"
    db_store.create_session(session_id=sid, project_name="Req ID Test", task_description="Test", db_path=test_db)
    tid = db_store.create_task(session_id=sid, title="Task 1", division_id="engineering", assigned_agent_id="dev", stage="dev", status="pending", db_path=test_db)

    ok, run_id = db_store.complete_task_with_agent_run(
        task_id=tid,
        session_id=sid,
        agent_id="dev",
        agent_name="Developer",
        division_id="engineering",
        prompt_file="dev.md",
        output_text="Code completed",
        tokens_used=500,
        token_source="provider_reported",
        provider_request_id="req_durable_gemini_001",
        db_path=test_db
    )
    assert ok is True

    runs = db_store.list_agent_runs(session_id=sid, db_path=test_db)
    assert len(runs) == 1
    assert runs[0]["run_id"] == run_id
    assert runs[0]["provider_request_id"] == "req_durable_gemini_001"
    assert runs[0]["token_source"] == "provider_reported"


# =========================================================================
# Phase 2.6-A2 Tests: Durable Idempotent Financial Reconciliation
# =========================================================================

def test_p26_a2_normal_reconciliation_synchronizes_sqlite_and_budget(tmp_path):
    """Authoritative reconciliation updates SQLite and projects delta into budget.json."""
    test_db = str(tmp_path / "fin_norm.db")
    db_store.init_db(test_db)
    test_budget = str(tmp_path / "budget_norm.json")

    sid = "sess_fin_norm"
    db_store.create_session(session_id=sid, project_name="Financial Sync", task_description="Test", db_path=test_db)
    tid = db_store.create_task(session_id=sid, title="Task 1", division_id="engineering", assigned_agent_id="dev", stage="dev", status="completed", db_path=test_db)

    run_id = db_store.record_agent_run(
        task_id=tid,
        session_id=sid,
        agent_id="dev",
        agent_name="Dev",
        division_id="engineering",
        prompt_file="dev.md",
        status="completed",
        tokens_used=1000, # Initial estimate
        token_source="estimated",
        db_path=test_db
    )

    # Initial budget has 1000 tokens
    initial_budget = {
        "tokens_used": 1000,
        "estimated_cost": 0.0002,
        "budget_limit": 50.0,
        "transactions": []
    }
    with open(test_budget, "w", encoding="utf-8") as bf:
        json.dump(initial_budget, bf)

    # Provider confirms actual usage was 1500 tokens (+500 delta)
    def lookup(rid, req_id):
        return {"found": True, "tokens": 1500, "provider_request_id": "req_conf_1500"}

    res = db_store.reconcile_provider_run_usage(
        run_id=run_id,
        provider_lookup_fn=lookup,
        db_path=test_db,
        budget_path=test_budget
    )
    assert res["success"] is True
    assert res["tokens_used"] == 1500
    assert res["token_source"] == "provider_confirmed"
    assert res["reconciliation_status"] == "completed"
    assert res["delta_tokens"] == 500

    # Verify SQLite state
    runs = db_store.list_agent_runs(sid, db_path=test_db)
    assert runs[0]["tokens_used"] == 1500
    assert runs[0]["token_source"] == "provider_confirmed"
    assert runs[0]["reconciliation_status"] == "completed"

    # Verify budget.json state
    with open(test_budget, "r", encoding="utf-8") as bf:
        b_data = json.load(bf)
    assert b_data["tokens_used"] == 1500
    assert len(b_data["transactions"]) == 1
    assert b_data["transactions"][0]["tokens"] == 500
    assert b_data["transactions"][0]["ref_run_id"] == run_id


def test_p26_a2_crash_after_db_commit_resumes_cleanly_on_startup(tmp_path):
    """
    Simulate crash after SQLite commit (status='applied_db') before budget.json update.
    Verify startup recovery drains the pending outbox without charging twice.
    """
    test_db = str(tmp_path / "crash_after_db.db")
    db_store.init_db(test_db)
    test_budget = str(tmp_path / "budget_crash.json")

    sid = "sess_crash_1"
    db_store.create_session(session_id=sid, project_name="Crash Test", task_description="Test", db_path=test_db)
    tid = db_store.create_task(session_id=sid, title="Task 1", division_id="engineering", assigned_agent_id="dev", stage="dev", status="completed", db_path=test_db)

    run_id = db_store.record_agent_run(
        task_id=tid,
        session_id=sid,
        agent_id="dev",
        agent_name="Dev",
        division_id="engineering",
        prompt_file="dev.md",
        status="completed",
        tokens_used=1000,
        token_source="estimated",
        db_path=test_db
    )

    with open(test_budget, "w", encoding="utf-8") as bf:
        json.dump({"tokens_used": 1000, "estimated_cost": 0.0002, "transactions": []}, bf)

    def lookup(rid, req_id):
        return {"found": True, "tokens": 1600}

    # Crash injection: fail right after SQLite commit
    with pytest.raises(IOError, match="Crash simulated after DB commit before budget update"):
        db_store.reconcile_provider_run_usage(
            run_id=run_id,
            provider_lookup_fn=lookup,
            db_path=test_db,
            budget_path=test_budget,
            failpoint="after_db_before_budget"
        )

    # Budget still has un-projected state
    with open(test_budget, "r", encoding="utf-8") as bf:
        b_pre = json.load(bf)
    assert b_pre["tokens_used"] == 1000

    # Server restarts: startup recovery runs!
    db_store.reconcile_interrupted_sessions(db_path=test_db, budget_path=test_budget)

    # Budget is now projected!
    with open(test_budget, "r", encoding="utf-8") as bf:
        b_post = json.load(bf)
    assert b_post["tokens_used"] == 1600
    assert len(b_post["transactions"]) == 1
    assert b_post["transactions"][0]["tokens"] == 600

    # Re-running startup recovery does not double-count!
    db_store.reconcile_interrupted_sessions(db_path=test_db, budget_path=test_budget)
    with open(test_budget, "r", encoding="utf-8") as bf:
        b_re = json.load(bf)
    assert b_re["tokens_used"] == 1600
    assert len(b_re["transactions"]) == 1


def test_p26_a2_reconciliation_retry_idempotency(tmp_path):
    """Calling reconcile_provider_run_usage multiple times produces identical state."""
    test_db = str(tmp_path / "idemp.db")
    db_store.init_db(test_db)
    test_budget = str(tmp_path / "budget_idemp.json")

    sid = "sess_idemp"
    db_store.create_session(session_id=sid, project_name="Idempotency", task_description="Test", db_path=test_db)
    tid = db_store.create_task(session_id=sid, title="Task 1", division_id="engineering", assigned_agent_id="dev", stage="dev", status="completed", db_path=test_db)

    run_id = db_store.record_agent_run(
        task_id=tid,
        session_id=sid,
        agent_id="dev",
        agent_name="Dev",
        division_id="engineering",
        prompt_file="dev.md",
        status="completed",
        tokens_used=800,
        token_source="estimated",
        db_path=test_db
    )

    with open(test_budget, "w", encoding="utf-8") as bf:
        json.dump({"tokens_used": 800, "estimated_cost": 0.0001, "transactions": []}, bf)

    def lookup(rid, req_id):
        return {"found": True, "tokens": 1200}

    # First reconciliation
    r1 = db_store.reconcile_provider_run_usage(run_id, lookup, db_path=test_db, budget_path=test_budget)
    assert r1["success"] is True
    assert r1["tokens_used"] == 1200
    assert r1["delta_tokens"] == 400

    # Second reconciliation (retry)
    r2 = db_store.reconcile_provider_run_usage(run_id, lookup, db_path=test_db, budget_path=test_budget)
    assert r2["success"] is True
    assert r2.get("already_reconciled") is True
    assert r2["tokens_used"] == 1200

    # Third reconciliation
    r3 = db_store.reconcile_provider_run_usage(run_id, lookup, db_path=test_db, budget_path=test_budget)
    assert r3["success"] is True
    assert r3.get("already_reconciled") is True

    # Check budget totals: exactly one delta of 400 applied
    with open(test_budget, "r", encoding="utf-8") as bf:
        b = json.load(bf)
    assert b["tokens_used"] == 1200
    assert len(b["transactions"]) == 1


def test_p26_a2_concurrency_protection(tmp_path):
    """Concurrent reconciliation calls for the same run do not double count."""
    test_db = str(tmp_path / "concurrent.db")
    db_store.init_db(test_db)
    test_budget = str(tmp_path / "budget_concurrent.json")

    sid = "sess_conc"
    db_store.create_session(session_id=sid, project_name="Concurrency", task_description="Test", db_path=test_db)
    tid = db_store.create_task(session_id=sid, title="Task 1", division_id="engineering", assigned_agent_id="dev", stage="dev", status="completed", db_path=test_db)

    run_id = db_store.record_agent_run(
        task_id=tid,
        session_id=sid,
        agent_id="dev",
        agent_name="Dev",
        division_id="engineering",
        prompt_file="dev.md",
        status="completed",
        tokens_used=500,
        token_source="estimated",
        db_path=test_db
    )

    with open(test_budget, "w", encoding="utf-8") as bf:
        json.dump({"tokens_used": 500, "estimated_cost": 0.0001, "transactions": []}, bf)

    def lookup(rid, req_id):
        time.sleep(0.05) # simulate network latency
        return {"found": True, "tokens": 900}

    results = []
    def worker():
        res = db_store.reconcile_provider_run_usage(run_id, lookup, db_path=test_db, budget_path=test_budget)
        results.append(res)

    t1 = threading.Thread(target=worker)
    t2 = threading.Thread(target=worker)
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert len(results) == 2
    assert all(r["success"] for r in results)

    with open(test_budget, "r", encoding="utf-8") as bf:
        b = json.load(bf)
    assert b["tokens_used"] == 900
    assert len(b["transactions"]) == 1


# =========================================================================
# Phase 2.6-B1 Tests: Safe Staging (.restoring_*) Cleanup
# =========================================================================

def test_p26_b1_abandoned_staging_cleaned_active_retained(tmp_path):
    """Verify abandoned staging files (>60s) are unlinked while active (<60s) are preserved."""
    app_dir = str(tmp_path / "app_staging")
    os.makedirs(app_dir, exist_ok=True)

    # Valid user file: MUST BE UNTOUCHED
    user_file = os.path.join(app_dir, "index.html")
    with open(user_file, "w") as f:
        f.write("<h1>Hello World</h1>")

    # Abandoned staging file (>60s old)
    abandoned_file = os.path.join(app_dir, "index.html.restoring_a1b2c3")
    with open(abandoned_file, "w") as f:
        f.write("old partial content")
    past_time = time.time() - 120.0
    os.utime(abandoned_file, (past_time, past_time))

    # Active staging file (fresh, 5s old)
    active_file = os.path.join(app_dir, "app.js.restoring_d4e5f6")
    with open(active_file, "w") as f:
        f.write("active write in progress")

    cleaned = db_store.cleanup_abandoned_restore_staging(app_dir, max_age_seconds=60.0)
    assert cleaned == 1

    assert os.path.exists(user_file), "User file was improperly deleted!"
    assert not os.path.exists(abandoned_file), "Abandoned staging file was not cleaned!"
    assert os.path.exists(active_file), "Active staging file was improperly deleted!"


def test_p26_b1_symlink_and_traversal_safety(tmp_path):
    """Verify symlinks pointing outside trusted app_dir are ignored and never followed."""
    app_dir = str(tmp_path / "app_symlink_test")
    external_dir = str(tmp_path / "external_user_dir")
    os.makedirs(app_dir, exist_ok=True)
    os.makedirs(external_dir, exist_ok=True)

    target_outside = os.path.join(external_dir, "important.json")
    with open(target_outside, "w") as f:
        f.write('{"secret": "data"}')

    symlink_path = os.path.join(app_dir, "external.restoring_001122")
    try:
        os.symlink(target_outside, symlink_path)
    except (OSError, NotImplementedError):
        pytest.skip("Symlinks not supported in this environment")

    past_time = time.time() - 200.0
    try:
        os.utime(symlink_path, (past_time, past_time), follow_symlinks=False)
    except Exception:
        pass

    cleaned = db_store.cleanup_abandoned_restore_staging(app_dir, max_age_seconds=60.0)
    assert cleaned == 0
    assert os.path.exists(target_outside), "Target outside directory was touched!"


# =========================================================================
# Phase 2.6-B2 Tests: Run ID Validation
# =========================================================================

def test_p26_b2_run_id_validation_format():
    """Verify regex boundary strictly matches valid generated and rejects malicious IDs."""
    # Valid IDs
    assert is_valid_run_id("run_a1b2c3d4e5f6") is True
    assert is_valid_run_id("run_test_001") is True
    assert is_valid_run_id("run-custom-id") is True
    assert is_valid_run_id("123456789") is True

    # Invalid IDs
    assert is_valid_run_id("") is False
    assert is_valid_run_id(None) is False
    assert is_valid_run_id(12345) is False
    assert is_valid_run_id("a" * 65) is False # > 64 chars
    assert is_valid_run_id("../secret") is False
    assert is_valid_run_id("run/../../etc/passwd") is False
    assert is_valid_run_id("run\\windows\\path") is False
    assert is_valid_run_id("run_1\x00null") is False
    assert is_valid_run_id("run_1' OR '1'='1") is False
    assert is_valid_run_id("run_<script>") is False


# =========================================================================
# Phase 2.6-C Tests: Authentication, Authorization & Force Override Safety
# =========================================================================

def test_p26_c_unauthenticated_requests_rejected_with_401(monkeypatch, tmp_path):
    """Calling recovery endpoints without credentials returns 401 Unauthorized."""
    test_db = str(tmp_path / "auth_test.db")
    db_store.init_db(test_db)
    monkeypatch.setattr("db_persistence.DEFAULT_DB_FILENAME", os.path.basename(test_db))
    monkeypatch.setenv("DATABASE_PATH", test_db)
    monkeypatch.setenv("OPERATOR_AUTH_TOKEN", "valid_operator_secret")
    monkeypatch.setenv("ADMIN_AUTH_TOKEN", "valid_admin_secret")

    client = app.test_client()

    # 1. Unauthenticated apply endpoint
    res_apply = client.post(
        "/api/sessions/sess_auth_test/reconcile_rollback/apply",
        headers={"Origin": "http://127.0.0.1:8080", "Content-Type": "application/json"},
        json={"force": False}
    )
    assert res_apply.status_code == 401
    assert "Missing operator authentication" in res_apply.get_json()["error"]

    # 2. Unauthenticated reconcile_usage endpoint
    res_usage = client.post(
        "/api/runs/run_test_auth/reconcile_usage",
        headers={"Origin": "http://127.0.0.1:8080", "Content-Type": "application/json"},
        json={"provider_tokens": 100}
    )
    assert res_usage.status_code == 401
    assert "Missing operator authentication" in res_usage.get_json()["error"]


def test_p26_c_operator_cannot_force_without_admin_privilege(monkeypatch, tmp_path):
    """Normal operator token cannot execute force=True; returns 403 Forbidden."""
    test_db = str(tmp_path / "auth_force_test.db")
    db_store.init_db(test_db)
    monkeypatch.setattr("db_persistence.DEFAULT_DB_FILENAME", os.path.basename(test_db))
    monkeypatch.setenv("DATABASE_PATH", test_db)
    monkeypatch.setenv("OPERATOR_AUTH_TOKEN", "normal_op_token_123")
    monkeypatch.setenv("ADMIN_AUTH_TOKEN", "elevated_admin_token_456")

    client = app.test_client()

    # Operator calling with force=True MUST be 403 Forbidden
    res = client.post(
        "/api/sessions/sess_force_test/reconcile_rollback/apply",
        headers={
            "Origin": "http://127.0.0.1:8080",
            "Content-Type": "application/json",
            "X-Operator-Token": "normal_op_token_123"
        },
        json={"force": True}
    )
    assert res.status_code == 403
    assert "Elevated administrator privileges required" in res.get_json()["error"]


def test_p26_c_admin_authorized_for_force_operation(monkeypatch, tmp_path):
    """Admin token successfully executes force=True recovery with full audit log."""
    test_db = str(tmp_path / "auth_admin_test.db")
    db_store.init_db(test_db)
    monkeypatch.setattr("db_persistence.DEFAULT_DB_FILENAME", os.path.basename(test_db))
    monkeypatch.setenv("DATABASE_PATH", test_db)
    monkeypatch.setenv("OPERATOR_AUTH_TOKEN", "normal_op_token_123")
    monkeypatch.setenv("ADMIN_AUTH_TOKEN", "elevated_admin_token_456")

    app_dir = str(tmp_path / "app_admin_rec")
    os.makedirs(app_dir, exist_ok=True)
    target = os.path.join(app_dir, "index.html")
    backup = target + ".bak"
    with open(target, "wb") as f:
        f.write(b"USER_EDITS")
    with open(backup, "wb") as f:
        f.write(b"ORIGINAL_BACKUP")

    sid = "sess_admin_ok"
    db_store.create_session(session_id=sid, project_name="Admin Force", task_description="Test", db_path=test_db)
    db_store.record_incomplete_rollback(
        session_id=sid,
        delivery_id="deliv_1",
        app_dir=app_dir,
        target_path=target,
        backup_path=backup,
        original_sha256=hashlib.sha256(b"ORIGINAL_BACKUP").hexdigest(),
        candidate_sha256=hashlib.sha256(b"CANDIDATE").hexdigest(),
        db_path=test_db
    )

    client = app.test_client()

    # Admin calling with force=True succeeds
    res = client.post(
        f"/api/sessions/{sid}/reconcile_rollback/apply",
        headers={
            "Origin": "http://127.0.0.1:8080",
            "Content-Type": "application/json",
            "X-Admin-Token": "elevated_admin_token_456"
        },
        json={"force": True}
    )
    assert res.status_code == 200
    assert res.get_json()["success"] is True

    # Verify durable audit record in SQLite
    with db_store.get_db_connection(test_db) as conn:
        audits = conn.execute("SELECT * FROM recovery_audit_log WHERE target_id = ?;", (sid,)).fetchall()
    assert len(audits) >= 1
    latest_audit = audits[-1]
    assert latest_audit["principal"] == "admin"
    assert latest_audit["authorized"] == 1
    assert latest_audit["force_requested"] == 1
    assert latest_audit["outcome"] == "success"
