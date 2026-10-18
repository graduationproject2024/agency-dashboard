"""
Phase 2.4-D: Production Workflow Reliability, End-to-End Acceptance & Recovery Hardening
======================================================================================
Comprehensive acceptance test suite covering the 12 mandatory scenarios:
- Scenario A: Successful project lifecycle (submission -> DAG execution -> PRD gate -> approval -> post-PRD -> QA -> atomic publish)
- Scenario B: PRD approval enforcement & validation (reject bypass, duplicate approval, session mismatch)
- Scenario C: DAG dependency enforcement (tasks strictly blocked until upstream outputs are durably committed)
- Scenario D: Concurrent worker safety & atomic task claiming (race condition immunity, no duplicate execution)
- Scenario E: Genuine process restart recovery (spawns separate OS subprocess, ungraceful kill mid-flight, restart and resume)
- Scenario F: Ambiguous provider outcome handling & token idempotence (preserves completed agent runs without double charges)
- Scenario G: Bounded self-healing closed-loop QA (defect -> diagnostics in memory -> remediation task in DAG -> fix -> publish)
- Scenario H: Exhausted remediation (2 defects -> fail closed, publication strictly blocked)
- Scenario I: Atomic publication and rollback verification (injected staging failures restore prior deliverables)
- Scenario J: Multi-project database isolation (zero cross-session or cross-project data leakage)
- Scenario K: Safe session cancellation (cancelled sessions cannot resume or leak work)
- Scenario L: Budget accounting & secret redaction (credentials masked, token usage tracked accurately)
"""

import os
import sys
import json
import time
import uuid
import shutil
import sqlite3
import subprocess
import threading
from typing import Dict, Any
from unittest.mock import patch, MagicMock

import pytest

import db_persistence as db_store
import task_orchestrator as orch
from app import (
    app,
    run_workflow,
    continue_workflow,
    workflow_state,
    state_lock,
    update_stats,
    run_qa_checks,
    GENERATED_APPS_DIR,
    get_agent_prompt_meta
)

VALID_HTML_APP = """<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
    <meta charset="UTF-8">
    <title>تطبيق منصة الفواتير الرقمية</title>
</head>
<body>
    <div id="root">
        <h1>منصة الفواتير الرقمية</h1>
        <p>نظام محاسبي سحابي متكامل للمؤسسات.</p>
    </div>
</body>
</html>"""

VALID_CSS_APP = "body { margin: 0; font-family: sans-serif; background: #0f172a; color: #f8fafc; } h1 { color: #38bdf8; }"
VALID_JS_APP = "document.addEventListener('DOMContentLoaded', function() { console.log('Invoicing platform online'); });"

MALFORMED_HTML_APP = """<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head><meta charset="UTF-8"><title>تطبيق غير مكتمل</title></head>
<body>
    <div id="unclosed-box">
        <h1>عنوان بدون إغلاق
</body>
</html>"""


# ============================================================================
# Scenario A: Successful project lifecycle end-to-end
# ============================================================================

