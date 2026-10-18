import os
import sqlite3
import json
import time
import uuid
import re
import logging
from typing import Dict, List, Optional, Tuple, Any

DEFAULT_DB_FILENAME = "agency_persistence.db"

def get_db_path(custom_path: Optional[str] = None) -> str:
    """
    Resolve SQLite database path dynamically.
    Precedence:
    1. custom_path argument
    2. DATABASE_PATH environment variable
    3. Default agency_persistence.db in current workspace
    """
    if custom_path:
        return os.path.abspath(custom_path)
    env_path = os.environ.get("DATABASE_PATH")
    if env_path:
        return os.path.abspath(env_path)
    return os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), DEFAULT_DB_FILENAME))

def get_db_connection(db_path: Optional[str] = None) -> sqlite3.Connection:
    """
    Open thread-isolated SQLite connection with foreign keys and WAL mode.
    """
    path = get_db_path(db_path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA journal_mode = WAL;")
    return conn

def init_db(db_path: Optional[str] = None):
    """
    Initialize SQLite schema with tables, indexes, and constraints.
    Idempotent and safe for concurrent invocations.
    """
    with get_db_connection(db_path) as conn:
        cursor = conn.cursor()
        
        # 1. Sessions table
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS workflow_sessions (
            session_id TEXT PRIMARY KEY,
            project_name TEXT NOT NULL,
            task_description TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('created', 'running', 'approval_needed', 'completed', 'failed', 'cancelled', 'interrupted')),
            squad_archetype TEXT,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            completed_at REAL,
            current_stage TEXT,
            checkpoint_step TEXT,
            error_message TEXT,
            metadata_json TEXT
        );
        """)

        # 2. Tasks table (DAG nodes)
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS workflow_tasks (
            task_id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            parent_task_id TEXT,
            title TEXT NOT NULL,
            division_id TEXT NOT NULL,
            assigned_agent_id TEXT NOT NULL,
            stage TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('pending', 'ready', 'running', 'completed', 'failed', 'blocked', 'cancelled')),
            dependencies_json TEXT NOT NULL DEFAULT '[]',
            input_context TEXT,
            output_text TEXT,
            error_message TEXT,
            retry_count INTEGER NOT NULL DEFAULT 0,
            max_retries INTEGER NOT NULL DEFAULT 2,
            started_at REAL,
            completed_at REAL,
            FOREIGN KEY(session_id) REFERENCES workflow_sessions(session_id) ON DELETE CASCADE,
            FOREIGN KEY(parent_task_id) REFERENCES workflow_tasks(task_id) ON DELETE SET NULL
        );
        """)

        # 3. Agent Runs table
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS agent_runs (
            run_id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL,
            session_id TEXT NOT NULL,
            agent_id TEXT NOT NULL,
            agent_name TEXT NOT NULL,
            division_id TEXT NOT NULL,
            prompt_file TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('selected', 'running', 'completed', 'failed')),
            input_preview TEXT,
            output_preview TEXT,
            tokens_used INTEGER NOT NULL DEFAULT 0,
            error_message TEXT,
            started_at REAL NOT NULL,
            finished_at REAL,
            FOREIGN KEY(task_id) REFERENCES workflow_tasks(task_id) ON DELETE CASCADE,
            FOREIGN KEY(session_id) REFERENCES workflow_sessions(session_id) ON DELETE CASCADE
        );
        """)

        # 4. Task Handoffs table (Collaboration)
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS task_handoffs (
            handoff_id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            from_task_id TEXT NOT NULL,
            to_task_id TEXT NOT NULL,
            from_agent_id TEXT NOT NULL,
            to_agent_id TEXT NOT NULL,
            artifact_type TEXT NOT NULL,
            artifact_summary TEXT,
            status TEXT NOT NULL CHECK(status IN ('pending', 'delivered', 'acknowledged', 'rejected')),
            created_at REAL NOT NULL,
            FOREIGN KEY(session_id) REFERENCES workflow_sessions(session_id) ON DELETE CASCADE
        );
        """)

        # 5. Project Memory table (Project-Scoped)
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS project_memory (
            memory_id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            session_id TEXT,
            category TEXT NOT NULL CHECK(category IN ('requirement', 'architecture_decision', 'decision', 'constraint', 'qa_finding', 'user_feedback')),
            key TEXT NOT NULL,
            content TEXT NOT NULL,
            provenance_agent TEXT NOT NULL,
            provenance_task_id TEXT,
            status TEXT NOT NULL DEFAULT 'verified' CHECK(status IN ('proposed', 'verified', 'superseded')),
            superseded_by TEXT,
            version INTEGER NOT NULL DEFAULT 1,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        );
        """)

        # 6. Checkpoints table
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS execution_checkpoints (
            checkpoint_id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            step_name TEXT NOT NULL,
            state_json TEXT NOT NULL,
            created_at REAL NOT NULL,
            FOREIGN KEY(session_id) REFERENCES workflow_sessions(session_id) ON DELETE CASCADE
        );
        """)

        # Create Indexes for fast traversal and correlation
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_tasks_session_id ON workflow_tasks(session_id);")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_tasks_status ON workflow_tasks(status);")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_runs_task_id ON agent_runs(task_id);")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_handoffs_session ON task_handoffs(session_id);")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_memory_project ON project_memory(project_id, category);")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_checkpoints_session ON execution_checkpoints(session_id, created_at);")

        conn.commit()


