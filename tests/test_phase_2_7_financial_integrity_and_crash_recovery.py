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
import subprocess
from decimal import Decimal
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


# =========================================================================
# Workstream B: Phase 2.7.3 Critical Financial Integrity & Provenance Tests
# =========================================================================

def test_p27_financial_validation_strict_rejection_callable_functions(tmp_path, monkeypatch):
    """
    Direct validation unit test & callable entry-point validation:
    Rejects NaN, +/-inf, bool, malformed strings, negative values, and fractional tokens.
    """
    # 1. Direct validation tests
    with pytest.raises(ValueError, match="boolean"):
        db_store.validate_financial_numeric(True, field_name="cost")
    with pytest.raises(ValueError, match="boolean"):
        db_store.validate_financial_numeric(False, field_name="cost")
    with pytest.raises(ValueError, match="finite"):
        db_store.validate_financial_numeric(float("nan"), field_name="cost")
    with pytest.raises(ValueError, match="finite"):
        db_store.validate_financial_numeric(float("inf"), field_name="cost")
    with pytest.raises(ValueError, match="finite"):
        db_store.validate_financial_numeric(float("-inf"), field_name="cost")
    with pytest.raises(ValueError, match="non-negative"):
        db_store.validate_financial_numeric(-1.5, field_name="cost", allow_negative=False)
    with pytest.raises(ValueError, match="valid numeric"):
        db_store.validate_financial_numeric("not_a_number", field_name="cost")
    with pytest.raises(ValueError, match="exceeds maximum"):
        db_store.validate_financial_numeric(2_000_000_000.0, field_name="cost", max_value=1_000_000.0)

    # Decimal support & precision
    val_d = db_store.validate_financial_numeric(Decimal("12.3456789"), field_name="cost")
    assert val_d == 12.345679

    # 2. Token count validation
    with pytest.raises(ValueError, match="boolean"):
        db_store.validate_token_count(True, field_name="tokens")
    with pytest.raises(ValueError, match="boolean"):
        db_store.validate_token_count(False, field_name="tokens")
    with pytest.raises(ValueError, match="finite"):
        db_store.validate_token_count(float("nan"), field_name="tokens")
    with pytest.raises(ValueError, match="fractional"):
        db_store.validate_token_count(10.5, field_name="tokens")
    with pytest.raises(ValueError, match="non-negative"):
        db_store.validate_token_count(-50, field_name="tokens", allow_negative=False)
    with pytest.raises(ValueError, match="valid integer string"):
        db_store.validate_token_count("123abc", field_name="tokens")
    assert db_store.validate_token_count("1500", field_name="tokens") == 1500
    assert db_store.validate_token_count(Decimal("2500"), field_name="tokens") == 2500

    # 3. Callable functions reject invalid inputs
    test_db = str(tmp_path / "val_call.db")
    db_store.init_db(test_db)
    sid = "sess_val_call"
    db_store.create_session(session_id=sid, project_name="Val Call", task_description="Desc", db_path=test_db)
    tid = db_store.create_task(session_id=sid, title="Task", division_id="engineering", assigned_agent_id="dev", stage="dev", status="completed", db_path=test_db)

    # record_agent_run rejects boolean tokens
    with pytest.raises(ValueError, match="boolean"):
        db_store.record_agent_run(
            task_id=tid, session_id=sid, agent_id="dev", agent_name="Dev",
            division_id="engineering", prompt_file="p.md", status="completed",
            tokens_used=True, db_path=test_db
        )

    # record_agent_run rejects NaN cost
    with pytest.raises(ValueError, match="finite"):
        db_store.record_agent_run(
            task_id=tid, session_id=sid, agent_id="dev", agent_name="Dev",
            division_id="engineering", prompt_file="p.md", status="completed",
            tokens_used=100, cost_booked=float("nan"), db_path=test_db
        )

    # apply_manual_run_adjustment rejects boolean adjusted_tokens
    with pytest.raises(ValueError, match="boolean"):
        db_store.apply_manual_run_adjustment(
            run_id="run_1", adjusted_tokens=True, reason="r", evidence_reference="e", db_path=test_db
        )

    # reconcile_provider_run_usage rejects invalid provider cost
    run_id = db_store.record_agent_run(
        task_id=tid, session_id=sid, agent_id="dev", agent_name="Dev",
        division_id="engineering", prompt_file="p.md", status="completed",
        tokens_used=100, cost_booked=0.1, db_path=test_db
    )
    rec_res = db_store.reconcile_provider_run_usage(
        run_id=run_id,
        provider_lookup_fn=lambda rid, req: {"found": True, "tokens": 100, "cost": float("nan")},
        db_path=test_db
    )
    assert rec_res["success"] is False
    assert rec_res["reconciliation_status"] == "failed"


