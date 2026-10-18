import os
import json
import pytest
import tempfile
import shutil
from unittest.mock import patch, MagicMock

from app import (
    app,
    triage_project_squad,
    safe_resolve_agent_path,
    safe_load_agent_prompt,
    normalize_text,
    contains_keyword,
    SQUAD_ARCHETYPES,
    workflow_state,
    state_lock,
    run_workflow,
    continue_workflow,
    MAX_AGENT_PROMPT_BYTES,
    MIN_AGENT_PROMPT_BYTES
)

@pytest.fixture
def client():
    app.config['TESTING'] = True
    with app.test_client() as c:
        yield c

@pytest.fixture(autouse=True)
def isolated_agents_environment(tmp_path, monkeypatch):
    """
    Hermetic test isolation fixture:
    Provisions a lightweight isolated directory containing the archetypal specialist agent definitions.
    Sets AGENTS_DIR to this isolated path for the duration of every test.
    Guarantees test portability across local workstations and headless CI runners.
    """
    agents_dir = tmp_path / "mock_agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    
    specialist_files = [
        os.path.join("specialized", "business-strategist.md"),
        os.path.join("specialized", "specialized-pricing-analyst.md"),
        os.path.join("healthcare", "healthcare-clinical-evidence-agent.md"),
        os.path.join("engineering", "engineering-database-optimizer.md"),
        os.path.join("engineering", "engineering-ai-data-remediation-engineer.md"),
        os.path.join("engineering", "engineering-code-reviewer.md"),
        os.path.join("testing", "testing-accessibility-auditor.md"),
        os.path.join("testing", "testing-performance-benchmarker.md"),
        os.path.join("game-development", "game-designer.md"),
        os.path.join("game-development", "economy-designer.md"),
        os.path.join("design", "design-whimsy-injector.md"),
        os.path.join("marketing", "marketing-seo-specialist.md"),
        os.path.join("security", "security-appsec-engineer.md"),
        os.path.join("finance", "finance-financial-analyst.md")
    ]
    for rel in specialist_files:
        fpath = agents_dir / rel
        fpath.parent.mkdir(parents=True, exist_ok=True)
        fpath.write_text(f"# Specialist Agent: {rel}\nRole: Autonomous specialist test definition.\n", encoding="utf-8")
        
    monkeypatch.setenv("AGENTS_DIR", str(agents_dir))
    monkeypatch.setattr("app.AGENTS_DIR", str(agents_dir))

@pytest.fixture(autouse=True)
def reset_state():
    """Reset workflow_state to idle baseline before and after each test."""
    with state_lock:
        workflow_state["status"] = "idle"
        workflow_state["current_task"] = None
        workflow_state["squad_plan"] = None
        workflow_state["active_squad"] = []
        workflow_state["feed"] = []
        workflow_state["blockers"] = []
        workflow_state["qa_report"] = None
        workflow_state["approval_request"] = None
        for d in workflow_state["departments"]:
            workflow_state["departments"][d] = {"status": "idle", "text": "في انتظار بدء المشروع"}
    yield
    with state_lock:
        workflow_state["status"] = "idle"
        workflow_state["current_task"] = None
        workflow_state["squad_plan"] = None
        workflow_state["active_squad"] = []
        workflow_state["feed"] = []
        workflow_state["blockers"] = []
        workflow_state["qa_report"] = None
        workflow_state["approval_request"] = None
        for d in workflow_state["departments"]:
            workflow_state["departments"][d] = {"status": "idle", "text": "في انتظار بدء المشروع"}

# =========================================================================
# 1. Classification Tests (Bilingual & Archetype Coverage)
# =========================================================================

def test_triage_arabic_dental_clinic_selects_healthcare_squad():
    """Arabic dental clinic brief selects healthcare-relevant squad."""
    brief = "عايز اعمل موقع لعيادة اسنان لحجز المواعيد والكشف الطبي للمرضى"
    res = triage_project_squad(brief, allow_semantic=False)
    assert res["archetype"] == "healthcare"
    assert "Healthcare" in res["label"]
    assert res["method"] == "heuristic"
    specialist_ids = [s["id"] for s in res["specialists"]]
    assert "healthcare-clinical-evidence-agent" in specialist_ids
    assert "engineering-database-optimizer" in specialist_ids
    assert "testing-accessibility-auditor" in specialist_ids
    assert len(res["specialists"]) == 3