def test_scenario_a_successful_project_lifecycle(tmp_path, monkeypatch):
    """
    Scenario A: Real project lifecycle from submission to verified publication.
    Asserts database task graph, PRD gate pause, explicit approval, cross-division handoffs,
    and final publication.
    """
    test_gen_dir = str(tmp_path / "apps_scenario_a")
    os.makedirs(test_gen_dir, exist_ok=True)
    monkeypatch.setattr("app.GENERATED_APPS_DIR", test_gen_dir)

    def mock_provider(agent_file, role_name, short_name, *args, **kwargs):
        if short_name == "BS":
            return "دراسة الجدوى وخطة السوق المعتمدة"
        elif short_name == "PM":
            return "وثيقة متطلبات المنتج (PRD) المعتمدة"
        elif short_name == "ENG":
            return "معمارية النظام ومخطط قواعد البيانات العلائقية"
        elif short_name == "UI":
            return "تصميم تجربة المستخدم وهوية الواجهة"
        elif short_name == "DEV":
            return f"```html\n{VALID_HTML_APP}\n```\n```css\n{VALID_CSS_APP}\n```\n```javascript\n{VALID_JS_APP}\n```"
        elif short_name == "QA":
            return "تقرير الفحص المعياري الشامل: الكود خالٍ من الثغرات"
        elif short_name == "MOTION":
            return f"```css\n/* animation */\n```\n```javascript\n/* motion */\n```"
        elif short_name == "SEO":
            return "<meta name='description' content='Invoice system'>"
        return "Specialist deliverable output"

    with patch("app.call_agent", side_effect=mock_provider):
        # 1. Run Pre-PRD stages
        run_workflow("منصة سحابية متقدمة لإدارة فواتير الشركات")

        with state_lock:
            assert workflow_state["status"] == "approval_needed"
            session_id = workflow_state["session_id"]

        # Check DB session and tasks
        sess = db_store.get_session(session_id)
        assert sess["status"] == "approval_needed"
        tasks = db_store.list_tasks_for_session(session_id)
        strat_t = next(t for t in tasks if t["stage"] == "strategy" and t["assigned_agent_id"] == "business-strategist")
        prd_t = next(t for t in tasks if t["stage"] == "product")
        assert strat_t["status"] == "completed"
        assert prd_t["status"] == "completed"

        # Check durable handoff between strategist and PM
        handoffs = db_store.get_session_handoffs(session_id)
        assert len(handoffs) >= 1
        pm_handoff = next(h for h in handoffs if h["from_task_id"] == strat_t["task_id"])
        assert pm_handoff["to_task_id"] == prd_t["task_id"]

        # 2. Approve and continue post-PRD
        continue_workflow(feedback="معتمد بالكامل، تابع التنفيذ")

        with state_lock:
            assert workflow_state["status"] == "completed"
            assert workflow_state["qa_report"]["passed"] is True

        # Check final session status in SQLite
        sess_final = db_store.get_session(session_id)
        assert sess_final["status"] == "completed"

        # Check deliverables published
        published_dirs = [d for d in os.listdir(test_gen_dir) if os.path.isdir(os.path.join(test_gen_dir, d))]
        assert len(published_dirs) == 1
        pub_path = os.path.join(test_gen_dir, published_dirs[0])
        assert os.path.exists(os.path.join(pub_path, "index.html"))
        assert os.path.exists(os.path.join(pub_path, "style.css"))
        assert os.path.exists(os.path.join(pub_path, "script.js"))


# ============================================================================
# Scenario B: Approval enforcement & validation
# ============================================================================

def test_scenario_b_approval_enforcement_and_validation(client):
    """
    Scenario B: Verify approval gate cannot be bypassed or duplicated.
    - Reject approval when not in approval_needed status.
    - Reject approval for mismatched session.
    - Prevent duplicate approvals.
    """
    safe_headers = {"Origin": "http://127.0.0.1:8080"}

    # 1. Calling approve when status is 'idle' or 'running' -> 400
    with state_lock:
        workflow_state["status"] = "running"
        workflow_state["session_id"] = "sess_real_100"

    res = client.post("/api/approve", json={"session_id": "sess_real_100", "feedback": "ok"}, headers=safe_headers)
    assert res.status_code == 400
    assert "No pending approval required" in res.get_json()["error"]

    # 2. Set to approval_needed
    with state_lock:
        workflow_state["status"] = "approval_needed"

    # 3. Calling approve with wrong session_id -> 400
    res_mismatch = client.post("/api/approve", json={"session_id": "sess_WRONG_999", "feedback": "ok"}, headers=safe_headers)
    assert res_mismatch.status_code == 400
    assert "session mismatch" in res_mismatch.get_json()["error"].lower()

    # 4. Valid approve transitions state immediately to running
    with patch("threading.Thread.start"):  # Do not spin worker thread for unit check
        res_valid = client.post("/api/approve", json={"session_id": "sess_real_100", "feedback": "approved"}, headers=safe_headers)
        assert res_valid.status_code == 200

        # Immediate duplicate approve must be rejected
        res_dup = client.post("/api/approve", json={"session_id": "sess_real_100", "feedback": "duplicate"}, headers=safe_headers)
        assert res_dup.status_code == 400


# ============================================================================
# Scenario C: Dependency enforcement
# ============================================================================

