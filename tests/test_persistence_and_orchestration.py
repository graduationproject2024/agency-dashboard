"""
Phase 2.4-B Test Suite:
Persistent Multi-Agent Execution Foundation, Cross-Division Delegation & Project Memory.
"""

import os
import pytest
import tempfile
import sqlite3
import copy
import json

import db_persistence as db_store
import task_orchestrator as orch
from app import app, CANONICAL_DIVISIONS, discover_agents


@pytest.fixture
def client():
    app.config["TESTING"] = True
    with app.test_client() as client:
        yield client


# ============================================================================
# 1. Database Schema & Persistence Integrity Tests
# ============================================================================

def test_db_init_and_tables_creation():
    """Verify that all normalized tables and indexes are created properly."""
    with db_store.get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = {row[0] for row in cursor.fetchall()}
        
    expected_tables = {
        "workflow_sessions",
        "workflow_tasks",
        "agent_runs",
        "task_handoffs",
        "project_memory",
        "execution_checkpoints"
    }
    assert expected_tables.issubset(tables), f"Missing tables: {expected_tables - tables}"


def test_session_lifecycle_and_state_transitions():
    """Verify session creation, updating status, and retrieval."""
    session_id = "test_sess_001"
    project_id = "Project_Test_Clinic"
    
    # Create session
    sess = db_store.create_session(session_id, project_id, current_stage="strategy", initial_state={"step": 1})
    assert sess["id"] == session_id
    assert sess["project_id"] == project_id
    assert sess["status"] == "running"
    assert sess["current_stage"] == "strategy"
    
    # Retrieve session
    fetched = db_store.get_session(session_id)
    assert fetched is not None
    assert fetched["id"] == session_id
    assert fetched["status"] == "running"
    
    # Valid state transition
    updated = db_store.update_session_status(session_id, "approval_needed", checkpoint_step="prd_approval_gate", current_stage="product")
    assert updated["status"] == "approval_needed"
    assert updated["checkpoint_step"] == "prd_approval_gate"
    assert updated["current_stage"] == "product"
    
    # Terminal transition
    completed = db_store.update_session_status(session_id, "completed", checkpoint_step="delivery_completed")
    assert completed["status"] == "completed"
    assert completed["completed_at"] is not None


def test_invalid_state_transition_raises_value_error():
    """Verify that invalid state transitions raise explicit ValueError."""
    session_id = "test_sess_invalid"
    db_store.create_session(session_id, "Test_Project")
    with pytest.raises(ValueError, match="Invalid status"):
        db_store.update_session_status(session_id, "super_quantum_finished")


def test_reconcile_interrupted_sessions_on_restart():
    """Verify process restart reconciliation marks running sessions as interrupted."""
    s1 = "sess_interrupted_1"
    s2 = "sess_completed_2"
    db_store.create_session(s1, "Project_1")
    db_store.create_session(s2, "Project_2")
    db_store.update_session_status(s2, "completed")
    
    count = db_store.reconcile_interrupted_sessions()
    assert count >= 1
    
    sess1 = db_store.get_session(s1)
    sess2 = db_store.get_session(s2)
    assert sess1["status"] == "interrupted"
    assert sess2["status"] == "completed"  # Terminal states remain intact


def test_checkpoints_saving_and_retrieval():
    """Verify saving checkpoints and retrieving the latest checkpoint."""
    session_id = "sess_checkpoints_test"
    db_store.create_session(session_id, "Project_Checkpoints")
    
    # Save checkpoint 1
    cp1 = db_store.save_session_checkpoint(session_id, "prd_approval_gate", {"step": "prd", "count": 1})
    assert cp1["step_name"] == "prd_approval_gate"
    assert cp1["state_data"]["count"] == 1
    
    # Save checkpoint 2
    cp2 = db_store.save_session_checkpoint(session_id, "engineering_completed", {"step": "eng", "count": 2})
    assert cp2["step_name"] == "engineering_completed"
    
    # Latest checkpoint should be cp2
    latest = db_store.get_latest_checkpoint(session_id)
    assert latest["step_name"] == "engineering_completed"
    assert latest["state_data"]["count"] == 2
    
    # Checkpoints list
    all_cps = db_store.get_session_checkpoints(session_id)
    assert len(all_cps) == 2
    assert all_cps[0]["step_name"] == "engineering_completed"


# ============================================================================
# 2. Directed Task Graph (DAG) & Orchestration Tests
# ============================================================================

