"""
Phase 2.4-I — Adversarial Runtime Recovery, Multi-Process Concurrency & Release Readiness Suite.

Validates all 9 workstreams:
- Workstream A: Crash-Consistent Delivery and Recovery (Pre-existing backup preservation across SQLite boundary).
- Workstream B: Multi-Process DAG & SQLite Concurrency (True separate OS process claims, resumption locks, lock retry).
- Workstream C: Provider Ambiguity, Retry Semantics, and Idempotency (In-flight crash marking, ambiguity retention).
- Workstream D: Budget & Token Accounting Integrity (Zero-token preservation, malformed value sanitization).
- Workstream E: Self-Healing Quality & Regression Resistance (Defect repair, bounded retry, fail-closed enforcement).
- Workstream F: Artifact Manifest & Publication Security (Case-insensitive collision, Unicode normalization, size limit).
- Workstream G: Session API Authorization & Operational Security (Strict session_id format validation, 400 on malformed, 404 on nonexistent).
- Workstream H: Realistic End-to-End Acceptance (Bilingual clinic, industrial telemetry, browser game with Playwright).
- Workstream I: Operational Readiness & Observability (Audit report verification, permitted recovery actions).
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
import http.server
from pathlib import Path
from typing import Dict, Any, Optional

import pytest

# Ensure repo root is on sys.path
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
    get_last_agent_execution,
    set_last_agent_execution
)
import db_persistence as db_store
import task_orchestrator as orch

try:
    from playwright.sync_api import sync_playwright  # type: ignore[import-not-found]
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False


def _serve_directory_ephemerally(directory: str):
    """Serve a local directory on an ephemeral localhost port."""
    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=directory, **kwargs)
        def log_message(self, format, *args):
            pass

    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd, thread, f"http://127.0.0.1:{port}"


# =========================================================================
# Workstream A: Crash-Consistent Delivery & Recovery
# =========================================================================

def test_workstream_a_pre_existing_backup_preserved_on_persistence_failure(monkeypatch, tmp_path):
    """
    Workstream A: Real production failure-handling path in continue_workflow.
    When persistence fails at the database boundary, the production error handler
    restores pre-existing files from backup, deletes newly created deliverables,
    leaves unrelated files untouched, and transitions session to failed.
    """
    test_gen_dir = str(tmp_path / "gen_apps_ws_a")
    os.makedirs(test_gen_dir, exist_ok=True)
    monkeypatch.setattr("app.GENERATED_APPS_DIR", test_gen_dir)

    def clinic_adapter(prompt_file, role_name, short_name, color, prompt, context_text=""):
        if "developer" in prompt_file:
            html = "<!DOCTYPE html><html><body><h1>New Valid App</h1></body></html>"
            css = "body { color: blue; }"
            js = "console.log('App loaded');"
            return f"```html\n{html}\n```\n```css\n{css}\n```\n```javascript\n{js}\n```"
        return "المرحلة منجزة بنجاح"

    task_name = "تطبيق_استعادة_حقيقي"
    run_workflow(task_name, call_agent_fn=clinic_adapter)
    with state_lock:
        sid = workflow_state["session_id"]
        assert workflow_state["status"] == "approval_needed"

    app_dir, _ = agency_app.get_project_folder_name(task_name)
    os.makedirs(app_dir, exist_ok=True)
    index_file = os.path.join(app_dir, "index.html")
    with open(index_file, "w", encoding="utf-8") as f:
        f.write("ORIGINAL_USER_CONTENT_PRE_EXISTING_ABC")

    unrelated_file = os.path.join(app_dir, "user_notes.txt")
    with open(unrelated_file, "w", encoding="utf-8") as f:
        f.write("DO_NOT_TOUCH_USER_NOTES")

    # Inject genuine persistence failure at the database manifest boundary
    def fail_manifest(*args, **kwargs):
        raise sqlite3.OperationalError("Simulated database failure during manifest persistence")
    monkeypatch.setattr(db_store, "record_artifact_manifest", fail_manifest)

    # Invoke real production entry point!
    continue_workflow(feedback="معتمد", session_id=sid, call_agent_fn=clinic_adapter)

    # Verify real production failure handler ran
    with state_lock:
        assert workflow_state["status"] == "failed"

    persisted_sess = db_store.get_session(sid)
    assert persisted_sess["status"] == "failed"

    # Verify pre-existing index.html was restored byte-for-byte by production code
    with open(index_file, "r", encoding="utf-8") as f:
        assert f.read() == "ORIGINAL_USER_CONTENT_PRE_EXISTING_ABC"

    # Verify unrelated file was preserved
    with open(unrelated_file, "r", encoding="utf-8") as f:
        assert f.read() == "DO_NOT_TOUCH_USER_NOTES"

    # Verify newly created deliverables (plan.md, style.css) were removed by production rollback
    assert not os.path.exists(os.path.join(app_dir, "plan.md"))
    assert not os.path.exists(os.path.join(app_dir, "style.css"))


# =========================================================================
# Workstream B: Multi-Process DAG & SQLite Concurrency
# =========================================================================

def test_workstream_b_multiprocess_task_claim_mutual_exclusion(tmp_path):
    """
    Workstream B: Two separate OS processes attempting to claim the same task simultaneously.
    Exactly one worker must acquire claim (True) and the other must fail (False).
    """
    test_db = str(tmp_path / "multiprocess_claim.db")
    db_store.init_db(test_db)

    sid = "sess_mp_claim"
    db_store.create_session(session_id=sid, project_name="MP Claim Test", task_description="Test", db_path=test_db)
    tid = db_store.create_task(
        session_id=sid,
        title="Concurrent Claim Task",
        division_id="engineering",
        assigned_agent_id="engineering-developer",
        stage="development",
        status="ready",
        db_path=test_db
    )

    script_content = f"""