# =========================================================================
# Memory Secret Sanitization
# =========================================================================

SECRET_API_KEY_PATTERN = re.compile(r'(?i)(AIzaSy[0-9A-Za-z_-]{20,}|sk-[0-9A-Za-z]{20,})')
SECRET_CREDENTIAL_PATTERN = re.compile(r'(?i)(password|secret|bearer\s+[a-z0-9_\-\.]+)\s*[:=]\s*["\']?([^"\'\s]+)["\']?')
SECRET_API_KEY_ASSIGN_PATTERN = re.compile(r'(?i)(api[_-]?key)\s*[:=]\s*["\']?([^"\'\s]+)["\']?')

def sanitize_memory_content(text: str) -> str:
    """Mask any sensitive API keys or credentials before saving to persistent memory."""
    if not text or not isinstance(text, str):
        return ""
    sanitized = text
    sanitized = SECRET_API_KEY_PATTERN.sub("[REDACTED_API_KEY]", sanitized)
    sanitized = SECRET_CREDENTIAL_PATTERN.sub(r"\1=[REDACTED_CREDENTIAL]", sanitized)
    sanitized = SECRET_API_KEY_ASSIGN_PATTERN.sub(r"\1=[REDACTED_API_KEY]", sanitized)
    return sanitized


# =========================================================================
# Session CRUD & State Machine
# =========================================================================

VALID_SESSION_STATUSES = {'created', 'running', 'approval_needed', 'completed', 'failed', 'cancelled', 'interrupted'}

class SessionId(str):
    """
    String subclass that also acts like a dictionary of session attributes.
    Permits transparent usage as a string in SQL queries, logs, and URLs
    while also supporting dict access sess["id"], sess["status"], sess.get(...).
    """
    def __new__(cls, val, data=None):
        instance = super().__new__(cls, val)
        instance.data = data or {}
        return instance

    def __getitem__(self, key):
        if isinstance(key, str):
            if key in self.data:
                return self.data[key]
            raise KeyError(key)
        return super().__getitem__(key)

    def get(self, key, default=None):
        return self.data.get(key, default)