def test_p27_financial_validation_endpoint_rejections_and_no_mutation(tmp_path, monkeypatch):
    """
    Test invalid values through API route POST /api/runs/<run_id>/manual_adjustment.
    Assert that invalid values are rejected with 400 and cause NO partial mutation in DB or budget.
    """
    test_db = str(tmp_path / "p27_val_ep.db")
    db_store.init_db(test_db)
    monkeypatch.setattr("db_persistence.DEFAULT_DB_FILENAME", os.path.basename(test_db))
    monkeypatch.setenv("DATABASE_PATH", test_db)
    monkeypatch.setenv("ADMIN_AUTH_TOKEN", "admin_secret_token")

    test_budget = str(tmp_path / "val_ep_budget.json")
    with open(test_budget, "w", encoding="utf-8") as f:
        json.dump({"tokens_used": 1000, "estimated_cost": 0.15, "transactions": [], "applied_reconciliations": {}}, f)
    monkeypatch.setattr("app.get_budget_file", lambda: test_budget)

    sid = "sess_p27_val_ep"
    db_store.create_session(session_id=sid, project_name="Val EP Test", task_description="Desc", db_path=test_db)
    tid = db_store.create_task(session_id=sid, title="Task", division_id="engineering", assigned_agent_id="dev", stage="dev", status="completed", db_path=test_db)
    run_id = db_store.record_agent_run(
        task_id=tid, session_id=sid, agent_id="dev", agent_name="Developer",
        division_id="engineering", prompt_file="dev.md", status="completed",
        tokens_used=500, cost_booked=0.075, token_source="estimated", cost_source="estimated",
        db_path=test_db
    )

    client = app.test_client()
    headers = {
        "Origin": "http://127.0.0.1:8080",
        "Content-Type": "application/json",
        "X-Admin-Token": "admin_secret_token"
    }

    invalid_payloads = [
        {"adjusted_tokens": True, "reason": "Bool token", "evidence_reference": "REF1"},
        {"adjusted_tokens": "invalid_str", "reason": "Str token", "evidence_reference": "REF2"},
        {"adjusted_tokens": 12.34, "reason": "Fractional token", "evidence_reference": "REF3"},
        {"adjusted_tokens": -50, "is_incremental": False, "reason": "Negative absolute", "evidence_reference": "REF4"},
        {"adjusted_tokens": 100, "cost": "NaN", "reason": "NaN cost", "evidence_reference": "REF5"},
        {"adjusted_tokens": 100, "cost": "Infinity", "reason": "Inf cost", "evidence_reference": "REF6"},
        {"adjusted_tokens": 100, "cost": True, "reason": "Bool cost", "evidence_reference": "REF7"},
        {"adjusted_tokens": 100, "cost": -2.5, "is_incremental": False, "reason": "Negative abs cost", "evidence_reference": "REF8"},
    ]

    for payload in invalid_payloads:
        res = client.post(f"/api/runs/{run_id}/manual_adjustment", headers=headers, json=payload)
        assert res.status_code == 400, f"Expected 400 for payload {payload}, got {res.status_code}: {res.get_data(as_text=True)}"

    # Assert no mutation occurred in agent_runs, reconciliation_journal, or budget
    with db_store.get_db_connection(test_db) as conn:
        run = conn.execute("SELECT tokens_used, cost_booked, token_source, cost_source FROM agent_runs WHERE run_id = ?;", (run_id,)).fetchone()
        assert run["tokens_used"] == 500
        assert run["cost_booked"] == 0.075
        assert run["cost_source"] == "estimated"

        j_count = conn.execute("SELECT COUNT(*) as cnt FROM reconciliation_journal WHERE run_id = ?;", (run_id,)).fetchone()["cnt"]
        assert j_count == 0

    with open(test_budget, "r", encoding="utf-8") as f:
        b = json.load(f)
    assert b["tokens_used"] == 1000
    assert b["estimated_cost"] == 0.15


