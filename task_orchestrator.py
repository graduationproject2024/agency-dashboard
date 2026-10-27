import os
import json
import time
import re
import copy
import logging
import hashlib
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

MAX_WORKFLOW_TASKS = 1000

def detect_cycles(task_defs: Any) -> Tuple[bool, List[str]]:
    """
    Check for circular dependencies in a proposed task graph using DFS.
    task_defs: either a list of task dicts (with 'id' or 'task_id' and 'dependencies')
               or a dict mapping node -> list of dependencies.
    Resilient against non-dictionary elements, invalid types, and deep graphs.
    Returns (has_cycle: bool, cycle_path: List[str]).
    """
    adj = {}
    if isinstance(task_defs, list):
        for t in task_defs:
            if not isinstance(t, dict):
                continue
            raw_id = t.get("id") if "id" in t else t.get("task_id")
            if raw_id is None:
                continue
            tid = str(raw_id).strip()
            if not tid:
                continue
            raw_deps = t.get("dependencies")
            if isinstance(raw_deps, list):
                adj[tid] = [str(d).strip() for d in raw_deps if d is not None and str(d).strip()]
            else:
                adj[tid] = []
    elif isinstance(task_defs, dict):
        for k, v in task_defs.items():
            k_str = str(k).strip()
            if not k_str:
                continue
            if isinstance(v, list):
                adj[k_str] = [str(d).strip() for d in v if d is not None and str(d).strip()]
            else:
                adj[k_str] = []
    else:
        return False, []

    # Check for direct self-dependencies first
    for node, deps in adj.items():
        if node in deps:
            return True, [node, node]

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
        if visited.get(node) == 0:
            if dfs(node, []):
                return True, cycle_path

    return False, []