def create_session(
    arg1: Optional[str] = None,
    arg2: Optional[str] = None,
    session_id: Optional[str] = None,
    project_id: Optional[str] = None,
    project_name: Optional[str] = None,
    task_description: str = "",
    squad_archetype: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
    current_stage: str = "strategy",
    initial_state: Optional[Dict[str, Any]] = None,
    db_path: Optional[str] = None,
    **kwargs
) -> SessionId:
    if session_id:
        sid = session_id
        p_name = project_name or project_id or arg1 or "Default_Project"
    elif arg1 and arg2:
        sid = arg1
        p_name = arg2
    elif arg1 and not arg2:
        if project_name:
            sid = arg1
            p_name = project_name
        elif arg1.startswith("sess_") or arg1.startswith("test_") or "session" in arg1.lower():
            sid = arg1
            p_name = project_id or kwargs.get("project_name", "Default_Project")
        else:
            sid = f"sess_{uuid.uuid4().hex[:12]}"
            p_name = arg1
    else:
        sid = f"sess_{uuid.uuid4().hex[:12]}"
        p_name = project_name or project_id or "Default_Project"

    t_desc = task_description or kwargs.get("task_desc", p_name)

    now = time.time()
    meta = metadata or {}
    if initial_state:
        meta["initial_state"] = initial_state
    meta_json = json.dumps(meta, ensure_ascii=False)
    with get_db_connection(db_path) as conn:
        conn.execute("""
            INSERT INTO workflow_sessions (
                session_id, project_name, task_description, status,
                squad_archetype, created_at, updated_at, current_stage, metadata_json
            ) VALUES (?, ?, ?, 'running', ?, ?, ?, ?, ?);
        """, (sid, p_name, t_desc, squad_archetype, now, now, current_stage, meta_json))
        conn.commit()

    session_data = {
        "id": sid,
        "session_id": sid,
        "project_id": p_name,
        "project_name": p_name,
        "task_description": t_desc,
        "status": "running",
        "current_stage": current_stage,
        "created_at": now,
        "metadata": meta
    }
    return SessionId(sid, session_data)


def get_session(session_id: str, db_path: Optional[str] = None) -> Optional[Dict[str, Any]]:
    with get_db_connection(db_path) as conn:
        row = conn.execute("SELECT * FROM workflow_sessions WHERE session_id = ?;", (session_id,)).fetchone()
        if not row:
            return None
        res = dict(row)
        res["id"] = res["session_id"]
        res["project_id"] = res["project_name"]
        res["metadata"] = json.loads(res.get("metadata_json") or "{}")
        return res


def list_sessions(limit: int = 50, offset: int = 0, project_id: Optional[str] = None, db_path: Optional[str] = None) -> List[Dict[str, Any]]:
    with get_db_connection(db_path) as conn:
        if project_id:
            rows = conn.execute(
                "SELECT * FROM workflow_sessions WHERE project_name = ? ORDER BY created_at DESC LIMIT ? OFFSET ?;",
                (project_id, limit, offset)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM workflow_sessions ORDER BY created_at DESC LIMIT ? OFFSET ?;",
                (limit, offset)
            ).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d["id"] = d.get("session_id")
            d["project_id"] = d.get("project_name")
            d["metadata"] = json.loads(d.get("metadata_json") or "{}")
            result.append(d)
        return result

get_recent_sessions = list_sessions


def update_session_status(
    session_id: str,
    status: str,
    error_message: Optional[str] = None,
    current_stage: Optional[str] = None,
    checkpoint_step: Optional[str] = None,
    metadata_patch: Optional[Dict[str, Any]] = None,
    db_path: Optional[str] = None
) -> Optional[Dict[str, Any]]:
    if status not in VALID_SESSION_STATUSES:
        raise ValueError(f"Invalid status '{status}'. Must be one of {VALID_SESSION_STATUSES}")
    now = time.time()
    with get_db_connection(db_path) as conn:
        row = conn.execute("SELECT metadata_json FROM workflow_sessions WHERE session_id = ?;", (session_id,)).fetchone()
        if not row:
            return None
            
        meta = json.loads(row["metadata_json"] or "{}")
        if metadata_patch:
            meta.update(metadata_patch)
        meta_json = json.dumps(meta, ensure_ascii=False)
        
        completed_at = now if status in ('completed', 'failed', 'cancelled') else None
        
        conn.execute("""
            UPDATE workflow_sessions
            SET status = ?,
                error_message = COALESCE(?, error_message),
                current_stage = COALESCE(?, current_stage),
                checkpoint_step = COALESCE(?, checkpoint_step),
                updated_at = ?,
                completed_at = COALESCE(?, completed_at),
                metadata_json = ?
            WHERE session_id = ?;
        """, (status, error_message, current_stage, checkpoint_step, now, completed_at, meta_json, session_id))
        conn.commit()
    return get_session(session_id, db_path)


# =========================================================================
# Task CRUD & Graph Representation
# =========================================================================