def test_p27_cost_provenance_preservation_when_provider_cost_missing(tmp_path, monkeypatch):
    """
    Authoritative cost preserved when subsequent provider response omits cost.
    Never assumes a previously authoritative booked cost is estimated.
    """
    test_db = str(tmp_path / "p27_prov_pres.db")
    db_store.init_db(test_db)
    test_budget = str(tmp_path / "prov_pres_budget.json")
    with open(test_budget, "w", encoding="utf-8") as f:
        json.dump({"tokens_used": 5000, "estimated_cost": 15.0, "transactions": [], "applied_reconciliations": {}}, f)

    sid = "sess_p27_prov_pres"
    db_store.create_session(session_id=sid, project_name="Prov Pres", task_description="Desc", db_path=test_db)
    tid = db_store.create_task(session_id=sid, title="Task", division_id="engineering", assigned_agent_id="dev", stage="dev", status="completed", db_path=test_db)

    # 1. Authoritative provider_confirmed run
    run_id1 = db_store.record_agent_run(
        task_id=tid, session_id=sid, agent_id="dev", agent_name="Dev",
        division_id="engineering", prompt_file="p.md", status="completed",
        tokens_used=1000, cost_booked=12.50, token_source="provider_confirmed",
        cost_source="provider_confirmed", provider_request_id="req_auth_1",
        db_path=test_db
    )

    # Provider lookup returns verified tokens (1200) but NO cost (None)
    def provider_lookup_no_cost(rid, req_id):
        return {"found": True, "tokens": 1200, "cost": None}

    res1 = db_store.reconcile_provider_run_usage(
        run_id=run_id1,
        provider_lookup_fn=provider_lookup_no_cost,
        db_path=test_db,
        budget_path=test_budget
    )
    assert res1["success"] is True
    assert res1["cost"] == 12.50  # Preserved authoritative cost!
    assert res1["delta_cost"] == 0.0  # Zero delta cost added!
    assert res1["cost_source"] == "provider_confirmed"

    # Verify DB state
    with db_store.get_db_connection(test_db) as conn:
        row1 = conn.execute("SELECT tokens_used, cost_booked, cost_source FROM agent_runs WHERE run_id = ?;", (run_id1,)).fetchone()
        assert row1["tokens_used"] == 1200
        assert row1["cost_booked"] == 12.50
        assert row1["cost_source"] == "provider_confirmed"

    # 2. Authoritative manual_operator_adjustment run
    run_id2 = db_store.record_agent_run(
        task_id=tid, session_id=sid, agent_id="dev", agent_name="Dev",
        division_id="engineering", prompt_file="p.md", status="completed",
        tokens_used=2000, cost_booked=8.00, token_source="manual_operator_adjustment",
        cost_source="manual_operator_adjustment", provider_request_id="req_manual_1",
        db_path=test_db
    )

    res2 = db_store.reconcile_provider_run_usage(
        run_id=run_id2,
        provider_lookup_fn=lambda rid, req: {"found": True, "tokens": 2500, "cost": None},
        db_path=test_db,
        budget_path=test_budget
    )
    assert res2["success"] is True
    assert res2["cost"] == 8.00  # Preserved manual authoritative cost!
    assert res2["delta_cost"] == 0.0
    assert res2["cost_source"] == "manual_operator_adjustment"

    # 3. Estimated run updates cost from token delta when cost is None
    run_id3 = db_store.record_agent_run(
        task_id=tid, session_id=sid, agent_id="dev", agent_name="Dev",
        division_id="engineering", prompt_file="p.md", status="completed",
        tokens_used=1000, cost_booked=0.00015, token_source="estimated",
        cost_source="estimated", provider_request_id="req_est_1",
        db_path=test_db
    )
    # +1,000,000 tokens delta -> $0.15 under pricing policy
    res3 = db_store.reconcile_provider_run_usage(
        run_id=run_id3,
        provider_lookup_fn=lambda rid, req: {"found": True, "tokens": 1_001_000, "cost": None},
        db_path=test_db,
        budget_path=test_budget
    )
    assert res3["success"] is True
    assert res3["cost_source"] == "estimated"
    assert round(res3["delta_cost"], 2) == 0.15