def validate_workflow_graph(task_defs: Any) -> Dict[str, Any]:
    """
    Validate a workflow graph before starting execution:
    1. Reject non-list collections and empty task list.
    2. Enforce complexity limits (<= 1000 tasks).
    3. Validate task dictionaries and non-empty, unique identifiers.
    4. Enforce valid dependency types and references.
    5. Detect self-dependencies.
    6. Detect circular dependencies and extract exact cycle path.
    7. Compute deterministic topological execution levels (for parallel execution stages).
    8. Validate input/output contract continuity between dependent tasks.
    """
    errors = []
    warnings = []

    if task_defs is None or not isinstance(task_defs, list):
        got_type = type(task_defs).__name__ if task_defs is not None else "None"
        return {
            "is_valid": False,
            "errors": [f"Task collection must be a list, got: {got_type}"],
            "warnings": [],
            "task_count": 0,
            "execution_levels": [],
            "has_cycles": False,
            "cycle_path": []
        }

    if len(task_defs) == 0:
        return {
            "is_valid": False,
            "errors": ["Task list cannot be empty"],
            "warnings": [],
            "task_count": 0,
            "execution_levels": [],
            "has_cycles": False,
            "cycle_path": []
        }

    if len(task_defs) > MAX_WORKFLOW_TASKS:
        errors.append(f"Workflow exceeds maximum task limit ({len(task_defs)} > {MAX_WORKFLOW_TASKS})")

    seen_ids = set()
    duplicate_ids = set()
    task_map = {}

    for idx, t in enumerate(task_defs):
        if not isinstance(t, dict):
            errors.append(f"Task definition at index {idx} must be a dictionary, got: {type(t).__name__}")
            continue
        raw_tid = t.get("id") if "id" in t else t.get("task_id")
        if raw_tid is None:
            errors.append(f"Task definition at index {idx} missing required 'id' or 'task_id'")
            continue
        tid_str = str(raw_tid).strip()
        if not tid_str:
            errors.append(f"Task definition at index {idx} has empty or whitespace identifier")
            continue
        if tid_str in seen_ids:
            duplicate_ids.add(tid_str)
        seen_ids.add(tid_str)
        task_map[tid_str] = t

    if duplicate_ids:
        errors.append(f"Duplicate task IDs detected: {sorted(list(duplicate_ids))}")

    # Dependency existence and type checks
    missing_deps = []
    for tid, t in task_map.items():
        if "dependencies" in t:
            deps = t.get("dependencies")
            if not isinstance(deps, list):
                errors.append(f"Task '{tid}' dependencies must be a list, got {type(deps).__name__}")
                continue
            for dep_item in deps:
                if dep_item is None or not isinstance(dep_item, (str, int)):
                    errors.append(f"Task '{tid}' contains invalid dependency value: {dep_item!r} (expected string or integer identifier)")
                    continue
                d_str = str(dep_item).strip()
                if not d_str:
                    errors.append(f"Task '{tid}' contains empty dependency identifier")
                    continue
                if d_str == tid:
                    errors.append(f"Self-dependency detected: task '{tid}' cannot depend on itself")
                    continue
                if d_str not in seen_ids:
                    missing_deps.append((tid, d_str))

    if missing_deps:
        for tid, dep in missing_deps:
            errors.append(f"Missing dependency reference: task '{tid}' depends on non-existent task '{dep}'")

    # Cycle detection: run ONLY on sanitized dictionary definitions
    valid_task_list = list(task_map.values())
    has_cycle, cycle_path = detect_cycles(valid_task_list)
    if has_cycle:
        errors.append(f"Cycle detected / Circular dependency detected in task graph: {' -> '.join(cycle_path)}")

    # Contract continuity checks
    for tid, t in task_map.items():
        req_contract = t.get("required_contract") or t.get("input_contract")
        if req_contract:
            deps = t.get("dependencies") or []
            if isinstance(deps, list):
                for d in deps:
                    dep_task = task_map.get(str(d).strip())
                    if dep_task and isinstance(dep_task, dict):
                        dep_output_contract = dep_task.get("output_contract")
                        if dep_output_contract and dep_output_contract != req_contract:
                            warnings.append(
                                f"Contract mismatch: task '{tid}' requires '{req_contract}', but dependency '{d}' produces '{dep_output_contract}'"
                            )

    # Compute topological execution levels if no cycles and no missing dependencies and no duplicates
    execution_levels = []
    if not has_cycle and not missing_deps and not duplicate_ids and len(errors) == 0:
        in_degree = {tid: 0 for tid in seen_ids}
        adj = {tid: [] for tid in seen_ids}
        for tid, t in task_map.items():
            raw_deps = t.get("dependencies") or []
            deps = [str(d).strip() for d in raw_deps if d is not None and str(d).strip()] if isinstance(raw_deps, list) else []
            in_degree[tid] = len(deps)
            for d in deps:
                if d in adj:
                    adj[d].append(tid)

        current_level = sorted([tid for tid, deg in in_degree.items() if deg == 0])
        remaining = set(seen_ids)

        while current_level:
            execution_levels.append(current_level)
            next_level = []
            for node in current_level:
                if node in remaining:
                    remaining.remove(node)
                for downstream in adj.get(node, []):
                    in_degree[downstream] -= 1
                    if in_degree[downstream] == 0:
                        next_level.append(downstream)
            current_level = sorted(next_level)

    is_valid = len(errors) == 0
    return {
        "is_valid": is_valid,
        "errors": errors,
        "warnings": warnings,
        "task_count": len(seen_ids),
        "execution_levels": execution_levels,
        "has_cycles": has_cycle,
        "cycle_path": cycle_path
    }


class WorkflowDAG:
    """Validated Directed Acyclic Graph wrapper for enterprise task orchestration."""
    def __init__(self, tasks: Any):
        self.raw_tasks = copy.deepcopy(tasks) if isinstance(tasks, (list, dict)) else []
        self.validation = validate_workflow_graph(tasks)
        if not self.validation["is_valid"]:
            raise ValueError(f"Invalid workflow task graph: {'; '.join(self.validation['errors'])}")
        self.task_map = {
            str(t.get("id") if "id" in t else t.get("task_id")).strip(): t
            for t in self.raw_tasks
            if isinstance(t, dict) and (t.get("id") or t.get("task_id"))
        }

    @property
    def is_valid(self) -> bool:
        return self.validation["is_valid"]

    @property
    def execution_levels(self) -> List[List[str]]:
        return self.validation["execution_levels"]

    def get_ready_tasks(self, completed_ids: set) -> List[Dict[str, Any]]:
        ready = []
        for tid, t in self.task_map.items():
            deps = set(t.get("dependencies") or [])
            if deps.issubset(completed_ids) and t.get("status") in ("ready", "pending"):
                ready.append(t)
        return ready

    def get_downstream_dependents(self, task_id: str) -> List[str]:
        """Transitively discover all downstream tasks depending on task_id."""
        dependents = set()
        queue = [task_id]
        while queue:
            curr = queue.pop(0)
            for tid, t in self.task_map.items():
                if curr in (t.get("dependencies") or []):
                    if tid not in dependents:
                        dependents.add(tid)
                        queue.append(tid)
        return sorted(list(dependents))