def test_triage_english_healthcare_selects_healthcare_squad():
    """English dental/doctor clinic brief selects healthcare squad."""
    brief = "Build a dental clinic patient appointment and doctor schedule platform"
    res = triage_project_squad(brief, allow_semantic=False)
    assert res["archetype"] == "healthcare"
    assert any(s["id"] == "healthcare-clinical-evidence-agent" for s in res["specialists"])

def test_triage_arabic_ecommerce_selects_commerce_squad():
    """Arabic e-commerce brief selects commerce-relevant roles."""
    brief = "متجر إلكتروني لبيع العطور مع سلة شراء ودفع بالفيزا وتتبع الشحنات والأسعار"
    res = triage_project_squad(brief, allow_semantic=False)
    assert res["archetype"] == "ecommerce"
    assert "Pricing" in res["label"] or "E-Commerce" in res["label"]
    specialist_ids = [s["id"] for s in res["specialists"]]
    assert "specialized-pricing-analyst" in specialist_ids
    assert "security-appsec-engineer" in specialist_ids
    assert "testing-performance-benchmarker" in specialist_ids

def test_triage_english_ecommerce_selects_commerce_squad():
    """English online shopping ecommerce store brief selects ecommerce squad."""
    brief = "Full-featured online retail store with shopping cart checkout and pricing inventory"
    res = triage_project_squad(brief, allow_semantic=False)
    assert res["archetype"] == "ecommerce"
    specialist_ids = [s["id"] for s in res["specialists"]]
    assert "specialized-pricing-analyst" in specialist_ids

def test_triage_industrial_telemetry_selects_iot_squad():
    """Industrial telemetry and sensor brief selects industrial/IoT squad (bilingual)."""
    ar_brief = "نظام مراقبة حساسات المصنع مع قراءات تليميتري وصيانة تنبؤية للآلات"
    en_brief = "Factory floor IoT sensor telemetry monitoring dashboard and predictive maintenance"
    
    res_ar = triage_project_squad(ar_brief, allow_semantic=False)
    assert res_ar["archetype"] == "industrial"
    assert any(s["id"] == "engineering-ai-data-remediation-engineer" for s in res_ar["specialists"])
    assert any(s["id"] == "security-appsec-engineer" for s in res_ar["specialists"])

    res_en = triage_project_squad(en_brief, allow_semantic=False)
    assert res_en["archetype"] == "industrial"

def test_triage_game_brief_selects_gaming_squad():
    """Game brief selects game-relevant specialists (bilingual)."""
    ar_brief = "لعبة تتريس كلاسيكية مع تسجيل النقاط ومراحل ومستويات متعددة"
    en_brief = "Classic arcade puzzle game with high score board and fluid controls"
    
    res_ar = triage_project_squad(ar_brief, allow_semantic=False)
    assert res_ar["archetype"] == "gaming"
    specialist_ids = [s["id"] for s in res_ar["specialists"]]
    assert "game-designer" in specialist_ids
    assert "economy-designer" in specialist_ids
    assert "design-whimsy-injector" in specialist_ids

    res_en = triage_project_squad(en_brief, allow_semantic=False)
    assert res_en["archetype"] == "gaming"

def test_triage_fintech_dashboard_selects_fintech_squad():
    """Fintech and financial dashboard brief selects financial/security roles."""
    ar_brief = "لوحة تحكم مالية لمتابعة الميزانية والفواتير والأرباح ومؤشرات الأداء KPI"
    en_brief = "Corporate accounting dashboard for budget forecasting, invoices, and KPI metrics"
    
    res_ar = triage_project_squad(ar_brief, allow_semantic=False)
    assert res_ar["archetype"] == "fintech"
    specialist_ids = [s["id"] for s in res_ar["specialists"]]
    assert "finance-financial-analyst" in specialist_ids
    assert "engineering-database-optimizer" in specialist_ids
    assert "security-appsec-engineer" in specialist_ids

    res_en = triage_project_squad(en_brief, allow_semantic=False)
    assert res_en["archetype"] == "fintech"