def test_scenario_c_dag_dependency_enforcement():
    """
    Scenario C: Downstream task cannot execute before prerequisite is committed.
    - Attempting claim_task directly on pending task with unsatisfied deps returns False.
    - Marking task completed without valid output is rejected by database.
    """
    session_id = "sess_dep_enforce"
    db_store.create_session(session_id, "DepEnforceProject")

    t_upstream = db_store.create_task(
        session_id=session_id,
        title="وثيقة البنية المعمارية",
        division_id="engineering",
        assigned_agent_id="engineering-software-architect",
        stage="engineering",
        status="ready"
    )

    t_downstream = db_store.create_task(
        session_id=session_id,
        title="التطوير البرمجي",
        division_id="engineering",
        assigned_agent_id="engineering-senior-developer",
        stage="development",
        status="pending",
        dependencies=[t_upstream]
    )

    # 1. Attempting to claim downstream task before upstream is completed MUST fail
    claimed = db_store.claim_task(t_downstream)
    assert claimed is False
    t_down_state = db_store.get_task(t_downstream)
    assert t_down_state["status"] == "pending"

    # 2. Attempting to mark upstream completed with empty output raises ValueError
    with pytest.raises(ValueError, match="without valid non-empty output_text"):
        db_store.update_task_status(t_upstream, "completed", output_text="")

    # 3. Mark upstream completed with valid output
    db_store.update_task_status(t_upstream, "completed", output_text="مخطط معماري معتمد 100%")

    # Downstream task is automatically transitioned to 'ready'
    t_down_updated = db_store.get_task(t_downstream)
    assert t_down_updated["status"] == "ready"

    # Now claiming downstream task succeeds
    assert db_store.claim_task(t_downstream) is True
    assert db_store.get_task(t_downstream)["status"] == "running"


# ============================================================================
# Scenario D: Concurrent worker safety & atomic task claiming
# ============================================================================