def fail_task_and_propagate(
    task_id: str,
    error_message: str,
    session_id: Optional[str] = None,
    db_path: Optional[str] = None
) -> List[str]:
    """
    Mark target task as failed, and transitively propagate failure to all downstream
    dependent tasks by updating their status to 'blocked'.
    Returns list of newly blocked task IDs.
    """
    blocked_ids = []
    with db.get_db_connection(db_path) as conn:
        conn.execute("""
            UPDATE workflow_tasks
            SET status = 'failed',
                error_message = ?,
                completed_at = COALESCE(completed_at, ?)
            WHERE task_id = ?;
        """, (db.sanitize_memory_content(error_message), time.time(), task_id))

        sess_clause = "session_id = (SELECT session_id FROM workflow_tasks WHERE task_id = ?)" if not session_id else "session_id = ?"
        sess_param = task_id if not session_id else session_id
        rows = conn.execute(f"SELECT task_id, status, dependencies_json FROM workflow_tasks WHERE {sess_clause};", (sess_param,)).fetchall()
        
        all_tasks = {r["task_id"]: {"status": r["status"], "dependencies": json.loads(r["dependencies_json"] or "[]")} for r in rows}

        queue = [task_id]
        while queue:
            curr = queue.pop(0)
            for tid, info in all_tasks.items():
                if curr in info["dependencies"]:
                    if info["status"] not in ("completed", "failed", "blocked", "cancelled"):
                        conn.execute("""
                            UPDATE workflow_tasks
                            SET status = 'blocked',
                                error_message = ?
                            WHERE task_id = ?;
                        """, (f"Blocked by upstream prerequisite task failure ({task_id}): {db.sanitize_memory_content(error_message)}", tid))
                        info["status"] = "blocked"
                        blocked_ids.append(tid)
                    if tid not in queue:
                        queue.append(tid)
        conn.commit()
    return blocked_ids


# =========================================================================
# Canonical Agent Capability Registry & Deterministic Assignment
# =========================================================================

_CAPABILITY_REGISTRY_CACHE: Dict[str, Dict[str, Any]] = {}

