import os
import json
import time
import re
import logging
from typing import Dict, List, Optional, Any, Callable, Tuple

import db_persistence as db

# Division metadata mappings for canonical division lookup
STAGE_DIVISION_DEFAULTS = {
    "strategy": ("specialized", "Specialized"),
    "product": ("product", "Product"),
    "engineering": ("engineering", "Engineering"),
    "design": ("design", "Design"),
    "development": ("engineering", "Engineering"),
    "qa": ("security", "Security"),
    "animation": ("design", "Design"),
    "seo": ("marketing", "Marketing"),
    "delivery": ("project-management", "Project Management")
}

def detect_cycles(task_defs: Any) -> Tuple[bool, List[str]]:
    """
    Check for circular dependencies in a proposed task graph using DFS.
    task_defs: either a list of task dicts (with 'id' or 'task_id' and 'dependencies')
               or a dict mapping node -> list of dependencies.
    Returns (has_cycle: bool, cycle_path: List[str]).
    """
    adj = {}
    if isinstance(task_defs, list):
        for t in task_defs:
            tid = t.get("id") or t.get("task_id")
            adj[tid] = t.get("dependencies", [])
    elif isinstance(task_defs, dict):
        adj = task_defs

    visited = {}  # 0 = unvisited, 1 = visiting, 2 = visited
    cycle_path = []

    def dfs(node: str, path: List[str]) -> bool:
        visited[node] = 1
        path.append(node)
        for dep in adj.get(node, []):
            if dep not in visited:
                continue
            if visited[dep] == 1:
                # Cycle found!
                idx = path.index(dep) if dep in path else 0
                cycle_path.extend(path[idx:] + [dep])
                return True
            if visited[dep] == 0:
                if dfs(dep, path):
                    return True
        path.pop()
        visited[node] = 2
        return False

    for node in adj:
        visited[node] = 0

    for node in adj:
        if visited[node] == 0:
            if dfs(node, []):
                return True, cycle_path

    return False, []


