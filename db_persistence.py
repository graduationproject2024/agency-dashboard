import os
import sqlite3
import json
import time
import uuid
import re
import hashlib
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

from contextlib import contextmanager

def _retry_on_db_lock(func, max_retries=5, initial_delay=0.05):
    delay = initial_delay
    for attempt in range(max_retries):
        try:
            return func()
        except sqlite3.OperationalError as e:
            err_msg = str(e).lower()
            if ("locked" in err_msg or "busy" in err_msg) and attempt < max_retries - 1:
                logging.warning("SQLite busy/locked (attempt %d/%d), retrying in %.2fs: %s", attempt + 1, max_retries, delay, e)
                time.sleep(delay)
                delay = min(delay * 2, 1.0)
            else:
                raise

def get_db_connection(db_path: Optional[str] = None):
    """
    Open thread-isolated SQLite connection with foreign keys and WAL mode.
    Deterministic closure on exit.
    """
    path = get_db_path(db_path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA busy_timeout = 30000;")
    return _DbConnectionContext(conn)

class _DbConnectionContext:
    def __init__(self, conn):
        self._conn = conn
    def __enter__(self):
        return self._conn
    def __exit__(self, exc_type, exc_val, exc_tb):
        try:
            if exc_type is None:
                _retry_on_db_lock(self._conn.commit)
            else:
                try:
                    self._conn.rollback()
                except Exception:
                    pass
        finally:
            self._conn.close()
    def cursor(self):
        return self._conn.cursor()
    def execute(self, *args, **kwargs):
        return _retry_on_db_lock(lambda: self._conn.execute(*args, **kwargs))
    def commit(self):
        return _retry_on_db_lock(self._conn.commit)
    def close(self):
        return self._conn.close()

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
            token_source TEXT NOT NULL DEFAULT 'estimated',
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

        # 7. Artifact Manifests table (Delivery Integrity & Manifest Provenance)
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS artifact_manifests (
            manifest_id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            qa_run_id TEXT,
            delivery_id TEXT,
            relative_path TEXT NOT NULL,
            file_size INTEGER NOT NULL,
            sha256 TEXT NOT NULL,
            artifact_type TEXT NOT NULL,
            validation_status TEXT NOT NULL,
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
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_manifest_session ON artifact_manifests(session_id);")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_manifest_qa_run ON artifact_manifests(qa_run_id);")

        # Ensure token_source column exists in agent_runs table
        cursor.execute("PRAGMA table_info(agent_runs);")
        run_cols = {row[1] for row in cursor.fetchall()}
        if "token_source" not in run_cols:
            cursor.execute("ALTER TABLE agent_runs ADD COLUMN token_source TEXT DEFAULT 'estimated';")

        # Ensure workflow_sessions check constraint includes 'interrupted'
        cursor.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='workflow_sessions';")
        sess_table = cursor.fetchone()
        if sess_table and "interrupted" not in sess_table[0]:
            cursor.execute("PRAGMA foreign_keys=OFF;")
            cursor.execute("""
            CREATE TABLE workflow_sessions_migrated (
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
            cursor.execute("INSERT INTO workflow_sessions_migrated SELECT * FROM workflow_sessions;")
            cursor.execute("DROP TABLE workflow_sessions;")
            cursor.execute("ALTER TABLE workflow_sessions_migrated RENAME TO workflow_sessions;")
            cursor.execute("PRAGMA foreign_keys=ON;")

        conn.commit()


# =========================================================================
# Memory Secret Sanitization
# =========================================================================

SECRET_API_KEY_PATTERN = re.compile(r'(?i)\b(AIzaSy[0-9A-Za-z_-]{20,}|sk-[0-9A-Za-z_-]{20,}|AKIA[0-9A-Z]{16})\b')
SECRET_JWT_PATTERN = re.compile(r'\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_\-.]*')
SECRET_BEARER_PATTERN = re.compile(r'(?i)\bbearer\s+([A-Za-z0-9_\-\.]{15,})')
SECRET_URL_KEY_PATTERN = re.compile(r'(?i)([?&](?:key|api[_-]?key|token|secret)=)([^&\s]+)')
SECRET_ASSIGN_PATTERN = re.compile(r'(?i)\b((?:password|secret|auth[_-]?token|client[_-]?secret))\s*[:=]\s*["\']?([^"\'\s,;{}]+)["\']?')
SECRET_API_KEY_ASSIGN_PATTERN = re.compile(r'(?i)\b(api[_-]?key)\s*[:=]\s*["\']?([^"\'\s,;{}]+)["\']?')
SECRET_JSON_PATTERN = re.compile(r'(?i)("(?:apiKey|clientSecret|secret|token|password|accessToken|refreshToken)")\s*:\s*"([^"]+)"')

def sanitize_memory_content(text: Any) -> str:
    """Mask sensitive API keys, bearer tokens, or credentials before saving to persistent memory, handoffs, or logs."""
    if text is None:
        return ""
    if isinstance(text, (dict, list)):
        try:
            text = json.dumps(text, ensure_ascii=False)
        except Exception:
            text = str(text)
    elif not isinstance(text, str):
        text = str(text)

    sanitized = text
    # Redact URL query params with secrets
    sanitized = SECRET_URL_KEY_PATTERN.sub(r"\1[REDACTED_KEY]", sanitized)
    # Redact Bearer tokens
    sanitized = SECRET_BEARER_PATTERN.sub("Bearer [REDACTED_CREDENTIAL]", sanitized)
    # Redact JSON structured credentials
    sanitized = SECRET_JSON_PATTERN.sub(r'\1: "[REDACTED_CREDENTIAL]"', sanitized)
    # Redact explicit credential assignments
    sanitized = SECRET_ASSIGN_PATTERN.sub(r"\1=[REDACTED_CREDENTIAL]", sanitized)
    # Redact api_key assignments
    sanitized = SECRET_API_KEY_ASSIGN_PATTERN.sub(r"\1=[REDACTED_API_KEY]", sanitized)
    # Redact known API key token formats (Gemini, OpenAI, AWS)
    sanitized = SECRET_API_KEY_PATTERN.sub("[REDACTED_API_KEY]", sanitized)
    # Redact JWT tokens
    sanitized = SECRET_JWT_PATTERN.sub("[REDACTED_JWT]", sanitized)
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
    safe_error = sanitize_memory_content(error_message) if error_message else None
    with get_db_connection(db_path) as conn:
        if status == 'completed':
            current = conn.execute("SELECT output_text FROM workflow_tasks WHERE task_id = ?;", (task_id,)).fetchone()
            existing_out = current["output_text"] if current else None
            effective_out = output_text if output_text is not None else existing_out
            if not effective_out or not str(effective_out).strip():
                raise ValueError(f"Cannot mark task {task_id} completed without valid non-empty output_text.")

        conn.execute("""
            UPDATE workflow_tasks
            SET status = ?,
                output_text = COALESCE(?, output_text),
                error_message = COALESCE(?, error_message),
                started_at = COALESCE(started_at, ?),
                completed_at = COALESCE(?, completed_at)
            WHERE task_id = ?;
        """, (status, output_text, safe_error, started_at, completed_at, task_id))
        
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
    Transitions status from 'ready' (or 'pending' with all dependencies satisfied) to 'running'.
    Returns True if successfully claimed, False if already claimed, unmet dependencies, or finished.
    """
    now = time.time()
    with get_db_connection(db_path) as conn:
        task = conn.execute("SELECT session_id, status, dependencies_json FROM workflow_tasks WHERE task_id = ?;", (task_id,)).fetchone()
        if not task or task["status"] not in ('ready', 'pending'):
            return False
            
        deps = json.loads(task["dependencies_json"] or "[]")
        if deps:
            placeholders = ",".join(["?"] * len(deps))
            completed_cnt = conn.execute(
                f"SELECT COUNT(*) as count FROM workflow_tasks WHERE session_id = ? AND task_id IN ({placeholders}) AND (status = 'completed' OR (status = 'failed' AND task_id LIKE '%_spec_%'));",
                [task["session_id"]] + deps
            ).fetchone()["count"]
            if completed_cnt < len(deps):
                return False  # Cannot claim before prerequisites are satisfied

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
    token_source: str = "estimated",
    input_preview: Optional[str] = None,
    output_preview: Optional[str] = None,
    error_message: Optional[str] = None,
    db_path: Optional[str] = None
) -> str:
    run_id = f"run_{uuid.uuid4().hex[:12]}"
    now = time.time()
    fin = now if status in ('completed', 'failed') else None
    safe_input = sanitize_memory_content(input_preview[:500]) if input_preview else None
    safe_output = sanitize_memory_content(output_preview[:500]) if output_preview else None
    safe_error = sanitize_memory_content(error_message) if error_message else None
    with get_db_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("PRAGMA table_info(agent_runs);")
        cols = {row[1] for row in cursor.fetchall()}
        
        # Check if an in-flight running agent_run exists for this task
        existing_run = cursor.execute("""
            SELECT run_id FROM agent_runs 
            WHERE task_id = ? AND session_id = ? AND status = 'running'
            ORDER BY started_at DESC LIMIT 1;
        """, (task_id, session_id)).fetchone()

        if existing_run:
            run_id = existing_run["run_id"]
            if "token_source" in cols:
                cursor.execute("""
                    UPDATE agent_runs
                    SET status = ?,
                        input_preview = COALESCE(?, input_preview),
                        output_preview = COALESCE(?, output_preview),
                        tokens_used = ?,
                        token_source = ?,
                        finished_at = ?,
                        error_message = ?
                    WHERE run_id = ?;
                """, (status, safe_input, safe_output, tokens_used, token_source, fin, safe_error, run_id))
            else:
                cursor.execute("""
                    UPDATE agent_runs
                    SET status = ?,
                        input_preview = COALESCE(?, input_preview),
                        output_preview = COALESCE(?, output_preview),
                        tokens_used = ?,
                        finished_at = ?,
                        error_message = ?
                    WHERE run_id = ?;
                """, (status, safe_input, safe_output, tokens_used, fin, safe_error, run_id))
        else:
            if "token_source" in cols:
                cursor.execute("""
                    INSERT INTO agent_runs (
                        run_id, task_id, session_id, agent_id, agent_name,
                        division_id, prompt_file, status, input_preview,
                        output_preview, tokens_used, token_source, error_message, started_at, finished_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                """, (
                    run_id, task_id, session_id, agent_id, agent_name,
                    division_id, prompt_file, status,
                    safe_input, safe_output,
                    tokens_used, token_source, safe_error, now, fin
                ))
            else:
                cursor.execute("""
                    INSERT INTO agent_runs (
                        run_id, task_id, session_id, agent_id, agent_name,
                        division_id, prompt_file, status, input_preview,
                        output_preview, tokens_used, error_message, started_at, finished_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                """, (
                    run_id, task_id, session_id, agent_id, agent_name,
                    division_id, prompt_file, status,
                    safe_input, safe_output,
                    tokens_used, safe_error, now, fin
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


def record_agent_run_start(
    task_id: str,
    session_id: str,
    agent_id: str,
    agent_name: str,
    division_id: str,
    prompt_file: str,
    input_preview: Optional[str] = None,
    db_path: Optional[str] = None
) -> str:
    """
    Durable pre-flight record created right before an external agent/provider is invoked.
    Allows recovery to distinguish:
    - Task claimed but provider never called (no agent_run row)
    - External call in-flight when process crashed (agent_run row in 'running' status)
    """
    run_id = f"run_{uuid.uuid4().hex[:12]}"
    now = time.time()
    safe_input = sanitize_memory_content(input_preview[:500]) if input_preview else None
    with get_db_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("PRAGMA table_info(agent_runs);")
        cols = {row[1] for row in cursor.fetchall()}
        if "token_source" in cols:
            conn.execute("""
                INSERT INTO agent_runs (
                    run_id, task_id, session_id, agent_id, agent_name,
                    division_id, prompt_file, status, input_preview,
                    tokens_used, token_source, started_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            """, (
                run_id, task_id, session_id, agent_id, agent_name,
                division_id, prompt_file, 'running', safe_input,
                0, 'estimated', now
            ))
        else:
            conn.execute("""
                INSERT INTO agent_runs (
                    run_id, task_id, session_id, agent_id, agent_name,
                    division_id, prompt_file, status, input_preview,
                    tokens_used, started_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            """, (
                run_id, task_id, session_id, agent_id, agent_name,
                division_id, prompt_file, 'running', safe_input,
                0, now
            ))
        conn.commit()
    return run_id


def complete_task_with_agent_run(
    task_id: str,
    session_id: str,
    agent_id: str,
    agent_name: str,
    division_id: str,
    prompt_file: str,
    output_text: str,
    tokens_used: int = 0,
    token_source: str = "estimated",
    input_preview: Optional[str] = None,
    output_preview: Optional[str] = None,
    error_message: Optional[str] = None,
    db_path: Optional[str] = None
) -> Tuple[bool, str]:
    """
    Atomically finalize a task and record its durable agent run within a single SQLite transaction.
    Guarantees that a task cannot become 'completed' without its corresponding execution evidence,
    token accounting, and deliverable output.
    Also evaluates and unblocks dependent tasks whose prerequisites are now fully satisfied.
    Returns (success, run_id).
    """
    if not output_text or not str(output_text).strip():
        raise ValueError(f"Cannot mark task {task_id} completed without valid non-empty output_text.")

    try:
        tokens_used = max(0, int(tokens_used or 0))
    except (ValueError, TypeError):
        tokens_used = 0

    now = time.time()
    safe_input = sanitize_memory_content(input_preview[:500]) if input_preview else None
    effective_out_prev = output_preview or output_text
    safe_output = sanitize_memory_content(effective_out_prev[:500]) if effective_out_prev else None
    safe_error = sanitize_memory_content(error_message) if error_message else None
    run_id = f"run_{uuid.uuid4().hex[:12]}"

    with get_db_connection(db_path) as conn:
        cursor = conn.cursor()
        
        # Check if an in-flight running agent_run exists for this task
        existing_run = cursor.execute("""
            SELECT run_id FROM agent_runs 
            WHERE task_id = ? AND session_id = ? AND status = 'running'
            ORDER BY started_at DESC LIMIT 1;
        """, (task_id, session_id)).fetchone()

        # Check if a completed run already exists for this task to avoid duplicate run rows on repeated callbacks
        existing_completed_run = None
        if not existing_run:
            existing_completed_run = cursor.execute("""
                SELECT run_id FROM agent_runs 
                WHERE task_id = ? AND session_id = ? AND status = 'completed'
                ORDER BY finished_at DESC LIMIT 1;
            """, (task_id, session_id)).fetchone()

        cursor.execute("PRAGMA table_info(agent_runs);")
        cols = {row[1] for row in cursor.fetchall()}

        if existing_run:
            run_id = existing_run["run_id"]
            if "token_source" in cols:
                cursor.execute("""
                    UPDATE agent_runs
                    SET status = 'completed',
                        input_preview = COALESCE(?, input_preview),
                        output_preview = ?,
                        tokens_used = ?,
                        token_source = ?,
                        finished_at = ?,
                        error_message = ?
                    WHERE run_id = ?;
                """, (safe_input, safe_output, tokens_used, token_source, now, safe_error, run_id))
            else:
                cursor.execute("""
                    UPDATE agent_runs
                    SET status = 'completed',
                        input_preview = COALESCE(?, input_preview),
                        output_preview = ?,
                        tokens_used = ?,
                        finished_at = ?,
                        error_message = ?
                    WHERE run_id = ?;
                """, (safe_input, safe_output, tokens_used, now, safe_error, run_id))
        elif existing_completed_run:
            run_id = existing_completed_run["run_id"]
            if "token_source" in cols:
                cursor.execute("""
                    UPDATE agent_runs
                    SET input_preview = COALESCE(?, input_preview),
                        output_preview = ?,
                        tokens_used = ?,
                        token_source = ?,
                        finished_at = ?,
                        error_message = ?
                    WHERE run_id = ?;
                """, (safe_input, safe_output, tokens_used, token_source, now, safe_error, run_id))
            else:
                cursor.execute("""
                    UPDATE agent_runs
                    SET input_preview = COALESCE(?, input_preview),
                        output_preview = ?,
                        tokens_used = ?,
                        finished_at = ?,
                        error_message = ?
                    WHERE run_id = ?;
                """, (safe_input, safe_output, tokens_used, now, safe_error, run_id))
        else:
            if "token_source" in cols:
                cursor.execute("""
                    INSERT INTO agent_runs (
                        run_id, task_id, session_id, agent_id, agent_name,
                        division_id, prompt_file, status, input_preview,
                        output_preview, tokens_used, token_source, error_message, started_at, finished_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                """, (
                    run_id, task_id, session_id, agent_id, agent_name,
                    division_id, prompt_file, 'completed',
                    safe_input, safe_output,
                    tokens_used, token_source, safe_error, now, now
                ))
            else:
                cursor.execute("""
                    INSERT INTO agent_runs (
                        run_id, task_id, session_id, agent_id, agent_name,
                        division_id, prompt_file, status, input_preview,
                        output_preview, tokens_used, error_message, started_at, finished_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                """, (
                    run_id, task_id, session_id, agent_id, agent_name,
                    division_id, prompt_file, 'completed',
                    safe_input, safe_output,
                    tokens_used, safe_error, now, now
                ))

        # Update workflow_tasks atomically
        cursor.execute("""
            UPDATE workflow_tasks
            SET status = 'completed',
                output_text = ?,
                error_message = NULL,
                completed_at = COALESCE(completed_at, ?)
            WHERE task_id = ?;
        """, (output_text, now, task_id))

        # Evaluate pending dependent tasks in the same session
        pending_tasks = cursor.execute(
            "SELECT task_id, dependencies_json FROM workflow_tasks WHERE session_id = ? AND status = 'pending';",
            (session_id,)
        ).fetchall()
        for pt in pending_tasks:
            pt_id = pt["task_id"]
            deps = json.loads(pt["dependencies_json"] or "[]")
            if deps:
                placeholders = ",".join(["?"] * len(deps))
                completed_deps = cursor.execute(
                    f"SELECT COUNT(*) as count FROM workflow_tasks WHERE session_id = ? AND task_id IN ({placeholders}) AND status = 'completed';",
                    [session_id] + deps
                ).fetchone()["count"]
                if completed_deps == len(deps):
                    cursor.execute("UPDATE workflow_tasks SET status = 'ready' WHERE task_id = ?;", (pt_id,))

        conn.commit()

    return True, run_id


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
    now = time.time()
    src_agent = from_agent or from_agent_id or "unknown"
    dst_agent = to_agent or to_agent_id or "unknown"
    summary = sanitize_memory_content(context_summary or artifact_summary or "")
    art_dict = artifacts if isinstance(artifacts, dict) else ({"summary": summary} if summary else {})
    stored_payload = {
        "context_summary": summary,
        "artifacts": art_dict
    }
    payload_json = json.dumps(stored_payload, ensure_ascii=False)
    
    with get_db_connection(db_path) as conn:
        cursor = conn.cursor()

        # 1. Source task verification if it exists in DB
        src_task = cursor.execute("SELECT session_id, status FROM workflow_tasks WHERE task_id = ?;", (from_task_id,)).fetchone()
        if src_task:
            if src_task["session_id"] != session_id:
                raise ValueError(
                    f"Cross-session handoff rejected: source task {from_task_id} belongs to session {src_task['session_id']}, not {session_id}"
                )
            if status in ("delivered", "acknowledged"):
                if src_task["status"] in ("failed", "cancelled", "blocked"):
                    raise ValueError(f"Cannot record successful handoff from {src_task['status']} task {from_task_id}")
                if src_task["status"] != "completed":
                    raise ValueError(
                        f"Cannot record successful handoff from incomplete task {from_task_id} (status: {src_task['status']}). Source task must be 'completed'."
                    )

        # 2. Destination task verification if it exists in DB
        if to_task_id:
            dst_task = cursor.execute(
                "SELECT session_id, dependencies_json FROM workflow_tasks WHERE task_id = ?;", 
                (to_task_id,)
            ).fetchone()
            if dst_task:
                if dst_task["session_id"] != session_id:
                    raise ValueError(
                        f"Cross-session handoff rejected: destination task {to_task_id} belongs to session {dst_task['session_id']}, not {session_id}"
                    )
                if from_task_id != to_task_id:
                    deps = json.loads(dst_task["dependencies_json"] or "[]")
                    if deps and from_task_id not in deps:
                        raise ValueError(
                            f"Invalid handoff: destination task {to_task_id} does not depend on source task {from_task_id} in DAG"
                        )

        # 3. Enforce deterministic identity and idempotency for handoff records via atomic upsert
        h = hashlib.sha256(f"{session_id}_{from_task_id}_{to_task_id}".encode("utf-8")).hexdigest()[:16]
        handoff_id = f"hoff_{h}"

        try:
            cursor.execute("""
                INSERT INTO task_handoffs (
                    handoff_id, session_id, from_task_id, to_task_id,
                    from_agent_id, to_agent_id, artifact_type,
                    artifact_summary, status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(handoff_id) DO UPDATE SET
                    from_agent_id = excluded.from_agent_id,
                    to_agent_id = excluded.to_agent_id,
                    artifact_type = excluded.artifact_type,
                    artifact_summary = excluded.artifact_summary,
                    status = excluded.status,
                    created_at = excluded.created_at;
            """, (handoff_id, session_id, from_task_id, to_task_id, src_agent, dst_agent, artifact_type, payload_json, status, now))
        except sqlite3.OperationalError:
            # Fallback for legacy SQLite engines without ON CONFLICT support
            cursor.execute("""
                INSERT OR REPLACE INTO task_handoffs (
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
        # Check if an active record with this key already exists in the same category
        existing = conn.execute(
            "SELECT memory_id, version FROM project_memory WHERE project_id = ? AND category = ? AND key = ? AND status = 'verified';",
            (project_id, category, key)
        ).fetchone()
        
        version = 1
        old_id = None
        if existing:
            old_id = existing["memory_id"]
            version = existing["version"] + 1

        valid_session_id = None
        if session_id:
            sess_row = conn.execute("SELECT session_id FROM workflow_sessions WHERE session_id = ?;", (session_id,)).fetchone()
            if sess_row:
                valid_session_id = session_id

        # 1. Insert the new verified record first (so memory_id exists for any FK references)
        conn.execute("""
            INSERT INTO project_memory (
                memory_id, project_id, session_id, category, key,
                content, provenance_agent, provenance_task_id, status,
                version, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
        """, (memory_id, project_id, valid_session_id, category, key, sanitized, provenance_agent, provenance_task_id, status, version, now, now))

        # 2. Mark previous record as superseded
        if old_id:
            conn.execute(
                "UPDATE project_memory SET status = 'superseded', superseded_by = ?, updated_at = ? WHERE memory_id = ?;",
                (memory_id, now, old_id)
            )

        # 3. Mark supersedes_key target as superseded
        if supersedes_key:
            conn.execute(
                "UPDATE project_memory SET status = 'superseded', superseded_by = ?, updated_at = ? WHERE project_id = ? AND key = ? AND memory_id != ?;",
                (memory_id, now, project_id, supersedes_key, memory_id)
            )

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
    if not sess or sess["status"] not in ('interrupted', 'failed', 'approval_needed'):
        return False
    chk = get_latest_checkpoint(session_id, db_path)
    return chk is not None

def reconcile_interrupted_sessions(db_path: Optional[str] = None) -> int:
    """
    Called upon server startup to reconcile any sessions left in 'running' state
    due to unexpected server restart or power loss. Never falsely marks them as completed.
    Safely reconciles interrupted running tasks via bounded retry without duplicating token costs.
    Also cleans up stale running tasks belonging to cancelled or failed sessions.
    Returns the integer count of reconciled sessions.
    """
    interrupted_ids = []
    with get_db_connection(db_path) as conn:
        # 1. Clean up stale running tasks belonging to cancelled sessions
        conn.execute("""
            UPDATE workflow_tasks
            SET status = 'cancelled',
                error_message = 'Task cancelled because parent session was cancelled.'
            WHERE status = 'running' AND session_id IN (
                SELECT session_id FROM workflow_sessions WHERE status = 'cancelled'
            );
        """)

        # 2. Clean up stale running tasks belonging to failed sessions
        conn.execute("""
            UPDATE workflow_tasks
            SET status = 'failed',
                error_message = 'Task failed because parent session failed.'
            WHERE status = 'running' AND session_id IN (
                SELECT session_id FROM workflow_sessions WHERE status = 'failed'
            );
        """)

        # 3. Reconcile sessions that were left in 'running' state
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
            
            # Reconcile running tasks for this session
            running_tasks = conn.execute("""
                SELECT task_id, retry_count, max_retries, output_text
                FROM workflow_tasks
                WHERE session_id = ? AND status = 'running';
            """, (sid,)).fetchall()
            
            session_should_fail = False
            for t in running_tasks:
                tid = t["task_id"]
                retries = t["retry_count"]
                max_retries = t["max_retries"]
                
                # Check if an agent_run completed for this task before process termination
                completed_run = conn.execute("""
                    SELECT output_preview FROM agent_runs
                    WHERE task_id = ? AND status = 'completed'
                    ORDER BY finished_at DESC LIMIT 1;
                """, (tid,)).fetchone()
                
                # Check if an in-flight provider call was ongoing when process died
                in_flight_run = conn.execute("""
                    SELECT run_id, started_at FROM agent_runs
                    WHERE task_id = ? AND status = 'running'
                    ORDER BY started_at DESC LIMIT 1;
                """, (tid,)).fetchone()
                
                if completed_run and completed_run["output_preview"]:
                    # Task completed execution before process shutdown; restore output and mark completed
                    out = completed_run["output_preview"]
                    conn.execute("""
                        UPDATE workflow_tasks
                        SET status = 'completed',
                            output_text = COALESCE(output_text, ?),
                            error_message = NULL,
                            completed_at = ?
                        WHERE task_id = ?;
                    """, (out, time.time(), tid))
                    
                    # Unblock any dependent tasks
                    pts = conn.execute(
                        "SELECT task_id, dependencies_json FROM workflow_tasks WHERE session_id = ? AND status = 'pending';",
                        (sid,)
                    ).fetchall()
                    for pt in pts:
                        deps = json.loads(pt["dependencies_json"] or "[]")
                        if deps:
                            placeholders = ",".join(["?"] * len(deps))
                            completed_cnt = conn.execute(
                                f"SELECT COUNT(*) as count FROM workflow_tasks WHERE session_id = ? AND task_id IN ({placeholders}) AND status = 'completed';",
                                [sid] + deps
                            ).fetchone()["count"]
                            if completed_cnt == len(deps):
                                conn.execute("UPDATE workflow_tasks SET status = 'ready' WHERE task_id = ?;", (pt["task_id"],))

                elif in_flight_run:
                    # An external provider request was in-flight when the process died.
                    # Outcome is ambiguous! Do NOT blindly rerun billable request without confirmation.
                    conn.execute("""
                        UPDATE agent_runs
                        SET status = 'failed',
                            error_message = 'Provider outcome ambiguous: external call was in-flight during crash',
                            finished_at = ?
                        WHERE run_id = ?;
                    """, (time.time(), in_flight_run["run_id"]))
                    conn.execute("""
                        UPDATE workflow_tasks
                        SET status = 'failed',
                            error_message = 'Provider outcome unknown: external request was in-flight when process terminated. Not automatically rerunning billable request.',
                            completed_at = ?
                        WHERE task_id = ?;
                    """, (time.time(), tid))
                    session_should_fail = True

                elif retries < max_retries:
                    # Task was claimed but provider call was never dispatched: safe to retry!
                    conn.execute("""
                        UPDATE workflow_tasks
                        SET status = 'ready',
                            retry_count = retry_count + 1,
                            error_message = 'Interrupted by unexpected process restart. Queued for retry.',
                            started_at = NULL
                        WHERE task_id = ?;
                    """, (tid,))
                else:
                    # Retries exhausted: mark failed
                    conn.execute("""
                        UPDATE workflow_tasks
                        SET status = 'failed',
                            error_message = 'Interrupted by unexpected process restart. Maximum retries exceeded.'
                        WHERE task_id = ?;
                    """, (tid,))
                    if "_spec_" not in tid:
                        session_should_fail = True

            if session_should_fail:
                conn.execute("""
                    UPDATE workflow_sessions
                    SET status = 'failed',
                        error_message = 'Interrupted session failed: core task exceeded maximum restart retry limit.'
                    WHERE session_id = ?;
                """, (sid,))

        # 4. Consistency check: any task marked 'completed' in an interrupted session
        # MUST have corresponding durable execution evidence in agent_runs.
        # If execution evidence is missing, reset to ready so it cannot be falsely completed without evidence.
        orphan_tasks = conn.execute("""
            SELECT t.task_id, t.session_id FROM workflow_tasks t
            WHERE t.status = 'completed' AND t.session_id IN (
                SELECT session_id FROM workflow_sessions WHERE status IN ('running', 'interrupted')
            ) AND NOT EXISTS (
                SELECT 1 FROM agent_runs r WHERE r.task_id = t.task_id AND r.status = 'completed'
            );
        """).fetchall()
        for ot in orphan_tasks:
            conn.execute("""
                UPDATE workflow_tasks
                SET status = 'ready',
                    error_message = 'Recovered from crash: missing durable agent execution evidence. Reset to ready for verified execution.'
                WHERE task_id = ?;
            """, (ot["task_id"],))

        conn.commit()
    return len(interrupted_ids)


def acquire_session_resumption_lock(session_id: str, db_path: Optional[str] = None, allow_approval_needed: bool = False) -> bool:
    """
    Atomically acquire a resumption lock on an interrupted, failed, or approval_needed session
    to prevent multiple concurrent recovery workers from resuming the same session.
    Returns True if lock acquired, False if already running or not in resumable state.
    """
    now = time.time()
    allowed_statuses = ('interrupted', 'failed', 'approval_needed') if allow_approval_needed else ('interrupted', 'failed')
    placeholders = ",".join(["?"] * len(allowed_statuses))
    with get_db_connection(db_path) as conn:
        cursor = conn.execute(f"""
            UPDATE workflow_sessions
            SET status = 'running', updated_at = ?
            WHERE session_id = ? AND status IN ({placeholders});
        """, [now, session_id] + list(allowed_statuses))
        conn.commit()
        return cursor.rowcount > 0


# =========================================================================
# Artifact Manifest & Delivery Provenance Layer
# =========================================================================

def record_artifact_manifest(
    session_id: str,
    qa_run_id: Optional[str] = None,
    manifest: Optional[List[Dict[str, Any]]] = None,
    delivery_id: Optional[str] = None,
    db_path: Optional[str] = None
) -> List[Dict[str, Any]]:
    """
    Persist validated artifact manifest entries linked to a specific session,
    QA validation run, and atomic publication delivery ID.
    Enforces deterministic manifest IDs and idempotent storage.
    """
    if not manifest:
        return []
    now = time.time()
    records = []
    with get_db_connection(db_path) as conn:
        for item in manifest:
            rel_path = item.get("relative_path") or ""
            file_size = int(item.get("file_size") or 0)
            sha256_hash = str(item.get("sha256") or "").lower()
            artifact_type = str(item.get("artifact_type") or "asset")
            val_status = str(item.get("validation_status") or "passed")
            
            # Deterministic manifest entry ID
            raw_key = f"{session_id}_{qa_run_id or 'none'}_{delivery_id or 'none'}_{rel_path}"
            mid = f"man_{hashlib.sha256(raw_key.encode('utf-8')).hexdigest()[:16]}"
            
            conn.execute("""
                INSERT INTO artifact_manifests (
                    manifest_id, session_id, qa_run_id, delivery_id,
                    relative_path, file_size, sha256, artifact_type,
                    validation_status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(manifest_id) DO UPDATE SET
                    delivery_id = excluded.delivery_id,
                    validation_status = excluded.validation_status;
            """, (mid, session_id, qa_run_id, delivery_id, rel_path, file_size, sha256_hash, artifact_type, val_status, now))
            
            records.append({
                "manifest_id": mid,
                "session_id": session_id,
                "qa_run_id": qa_run_id,
                "delivery_id": delivery_id,
                "relative_path": rel_path,
                "file_size": file_size,
                "sha256": sha256_hash,
                "artifact_type": artifact_type,
                "validation_status": val_status,
                "created_at": now
            })
        conn.commit()
    return records


def get_artifact_manifest(
    session_id: str,
    qa_run_id: Optional[str] = None,
    delivery_id: Optional[str] = None,
    db_path: Optional[str] = None
) -> List[Dict[str, Any]]:
    """Retrieve persisted deliverable artifact manifest entries for a session."""
    with get_db_connection(db_path) as conn:
        query = "SELECT * FROM artifact_manifests WHERE session_id = ?"
        params = [session_id]
        if qa_run_id:
            query += " AND qa_run_id = ?"
            params.append(qa_run_id)
        if delivery_id:
            query += " AND delivery_id = ?"
            params.append(delivery_id)
        query += " ORDER BY relative_path ASC;"
        rows = conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]


def get_operator_session_audit(session_id: str, db_path: Optional[str] = None) -> Dict[str, Any]:
    """
    Produce a complete, correlation-verified operator failure and diagnostic report.
    Aggregates:
    - Session, tasks, agent runs, handoffs, checkpoints, and project memory
    - Cost & token accounting (provider_reported vs estimated, ambiguity status)
    - QA run & self-healing remediation cycles
    - Published artifact manifest & delivery identity
    - Actionable failure analysis and permitted recovery actions
    """
    with get_db_connection(db_path) as conn:
        sess_row = conn.execute("SELECT * FROM workflow_sessions WHERE session_id = ?;", (session_id,)).fetchone()
        if not sess_row:
            return {"error": "Session not found", "session_id": session_id}
        
        session = dict(sess_row)
        task_rows = conn.execute("SELECT * FROM workflow_tasks WHERE session_id = ? ORDER BY stage ASC, rowid ASC;", (session_id,)).fetchall()
        tasks = [dict(t) for t in task_rows]
        
        run_rows = conn.execute("SELECT * FROM agent_runs WHERE session_id = ? ORDER BY started_at ASC;", (session_id,)).fetchall()
        runs = [dict(r) for r in run_rows]
        
        handoff_rows = conn.execute("SELECT * FROM task_handoffs WHERE session_id = ? ORDER BY created_at ASC;", (session_id,)).fetchall()
        handoffs = [dict(h) for h in handoff_rows]
        
        manifest_rows = conn.execute("SELECT * FROM artifact_manifests WHERE session_id = ? ORDER BY relative_path ASC;", (session_id,)).fetchall()
        manifest = [dict(m) for m in manifest_rows]
        
        mem_rows = conn.execute("SELECT * FROM project_memory WHERE session_id = ? ORDER BY created_at ASC;", (session_id,)).fetchall()
        memory_items = [dict(m) for m in mem_rows]

    # Token & financial accounting analysis
    total_tokens = 0
    provider_reported_tokens = 0
    estimated_tokens = 0
    in_flight_runs = []
    
    for r in runs:
        tok = int(r.get("tokens_used") or 0)
        source = r.get("token_source") or "estimated"
        if r.get("status") == "completed":
            total_tokens += tok
            if source == "provider_reported":
                provider_reported_tokens += tok
            else:
                estimated_tokens += tok
        elif r.get("status") in ("in_progress", "pending"):
            in_flight_runs.append(r)

    has_ambiguous_provider_call = any(
        "ambiguous" in (r.get("error_message") or "").lower() or
        "in-flight" in (r.get("error_message") or "").lower()
        for r in runs
    )
    
    potential_cost_incurred = len(runs) > 0

    # QA & remediation analysis
    qa_findings = [m for m in memory_items if m.get("category") == "qa_finding"]
    remediation_cycles = [m for m in qa_findings if "QA_Defect_Cycle" in m.get("key", "")]
    remediation_successes = [m for m in qa_findings if "QA_Remediation_Success" in m.get("key", "")]

    # Publication analysis
    delivery_task = next((t for t in tasks if t.get("stage") == "delivery"), None)
    is_delivery_completed = bool(delivery_task and delivery_task.get("status") == "completed")
    delivery_id = manifest[0].get("delivery_id") if manifest else None

    # Failure analysis
    failed_task = next((t for t in tasks if t.get("status") == "failed"), None)
    rollback_failed = "rollback error" in (session.get("error_message") or "").lower()
    is_retry_safe = not has_ambiguous_provider_call and not rollback_failed and (session.get("status") in ("interrupted", "failed"))

    # Permitted recovery actions
    status = session.get("status")
    permitted_recovery = []
    if status == "running":
        permitted_recovery = ["cancel"]
    elif status == "approval_needed":
        permitted_recovery = ["approve", "cancel"]
    elif status == "interrupted":
        permitted_recovery = ["resume", "cancel"]
    elif status == "failed":
        if rollback_failed:
            permitted_recovery = ["inspect_diagnostics", "reconcile_incomplete_rollback"]
        else:
            permitted_recovery = ["retry"] if is_retry_safe else ["inspect_diagnostics"]
    elif status == "completed":
        permitted_recovery = ["inspect_deliverables"]
    elif status == "cancelled":
        permitted_recovery = ["view_only"]

    return {
        "session": session,
        "tasks_summary": {
            "total_tasks": len(tasks),
            "completed": sum(1 for t in tasks if t.get("status") == "completed"),
            "failed": sum(1 for t in tasks if t.get("status") == "failed"),
            "in_progress": sum(1 for t in tasks if t.get("status") == "in_progress"),
            "ready": sum(1 for t in tasks if t.get("status") == "ready"),
            "blocked": sum(1 for t in tasks if t.get("status") == "blocked"),
        },
        "cost_and_token_accounting": {
            "total_tokens_billed": total_tokens,
            "provider_reported_tokens": provider_reported_tokens,
            "estimated_tokens": estimated_tokens,
            "potential_cost_incurred": potential_cost_incurred,
            "has_ambiguous_provider_call": has_ambiguous_provider_call,
            "in_flight_runs_count": len(in_flight_runs)
        },
        "qa_and_remediation": {
            "qa_finding_count": len(qa_findings),
            "remediation_cycles_executed": len(remediation_cycles),
            "remediation_succeeded": len(remediation_successes) > 0,
            "qa_passed": len(remediation_successes) > 0 or (is_delivery_completed and len(qa_findings) == 0)
        },
        "publication_and_delivery": {
            "is_published": is_delivery_completed and len(manifest) > 0,
            "delivery_id": delivery_id,
            "published_artifact_count": len(manifest),
            "manifest_verified": len(manifest) > 0
        },
        "failure_analysis": {
            "status": status,
            "failed_at_stage": session.get("current_stage"),
            "failed_task_id": failed_task["task_id"] if failed_task else None,
            "error_message": session.get("error_message"),
            "is_provider_ambiguous": has_ambiguous_provider_call,
            "rollback_failed": rollback_failed,
            "is_retry_safe": is_retry_safe
        },
        "permitted_recovery_actions": permitted_recovery,
        "manifest": manifest,
        "handoffs": handoffs
    }