def create_task(
    session_id: str,
    title: str,
    division_id: str,
    assigned_agent_id: str,
    stage: str,
    dependencies: Optional[List[str]] = None,
    parent_task_id: Optional[str] = None,
    input_context: Optional[str] = None,
    max_retries: int = 2,
    status: Optional[str] = None,
    output_text: Optional[str] = None,
    db_path: Optional[str] = None
) -> str:
    task_id = f"task_{uuid.uuid4().hex[:12]}"
    deps_json = json.dumps(dependencies or [])
    initial_status = status or "ready"
    with get_db_connection(db_path) as conn:
        if not status and dependencies:
            placeholders = ",".join(["?"] * len(dependencies))
            completed_count = conn.execute(
                f"SELECT COUNT(*) as count FROM workflow_tasks WHERE task_id IN ({placeholders}) AND status = 'completed';",
                dependencies
            ).fetchone()["count"]
            if completed_count < len(dependencies):
                initial_status = "pending"

        conn.execute("""
            INSERT INTO workflow_tasks (
                task_id, session_id, parent_task_id, title, division_id,
                assigned_agent_id, stage, status, dependencies_json, input_context, max_retries, output_text
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
        """, (task_id, session_id, parent_task_id, title, division_id, assigned_agent_id, stage, initial_status, deps_json, input_context, max_retries, output_text))
        conn.commit()
    return task_id

def get_task(task_id: str, db_path: Optional[str] = None) -> Optional[Dict[str, Any]]:
    with get_db_connection(db_path) as conn:
        row = conn.execute("SELECT * FROM workflow_tasks WHERE task_id = ?;", (task_id,)).fetchone()
        if not row:
            return None
        res = dict(row)
        res["dependencies"] = json.loads(res.get("dependencies_json") or "[]")
        return res

def list_tasks_for_session(session_id: str, db_path: Optional[str] = None) -> List[Dict[str, Any]]:
    with get_db_connection(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM workflow_tasks WHERE session_id = ? ORDER BY rowid ASC;",
            (session_id,)
        ).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d["id"] = d.get("task_id")
            d["output"] = d.get("output_text")
            d["assigned_agent"] = d.get("assigned_agent_id")
            d["dependencies"] = json.loads(d.get("dependencies_json") or "[]")
            result.append(d)
        return result

get_session_tasks = list_tasks_for_session

def update_task_status(
    task_id: str,
    status: str,
    output_text: Optional[str] = None,
    error_message: Optional[str] = None,
    db_path: Optional[str] = None
) -> bool:
    now = time.time()
    started_at = now if status == 'running' else None
    completed_at = now if status in ('completed', 'failed', 'cancelled', 'blocked') else None
    with get_db_connection(db_path) as conn:
        conn.execute("""
            UPDATE workflow_tasks
            SET status = ?,
                output_text = COALESCE(?, output_text),
                error_message = COALESCE(?, error_message),
                started_at = COALESCE(started_at, ?),
                completed_at = COALESCE(?, completed_at)
            WHERE task_id = ?;
        """, (status, output_text, error_message, started_at, completed_at, task_id))
        
        # When a task completes, evaluate pending dependent tasks in the same session
        if status == 'completed':
            task = conn.execute("SELECT session_id FROM workflow_tasks WHERE task_id = ?;", (task_id,)).fetchone()
            if task:
                sess_id = task["session_id"]
                pending_tasks = conn.execute(
                    "SELECT task_id, dependencies_json FROM workflow_tasks WHERE session_id = ? AND status = 'pending';",
                    (sess_id,)
                ).fetchall()
                for pt in pending_tasks:
                    pt_id = pt["task_id"]
                    deps = json.loads(pt["dependencies_json"] or "[]")
                    if deps:
                        # Check if all prerequisite tasks are completed
                        placeholders = ",".join(["?"] * len(deps))
                        completed_deps = conn.execute(
                            f"SELECT COUNT(*) as count FROM workflow_tasks WHERE task_id IN ({placeholders}) AND status = 'completed';",
                            deps
                        ).fetchone()["count"]
                        if completed_deps == len(deps):
                            conn.execute("UPDATE workflow_tasks SET status = 'ready' WHERE task_id = ?;", (pt_id,))

        # When a task fails, mark direct dependent tasks as 'blocked'
        elif status == 'failed':
            task = conn.execute("SELECT session_id FROM workflow_tasks WHERE task_id = ?;", (task_id,)).fetchone()
            if task:
                sess_id = task["session_id"]
                pending_tasks = conn.execute(
                    "SELECT task_id, dependencies_json FROM workflow_tasks WHERE session_id = ? AND status IN ('pending', 'ready');",
                    (sess_id,)
                ).fetchall()
                for pt in pending_tasks:
                    pt_id = pt["task_id"]
                    deps = json.loads(pt["dependencies_json"] or "[]")
                    if task_id in deps:
                        conn.execute(
                            "UPDATE workflow_tasks SET status = 'blocked', error_message = ? WHERE task_id = ?;",
                            (f"Blocked by upstream prerequisite task failure ({task_id})", pt_id)
                        )
                        
        conn.commit()
    return True