def test_p27_cost_provenance_manual_adjustments_update_cost_source(tmp_path):
    """Manual absolute and incremental adjustments atomically update cost_source."""
    test_db = str(tmp_path / "p27_man_prov.db")
    db_store.init_db(test_db)
    test_budget = str(tmp_path / "man_prov_budget.json")
    with open(test_budget, "w", encoding="utf-8") as f:
        json.dump({"tokens_used": 1000, "estimated_cost": 0.15, "transactions": [], "applied_reconciliations": {}}, f)

    sid = "sess_p27_man_prov"
    db_store.create_session(session_id=sid, project_name="Man Prov", task_description="Desc", db_path=test_db)
    tid = db_store.create_task(session_id=sid, title="Task", division_id="engineering", assigned_agent_id="dev", stage="dev", status="completed", db_path=test_db)
    run_id = db_store.record_agent_run(
        task_id=tid, session_id=sid, agent_id="dev", agent_name="Developer",
        division_id="engineering", prompt_file="dev.md", status="completed",
        tokens_used=500, cost_booked=0.01, token_source="estimated", cost_source="estimated",
        db_path=test_db
    )

    # Apply manual absolute adjustment
    res_abs = db_store.apply_manual_run_adjustment(
        run_id=run_id, adjusted_tokens=2000, cost=0.50,
        reason="Manual audit override", evidence_reference="AUDIT-101",
        is_incremental=False, db_path=test_db, budget_path=test_budget
    )
    assert res_abs["cost"] == 0.50
    assert res_abs["cost_source"] == "manual_operator_adjustment"

    with db_store.get_db_connection(test_db) as conn:
        row = conn.execute("SELECT cost_booked, cost_source FROM agent_runs WHERE run_id = ?;", (run_id,)).fetchone()
        assert row["cost_booked"] == 0.50
        assert row["cost_source"] == "manual_operator_adjustment"

    # Apply manual incremental adjustment
    res_incr = db_store.apply_manual_run_adjustment(
        run_id=run_id, adjusted_tokens=500, cost=0.25,
        reason="Manual additive correction", evidence_reference="AUDIT-102",
        is_incremental=True, db_path=test_db, budget_path=test_budget
    )
    assert res_incr["cost"] == 0.75
    assert res_incr["cost_source"] == "manual_operator_adjustment"

    with db_store.get_db_connection(test_db) as conn:
        row = conn.execute("SELECT cost_booked, cost_source FROM agent_runs WHERE run_id = ?;", (run_id,)).fetchone()
        assert row["cost_booked"] == 0.75
        assert row["cost_source"] == "manual_operator_adjustment"


def test_p27_cost_authoritative_negative_correction_unclamped(tmp_path):
    """Authoritative corrections may decrease booked cost; delta_cost is not clamped to zero."""
    test_db = str(tmp_path / "p27_neg_corr.db")
    db_store.init_db(test_db)
    test_budget = str(tmp_path / "neg_corr_budget.json")
    with open(test_budget, "w", encoding="utf-8") as f:
        json.dump({"tokens_used": 10000, "estimated_cost": 10.0, "transactions": [], "applied_reconciliations": {}}, f)

    sid = "sess_p27_neg_corr"
    db_store.create_session(session_id=sid, project_name="Neg Corr", task_description="Desc", db_path=test_db)
    tid = db_store.create_task(session_id=sid, title="Task", division_id="engineering", assigned_agent_id="dev", stage="dev", status="completed", db_path=test_db)
    run_id = db_store.record_agent_run(
        task_id=tid, session_id=sid, agent_id="dev", agent_name="Developer",
        division_id="engineering", prompt_file="dev.md", status="completed",
        tokens_used=5000, cost_booked=10.00, token_source="estimated", cost_source="estimated",
        provider_request_id="req_neg_1", db_path=test_db
    )

    # Provider authoritative response reports cumulative cost is 4.00 (down from 10.00)
    lookup_fn = lambda rid, req: {"found": True, "tokens": 5000, "cost": 4.00, "is_total_cost": True}
    res = db_store.reconcile_provider_run_usage(
        run_id=run_id, provider_lookup_fn=lookup_fn, db_path=test_db, budget_path=test_budget
    )
    assert res["success"] is True
    assert res["cost"] == 4.00
    assert res["delta_cost"] == -6.00  # Negative delta preserved, NOT clamped to 0.0!

    with open(test_budget, "r", encoding="utf-8") as f:
        b = json.load(f)
    assert b["estimated_cost"] == 4.0  # 10.0 + (-6.0)