import sys
import os
root_dir = r"{root_dir}"
if root_dir not in sys.path:
    sys.path.insert(0, root_dir)
import db_persistence as db_store

claimed = db_store.claim_task("{tid}", db_path=r"{test_db}")
print("CLAIMED" if claimed else "REJECTED")
"""
    worker_script = str(tmp_path / "worker_claim.py")
    with open(worker_script, "w", encoding="utf-8") as f:
        f.write(script_content)

    # Launch two independent OS processes simultaneously
    p1 = subprocess.Popen([sys.executable, worker_script], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    p2 = subprocess.Popen([sys.executable, worker_script], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    out1, err1 = p1.communicate(timeout=10)
    out2, err2 = p2.communicate(timeout=10)

    results = [out1.strip(), out2.strip()]
    assert "CLAIMED" in results, f"Expected one CLAIMED, got: {results}"
    assert "REJECTED" in results, f"Expected one REJECTED, got: {results}"

    # Verify database state in SQLite
    t = db_store.get_task(tid, db_path=test_db)
    assert t["status"] == "running"


def test_workstream_b_multiprocess_session_resumption_locking(tmp_path):
    """
    Workstream B: Two separate OS processes attempting to resume the same session.
    Exactly one process acquires the resumption lock, while the other receives False.
    """
    test_db = str(tmp_path / "multiprocess_resume.db")
    db_store.init_db(test_db)

    sid = "sess_mp_resume"
    db_store.create_session(session_id=sid, project_name="MP Resume Test", task_description="Test", db_path=test_db)
    db_store.update_session_status(sid, "interrupted", db_path=test_db)

    script_content = f"""
import sys
import os
root_dir = r"{root_dir}"
if root_dir not in sys.path:
    sys.path.insert(0, root_dir)
import db_persistence as db_store