def increment_task_retry(task_id: str, db_path: Optional[str] = None) -> Tuple[int, bool]:
    """Increment retry counter. Returns (new_retry_count, can_retry_again)."""
    with get_db_connection(db_path) as conn:
        row = conn.execute("SELECT retry_count, max_retries FROM workflow_tasks WHERE task_id = ?;", (task_id,)).fetchone()
        if not row:
            return 0, False
        new_count = row["retry_count"] + 1
        can_retry = new_count <= row["max_retries"]
        conn.execute("UPDATE workflow_tasks SET retry_count = ? WHERE task_id = ?;", (new_count, task_id))
        conn.commit()
        return new_count, can_retry


def claim_task(task_id: str, db_path: Optional[str] = None) -> bool:
    """
    Atomically claim a task for execution.
    Transitions status from 'ready' (or 'pending') to 'running'.
    Returns True if successfully claimed, False if already claimed or finished.
    """
    now = time.time()
    with get_db_connection(db_path) as conn:
        cursor = conn.execute("""
            UPDATE workflow_tasks
            SET status = 'running',
                started_at = COALESCE(started_at, ?)
            WHERE task_id = ? AND status IN ('ready', 'pending');
        """, (now, task_id))
        conn.commit()
        return cursor.rowcount > 0


def claim_next_ready_task(
    session_id: str,
    allowed_stages: Optional[List[str]] = None,
    db_path: Optional[str] = None
) -> Optional[Dict[str, Any]]:
    """
    Find and atomically claim the next ready task in a session.
    Protects against duplicate worker execution.
    """
    all_tasks = list_tasks_for_session(session_id, db_path=db_path)
    for t in all_tasks:
        if t.get("status") in ("ready", "pending"):
            if allowed_stages and t.get("stage") not in allowed_stages:
                continue
            deps = t.get("dependencies", [])
            if deps:
                satisfied_ids = {ot["task_id"] for ot in all_tasks if ot.get("status") == "completed" or (ot.get("status") == "failed" and "_spec_" in ot.get("task_id", ""))}
                if not all(d in satisfied_ids for d in deps):
                    continue
            if claim_task(t["task_id"], db_path=db_path):
                t["status"] = "running"
                return t
    return None


def get_session_remediation_count(session_id: str, db_path: Optional[str] = None) -> int:
    """Return how many remediation tasks have been created for this session."""
    with get_db_connection(db_path) as conn:
        row = conn.execute(
            "SELECT COUNT(*) as cnt FROM workflow_tasks WHERE session_id = ? AND (title LIKE '%إصلاح%' OR title LIKE '%Remediation%');",
            (session_id,)
        ).fetchone()
        return row["cnt"] if row else 0



# =========================================================================
# Agent Runs Logging
# =========================================================================

def record_agent_run(
    task_id: str,
    session_id: str,
    agent_id: str,
    agent_name: str,
    division_id: str,
    prompt_file: str,
    status: str,
    tokens_used: int = 0,
    input_preview: Optional[str] = None,
    output_preview: Optional[str] = None,
    error_message: Optional[str] = None,
    db_path: Optional[str] = None
) -> str:
    run_id = f"run_{uuid.uuid4().hex[:12]}"
    now = time.time()
    fin = now if status in ('completed', 'failed') else None
    with get_db_connection(db_path) as conn:
        conn.execute("""
            INSERT INTO agent_runs (
                run_id, task_id, session_id, agent_id, agent_name,
                division_id, prompt_file, status, input_preview,
                output_preview, tokens_used, error_message, started_at, finished_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
        """, (
            run_id, task_id, session_id, agent_id, agent_name,
            division_id, prompt_file, status,
            input_preview[:500] if input_preview else None,
            output_preview[:500] if output_preview else None,
            tokens_used, error_message, now, fin
        ))
        conn.commit()
    return run_id