def parse_agent_capabilities(
    agent_id: str,
    raw_content: str = "",
    metadata: Optional[Dict[str, Any]] = None,
    prompt_file: Optional[str] = None
) -> Dict[str, Any]:
    """
    Parse canonical capability profile from agent definition and frontmatter.
    Populates only verified metadata. Unknown fields remain explicitly unknown/empty.
    """
    meta = metadata or {}
    name = meta.get("name") or agent_id.replace('-', ' ').title()
    desc = meta.get("description") or ""

    division_id = (meta.get("division_id") or meta.get("division") or meta.get("department") or "").lower()
    if not division_id and "-" in agent_id:
        division_id = agent_id.split("-")[0].lower()
    if not division_id:
        division_id = "specialized"

    subdept = meta.get("subdepartment") or meta.get("department")

    role = "unknown"
    if raw_content:
        for line in raw_content.splitlines():
            clean = line.strip()
            if (clean.lower().startswith("- **role**:") or 
                clean.lower().startswith("**role**:") or 
                clean.lower().startswith("role:") or 
                clean.lower().startswith("- role:")):
                role = clean.split(":", 1)[1].strip().strip("* ")
                break
    if role == "unknown" and desc:
        role = desc.split(".")[0].strip()

    seniority = "unknown"
    check_str = f"{name} {role} {agent_id}".lower()
    if any(k in check_str for k in ("chief", "head of", "principal", "lead")):
        seniority = "lead"
    elif any(k in check_str for k in ("senior", "sr.", "sr ", "architect", "staff")):
        seniority = "senior"
    elif any(k in check_str for k in ("specialist", "expert", "consultant", "strategist", "manager")):
        seniority = "specialist"
    elif any(k in check_str for k in ("junior", "jr.", "associate")):
        seniority = "junior"

    responsibilities = []
    if raw_content:
        in_mission = False
        for line in raw_content.splitlines():
            if "###" in line or "##" in line:
                if any(h in line.lower() for h in ("mission", "responsibilities", "excellence", "duties")):
                    in_mission = True
                elif in_mission and ("rules" in line.lower() or "identity" in line.lower()):
                    in_mission = False
            elif in_mission and line.strip().startswith("- "):
                resp = line.strip()[2:].strip()
                if resp and len(resp) > 5 and resp not in responsibilities:
                    responsibilities.append(resp[:120])
                if len(responsibilities) >= 8:
                    break

    text_corpus = f"{name} {role} {desc} {agent_id} {raw_content}".lower()
    capabilities_set = set()

    div_caps = {
        "academic": ["academic_research", "domain_analysis", "literature_review"],
        "engineering": ["software_engineering", "code_implementation"],
        "testing": ["qa_evaluation", "syntax_verification", "test_automation", "code_review"],
        "security": ["security_audit", "vulnerability_assessment", "access_control"],
        "design": ["ui_design", "ux_architecture", "visual_identity"],
        "product": ["requirements_gathering", "prd_authoring", "roadmap_planning"],
        "specialized": ["business_strategy", "feasibility_analysis"],
        "marketing": ["seo_optimization", "content_strategy"],
        "paid-media": ["campaign_management", "ad_analytics"],
        "finance": ["budget_management", "financial_modeling", "cost_accounting"],
        "game-development": ["game_mechanics", "interactive_systems"],
        "gis": ["spatial_analysis", "cartography", "geo_modeling"],
        "healthcare": ["clinical_compliance", "health_informatics"],
        "project-management": ["workflow_orchestration", "delivery_management"],
        "research": ["market_intelligence", "user_research"],
        "sales": ["sales_pipeline", "revenue_strategy"],
        "spatial-computing": ["spatial_computing", "3d_interface_design"],
        "support": ["customer_support", "incident_triage"]
    }
    for c in div_caps.get(division_id, []):
        capabilities_set.add(c)

    keyword_map = {
        "backend": "backend_architecture",
        "database": "database_design",
        "api": "api_design",
        "frontend": "frontend_development",
        "review": "code_review",
        "security": "security_audit",
        "test": "test_automation",
        "architect": "system_architecture",
        "mobile": "mobile_development",
        "devops": "devops_automation",
        "cloud": "cloud_infrastructure",
        "animation": "animation_design",
        "seo": "seo_optimization",
        "strategy": "business_strategy",
        "prd": "prd_authoring",
        "remediation": "code_remediation"
    }
    for kw, cap in keyword_map.items():
        if kw in text_corpus:
            capabilities_set.add(cap)
    if "system_architecture" in capabilities_set:
        capabilities_set.add("architecture")

    raw_tools = meta.get("tools") or []
    if isinstance(raw_tools, str):
        tools_list = [t.strip() for t in raw_tools.split(",") if t.strip()]
    elif isinstance(raw_tools, list):
        tools_list = [str(t).strip() for t in raw_tools if str(t).strip()]
    else:
        tools_list = []

    if division_id in ("security",):
        permissions = ["read_artifacts", "audit_code", "flag_vulnerabilities"]
    elif division_id in ("engineering", "design"):
        permissions = ["read_artifacts", "write_code"]
    elif division_id in ("finance",):
        permissions = ["manage_budget", "financial_reconciliation"]
    elif division_id in ("project-management",):
        permissions = ["orchestrate_workflow", "verify_release"]
    else:
        permissions = ["read_artifacts"]

    is_qa = division_id in ("testing",) or any(k in text_corpus for k in ("code reviewer", "qa", "quality assurance", "audit"))
    qa_resp = ["code_review", "syntax_validation", "artifact_integrity"] if is_qa else []
    is_high_risk = division_id in ("security", "finance") or any(k in text_corpus for k in ("deploy", "release", "administrator", "billing"))

    status = "available" if (prompt_file and os.path.exists(prompt_file)) else "available"

    return {
        "agent_id": agent_id,
        "name": name,
        "division_id": division_id,
        "subdepartment": subdept,
        "role": role,
        "seniority": seniority,
        "responsibilities": responsibilities,
        "capabilities": sorted(list(capabilities_set)),
        "input_contracts": ["requirements_spec", "task_context"] if division_id != "testing" else ["candidate_code", "test_suite"],
        "output_contracts": ["code_deliverable"] if division_id == "engineering" else (["qa_findings", "qa_report"] if is_qa else ["context_document"]),
        "required_context": ["task_description", "upstream_outputs"],
        "produced_artifacts": ["source_code"] if division_id == "engineering" else (["audit_report"] if is_qa else ["document"]),
        "supported_task_types": sorted(list(capabilities_set)),
        "dependencies": [],
        "handoff_targets": [],
        "required_tools": tools_list,
        "security_permissions": permissions,
        "qa_responsibilities": qa_resp,
        "failure_policy": {"max_retries": 2, "retryable_errors": ["timeout", "rate_limit", "connection_error"]},
        "approval_required": is_high_risk,
        "status": status,
        "prompt_file": prompt_file
    }