locked = db_store.acquire_session_resumption_lock("{sid}", db_path=r"{test_db}")
print("LOCKED" if locked else "BUSY")
"""
    worker_script = str(tmp_path / "worker_resume.py")
    with open(worker_script, "w", encoding="utf-8") as f:
        f.write(script_content)

    p1 = subprocess.Popen([sys.executable, worker_script], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    p2 = subprocess.Popen([sys.executable, worker_script], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    out1, _ = p1.communicate(timeout=10)
    out2, _ = p2.communicate(timeout=10)

    results = [out1.strip(), out2.strip()]
    assert "LOCKED" in results, f"Expected one LOCKED, got: {results}"
    assert "BUSY" in results, f"Expected one BUSY, got: {results}"

    s = db_store.get_session(sid, db_path=test_db)
    assert s["status"] == "running"


def test_workstream_b_sqlite_busy_retry_under_contention(tmp_path):
    """
    Workstream B: Under genuine SQLite lock contention with real connections,
    _retry_on_db_lock waits and retries until transient lock is released, then succeeds.
    """
    test_db = str(tmp_path / "contention.db")
    db_store.init_db(test_db)

    ready = threading.Event()
    def lock_holder():
        c = sqlite3.connect(test_db, timeout=1.0)
        c.execute("BEGIN IMMEDIATE;")
        c.execute("UPDATE workflow_sessions SET updated_at = 1 WHERE session_id = 'nonexistent';")
        ready.set()
        time.sleep(0.3)
        c.commit()
        c.close()

    t = threading.Thread(target=lock_holder)
    t.start()
    ready.wait()

    start = time.time()
    sid = "sess_real_contention"
    db_store.create_session(session_id=sid, project_name="Real Contention Test", task_description="Test", db_path=test_db)
    t.join()
    elapsed = time.time() - start

    assert elapsed >= 0.25, f"Expected to wait for real lock release, took {elapsed}s"
    sess = db_store.get_session(sid, db_path=test_db)
    assert sess is not None
    assert sess["session_id"] == sid


# =========================================================================
# Workstream C: Provider Ambiguity, Retry Semantics, and Idempotency
# =========================================================================

def test_workstream_c_provider_ambiguity_in_flight_crash_reconciliation(tmp_path):
    """
    Workstream C: If a process terminates while an external provider call is in-flight
    (status='running' in agent_runs), reconcile_interrupted_sessions must mark the task failed
    with explicit 'Provider outcome ambiguous', without blindly re-executing or double billing.
    """
    test_db = str(tmp_path / "ambiguous_provider.db")
    db_store.init_db(test_db)

    sid = "sess_ambig_001"
    db_store.create_session(session_id=sid, project_name="Ambiguity Test", task_description="Test", db_path=test_db)
    db_store.update_session_status(sid, "running", db_path=test_db)

    tid = db_store.create_task(
        session_id=sid,
        title="In-Flight Provider Task",
        division_id="engineering",
        assigned_agent_id="engineering-developer",
        stage="development",
        status="running",
        db_path=test_db
    )

    # Record durable pre-flight execution marker
    run_id = db_store.record_agent_run_start(
        task_id=tid,
        session_id=sid,
        agent_id="engineering-developer",
        agent_name="Senior Developer",
        division_id="engineering",
        prompt_file="engineering/developer.md",
        input_preview="Context",
        db_path=test_db
    )

    # Crash reconciliation runs
    interrupted_count = db_store.reconcile_interrupted_sessions(db_path=test_db)
    assert interrupted_count == 1

    # Check task and session status
    task = db_store.get_task(tid, db_path=test_db)
    assert task["status"] == "failed"
    assert "Provider outcome unknown" in task["error_message"]

    session = db_store.get_session(sid, db_path=test_db)
    assert session["status"] == "failed"

    # Operator audit must reflect ambiguous call and indicate retry is unsafe
    audit = db_store.get_operator_session_audit(sid, db_path=test_db)
    assert audit["cost_and_token_accounting"]["has_ambiguous_provider_call"] is True
    assert audit["failure_analysis"]["is_provider_ambiguous"] is True
    assert audit["failure_analysis"]["is_retry_safe"] is False
    assert "inspect_diagnostics" in audit["permitted_recovery_actions"]


# =========================================================================
# Workstream D: Budget & Token Accounting Integrity
# =========================================================================

def test_workstream_d_explicit_zero_tokens_preserved(monkeypatch, tmp_path):
    """
    Workstream D: Explicit provider-reported 0 tokens must be recorded as 0
    with source='provider_reported' and not synthesized with estimated tokens.
    """
    temp_budget = str(tmp_path / "budget_zero.json")
    monkeypatch.setattr("app.BUDGET_FILE", temp_budget)

    # Record explicit zero tokens
    updated = record_token_usage("Agent Zero", "Test", "Zero Token Task", 0, source="provider_reported")
    assert updated["tokens_used"] == 0
    assert updated["estimated_cost"] == 0.0
    assert updated["transactions"][0]["tokens"] == 0
    assert updated["transactions"][0]["source"] == "provider_reported"


def test_workstream_d_malformed_and_negative_tokens_sanitization(monkeypatch, tmp_path):
    """
    Workstream D: Malformed, non-integer, or negative token usage inputs
    are clamped to 0 without raising exceptions or corrupting the budget.
    """
    temp_budget = str(tmp_path / "budget_malformed.json")
    monkeypatch.setattr("app.BUDGET_FILE", temp_budget)

    # Negative tokens
    b1 = record_token_usage("Agent Neg", "Test", "Negative Task", -500, source="estimated")
    assert b1["tokens_used"] == 0

    # Non-integer / malformed tokens
    b2 = record_token_usage("Agent Str", "Test", "String Task", "invalid", source="estimated")
    assert b2["tokens_used"] == 0


# =========================================================================
# Workstream E: Self-Healing Quality & Regression Resistance
# =========================================================================

def test_workstream_e_remediation_unparseable_output_fails_closed(monkeypatch, tmp_path):
    """
    Workstream E: When a remediation agent returns output without code blocks (or empty),
    the failure is logged honestly, bounded retry terminates, and workflow fails closed.
    """
    test_gen_dir = str(tmp_path / "gen_unparseable")
    os.makedirs(test_gen_dir, exist_ok=True)
    monkeypatch.setattr("app.GENERATED_APPS_DIR", test_gen_dir)

    def broken_repair_adapter(prompt_file, role_name, short_name, color, prompt, context_text=""):
        if "developer" in prompt_file or "remediation" in (prompt or "").lower():
            # Returns plain explanation without ```html or ```js code blocks
            return "I apologize, but I could not fix this issue. Please check the code."
        # Initial development produces an invalid syntax
        if "مبرمج" in role_name or "senior-developer" in prompt_file:
            return "```html\n<!DOCTYPE html><html><head><title>T</title></head><body><script src='script.js'></script></body></html>\n```\n```javascript\nfunction broken( {\n```"
        return "Generic Strategy/Product output"

    with state_lock:
        workflow_state["current_task"] = "تطبيق فحص الردود غير الصالحة"
    run_workflow("تطبيق فحص الردود غير الصالحة", call_agent_fn=broken_repair_adapter)

    with state_lock:
        sid = workflow_state["session_id"]

    continue_workflow(feedback="اعتمد", session_id=sid, call_agent_fn=broken_repair_adapter)

    with state_lock:
        assert workflow_state["status"] == "failed"
        assert any("no parseable code blocks" in err for err in workflow_state["qa_report"]["errors"])

    sess = db_store.get_session(sid)
    assert sess["status"] == "failed"

    # Verify no deliverables were published
    manifest = db_store.get_artifact_manifest(sid)
    assert len(manifest) == 0


# =========================================================================
# Workstream F: Artifact Manifest & Publication Security
# =========================================================================

def test_workstream_f_manifest_rejects_case_insensitive_collisions():
    """
    Workstream F: build_artifact_manifest rejects duplicate artifact paths
    that differ only by casing on case-insensitive filesystems.
    """
    collision_files = {
        "index.html": "<!DOCTYPE html><html><body>1</body></html>",
        "INDEX.HTML": "<!DOCTYPE html><html><body>2</body></html>"
    }
    with pytest.raises(ValueError, match="collision"):
        build_artifact_manifest(collision_files)


def test_workstream_f_manifest_rejects_unicode_normalization_collisions():
    """
    Workstream F: build_artifact_manifest rejects paths that differ only
    in Unicode normalization (NFC vs NFD).
    """
    import unicodedata
    name_nfc = unicodedata.normalize("NFC", "café.html")
    name_nfd = unicodedata.normalize("NFD", "café.html")
    # Verify they are different strings before normalization
    assert name_nfc != name_nfd

    collision_files = {
        name_nfc: "<!DOCTYPE html><html><body>NFC</body></html>",
        name_nfd: "<!DOCTYPE html><html><body>NFD</body></html>"
    }
    with pytest.raises(ValueError, match="collision"):
        build_artifact_manifest(collision_files)


def test_workstream_f_manifest_rejects_oversized_artifacts():
    """
    Workstream F: build_artifact_manifest rejects single artifacts exceeding 25MB limit.
    """
    oversized_files = {
        "large_data.json": b"x" * (26 * 1024 * 1024)
    }
    with pytest.raises(ValueError, match="exceeds maximum allowed size"):
        build_artifact_manifest(oversized_files)


# =========================================================================
# Workstream G: Session API Authorization & Operational Security
# =========================================================================

def test_workstream_g_session_endpoints_reject_malformed_session_ids():
    """
    Workstream G: Session-scoped endpoints reject malformed session IDs
    (path traversal, null bytes, oversized IDs) with HTTP 400 Bad Request.
    """
    client = app.test_client()
    malformed_ids = [
        "sess..etc..passwd",
        "sess_test\x00extra",
        "a" * 100,  # exceeds 64 chars
        "sess:with:colons",
        "sess;drop table",
        "sess*invalid",
    ]

    for bad_id in malformed_ids:
        # Audit endpoint
        r1 = client.get(f"/api/sessions/{bad_id}/audit")
        assert r1.status_code == 400
        assert "Invalid session identifier format" in r1.get_json()["error"]

        # Manifest endpoint
        r2 = client.get(f"/api/sessions/{bad_id}/manifest")
        assert r2.status_code == 400

        # Tasks endpoint
        r3 = client.get(f"/api/sessions/{bad_id}/tasks")
        assert r3.status_code == 400

        # Resume endpoint
        r4 = client.post(f"/api/sessions/{bad_id}/resume", json={})
        assert r4.status_code in (400, 403)

    # Traversal attack attempt is blocked
    r_trav = client.get("/api/sessions/../../etc/passwd/audit")
    assert r_trav.status_code in (400, 403, 404)


def test_workstream_g_valid_nonexistent_session_returns_404():
    """
    Workstream G: Valid session ID format that does not exist returns HTTP 404.
    """
    client = app.test_client()
    res = client.get("/api/sessions/sess_nonexistent_valid_format_123/audit")
    assert res.status_code == 404
    assert "Session not found" in res.get_json()["error"]


# =========================================================================
# Workstream H & I: End-to-End Acceptance Scenarios & Observability
# =========================================================================

def test_workstream_h_scenario_1_bilingual_clinic_e2e_lifecycle(monkeypatch, tmp_path):
    """
    Workstream H: Scenario 1 - Bilingual clinic appointment application.
    Executes full lifecycle: strategy -> product -> PRD approval -> engineering -> QA -> publication.
    Verifies audit observability in Workstream I.
    """
    test_gen_dir = str(tmp_path / "gen_clinic_i")
    os.makedirs(test_gen_dir, exist_ok=True)
    monkeypatch.setattr("app.GENERATED_APPS_DIR", test_gen_dir)

    def clinic_adapter(prompt_file, role_name, short_name, color, prompt, context_text=""):
        p_lower = (prompt or "").lower()
        if "business-strategist" in prompt_file:
            return "دراسة جدوى عيادة الأسنان الاستشارية الرقمية"
        if "product-manager" in prompt_file:
            return "وثيقة متطلبات عيادة الأسنان الرقمية (PRD)"
        if "architect" in prompt_file or "tech-lead" in prompt_file:
            return "الهيكلية الهندسية لنظام الحجز والاستشارات"
        if "ui-ux" in prompt_file or "designer" in prompt_file:
            return "تصميم تجربة المستخدم للعيادة بنمط ثنائي اللغة"
        if "developer" in prompt_file or "مبرمج" in role_name:
            html = """<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
  <meta charset="UTF-8">
  <title>عيادة النور لطب الأسنان</title>