def test_triage_general_portfolio_selects_generalist_squad():
    """General portfolio, blog, or showcase selects generalist squad."""
    ar_brief = "موقع بورتفوليو شخصي لمعرض الأعمال مع مدونة تعريفية"
    en_brief = "Personal portfolio showcase blog and landing page"
    
    res_ar = triage_project_squad(ar_brief, allow_semantic=False)
    assert res_ar["archetype"] == "general"
    specialist_ids = [s["id"] for s in res_ar["specialists"]]
    assert "engineering-code-reviewer" in specialist_ids
    assert "design-whimsy-injector" in specialist_ids
    assert "marketing-seo-specialist" in specialist_ids

    res_en = triage_project_squad(en_brief, allow_semantic=False)
    assert res_en["archetype"] == "general"

def test_triage_ambiguous_and_empty_briefs_produce_safe_results():
    """Empty, whitespace, non-string, or ambiguous briefs return safe generalist squad."""
    for empty_input in ["", "   ", None, "\n\t"]:
        res = triage_project_squad(empty_input, allow_semantic=False)
        assert res["archetype"] == "general"
        assert res["method"] == "fallback"
        assert len(res["specialists"]) == 3

    vague_brief = "مشروع برمجي جديد بدون تفاصيل محددة"
    res_vague = triage_project_squad(vague_brief, allow_semantic=False)
    assert res_vague["archetype"] == "general"
    assert res_vague["method"] == "fallback"
    assert res_vague.get("ambiguous") is True

def test_triage_conflicting_keywords_do_not_produce_oversized_squads():
    """Conflicting keywords from multiple archetypes do not inflate squad size beyond 3-5."""
    brief = "متجر إلكتروني لمعدات المصانع مع لعبة نقاط ولوحة تحكم مالية"
    res = triage_project_squad(brief, allow_semantic=False)
    assert res["archetype"] in SQUAD_ARCHETYPES
    assert 3 <= len(res["specialists"]) <= 5


# =========================================================================
# 2. Discovery & Security Tests (Path Containment, Size, Symlinks)
# =========================================================================

def test_safe_resolve_agent_path_valid():
    """Resolving a legitimate agent relative path succeeds."""
    path, err = safe_resolve_agent_path(r"specialized\business-strategist.md")
    assert err is None
    assert path is not None
    assert os.path.exists(path)

def test_safe_resolve_agent_path_rejects_traversal():
    """Directory traversal attempts ('..') are strictly rejected."""
    bad_paths = [
        r"..\app.py",
        r"specialized\..\..\app.py",
        r"../app.py",
        r"healthcare/../../../etc/passwd",
        r"..\..\..\..\Windows\System32\cmd.exe"
    ]
    for bp in bad_paths:
        path, err = safe_resolve_agent_path(bp)
        assert path is None
        assert "Path traversal" in err or "escapes" in err

def test_safe_resolve_agent_path_rejects_absolute_paths():
    """Absolute Windows and Unix paths are strictly rejected."""
    bad_paths = [
        r"C:\Windows\System32\drivers\etc\hosts",
        r"C:\Users\sam20\agency-dashboard\app.py",
        r"/etc/passwd",
        r"\Windows\win.ini"
    ]
    for bp in bad_paths:
        path, err = safe_resolve_agent_path(bp)
        assert path is None
        assert "Absolute paths not permitted" in err

def test_safe_resolve_agent_path_handles_missing_file():
    """Non-existent agent files return explicit error."""
    path, err = safe_resolve_agent_path(r"nonexistent\missing-specialist.md")
    assert path is None
    assert "does not exist" in err

def test_safe_resolve_agent_path_invalid_types():
    """Non-string, empty, or None paths return explicit error."""
    for invalid in [None, "", "   ", 123, []]:
        path, err = safe_resolve_agent_path(invalid)
        assert path is None
        assert "Invalid path" in err

def test_safe_load_agent_prompt_oversized_file(tmp_path):
    """Prompt files exceeding max_bytes threshold are rejected with fallback."""
    fake_agents_dir = tmp_path / "agents"
    fake_agents_dir.mkdir()
    huge_file = fake_agents_dir / "huge-specialist.md"
    huge_content = "X" * (MAX_AGENT_PROMPT_BYTES + 500)
    huge_file.write_text(huge_content, encoding="utf-8")

    content, ok, err = safe_load_agent_prompt(
        "huge-specialist.md",
        fallback_prompt="Default Fallback",
        agents_dir=str(fake_agents_dir)
    )
    assert ok is False
    assert "exceeds maximum limit" in err
    assert content == "Default Fallback"