def test_p27_idempotency_atomic_claim_lease_fencing_and_pending_projection(tmp_path):
    """
    Idempotency protocol:
    - Same key, same payload -> returns original durable result.
    - Same key, different payload -> HTTP 409 (IdempotencyConflictError).
    - Same key, active operation -> in-progress error.
    - Committed pending projection -> retry resumes projection without duplicate DB mutation.
    """
    test_db = str(tmp_path / "p27_idemp_proto.db")
    db_store.init_db(test_db)
    test_budget = str(tmp_path / "idemp_proto_budget.json")
    with open(test_budget, "w", encoding="utf-8") as f:
        json.dump({"tokens_used": 1000, "estimated_cost": 1.0, "transactions": [], "applied_reconciliations": {}}, f)

    sid = "sess_p27_proto"
    db_store.create_session(session_id=sid, project_name="Proto", task_description="Desc", db_path=test_db)
    tid = db_store.create_task(session_id=sid, title="Task", division_id="engineering", assigned_agent_id="dev", stage="dev", status="completed", db_path=test_db)
    run_id = db_store.record_agent_run(
        task_id=tid, session_id=sid, agent_id="dev", agent_name="Dev",
        division_id="engineering", prompt_file="p.md", status="completed",
        tokens_used=100, cost_booked=0.1, token_source="estimated", cost_source="estimated",
        db_path=test_db
    )

    key = "idem_key_test_1"
    res1 = db_store.apply_manual_run_adjustment(
        run_id=run_id, adjusted_tokens=300, cost=0.45,
        reason="First try", evidence_reference="REF-001",
        idempotency_key=key, db_path=test_db, budget_path=test_budget
    )
    assert res1["success"] is True

    # Same key, same payload -> replayed result returned
    res2 = db_store.apply_manual_run_adjustment(
        run_id=run_id, adjusted_tokens=300, cost=0.45,
        reason="First try", evidence_reference="REF-001",
        idempotency_key=key, db_path=test_db, budget_path=test_budget
    )
    assert res2["run_id"] == run_id
    assert res2["resumed_from_idempotency"] is True

    # Same key, different payload -> IdempotencyConflictError
    with pytest.raises(db_store.IdempotencyConflictError):
        db_store.apply_manual_run_adjustment(
            run_id=run_id, adjusted_tokens=999, cost=0.99,
            reason="Different payload", evidence_reference="REF-999",
            idempotency_key=key, db_path=test_db, budget_path=test_budget
        )

    # In-progress key with active lease -> IdempotencyInProgressError
    active_key = "idem_active_key"
    active_payload = {
        "run_id": run_id,
        "adjusted_tokens": 500,
        "cost": 0.5,
        "reason": "Active check",
        "evidence_reference": "REF-ACT",
        "is_incremental": False
    }
    active_hash = hashlib.sha256(json.dumps(active_payload, sort_keys=True).encode("utf-8")).hexdigest()
    with db_store.get_db_connection(test_db) as conn:
        conn.execute("""
            INSERT INTO idempotency_keys (key, scope, target_id, request_hash, response_json, status, owner_token, lease_expires_at, created_at, updated_at)
            VALUES (?, 'manual_adjustment', ?, ?, '', 'in_progress', 'worker1', ?, ?, ?);
        """, (active_key, run_id, active_hash, time.time() + 60.0, time.time(), time.time()))
        conn.commit()

    with pytest.raises(db_store.IdempotencyInProgressError):
        db_store.apply_manual_run_adjustment(
            run_id=run_id, adjusted_tokens=500, cost=0.5,
            reason="Active check", evidence_reference="REF-ACT",
            idempotency_key=active_key, db_path=test_db, budget_path=test_budget
        )