def get_ready_tasks(tasks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Identify tasks whose dependencies are all completed and are ready to run.
    """
    completed_ids = {
        t.get("id") or t.get("task_id")
        for t in tasks
        if t.get("status") == "completed"
    }
    ready = []
    for t in tasks:
        if t.get("status") in ("pending", "ready"):
            deps = t.get("dependencies", [])
            if all(d in completed_ids for d in deps):
                ready.append(t)
    return ready


def resolve_task_inputs(task: Dict[str, Any], all_tasks: List[Dict[str, Any]]) -> str:
    """
    Propagate outputs from upstream prerequisite tasks into a composite input text.
    """
    deps = task.get("dependencies", [])
    task_map = {t.get("id") or t.get("task_id"): t for t in all_tasks}
    propagated = []
    for d in deps:
        if d in task_map:
            dep_task = task_map[d]
            output = dep_task.get("output") or dep_task.get("output_text") or ""
            propagated.append(f"[{d}]: {output}")
    return "\n\n".join(propagated)


def build_project_task_graph(*args, **kwargs) -> Any:
    """
    Construct a deterministic, cross-division Directed Acyclic Graph (DAG) of tasks.
    Can be called as:
      1. build_project_task_graph(task_desc, squad_plan) -> returns List[Dict[str, Any]] (in-memory plan)
      2. build_project_task_graph(session_id, project_title, task_desc, squad_plan, db_path=None) -> returns List[str] (persisted in DB)
    """
    if len(args) == 2:
        session_id = None
        project_title = str(args[0])[:40].strip()
        task_desc = str(args[0])
        squad_plan = args[1]
        db_path = kwargs.get("db_path")
    elif len(args) >= 3:
        session_id = args[0]
        project_title = args[1]
        task_desc = args[2]
        squad_plan = args[3] if len(args) > 3 else kwargs.get("squad_plan", {})
        db_path = kwargs.get("db_path")
    else:
        session_id = kwargs.get("session_id")
        project_title = kwargs.get("project_title", "Project")
        task_desc = kwargs.get("task_desc", project_title)
        squad_plan = kwargs.get("squad_plan", {})
        db_path = kwargs.get("db_path")

    specialists = squad_plan.get("specialists", [])

    # Map specialists by stage
    strat_specs = [s for s in specialists if s.get("stage") == "strategy"]
    eng_specs = [s for s in specialists if s.get("stage") == "engineering"]
    qa_specs = [s for s in specialists if s.get("stage") == "qa"]
    anim_specs = [s for s in specialists if s.get("stage") == "animation"]
    seo_specs = [s for s in specialists if s.get("stage") == "seo"]

    # Helper to resolve division from specialist
    def get_spec_division(s, default):
        div = s.get("division_id")
        if not div and "-" in s.get("id", ""):
            div = s["id"].split("-")[0]
        return div or default

    tasks_plan = []

    # 1. Strategy Specialists
    strat_deps = []
    for idx, s in enumerate(strat_specs):
        tid = f"task_strat_spec_{idx+1}"
        div = get_spec_division(s, "specialized")
        tasks_plan.append({
            "id": tid,
            "task_id": tid,
            "title": f"استشارة استراتيجية تخصصية: {s['name']}",
            "division_id": div,
            "assigned_agent_id": s["id"],
            "stage": "strategy",
            "dependencies": [],
            "input_context": f"المشروع: {task_desc}\nالهدف التخصصي: {s.get('reason', '')}"
        })
        strat_deps.append(tid)

    # Lead Business Strategist
    lead_strat_id = "task_strat_lead"
    tasks_plan.append({
        "id": lead_strat_id,
        "task_id": lead_strat_id,
        "title": "دراسة الجدوى والتوجيه الاستراتيجي",
        "division_id": "specialized",
        "assigned_agent_id": "business-strategist",
        "stage": "strategy",
        "dependencies": strat_deps,
        "input_context": f"المشروع هو: {task_desc}. قدم دراسة جدوى استراتيجية شاملة وموجزة."
    })

    # 2. Product PRD
    prd_id = "task_prod_prd"
    tasks_plan.append({
        "id": prd_id,
        "task_id": prd_id,
        "title": "وثيقة متطلبات المنتج (PRD)",
        "division_id": "product",
        "assigned_agent_id": "product-manager",
        "stage": "product",
        "dependencies": [lead_strat_id],
        "input_context": "قم بكتابة وثيقة متطلبات المنتج (PRD) بناءً على دراسة الجدوى الاستراتيجية المعتمدة."
    })

    # 3. Engineering Specialists
    eng_deps = [prd_id]
    for idx, s in enumerate(eng_specs):
        tid = f"task_eng_spec_{idx+1}"
        div = get_spec_division(s, "engineering")
        tasks_plan.append({
            "id": tid,
            "task_id": tid,
            "title": f"التصميم الهندسي المتخصص: {s['name']}",
            "division_id": div,
            "assigned_agent_id": s["id"],
            "stage": "engineering",
            "dependencies": [prd_id],
            "input_context": f"بصفتك ({s['name']})، ضع التوصيات الهندسية والمعمارية لمشروع: {project_title} استناداً إلى: {s.get('reason', '')}."
        })
        eng_deps.append(tid)

    # Lead Software Architect
    arch_id = "task_eng_arch"
    tasks_plan.append({
        "id": arch_id,
        "task_id": arch_id,
        "title": "معمارية النظام وقواعد البيانات",
        "division_id": "engineering",
        "assigned_agent_id": "engineering-software-architect",
        "stage": "engineering",
        "dependencies": eng_deps,
        "input_context": "ضع خطة هندسية وهيكلية لقواعد البيانات ومخطط النظام بناء على وثيقة المتطلبات."
    })

    # 4. UI/UX Design
    design_id = "task_design_ui"
    tasks_plan.append({
        "id": design_id,
        "task_id": design_id,
        "title": "تصميم الهوية البصرية والواجهات",
        "division_id": "design",
        "assigned_agent_id": "design-ui-designer",
        "stage": "design",
        "dependencies": [arch_id],
        "input_context": "صمم هوية بصرية كاملة وتجربة مستخدم فاخرة بناءً على المخطط المعماري للنظام."
    })

    # 5. Senior Developer
    dev_id = "task_eng_dev"
    tasks_plan.append({
        "id": dev_id,
        "task_id": dev_id,
        "title": "البرمجة والتطوير الفني المتكامل",
        "division_id": "engineering",
        "assigned_agent_id": "engineering-senior-developer",
        "stage": "development",
        "dependencies": [arch_id, design_id],
        "input_context": f"قم ببرمجة هذا التطبيق فوراً كمشروع ويب متكامل وفائق الاحترافية (HTML, CSS, JS منفصلين) لمشروع: {project_title}."
    })

    # 6. QA Specialists
    qa_deps = [dev_id]
    for idx, s in enumerate(qa_specs):
        tid = f"task_qa_spec_{idx+1}"
        div = get_spec_division(s, "testing")
        tasks_plan.append({
            "id": tid,
            "task_id": tid,
            "title": f"تدقيق الجودة والأمان التخصصي: {s['name']}",
            "division_id": div,
            "assigned_agent_id": s["id"],
            "stage": "qa",
            "dependencies": [dev_id],
            "input_context": f"بصفتك ({s['name']})، راجع الأكواد البرمجية والتصميم بناءً على اختصاصك: {s.get('reason', '')}."
        })
        qa_deps.append(tid)

    # 7. Primary Code Reviewer
    qa_review_id = "task_qa_review"
    tasks_plan.append({
        "id": qa_review_id,
        "task_id": qa_review_id,
        "title": "المراجعة البرمجية وضمان الجودة الشامل",
        "division_id": "testing",
        "assigned_agent_id": "engineering-code-reviewer",
        "stage": "qa",
        "dependencies": qa_deps,
        "input_context": "قم بمراجعة الأكواد البرمجية، وتأكد من خلوها من الأخطاء وراجع معايير الأمان وتوصيات المتخصصين المستقلة."
    })

    # 8. Motion & SEO Polish
    anim_agent = anim_specs[0]["id"] if anim_specs else "design-whimsy-injector"
    anim_name = anim_specs[0]["name"] if anim_specs else "خبير الأنيميشن"
    anim_id = "task_anim_motion"
    tasks_plan.append({
        "id": anim_id,
        "task_id": anim_id,
        "title": f"تطوير التأثيرات الحركية والتفاعل: {anim_name}",
        "division_id": "design",
        "assigned_agent_id": anim_agent,
        "stage": "animation",
        "dependencies": [dev_id],
        "input_context": "طور كود CSS و JS لإضافة تأثيرات بصرية وحركية احترافية تجعل الموقع حياً وفخماً."
    })

    seo_agent = seo_specs[0]["id"] if seo_specs else "marketing-seo-specialist"
    seo_name = seo_specs[0]["name"] if seo_specs else "خبير SEO"
    seo_id = "task_marketing_seo"
    tasks_plan.append({
        "id": seo_id,
        "task_id": seo_id,
        "title": f"تحسين محركات البحث والبيانات الوصفية: {seo_name}",
        "division_id": "marketing",
        "assigned_agent_id": seo_agent,
        "stage": "seo",
        "dependencies": [dev_id],
        "input_context": "اقترح تحسينات محركات البحث وعلامات Meta tags والكلمات المفتاحية لتحسين الظهور."
    })

    # 9. Packaging & Final Delivery Gate
    delivery_id = "task_delivery_pack"
    tasks_plan.append({
        "id": delivery_id,
        "task_id": delivery_id,
        "title": "فحص الجودة البرمجية النهائي والنشر الذري",
        "division_id": "project-management",
        "assigned_agent_id": "project-manager",
        "stage": "delivery",
        "dependencies": [qa_review_id, anim_id, seo_id],
        "input_context": "فحص الجودة الصارم قبل النشر الفعلي للأصول المستخرجة والتحقق من الهوية الرقمية للملفات."
    })

    # Verify no cycle
    has_cycle, cycle_path = detect_cycles(tasks_plan)
    if has_cycle:
        raise ValueError(f"Cycle detected in generated task graph: {cycle_path}")

    # If in-memory plan requested (no session_id), return task dicts
    if not session_id:
        return tasks_plan

    # Otherwise persist to SQLite database
    id_mapping = {}
    created_task_ids = []
    for t in tasks_plan:
        translated_deps = [id_mapping.get(d, d) for d in t["dependencies"]]
        tid = db.create_task(
            session_id=session_id,
            title=t["title"],
            division_id=t["division_id"],
            assigned_agent_id=t["assigned_agent_id"],
            stage=t["stage"],
            dependencies=translated_deps,
            input_context=t["input_context"],
            db_path=db_path
        )
        id_mapping[t["id"]] = tid
        created_task_ids.append(tid)

    return created_task_ids



def get_task_execution_context(
    session_id: str,
    task: Dict[str, Any],
    db_path: Optional[str] = None
) -> str:
    """
    Assemble truth-grounded input context for a task:
    1. Base task input context
    2. Outputs from direct prerequisite tasks (via dependencies)
    3. Verified project decisions from project_memory
    """
    context_parts = []
    
    # 1. Base input context
    if task.get("input_context"):
        context_parts.append(task["input_context"])
        
    # 2. Prerequisite task outputs
    deps = task.get("dependencies", [])
    if deps:
        upstream_outputs = []
        for dep_id in deps:
            dep_task = db.get_task(dep_id, db_path=db_path)
            if dep_task and dep_task.get("output_text"):
                upstream_outputs.append(f"[{dep_task['title']} ({dep_task['assigned_agent_id']})]:\n{dep_task['output_text']}")
        if upstream_outputs:
            if task.get("assigned_agent_id") == "engineering-code-reviewer":
                qa_spec_reports = []
                dev_text = ""
                for dep_id in deps:
                    dep_task = db.get_task(dep_id, db_path=db_path)
                    if dep_task and dep_task.get("output_text"):
                        if dep_task.get("stage") == "development":
                            dev_text = dep_task["output_text"]
                        elif "_spec_" in dep_id or dep_task.get("stage") in ("qa", "testing"):
                            qa_spec_reports.append(f"### تقرير {dep_task['title']} ({dep_task['assigned_agent_id']}):\n{dep_task['output_text']}")
                qa_context = dev_text or task.get("input_context", "")
                if qa_spec_reports:
                    qa_spec_text = "\n\n".join(qa_spec_reports)
                    context_parts.append(f"{qa_context}\n\n---\n## نتائج وتدقيقات المتخصصين المستقلة (QA Specialists Findings):\n{qa_spec_text}\n---\nيُرجى أخذ نتائج المتخصصين أعلاه بعين الاعتبار والتحقق من توصياتهم أثناء تقييم الكود النهائي.")
                else:
                    context_parts.append("--- مخرجات المهام السابقة المعتمدة ---\n" + "\n\n".join(upstream_outputs))
            else:
                context_parts.append("--- مخرجات المهام السابقة المعتمدة ---\n" + "\n\n".join(upstream_outputs))

    # 3. Verified project memory
    session = db.get_session(session_id, db_path=db_path)
    if session:
        proj_id = session.get("project_name", session_id)
        mem_text = db.get_project_memory_context(proj_id, max_chars=3000, db_path=db_path)
        if mem_text:
            context_parts.append("--- قرارات ومتطلبات المشروع المعتمدة (Project Memory) ---\n" + mem_text)

    return "\n\n".join(context_parts)


def execute_single_task(
    task: Dict[str, Any],
    session: Dict[str, Any],
    call_agent_fn: Callable[..., str],
    agent_prompt_resolver_fn: Callable[[str], Tuple[str, str, str]],
    add_feed_fn: Optional[Callable] = None,
    db_path: Optional[str] = None
) -> Tuple[bool, Optional[str], Optional[str]]:
    """
    Execute a single task with honest status lifecycle:
    'ready' -> 'running' -> 'completed' | 'failed'
    Records agent run, logs handoffs, and saves project memory.
    """
    task_id = task["task_id"]
    session_id = session["session_id"]
    agent_id = task["assigned_agent_id"]
    stage = task["stage"]
    project_id = session.get("project_name", session_id)

    # 1. Transition task status to running
    db.update_task_status(task_id, "running", db_path=db_path)
    
    # Resolve agent metadata (file path, role name, color)
    prompt_file, role_name, color = agent_prompt_resolver_fn(agent_id)
    
    if add_feed_fn:
        add_feed_fn(role_name, agent_id, color, "WRK", f"بدأ العمل على مهمة: {task['title']}")

    # 2. Assemble context
    full_context = get_task_execution_context(session_id, task, db_path=db_path)

    # Resolve canonical short name expected by provider wrappers and test fixtures
    canonical_short_names = {
        "engineering-senior-developer": "DEV",
        "engineering-code-reviewer": "QA",
        "design-whimsy-injector": "MOTION",
        "marketing-seo-specialist": "SEO",
        "engineering-software-architect": "ENG",
        "design-ui-designer": "UI",
        "business-strategist": "BS",
        "product-manager": "PM",
    }
    short_name = canonical_short_names.get(agent_id)
    if not short_name:
        if stage in ("qa", "testing") or "qa" in agent_id or "reviewer" in agent_id or "sec" in agent_id:
            short_name = "QA"
        elif stage == "engineering" or task.get("division_id") == "engineering":
            short_name = "ENG"
        elif stage == "strategy" or task.get("division_id") == "specialized":
            short_name = "SPC"
        elif stage == "animation":
            short_name = "MOTION"
        elif stage == "seo":
            short_name = "SEO"
        else:
            short_name = agent_id[:3].upper()
    
    # 3. Execute agent call
    try:
        output = call_agent_fn(
            prompt_file,
            role_name,
            short_name,
            color,
            task.get("input_context", ""),
            full_context
        )
        
        if not output or not str(output).strip():
            raise RuntimeError(f"Agent {agent_id} returned empty or invalid output.")
            
        # 4. Mark task completed
        db.update_task_status(task_id, "completed", output_text=output, db_path=db_path)
        
        # 5. Record agent run
        tokens_est = max(1, (len(task.get("input_context") or "") + len(full_context or "") + len(output or "")) // 4)
        db.record_agent_run(
            task_id=task_id,
            session_id=session_id,
            agent_id=agent_id,
            agent_name=role_name,
            division_id=task["division_id"],
            prompt_file=prompt_file,
            status="completed",
            tokens_used=tokens_est,
            token_source="estimated",
            input_preview=full_context[:400],
            output_preview=output[:400],
            db_path=db_path
        )
        
        # 6. Record relevant project memory
        if stage == "product" and ("PRD" in output or "متطلبات" in output):
            db.add_project_memory(
                project_id=project_id,
                session_id=session_id,
                category="requirement",
                key="PRD_Approved_Requirements",
                content=output[:1500],
                provenance_agent=agent_id,
                provenance_task_id=task_id,
                db_path=db_path
            )
        elif stage == "engineering" and ("معمارية" in output or "architecture" in output.lower()):
            db.add_project_memory(
                project_id=project_id,
                session_id=session_id,
                category="architecture_decision",
                key="System_Architecture_Plan",
                content=output[:1500],
                provenance_agent=agent_id,
                provenance_task_id=task_id,
                db_path=db_path
            )
        elif stage == "qa" and ("ثغرة" in output or "vulnerability" in output.lower() or "audit" in output.lower()):
            db.add_project_memory(
                project_id=project_id,
                session_id=session_id,
                category="qa_finding",
                key=f"QA_Finding_{agent_id}",
                content=output[:1000],
                provenance_agent=agent_id,
                provenance_task_id=task_id,
                db_path=db_path
            )

        # 7. Record handoffs to all dependent downstream tasks
        all_tasks = db.list_tasks_for_session(session_id, db_path=db_path)
        for other in all_tasks:
            if task_id in other.get("dependencies", []):
                db.create_task_handoff(
                    session_id=session_id,
                    from_task_id=task_id,
                    to_task_id=other["task_id"],
                    from_agent_id=agent_id,
                    to_agent_id=other["assigned_agent_id"],
                    artifact_type=f"{stage}_deliverable",
                    artifact_summary=f"Handoff from {task['title']} to {other['title']}",
                    status="delivered",
                    db_path=db_path
                )

        if add_feed_fn:
            add_feed_fn(role_name, agent_id, color, "DONE", f"✅ أنجز بنجاح مهمة: {task['title']}")

        return True, output, None

    except Exception as e:
        err_msg = str(e)
        new_retry, can_retry = db.increment_task_retry(task_id, db_path=db_path)
        
        tokens_est = max(1, (len(task.get("input_context") or "") + len(full_context or "")) // 4)
        db.record_agent_run(
            task_id=task_id,
            session_id=session_id,
            agent_id=agent_id,
            agent_name=role_name,
            division_id=task["division_id"],
            prompt_file=prompt_file,
            status="failed",
            tokens_used=tokens_est,
            token_source="estimated",
            input_preview=full_context[:400],
            error_message=err_msg,
            db_path=db_path
        )
        
        if can_retry:
            db.update_task_status(task_id, "ready", error_message=f"Attempt {new_retry} failed: {err_msg}", db_path=db_path)
            if add_feed_fn:
                add_feed_fn("النظام", "Task Engine", "orange", "RETRY", f"إعادة محاولة تنفيذ {task['title']} (محاولة {new_retry}): {err_msg}")
        else:
            db.update_task_status(task_id, "failed", error_message=f"Exhausted retries: {err_msg}", db_path=db_path)
            if add_feed_fn:
                add_feed_fn("النظام", "Task Engine", "red", "FAIL", f"فشل تنفيذ مهمة {task['title']} نهائياً: {err_msg}")
                
        return False, None, err_msg


def run_dag_stages(
    session_id: str,
    allowed_stages: List[str],
    call_agent_fn: Callable[..., str],
    agent_prompt_resolver_fn: Callable[[str], Tuple[str, str, str]],
    add_feed_fn: Optional[Callable] = None,
    on_task_start_fn: Optional[Callable[[Dict[str, Any]], None]] = None,
    on_task_complete_fn: Optional[Callable[[Dict[str, Any], str], None]] = None,
    on_task_fail_fn: Optional[Callable[[Dict[str, Any], str], None]] = None,
    db_path: Optional[str] = None
) -> Tuple[bool, List[str], Optional[str]]:
    """
    Execute all ready tasks in a session that belong to allowed_stages
    following the Directed Acyclic Graph (DAG) dependencies.
    Dispatches tasks as dependencies become satisfied.
    Prevents duplicate claims via atomic database conditional locking.
    Returns (success: bool, completed_task_ids: List[str], error_message: Optional[str]).
    """
    session = db.get_session(session_id, db_path=db_path)
    if not session:
        return False, [], f"Session {session_id} not found."

    completed_ids: List[str] = []

    while True:
        all_tasks = db.list_tasks_for_session(session_id, db_path=db_path)
        stage_tasks = [t for t in all_tasks if t.get("stage") in allowed_stages]
        
        if not stage_tasks:
            return True, completed_ids, None

        # Check if any core task in allowed_stages is terminally failed
        failed_core_tasks = [t for t in stage_tasks if t.get("status") == "failed" and "_spec_" not in t.get("task_id", "")]
        if failed_core_tasks:
            err = failed_core_tasks[0].get("error_message") or "Prerequisite core task failed."
            return False, completed_ids, f"Task {failed_core_tasks[0].get('title')} failed: {err}"

        # Check if all tasks in allowed_stages are already completed (or specialist failed)
        uncompleted = [t for t in stage_tasks if t.get("status") not in ("completed", "failed")]
        if not uncompleted:
            return True, [t["task_id"] for t in stage_tasks if t.get("status") == "completed"], None

        # Claim the next ready task in allowed_stages
        claimed_task = db.claim_next_ready_task(session_id, allowed_stages=allowed_stages, db_path=db_path)
        if not claimed_task:
            # Check if any tasks in allowed_stages are currently running
            running_tasks = [t for t in stage_tasks if t.get("status") == "running"]
            if running_tasks:
                time.sleep(0.3)
                continue
                
            # Check if any tasks in allowed_stages are blocked
            blocked_tasks = [t for t in stage_tasks if t.get("status") == "blocked"]
            if blocked_tasks:
                return False, completed_ids, f"Execution halted: Tasks {', '.join([b['title'] for b in blocked_tasks])} are blocked."
            
            # Check if pending tasks exist but their prerequisite is outside allowed_stages
            pending_tasks = [t for t in stage_tasks if t.get("status") == "pending"]
            if pending_tasks:
                completed_overall = {t["task_id"] for t in all_tasks if t.get("status") == "completed"}
                waiting_on_external = False
                for pt in pending_tasks:
                    deps = pt.get("dependencies", [])
                    if any(d not in completed_overall for d in deps):
                        waiting_on_external = True
                        break
                if waiting_on_external:
                    return True, completed_ids, None
                    
            return False, completed_ids, "No task in allowed stages is ready to execute."

        # Task was claimed! Execute it.
        if on_task_start_fn:
            try:
                on_task_start_fn(claimed_task)
            except Exception as ex:
                logging.warning("on_task_start_fn error: %s", ex)

        success, output, err = execute_single_task(
            task=claimed_task,
            session=session,
            call_agent_fn=call_agent_fn,
            agent_prompt_resolver_fn=agent_prompt_resolver_fn,
            add_feed_fn=add_feed_fn,
            db_path=db_path
        )

        if success:
            completed_ids.append(claimed_task["task_id"])
            if on_task_complete_fn:
                try:
                    on_task_complete_fn(claimed_task, output or "")
                except Exception as ex:
                    logging.warning("on_task_complete_fn error: %s", ex)
        else:
            if on_task_fail_fn:
                try:
                    on_task_fail_fn(claimed_task, err or "Unknown error")
                except Exception as ex:
                    logging.warning("on_task_fail_fn error: %s", ex)
            
            latest_task = db.get_task(claimed_task["task_id"], db_path=db_path)
            if latest_task and latest_task.get("status") == "failed" and "_spec_" not in claimed_task.get("task_id", ""):
                return False, completed_ids, f"Task {claimed_task.get('title')} failed: {err}"


def create_remediation_task(
    session_id: str,
    original_task_id: str,
    qa_errors: List[str],
    candidate_code: Dict[str, str],
    attempt_num: int,
    db_path: Optional[str] = None
) -> str:
    """
    Dynamically inject a remediation task into the DAG for bounded self-healing.
    Assigned to the responsible agent (senior developer).
    """
    title = f"إصلاح أخطاء فحص الجودة (محاولة {attempt_num} من 2)"
    # Idempotency check: if remediation task already exists for this session/attempt, return existing ID
    existing_tasks = db.list_tasks_for_session(session_id, db_path=db_path)
    for t in existing_tasks:
        if t.get("title") == title:
            return t["task_id"]

    errors_str = "\n- ".join(qa_errors)
    input_ctx = (
        f"فحص الجودة البرمجية كشف عن الأخطاء التالية التي يجب إصلاحها فوراً:\n- {errors_str}\n\n"
        f"الأكواد الحالية:\n"
        f"```html\n{candidate_code.get('html', '')}\n```\n\n"
        f"```css\n{candidate_code.get('css', '')}\n```\n\n"
        f"```javascript\n{candidate_code.get('js', '')}\n```\n\n"
        f"المطلوب: قم بإصلاح الأخطاء المذكورة فوراً، وأرجع كود HTML الكامل المصحح داخل ```html وكود CSS داخل ```css وكود JS داخل ```javascript."
    )
    remediation_task_id = db.create_task(
        session_id=session_id,
        title=title,
        division_id="engineering",
        assigned_agent_id="engineering-senior-developer",
        stage="development",
        dependencies=[original_task_id],
        input_context=input_ctx,
        max_retries=1,
        db_path=db_path
    )
    return remediation_task_id