def test_safe_load_agent_prompt_empty_file(tmp_path):
    """Suspiciously tiny prompt files (<20 bytes) are rejected with fallback."""
    fake_agents_dir = tmp_path / "agents"
    fake_agents_dir.mkdir()
    tiny_file = fake_agents_dir / "tiny.md"
    tiny_file.write_text("# Hi", encoding="utf-8")

    content, ok, err = safe_load_agent_prompt(
        "tiny.md",
        fallback_prompt="Safe Prompt",
        agents_dir=str(fake_agents_dir)
    )
    assert ok is False
    assert "below minimum viable threshold" in err
    assert content == "Safe Prompt"

def test_symlink_escape_rejection(tmp_path):
    """Symlink pointing outside agents root is detected and rejected."""
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    outside_file = tmp_path / "outside_secret.txt"
    outside_file.write_text("secret content", encoding="utf-8")

    link_path = agents_dir / "escaped_link.md"
    try:
        os.symlink(str(outside_file), str(link_path))
    except (OSError, NotImplementedError):
        pytest.skip("Symlink creation not supported in current environment/permissions")

    path, err = safe_resolve_agent_path("escaped_link.md", agents_dir=str(agents_dir))
    assert path is None
    assert "Symlink escape" in err


# =========================================================================
# 3. Semantic Fallback & Mocked Provider Tests
# =========================================================================

def test_semantic_triage_fallback_mocked_success():
    """Mocked LLM semantic triage correctly classifies ambiguous brief."""
    ambiguous_brief = "مشروع تقني متقدم يتطلب إرشادات استراتيجية مخصصة"
    mock_llm_json = json.dumps({
        "archetype": "fintech",
        "confidence": 0.88,
        "reasoning": "Project references enterprise financial strategy"
    })

    with patch("app.classify_project_semantic", return_value=("fintech", 0.88, "Mocked classification")):
        res = triage_project_squad(ambiguous_brief, allow_semantic=True)
        assert res["archetype"] == "fintech"
        assert res["method"] == "semantic"
        assert res["confidence"] == 0.88
        assert res["reasoning"] == "Mocked classification"
        assert any(s["id"] == "finance-financial-analyst" for s in res["specialists"])

def test_semantic_triage_fallback_on_llm_error():
    """When semantic LLM fails, triage safely falls back to general archetype."""
    ambiguous_brief = "مشروع تقني مبهم تماماً"
    with patch("app.classify_project_semantic", return_value=None):
        res = triage_project_squad(ambiguous_brief, allow_semantic=True)
        assert res["archetype"] == "general"
        assert res["method"] == "fallback"
        assert res.get("ambiguous") is True

def test_semantic_triage_disabled_in_testing_env():
    """Ensure automated tests never trigger real external network calls."""
    ambiguous_brief = "مشروع تقني غامض"
    # Even if allow_semantic=True, classify_project_semantic should return None under pytest
    res = triage_project_squad(ambiguous_brief, allow_semantic=True)
    assert res["archetype"] == "general"
    assert res["method"] == "fallback"


# =========================================================================
# 4. Integration & Truthful Telemetry Tests
# =========================================================================

def test_active_squad_initialized_and_exposed_truthfully(client):
    """active_squad is initialized with 'selected' status and exposed via /api/state."""
    brief = "عيادة أسنان لحجز المواعيد والكشف الطبي"
    squad_plan = triage_project_squad(brief, allow_semantic=False)
    
    with state_lock:
        workflow_state["status"] = "running"
        workflow_state["current_task"] = brief
        workflow_state["squad_plan"] = squad_plan
        workflow_state["active_squad"] = [
            {
                "id": s["id"],
                "role_id": s["id"],
                "name": s["name"],
                "display_name": s["display_name"],
                "category": s["category"],
                "stage": s["stage"],
                "reason": s["reason"],
                "status": "selected"
            }
            for s in squad_plan["specialists"]
        ]

    # Check /api/state
    res = client.get('/api/state')
    assert res.status_code == 200
    state = res.get_json()
    assert "active_squad" in state
    assert len(state["active_squad"]) == 3
    for agent in state["active_squad"]:
        assert agent["status"] == "selected"
        assert "prompt_file" not in agent  # Sensitive path not exposed!
        assert "api_key" not in agent

    # Check /api/agents truthful telemetry
    res_agents = client.get('/api/agents')
    assert res_agents.status_code == 200
    agents_list = res_agents.get_json()
    
    agent_map = {a["id"]: a["status"] for a in agents_list}
    assert agent_map.get("healthcare-clinical-evidence-agent") == "selected"
    assert agent_map.get("engineering-database-optimizer") == "selected"
    assert agent_map.get("testing-accessibility-auditor") == "selected"
    # Unselected agent must be idle!
    assert agent_map.get("game-designer") == "idle"