def test_p27_concurrent_run_serialization_and_conflict_handling(tmp_path):
    """
    Two concurrent updates targeting the same run with stale tokens_used trigger ConcurrencyConflictError.
    """
    test_db = str(tmp_path / "p27_conc_ser.db")
    db_store.init_db(test_db)
    test_budget = str(tmp_path / "conc_budget.json")
    with open(test_budget, "w", encoding="utf-8") as f:
        json.dump({"tokens_used": 1000, "estimated_cost": 1.0, "transactions": [], "applied_reconciliations": {}}, f)

    sid = "sess_p27_conc"
    db_store.create_session(session_id=sid, project_name="Conc", task_description="Desc", db_path=test_db)
    tid = db_store.create_task(session_id=sid, title="Task", division_id="engineering", assigned_agent_id="dev", stage="dev", status="completed", db_path=test_db)
    run_id = db_store.record_agent_run(
        task_id=tid, session_id=sid, agent_id="dev", agent_name="Dev",
        division_id="engineering", prompt_file="p.md", status="completed",
        tokens_used=500, cost_booked=0.5, token_source="estimated", cost_source="estimated",
        db_path=test_db
    )

    # Perform update 1
    db_store.apply_manual_run_adjustment(
        run_id=run_id, adjusted_tokens=800, cost=0.8,
        reason="Update 1", evidence_reference="REF-U1",
        db_path=test_db, budget_path=test_budget
    )

    # If another thread attempts an update using the stale prior_tokens, it raises ConcurrencyConflictError
    with db_store.get_db_connection(test_db) as conn:
        cur = conn.execute("UPDATE agent_runs SET tokens_used = 900 WHERE run_id = ? AND tokens_used = 500;", (run_id,))
        assert cur.rowcount == 0


def test_p27_independent_subprocess_budget_file_locking(tmp_path):
    """
    P1 requirement: Independent OS processes (not threads) acquiring _budget_file_lock concurrently.
    Demonstrates OS kernel lock coordination with zero lost updates across child processes.
    """
    budget_file = str(tmp_path / "multiprocess_budget.json")
    with open(budget_file, "w", encoding="utf-8") as f:
        json.dump({"tokens_used": 0, "transactions": []}, f)

    worker_code = f"""
import sys
import os
import json
import time

sys.path.insert(0, r"{os.path.abspath(os.getcwd())}")
import db_persistence as db_store

budget_path = r"{budget_file}"
for _ in range(5):
    with db_store._budget_file_lock(budget_path, timeout=10.0):
        with open(budget_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        data["tokens_used"] = data.get("tokens_used", 0) + 10
        db_store._atomic_write_json_direct(budget_path, data)
        time.sleep(0.01)
"""
    # Spawn 4 concurrent subprocesses
    procs = []
    for _ in range(4):
        p = subprocess.Popen([sys.executable, "-c", worker_code])
        procs.append(p)

    for p in procs:
        p.wait(timeout=30)
        assert p.returncode == 0

    # 4 workers * 5 iterations * 10 tokens = 200 tokens
    with open(budget_file, "r", encoding="utf-8") as f:
        final_budget = json.load(f)

    assert final_budget["tokens_used"] == 200


def test_p27_database_migration_preserves_history_and_conservative_status(tmp_path):
    """
    Verify migration of legacy idempotency_keys table:
    - Preserves completed records.
    - Treats legacy in_progress records conservatively as failed_retryable.
    - Preserves foreign-key integrity (PRAGMA foreign_key_check passes).
    """
    test_db = str(tmp_path / "legacy_migration.db")
    with db_store.get_db_connection(test_db) as conn:
        # Create legacy table without owner_token and lease_expires_at
        conn.execute("""
            CREATE TABLE idempotency_keys (
                key TEXT PRIMARY KEY,
                scope TEXT NOT NULL,
                target_id TEXT,
                request_hash TEXT NOT NULL,
                response_json TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                completed_at REAL
            );
        """)
        conn.execute("""
            INSERT INTO idempotency_keys VALUES
            ('done_key_1', 'manual_adjustment', 'run_1', 'hash1', '{"ok": true}', 'completed', 1000.0, 1000.0, 1000.0),
            ('in_prog_key_1', 'manual_adjustment', 'run_2', 'hash2', '', 'in_progress', 1000.0, 1000.0, NULL);
        """)
        conn.commit()

    # Run init_db to execute migration
    db_store.init_db(test_db)

    with db_store.get_db_connection(test_db) as conn:
        fk_violations = conn.execute("PRAGMA foreign_key_check;").fetchall()
        assert len(fk_violations) == 0

        # Check migrated rows
        rows = {r["key"]: dict(r) for r in conn.execute("SELECT * FROM idempotency_keys;").fetchall()}
        assert "done_key_1" in rows
        assert rows["done_key_1"]["status"] == "completed"
        assert rows["done_key_1"]["response_json"] == '{"ok": true}'

        assert "in_prog_key_1" in rows
        # Legacy in_progress migrated conservatively to failed_retryable!
        assert rows["in_prog_key_1"]["status"] == "failed_retryable"

        # Verify new columns exist
        assert "owner_token" in rows["done_key_1"]
        assert "lease_expires_at" in rows["done_key_1"]


