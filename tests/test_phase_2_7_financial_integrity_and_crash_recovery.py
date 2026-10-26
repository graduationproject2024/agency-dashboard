"""
Phase 2.7 Financial Integrity, Crash Recovery & Production Readiness Test Suite

Validates:
- Priority Zero Problem A: Financial reconciliation integrity, authoritative provider query,
  rejection of client-supplied tokens/cost, explicit unresolved states, provider/model persistence,
  and dedicated admin-authorized manual adjustment workflow.
- Priority Zero Problem B: Durable dual-ledger outbox, cross-process/cross-thread budget locking,
  truncation-proof idempotency (>60 transactions), crash recovery failpoints, and corrupted JSON resilience.
- Security and audit: Operator vs Admin authorization, fail-closed audit logging, and audit verification.
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
    set_test_authoritative_provider_record,
    clear_test_authoritative_provider_records,
    query_authoritative_provider_usage
)


@pytest.fixture(autouse=True)
def isolate_test_budget(tmp_path, monkeypatch):
    """Ensure all tests operate against an isolated dummy budget file."""
    temp_budget = str(tmp_path / "isolated_p27_budget.json")
    with open(temp_budget, "w", encoding="utf-8") as f:
        json.dump({
            "tokens_used": 1000,
            "estimated_cost": 0.001,
            "budget_limit": 50.0,
            "transactions": [],
            "applied_reconciliations": {}
        }, f)
    monkeypatch.setenv("BUDGET_FILE", temp_budget)
    monkeypatch.setenv("BUDGET_FILE_PATH", temp_budget)
    monkeypatch.setattr("app.BUDGET_FILE", temp_budget, raising=False)
    clear_test_authoritative_provider_records()


# =========================================================================
# Workstream A: Financial Reconciliation Integrity & Authoritative Source
# =========================================================================

def test_p27_rejection_of_client_supplied_tokens_in_reconcile_usage(tmp_path, monkeypatch):
    """Client-supplied provider_tokens and cost must be rejected with HTTP 400."""
    test_db = str(tmp_path / "p27_reject.db")
    db_store.init_db(test_db)
    monkeypatch.setattr("db_persistence.DEFAULT_DB_FILENAME", os.path.basename(test_db))
    monkeypatch.setenv("DATABASE_PATH", test_db)
    monkeypatch.setenv("OPERATOR_AUTH_TOKEN", "op_token_123")
    monkeypatch.setenv("ADMIN_AUTH_TOKEN", "admin_token_456")

    sid = "sess_p27_rej"
    db_store.create_session(session_id=sid, project_name="Rejection Test", task_description="Desc", db_path=test_db)
    tid = db_store.create_task(session_id=sid, title="Task Rej", division_id="engineering", assigned_agent_id="dev", stage="dev", status="completed", db_path=test_db)
    run_id = db_store.record_agent_run(
        task_id=tid,
        session_id=sid,
        agent_id="dev",
        agent_name="Developer",
        division_id="engineering",
        prompt_file="dev.md",
        status="completed",
        tokens_used=200,
        token_source="estimated",
        provider="openrouter",
        model="test-model",
        provider_request_id="req_rej_1",
        db_path=test_db
    )

    client = app.test_client()

    # Attempt 1: Client provides provider_tokens -> 400 Bad Request
    res1 = client.post(
        f"/api/runs/{run_id}/reconcile_usage",
        headers={"Origin": "http://127.0.0.1:8080", "Content-Type": "application/json", "X-Operator-Token": "op_token_123"},
        json={"provider_tokens": 500}
    )
    assert res1.status_code == 400
    assert "Client-supplied provider_tokens and cost are forbidden" in res1.get_json()["error"]

    # Attempt 2: Client provides cost -> 400 Bad Request
    res2 = client.post(
        f"/api/runs/{run_id}/reconcile_usage",
        headers={"Origin": "http://127.0.0.1:8080", "Content-Type": "application/json", "X-Operator-Token": "op_token_123"},
        json={"cost": 0.05}
    )
    assert res2.status_code == 400
    assert "Client-supplied provider_tokens and cost are forbidden" in res2.get_json()["error"]

    # Verify audit log recorded rejection
    with db_store.get_db_connection(test_db) as conn:
        audit = conn.execute("SELECT * FROM recovery_audit_log WHERE target_id = ?;", (run_id,)).fetchone()
    assert audit is not None
    assert audit["outcome"] == "rejected"


def test_p27_authoritative_provider_lookup_and_db_persistence(tmp_path, monkeypatch):
    """Reconcile usage succeeds using authoritative lookup adapter and trusted run metadata."""
    test_db = str(tmp_path / "p27_auth_lookup.db")
    db_store.init_db(test_db)
    monkeypatch.setattr("db_persistence.DEFAULT_DB_FILENAME", os.path.basename(test_db))
    monkeypatch.setenv("DATABASE_PATH", test_db)
    monkeypatch.setenv("OPERATOR_AUTH_TOKEN", "op_token_123")

    sid = "sess_p27_auth"
    db_store.create_session(session_id=sid, project_name="Auth Lookup", task_description="Desc", db_path=test_db)
    tid = db_store.create_task(session_id=sid, title="Task Auth", division_id="engineering", assigned_agent_id="dev", stage="dev", status="completed", db_path=test_db)
    
    # Run recorded with provider, model, and genuine provider_request_id
    run_id = db_store.record_agent_run(
        task_id=tid,
        session_id=sid,
        agent_id="dev",
        agent_name="Developer",
        division_id="engineering",
        prompt_file="dev.md",
        status="completed",
        tokens_used=150,
        token_source="estimated",
        provider="openrouter",
        model="anthropic/claude-3.5-sonnet",
        provider_request_id="gen_openrouter_auth_444",
        db_path=test_db
    )

    # Verify persisted metadata via get_agent_run
    run_record = db_store.get_agent_run(run_id, db_path=test_db)
    assert run_record is not None
    assert run_record["provider"] == "openrouter"
    assert run_record["model"] == "anthropic/claude-3.5-sonnet"
    assert run_record["provider_request_id"] == "gen_openrouter_auth_444"

    # Register authoritative mock record
    set_test_authoritative_provider_record("gen_openrouter_auth_444", {
        "found": True,
        "tokens": 444,
        "cost": 0.00888,
        "provider_request_id": "gen_openrouter_auth_444"
    })

    client = app.test_client()
    res = client.post(
        f"/api/runs/{run_id}/reconcile_usage",
        headers={"Origin": "http://127.0.0.1:8080", "Content-Type": "application/json", "X-Operator-Token": "op_token_123"},
        json={}  # No tokens supplied!
    )
    assert res.status_code == 200
    data = res.get_json()
    assert data["success"] is True
    assert data["tokens_used"] == 444
    assert data["token_source"] == "provider_confirmed"
    assert data["reconciliation_status"] == "completed"

    # Verify run updated in SQLite
    updated_run = db_store.get_agent_run(run_id, db_path=test_db)
    assert updated_run["tokens_used"] == 444
    assert updated_run["token_source"] == "provider_confirmed"
    assert updated_run["reconciliation_status"] == "completed"


def test_p27_unresolved_state_when_authoritative_source_unavailable(tmp_path, monkeypatch):
    """When authoritative usage source cannot confirm tokens, run transitions to explicit 'unresolved' state."""
    test_db = str(tmp_path / "p27_unresolved.db")
    db_store.init_db(test_db)
    monkeypatch.setattr("db_persistence.DEFAULT_DB_FILENAME", os.path.basename(test_db))
    monkeypatch.setenv("DATABASE_PATH", test_db)
    monkeypatch.setenv("OPERATOR_AUTH_TOKEN", "op_token_123")

    sid = "sess_p27_unres"
    db_store.create_session(session_id=sid, project_name="Unresolved Test", task_description="Desc", db_path=test_db)
    tid = db_store.create_task(session_id=sid, title="Task Unres", division_id="engineering", assigned_agent_id="dev", stage="dev", status="completed", db_path=test_db)
    
    # Gemini run without post-hoc query support
    run_id = db_store.record_agent_run(
        task_id=tid,
        session_id=sid,
        agent_id="dev",
        agent_name="Developer",
        division_id="engineering",
        prompt_file="dev.md",
        status="completed",
        tokens_used=300,
        token_source="estimated",
        provider="gemini",
        model="gemini-3.5-flash-lite",
        provider_request_id="req_goog_unqueryable_999",
        db_path=test_db
    )

    client = app.test_client()
    res = client.post(
        f"/api/runs/{run_id}/reconcile_usage",
        headers={"Origin": "http://127.0.0.1:8080", "Content-Type": "application/json", "X-Operator-Token": "op_token_123"},
        json={}
    )
    assert res.status_code == 400
    data = res.get_json()
    assert data["success"] is False
    assert data["reconciliation_status"] == "unresolved"

    # Verify run record in SQLite is updated to unresolved
    run = db_store.get_agent_run(run_id, db_path=test_db)
    assert run["reconciliation_status"] == "unresolved"
    assert run["tokens_used"] == 300
    assert run["token_source"] == "estimated"


def test_p27_manual_adjustment_workflow_requires_admin_and_records_audit(tmp_path, monkeypatch):
    """Manual adjustment requires admin credentials, applies delta, and records manual_operator_adjustment."""
    test_db = str(tmp_path / "p27_manual_adj.db")
    db_store.init_db(test_db)
    monkeypatch.setattr("db_persistence.DEFAULT_DB_FILENAME", os.path.basename(test_db))
    monkeypatch.setenv("DATABASE_PATH", test_db)
    monkeypatch.setenv("OPERATOR_AUTH_TOKEN", "op_token_123")
    monkeypatch.setenv("ADMIN_AUTH_TOKEN", "admin_token_456")

    sid = "sess_p27_man"
    db_store.create_session(session_id=sid, project_name="Manual Adj", task_description="Desc", db_path=test_db)
    tid = db_store.create_task(session_id=sid, title="Task Man", division_id="engineering", assigned_agent_id="dev", stage="dev", status="completed", db_path=test_db)
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

    # 1. Non-admin operator is denied with 403 Forbidden
    res_op = client.post(
        f"/api/runs/{run_id}/manual_adjustment",
        headers={"Origin": "http://127.0.0.1:8080", "Content-Type": "application/json", "X-Operator-Token": "op_token_123"},
        json={"adjusted_tokens": 1200, "reason": "Invoice check", "evidence_reference": "INV-2026-99"}
    )
    assert res_op.status_code == 403
    assert "Elevated administrator privileges required" in res_op.get_json()["error"]

    # 2. Validation: missing reason or evidence
    res_val = client.post(
        f"/api/runs/{run_id}/manual_adjustment",
        headers={"Origin": "http://127.0.0.1:8080", "Content-Type": "application/json", "X-Admin-Token": "admin_token_456"},
        json={"adjusted_tokens": 1200, "reason": ""}
    )
    assert res_val.status_code == 400

    # 3. Admin successfully applies manual adjustment
    res_admin = client.post(
        f"/api/runs/{run_id}/manual_adjustment",
        headers={"Origin": "http://127.0.0.1:8080", "Content-Type": "application/json", "X-Admin-Token": "admin_token_456"},
        json={
            "adjusted_tokens": 1250,
            "cost": 0.025,
            "reason": "Verified against external provider invoice CSV",
            "evidence_reference": "PROVIDER-INV-OCT-2026-ROW-44"
        }
    )
    assert res_admin.status_code == 200
    data = res_admin.get_json()
    assert data["success"] is True
    assert data["adjusted_tokens"] == 1250
    assert data["token_source"] == "manual_operator_adjustment"
    assert data["reconciliation_status"] == "completed"

    # Verify run updated in SQLite
    run = db_store.get_agent_run(run_id, db_path=test_db)
    assert run["tokens_used"] == 1250
    assert run["token_source"] == "manual_operator_adjustment"
    assert run["reconciliation_status"] == "completed"

    # Verify audit log has durable record
    with db_store.get_db_connection(test_db) as conn:
        audits = conn.execute("SELECT * FROM recovery_audit_log WHERE target_id = ? ORDER BY timestamp ASC;", (run_id,)).fetchall()
    assert len(audits) >= 2
    assert audits[0]["principal"] == "operator"
    assert audits[0]["outcome"] == "denied"
    assert audits[-1]["principal"] == "admin"
    assert audits[-1]["outcome"] == "success"
    assert "PROVIDER-INV-OCT-2026-ROW-44" in audits[-1]["error_message"]


# =========================================================================
# Workstream B: Truncation-Proof Idempotency & Budget File Locking
# =========================================================================

def test_p27_truncation_proof_idempotency_after_60_transactions(tmp_path):
    """Reconciliation must never double-charge even if 60+ transactions evict it from rolling list."""
    temp_budget = str(tmp_path / "budget_truncation_test.json")
    with open(temp_budget, "w", encoding="utf-8") as f:
        json.dump({
            "tokens_used": 5000,
            "estimated_cost": 0.05,
            "transactions": [],
            "applied_reconciliations": {}
        }, f)

    journal_id = "rec_journal_trunc_test_001"
    run_id = "run_trunc_test"

    # 1. Apply reconciliation delta of +1000 tokens
    proj1 = db_store.project_reconciliation_to_budget(
        budget_path=temp_budget,
        journal_id=journal_id,
        run_id=run_id,
        delta_tokens=1000,
        delta_cost=0.01
    )
    assert proj1["projected"] is True
    assert proj1["already_applied"] is False
    assert proj1["budget"]["tokens_used"] == 6000

    # 2. Simulate 75 subsequent transactions in budget, evicting the transaction from rolling txs[:60]
    with open(temp_budget, "r", encoding="utf-8") as f:
        budget = json.load(f)

    for i in range(75):
        budget["transactions"].insert(0, {
            "id": 9000000 + i,
            "time": "2026-10-26 12:00:00",
            "agent": "Worker",
            "department": "Engineering",
            "task": f"Task {i}",
            "tokens": 10,
            "cost": 0.0001,
            "source": "provider_reported",
            "ref_journal_id": f"other_journal_{i}"
        })
    budget["transactions"] = budget["transactions"][:60]
    budget["tokens_used"] += 750
    db_store._atomic_write_json_direct(temp_budget, budget)

    # Verify journal_id is no longer present in budget["transactions"]
    with open(temp_budget, "r", encoding="utf-8") as f:
        budget_now = json.load(f)
    assert not any(tx.get("ref_journal_id") == journal_id for tx in budget_now["transactions"])
    assert journal_id in budget_now.get("applied_reconciliations", {})

    # 3. Simulate recovery or retry of the original reconciliation
    proj2 = db_store.project_reconciliation_to_budget(
        budget_path=temp_budget,
        journal_id=journal_id,
        run_id=run_id,
        delta_tokens=1000,
        delta_cost=0.01
    )
    assert proj2["projected"] is True
    assert proj2["already_applied"] is True
    # Tokens must NOT have increased!
    assert proj2["budget"]["tokens_used"] == budget_now["tokens_used"]


def test_p27_concurrent_budget_locking_prevents_lost_updates(tmp_path):
    """Concurrent updates across multiple threads or processes must not cause lost updates."""
    temp_budget = str(tmp_path / "budget_concurrent.json")
    with open(temp_budget, "w", encoding="utf-8") as f:
        json.dump({
            "tokens_used": 0,
            "estimated_cost": 0.0,
            "transactions": [],
            "applied_reconciliations": {}
        }, f)

    num_threads = 10
    tokens_per_thread = 50
    errors = []

    def _worker(thread_idx):
        try:
            jid = f"journal_conc_{thread_idx}"
            rid = f"run_conc_{thread_idx}"
            res = db_store.project_reconciliation_to_budget(
                budget_path=temp_budget,
                journal_id=jid,
                run_id=rid,
                delta_tokens=tokens_per_thread,
                delta_cost=0.001
            )
            assert res["projected"] is True
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=_worker, args=(i,)) for i in range(num_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(errors) == 0

    with open(temp_budget, "r", encoding="utf-8") as f:
        final_budget = json.load(f)

    # Exactly 10 * 50 = 500 tokens used
    assert final_budget["tokens_used"] == num_threads * tokens_per_thread
    assert len(final_budget["applied_reconciliations"]) == num_threads


def test_p27_corrupted_json_budget_restores_from_backup(tmp_path):
    """When budget.json is corrupted or partially written, recovery falls back to .bak."""
    temp_budget = str(tmp_path / "corrupted_budget.json")
    bak_budget = str(tmp_path / "corrupted_budget.json.bak")

    # Create healthy backup
    with open(bak_budget, "w", encoding="utf-8") as bf:
        json.dump({
            "tokens_used": 4200,
            "estimated_cost": 0.042,
            "budget_limit": 50.0,
            "transactions": [],
            "applied_reconciliations": {}
        }, bf)

    # Write truncated/corrupt budget.json
    with open(temp_budget, "w", encoding="utf-8") as f:
        f.write('{"tokens_used": 4200, "estimated_cost": ')  # Syntax error

    # Projecting reconciliation should safely restore from .bak and apply delta
    res = db_store.project_reconciliation_to_budget(
        budget_path=temp_budget,
        journal_id="rec_bak_recovery_1",
        run_id="run_bak_1",
        delta_tokens=100,
        delta_cost=0.001
    )
    assert res["projected"] is True
    assert res["budget"]["tokens_used"] == 4300

    # Verify temp_budget is now valid readable JSON
    with open(temp_budget, "r", encoding="utf-8") as f:
        recovered = json.load(f)
    assert recovered["tokens_used"] == 4300


# =========================================================================
# Workstream C: Crash Boundaries & Dual-Ledger Outbox Failpoints
# =========================================================================

def test_p27_crash_failpoint_after_db_before_budget_and_startup_drain(tmp_path, monkeypatch):
    """Simulated crash after SQLite journal commit (applied_db) drains safely on startup."""
    test_db = str(tmp_path / "p27_crash_db.db")
    db_store.init_db(test_db)
    monkeypatch.setattr("db_persistence.DEFAULT_DB_FILENAME", os.path.basename(test_db))
    monkeypatch.setenv("DATABASE_PATH", test_db)
    test_budget = str(tmp_path / "test_budget_crash.json")
    monkeypatch.setattr("app.BUDGET_FILE", test_budget)

    with open(test_budget, "w", encoding="utf-8") as f:
        json.dump({"tokens_used": 1000, "estimated_cost": 0.01, "transactions": [], "applied_reconciliations": {}}, f)

    sid = "sess_p27_crash"
    db_store.create_session(session_id=sid, project_name="Crash Test", task_description="Desc", db_path=test_db)
    tid = db_store.create_task(session_id=sid, title="Task Crash", division_id="engineering", assigned_agent_id="dev", stage="dev", status="completed", db_path=test_db)
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
        provider="openrouter",
        provider_request_id="req_crash_fp",
        db_path=test_db
    )

    lookup = lambda rid, qid: {"found": True, "tokens": 800, "cost": 0.008, "provider_request_id": "req_crash_fp"}

    # Simulate crash right after DB commit
    with pytest.raises(IOError, match="Crash simulated after DB commit before budget update"):
        db_store.reconcile_provider_run_usage(
            run_id=run_id,
            provider_lookup_fn=lookup,
            failpoint="after_db_before_budget",
            budget_path=test_budget,
            db_path=test_db
        )

    # SQLite has committed the journal row as 'applied_db'
    with db_store.get_db_connection(test_db) as conn:
        j_row = conn.execute("SELECT * FROM reconciliation_journal WHERE run_id = ?;", (run_id,)).fetchone()
    assert j_row is not None
    assert j_row["status"] == "applied_db"

    # Budget has NOT yet been updated (remains 1000)
    with open(test_budget, "r", encoding="utf-8") as f:
        b_pre = json.load(f)
    assert b_pre["tokens_used"] == 1000

    # Startup recovery runs and drains the outbox entry
    db_store.reconcile_interrupted_sessions(db_path=test_db, budget_path=test_budget)

    # Outbox journal is now 'completed'
    with db_store.get_db_connection(test_db) as conn:
        j_row_post = conn.execute("SELECT * FROM reconciliation_journal WHERE run_id = ?;", (run_id,)).fetchone()
    assert j_row_post["status"] == "completed"

    # Budget has been updated: 1000 + (800 - 100) = 1700
    with open(test_budget, "r", encoding="utf-8") as f:
        b_post = json.load(f)
    assert b_post["tokens_used"] == 1700


def test_p27_crash_failpoint_after_budget_before_journal_complete(tmp_path, monkeypatch):
    """Simulated crash after budget projection before journal completion recovers idempotently."""
    test_db = str(tmp_path / "p27_crash_post_budget.db")
    db_store.init_db(test_db)
    monkeypatch.setattr("db_persistence.DEFAULT_DB_FILENAME", os.path.basename(test_db))
    monkeypatch.setenv("DATABASE_PATH", test_db)
    test_budget = str(tmp_path / "test_budget_crash2.json")

    with open(test_budget, "w", encoding="utf-8") as f:
        json.dump({"tokens_used": 1000, "estimated_cost": 0.01, "transactions": [], "applied_reconciliations": {}}, f)

    sid = "sess_p27_crash2"
    db_store.create_session(session_id=sid, project_name="Crash Test 2", task_description="Desc", db_path=test_db)
    tid = db_store.create_task(session_id=sid, title="Task Crash 2", division_id="engineering", assigned_agent_id="dev", stage="dev", status="completed", db_path=test_db)
    run_id = db_store.record_agent_run(
        task_id=tid,
        session_id=sid,
        agent_id="dev",
        agent_name="Developer",
        division_id="engineering",
        prompt_file="dev.md",
        status="completed",
        tokens_used=200,
        token_source="estimated",
        provider="openrouter",
        provider_request_id="req_crash_fp2",
        db_path=test_db
    )

    lookup = lambda rid, qid: {"found": True, "tokens": 600, "cost": 0.006, "provider_request_id": "req_crash_fp2"}

    # Simulate crash after budget update
    with pytest.raises(IOError, match="Crash simulated after budget projection before journal completion"):
        db_store.reconcile_provider_run_usage(
            run_id=run_id,
            provider_lookup_fn=lookup,
            failpoint="after_budget_before_journal_complete",
            budget_path=test_budget,
            db_path=test_db
        )

    # Budget has been updated once: 1000 + (600 - 200) = 1400
    with open(test_budget, "r", encoding="utf-8") as f:
        b_mid = json.load(f)
    assert b_mid["tokens_used"] == 1400

    # Retry reconciliation (should detect already projected in budget and complete journal without double-charging)
    res_retry = db_store.reconcile_provider_run_usage(
        run_id=run_id,
        provider_lookup_fn=lookup,
        budget_path=test_budget,
        db_path=test_db
    )
    assert res_retry["success"] is True

    # Budget must STILL be 1400 (NOT 1800!)
    with open(test_budget, "r", encoding="utf-8") as f:
        b_final = json.load(f)
    assert b_final["tokens_used"] == 1400

    # Journal marked completed
    with db_store.get_db_connection(test_db) as conn:
        j_final = conn.execute("SELECT * FROM reconciliation_journal WHERE run_id = ?;", (run_id,)).fetchone()
    assert j_final["status"] == "completed"


# =========================================================================
# Workstream D: Fail-Closed Security & Audit Integrity
# =========================================================================

def test_p27_audit_failure_fails_closed(tmp_path, monkeypatch):
    """When audit logging fails, endpoint must fail closed and return HTTP 500 without silently succeeding."""
    test_db = str(tmp_path / "p27_audit_fail.db")
    db_store.init_db(test_db)
    monkeypatch.setattr("db_persistence.DEFAULT_DB_FILENAME", os.path.basename(test_db))
    monkeypatch.setenv("DATABASE_PATH", test_db)
    monkeypatch.setenv("ADMIN_AUTH_TOKEN", "admin_secret_token")

    sid = "sess_p27_aud"
    db_store.create_session(session_id=sid, project_name="Audit Fail", task_description="Desc", db_path=test_db)
    tid = db_store.create_task(session_id=sid, title="Task Aud", division_id="engineering", assigned_agent_id="dev", stage="dev", status="completed", db_path=test_db)
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

    # Mock audit recording to raise an exception
    with patch("db_persistence.record_recovery_audit_event", side_effect=RuntimeError("Disk write failed on audit log")):
        res = client.post(
            f"/api/runs/{run_id}/manual_adjustment",
            headers={"Origin": "http://127.0.0.1:8080", "Content-Type": "application/json", "X-Admin-Token": "admin_secret_token"},
            json={"adjusted_tokens": 500, "reason": "Test", "evidence_reference": "EVID-1"}
        )
        assert res.status_code == 500
        assert "Failed to apply manual adjustment" in res.get_json()["error"]


def test_p27_crash_before_first_ledger_write(tmp_path, monkeypatch):
    """Crash before first ledger write leaves SQLite run untouched and budget unaffected."""
    test_db = str(tmp_path / "p27_pre_write.db")
    db_store.init_db(test_db)
    monkeypatch.setattr("db_persistence.DEFAULT_DB_FILENAME", os.path.basename(test_db))
    monkeypatch.setenv("DATABASE_PATH", test_db)
    test_budget = str(tmp_path / "test_budget_pre_write.json")

    with open(test_budget, "w", encoding="utf-8") as f:
        json.dump({"tokens_used": 500, "estimated_cost": 0.005, "transactions": [], "applied_reconciliations": {}}, f)

    sid = "sess_p27_pre"
    db_store.create_session(session_id=sid, project_name="Pre Write", task_description="Desc", db_path=test_db)
    tid = db_store.create_task(session_id=sid, title="Task Pre", division_id="engineering", assigned_agent_id="dev", stage="dev", status="completed", db_path=test_db)
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

    lookup = lambda rid, qid: {"found": True, "tokens": 800, "cost": 0.008, "provider_request_id": "req_pre_fail"}

    with pytest.raises(IOError, match="Crash simulated before SQLite commit"):
        db_store.reconcile_provider_run_usage(
            run_id=run_id,
            provider_lookup_fn=lookup,
            failpoint="before_db_commit",
            budget_path=test_budget,
            db_path=test_db
        )

    # SQLite journal has no entry
    with db_store.get_db_connection(test_db) as conn:
        j_count = conn.execute("SELECT COUNT(*) as cnt FROM reconciliation_journal WHERE run_id = ?;", (run_id,)).fetchone()["cnt"]
    assert j_count == 0

    # Run remains 'estimated' with 100 tokens
    run = db_store.get_agent_run(run_id, db_path=test_db)
    assert run["tokens_used"] == 100
    assert run["token_source"] == "estimated"
    assert run["reconciliation_status"] in ("none", None)

    # Budget remains 500 tokens
    with open(test_budget, "r", encoding="utf-8") as f:
        budget = json.load(f)
    assert budget["tokens_used"] == 500


def test_p27_provider_and_model_persistence_in_task_completion(tmp_path):
    """complete_task_with_agent_run stores provider and model in agent_runs table."""
    test_db = str(tmp_path / "p27_prov_model.db")
    db_store.init_db(test_db)

    sid = "sess_p27_pm"
    db_store.create_session(session_id=sid, project_name="Prov Model", task_description="Desc", db_path=test_db)
    tid = db_store.create_task(session_id=sid, title="Task PM", division_id="engineering", assigned_agent_id="dev", stage="dev", status="pending", db_path=test_db)

    # 1. Pre-flight start record with provider & model
    run_id_pre = db_store.record_agent_run_start(
        task_id=tid,
        session_id=sid,
        agent_id="dev",
        agent_name="Developer",
        division_id="engineering",
        prompt_file="dev.md",
        provider="openrouter",
        model="meta-llama/llama-3.3-70b-instruct",
        db_path=test_db
    )
    pre_run = db_store.get_agent_run(run_id_pre, db_path=test_db)
    assert pre_run["status"] == "running"
    assert pre_run["provider"] == "openrouter"
    assert pre_run["model"] == "meta-llama/llama-3.3-70b-instruct"

    # 2. Finalize completion with provider & model
    ok, final_run_id = db_store.complete_task_with_agent_run(
        task_id=tid,
        session_id=sid,
        agent_id="dev",
        agent_name="Developer",
        division_id="engineering",
        prompt_file="dev.md",
        output_text="Completed app deliverable",
        tokens_used=750,
        token_source="provider_reported",
        provider_request_id="req_llama_750",
        provider="openrouter",
        model="meta-llama/llama-3.3-70b-instruct",
        db_path=test_db
    )
    assert ok is True
    assert final_run_id == run_id_pre

    final_run = db_store.get_agent_run(final_run_id, db_path=test_db)
    assert final_run["status"] == "completed"
    assert final_run["tokens_used"] == 750
    assert final_run["provider"] == "openrouter"
    assert final_run["model"] == "meta-llama/llama-3.3-70b-instruct"
    assert final_run["provider_request_id"] == "req_llama_750"


def test_p27_multiple_consecutive_reconciliation_retries_idempotency(tmp_path):
    """Calling reconcile_provider_run_usage repeatedly never double-charges budget."""
    test_db = str(tmp_path / "p27_retries.db")
    db_store.init_db(test_db)
    test_budget = str(tmp_path / "test_budget_retries.json")

    with open(test_budget, "w", encoding="utf-8") as f:
        json.dump({"tokens_used": 1000, "estimated_cost": 0.01, "transactions": [], "applied_reconciliations": {}}, f)

    sid = "sess_p27_ret"
    db_store.create_session(session_id=sid, project_name="Retries Test", task_description="Desc", db_path=test_db)
    tid = db_store.create_task(session_id=sid, title="Task Ret", division_id="engineering", assigned_agent_id="dev", stage="dev", status="completed", db_path=test_db)
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
        provider_request_id="req_repeat_1",
        db_path=test_db
    )

    lookup = lambda rid, qid: {"found": True, "tokens": 500, "cost": 0.005, "provider_request_id": "req_repeat_1"}

    # Run 1: Normal initial reconciliation
    res1 = db_store.reconcile_provider_run_usage(
        run_id=run_id,
        provider_lookup_fn=lookup,
        budget_path=test_budget,
        db_path=test_db
    )
    assert res1["success"] is True
    assert res1["tokens_used"] == 500
    assert not res1.get("already_reconciled")

    with open(test_budget, "r", encoding="utf-8") as f:
        b1 = json.load(f)
    assert b1["tokens_used"] == 1400  # 1000 + (500 - 100)

    # Consecutive runs 2 through 6
    for attempt in range(2, 7):
        res_n = db_store.reconcile_provider_run_usage(
            run_id=run_id,
            provider_lookup_fn=lookup,
            budget_path=test_budget,
            db_path=test_db
        )
        assert res_n["success"] is True
        assert res_n.get("already_reconciled") is True

    # Final budget tokens MUST still be exactly 1400
    with open(test_budget, "r", encoding="utf-8") as f:
        b_final = json.load(f)
    assert b_final["tokens_used"] == 1400


def test_p27_manual_adjustment_idempotency_key_replay_and_conflict(tmp_path, monkeypatch):
    """Idempotency-Key header provides replay protection (200 OK) and conflict detection (409)."""
    test_db = str(tmp_path / "p27_idemp_key.db")
    db_store.init_db(test_db)
    monkeypatch.setattr("db_persistence.DEFAULT_DB_FILENAME", os.path.basename(test_db))
    monkeypatch.setenv("DATABASE_PATH", test_db)
    monkeypatch.setenv("ADMIN_AUTH_TOKEN", "admin_secret_token")

    test_budget = str(tmp_path / "test_budget_idemp.json")
    with open(test_budget, "w", encoding="utf-8") as f:
        json.dump({"tokens_used": 1000, "estimated_cost": 0.01, "transactions": [], "applied_reconciliations": {}}, f)
    monkeypatch.setenv("BUDGET_FILE", test_budget)
    monkeypatch.setenv("BUDGET_FILE_PATH", test_budget)
    monkeypatch.setattr("app.BUDGET_FILE", test_budget, raising=False)

    sid = "sess_p27_idemp"
    db_store.create_session(session_id=sid, project_name="Idemp Test", task_description="Desc", db_path=test_db)
    tid = db_store.create_task(session_id=sid, title="Task Idemp", division_id="engineering", assigned_agent_id="dev", stage="dev", status="completed", db_path=test_db)
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
    idemp_key = f"idemp_{uuid.uuid4().hex}"

    # Request 1: First application with Idempotency-Key
    res1 = client.post(
        f"/api/runs/{run_id}/manual_adjustment",
        headers={
            "Origin": "http://127.0.0.1:8080",
            "Content-Type": "application/json",
            "X-Admin-Token": "admin_secret_token",
            "Idempotency-Key": idemp_key
        },
        json={"adjusted_tokens": 800, "reason": "First manual adjustment", "evidence_reference": "REF-001"}
    )
    assert res1.status_code == 200
    data1 = res1.get_json()
    assert data1["adjusted_tokens"] == 800
    assert data1["delta_tokens"] == 700

    # Request 2: Replay with identical payload and same Idempotency-Key -> 200 OK with X-Idempotent-Replay header
    res2 = client.post(
        f"/api/runs/{run_id}/manual_adjustment",
        headers={
            "Origin": "http://127.0.0.1:8080",
            "Content-Type": "application/json",
            "X-Admin-Token": "admin_secret_token",
            "Idempotency-Key": idemp_key
        },
        json={"adjusted_tokens": 800, "reason": "First manual adjustment", "evidence_reference": "REF-001"}
    )
    assert res2.status_code == 200
    assert res2.headers.get("X-Idempotent-Replay") == "true"
    data2 = res2.get_json()
    assert data2["adjusted_tokens"] == 800
    assert data2["resumed_from_idempotency"] is True

    # Request 3: Replay with DIFFERENT payload using same Idempotency-Key -> 409 Conflict
    res3 = client.post(
        f"/api/runs/{run_id}/manual_adjustment",
        headers={
            "Origin": "http://127.0.0.1:8080",
            "Content-Type": "application/json",
            "X-Admin-Token": "admin_secret_token",
            "Idempotency-Key": idemp_key
        },
        json={"adjusted_tokens": 1500, "reason": "Conflicting different payload", "evidence_reference": "REF-CONFLICT"}
    )
    assert res3.status_code == 409
    assert "Idempotency key" in res3.get_json()["error"]
    assert "previously used with different request parameters" in res3.get_json()["error"]


def test_p27_manual_adjustment_incremental_mode(tmp_path, monkeypatch):
    """Incremental manual adjustment modifies tokens additively."""
    test_db = str(tmp_path / "p27_incr.db")
    db_store.init_db(test_db)
    monkeypatch.setattr("db_persistence.DEFAULT_DB_FILENAME", os.path.basename(test_db))
    monkeypatch.setenv("DATABASE_PATH", test_db)
    monkeypatch.setenv("ADMIN_AUTH_TOKEN", "admin_secret_token")

    test_budget = str(tmp_path / "test_budget_incr.json")
    with open(test_budget, "w", encoding="utf-8") as f:
        json.dump({"tokens_used": 1000, "estimated_cost": 0.01, "transactions": [], "applied_reconciliations": {}}, f)
    monkeypatch.setenv("BUDGET_FILE", test_budget)
    monkeypatch.setenv("BUDGET_FILE_PATH", test_budget)
    monkeypatch.setattr("app.BUDGET_FILE", test_budget, raising=False)

    sid = "sess_p27_incr"
    db_store.create_session(session_id=sid, project_name="Incr Test", task_description="Desc", db_path=test_db)
    tid = db_store.create_task(session_id=sid, title="Task Incr", division_id="engineering", assigned_agent_id="dev", stage="dev", status="completed", db_path=test_db)
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

    client = app.test_client()

    # Apply incremental +250 tokens
    res = client.post(
        f"/api/runs/{run_id}/manual_adjustment",
        headers={"Origin": "http://127.0.0.1:8080", "Content-Type": "application/json", "X-Admin-Token": "admin_secret_token"},
        json={
            "adjusted_tokens": 250,
            "is_incremental": True,
            "reason": "Additive late billing credit",
            "evidence_reference": "INCR-001"
        }
    )
    assert res.status_code == 200
    data = res.get_json()
    assert data["prior_tokens"] == 500
    assert data["adjusted_tokens"] == 750
    assert data["delta_tokens"] == 250

    # Verify budget ledger updated by delta (+250)
    with open(test_budget, "r", encoding="utf-8") as f:
        b = json.load(f)
    assert b["tokens_used"] == 1250  # 1000 + 250


def test_p27_manual_adjustment_negative_token_rejection(tmp_path, monkeypatch):
    """Negative absolute token values are rejected with HTTP 400."""
    test_db = str(tmp_path / "p27_neg.db")
    db_store.init_db(test_db)
    monkeypatch.setattr("db_persistence.DEFAULT_DB_FILENAME", os.path.basename(test_db))
    monkeypatch.setenv("DATABASE_PATH", test_db)
    monkeypatch.setenv("ADMIN_AUTH_TOKEN", "admin_secret_token")

    sid = "sess_p27_neg"
    db_store.create_session(session_id=sid, project_name="Neg Test", task_description="Desc", db_path=test_db)
    tid = db_store.create_task(session_id=sid, title="Task Neg", division_id="engineering", assigned_agent_id="dev", stage="dev", status="completed", db_path=test_db)
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

    client = app.test_client()

    # Negative absolute token adjustment -> 400
    res = client.post(
        f"/api/runs/{run_id}/manual_adjustment",
        headers={"Origin": "http://127.0.0.1:8080", "Content-Type": "application/json", "X-Admin-Token": "admin_secret_token"},
        json={"adjusted_tokens": -50, "reason": "Invalid negative", "evidence_reference": "NEG-001"}
    )
    assert res.status_code == 400
    assert "'adjusted_tokens' must be non-negative" in res.get_json()["error"]


def test_p27_cross_process_file_lock_dead_process_cleanup(tmp_path):
    """_budget_file_lock automatically detects and breaks dead process abandoned locks."""
    budget_file = str(tmp_path / "dead_lock_budget.json")
    with open(budget_file, "w", encoding="utf-8") as f:
        json.dump({"tokens_used": 0, "transactions": []}, f)

    lock_file = f"{budget_file}.lock"
    # Create an abandoned lock file owned by a PID that definitely does not exist (PID 999999)
    with open(lock_file, "w", encoding="utf-8") as lf:
        lf.write(f"999999:{time.time() - 100.0}\n")

    # Acquiring the lock should detect that PID 999999 is dead and break it without timing out
    t0 = time.time()
    with db_store._budget_file_lock(budget_file, timeout=5.0):
        # We successfully acquired the lock
        assert os.path.exists(lock_file)
        with open(lock_file, "r", encoding="utf-8") as lf:
            content = lf.read()
        assert content.startswith(f"{os.getpid()}:")

    # Lock should take < 1 second to break dead PID
    assert time.time() - t0 < 2.0
    # On exit, lock should be cleanly removed
    assert not os.path.exists(lock_file)