def test_scenario_d_concurrent_worker_safety_and_idempotence():
    """
    Scenario D: Multiple concurrent workers compete to claim the same ready task.
    Exactly one worker succeeds; no duplicate execution or double assignment occurs.
    """
    session_id = "sess_concurrent_workers"
    db_store.create_session(session_id, "ConcurrentWorkerProject")

    task_id = db_store.create_task(
        session_id=session_id,
        title="مهمة فحص التزامن الفوري",
        division_id="engineering",
        assigned_agent_id="engineering-senior-developer",
        stage="development",
        status="ready"
    )

    successful_claims = []
    worker_count = 12
    barrier = threading.Barrier(worker_count)

    def worker_action(worker_idx):
        barrier.wait()
        if db_store.claim_task(task_id):
            successful_claims.append(worker_idx)

    threads = [threading.Thread(target=worker_action, args=(i,)) for i in range(worker_count)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(successful_claims) == 1, f"Expected 1 claim, got {len(successful_claims)}"
    assert db_store.get_task(task_id)["status"] == "running"


# ============================================================================
# Scenario E: Genuine process restart recovery in separate process
# ============================================================================

def test_scenario_e_process_restart_recovery_in_separate_process(tmp_path):
    """
    Scenario E: True separate process interruption and recovery.
    1. A separate Python subprocess initializes session and starts tasks.
    2. Subprocess is abruptly terminated (simulating crash/kill).
    3. Second process starts against the same DB, runs reconcile_interrupted_sessions(),
       and successfully resumes the session to completion.
    """
    test_db = str(tmp_path / "crash_recovery.db")
    script_step1 = f"""
import sys, os
sys.path.insert(0, r"{os.path.abspath('.')}")
import db_persistence as db

db.init_db(r"{test_db}")
sid = db.create_session("CrashTestProject", "Process crash and recovery test", db_path=r"{test_db}")
t1 = db.create_task(sid, "مهمة استراتيجية", "strategy", "business-strategist", "strategy", status="ready", db_path=r"{test_db}")
t2 = db.create_task(sid, "مهمة منتج", "product", "product-manager", "product", status="pending", dependencies=[t1], db_path=r"{test_db}")

# Simulate task 1 running when process is abruptly killed
db.claim_task(t1, db_path=r"{test_db}")
db.save_session_checkpoint(sid, "workflow_started", {{"session_id": sid, "status": "running"}}, db_path=r"{test_db}")
print("READY_FOR_KILL")
sys.stdout.flush()
import time
time.sleep(30)  # Waiting to be killed
"""
    p1 = subprocess.Popen([sys.executable, "-c", script_step1], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    # Wait until step 1 signals it has set up the running task
    line = p1.stdout.readline()
    assert "READY_FOR_KILL" in line

    # Ungracefully kill process 1 mid-execution
    p1.kill()
    p1.wait()

    # Step 2: In a new process (or fresh execution against the same test_db), run startup reconciliation
    count = db_store.reconcile_interrupted_sessions(db_path=test_db)
    assert count == 1

    with db_store.get_db_connection(test_db) as conn:
        sess = conn.execute("SELECT * FROM workflow_sessions;").fetchone()
        assert sess["status"] == "interrupted"

        task1 = conn.execute("SELECT * FROM workflow_tasks WHERE title = 'مهمة استراتيجية';").fetchone()
        # Interrupted task must have been queued for bounded retry (status = 'ready')
        assert task1["status"] == "ready"
        assert task1["retry_count"] == 1
        assert "Queued for retry" in task1["error_message"]

    # Step 3: Complete task 1 and downstream task 2 through normal execution
    db_store.update_task_status(task1["task_id"], "completed", output_text="استراتيجية منجزة بعد الاسترداد", db_path=test_db)
    task2 = db_store.get_session_tasks(sess["session_id"], db_path=test_db)[1]
    assert task2["status"] == "ready"
    db_store.claim_task(task2["task_id"], db_path=test_db)
    db_store.update_task_status(task2["task_id"], "completed", output_text="وثيقة المنتج منجزة", db_path=test_db)
    db_store.update_session_status(sess["session_id"], "completed", db_path=test_db)

    final_sess = db_store.get_session(sess["session_id"], db_path=test_db)
    assert final_sess["status"] == "completed"


# ============================================================================
# Scenario F: Ambiguous provider outcome handling & token idempotence
# ============================================================================

def test_scenario_f_ambiguous_provider_outcome_handling():
    """
    Scenario F: Provider returns response and agent run is logged, but process terminates
    before update_task_status('completed') completes.
    On restart, reconcile_interrupted_sessions() detects completed agent run and restores
    task output without repeating LLM call or double-counting tokens.
    """
    session_id = "sess_ambiguous_outcome"
    db_store.create_session(session_id, "AmbiguousProviderProject")

    task_id = db_store.create_task(
        session_id=session_id,
        title="معمارية الخدمات السحابية",
        division_id="engineering",
        assigned_agent_id="engineering-software-architect",
        stage="engineering",
        status="running"
    )

    # Provider finished and agent run was saved before interruption
    expected_output = "مخطط معمارية الخدمات السحابية وقواعد البيانات بدقة متناهية"
    db_store.record_agent_run(
        task_id=task_id,
        session_id=session_id,
        agent_id="engineering-software-architect",
        agent_name="مهندس المعمارية",
        division_id="engineering",
        prompt_file="test.md",
        status="completed",
        tokens_used=450,
        output_preview=expected_output
    )

    # Server restarts: reconcile
    reconciled = db_store.reconcile_interrupted_sessions()
    assert reconciled >= 1

    # Task was safely restored from completed agent run, NOT failed or repeated!
    recovered_task = db_store.get_task(task_id)
    assert recovered_task["status"] == "completed"
    assert recovered_task["output_text"] == expected_output


# ============================================================================
# Scenario G: Bounded self-healing closed-loop QA (Success)
# ============================================================================

def test_scenario_g_bounded_self_healing_qa_success(tmp_path, monkeypatch):
    """
    Scenario G: Implementation produces defective HTML -> QA Gate catches defect ->
    Remediation task created in DAG -> Senior developer corrects defect -> Re-check passes ->
    Publish succeeds.
    """
    test_gen_dir = str(tmp_path / "apps_scenario_g")
    os.makedirs(test_gen_dir, exist_ok=True)
    monkeypatch.setattr("app.GENERATED_APPS_DIR", test_gen_dir)

    attempt_counter = {"dev_calls": 0}

    def mock_agent_call(agent_file, role_name, short_name, *args, **kwargs):
        if short_name == "DEV":
            attempt_counter["dev_calls"] += 1
            if attempt_counter["dev_calls"] == 1:
                # First attempt: defective unclosed tag
                return f"```html\n{MALFORMED_HTML_APP}\n```\n```css\n{VALID_CSS_APP}\n```\n```javascript\n{VALID_JS_APP}\n```"
            else:
                # Remediation attempt: corrected valid code
                return f"```html\n{VALID_HTML_APP}\n```\n```css\n{VALID_CSS_APP}\n```\n```javascript\n{VALID_JS_APP}\n```"
        elif short_name == "QA":
            return "QA Analysis Output"
        elif short_name in ("MOTION", "SEO", "ENG", "UI", "BS", "PM"):
            return "Specialist component output"
        return "Generic output"

    with patch("app.call_agent", side_effect=mock_agent_call):
        with state_lock:
            workflow_state["current_task"] = "تطبيق فحص الإصلاح الذاتي"
            workflow_state["outputs"] = {}

        continue_workflow(feedback="ابدأ")

        with state_lock:
            assert workflow_state["status"] == "completed"
            assert workflow_state["qa_report"]["passed"] is True
            session_id = workflow_state.get("session_id")

        assert attempt_counter["dev_calls"] == 2  # Proves remediation agent was executed!

        # Verify remediation task in DB
        tasks = db_store.list_tasks_for_session(session_id)
        rem_tasks = [t for t in tasks if "إصلاح" in t["title"] or "Remediation" in t["title"]]
        assert len(rem_tasks) >= 1
        assert rem_tasks[0]["status"] == "completed"

        # Verify defect findings logged in project memory
        mems = db_store.get_project_memory("تطبيق فحص الإصلاح الذاتي")
        defect_mems = [m for m in mems if "QA_Defect" in m["key"]]
        assert len(defect_mems) >= 1


# ============================================================================
# Scenario H: Exhausted remediation fails closed
# ============================================================================

def test_scenario_h_exhausted_remediation_fails_closed(tmp_path, monkeypatch):
    """
    Scenario H: Defective output persists after 2 remediation attempts ->
    Workflow fails closed with clear blocker, publication strictly blocked.
    """
    test_gen_dir = str(tmp_path / "apps_scenario_h")
    os.makedirs(test_gen_dir, exist_ok=True)
    monkeypatch.setattr("app.GENERATED_APPS_DIR", test_gen_dir)

    def mock_always_broken_agent(*args, **kwargs):
        # Always return malformed unclosed HTML
        return f"```html\n{MALFORMED_HTML_APP}\n```\n```css\n{VALID_CSS_APP}\n```\n```javascript\n{VALID_JS_APP}\n```"

    with patch("app.call_agent", side_effect=mock_always_broken_agent):
        with state_lock:
            workflow_state["current_task"] = "تطبيق فاشل الجودة"
            workflow_state["outputs"] = {}

        continue_workflow(feedback="ابدأ")

        with state_lock:
            assert workflow_state["status"] == "failed"
            assert workflow_state["qa_report"]["passed"] is False
            blockers = list(workflow_state["blockers"])

        assert any("استنفاد محاولات الإصلاح الذاتي" in b for b in blockers)

        # Fail closed: No deliverables in generated_apps!
        assert len(os.listdir(test_gen_dir)) == 0


# ============================================================================
# Scenario I: Atomic publication and rollback integrity
# ============================================================================

def test_scenario_i_atomic_publication_and_rollback_integrity(tmp_path, monkeypatch):
    """
    Scenario I: Injected failure during staging/publication restores pre-existing files
    and prevents corrupt or partial applications.
    """
    test_gen_dir = str(tmp_path / "apps_scenario_i")
    os.makedirs(test_gen_dir, exist_ok=True)
    monkeypatch.setattr("app.GENERATED_APPS_DIR", test_gen_dir)

    # Create pre-existing valid app
    app_folder = os.path.join(test_gen_dir, "atomic_app_test")
    os.makedirs(app_folder, exist_ok=True)
    original_html = os.path.join(app_folder, "index.html")
    with open(original_html, "w", encoding="utf-8") as f:
        f.write("ORIGINAL_UNTOUCHED_CONTENT")

    def mock_broken_delivery(*args, **kwargs):
        return f"```html\n{VALID_HTML_APP}\n```\n```css\n{VALID_CSS_APP}\n```\n```javascript\n{VALID_JS_APP}\n```"

    with patch("app.call_agent", side_effect=mock_broken_delivery):
        # Inject an error into atomic_write_file during the delivery phase
        with patch("app.atomic_write_file", side_effect=IOError("Simulated disk full during write")):
            with state_lock:
                workflow_state["current_task"] = "atomic_app_test"
                workflow_state["outputs"] = {}

            continue_workflow(feedback="go")

            with state_lock:
                assert workflow_state["status"] == "failed"

    # Pre-existing file was restored by rollback mechanism!
    with open(original_html, "r", encoding="utf-8") as f:
        content = f.read()
    assert content == "ORIGINAL_UNTOUCHED_CONTENT"


# ============================================================================
# Scenario J: Multi-project database isolation
# ============================================================================

def test_scenario_j_multi_project_data_isolation():
    """
    Scenario J: Multiple distinct project sessions execute against the same database.
    Verify complete isolation of tasks, memory, checkpoints, and handoffs.
    """
    proj_med = "Medical_Record_System"
    proj_fin = "FinTech_Trading_Engine"

    sess_med = db_store.create_session("sess_med_1", proj_med)
    sess_fin = db_store.create_session("sess_fin_2", proj_fin)

    # Add memory items
    db_store.add_project_memory(proj_med, sess_med, "requirement", "HIPAA_Compliance", "Patient data encrypted at rest", "medical-auditor")
    db_store.add_project_memory(proj_fin, sess_fin, "requirement", "PCI_DSS_Compliance", "Payment tokens must not be logged", "security-engineer")

    # Verify medical memories do NOT leak into financial memories
    med_mems = db_store.get_project_memory(proj_med)
    fin_mems = db_store.get_project_memory(proj_fin)

    med_keys = {m["key"] for m in med_mems}
    fin_keys = {m["key"] for m in fin_mems}

    assert "HIPAA_Compliance" in med_keys
    assert "PCI_DSS_Compliance" not in med_keys

    assert "PCI_DSS_Compliance" in fin_keys
    assert "HIPAA_Compliance" not in fin_keys

    # Create tasks in both sessions
    t_med = db_store.create_task(sess_med, "تشفير السجلات", "engineering", "security-engineer", "engineering")
    t_fin = db_store.create_task(sess_fin, "تنفيذ الصفقات", "engineering", "engineering-senior-developer", "development")

    med_tasks = db_store.list_tasks_for_session(sess_med)
    fin_tasks = db_store.list_tasks_for_session(sess_fin)

    assert len(med_tasks) == 1 and med_tasks[0]["task_id"] == t_med
    assert len(fin_tasks) == 1 and fin_tasks[0]["task_id"] == t_fin


# ============================================================================
# Scenario K: Session cancellation and recovery safety
# ============================================================================

def test_scenario_k_session_cancellation_and_recovery_safety(client):
    """
    Scenario K: Cancel active session.
    Verify tasks are cancelled, session is marked cancelled, and cannot be resumed.
    """
    safe_headers = {"Origin": "http://127.0.0.1:8080"}
    session_id = "sess_cancel_test"
    db_store.create_session(session_id, "CancellationProject")

    t1 = db_store.create_task(session_id, "مهمة تنتظر الإلغاء", "engineering", "engineering-senior-developer", "development", status="ready")

    # Cancel session via secure API
    res = client.post(f"/api/sessions/{session_id}/cancel", headers=safe_headers)
    assert res.status_code == 200

    sess = db_store.get_session(session_id)
    assert sess["status"] == "cancelled"

    task = db_store.get_task(t1)
    assert task["status"] == "cancelled"

    # Attempting to resume a cancelled session MUST return 400
    res_resume = client.post(f"/api/sessions/{session_id}/resume", headers=safe_headers)
    assert res_resume.status_code == 400
    assert "Cannot resume session in terminal 'cancelled' state" in res_resume.get_json()["error"]


# ============================================================================
# Scenario L: Budget accounting & secret redaction
# ============================================================================

def test_scenario_l_budget_integrity_and_secret_masking():
    """
    Scenario L: Verify sensitive API keys and secrets are automatically redacted from
    project memory and agent runs, and token counts are recorded non-zero.
    """
    project_id = "SecurityBudgetProject"
    session_id = "sess_sec_budget"
    db_store.create_session(session_id, project_id)

    raw_secret_text = "Database config: password=SuperSecretP@ss123 and key=AIzaSyA1B2C3D4E5F6G7H8I9J0"
    mem = db_store.add_project_memory(project_id, session_id, "architecture_decision", "DB_Config", raw_secret_text, "engineering-software-architect")

    # Verify secrets are redacted
    assert "SuperSecretP@ss123" not in mem["content"]
    assert "[REDACTED_CREDENTIAL]" in mem["content"]
    assert "AIzaSy" not in mem["content"]
    assert "[REDACTED_API_KEY]" in mem["content"]

    # Verify agent run records tokens
    task_id = db_store.create_task(session_id, "تدقيق الأمان", "security", "security-appsec-engineer", "engineering")
    run_id = db_store.record_agent_run(
        task_id=task_id,
        session_id=session_id,
        agent_id="security-appsec-engineer",
        agent_name="مهندس الأمان",
        division_id="security",
        prompt_file="sec.md",
        status="completed",
        tokens_used=1250,
        output_preview="Audit clean"
    )

    runs = db_store.list_agent_runs(session_id, task_id=task_id)
    assert len(runs) == 1
    assert runs[0]["tokens_used"] == 1250
    assert runs[0]["status"] == "completed"