def get_agent_capability_registry(agents_dir: Optional[str] = None) -> Dict[str, Dict[str, Any]]:
    """Build and cache the canonical agent capability registry."""
    global _CAPABILITY_REGISTRY_CACHE
    if _CAPABILITY_REGISTRY_CACHE and not agents_dir:
        return _CAPABILITY_REGISTRY_CACHE

    from app import discover_agents, get_agents_dir
    target_dir = agents_dir or get_agents_dir()
    inv = discover_agents(agents_dir=target_dir)

    registry = {}
    for a in inv.get("agents", []):
        aid = a["id"]
        rel_p = a.get("relative_path")
        full_p = os.path.join(target_dir, rel_p) if (target_dir and rel_p) else None
        content = ""
        if full_p and os.path.exists(full_p):
            try:
                with open(full_p, "r", encoding="utf-8", errors="replace") as fp:
                    content = fp.read()
            except OSError:
                pass
        profile = parse_agent_capabilities(aid, content, metadata=a, prompt_file=full_p)
        registry[aid] = profile

    if not agents_dir:
        _CAPABILITY_REGISTRY_CACHE = registry
    return registry

def get_agent_capabilities(agent_id: str, agents_dir: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Retrieve capability profile for a single agent."""
    reg = get_agent_capability_registry(agents_dir=agents_dir)
    return reg.get(agent_id)

def assign_agent_to_task(
    task_requirements: Dict[str, Any],
    candidates: Optional[List[Dict[str, Any]]] = None,
    agents_dir: Optional[str] = None,
    db_path: Optional[str] = None
) -> Dict[str, Any]:
    """
    Deterministic capability-based task assignment mechanism.
    Evaluates requirements against candidate capabilities and attributes.
    Returns:
    {
        "selected_agent_id": Optional[str],
        "assigned": bool,
        "score": float,
        "reason": str,
        "matched_capabilities": List[str],
        "candidate_count": int,
        "evaluated_candidates": int
    }
    """
    req_caps = [c.lower().strip() for c in (task_requirements.get("required_capabilities") or [])]
    pref_div = (task_requirements.get("preferred_division") or "").lower().strip()
    req_tools = set(t.lower().strip() for t in (task_requirements.get("required_tools") or []))
    req_perms = set(p.lower().strip() for p in (task_requirements.get("required_permissions") or []))
    min_seniority = (task_requirements.get("min_seniority") or "").lower().strip()
    exclude_ids = set(task_requirements.get("exclude_agent_ids") or [])
    approval_constrained = bool(task_requirements.get("approval_constrained", False))

    seniority_ranks = {"lead": 3, "senior": 2, "specialist": 1, "junior": 0, "unknown": 0}
    min_rank = seniority_ranks.get(min_seniority, 0)

    if candidates is None:
        reg = get_agent_capability_registry(agents_dir=agents_dir)
        pool = list(reg.values())
    else:
        pool = []
        for c in candidates:
            if isinstance(c, dict) and "capabilities" in c:
                pool.append(c)
            elif isinstance(c, dict) and ("id" in c or "agent_id" in c):
                cid = c.get("agent_id") or c.get("id")
                pool.append(parse_agent_capabilities(cid, "", c))

    evaluated_candidates = len(pool)
    scored_candidates = []

    for cand in pool:
        cid = cand.get("agent_id") or cand.get("id")
        if not cid or cid in exclude_ids:
            continue
        if cand.get("status") not in ("available", None):
            continue
        if approval_constrained and cand.get("approval_required", False):
            continue

        cand_perms = set(p.lower() for p in cand.get("security_permissions", []))
        if req_perms and not req_perms.issubset(cand_perms):
            continue

        cand_tools = set(t.lower() for t in cand.get("required_tools", []))
        if req_tools and not req_tools.issubset(cand_tools):
            continue

        cand_sen = cand.get("seniority", "unknown")
        if min_seniority and seniority_ranks.get(cand_sen, 0) < min_rank:
            continue

        cand_caps = set(c.lower() for c in cand.get("capabilities", []))

        def _cap_match(rc: str, cc_set: set) -> bool:
            if rc in cc_set:
                return True
            return any(rc in c or c in rc for c in cc_set)

        matched = [c for c in req_caps if _cap_match(c, cand_caps)]
        
        # If required_capabilities specified, must match ALL required capabilities
        if req_caps:
            if len(matched) < len(req_caps):
                continue

        score = len(matched) * 10
        div_match = (cand.get("division_id") == pref_div) if pref_div else False
        if div_match:
            score += 5
        score += seniority_ranks.get(cand_sen, 0)

        scored_candidates.append({
            "agent": cand,
            "score": score,
            "matched_capabilities": matched,
            "div_match": div_match
        })

    if not scored_candidates:
        return {
            "selected_agent_id": None,
            "assigned": False,
            "score": 0,
            "reason": f"No suitable agent found satisfying required capabilities: {req_caps}" if req_caps else "No suitable agent found meeting requirements",
            "matched_capabilities": [],
            "candidate_count": 0,
            "evaluated_candidates": evaluated_candidates
        }

    scored_candidates.sort(key=lambda x: (-x["score"], x["agent"]["agent_id"]))
    best = scored_candidates[0]
    best_agent = best["agent"]

    reason_parts = [f"Satisfied required capabilities ({len(best['matched_capabilities'])} matched: {best['matched_capabilities']})"]
    if best["div_match"]:
        reason_parts.append(f"affinity with division '{pref_div}'")
    reason_parts.append(f"seniority tier '{best_agent.get('seniority', 'unknown')}'")
    reason_str = "; ".join(reason_parts) + f". Selected from {len(scored_candidates)} qualified candidates."

    return {
        "selected_agent_id": best_agent["agent_id"],
        "assigned": True,
        "score": best["score"],
        "reason": reason_str,
        "matched_capabilities": best["matched_capabilities"],
        "candidate_count": len(scored_candidates),
        "evaluated_candidates": evaluated_candidates
    }


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

    # Prevent duplicate execution: check if task is already completed
    existing_task = db.get_task(task_id, db_path=db_path)
    if existing_task and existing_task.get("status") == "completed":
        logging.info("Task %s is already completed. Skipping duplicate execution.", task_id)
        return True, existing_task.get("output_text"), None

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
        # Pre-flight durable marker: records that provider call is in-flight
        db.record_agent_run_start(
            task_id=task_id,
            session_id=session_id,
            agent_id=agent_id,
            agent_name=role_name,
            division_id=task["division_id"],
            prompt_file=prompt_file,
            input_preview=full_context[:400],
            db_path=db_path
        )

        output_res = call_agent_fn(
            prompt_file,
            role_name,
            short_name,
            color,
            task.get("input_context", ""),
            full_context
        )
        
        # Propagate provider usage metadata if returned directly or via provider execution context
        tokens_used = None
        token_source = "estimated"
        provider_request_id = None
        provider = None
        model = None

        if isinstance(output_res, tuple):
            if len(output_res) == 0:
                output = ""
            elif len(output_res) == 1:
                output = output_res[0]
            elif len(output_res) == 2:
                output = output_res[0]
                if isinstance(output_res[1], dict):
                    m = output_res[1]
                    tokens_used = m.get("tokens_used") or m.get("tokens")
                    token_source = m.get("token_source", "provider_reported" if tokens_used else "estimated")
                    provider_request_id = m.get("provider_request_id")
                    provider = m.get("provider")
                    model = m.get("model")
                elif isinstance(output_res[1], (int, float)):
                    tokens_used = int(output_res[1])
                    token_source = "provider_reported"
            elif len(output_res) >= 3:
                output = output_res[0]
                tokens_used = output_res[1]
                token_source = output_res[2]
                if len(output_res) >= 4:
                    provider_request_id = output_res[3]
        elif isinstance(output_res, dict):
            output = output_res.get("output") or output_res.get("content") or output_res.get("text") or ""
            tokens_used = output_res.get("tokens_used") or output_res.get("tokens")
            token_source = output_res.get("token_source")
            provider_request_id = output_res.get("provider_request_id")
            provider = output_res.get("provider")
            model = output_res.get("model")
            if token_source is None:
                token_source = "provider_reported" if tokens_used is not None else "estimated"
        else:
            output = output_res

        try:
            from app import get_last_agent_execution
            meta = get_last_agent_execution()
            if meta:
                if tokens_used is None and meta.get("tokens_used") is not None:
                    tokens_used = meta["tokens_used"]
                    token_source = meta.get("token_source", "provider_reported")
                if provider_request_id is None:
                    provider_request_id = meta.get("provider_request_id")
                if provider is None:
                    provider = meta.get("provider")
                if model is None:
                    model = meta.get("model")
        except Exception:
            pass
        
        if not output or not str(output).strip():
            raise RuntimeError(f"Agent {agent_id} returned empty or invalid output.")

        if tokens_used is None:
            tokens_used = max(1, (len(task.get("input_context") or "") + len(full_context or "") + len(output or "")) // 4)
            token_source = "estimated"

        # 4 & 5. Atomically complete task AND persist durable agent run in a single transaction
        db.complete_task_with_agent_run(
            task_id=task_id,
            session_id=session_id,
            agent_id=agent_id,
            agent_name=role_name,
            division_id=task["division_id"],
            prompt_file=prompt_file,
            output_text=output,
            tokens_used=tokens_used,
            token_source=token_source,
            provider_request_id=provider_request_id,
            provider=provider,
            model=model,
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
                    contract_version="1.0",
                    trace_id=f"trc_{session_id[:8]}_{task_id[:8]}",
                    validation_outcome="valid",
                    required_context={"upstream_task_id": task_id, "stage": stage},
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
            fail_task_and_propagate(task_id, f"Exhausted retries: {err_msg}", session_id=session_id, db_path=db_path)
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
    Uses deterministic task IDs and atomic database upsert to prevent duplicate tasks under concurrency.
    """
    title = f"إصلاح أخطاء فحص الجودة (محاولة {attempt_num} من 2)"
    h = hashlib.sha256(f"{session_id}_{original_task_id}_{attempt_num}".encode("utf-8")).hexdigest()[:12]
    remed_task_id = f"task_remed_{h}"

    with db.get_db_connection(db_path) as conn:
        existing = conn.execute(
            "SELECT task_id FROM workflow_tasks WHERE task_id = ? OR (session_id = ? AND title = ?);",
            (remed_task_id, session_id, title)
        ).fetchone()
        if existing:
            return existing["task_id"]

        errors_str = "\n- ".join(qa_errors)
        input_ctx = (
            f"فحص الجودة البرمجية كشف عن الأخطاء التالية التي يجب إصلاحها فوراً:\n- {errors_str}\n\n"
            f"الأكواد الحالية:\n"
            f"```html\n{candidate_code.get('html', '')}\n```\n\n"
            f"```css\n{candidate_code.get('css', '')}\n```\n\n"
            f"```javascript\n{candidate_code.get('js', '')}\n```\n\n"
            f"المطلوب: قم بإصلاح الأخطاء المذكورة فوراً، وأرجع كود HTML الكامل المصحح داخل ```html وكود CSS داخل ```css وكود JS داخل ```javascript."
        )
        deps_json = json.dumps([original_task_id])
        conn.execute("""
            INSERT OR IGNORE INTO workflow_tasks (
                task_id, session_id, parent_task_id, title, division_id,
                assigned_agent_id, stage, status, dependencies_json, input_context, max_retries
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
        """, (
            remed_task_id, session_id, original_task_id, title, "engineering",
            "engineering-senior-developer", "development", "ready", deps_json, input_ctx, 1
        ))
        conn.commit()

    return remed_task_id