def test_workflow_execution_transitions_specialist_statuses_honestly():
    """Specialists transition from 'selected' -> 'running' -> 'completed' truthfully."""
    brief = "متجر إلكتروني مع سلة شراء ودفع"
    
    def mock_call_agent(agent_file, role_name, short_name, color, prompt, context_text=""):
        # Verify that during call_agent execution, the corresponding specialist is 'running'
        with state_lock:
            running_agents = [s["id"] for s in workflow_state.get("active_squad", []) if s["status"] == "running"]
        return f"Mock response for {role_name}"

    with patch("app.call_agent", side_effect=mock_call_agent):
        run_workflow(brief)

    with state_lock:
        status = workflow_state["status"]
        squad = workflow_state.get("active_squad", [])

    # The workflow stops at approval_needed gate
    assert status == "approval_needed"
    assert len(squad) == 3
    # Strategy stage specialist should be completed
    strat_spec = next(s for s in squad if s["stage"] == "strategy")
    assert strat_spec["status"] == "completed"
    
    # Engineering and QA stage specialists should still be 'selected' (not yet executed!)
    eng_spec = next(s for s in squad if s["stage"] == "engineering")
    assert eng_spec["status"] == "selected"
    qa_spec = next(s for s in squad if s["stage"] == "qa")
    assert qa_spec["status"] == "selected"

def test_failed_specialist_invocation_records_honest_failure():
    """If a specialist invocation fails, it is marked 'failed' and observable in feed."""
    brief = "نظام مراقبة حساسات المصنع مع قراءات تليميتري"
    
    def failing_call_agent(agent_file, role_name, short_name, color, prompt, context_text=""):
        if "حساسات" in role_name or short_name == "IOT":
            raise RuntimeError("Simulated IoT specialist timeout")
        return "Normal agent output"

    with patch("app.call_agent", side_effect=failing_call_agent):
        run_workflow(brief)

    with state_lock:
        squad = workflow_state.get("active_squad", [])
        feed_texts = [f["text"] for f in workflow_state.get("feed", [])]

    failed_specs = [s for s in squad if s["status"] == "failed"]
    assert len(failed_specs) >= 1
    assert any("Simulated IoT specialist timeout" in t or "تعذر استكمال" in t for t in feed_texts)

def test_approval_gate_preserved_and_continues_to_completion(tmp_path, monkeypatch):
    """Full workflow with triage: triage -> strategy -> PRD gate -> approval -> delivery."""
    test_gen_dir = str(tmp_path / "generated_apps")
    os.makedirs(test_gen_dir, exist_ok=True)
    monkeypatch.setattr("app.GENERATED_APPS_DIR", test_gen_dir)

    valid_html = """<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head><meta charset="UTF-8"><title>تطبيق طبي</title></head>
<body><div id="app"><h1>عيادة النور</h1></div></body>
</html>"""
    valid_css = "body { margin: 0; background: #fff; } #app { color: #000; }"
    valid_js = "document.addEventListener('DOMContentLoaded', function(){ console.log('Ready'); });"

    def fake_call(agent_file, role_name, short_name, color, prompt, context_text=""):
        if short_name == "DEV":
            return f"```html\n{valid_html}\n```\n```css\n{valid_css}\n```\n```javascript\n{valid_js}\n```"
        elif short_name == "MOTION":
            return f"```css\n/* motion */\n```\n```javascript\n/* motion */\n```"
        elif short_name == "SEO":
            return "<meta name='description' content='medical'>"
        elif short_name == "QA":
            return "Approved"
        return "Stage output"

    brief = "عيادة أسنان لحجز المواعيد الطبية"
    with patch("app.call_agent", side_effect=fake_call):
        run_workflow(brief)
        with state_lock:
            assert workflow_state["status"] == "approval_needed"
            assert workflow_state["approval_request"] is not None

        continue_workflow(feedback="معتمد بالكامل")
        with state_lock:
            assert workflow_state["status"] == "completed"
            assert workflow_state["qa_report"]["passed"] is True
            # All specialists should now be completed
            for s in workflow_state["active_squad"]:
                assert s["status"] == "completed"