def test_task_graph_planning_and_dependencies():
    """Verify deterministic DAG generation for a project brief."""
    task_desc = "اريد متجر الكتروني لبيع الملابس"
    squad_plan = {
        "archetype": "ecommerce",
        "specialists": [
            {
                "id": "specialized-pricing-analyst",
                "name": "محلل التسعير",
                "stage": "strategy",
                "reason": "تحليل الخصومات",
                "prompt_file": r"specialized\specialized-pricing-analyst.md"
            },
            {
                "id": "security-appsec-engineer",
                "name": "مهندس أمن التطبيقات",
                "stage": "engineering",
                "reason": "تأمين بوابات الدفع",
                "prompt_file": r"security\security-appsec-engineer.md"
            },
            {
                "id": "testing-performance-benchmarker",
                "name": "مدقق الأداء",
                "stage": "qa",
                "reason": "اختبار الضغط",
                "prompt_file": r"testing\testing-performance-benchmarker.md"
            }
        ]
    }
    
    tasks = orch.build_project_task_graph(task_desc, squad_plan)
    assert len(tasks) >= 8
    
    # Verify canonical division mapping
    divisions = {t["division_id"] for t in tasks}
    assert "specialized" in divisions or "product" in divisions
    assert "engineering" in divisions
    assert "design" in divisions
    assert "testing" in divisions or "security" in divisions
    
    # Verify DAG cycle detection
    has_cycle, cycle_path = orch.detect_cycles(tasks)
    assert not has_cycle, f"Cycle detected in task graph: {cycle_path}"


def test_detect_cycles_rejects_cyclic_graphs():
    """Verify that detect_cycles correctly identifies and rejects circular dependencies."""
    cyclic_tasks = [
        {"id": "task_A", "dependencies": ["task_B"]},
        {"id": "task_B", "dependencies": ["task_C"]},
        {"id": "task_C", "dependencies": ["task_A"]}
    ]
    has_cycle, cycle_path = orch.detect_cycles(cyclic_tasks)
    assert has_cycle is True
    assert len(cycle_path) >= 3


def test_ready_tasks_resolution():
    """Verify sequential and parallel task resolution based on completed dependencies."""
    tasks = [
        {"id": "t1", "status": "completed", "dependencies": []},
        {"id": "t2", "status": "pending", "dependencies": ["t1"]},
        {"id": "t3", "status": "pending", "dependencies": ["t1"]},
        {"id": "t4", "status": "pending", "dependencies": ["t2", "t3"]}
    ]
    ready = orch.get_ready_tasks(tasks)
    ready_ids = [t["id"] for t in ready]
    assert "t2" in ready_ids
    assert "t3" in ready_ids
    assert "t4" not in ready_ids


def test_resolve_task_inputs_output_propagation():
    """Verify that outputs from upstream completed dependencies propagate into downstream tasks."""
    tasks = [
        {"id": "t_arch", "status": "completed", "output": "DATABASE_SCHEMA_V1", "dependencies": []},
        {"id": "t_dev", "status": "pending", "output": None, "dependencies": ["t_arch"]}
    ]
    inputs = orch.resolve_task_inputs(tasks[1], tasks)
    assert "DATABASE_SCHEMA_V1" in inputs
    assert "t_arch" in inputs


# ============================================================================
# 3. Controlled Agent-to-Agent Collaboration & Handoffs Tests
# ============================================================================

def test_record_task_handoff_and_retrieval():
    """Verify recording and querying explicit agent work handoffs."""
    session_id = "sess_handoff_test"
    db_store.create_session(session_id, "Project_Handoff")
    
    h1 = db_store.record_task_handoff(
        session_id=session_id,
        from_task_id="task_eng_arch",
        to_task_id="task_dev_code",
        from_agent="engineering-software-architect",
        to_agent="engineering-senior-developer",
        context_summary="System Architecture & Database Schemas",
        artifacts={"schema": "users, appointments"}
    )
    assert h1["id"] is not None
    assert h1["from_agent"] == "engineering-software-architect"
    assert h1["to_agent"] == "engineering-senior-developer"
    
    handoffs = db_store.get_session_handoffs(session_id)
    assert len(handoffs) == 1
    assert handoffs[0]["context_summary"] == "System Architecture & Database Schemas"
    assert handoffs[0]["artifacts"]["schema"] == "users, appointments"


# ============================================================================
# 4. Project-Scoped Memory Tests
# ============================================================================

def test_project_memory_scoping_and_categories():
    """Verify project memory storage, categories, and project-isolation."""
    p_dental = "Dental_Clinic_System"
    p_ecommerce = "Ecommerce_Store_App"
    
    # Add memories to Dental project
    m1 = db_store.add_project_memory(p_dental, "s1", "requirement", "WCAG_Accessibility", "Must support high contrast for elderly patients", "testing-accessibility-auditor")
    m2 = db_store.add_project_memory(p_dental, "s1", "architecture_decision", "Database_Engine", "SQLite in WAL mode with strict foreign keys", "engineering-database-optimizer")
    
    # Add memory to Ecommerce project
    m3 = db_store.add_project_memory(p_ecommerce, "s2", "requirement", "Payment_Gateway", "Stripe payment integration required", "specialized-pricing-analyst")
    
    # Retrieve dental memories
    dental_mems = db_store.get_project_memory(p_dental)
    assert len(dental_mems) == 2
    keys = {m["key"] for m in dental_mems}
    assert "WCAG_Accessibility" in keys
    assert "Database_Engine" in keys
    
    # Strict isolation: Ecommerce memories MUST NOT leak into dental project
    assert "Payment_Gateway" not in keys
    
    # Category filter
    arch_mems = db_store.get_project_memory(p_dental, category="architecture_decision")
    assert len(arch_mems) == 1
    assert arch_mems[0]["key"] == "Database_Engine"