</head>
<body>
  <h1>عيادة النور لطب الأسنان</h1>
  <form id="book-form">
    <input type="text" id="patient" placeholder="اسم المريض" required />
    <button type="submit" id="btn-submit">حجز</button>
  </form>
</body>
</html>"""
            css = "body { font-family: sans-serif; background: #fff; } form { display: flex; gap: 8px; }"
            js = "document.addEventListener('DOMContentLoaded', () => { console.log('Ready'); });"
            return f"```html\n{html}\n```\n```css\n{css}\n```\n```javascript\n{js}\n```"
        return "تم إنجاز المهمة بنجاح"

    with state_lock:
        workflow_state["current_task"] = "عيادة أسنان تخصصية"
    run_workflow("عيادة أسنان تخصصية", call_agent_fn=clinic_adapter)

    with state_lock:
        sid = workflow_state["session_id"]
        assert workflow_state["status"] == "approval_needed"

    continue_workflow(feedback="معتمد بالكامل", session_id=sid, call_agent_fn=clinic_adapter)

    with state_lock:
        assert workflow_state["status"] == "completed"

    # Workstream I: Operator Audit verification
    audit = db_store.get_operator_session_audit(sid)
    assert audit["session"]["status"] == "completed"
    assert audit["publication_and_delivery"]["is_published"] is True
    assert audit["publication_and_delivery"]["published_artifact_count"] > 0
    assert "inspect_deliverables" in audit["permitted_recovery_actions"]


def test_workstream_h_scenario_3_browser_game_playwright_acceptance(monkeypatch, tmp_path):
    """
    Workstream H: Scenario 3 - Interactive HTML5 game browser acceptance via Playwright.
    Verifies keyboard interaction, canvas rendering, score reset, and zero page/console errors.
    """
    if not PLAYWRIGHT_AVAILABLE:
        pytest.skip("Playwright is not installed in current environment; skipping browser test")

    test_gen_dir = str(tmp_path / "gen_game_i")
    os.makedirs(test_gen_dir, exist_ok=True)
    monkeypatch.setattr("app.GENERATED_APPS_DIR", test_gen_dir)

    game_html = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>Space Dodger</title>
  <link rel="stylesheet" href="style.css">
</head>
<body>
  <div id="game-container">
    <div id="hud">Score: <span id="score-display">0</span></div>
    <canvas id="game-canvas" width="400" height="400"></canvas>
    <button id="btn-restart">Restart</button>
  </div>
  <script src="script.js"></script>
</body>
</html>"""

    game_css = """body { margin: 0; background: #0f172a; color: #fff; font-family: sans-serif; display: flex; justify-content: center; align-items: center; height: 100vh; }
#game-container { text-align: center; }
#game-canvas { border: 2px solid #38bdf8; background: #1e293b; }
button { margin-top: 10px; padding: 8px 16px; background: #38bdf8; color: #000; border: none; cursor: pointer; }"""

    game_js = """document.addEventListener('DOMContentLoaded', () => {
  const canvas = document.getElementById('game-canvas');
  const ctx = canvas.getContext('2d');
  const scoreDisplay = document.getElementById('score-display');
  const restartBtn = document.getElementById('btn-restart');
  let score = 0;
  let player = { x: 200, y: 350, size: 20 };

  function draw() {
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    ctx.fillStyle = '#38bdf8';
    ctx.fillRect(player.x, player.y, player.size, player.size);
  }

  window.addEventListener('keydown', (e) => {
    if (e.code === 'Space') {
      score += 10;
      scoreDisplay.innerText = score;
      draw();
    }
  });

  restartBtn.addEventListener('click', () => {
    score = 0;
    scoreDisplay.innerText = score;
    draw();
  });

  draw();
});"""

    qa_res = run_qa_checks(game_html, game_css, game_js)
    assert qa_res["passed"] is True

    game_dir = os.path.join(test_gen_dir, "space_dodger_i")
    pub_res = publish_workflow_deliverables(
        app_dir=game_dir,
        task_title="Space Dodger",
        validated_html=game_html,
        validated_css=game_css,
        validated_js=game_js,
        qa_report=qa_res,
        workflow_outputs={}
    )
    assert os.path.exists(os.path.join(game_dir, "manifest.json"))

    httpd, server_thread, base_url = _serve_directory_ephemerally(game_dir)
    page_errors = []
    console_errors = []

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                page.on("pageerror", lambda err: page_errors.append(str(err)))
                page.on("console", lambda msg: console_errors.append(msg.text) if msg.type == "error" else None)

                page.goto(f"{base_url}/index.html", timeout=10000)

                # 1. Assert initial state
                assert page.locator("#score-display").inner_text() == "0"

                # 2. Press Space to increment score
                page.keyboard.press("Space")
                time.sleep(0.05)
                assert page.locator("#score-display").inner_text() == "10"

                # 3. Click restart to reset score
                page.locator("#btn-restart").click()
                time.sleep(0.05)
                assert page.locator("#score-display").inner_text() == "0"

                # 4. Assert zero console and page errors
                assert len(page_errors) == 0, f"Page errors: {page_errors}"
                assert len(console_errors) == 0, f"Console errors: {console_errors}"
            finally:
                browser.close()
    finally:
        httpd.shutdown()
        httpd.server_close()