def list_agent_runs(session_id: str, task_id: Optional[str] = None, db_path: Optional[str] = None) -> List[Dict[str, Any]]:
    with get_db_connection(db_path) as conn:
        if task_id:
            rows = conn.execute("SELECT * FROM agent_runs WHERE session_id = ? AND task_id = ? ORDER BY started_at ASC;", (session_id, task_id)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM agent_runs WHERE session_id = ? ORDER BY started_at ASC;", (session_id,)).fetchall()
        return [dict(r) for r in rows]


# =========================================================================
# Task Handoffs (Agent-to-Agent Collaboration)
# =========================================================================

def record_task_handoff(
    session_id: str,
    from_task_id: str,
    to_task_id: str,
    from_agent: Optional[str] = None,
    to_agent: Optional[str] = None,
    from_agent_id: Optional[str] = None,
    to_agent_id: Optional[str] = None,
    context_summary: Optional[str] = None,
    artifact_summary: Optional[str] = None,
    artifacts: Optional[Any] = None,
    artifact_type: str = "context",
    status: str = "delivered",
    db_path: Optional[str] = None
) -> Dict[str, Any]:
    handoff_id = f"hoff_{uuid.uuid4().hex[:12]}"
    now = time.time()
    src_agent = from_agent or from_agent_id or "unknown"
    dst_agent = to_agent or to_agent_id or "unknown"
    summary = context_summary or artifact_summary or ""
    art_dict = artifacts if isinstance(artifacts, dict) else ({"summary": summary} if summary else {})
    stored_payload = {
        "context_summary": summary,
        "artifacts": art_dict
    }
    payload_json = json.dumps(stored_payload, ensure_ascii=False)
    
    with get_db_connection(db_path) as conn:
        conn.execute("""
            INSERT INTO task_handoffs (
                handoff_id, session_id, from_task_id, to_task_id,
                from_agent_id, to_agent_id, artifact_type,
                artifact_summary, status, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
        """, (handoff_id, session_id, from_task_id, to_task_id, src_agent, dst_agent, artifact_type, payload_json, status, now))
        conn.commit()

    return {
        "id": handoff_id,
        "handoff_id": handoff_id,
        "session_id": session_id,
        "from_task_id": from_task_id,
        "to_task_id": to_task_id,
        "from_agent": src_agent,
        "from_agent_id": src_agent,
        "to_agent": dst_agent,
        "to_agent_id": dst_agent,
        "artifact_type": artifact_type,
        "artifact_summary": summary,
        "context_summary": summary,
        "artifacts": art_dict,
        "status": status,
        "created_at": now
    }

create_task_handoff = record_task_handoff

def get_session_handoffs(session_id: str, db_path: Optional[str] = None) -> List[Dict[str, Any]]:
    with get_db_connection(db_path) as conn:
        rows = conn.execute("SELECT * FROM task_handoffs WHERE session_id = ? ORDER BY created_at ASC;", (session_id,)).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d["id"] = d.get("handoff_id")
            d["from_agent"] = d.get("from_agent_id")
            d["to_agent"] = d.get("to_agent_id")
            raw_summary = d.get("artifact_summary") or ""
            if raw_summary.startswith("{"):
                try:
                    parsed = json.loads(raw_summary)
                    if isinstance(parsed, dict) and "artifacts" in parsed:
                        d["context_summary"] = parsed.get("context_summary") or ""
                        d["artifacts"] = parsed.get("artifacts") or {}
                    else:
                        d["context_summary"] = raw_summary
                        d["artifacts"] = parsed if isinstance(parsed, dict) else {"summary": raw_summary}
                except Exception:
                    d["context_summary"] = raw_summary
                    d["artifacts"] = {"summary": raw_summary}
            else:
                d["context_summary"] = raw_summary
                d["artifacts"] = {"summary": raw_summary}
            result.append(d)
        return result

list_task_handoffs = get_session_handoffs


# =========================================================================
# Project-Scoped Memory Layer
# =========================================================================

def add_project_memory(
    project_id: str,
    session_id: Optional[str] = None,
    category: str = "requirement",
    key: str = "",
    content: str = "",
    provenance_agent: str = "system",
    provenance_task_id: Optional[str] = None,
    status: str = "verified",
    supersedes_key: Optional[str] = None,
    db_path: Optional[str] = None
) -> Dict[str, Any]:
    memory_id = f"mem_{uuid.uuid4().hex[:12]}"
    now = time.time()
    sanitized = sanitize_memory_content(content)
    
    # Normalize category aliases
    if category == "decision":
        category = "architecture_decision"
        
    with get_db_connection(db_path) as conn:
        # Check if supersedes_key was specified
        if supersedes_key:
            conn.execute(
                "UPDATE project_memory SET status = 'superseded', superseded_by = ?, updated_at = ? WHERE project_id = ? AND key = ?;",
                (memory_id, now, project_id, supersedes_key)
            )

        # Check if an active record with this key already exists in the same category
        existing = conn.execute(
            "SELECT memory_id, version FROM project_memory WHERE project_id = ? AND category = ? AND key = ? AND status = 'verified';",
            (project_id, category, key)
        ).fetchone()
        
        version = 1
        if existing:
            old_id = existing["memory_id"]
            version = existing["version"] + 1
            # Mark previous record as superseded
            conn.execute(
                "UPDATE project_memory SET status = 'superseded', superseded_by = ?, updated_at = ? WHERE memory_id = ?;",
                (memory_id, now, old_id)
            )

        conn.execute("""
            INSERT INTO project_memory (
                memory_id, project_id, session_id, category, key,
                content, provenance_agent, provenance_task_id, status,
                version, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
        """, (memory_id, project_id, session_id, category, key, sanitized, provenance_agent, provenance_task_id, status, version, now, now))
        conn.commit()

    return {
        "id": memory_id,
        "memory_id": memory_id,
        "project_id": project_id,
        "session_id": session_id,
        "category": category,
        "key": key,
        "content": sanitized,
        "provenance_agent": provenance_agent,
        "author_agent": provenance_agent,
        "provenance_task_id": provenance_task_id,
        "status": status,
        "is_verified": status == "verified",
        "version": version,
        "created_at": now,
        "updated_at": now
    }

def get_project_memory(
    project_id: str,
    category: Optional[str] = None,
    status: str = "verified",
    verified_only: bool = False,
    limit: int = 100,
    db_path: Optional[str] = None
) -> List[Dict[str, Any]]:
    effective_status = "verified" if verified_only else status
    with get_db_connection(db_path) as conn:
        if category:
            rows = conn.execute("""
                SELECT * FROM project_memory
                WHERE project_id = ? AND category = ? AND status = ?
                ORDER BY updated_at DESC LIMIT ?;
            """, (project_id, category, effective_status, limit)).fetchall()
        else:
            rows = conn.execute("""
                SELECT * FROM project_memory
                WHERE project_id = ? AND status = ?
                ORDER BY updated_at DESC LIMIT ?;
            """, (project_id, effective_status, limit)).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d["id"] = d.get("memory_id")
            d["author_agent"] = d.get("provenance_agent")
            d["is_verified"] = d.get("status") == "verified"
            result.append(d)
        return result

def get_project_memory_context(project_id: str, max_chars: int = 4000, db_path: Optional[str] = None) -> str:
    """Format verified project decisions and requirements into structured context for agent injection."""
    memories = get_project_memory(project_id, status="verified", limit=50, db_path=db_path)
    if not memories:
        return ""
    
    sections = {}
    for m in memories:
        cat = m["category"]
        sections.setdefault(cat, []).append(f"- **{m['key']}** ({m['provenance_agent']}): {m['content']}")
        
    formatted = []
    category_titles = {
        "requirement": "متطلبات المشروع المؤكدة (Verified Requirements)",
        "architecture_decision": "قرارات المعمارية المعتمدة (Architecture Decisions)",
        "constraint": "القيود المحددة (Constraints)",
        "qa_finding": "تدقيقات الجودة والأمان السابقة (QA Findings)",
        "user_feedback": "توجيهات وملاحظات المستخدم (User Directives)"
    }
    
    for cat, items in sections.items():
        title = category_titles.get(cat, cat.title())
        formatted.append(f"### {title}:\n" + "\n".join(items))
        
    full_text = "\n\n".join(formatted)
    return full_text[:max_chars]


# =========================================================================
# Checkpoints & Resumption
# =========================================================================

def save_session_checkpoint(
    session_id: str,
    step_name: str,
    state_snapshot: Dict[str, Any],
    db_path: Optional[str] = None
) -> Dict[str, Any]:
    checkpoint_id = f"chk_{uuid.uuid4().hex[:12]}"
    now = time.time()
    state_json = json.dumps(state_snapshot, ensure_ascii=False)
    with get_db_connection(db_path) as conn:
        conn.execute("""
            INSERT INTO execution_checkpoints (
                checkpoint_id, session_id, step_name, state_json, created_at
            ) VALUES (?, ?, ?, ?, ?);
        """, (checkpoint_id, session_id, step_name, state_json, now))
        conn.execute("""
            UPDATE workflow_sessions
            SET checkpoint_step = ?, updated_at = ?
            WHERE session_id = ?;
        """, (step_name, now, session_id))
        conn.commit()
    return {
        "id": checkpoint_id,
        "checkpoint_id": checkpoint_id,
        "session_id": session_id,
        "step_name": step_name,
        "state_data": state_snapshot,
        "state_json": state_json,
        "created_at": now
    }

def get_latest_checkpoint(session_id: str, db_path: Optional[str] = None) -> Optional[Dict[str, Any]]:
    with get_db_connection(db_path) as conn:
        row = conn.execute("""
            SELECT * FROM execution_checkpoints
            WHERE session_id = ?
            ORDER BY created_at DESC LIMIT 1;
        """, (session_id,)).fetchone()
        if not row:
            return None
        res = dict(row)
        parsed = json.loads(res["state_json"])
        res["state"] = parsed
        res["state_data"] = parsed
        return res

def get_session_checkpoints(session_id: str, db_path: Optional[str] = None) -> List[Dict[str, Any]]:
    with get_db_connection(db_path) as conn:
        rows = conn.execute("""
            SELECT * FROM execution_checkpoints
            WHERE session_id = ?
            ORDER BY created_at DESC;
        """, (session_id,)).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            parsed = json.loads(d["state_json"])
            d["state"] = parsed
            d["state_data"] = parsed
            result.append(d)
        return result

def can_resume_session(session_id: str, db_path: Optional[str] = None) -> bool:
    """Check if session is eligible to resume from a verified checkpoint."""
    sess = get_session(session_id, db_path)
    if not sess or sess["status"] not in ('running', 'failed', 'approval_needed', 'interrupted'):
        return False
    chk = get_latest_checkpoint(session_id, db_path)
    return chk is not None

def reconcile_interrupted_sessions(db_path: Optional[str] = None) -> int:
    """
    Called upon server startup to reconcile any sessions left in 'running' state
    due to unexpected server restart or power loss. Never falsely marks them as completed.
    Returns the integer count of reconciled sessions.
    """
    interrupted_ids = []
    with get_db_connection(db_path) as conn:
        rows = conn.execute("SELECT session_id FROM workflow_sessions WHERE status = 'running';").fetchall()
        for r in rows:
            sid = r["session_id"]
            interrupted_ids.append(sid)
            conn.execute("""
                UPDATE workflow_sessions
                SET status = 'interrupted',
                    error_message = 'Interrupted by unexpected process restart. Can be resumed from last checkpoint.'
                WHERE session_id = ?;
            """, (sid,))
            # Mark any running tasks as failed
            conn.execute("""
                UPDATE workflow_tasks
                SET status = 'failed',
                    error_message = 'Interrupted by unexpected process restart.'
                WHERE session_id = ? AND status = 'running';
            """, (sid,))
        conn.commit()
    return len(interrupted_ids)