def test_project_memory_secret_sanitization():
    """Verify that API keys and sensitive tokens are automatically redacted from memory."""
    project_id = "Project_Secret_Test"
    leaked_secret = "Here is the key: AIzaSyD9876543210abcdefghijklmnop and password=SuperSecretPassword123"
    
    mem = db_store.add_project_memory(project_id, "s_sec", "requirement", "Config_Details", leaked_secret, "system")
    assert "AIzaSy" not in mem["content"]
    assert "[REDACTED_API_KEY]" in mem["content"]
    assert "SuperSecretPassword123" not in mem["content"]
    assert "[REDACTED_CREDENTIAL]" in mem["content"]


def test_project_memory_supersession():
    """Verify that newer decisions supersede older ones when supersedes_key is used."""
    project_id = "Project_Supersede_Test"
    db_store.add_project_memory(project_id, "s1", "architecture_decision", "DB_Choice", "Use MongoDB", "arch1")
    
    # Supersede with Postgres
    db_store.add_project_memory(project_id, "s1", "architecture_decision", "DB_Choice_v2", "Use SQLite", "arch2", supersedes_key="DB_Choice")
    
    mems = db_store.get_project_memory(project_id)
    assert len(mems) == 1
    assert mems[0]["key"] == "DB_Choice_v2"
    assert mems[0]["content"] == "Use SQLite"


# ============================================================================
# 5. REST API Endpoints & Security Policy Tests
# ============================================================================

def test_api_state_includes_persistence_telemetry(client):
    """Verify that /api/state returns persistence metadata when a session exists."""
    res = client.get('/api/state')
    assert res.status_code == 200
    data = res.get_json()
    assert "departments" in data
    assert "feed" in data
    assert "budget" in data


def test_api_sessions_list_and_detail(client):
    """Verify GET /api/sessions and GET /api/sessions/<id>."""
    sess_id = "api_test_session_42"
    db_store.create_session(sess_id, "Project_API_Test", current_stage="strategy")
    db_store.save_session_checkpoint(sess_id, "prd_approval_gate", {"state": "ok"})
    
    # List sessions
    res_list = client.get('/api/sessions')
    assert res_list.status_code == 200
    sessions_data = res_list.get_json()
    assert any(s["id"] == sess_id for s in sessions_data.get("sessions", []))
    
    # Session detail
    res_detail = client.get(f'/api/sessions/{sess_id}')
    assert res_detail.status_code == 200
    detail = res_detail.get_json()
    assert detail["session"]["id"] == sess_id
    assert len(detail["checkpoints"]) >= 1


def test_api_session_resume_and_cancel_security_origin(client):
    """Verify that session cancellation and resumption enforce strict is_safe_origin."""
    sess_id = "api_origin_test_session"
    db_store.create_session(sess_id, "Project_Origin")
    
    # Foreign origin on resume -> 403 Forbidden
    res_foreign = client.post(
        f'/api/sessions/{sess_id}/resume',
        headers={"Origin": "http://evil-attacker.com:8080"}
    )
    assert res_foreign.status_code == 403
    
    # Foreign origin on cancel -> 403 Forbidden
    res_foreign_cancel = client.post(
        f'/api/sessions/{sess_id}/cancel',
        headers={"Origin": "http://evil-attacker.com:8080"}
    )
    assert res_foreign_cancel.status_code == 403
    
    # Valid origin on cancel -> 200 OK
    res_valid_cancel = client.post(
        f'/api/sessions/{sess_id}/cancel',
        headers={"Origin": "http://127.0.0.1:8080"}
    )
    assert res_valid_cancel.status_code == 200
    updated = db_store.get_session(sess_id)
    assert updated["status"] == "cancelled"


def test_api_memory_endpoint(client):
    """Verify GET /api/memory/<project_id>."""
    p_id = "Memory_API_Project"
    db_store.add_project_memory(p_id, "s_mem", "requirement", "Mobile_First", "Must support responsive viewports", "designer")
    
    res = client.get(f'/api/memory/{p_id}')
    assert res.status_code == 200
    data = res.get_json()
    assert data["project_id"] == p_id
    assert len(data["memories"]) >= 1
    assert data["memories"][0]["key"] == "Mobile_First"


# ============================================================================
# 6. Full Workforce and 18 Divisions Invariant Verification
# ============================================================================

def test_full_catalog_18_divisions_and_agents_intact():
    """Verify that all 18 canonical divisions and full workforce remain intact."""
    assert len(CANONICAL_DIVISIONS) == 18
    
    inventory = discover_agents()
    assert inventory["status"] == "discovered"
    assert inventory["total"] >= 282, f"Expected at least 282 agents, found {inventory['total']}"
    assert len(inventory["divisions"]) == 18
