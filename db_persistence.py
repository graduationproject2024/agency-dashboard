import os
import sys
import math
import sqlite3
import json
import time
import uuid
import re
import hashlib
import logging
import threading
import functools
from decimal import Decimal, ROUND_HALF_UP
from contextlib import contextmanager
from typing import Dict, List, Optional, Tuple, Any

DEFAULT_DB_FILENAME = "agency_persistence.db"

class IdempotencyConflictError(ValueError):
    """Raised when an idempotency key is reused with a different request payload."""
    pass

class IdempotencyInProgressError(RuntimeError):
    """Raised when a request with the same idempotency key is currently executing."""
    pass

class ConcurrencyConflictError(RuntimeError):
    """Raised when concurrent operations attempt conflicting updates on the same run ledger."""
    pass

def round_cost(val: Any, places: int = 6) -> float:
    """Rounds financial cost to specified decimal precision using Decimal quantization to avoid drift."""
    try:
        if val is None:
            return 0.0
        d = Decimal(str(val)).quantize(Decimal(10) ** -places, rounding=ROUND_HALF_UP)
        return float(d)
    except Exception:
        return round(float(val or 0.0), places)

def validate_financial_numeric(
    val: Any,
    field_name: str = "cost",
    allow_none: bool = False,
    allow_negative: bool = False,
    max_value: float = 1_000_000.0
) -> Optional[float]:
    """
    Phase 2.7.3: Strict financial numeric validation.
    Enforces:
    - Rejects booleans (isinstance(True, int) == True)
    - Rejects NaN, +Infinity, -Infinity
    - Rejects negative values unless allow_negative=True
    - Rejects out-of-range values exceeding max_value
    - Supports Decimal with precision checks
    - Normalizes precision consistently using round_cost
    """
    if val is None:
        if allow_none:
            return None
        raise ValueError(f"{field_name} cannot be None.")

    if isinstance(val, bool):
        raise ValueError(f"{field_name} cannot be a boolean (got {val}).")

    if isinstance(val, Decimal):
        if val.is_nan():
            raise ValueError(f"{field_name} must be a finite number (got NaN).")
        if val.is_infinite():
            raise ValueError(f"{field_name} must be a finite number (got Infinity).")
        num = float(val)
    else:
        try:
            num = float(val)
        except (ValueError, TypeError):
            raise ValueError(f"{field_name} must be a valid numeric value, got: {val!r}")

    import math
    if not math.isfinite(num):
        raise ValueError(f"{field_name} must be a finite number (got {val}).")

    if not allow_negative and num < 0.0:
        raise ValueError(f"'{field_name}' must be non-negative (got {num}).")

    if abs(num) > max_value:
        raise ValueError(f"{field_name} exceeds maximum allowable threshold ({max_value}).")

    return round_cost(num, 6)

def validate_token_count(
    val: Any,
    field_name: str = "tokens",
    allow_negative: bool = False,
    max_tokens: int = 1_000_000_000
) -> int:
    """
    Phase 2.7.3: Strict token count integer validation.
    Enforces:
    - Rejects booleans (True/False)
    - Rejects NaN, +Infinity, -Infinity
    - Rejects floats with fractional part (e.g. 10.5)
    - Rejects malformed strings
    - Rejects negative tokens unless allow_negative=True
    - Enforces maximum threshold
    """
    if val is None:
        raise ValueError(f"{field_name} cannot be None.")

    if isinstance(val, bool):
        raise ValueError(f"{field_name} cannot be a boolean (got {val}).")

    import math
    if isinstance(val, float):
        if not math.isfinite(val):
            raise ValueError(f"{field_name} must be a finite integer (got {val}).")
        if not val.is_integer():
            raise ValueError(f"{field_name} cannot be fractional (got {val}).")
        num = int(val)
    elif isinstance(val, str):
        s = val.strip()
        if not re.match(r"^-?\d+$", s):
            raise ValueError(f"{field_name} must be a valid integer string (got {val!r}).")
        num = int(s)
    elif isinstance(val, int):
        num = val
    elif isinstance(val, Decimal):
        if val.is_nan() or val.is_infinite():
            raise ValueError(f"{field_name} must be a finite integer.")
        if val % 1 != 0:
            raise ValueError(f"{field_name} cannot be fractional (got {val}).")
        num = int(val)
    else:
        raise ValueError(f"{field_name} must be an integer, got: {val!r}")

    if not allow_negative and num < 0:
        raise ValueError(f"'{field_name}' must be non-negative (got {num}).")

    if abs(num) > max_tokens:
        raise ValueError(f"{field_name} exceeds maximum allowable threshold ({max_tokens}).")

    return num

def is_process_alive(pid: int) -> bool:
    """
    Check if a process with given PID is actively running.
    Works reliably on Windows and POSIX systems.
    """
    if pid <= 0:
        return False
    if sys.platform == "win32":
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            SYNCHRONIZE = 0x00100000
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE, False, pid)
            if not handle:
                return False
            exit_code = ctypes.c_ulong()
            still_active = 259  # STILL_ACTIVE
            if kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                is_active = (exit_code.value == still_active)
            else:
                is_active = False
            kernel32.CloseHandle(handle)
            return is_active
        except Exception:
            return False
    else:
        try:
            os.kill(pid, 0)
            return True
        except OSError as err:
            import errno
            return err.errno == errno.EPERM

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
            cost_booked REAL NOT NULL DEFAULT 0.0,
            token_source TEXT NOT NULL DEFAULT 'estimated',
            cost_source TEXT NOT NULL DEFAULT 'estimated',
            provider TEXT,
            model TEXT,
            provider_request_id TEXT,
            reconciliation_status TEXT NOT NULL DEFAULT 'none',
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

        # 8. Rollback Recovery Records table (Phase 2.5 Incomplete Rollback Reconciliation)
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS rollback_recovery_records (
            recovery_id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            delivery_id TEXT,
            app_dir TEXT NOT NULL,
            relative_path TEXT NOT NULL,
            backup_path TEXT NOT NULL,
            original_sha256 TEXT,
            candidate_sha256 TEXT,
            status TEXT NOT NULL CHECK(status IN ('pending', 'in_progress', 'restored', 'conflicted', 'failed', 'completed')),
            error_message TEXT,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
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
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_rollback_recovery_session ON rollback_recovery_records(session_id);")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_rollback_recovery_status ON rollback_recovery_records(status);")

        # 9. Reconciliation Journal table (Phase 2.6 Dual-Ledger Financial Reconciliation Outbox)
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS reconciliation_journal (
            journal_id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL,
            target_tokens INTEGER NOT NULL,
            delta_tokens INTEGER NOT NULL,
            delta_cost REAL NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('applied_db', 'completed', 'failed')),
            provider_request_id TEXT,
            created_at REAL NOT NULL,
            completed_at REAL,
            target_cost REAL NOT NULL DEFAULT 0.0
        );
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_rec_journal_run ON reconciliation_journal(run_id);")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_rec_journal_status ON reconciliation_journal(status);")

        # 10. Recovery Audit Log table (Phase 2.6 Security & Privileged Operation Audit)
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS recovery_audit_log (
            audit_id TEXT PRIMARY KEY,
            timestamp REAL NOT NULL,
            principal TEXT NOT NULL,
            action TEXT NOT NULL,
            target_id TEXT NOT NULL,
            force_requested INTEGER NOT NULL,
            authorized INTEGER NOT NULL,
            outcome TEXT NOT NULL,
            error_message TEXT
        );
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_rec_audit_target ON recovery_audit_log(target_id);")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_rec_audit_time ON recovery_audit_log(timestamp);")

        # 11. Idempotency Keys table (Phase 2.7.2 Financial & Administrative Idempotency)
        cursor.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='idempotency_keys';")
        idemp_sql_row = cursor.fetchone()
        if idemp_sql_row is None:
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS idempotency_keys (
                key TEXT PRIMARY KEY,
                scope TEXT NOT NULL,
                target_id TEXT NOT NULL,
                request_hash TEXT NOT NULL,
                response_json TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL CHECK(status IN ('in_progress', 'committed_pending_projection', 'completed', 'failed_retryable', 'failed_terminal')),
                owner_token TEXT NOT NULL DEFAULT '',
                lease_expires_at REAL NOT NULL DEFAULT 0.0,
                journal_id TEXT,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                completed_at REAL
            );
            """)
        elif "committed_pending_projection" not in idemp_sql_row[0]:
            cursor.execute("PRAGMA foreign_keys=OFF;")
            cursor.execute("ALTER TABLE idempotency_keys RENAME TO idempotency_keys_old;")
            cursor.execute("""
            CREATE TABLE idempotency_keys (
                key TEXT PRIMARY KEY,
                scope TEXT NOT NULL,
                target_id TEXT NOT NULL,
                request_hash TEXT NOT NULL,
                response_json TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL CHECK(status IN ('in_progress', 'committed_pending_projection', 'completed', 'failed_retryable', 'failed_terminal')),
                owner_token TEXT NOT NULL DEFAULT '',
                lease_expires_at REAL NOT NULL DEFAULT 0.0,
                journal_id TEXT,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                completed_at REAL
            );
            """)
            cursor.execute("PRAGMA table_info(idempotency_keys_old);")
            old_cols = {row[1] for row in cursor.fetchall()}
            owner_sel = "owner_token" if "owner_token" in old_cols else "''"
            lease_sel = "lease_expires_at" if "lease_expires_at" in old_cols else "0.0"
            journal_sel = "journal_id" if "journal_id" in old_cols else "NULL"
            cursor.execute(f"""
            INSERT INTO idempotency_keys (key, scope, target_id, request_hash, response_json, status, owner_token, lease_expires_at, journal_id, created_at, updated_at, completed_at)
            SELECT key, scope, target_id, request_hash, response_json,
                   CASE
                       WHEN status = 'failed' THEN 'failed_terminal'
                       WHEN status = 'in_progress' THEN 'failed_retryable'
                       ELSE status
                   END,
                   {owner_sel}, {lease_sel}, {journal_sel},
                   created_at, created_at, completed_at
            FROM idempotency_keys_old;
            """)
            cursor.execute("DROP TABLE idempotency_keys_old;")
            cursor.execute("PRAGMA foreign_keys=ON;")
            cursor.execute("PRAGMA foreign_key_check;")

        cursor.execute("PRAGMA table_info(idempotency_keys);")
        idemp_cols = {row[1] for row in cursor.fetchall()}
        if "owner_token" not in idemp_cols:
            cursor.execute("ALTER TABLE idempotency_keys ADD COLUMN owner_token TEXT NOT NULL DEFAULT '';")
        if "lease_expires_at" not in idemp_cols:
            cursor.execute("ALTER TABLE idempotency_keys ADD COLUMN lease_expires_at REAL NOT NULL DEFAULT 0.0;")
        if "journal_id" not in idemp_cols:
            cursor.execute("ALTER TABLE idempotency_keys ADD COLUMN journal_id TEXT;")

        cursor.execute("CREATE INDEX IF NOT EXISTS idx_idemp_scope_target ON idempotency_keys(scope, target_id);")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_idemp_status ON idempotency_keys(status);")

        # Ensure token_source, provider_request_id, reconciliation_status, provider, model, cost_booked, and cost_source columns exist in agent_runs table
        cursor.execute("PRAGMA table_info(agent_runs);")
        run_cols = {row[1] for row in cursor.fetchall()}
        if "token_source" not in run_cols:
            cursor.execute("ALTER TABLE agent_runs ADD COLUMN token_source TEXT DEFAULT 'estimated';")
        if "provider_request_id" not in run_cols:
            cursor.execute("ALTER TABLE agent_runs ADD COLUMN provider_request_id TEXT;")
        if "reconciliation_status" not in run_cols:
            cursor.execute("ALTER TABLE agent_runs ADD COLUMN reconciliation_status TEXT DEFAULT 'none';")
        if "provider" not in run_cols:
            cursor.execute("ALTER TABLE agent_runs ADD COLUMN provider TEXT;")
        if "model" not in run_cols:
            cursor.execute("ALTER TABLE agent_runs ADD COLUMN model TEXT;")
        if "cost_booked" not in run_cols:
            cursor.execute("ALTER TABLE agent_runs ADD COLUMN cost_booked REAL DEFAULT 0.0;")
        if "cost_source" not in run_cols:
            cursor.execute("ALTER TABLE agent_runs ADD COLUMN cost_source TEXT DEFAULT 'estimated';")
            cursor.execute("""
                UPDATE agent_runs
                SET cost_source = CASE
                    WHEN token_source IN ('provider_confirmed', 'provider_reported') THEN 'provider_confirmed'
                    WHEN token_source = 'manual_operator_adjustment' THEN 'manual_operator_adjustment'
                    ELSE 'estimated'
                END
                WHERE cost_source IS NULL OR cost_source = 'estimated';
            """)

        cursor.execute("PRAGMA table_info(reconciliation_journal);")
        j_cols = {row[1] for row in cursor.fetchall()}
        if "target_cost" not in j_cols:
            cursor.execute("ALTER TABLE reconciliation_journal ADD COLUMN target_cost REAL DEFAULT 0.0;")

        cursor.execute("CREATE INDEX IF NOT EXISTS idx_agent_runs_provider ON agent_runs(provider);")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_agent_runs_cost_source ON agent_runs(cost_source);")

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

        # 12. QA Findings table (Phase 2.8 Independent QA & Quality Gates)
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS qa_findings (
            finding_id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            task_id TEXT,
            qa_run_id TEXT NOT NULL,
            severity TEXT NOT NULL CHECK(severity IN ('critical', 'high', 'medium', 'low', 'warning')),
            category TEXT NOT NULL CHECK(category IN ('security', 'syntax', 'contract', 'accessibility', 'integrity', 'performance')),
            artifact_name TEXT NOT NULL,
            evidence TEXT,
            remediation_recommendation TEXT,
            responsible_agent_id TEXT,
            status TEXT NOT NULL CHECK(status IN ('open', 'remediated', 'accepted', 'verified_resolved')),
            verification_history_json TEXT NOT NULL DEFAULT '[]',
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            FOREIGN KEY(session_id) REFERENCES workflow_sessions(session_id) ON DELETE CASCADE
        );
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_qa_findings_session ON qa_findings(session_id);")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_qa_findings_status ON qa_findings(status);")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_qa_findings_severity ON qa_findings(severity);")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_qa_findings_run ON qa_findings(qa_run_id);")

        # 13. Human Approvals table (Phase 2.8 Approval & Authorization Lifecycle)
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS human_approvals (
            approval_id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            task_id TEXT,
            action_type TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('pending', 'approved', 'rejected', 'expired', 'cancelled')),
            requested_by TEXT NOT NULL,
            requested_at REAL NOT NULL,
            expires_at REAL NOT NULL,
            artifact_revision_hash TEXT,
            policy_rule TEXT,
            decision_by TEXT,
            decision_at REAL,
            decision_reason TEXT,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            FOREIGN KEY(session_id) REFERENCES workflow_sessions(session_id) ON DELETE CASCADE
        );
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_approvals_session ON human_approvals(session_id);")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_approvals_status ON human_approvals(status);")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_approvals_action ON human_approvals(action_type);")

        # 14. QA Runs table (Phase 2.8 Trusted QA Certification & Execution Evidence)
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS qa_runs (
            qa_run_id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            task_id TEXT,
            artifact_revision TEXT NOT NULL,
            validation_status TEXT NOT NULL CHECK(validation_status IN ('passed', 'failed')),
            checks_json TEXT NOT NULL DEFAULT '{}',
            errors_json TEXT NOT NULL DEFAULT '[]',
            created_at REAL NOT NULL,
            FOREIGN KEY(session_id) REFERENCES workflow_sessions(session_id) ON DELETE CASCADE
        );
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_qa_runs_session ON qa_runs(session_id);")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_qa_runs_status ON qa_runs(validation_status);")

        # Phase 2.8 schema enhancements to qa_findings and artifact_manifests (artifact_revision binding)
        cursor.execute("PRAGMA table_info(qa_findings);")
        qaf_cols = {row[1] for row in cursor.fetchall()}
        if "artifact_revision" not in qaf_cols:
            cursor.execute("ALTER TABLE qa_findings ADD COLUMN artifact_revision TEXT;")

        cursor.execute("PRAGMA table_info(artifact_manifests);")
        man_cols = {row[1] for row in cursor.fetchall()}
        if "artifact_revision" not in man_cols:
            cursor.execute("ALTER TABLE artifact_manifests ADD COLUMN artifact_revision TEXT;")

        # 15. Phase 2.8 schema enhancements to task_handoffs and workflow_tasks
        cursor.execute("PRAGMA table_info(task_handoffs);")
        hoff_cols = {row[1] for row in cursor.fetchall()}
        if "contract_version" not in hoff_cols:
            cursor.execute("ALTER TABLE task_handoffs ADD COLUMN contract_version TEXT DEFAULT '1.0';")
        if "trace_id" not in hoff_cols:
            cursor.execute("ALTER TABLE task_handoffs ADD COLUMN trace_id TEXT;")
        if "validation_outcome" not in hoff_cols:
            cursor.execute("ALTER TABLE task_handoffs ADD COLUMN validation_outcome TEXT DEFAULT 'valid';")
        if "required_context_json" not in hoff_cols:
            cursor.execute("ALTER TABLE task_handoffs ADD COLUMN required_context_json TEXT DEFAULT '{}';")

        cursor.execute("PRAGMA table_info(workflow_tasks);")
        task_cols = {row[1] for row in cursor.fetchall()}
        if "required_capabilities_json" not in task_cols:
            cursor.execute("ALTER TABLE workflow_tasks ADD COLUMN required_capabilities_json TEXT DEFAULT '[]';")
        if "output_contract" not in task_cols:
            cursor.execute("ALTER TABLE workflow_tasks ADD COLUMN output_contract TEXT;")
        if "assigned_reason" not in task_cols:
            cursor.execute("ALTER TABLE workflow_tasks ADD COLUMN assigned_reason TEXT;")

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
    required_capabilities: Optional[List[str]] = None,
    output_contract: Optional[str] = None,
    assigned_reason: Optional[str] = None,
    db_path: Optional[str] = None
) -> str:
    task_id = f"task_{uuid.uuid4().hex[:12]}"
    deps_json = json.dumps(dependencies or [])
    caps_json = json.dumps(required_capabilities or [])
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

        cols = [c[1] for c in conn.execute("PRAGMA table_info(workflow_tasks);").fetchall()]
        if "required_capabilities_json" in cols:
            conn.execute("""
                INSERT INTO workflow_tasks (
                    task_id, session_id, parent_task_id, title, division_id,
                    assigned_agent_id, stage, status, dependencies_json, input_context, max_retries, output_text,
                    required_capabilities_json, output_contract, assigned_reason
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            """, (task_id, session_id, parent_task_id, title, division_id, assigned_agent_id, stage, initial_status, deps_json, input_context, max_retries, output_text, caps_json, output_contract, assigned_reason))
        else:
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
        res["required_capabilities"] = json.loads(res.get("required_capabilities_json") or "[]")
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
            d["required_capabilities"] = json.loads(d.get("required_capabilities_json") or "[]")
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
    provider_request_id: Optional[str] = None,
    provider: Optional[str] = None,
    model: Optional[str] = None,
    cost_booked: Optional[float] = None,
    cost_source: Optional[str] = None,
    db_path: Optional[str] = None
) -> str:
    run_id = f"run_{uuid.uuid4().hex[:12]}"
    now = time.time()
    fin = now if status in ('completed', 'failed') else None
    safe_input = sanitize_memory_content(input_preview[:500]) if input_preview else None
    safe_output = sanitize_memory_content(output_preview[:500]) if output_preview else None
    safe_error = sanitize_memory_content(error_message[:500]) if error_message else None
    if provider_request_id is not None and not isinstance(provider_request_id, str):
        provider_request_id = None
    if provider is not None and not isinstance(provider, str):
        provider = str(provider)
    if model is not None and not isinstance(model, str):
        model = str(model)
    if tokens_used is not None:
        tokens_used = validate_token_count(tokens_used, field_name="tokens_used", allow_negative=False)
    else:
        tokens_used = 0
    if cost_booked is not None:
        cost_booked = validate_financial_numeric(cost_booked, field_name="cost_booked", allow_none=True, allow_negative=False)
    initial_cost = round_cost(cost_booked if cost_booked is not None else (tokens_used / 1_000_000.0) * 0.15, 6)
    effective_cost_source = cost_source or ("provider_confirmed" if token_source in ("provider_confirmed", "provider_reported") else ("manual_operator_adjustment" if token_source == "manual_operator_adjustment" else "estimated"))
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
                up_fields = [
                    "status = ?",
                    "input_preview = COALESCE(?, input_preview)",
                    "output_preview = COALESCE(?, output_preview)",
                    "tokens_used = ?",
                    "token_source = ?",
                    "finished_at = ?",
                    "error_message = ?"
                ]
                up_params = [status, safe_input, safe_output, tokens_used, token_source, fin, safe_error]
                if "cost_booked" in cols:
                    up_fields.append("cost_booked = ?")
                    up_params.append(initial_cost)
                if "cost_source" in cols:
                    up_fields.append("cost_source = ?")
                    up_params.append(effective_cost_source)
                if "provider_request_id" in cols and provider_request_id:
                    up_fields.append("provider_request_id = COALESCE(?, provider_request_id)")
                    up_params.append(provider_request_id)
                if "provider" in cols and provider:
                    up_fields.append("provider = COALESCE(?, provider)")
                    up_params.append(provider)
                if "model" in cols and model:
                    up_fields.append("model = COALESCE(?, model)")
                    up_params.append(model)
                up_params.append(run_id)
                cursor.execute(f"UPDATE agent_runs SET {', '.join(up_fields)} WHERE run_id = ?;", up_params)
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
            ins_cols = [
                "run_id", "task_id", "session_id", "agent_id", "agent_name",
                "division_id", "prompt_file", "status", "input_preview",
                "output_preview", "tokens_used"
            ]
            ins_vals = [
                run_id, task_id, session_id, agent_id, agent_name,
                division_id, prompt_file, status,
                safe_input, safe_output, tokens_used
            ]
            if "cost_booked" in cols:
                ins_cols.append("cost_booked")
                ins_vals.append(initial_cost)
            if "cost_source" in cols:
                ins_cols.append("cost_source")
                ins_vals.append(effective_cost_source)
            if "token_source" in cols:
                ins_cols.append("token_source")
                ins_vals.append(token_source)
            if "provider_request_id" in cols:
                ins_cols.append("provider_request_id")
                ins_vals.append(provider_request_id)
            if "provider" in cols:
                ins_cols.append("provider")
                ins_vals.append(provider)
            if "model" in cols:
                ins_cols.append("model")
                ins_vals.append(model)
            ins_cols.extend(["error_message", "started_at", "finished_at"])
            ins_vals.extend([safe_error, now, fin])
            placeholders = ", ".join(["?"] * len(ins_cols))
            cursor.execute(
                f"INSERT INTO agent_runs ({', '.join(ins_cols)}) VALUES ({placeholders});",
                ins_vals
            )
        conn.commit()
    return run_id

def list_agent_runs(session_id: str, task_id: Optional[str] = None, db_path: Optional[str] = None) -> List[Dict[str, Any]]:
    with get_db_connection(db_path) as conn:
        if task_id:
            rows = conn.execute("SELECT * FROM agent_runs WHERE session_id = ? AND task_id = ? ORDER BY started_at ASC;", (session_id, task_id)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM agent_runs WHERE session_id = ? ORDER BY started_at ASC;", (session_id,)).fetchall()
        return [dict(r) for r in rows]

def get_agent_run(run_id: str, db_path: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Retrieve an individual agent run by run_id."""
    with get_db_connection(db_path) as conn:
        row = conn.execute("SELECT * FROM agent_runs WHERE run_id = ?;", (run_id,)).fetchone()
        return dict(row) if row else None


def record_agent_run_start(
    task_id: str,
    session_id: str,
    agent_id: str,
    agent_name: str,
    division_id: str,
    prompt_file: str,
    input_preview: Optional[str] = None,
    provider: Optional[str] = None,
    model: Optional[str] = None,
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
    if provider is not None and not isinstance(provider, str):
        provider = str(provider)
    if model is not None and not isinstance(model, str):
        model = str(model)
    with get_db_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("PRAGMA table_info(agent_runs);")
        cols = {row[1] for row in cursor.fetchall()}
        ins_cols = [
            "run_id", "task_id", "session_id", "agent_id", "agent_name",
            "division_id", "prompt_file", "status", "input_preview",
            "tokens_used", "started_at"
        ]
        ins_vals = [
            run_id, task_id, session_id, agent_id, agent_name,
            division_id, prompt_file, 'running', safe_input,
            0, now
        ]
        if "token_source" in cols:
            ins_cols.append("token_source")
            ins_vals.append("estimated")
        if "cost_source" in cols:
            ins_cols.append("cost_source")
            ins_vals.append("estimated")
        if "provider" in cols:
            ins_cols.append("provider")
            ins_vals.append(provider)
        if "model" in cols:
            ins_cols.append("model")
            ins_vals.append(model)

        placeholders = ", ".join(["?"] * len(ins_cols))
        conn.execute(
            f"INSERT INTO agent_runs ({', '.join(ins_cols)}) VALUES ({placeholders});",
            ins_vals
        )
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
    provider_request_id: Optional[str] = None,
    provider: Optional[str] = None,
    model: Optional[str] = None,
    cost_booked: Optional[float] = None,
    cost_source: Optional[str] = None,
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
    if cost_booked is not None:
        cost_booked = validate_financial_numeric(cost_booked, field_name="cost_booked", allow_none=True, allow_negative=False)
    if provider_request_id is not None and not isinstance(provider_request_id, str):
        provider_request_id = None
    if provider is not None and not isinstance(provider, str):
        provider = str(provider)
    if model is not None and not isinstance(model, str):
        model = str(model)

    now = time.time()
    safe_input = sanitize_memory_content(input_preview[:500]) if input_preview else None
    effective_out_prev = output_preview or output_text
    safe_output = sanitize_memory_content(effective_out_prev[:500]) if effective_out_prev else None
    safe_error = sanitize_memory_content(error_message) if error_message else None
    initial_cost = round_cost(cost_booked if cost_booked is not None else (tokens_used / 1_000_000.0) * 0.15, 6)
    effective_cost_source = cost_source or ("provider_confirmed" if token_source in ("provider_confirmed", "provider_reported") else ("manual_operator_adjustment" if token_source == "manual_operator_adjustment" else "estimated"))
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
                up_fields = [
                    "status = 'completed'",
                    "input_preview = COALESCE(?, input_preview)",
                    "output_preview = ?",
                    "tokens_used = ?",
                    "token_source = ?",
                    "finished_at = ?",
                    "error_message = ?"
                ]
                up_params = [safe_input, safe_output, tokens_used, token_source, now, safe_error]
                if "cost_booked" in cols:
                    up_fields.append("cost_booked = ?")
                    up_params.append(initial_cost)
                if "cost_source" in cols:
                    up_fields.append("cost_source = ?")
                    up_params.append(effective_cost_source)
                if "provider_request_id" in cols and provider_request_id:
                    up_fields.append("provider_request_id = COALESCE(?, provider_request_id)")
                    up_params.append(provider_request_id)
                if "provider" in cols and provider:
                    up_fields.append("provider = COALESCE(?, provider)")
                    up_params.append(provider)
                if "model" in cols and model:
                    up_fields.append("model = COALESCE(?, model)")
                    up_params.append(model)
                up_params.append(run_id)
                cursor.execute(f"UPDATE agent_runs SET {', '.join(up_fields)} WHERE run_id = ?;", up_params)
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
                up_fields = [
                    "input_preview = COALESCE(?, input_preview)",
                    "output_preview = ?",
                    "tokens_used = ?",
                    "token_source = ?",
                    "finished_at = ?",
                    "error_message = ?"
                ]
                up_params = [safe_input, safe_output, tokens_used, token_source, now, safe_error]
                if "cost_booked" in cols:
                    up_fields.append("cost_booked = ?")
                    up_params.append(initial_cost)
                if "cost_source" in cols:
                    up_fields.append("cost_source = ?")
                    up_params.append(effective_cost_source)
                if "provider_request_id" in cols and provider_request_id:
                    up_fields.append("provider_request_id = COALESCE(?, provider_request_id)")
                    up_params.append(provider_request_id)
                if "provider" in cols and provider:
                    up_fields.append("provider = COALESCE(?, provider)")
                    up_params.append(provider)
                if "model" in cols and model:
                    up_fields.append("model = COALESCE(?, model)")
                    up_params.append(model)
                up_params.append(run_id)
                cursor.execute(f"UPDATE agent_runs SET {', '.join(up_fields)} WHERE run_id = ?;", up_params)
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
            ins_cols = [
                "run_id", "task_id", "session_id", "agent_id", "agent_name",
                "division_id", "prompt_file", "status", "input_preview",
                "output_preview", "tokens_used"
            ]
            ins_vals = [
                run_id, task_id, session_id, agent_id, agent_name,
                division_id, prompt_file, 'completed',
                safe_input, safe_output, tokens_used
            ]
            if "cost_booked" in cols:
                ins_cols.append("cost_booked")
                ins_vals.append(initial_cost)
            if "cost_source" in cols:
                ins_cols.append("cost_source")
                ins_vals.append(effective_cost_source)
            if "token_source" in cols:
                ins_cols.append("token_source")
                ins_vals.append(token_source)
            if "provider_request_id" in cols:
                ins_cols.append("provider_request_id")
                ins_vals.append(provider_request_id)
            if "provider" in cols:
                ins_cols.append("provider")
                ins_vals.append(provider)
            if "model" in cols:
                ins_cols.append("model")
                ins_vals.append(model)
            ins_cols.extend(["error_message", "started_at", "finished_at"])
            ins_vals.extend([safe_error, now, now])
            placeholders = ", ".join(["?"] * len(ins_cols))
            cursor.execute(
                f"INSERT INTO agent_runs ({', '.join(ins_cols)}) VALUES ({placeholders});",
                ins_vals
            )

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

class HandoffResult(str):
    def __new__(cls, val, data=None):
        s = super().__new__(cls, str(val))
        s._data = data or {}
        return s
    def __getitem__(self, item):
        if isinstance(item, str):
            return self._data.get(item)
        return super().__getitem__(item)
    def get(self, key, default=None):
        return self._data.get(key, default)

class FindingResult(str):
    def __new__(cls, val, data=None):
        s = super().__new__(cls, str(val))
        s._data = data or {}
        return s
    def __getitem__(self, item):
        if isinstance(item, str):
            return self._data.get(item)
        return super().__getitem__(item)
    def get(self, key, default=None):
        return self._data.get(key, default)

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
    contract_version: str = "1.0",
    trace_id: Optional[str] = None,
    validation_outcome: str = "valid",
    required_context: Optional[Dict[str, Any]] = None,
    db_path: Optional[str] = None
) -> HandoffResult:
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
    req_ctx_json = json.dumps(required_context or {}, ensure_ascii=False)
    
    # Payload bounds verification: reject oversized payloads
    if len(summary) > 100000 or len(payload_json) > 100000:
        raise ValueError("Handoff artifact_summary exceeds max payload (100,000 characters)")
    if not contract_version or not isinstance(contract_version, str):
        raise ValueError("Handoff contract_version must be a non-empty string")
    
    with get_db_connection(db_path) as conn:
        cursor = conn.cursor()

        # 1. Destination task verification if it exists in DB
        if to_task_id:
            dst_task = cursor.execute(
                "SELECT session_id, assigned_agent_id, dependencies_json FROM workflow_tasks WHERE task_id = ?;", 
                (to_task_id,)
            ).fetchone()
            if dst_task:
                dst_d = dict(dst_task)
                if dst_d["session_id"] != session_id:
                    raise ValueError(
                        f"Cross-session handoff rejected: destination task {to_task_id} belongs to session {dst_d['session_id']}, not {session_id}"
                    )
                if from_task_id != to_task_id:
                    if dst_d.get("assigned_agent_id") and dst_agent not in ("unknown", "production-environment", "user", "external", dst_d["assigned_agent_id"]):
                        raise ValueError(
                            f"Destination agent mismatch: destination task {to_task_id} is assigned to '{dst_d['assigned_agent_id']}', got '{dst_agent}'"
                        )
                    deps = json.loads(dst_d["dependencies_json"] or "[]")
                    if deps and from_task_id not in deps:
                        raise ValueError(
                            f"Invalid handoff: destination task {to_task_id} does not depend on source task {from_task_id} in DAG"
                        )

        # 2. Source task verification if it exists in DB
        src_task = cursor.execute("SELECT session_id, status FROM workflow_tasks WHERE task_id = ?;", (from_task_id,)).fetchone()
        if src_task:
            src_d = dict(src_task)
            if src_d["session_id"] != session_id:
                raise ValueError(
                    f"Cross-session handoff rejected: source task {from_task_id} belongs to session {src_d['session_id']}, not {session_id}"
                )
            if src_d["status"] in ("failed", "cancelled", "blocked"):
                raise ValueError(f"Cannot record successful handoff from {src_d['status']} task {from_task_id}")
            if status in ("delivered", "acknowledged"):
                if from_task_id != to_task_id and src_d["status"] != "completed":
                    raise ValueError(
                        f"Cannot record successful handoff from incomplete task {from_task_id} (status: {src_d['status']}). Source task must be 'completed'."
                    )

        # 3. Enforce deterministic identity and idempotency for handoff records via atomic upsert
        h = hashlib.sha256(f"{session_id}_{from_task_id}_{to_task_id}".encode("utf-8")).hexdigest()[:16]
        handoff_id = f"hoff_{h}"

        cols = [c[1] for c in cursor.execute("PRAGMA table_info(task_handoffs);").fetchall()]
        has_new_cols = "contract_version" in cols

        if has_new_cols:
            try:
                cursor.execute("""
                    INSERT INTO task_handoffs (
                        handoff_id, session_id, from_task_id, to_task_id,
                        from_agent_id, to_agent_id, artifact_type,
                        artifact_summary, status, contract_version, trace_id, validation_outcome,
                        required_context_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(handoff_id) DO UPDATE SET
                        from_agent_id = excluded.from_agent_id,
                        to_agent_id = excluded.to_agent_id,
                        artifact_type = excluded.artifact_type,
                        artifact_summary = excluded.artifact_summary,
                        status = excluded.status,
                        contract_version = excluded.contract_version,
                        trace_id = excluded.trace_id,
                        validation_outcome = excluded.validation_outcome,
                        required_context_json = excluded.required_context_json,
                        created_at = excluded.created_at;
                """, (handoff_id, session_id, from_task_id, to_task_id, src_agent, dst_agent, artifact_type, payload_json, status, contract_version, trace_id, validation_outcome, req_ctx_json, now))
            except sqlite3.OperationalError:
                cursor.execute("""
                    INSERT OR REPLACE INTO task_handoffs (
                        handoff_id, session_id, from_task_id, to_task_id,
                        from_agent_id, to_agent_id, artifact_type,
                        artifact_summary, status, contract_version, trace_id, validation_outcome,
                        required_context_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                """, (handoff_id, session_id, from_task_id, to_task_id, src_agent, dst_agent, artifact_type, payload_json, status, contract_version, trace_id, validation_outcome, req_ctx_json, now))
        else:
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
                cursor.execute("""
                    INSERT OR REPLACE INTO task_handoffs (
                        handoff_id, session_id, from_task_id, to_task_id,
                        from_agent_id, to_agent_id, artifact_type,
                        artifact_summary, status, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                """, (handoff_id, session_id, from_task_id, to_task_id, src_agent, dst_agent, artifact_type, payload_json, status, now))
        conn.commit()

    return HandoffResult(handoff_id, {
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
        "contract_version": contract_version,
        "trace_id": trace_id,
        "validation_outcome": validation_outcome,
        "required_context": required_context or {},
        "status": status,
        "created_at": now
    })

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
            d["contract_version"] = d.get("contract_version", "1.0")
            d["trace_id"] = d.get("trace_id")
            d["validation_outcome"] = d.get("validation_outcome", "valid")
            raw_summary = d.get("artifact_summary") or ""
            if raw_summary.startswith("{"):
                try:
                    parsed = json.loads(raw_summary)
                    if isinstance(parsed, dict) and "artifacts" in parsed:
                        d["context_summary"] = parsed.get("context_summary") or ""
                        d["artifacts"] = parsed.get("artifacts") or {}
                        d["artifact_summary"] = d["context_summary"]
                    else:
                        d["context_summary"] = raw_summary
                        d["artifacts"] = parsed if isinstance(parsed, dict) else {"summary": raw_summary}
                        d["artifact_summary"] = raw_summary
                except Exception:
                    d["context_summary"] = raw_summary
                    d["artifacts"] = {"summary": raw_summary}
                    d["artifact_summary"] = raw_summary
            else:
                d["context_summary"] = raw_summary
                d["artifacts"] = {"summary": raw_summary}
                d["artifact_summary"] = raw_summary
            raw_req = d.get("required_context_json")
            d["required_context"] = json.loads(raw_req) if raw_req else {}
            result.append(d)
        return result

list_task_handoffs = get_session_handoffs


# =========================================================================
# QA Findings & Quality Gates (Phase 2.8 Independent QA Execution)
# =========================================================================

VALID_QA_SEVERITIES = {'critical', 'high', 'medium', 'low', 'warning'}
VALID_QA_CATEGORIES = {'security', 'syntax', 'contract', 'accessibility', 'integrity', 'performance'}
VALID_QA_STATUSES = {'open', 'remediated', 'accepted', 'verified_resolved'}

def record_qa_finding(
    session_id: str,
    qa_run_id: str,
    severity: str,
    category: str,
    artifact_name: str,
    evidence: Optional[str] = None,
    remediation_recommendation: Optional[str] = None,
    responsible_agent_id: Optional[str] = None,
    task_id: Optional[str] = None,
    status: str = "open",
    artifact_revision: Optional[str] = None,
    db_path: Optional[str] = None
) -> Dict[str, Any]:
    """
    Persist structured QA finding with audit trail and artifact revision binding.
    Ensures that verification history and findings remain durable and tamper-resistant.
    """
    if severity not in VALID_QA_SEVERITIES:
        raise ValueError(f"Invalid QA finding severity: '{severity}'. Must be one of {sorted(VALID_QA_SEVERITIES)}")
    if category not in VALID_QA_CATEGORIES:
        raise ValueError(f"Invalid QA finding category: '{category}'. Must be one of {sorted(VALID_QA_CATEGORIES)}")
    if status not in VALID_QA_STATUSES:
        raise ValueError(f"Invalid QA finding status: '{status}'. Must be one of {sorted(VALID_QA_STATUSES)}")

    finding_id = f"fnd_{uuid.uuid4().hex[:12]}"
    now = time.time()
    safe_evidence = sanitize_memory_content(evidence) if evidence else None
    safe_remed = sanitize_memory_content(remediation_recommendation) if remediation_recommendation else None

    with get_db_connection(db_path) as conn:
        conn.execute("""
            INSERT INTO qa_findings (
                finding_id, session_id, task_id, qa_run_id, severity, category,
                artifact_name, evidence, remediation_recommendation, responsible_agent_id,
                status, verification_history_json, artifact_revision, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '[]', ?, ?, ?);
        """, (
            finding_id, session_id, task_id, qa_run_id, severity, category,
            artifact_name, safe_evidence, safe_remed, responsible_agent_id,
            status, artifact_revision, now, now
        ))
        conn.commit()

    return FindingResult(finding_id, {
        "finding_id": finding_id,
        "session_id": session_id,
        "task_id": task_id,
        "qa_run_id": qa_run_id,
        "severity": severity,
        "category": category,
        "artifact_name": artifact_name,
        "evidence": safe_evidence,
        "remediation_recommendation": safe_remed,
        "responsible_agent_id": responsible_agent_id,
        "status": status,
        "verification_history": [],
        "artifact_revision": artifact_revision,
        "created_at": now,
        "updated_at": now
    })

def get_qa_finding(finding_id: Any, db_path: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Retrieve a single QA finding by ID."""
    if isinstance(finding_id, dict):
        finding_id = finding_id.get("finding_id") or finding_id.get("id")
    with get_db_connection(db_path) as conn:
        row = conn.execute("SELECT * FROM qa_findings WHERE finding_id = ?;", (str(finding_id),)).fetchone()
        if not row:
            return None
        d = dict(row)
        d["verification_history"] = json.loads(d.get("verification_history_json") or "[]")
        return d

def list_qa_findings(
    session_id: Optional[str] = None,
    qa_run_id: Optional[str] = None,
    status: Optional[str] = None,
    min_severity: Optional[str] = None,
    severity: Optional[str] = None,
    db_path: Optional[str] = None
) -> List[Dict[str, Any]]:
    """
    List QA findings with optional filters.
    Orders results deterministically by severity (critical first) then created_at.
    """
    severity_order = {"critical": 0, "high": 1, "medium": 2, "low": 3, "warning": 4}
    min_rank = severity_order.get(min_severity, 5) if min_severity else 5

    conditions = []
    params = []
    if session_id:
        conditions.append("session_id = ?")
        params.append(session_id)
    if qa_run_id:
        conditions.append("qa_run_id = ?")
        params.append(qa_run_id)
    if status:
        conditions.append("status = ?")
        params.append(status)
    if severity:
        conditions.append("severity = ?")
        params.append(severity)

    where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    query = f"SELECT * FROM qa_findings {where_clause} ORDER BY created_at ASC;"

    with get_db_connection(db_path) as conn:
        rows = conn.execute(query, params).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d["verification_history"] = json.loads(d.get("verification_history_json") or "[]")
            sev_rank = severity_order.get(d.get("severity"), 99)
            if min_severity and sev_rank > min_rank:
                continue
            result.append(d)

        result.sort(key=lambda x: (severity_order.get(x["severity"], 99), x["created_at"]))
        return result

def update_qa_finding_status(
    finding_id: Any,
    status: Optional[str] = None,
    verified_by_agent: Optional[str] = None,
    note: str = "",
    new_status: Optional[str] = None,
    verification_note: Optional[str] = None,
    db_path: Optional[str] = None
) -> Tuple[bool, str]:
    """
    Update QA finding status with mandatory verification audit trail.
    CRITICAL QUALITY INVARIANT:
    An implementation agent cannot unilaterally mark its own failed QA finding as 'verified_resolved'.
    Independent verification is strictly enforced.
    Returns (success, error_or_empty_str).
    """
    if isinstance(finding_id, dict):
        finding_id = finding_id.get("finding_id") or finding_id.get("id")

    eff_status = new_status or status
    eff_note = verification_note if verification_note is not None else note

    if eff_status not in VALID_QA_STATUSES:
        return (False, f"Invalid QA finding status: '{eff_status}'. Must be one of {sorted(VALID_QA_STATUSES)}")

    now = time.time()
    with get_db_connection(db_path) as conn:
        row = conn.execute("SELECT * FROM qa_findings WHERE finding_id = ?;", (finding_id,)).fetchone()
        if not row:
            return (False, f"QA finding {finding_id} not found")

        finding = dict(row)
        # Idempotent state transition guard: preserve audit integrity on repeat runs
        if finding.get("status") == eff_status:
            return (True, "")

        # Prevent self-certification
        if eff_status == "verified_resolved":
            if verified_by_agent and verified_by_agent == finding.get("responsible_agent_id"):
                return (
                    False,
                    f"Self-certification rejected: responsible agent '{verified_by_agent}' cannot verify and resolve their own finding {finding_id}"
                )

        history = json.loads(finding.get("verification_history_json") or "[]")
        history.append({
            "timestamp": now,
            "status": eff_status,
            "verified_by": verified_by_agent,
            "note": sanitize_memory_content(eff_note)
        })
        history_json = json.dumps(history, ensure_ascii=False)

        conn.execute("""
            UPDATE qa_findings
            SET status = ?,
                verification_history_json = ?,
                updated_at = ?
            WHERE finding_id = ?;
        """, (eff_status, history_json, now, finding_id))
        conn.commit()

    return (True, "")


# =========================================================================
# QA Execution Certification & Evidence Layer (Phase 2.8)
# =========================================================================

def record_qa_run(
    qa_run_id: str,
    session_id: str,
    artifact_revision: str,
    validation_status: str,
    task_id: Optional[str] = None,
    checks: Optional[Dict[str, Any]] = None,
    errors: Optional[List[str]] = None,
    db_path: Optional[str] = None
) -> Dict[str, Any]:
    """
    Persist an authoritative QA test execution record linked to session and immutable artifact revision.
    Enforces deterministic validation_status ('passed' or 'failed').
    """
    if not qa_run_id or not str(qa_run_id).strip():
        raise ValueError("qa_run_id is required and cannot be empty")
    if not session_id or not str(session_id).strip():
        raise ValueError("session_id is required and cannot be empty")
    if not artifact_revision or not str(artifact_revision).strip():
        raise ValueError("artifact_revision is required and cannot be empty")
    if validation_status not in ("passed", "failed"):
        raise ValueError(f"Invalid validation_status '{validation_status}', must be 'passed' or 'failed'")

    now = time.time()
    checks_json = json.dumps(checks or {})
    errors_json = json.dumps(errors or [])

    with get_db_connection(db_path) as conn:
        conn.execute("""
            INSERT INTO qa_runs (
                qa_run_id, session_id, task_id, artifact_revision,
                validation_status, checks_json, errors_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(qa_run_id) DO UPDATE SET
                validation_status = excluded.validation_status,
                checks_json = excluded.checks_json,
                errors_json = excluded.errors_json,
                artifact_revision = excluded.artifact_revision;
        """, (qa_run_id, session_id, task_id, artifact_revision, validation_status, checks_json, errors_json, now))
        conn.commit()

    return {
        "qa_run_id": qa_run_id,
        "session_id": session_id,
        "task_id": task_id,
        "artifact_revision": artifact_revision,
        "validation_status": validation_status,
        "checks": checks or {},
        "errors": errors or [],
        "created_at": now
    }


def get_qa_run(qa_run_id: str, db_path: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Retrieve an authoritative persisted QA execution record by qa_run_id."""
    if not qa_run_id:
        return None
    with get_db_connection(db_path) as conn:
        row = conn.execute("SELECT * FROM qa_runs WHERE qa_run_id = ?;", (str(qa_run_id),)).fetchone()
        if not row:
            return None
        d = dict(row)
        try:
            d["checks"] = json.loads(d.get("checks_json") or "{}")
        except Exception:
            d["checks"] = {}
        try:
            d["errors"] = json.loads(d.get("errors_json") or "[]")
        except Exception:
            d["errors"] = []
        return d


def list_qa_runs(session_id: str, db_path: Optional[str] = None) -> List[Dict[str, Any]]:
    """List all persisted QA execution records for a session in reverse chronological order."""
    with get_db_connection(db_path) as conn:
        rows = conn.execute("SELECT * FROM qa_runs WHERE session_id = ? ORDER BY created_at DESC;", (session_id,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["checks"] = json.loads(d.get("checks_json") or "{}")
            except Exception:
                d["checks"] = {}
            try:
                d["errors"] = json.loads(d.get("errors_json") or "[]")
            except Exception:
                d["errors"] = []
            out.append(d)
        return out


# =========================================================================
# Human Approval & Authorization Lifecycle (Phase 2.8 Governance)
# =========================================================================

VALID_APPROVAL_STATUSES = {'pending', 'approved', 'rejected', 'expired', 'cancelled'}
VALID_APPROVAL_ACTIONS = {
    'release_deployment', 'high_cost_execution', 'destructive_action',
    'policy_override', 'manual_adjustment', 'code_remediation', 'production_release', 'release'
}

def create_approval_request(
    session_id: str,
    action_type: str,
    requested_by: str,
    artifact_revision_hash: Optional[str] = None,
    policy_rule: Optional[str] = None,
    timeout_seconds: float = 3600.0,
    expires_in_seconds: Optional[float] = None,
    task_id: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
    db_path: Optional[str] = None
) -> str:
    """
    Create a durable, revision-bound human approval request.
    Idempotent: If an active pending request for the same session, action, and revision exists, returns its ID.
    Returns approval_id: str.
    """
    now = time.time()
    eff_timeout = expires_in_seconds if expires_in_seconds is not None else timeout_seconds
    expires_at = now + max(60.0, float(eff_timeout))
    meta_json = json.dumps(metadata or {}, ensure_ascii=False)

    with get_db_connection(db_path) as conn:
        # Check for existing active pending request for exact same action and revision
        existing = conn.execute("""
            SELECT * FROM human_approvals
            WHERE session_id = ? AND action_type = ? AND status = 'pending';
        """, (session_id, action_type)).fetchall()

        for ex in existing:
            ex_d = dict(ex)
            # Lazily expire if time has passed
            if now > ex_d["expires_at"]:
                conn.execute("UPDATE human_approvals SET status = 'expired' WHERE approval_id = ?;", (ex_d["approval_id"],))
                continue
            # If revision matches, return existing idempotent request id
            if ex_d.get("artifact_revision_hash") == artifact_revision_hash:
                conn.commit()
                return ex_d["approval_id"]

        approval_id = f"apr_{uuid.uuid4().hex[:12]}"
        conn.execute("""
            INSERT INTO human_approvals (
                approval_id, session_id, task_id, action_type, status,
                requested_by, requested_at, expires_at, artifact_revision_hash,
                policy_rule, metadata_json
            ) VALUES (?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?);
        """, (
            approval_id, session_id, task_id, action_type,
            requested_by, now, expires_at, artifact_revision_hash,
            policy_rule, meta_json
        ))
        conn.commit()

    return approval_id

def get_approval_request(approval_id: Any, db_path: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Retrieve an approval request with lazy expiry evaluation."""
    if isinstance(approval_id, dict):
        approval_id = approval_id.get("approval_id") or approval_id.get("id")
    now = time.time()
    with get_db_connection(db_path) as conn:
        row = conn.execute("SELECT * FROM human_approvals WHERE approval_id = ?;", (approval_id,)).fetchone()
        if not row:
            return None
        d = dict(row)
        if d["status"] == "pending" and now > d["expires_at"]:
            conn.execute("UPDATE human_approvals SET status = 'expired' WHERE approval_id = ?;", (approval_id,))
            conn.commit()
            d["status"] = "expired"
        d["metadata"] = json.loads(d.get("metadata_json") or "{}")
        return d

def list_approval_requests(
    session_id: Optional[str] = None,
    status: Optional[str] = None,
    action_type: Optional[str] = None,
    db_path: Optional[str] = None
) -> List[Dict[str, Any]]:
    """List approval requests with lazy expiry updates."""
    now = time.time()
    with get_db_connection(db_path) as conn:
        # Lazily expire overdue pending requests
        conn.execute("""
            UPDATE human_approvals
            SET status = 'expired'
            WHERE status = 'pending' AND expires_at < ?;
        """, (now,))
        conn.commit()

        conditions = []
        params = []
        if session_id:
            conditions.append("session_id = ?")
            params.append(session_id)
        if status:
            conditions.append("status = ?")
            params.append(status)
        if action_type:
            conditions.append("action_type = ?")
            params.append(action_type)

        where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        rows = conn.execute(f"SELECT * FROM human_approvals {where_clause} ORDER BY requested_at DESC;", params).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d["metadata"] = json.loads(d.get("metadata_json") or "{}")
            result.append(d)
        return result

def decide_approval_request(
    approval_id: Any,
    decision: str,
    principal: str,
    reason: str = "",
    current_revision_hash: Optional[str] = None,
    allow_self_approval: bool = False,
    db_path: Optional[str] = None
) -> Tuple[bool, str]:
    """
    Atomically record human approval decision.
    Enforces:
    - Rejects expired requests.
    - Rejects terminal requests (already rejected/cancelled).
    - Idempotent on repeated identical approval.
    - Binds decision strictly to candidate artifact revision hash.
    - Blocks unauthorized self-approval unless explicitly permitted.
    Returns (success: bool, error_or_empty_str: str).
    """
    if isinstance(approval_id, dict):
        approval_id = approval_id.get("approval_id") or approval_id.get("id")

    decision_clean = str(decision).strip().lower()
    decision_map = {
        "approve": "approved",
        "approved": "approved",
        "reject": "rejected",
        "rejected": "rejected",
        "cancel": "cancelled",
        "cancelled": "cancelled"
    }
    if decision_clean not in decision_map:
        return (False, f"Invalid approval decision: '{decision}'. Must be one of ('approve', 'approved', 'reject', 'rejected', 'cancel', 'cancelled')")
    target_status = decision_map[decision_clean]

    now = time.time()
    with get_db_connection(db_path) as conn:
        row = conn.execute("SELECT * FROM human_approvals WHERE approval_id = ?;", (approval_id,)).fetchone()
        if not row:
            return (False, f"Approval request {approval_id} not found")

        req = dict(row)

        # 1. Expiration check
        if req["status"] == "expired" or (req["status"] == "pending" and now > req["expires_at"]):
            conn.execute("UPDATE human_approvals SET status = 'expired' WHERE approval_id = ?;", (approval_id,))
            conn.commit()
            return (False, f"Cannot decide on expired approval request {approval_id}")

        # 2. Terminal state check
        if req["status"] in ("rejected", "cancelled"):
            return (False, f"Cannot decide on terminal approval request (status: {req['status']})")

        # 3. Idempotency check
        if req["status"] == "approved":
            if target_status == "approved" and req.get("decision_by") == principal:
                return (True, "")
            return (False, f"Approval request {approval_id} has already been approved by '{req.get('decision_by')}'")

        # 4. Revision-bound staleness check
        if current_revision_hash and req.get("artifact_revision_hash"):
            if current_revision_hash != req["artifact_revision_hash"]:
                return (
                    False,
                    f"Revision mismatch: candidate artifact revision ({current_revision_hash[:8]}) does not match approval requested revision ({req['artifact_revision_hash'][:8]})"
                )

        # 5. Authorization / Self-approval policy check
        if not allow_self_approval and principal and principal == req.get("requested_by"):
            return (
                False,
                f"Self-approval is forbidden: requester '{principal}' cannot approve their own request under strict authorization policy"
            )

        # 6. Apply decision atomically
        conn.execute("""
            UPDATE human_approvals
            SET status = ?,
                decision_by = ?,
                decision_at = ?,
                decision_reason = ?
            WHERE approval_id = ?;
        """, (target_status, principal, now, sanitize_memory_content(reason), approval_id))
        conn.commit()

    return (True, "")


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

def reconcile_interrupted_sessions(db_path: Optional[str] = None, budget_path: Optional[str] = None) -> int:
    """
    Called upon server startup to reconcile any sessions left in 'running' state
    due to unexpected server restart or power loss. Never falsely marks them as completed.
    Safely reconciles interrupted running tasks via bounded retry without duplicating token costs.
    Also cleans up stale running tasks belonging to cancelled or failed sessions,
    cleans up abandoned staging files from interrupted restorations (Phase 2.6-B1),
    and drains any pending reconciliation outbox entries to the budget ledger (Phase 2.6-A2).
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

        # 5. Crash-Consistent Recovery for In-Flight Rollback Records (Phase 2.5)
        # Reconcile any rollback_recovery_records stuck in 'in_progress' or 'restored'
        try:
            r_table = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='rollback_recovery_records';").fetchone()
            if r_table:
                in_flight_recoveries = conn.execute("""
                    SELECT * FROM rollback_recovery_records
                    WHERE status IN ('in_progress', 'restored');
                """).fetchall()

                for rec in in_flight_recoveries:
                    r_id = rec["recovery_id"]
                    r_app_dir = rec["app_dir"]
                    r_rel_path = rec["relative_path"]
                    r_bak_path = rec["backup_path"]

                    try:
                        r_target = validate_project_artifact_path(r_app_dir, r_rel_path)
                    except Exception:
                        continue

                    bak_exists = os.path.exists(r_bak_path)
                    bak_sha = None
                    if bak_exists:
                        try:
                            with open(r_bak_path, "rb") as bf:
                                bak_sha = hashlib.sha256(bf.read()).hexdigest()
                        except Exception:
                            pass

                    tgt_exists = os.path.exists(r_target)
                    tgt_sha = None
                    if tgt_exists:
                        try:
                            with open(r_target, "rb") as tf:
                                tgt_sha = hashlib.sha256(tf.read()).hexdigest()
                        except Exception:
                            pass

                    if tgt_exists and bak_sha and tgt_sha == bak_sha:
                        # Target file was already restored before process died!
                        # Complete the recovery record and safely unlink the backup file
                        conn.execute("""
                            UPDATE rollback_recovery_records
                            SET status = 'completed', error_message = NULL, updated_at = ?
                            WHERE recovery_id = ?;
                        """, (time.time(), r_id))
                        try:
                            if os.path.exists(r_bak_path):
                                os.remove(r_bak_path)
                        except Exception:
                            pass
                    else:
                        # Target was not restored or differs from backup:
                        # Reset status to 'pending' so it can be resumed by operator
                        conn.execute("""
                            UPDATE rollback_recovery_records
                            SET status = 'pending',
                                error_message = 'Interrupted during recovery. Reset to pending for verified operator execution.',
                                updated_at = ?
                            WHERE recovery_id = ?;
                        """, (time.time(), r_id))

                # Phase 2.6-B1: Scan project app directories from rollback records to prune abandoned .restoring_* files
                distinct_app_dirs = conn.execute("SELECT DISTINCT app_dir FROM rollback_recovery_records;").fetchall()
                for row in distinct_app_dirs:
                    d = row["app_dir"]
                    if d and os.path.isdir(d):
                        cleanup_abandoned_restore_staging(d, max_age_seconds=60.0)

        except Exception as rec_err:
            logging.warning("Error reconciling rollback recovery records: %s", rec_err)

        # 6. Drain pending reconciliation journal entries (Phase 2.6-A2 Dual-Ledger Outbox)
        try:
            j_table = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='reconciliation_journal';").fetchone()
            if j_table:
                pending_journals = conn.execute("SELECT * FROM reconciliation_journal WHERE status = 'applied_db';").fetchall()
                target_budget = budget_path or os.environ.get("BUDGET_FILE_PATH") or os.environ.get("BUDGET_FILE") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "budget.json")
                for pj in pending_journals:
                    try:
                        project_reconciliation_to_budget(
                            budget_path=target_budget,
                            journal_id=pj["journal_id"],
                            run_id=pj["run_id"],
                            delta_tokens=pj["delta_tokens"],
                            delta_cost=pj["delta_cost"]
                        )
                        conn.execute(
                            "UPDATE reconciliation_journal SET status = 'completed', completed_at = ? WHERE journal_id = ?;",
                            (time.time(), pj["journal_id"])
                        )
                        conn.execute(
                            "UPDATE recovery_audit_log SET outcome = 'success' WHERE target_id = ? AND outcome = 'applied_db';",
                            (pj["run_id"],)
                        )
                        conn.execute(
                            "UPDATE idempotency_keys SET status = 'completed', completed_at = ? WHERE target_id = ? AND status = 'in_progress';",
                            (time.time(), pj["run_id"])
                        )
                    except Exception as drain_err:
                        logging.warning("Error draining reconciliation journal %s: %s", pj["journal_id"], drain_err)
        except Exception as j_err:
            logging.warning("Error checking reconciliation journal: %s", j_err)

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
    artifact_revision: Optional[str] = None,
    db_path: Optional[str] = None
) -> List[Dict[str, Any]]:
    """
    Persist validated artifact manifest entries linked to a specific session,
    QA validation run, atomic publication delivery ID, and immutable artifact revision.
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
            eff_rev = artifact_revision or item.get("artifact_revision")
            
            # Deterministic manifest entry ID
            raw_key = f"{session_id}_{qa_run_id or 'none'}_{delivery_id or 'none'}_{rel_path}"
            mid = f"man_{hashlib.sha256(raw_key.encode('utf-8')).hexdigest()[:16]}"
            
            conn.execute("""
                INSERT INTO artifact_manifests (
                    manifest_id, session_id, qa_run_id, delivery_id,
                    relative_path, file_size, sha256, artifact_type,
                    validation_status, artifact_revision, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(manifest_id) DO UPDATE SET
                    delivery_id = excluded.delivery_id,
                    validation_status = excluded.validation_status,
                    artifact_revision = excluded.artifact_revision;
            """, (mid, session_id, qa_run_id, delivery_id, rel_path, file_size, sha256_hash, artifact_type, val_status, eff_rev, now))
            
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
                "artifact_revision": eff_rev,
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
    - Cost & token accounting (provider_reported, provider_confirmed, estimated, ambiguity status)
    - QA run & self-healing remediation cycles
    - Published artifact manifest & delivery identity
    - Incomplete rollback recovery tracking and conflict detection
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

        # Query rollback recovery records
        rollback_records = []
        try:
            r_table = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='rollback_recovery_records';").fetchone()
            if r_table:
                r_rows = conn.execute("SELECT * FROM rollback_recovery_records WHERE session_id = ? ORDER BY created_at ASC;", (session_id,)).fetchall()
                rollback_records = [dict(r) for r in r_rows]
        except Exception:
            rollback_records = []

    # Token & financial accounting analysis
    total_tokens = 0
    provider_reported_tokens = 0
    provider_confirmed_tokens = 0
    estimated_tokens = 0
    in_flight_runs = []
    provider_reconciliation_pending = False
    
    for r in runs:
        tok = int(r.get("tokens_used") or 0)
        source = r.get("token_source") or "estimated"
        rec_st = r.get("reconciliation_status")
        if source == "provider_confirmed":
            provider_confirmed_tokens += tok
            total_tokens += tok
        elif r.get("status") == "completed":
            total_tokens += tok
            if source == "provider_reported":
                provider_reported_tokens += tok
            else:
                estimated_tokens += tok
        elif r.get("status") in ("in_progress", "pending"):
            in_flight_runs.append(r)

        if rec_st in ("pending", "none") and ("ambiguous" in (r.get("error_message") or "").lower() or r.get("status") == "failed"):
            provider_reconciliation_pending = True

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

    # Rollback recovery state analysis
    has_rollback_records = len(rollback_records) > 0
    recovery_required = any(r.get("status") in ("pending", "in_progress") for r in rollback_records)
    recovery_in_progress = any(r.get("status") == "in_progress" for r in rollback_records)
    recovery_completed = has_rollback_records and all(r.get("status") == "completed" for r in rollback_records)
    artifact_conflict_detected = any(r.get("status") == "conflicted" for r in rollback_records)
    backup_retained = any(os.path.exists(r.get("backup_path", "")) for r in rollback_records)

    # Failure analysis
    failed_task = next((t for t in tasks if t.get("status") == "failed"), None)
    rollback_failed = ("rollback error" in (session.get("error_message") or "").lower()) and not recovery_completed
    if rollback_failed:
        backup_retained = True
    is_retry_safe = not has_ambiguous_provider_call and not rollback_failed and not recovery_required and not artifact_conflict_detected and (session.get("status") in ("interrupted", "failed"))

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
        if artifact_conflict_detected:
            permitted_recovery = ["inspect_diagnostics", "resolve_conflict_manually"]
        elif rollback_failed or recovery_required:
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
            "provider_confirmed_tokens": provider_confirmed_tokens,
            "estimated_tokens": estimated_tokens,
            "potential_cost_incurred": potential_cost_incurred,
            "has_ambiguous_provider_call": has_ambiguous_provider_call,
            "provider_reconciliation_pending": provider_reconciliation_pending,
            "permitted_accounting_actions": ["reconcile_provider_usage"] if (provider_reconciliation_pending or has_ambiguous_provider_call) else [],
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
        "rollback_recovery": {
            "records": rollback_records,
            "recovery_required": recovery_required or rollback_failed,
            "recovery_in_progress": recovery_in_progress,
            "recovery_completed": recovery_completed,
            "artifact_conflict_detected": artifact_conflict_detected,
            "backup_retained": backup_retained
        },
        "failure_analysis": {
            "status": status,
            "failed_at_stage": session.get("current_stage"),
            "failed_task_id": failed_task["task_id"] if failed_task else None,
            "error_message": session.get("error_message"),
            "is_provider_ambiguous": has_ambiguous_provider_call,
            "provider_reconciliation_pending": provider_reconciliation_pending,
            "rollback_failed": rollback_failed,
            "recovery_required": recovery_required or rollback_failed,
            "recovery_in_progress": recovery_in_progress,
            "recovery_completed": recovery_completed,
            "artifact_conflict_detected": artifact_conflict_detected,
            "backup_retained": backup_retained,
            "is_retry_safe": is_retry_safe
        },
        "permitted_recovery_actions": permitted_recovery,
        "manifest": manifest,
        "handoffs": handoffs
    }


# =========================================================================
# Phase 2.5: Incomplete Rollback Discovery, Preview & Safe Reconciliation
# =========================================================================

WINDOWS_RESERVED_DEVICE_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    "COM1", "COM2", "COM3", "COM4", "COM5", "COM6", "COM7", "COM8", "COM9",
    "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9"
}

def validate_project_artifact_path(app_dir: str, rel_path: str) -> str:
    """
    Validate that rel_path is a safe relative artifact path strictly confined inside app_dir.
    Rejects:
    - Empty paths and null bytes
    - Absolute paths, drive prefixes (e.g. C:, D:), UNC paths (\\\\server\\share)
    - Path traversal ('..')
    - Alternate data streams (':')
    - Windows reserved device names (CON, PRN, AUX, NUL, COM1-9, LPT1-9)
    - Symlink escapes resolving outside app_dir
    Returns the resolved absolute path.
    Raises ValueError on violation.
    """
    if not rel_path or not isinstance(rel_path, str) or not rel_path.strip():
        raise ValueError("Artifact path must be a non-empty string.")
    if "\0" in rel_path:
        raise ValueError("Artifact path contains illegal null byte.")
    
    # Check for drive prefixes (e.g. C:) or UNC paths
    drive, _ = os.path.splitdrive(rel_path)
    if drive or rel_path.startswith(("\\\\", "//")):
        raise ValueError(f"Drive prefixes and UNC paths are forbidden: '{rel_path}'")
    
    # Check for alternate data streams (contain ':')
    if ":" in rel_path:
        raise ValueError(f"Alternate data streams are forbidden: '{rel_path}'")

    # Reject absolute paths
    if os.path.isabs(rel_path) or rel_path.startswith(("/", "\\")):
        raise ValueError(f"Absolute artifact paths are forbidden: '{rel_path}'")

    # Normalize separators
    normalized = rel_path.replace("\\", "/").strip("/")
    parts = normalized.split("/")

    # Check each part for traversal or reserved names
    for part in parts:
        if not part or part == ".":
            continue
        if part == "..":
            raise ValueError(f"Path traversal ('..') is forbidden: '{rel_path}'")
        
        # Check Windows reserved device names (e.g. NUL, CON, COM1.txt, etc.)
        stem = part.split(".")[0].upper()
        if stem in WINDOWS_RESERVED_DEVICE_NAMES or part.upper() in WINDOWS_RESERVED_DEVICE_NAMES:
            raise ValueError(f"Windows reserved device name forbidden: '{part}' in '{rel_path}'")

    canonical_app_dir = os.path.realpath(os.path.abspath(app_dir))
    target_abs = os.path.abspath(os.path.join(canonical_app_dir, *parts))
    canonical_target = os.path.realpath(target_abs)

    # Ensure target is strictly within app_dir
    try:
        common = os.path.commonpath([canonical_app_dir, canonical_target])
    except ValueError as e:
        raise ValueError(f"Artifact path lies outside permitted project root: {e}")

    if common != canonical_app_dir:
        raise ValueError(f"Path escape detected: '{rel_path}' resolves outside project root '{app_dir}'")

    return target_abs


def record_incomplete_rollback(
    session_id: str,
    delivery_id: Optional[str],
    app_dir: str,
    target_path: str,
    backup_path: str,
    original_sha256: Optional[str] = None,
    candidate_sha256: Optional[str] = None,
    error_message: Optional[str] = None,
    db_path: Optional[str] = None
) -> str:
    """
    Durably records an incomplete or failed rollback artifact.
    Computes original_sha256 from backup if present and candidate_sha256 from target if present.
    Ensures safe path confinement and valid record status ('pending').
    """
    # Validate relative path
    rel_path = os.path.relpath(target_path, app_dir)
    validated_target = validate_project_artifact_path(app_dir, rel_path)

    now = time.time()
    recovery_id = f"rec_{uuid.uuid4().hex[:12]}"

    # Compute digests if not provided
    if not original_sha256 and os.path.exists(backup_path):
        try:
            with open(backup_path, "rb") as bf:
                original_sha256 = hashlib.sha256(bf.read()).hexdigest()
        except Exception:
            pass

    if not candidate_sha256 and os.path.exists(validated_target):
        try:
            with open(validated_target, "rb") as tf:
                candidate_sha256 = hashlib.sha256(tf.read()).hexdigest()
        except Exception:
            pass

    safe_error = sanitize_memory_content(error_message) if error_message else None

    with get_db_connection(db_path) as conn:
        conn.execute("""
            INSERT INTO rollback_recovery_records (
                recovery_id, session_id, delivery_id, app_dir, relative_path,
                backup_path, original_sha256, candidate_sha256, status,
                error_message, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?);
        """, (
            recovery_id, session_id, delivery_id, os.path.abspath(app_dir),
            rel_path.replace("\\", "/"), os.path.abspath(backup_path),
            original_sha256, candidate_sha256, safe_error, now, now
        ))
        conn.commit()

    return recovery_id


def preview_rollback_reconciliation(
    session_id: str,
    db_path: Optional[str] = None
) -> Dict[str, Any]:
    """
    Produces a non-destructive audit preview for incomplete rollback reconciliation.
    Inspects disk and durable metadata:
    - Original artifact relative path and canonical absolute path
    - Backup file existence and SHA-256 digest
    - Current target file existence and SHA-256 digest
    - Candidate and expected original digests
    - Comparison:
      - 'already_restored': current matches backup SHA
      - 'safe_to_restore': current matches candidate SHA or is missing
      - 'conflicted': current exists and matches neither (newer user edits!)
      - 'backup_missing': backup file deleted or unreadable
    - Proposed action and supporting evidence
    - Never mutates files or session state.
    """
    with get_db_connection(db_path) as conn:
        sess_row = conn.execute("SELECT * FROM workflow_sessions WHERE session_id = ?;", (session_id,)).fetchone()
        if not sess_row:
            return {"error": "Session not found", "session_id": session_id}
        
        records = conn.execute(
            "SELECT * FROM rollback_recovery_records WHERE session_id = ? ORDER BY created_at ASC;",
            (session_id,)
        ).fetchall()
        rec_list = [dict(r) for r in records]

    artifacts_preview = []
    has_conflicts = False
    all_safe = True

    for r in rec_list:
        rec_id = r["recovery_id"]
        r_app_dir = r["app_dir"]
        rel_path = r["relative_path"]
        bak_path = r["backup_path"]
        orig_sha = r["original_sha256"]
        cand_sha = r["candidate_sha256"]
        db_status = r["status"]

        # Validate path
        try:
            target_path = validate_project_artifact_path(r_app_dir, rel_path)
        except Exception as pe:
            artifacts_preview.append({
                "recovery_id": rec_id,
                "relative_path": rel_path,
                "status": "invalid_path",
                "error": str(pe),
                "is_safe": False,
                "proposed_action": "reject_unsafe_path"
            })
            has_conflicts = True
            all_safe = False
            continue

        backup_exists = os.path.exists(bak_path)
        backup_sha = None
        if backup_exists:
            try:
                with open(bak_path, "rb") as bf:
                    backup_sha = hashlib.sha256(bf.read()).hexdigest()
            except Exception:
                pass

        target_exists = os.path.exists(target_path)
        target_sha = None
        if target_exists:
            try:
                with open(target_path, "rb") as tf:
                    target_sha = hashlib.sha256(tf.read()).hexdigest()
            except Exception:
                pass

        # Determine content state and proposed action
        if not backup_exists or not backup_sha:
            content_state = "backup_missing"
            proposed_action = "manual_investigation"
            is_safe = False
            evidence = "Retained recovery backup is missing or unreadable on disk."
            has_conflicts = True
            all_safe = False
        elif target_exists and target_sha == backup_sha:
            content_state = "already_restored"
            proposed_action = "cleanup_backup_only"
            is_safe = True
            evidence = "Current file SHA-256 matches backup digest; original content is already restored."
        elif not target_exists or (cand_sha and target_sha == cand_sha):
            content_state = "matches_candidate" if target_exists else "target_missing"
            proposed_action = "restore_backup"
            is_safe = True
            evidence = "Current file matches uncommitted candidate deliverable or is missing; safe to restore original backup."
        else:
            # File exists, but differs from BOTH backup and candidate!
            # Newer user edit or external modification!
            content_state = "conflicted_newer_user_edit"
            proposed_action = "manual_intervention_required"
            is_safe = False
            evidence = (
                f"Current file digest ({target_sha[:12]}...) differs from both original backup "
                f"({backup_sha[:12]}...) and candidate ({cand_sha[:12] if cand_sha else 'unknown'}...). "
                f"External modifications or newer user edits detected. Automatic overwrite is forbidden."
            )
            has_conflicts = True
            all_safe = False

        artifacts_preview.append({
            "recovery_id": rec_id,
            "relative_path": rel_path,
            "target_path": target_path,
            "backup_path": bak_path,
            "db_status": db_status,
            "backup_exists": backup_exists,
            "backup_sha256": backup_sha,
            "target_exists": target_exists,
            "target_sha256": target_sha,
            "original_sha256": orig_sha,
            "candidate_sha256": cand_sha,
            "content_state": content_state,
            "proposed_action": proposed_action,
            "is_safe": is_safe,
            "evidence": evidence
        })

    return {
        "session_id": session_id,
        "delivery_id": rec_list[0].get("delivery_id") if rec_list else None,
        "total_records": len(artifacts_preview),
        "can_auto_reconcile": all_safe and len(artifacts_preview) > 0,
        "conflicts_detected": has_conflicts,
        "artifacts": artifacts_preview,
        "summary": "Reconciliation preview complete. Review proposed actions."
    }


def apply_rollback_reconciliation(
    session_id: str,
    recovery_ids: Optional[List[str]] = None,
    operator_action: str = "restore_safe",
    force: bool = False,
    failpoint: Optional[str] = None,
    db_path: Optional[str] = None
) -> Dict[str, Any]:
    """
    Executes authorized reconciliation for incomplete rollbacks under a durable state machine:
    1. Acquire claim: status -> 'in_progress' with updated_at timestamp.
       Prevents concurrent reconciliation on the same session/records.
    2. Validate safety:
       - If current file has newer user edits (differs from candidate and backup),
         and force is False: transitions status -> 'conflicted', stops, returns conflict details.
       - Re-verifies target path inside project root.
    3. Execute restoration:
       - Restores backup to target using atomic replacement.
       - Immediately computes SHA-256 of restored file and verifies match against backup SHA.
       - If mismatch: transitions to 'failed', retains backup.
       - If match: status -> 'restored'.
    4. Durable DB commit: status -> 'completed'.
    5. Cleanup: Backup file is ONLY unlinked after verified restore and DB commit!
    6. Updates session failure_analysis / permitted_recovery_actions if all recoveries complete.
    7. Idempotent: repeated runs for completed recoveries return success without error.
    """
    now = time.time()
    results = []
    
    with get_db_connection(db_path) as conn:
        sess_row = conn.execute("SELECT * FROM workflow_sessions WHERE session_id = ?;", (session_id,)).fetchone()
        if not sess_row:
            return {"success": False, "error": "Session not found", "session_id": session_id}

        # Select target recovery records
        if recovery_ids:
            placeholders = ",".join(["?"] * len(recovery_ids))
            query = f"SELECT * FROM rollback_recovery_records WHERE session_id = ? AND recovery_id IN ({placeholders});"
            records = conn.execute(query, [session_id] + list(recovery_ids)).fetchall()
        else:
            records = conn.execute(
                "SELECT * FROM rollback_recovery_records WHERE session_id = ? AND status != 'completed';",
                (session_id,)
            ).fetchall()

        rec_list = [dict(r) for r in records]
        if not rec_list:
            # Check if all records were already completed
            completed_count = conn.execute(
                "SELECT COUNT(*) as cnt FROM rollback_recovery_records WHERE session_id = ? AND status = 'completed';",
                (session_id,)
            ).fetchone()["cnt"]
            if completed_count > 0:
                return {
                    "success": True,
                    "session_id": session_id,
                    "reconciled_count": 0,
                    "already_completed": True,
                    "message": "All rollback recoveries for this session have already been completed."
                }
            return {
                "success": False,
                "error": "No recovery records found to reconcile",
                "session_id": session_id
            }

        # 1. Acquire claims atomically
        for r in rec_list:
            rec_id = r["recovery_id"]
            if r["status"] == "in_progress":
                # Check for stale lease (older than 60s)
                if now - r["updated_at"] < 60.0:
                    return {
                        "success": False,
                        "error": f"Concurrent reconciliation in progress for recovery record {rec_id}",
                        "recovery_id": rec_id
                    }
            
            conn.execute("""
                UPDATE rollback_recovery_records
                SET status = 'in_progress', updated_at = ?
                WHERE recovery_id = ?;
            """, (now, rec_id))
        conn.commit()

    if failpoint == "after_claim_before_intent":
        raise IOError("Crash simulated after claim acquired before intent persisted")

    # Process each record
    all_succeeded = True
    for r in rec_list:
        rec_id = r["recovery_id"]
        app_dir = r["app_dir"]
        rel_path = r["relative_path"]
        bak_path = r["backup_path"]
        cand_sha = r["candidate_sha256"]

        try:
            target_path = validate_project_artifact_path(app_dir, rel_path)
        except Exception as pe:
            with get_db_connection(db_path) as conn:
                conn.execute(
                    "UPDATE rollback_recovery_records SET status = 'failed', error_message = ?, updated_at = ? WHERE recovery_id = ?;",
                    (str(pe), time.time(), rec_id)
                )
                conn.commit()
            results.append({"recovery_id": rec_id, "status": "failed", "error": str(pe)})
            all_succeeded = False
            continue

        if not os.path.exists(bak_path):
            err_msg = f"Recovery backup file missing on disk: {bak_path}"
            with get_db_connection(db_path) as conn:
                conn.execute(
                    "UPDATE rollback_recovery_records SET status = 'failed', error_message = ?, updated_at = ? WHERE recovery_id = ?;",
                    (err_msg, time.time(), rec_id)
                )
                conn.commit()
            results.append({"recovery_id": rec_id, "status": "failed", "error": err_msg})
            all_succeeded = False
            continue

        with open(bak_path, "rb") as bf:
            backup_bytes = bf.read()
        backup_sha = hashlib.sha256(backup_bytes).hexdigest()

        target_exists = os.path.exists(target_path)
        target_sha = None
        if target_exists:
            with open(target_path, "rb") as tf:
                target_sha = hashlib.sha256(tf.read()).hexdigest()

        # Check for conflict: current file differs from both original backup and candidate!
        if target_exists and target_sha != backup_sha and target_sha != cand_sha:
            if not force:
                conflict_msg = (
                    f"Conflict detected: target file '{rel_path}' has newer user edits "
                    f"(SHA {target_sha[:12]}... matches neither backup nor candidate). "
                    f"Automatic overwrite refused."
                )
                with get_db_connection(db_path) as conn:
                    conn.execute(
                        "UPDATE rollback_recovery_records SET status = 'conflicted', error_message = ?, updated_at = ? WHERE recovery_id = ?;",
                        (conflict_msg, time.time(), rec_id)
                    )
                    conn.commit()
                results.append({"recovery_id": rec_id, "status": "conflicted", "error": conflict_msg})
                all_succeeded = False
                continue

        if failpoint == "before_filesystem_restore":
            raise IOError("Crash simulated before filesystem restore")

        # Restoration step:
        if not target_exists or target_sha != backup_sha:
            try:
                temp_restore = target_path + f".restoring_{uuid.uuid4().hex[:6]}"
                with open(temp_restore, "wb") as rf:
                    rf.write(backup_bytes)
                    rf.flush()
                    os.fsync(rf.fileno())
                os.replace(temp_restore, target_path)
            except Exception as restore_err:
                with get_db_connection(db_path) as conn:
                    conn.execute(
                        "UPDATE rollback_recovery_records SET status = 'failed', error_message = ?, updated_at = ? WHERE recovery_id = ?;",
                        (f"Filesystem restoration failed: {restore_err}", time.time(), rec_id)
                    )
                    conn.commit()
                results.append({"recovery_id": rec_id, "status": "failed", "error": str(restore_err)})
                all_succeeded = False
                continue

        if failpoint == "after_restore_before_verification":
            raise IOError("Crash simulated after restore before verification")

        # Verification step:
        with open(target_path, "rb") as tf:
            restored_sha = hashlib.sha256(tf.read()).hexdigest()

        if restored_sha != backup_sha:
            err_msg = f"Post-restore verification failed: restored SHA ({restored_sha[:12]}...) does not match backup ({backup_sha[:12]}...)"
            with get_db_connection(db_path) as conn:
                conn.execute(
                    "UPDATE rollback_recovery_records SET status = 'failed', error_message = ?, updated_at = ? WHERE recovery_id = ?;",
                    (err_msg, time.time(), rec_id)
                )
                conn.commit()
            results.append({"recovery_id": rec_id, "status": "failed", "error": err_msg})
            all_succeeded = False
            continue

        with get_db_connection(db_path) as conn:
            conn.execute(
                "UPDATE rollback_recovery_records SET status = 'restored', updated_at = ? WHERE recovery_id = ?;",
                (time.time(), rec_id)
            )
            conn.commit()

        if failpoint == "after_verification_before_sqlite_commit":
            raise IOError("Crash simulated after verification before SQLite commit")

        # SQLite outcome persisted: status = 'completed'
        with get_db_connection(db_path) as conn:
            conn.execute(
                "UPDATE rollback_recovery_records SET status = 'completed', error_message = NULL, updated_at = ? WHERE recovery_id = ?;",
                (time.time(), rec_id)
            )
            conn.commit()

        if failpoint == "after_sqlite_commit_before_cleanup":
            raise IOError("Crash simulated after SQLite commit before backup cleanup")

        # Backup cleanup: ONLY after verified restore and DB commit!
        try:
            if os.path.exists(bak_path):
                os.remove(bak_path)
        except Exception as clean_err:
            logging.warning("Could not unlink backup %s after completed recovery: %s", bak_path, clean_err)

        results.append({
            "recovery_id": rec_id,
            "relative_path": rel_path,
            "status": "completed",
            "restored_sha256": restored_sha
        })

    # If all records completed, check if session rollback_failed error can be cleared
    with get_db_connection(db_path) as conn:
        pending_recs = conn.execute(
            "SELECT COUNT(*) as cnt FROM rollback_recovery_records WHERE session_id = ? AND status != 'completed';",
            (session_id,)
        ).fetchone()["cnt"]
        if pending_recs == 0:
            sess_err = conn.execute("SELECT error_message FROM workflow_sessions WHERE session_id = ?;", (session_id,)).fetchone()
            if sess_err and sess_err["error_message"] and "rollback error" in sess_err["error_message"].lower():
                cleared_msg = sess_err["error_message"] + " [Rollback successfully reconciled by operator]"
                conn.execute(
                    "UPDATE workflow_sessions SET error_message = ?, updated_at = ? WHERE session_id = ?;",
                    (cleared_msg, time.time(), session_id)
                )
                conn.commit()

    return {
        "success": all_succeeded,
        "session_id": session_id,
        "results": results,
        "reconciled_count": sum(1 for r in results if r["status"] == "completed"),
        "conflicts_count": sum(1 for r in results if r["status"] == "conflicted"),
        "failed_count": sum(1 for r in results if r["status"] == "failed")
    }


# =========================================================================
# Phase 2.6: Production Readiness Helpers & Dual-Ledger Outbox
# =========================================================================

RESTORING_FILE_PATTERN = re.compile(r'^(?:.+)?\.restoring_[a-f0-9]{6,}$')

def cleanup_abandoned_restore_staging(app_dir: str, max_age_seconds: float = 60.0) -> int:
    """
    Phase 2.6-B1: Safely cleans up orphaned temporary staging files (.restoring_*)
    left by crashes during file restoration.
    Constraints:
    - Never deletes files outside verified app_dir.
    - Never follows symlinks.
    - Never removes active staging files (st_mtime younger than max_age_seconds).
    - Never removes user files or files not matching the exact staging pattern.
    - Safe to run repeatedly.
    Returns integer count of unlinked abandoned files.
    """
    if not app_dir or not os.path.isdir(app_dir):
        return 0

    canonical_root = os.path.realpath(app_dir)
    cleaned_count = 0
    now = time.time()

    try:
        with os.scandir(canonical_root) as it:
            for entry in it:
                try:
                    # Never follow symlinks
                    if entry.is_symlink():
                        continue
                    if not entry.is_file(follow_symlinks=False):
                        continue
                    if not RESTORING_FILE_PATTERN.match(entry.name):
                        continue

                    # Verify canonical containment
                    entry_real = os.path.realpath(entry.path)
                    if os.path.commonpath([canonical_root, entry_real]) != canonical_root:
                        continue

                    # Check age: only remove if older than max_age_seconds
                    st = entry.stat(follow_symlinks=False)
                    if (now - st.st_mtime) >= max_age_seconds:
                        try:
                            os.remove(entry.path)
                            cleaned_count += 1
                            logging.info("Cleaned abandoned staging file: %s", entry.name)
                        except OSError as oe:
                            logging.warning("Failed to remove staging file %s: %s", entry.name, oe)
                except (OSError, ValueError):
                    continue
    except Exception as scan_err:
        logging.warning("Error scanning app_dir for staging cleanup: %s", scan_err)

    return cleaned_count


try:
    import msvcrt
    HAS_MSVCRT = True
except ImportError:
    HAS_MSVCRT = False

try:
    import fcntl
    HAS_FCNTL = True
except ImportError:
    HAS_FCNTL = False

_BUDGET_THREAD_LOCK = threading.RLock()
_BUDGET_LOCK_LOCAL = threading.local()

@contextmanager
def _budget_file_lock(budget_path: str, timeout: float = 15.0):
    """
    Phase 2.7.3: Hardened cross-process and cross-thread file lock on budget_path.lock.
    Uses OS-level kernel file locking (msvcrt on Windows, fcntl on POSIX) on the open file descriptor.
    Does NOT remove locks based on fixed age if the owner process is still actively running.
    Automatic OS kernel cleanup on process termination without TOCTOU deletion race.
    Thread-safe and re-entrant within the same thread.
    Writes owner PID and timestamp into lock file for inspection.
    """
    lock_path = f"{budget_path}.lock"

    if not hasattr(_BUDGET_LOCK_LOCAL, "held_locks"):
        _BUDGET_LOCK_LOCAL.held_locks = {}

    held = _BUDGET_LOCK_LOCAL.held_locks
    if lock_path in held and held[lock_path] > 0:
        held[lock_path] += 1
        try:
            yield
        finally:
            held[lock_path] -= 1
        return

    _BUDGET_THREAD_LOCK.acquire()
    acquired = False
    fd = None
    start_time = time.time()
    try:
        while time.time() - start_time < timeout:
            try:
                fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o666)
            except OSError:
                time.sleep(0.02)
                continue

            try:
                if HAS_MSVCRT:
                    os.lseek(fd, 1024, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                elif HAS_FCNTL:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except (OSError, BlockingIOError, PermissionError):
                try:
                    os.close(fd)
                except OSError:
                    pass
                fd = None
                time.sleep(0.02)

        if not acquired:
            owner_pid = "unknown"
            try:
                if os.path.exists(lock_path):
                    with open(lock_path, "r", encoding="utf-8") as lf:
                        lock_text = lf.read().strip()
                        if ":" in lock_text:
                            owner_pid = lock_text.split(":", 1)[0].strip()
            except Exception:
                pass
            raise TimeoutError(f"Could not acquire budget file lock on {lock_path} within {timeout}s: held by process PID {owner_pid}")

        # Once lock acquired, write diagnostic metadata at start of file
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            os.ftruncate(fd, 0)
            payload = f"{os.getpid()}:{time.time()}\n".encode("utf-8")
            os.write(fd, payload)
            os.fsync(fd)
        except Exception:
            pass

        held[lock_path] = 1
        try:
            yield
        finally:
            held[lock_path] = 0
            if acquired and fd is not None:
                try:
                    os.lseek(fd, 0, os.SEEK_SET)
                    os.ftruncate(fd, 0)
                    os.fsync(fd)
                except Exception:
                    pass
                if HAS_MSVCRT:
                    try:
                        os.lseek(fd, 1024, os.SEEK_SET)
                        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                    except Exception:
                        pass
                elif HAS_FCNTL:
                    try:
                        fcntl.flock(fd, fcntl.LOCK_UN)
                    except Exception:
                        pass
                try:
                    os.close(fd)
                except OSError:
                    pass
                try:
                    os.remove(lock_path)
                except OSError:
                    pass
    finally:
        _BUDGET_THREAD_LOCK.release()


def _atomic_write_json_direct(filepath: str, data: Any):
    """Atomically write JSON data with fsync durability before replace and safe cleanup."""
    # Backup existing valid file before replacement
    if os.path.exists(filepath):
        try:
            bak_path = f"{filepath}.bak"
            with open(filepath, "r", encoding="utf-8") as sf:
                content = sf.read()
            with open(bak_path, "w", encoding="utf-8") as df:
                df.write(content)
                df.flush()
                os.fsync(df.fileno())
        except Exception:
            pass

    tmp_path = f"{filepath}.tmp_{uuid.uuid4().hex[:8]}"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, filepath)
    finally:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass


def project_reconciliation_to_budget(
    budget_path: str,
    journal_id: str,
    run_id: str,
    delta_tokens: int,
    delta_cost: float,
    source: str = "provider_confirmed",
    department: str = "إدارة الميزانية والتدقيق",
    task_desc: Optional[str] = None
) -> Dict[str, Any]:
    """
    Phase 2.7: Projects reconciliation delta into budget ledger idempotently and safely.
    - Uses cross-process/cross-thread file lock (_budget_file_lock) during read-modify-write.
    - Uses permanent 'applied_reconciliations' registry in budget.json that is NEVER truncated.
    - Checks legacy rolling transactions for backwards compatibility.
    - Handles corrupted or partially written JSON budget files safely.
    - Uses atomic replacement with fsync.
    """
    task_label = task_desc or f"تسوية استهلاك {run_id}"

    with _budget_file_lock(budget_path):
        budget = None
        if os.path.exists(budget_path):
            try:
                with open(budget_path, "r", encoding="utf-8") as f:
                    content = f.read().strip()
                    if content:
                        budget = json.loads(content)
            except Exception:
                # Corrupted or unparseable JSON: check for .bak file
                bak_path = f"{budget_path}.bak"
                if os.path.exists(bak_path):
                    try:
                        with open(bak_path, "r", encoding="utf-8") as bf:
                            b_content = bf.read().strip()
                            if b_content:
                                budget = json.loads(b_content)
                    except Exception:
                        budget = None

        if not isinstance(budget, dict):
            budget = {
                "tokens_used": 0,
                "estimated_cost": 0.0,
                "budget_limit": 50.0,
                "transactions": [],
                "applied_reconciliations": {}
            }

        applied_registry = budget.setdefault("applied_reconciliations", {})

        # 1. Truncation-proof permanent registry check
        if journal_id in applied_registry:
            return {"projected": True, "already_applied": True, "budget": budget}

        # 2. Check legacy transactions list for backwards compatibility
        txs = budget.get("transactions", [])
        for tx in txs:
            if tx.get("ref_journal_id") == journal_id:
                # Backfill into truncation-proof registry
                applied_registry[journal_id] = {
                    "applied_at": time.time(),
                    "run_id": run_id,
                    "delta_tokens": delta_tokens,
                    "delta_cost": delta_cost,
                    "source": source
                }
                _atomic_write_json_direct(budget_path, budget)
                return {"projected": True, "already_applied": True, "budget": budget}

        curr_tokens = budget.get("tokens_used", 0)
        curr_cost = budget.get("estimated_cost", 0.0)

        new_tokens = max(0, curr_tokens + delta_tokens)
        new_cost = max(0.0, round(curr_cost + delta_cost, 4))

        budget["tokens_used"] = new_tokens
        budget["estimated_cost"] = new_cost

        new_tx = {
            "id": int(time.time() * 1000) + len(txs),
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "agent": "Financial Reconciler",
            "department": department,
            "task": task_label,
            "tokens": delta_tokens,
            "cost": round(delta_cost, 4),
            "source": source,
            "ref_journal_id": journal_id,
            "ref_run_id": run_id
        }
        txs.insert(0, new_tx)
        budget["transactions"] = txs[:60]
        applied_registry[journal_id] = {
            "applied_at": time.time(),
            "run_id": run_id,
            "delta_tokens": delta_tokens,
            "delta_cost": delta_cost,
            "source": source
        }

        _atomic_write_json_direct(budget_path, budget)
        return {"projected": True, "already_applied": False, "budget": budget}


_RUN_OPERATION_LOCK = threading.RLock()

def apply_manual_run_adjustment(
    run_id: str,
    adjusted_tokens: int,
    cost: Optional[float] = None,
    reason: str = "",
    evidence_reference: str = "",
    principal: str = "admin",
    idempotency_key: Optional[str] = None,
    is_incremental: bool = False,
    failpoint: Optional[str] = None,
    db_path: Optional[str] = None,
    budget_path: Optional[str] = None
) -> Dict[str, Any]:
    """
    Phase 2.7.3: Applies a distinct, operator-audited manual token and cost adjustment to an agent run.
    - Requires non-empty reason and evidence reference.
    - Strict numeric financial validation for tokens and cost values.
    - Atomic idempotency claim protocol with lease fencing, durable recovery, and rejection of conflicting payloads.
    - Supports absolute or incremental adjustments with correct mathematical deltas.
    - Atomically updates cost_booked AND cost_source = 'manual_operator_adjustment'.
    - Optimistic concurrency control and run-level serialization.
    - Projects delta into budget.json idempotently via truncation-proof registry under cross-process OS lock.
    """
    adjusted_tokens = validate_token_count(adjusted_tokens, field_name="adjusted_tokens", allow_negative=is_incremental)
    if cost is not None:
        cost = validate_financial_numeric(cost, field_name="cost", allow_none=True, allow_negative=is_incremental)
    if not reason or not reason.strip():
        raise ValueError("Manual adjustment requires a non-empty reason.")
    if not evidence_reference or not evidence_reference.strip():
        raise ValueError("Manual adjustment requires a non-empty evidence_reference.")

    eff_budget_path = budget_path or os.environ.get("BUDGET_FILE_PATH") or os.environ.get("BUDGET_FILE") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "budget.json")

    request_payload = {
        "run_id": run_id,
        "adjusted_tokens": adjusted_tokens,
        "cost": cost,
        "reason": reason.strip(),
        "evidence_reference": evidence_reference.strip(),
        "is_incremental": is_incremental
    }
    req_hash = hashlib.sha256(json.dumps(request_payload, sort_keys=True).encode("utf-8")).hexdigest()

    owner_token = uuid.uuid4().hex
    lease_duration = 30.0

    with _RUN_OPERATION_LOCK:
        with get_db_connection(db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("PRAGMA table_info(agent_runs);")
            cols = {row[1] for row in cursor.fetchall()}

            j_table = cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='reconciliation_journal';").fetchone()
            idemp_table = cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='idempotency_keys';").fetchone()

            if idempotency_key and idemp_table:
                now_ts = time.time()
                lease_expires = now_ts + lease_duration
                while True:
                    row_idemp = cursor.execute("SELECT * FROM idempotency_keys WHERE key = ?;", (idempotency_key,)).fetchone()
                    if row_idemp:
                        if row_idemp["request_hash"] != req_hash:
                            raise IdempotencyConflictError(f"Idempotency key '{idempotency_key}' was previously used with different request parameters.")
                        if row_idemp["status"] == "completed":
                            try:
                                cached_resp = json.loads(row_idemp["response_json"])
                                cached_resp["resumed_from_idempotency"] = True
                                return cached_resp
                            except Exception:
                                pass
                        elif row_idemp["status"] == "committed_pending_projection":
                            # Commit already happened, resume budget projection without re-mutating DB
                            journal_id = row_idemp["journal_id"]
                            proj_budget = {}
                            if journal_id and j_table:
                                j_row = cursor.execute("SELECT * FROM reconciliation_journal WHERE journal_id = ?;", (journal_id,)).fetchone()
                                if j_row:
                                    proj = project_reconciliation_to_budget(
                                        budget_path=eff_budget_path,
                                        journal_id=journal_id,
                                        run_id=run_id,
                                        delta_tokens=j_row["delta_tokens"],
                                        delta_cost=j_row["delta_cost"],
                                        source="manual_operator_adjustment",
                                        department="إدارة التدقيق المالي اليدوي",
                                        task_desc=f"تعديل إداري لاستهلاك {run_id}: {reason[:40]}"
                                    )
                                    proj_budget = proj.get("budget", {})
                                    cursor.execute(
                                        "UPDATE reconciliation_journal SET status = 'completed', completed_at = ? WHERE journal_id = ?;",
                                        (time.time(), journal_id)
                                    )
                            cached_resp = {}
                            if row_idemp["response_json"]:
                                try:
                                    cached_resp = json.loads(row_idemp["response_json"])
                                except Exception:
                                    pass
                            if not cached_resp:
                                run_row = cursor.execute("SELECT * FROM agent_runs WHERE run_id = ?;", (run_id,)).fetchone()
                                cached_resp = {
                                    "success": True,
                                    "run_id": run_id,
                                    "adjusted_tokens": run_row["tokens_used"] if run_row else adjusted_tokens,
                                    "cost": run_row["cost_booked"] if run_row else cost,
                                    "token_source": "manual_operator_adjustment",
                                    "cost_source": "manual_operator_adjustment",
                                    "reconciliation_status": "completed",
                                    "reason": reason,
                                    "evidence_reference": evidence_reference,
                                    "journal_id": journal_id,
                                    "budget": proj_budget,
                                    "message": "Manual run adjustment successfully resumed from committed state."
                                }
                            cached_resp["resumed_from_idempotency"] = True
                            cursor.execute(
                                "UPDATE idempotency_keys SET status = 'completed', response_json = ?, updated_at = ?, completed_at = ? WHERE key = ?;",
                                (json.dumps(cached_resp), time.time(), time.time(), idempotency_key)
                            )
                            conn.commit()
                            return cached_resp
                        elif row_idemp["status"] == "in_progress":
                            if now_ts < row_idemp["lease_expires_at"]:
                                raise IdempotencyInProgressError(f"Request with idempotency key '{idempotency_key}' is currently in progress.")
                            # Lease expired: check if previous worker actually committed to reconciliation_journal
                            if row_idemp["journal_id"] and j_table:
                                j_row = cursor.execute("SELECT * FROM reconciliation_journal WHERE journal_id = ?;", (row_idemp["journal_id"],)).fetchone()
                                if j_row and j_row["status"] in ("applied_db", "completed"):
                                    cursor.execute("UPDATE idempotency_keys SET status = 'committed_pending_projection', updated_at = ? WHERE key = ?;", (time.time(), idempotency_key))
                                    conn.commit()
                                    continue
                            # Previous worker crashed before commit. Claim lease atomically:
                            claim_cur = cursor.execute("""
                                UPDATE idempotency_keys
                                SET owner_token = ?, lease_expires_at = ?, updated_at = ?
                                WHERE key = ? AND status = 'in_progress' AND lease_expires_at <= ?;
                            """, (owner_token, lease_expires, now_ts, idempotency_key, now_ts))
                            if claim_cur.rowcount == 0:
                                raise IdempotencyInProgressError(f"Request with idempotency key '{idempotency_key}' is currently in progress.")
                            conn.commit()
                            break
                        elif row_idemp["status"] in ("failed_retryable",):
                            claim_cur = cursor.execute("""
                                UPDATE idempotency_keys
                                SET owner_token = ?, lease_expires_at = ?, status = 'in_progress', updated_at = ?
                                WHERE key = ? AND status = 'failed_retryable';
                            """, (owner_token, lease_expires, now_ts, idempotency_key))
                            if claim_cur.rowcount == 0:
                                raise IdempotencyInProgressError(f"Request with idempotency key '{idempotency_key}' is currently in progress.")
                            conn.commit()
                            break
                        else:
                            raise IdempotencyConflictError(f"Idempotency key '{idempotency_key}' is in terminal failure state.")
                    else:
                        try:
                            cursor.execute("""
                                INSERT INTO idempotency_keys (key, scope, target_id, request_hash, response_json, status, owner_token, lease_expires_at, created_at, updated_at)
                                VALUES (?, 'manual_adjustment', ?, ?, '', 'in_progress', ?, ?, ?, ?);
                            """, (idempotency_key, run_id, req_hash, owner_token, lease_expires, now_ts, now_ts))
                            conn.commit()
                            break
                        except sqlite3.IntegrityError:
                            continue

            row = cursor.execute("SELECT * FROM agent_runs WHERE run_id = ?;", (run_id,)).fetchone()
            if not row:
                raise LookupError(f"Run {run_id} not found.")

            run_data = dict(row)
            prior_tokens = int(run_data.get("tokens_used", 0) or 0)
            prior_cost = float(run_data.get("cost_booked") if "cost_booked" in run_data and run_data["cost_booked"] is not None else round_cost((prior_tokens / 1_000_000.0) * 0.15, 6))

            if is_incremental:
                final_tokens = max(0, prior_tokens + adjusted_tokens)
                delta_tokens = adjusted_tokens
                if cost is not None:
                    delta_cost = round_cost(cost, 6)
                    final_cost = max(0.0, round_cost(prior_cost + delta_cost, 6))
                else:
                    delta_cost = round_cost((delta_tokens / 1_000_000.0) * 0.15, 6)
                    final_cost = max(0.0, round_cost(prior_cost + delta_cost, 6))
            else:
                final_tokens = max(0, adjusted_tokens)
                delta_tokens = final_tokens - prior_tokens
                if cost is not None:
                    final_cost = max(0.0, round_cost(cost, 6))
                    delta_cost = round_cost(final_cost - prior_cost, 6)
                else:
                    final_cost = max(0.0, round_cost((final_tokens / 1_000_000.0) * 0.15, 6))
                    delta_cost = round_cost(final_cost - prior_cost, 6)

            final_tokens = validate_token_count(final_tokens, field_name="final_tokens", allow_negative=False)
            delta_tokens = validate_token_count(delta_tokens, field_name="delta_tokens", allow_negative=True)
            final_cost = validate_financial_numeric(final_cost, field_name="final_cost", allow_negative=False)
            delta_cost = validate_financial_numeric(delta_cost, field_name="delta_cost", allow_negative=True)

            if failpoint == "before_db_commit":
                raise IOError("Crash simulated before SQLite commit")

            journal_id = f"adj_{run_id}_{final_tokens}_{int(time.time() * 1000)}_{uuid.uuid4().hex[:6]}"
            audit_id = f"aud_{uuid.uuid4().hex[:12]}"

            update_fields = [
                "tokens_used = ?",
                "token_source = 'manual_operator_adjustment'",
                "reconciliation_status = 'completed'"
            ]
            params = [final_tokens]
            if "cost_booked" in cols:
                update_fields.append("cost_booked = ?")
                params.append(final_cost)
            if "cost_source" in cols:
                update_fields.append("cost_source = ?")
                params.append("manual_operator_adjustment")

            params.append(run_id)
            params.append(prior_tokens)
            cur_up = conn.execute(f"UPDATE agent_runs SET {', '.join(update_fields)} WHERE run_id = ? AND tokens_used = ?;", params)
            if cur_up.rowcount == 0:
                raise ConcurrencyConflictError(f"Concurrent conflicting update detected on run {run_id}.")

            if j_table:
                conn.execute("""
                    INSERT INTO reconciliation_journal (
                        journal_id, run_id, target_tokens, delta_tokens, delta_cost,
                        status, provider_request_id, created_at, target_cost
                    ) VALUES (?, ?, ?, ?, ?, 'applied_db', ?, ?, ?);
                """, (journal_id, run_id, final_tokens, delta_tokens, delta_cost, f"manual:{evidence_reference[:50]}", time.time(), final_cost))

            if idempotency_key and idemp_table:
                fence_cur = conn.execute("""
                    UPDATE idempotency_keys
                    SET status = 'committed_pending_projection', journal_id = ?, updated_at = ?
                    WHERE key = ? AND owner_token = ?;
                """, (journal_id, time.time(), idempotency_key, owner_token))
                if fence_cur.rowcount == 0:
                    raise ConcurrencyConflictError("Idempotency lease ownership superseded; aborting commit.")

            conn.execute("""
                INSERT INTO recovery_audit_log (
                    audit_id, timestamp, principal, action, target_id,
                    force_requested, authorized, outcome, error_message
                ) VALUES (?, ?, ?, 'manual_adjustment', ?, 1, 1, 'applied_db', ?);
            """, (audit_id, time.time(), principal or "admin", run_id, f"Adjusted to {final_tokens} tokens (delta: {delta_tokens}, cost: {final_cost}). Reason: {reason}. Ref: {evidence_reference}"))

            conn.commit()

            if failpoint == "after_db_before_budget":
                raise IOError("Crash simulated after DB commit before budget update")

            # Project to budget
            proj = project_reconciliation_to_budget(
                budget_path=eff_budget_path,
                journal_id=journal_id,
                run_id=run_id,
                delta_tokens=delta_tokens,
                delta_cost=delta_cost,
                source="manual_operator_adjustment",
                department="إدارة التدقيق المالي اليدوي",
                task_desc=f"تعديل إداري لاستهلاك {run_id}: {reason[:40]}"
            )

            if failpoint == "after_budget_before_journal_complete":
                raise IOError("Crash simulated after budget projection before journal completion")

            if j_table:
                conn.execute(
                    "UPDATE reconciliation_journal SET status = 'completed', completed_at = ? WHERE journal_id = ?;",
                    (time.time(), journal_id)
                )

            conn.execute(
                "UPDATE recovery_audit_log SET outcome = 'success' WHERE audit_id = ?;",
                (audit_id,)
            )

            resp_data = {
                "success": True,
                "run_id": run_id,
                "prior_tokens": prior_tokens,
                "prior_cost": prior_cost,
                "adjusted_tokens": final_tokens,
                "delta_tokens": delta_tokens,
                "delta_cost": delta_cost,
                "cost": final_cost,
                "token_source": "manual_operator_adjustment",
                "cost_source": "manual_operator_adjustment",
                "reconciliation_status": "completed",
                "reason": reason,
                "evidence_reference": evidence_reference,
                "journal_id": journal_id,
                "budget": proj.get("budget", {}),
                "message": "Manual run adjustment successfully applied and projected."
            }

            if idempotency_key and idemp_table:
                conn.execute(
                    "UPDATE idempotency_keys SET status = 'completed', response_json = ?, updated_at = ?, completed_at = ? WHERE key = ?;",
                    (json.dumps(resp_data), time.time(), time.time(), idempotency_key)
                )

            conn.commit()

        # Durably record audit event via function outside connection context (fails closed if audit fails)
        record_recovery_audit_event(
            principal=principal or "admin",
            action="manual_adjustment",
            target_id=run_id,
            force_requested=True,
            authorized=True,
            outcome="success",
            error_message=f"Adjusted to {final_tokens} tokens (delta: {delta_tokens}, cost: {final_cost}). Reason: {reason}. Ref: {evidence_reference}",
            db_path=db_path
        )

        return resp_data


def record_recovery_audit_event(
    principal: str,
    action: str,
    target_id: str,
    force_requested: bool,
    authorized: bool,
    outcome: str,
    error_message: Optional[str] = None,
    db_path: Optional[str] = None
) -> str:
    """
    Phase 2.6-C: Durably records an audit event for recovery or administrative operations in SQLite.
    Captures operator identity, action, target, authorization decision, outcome, and error.
    Never logs secrets, API keys, or raw authentication tokens.
    """
    audit_id = f"aud_{uuid.uuid4().hex[:12]}"
    now = time.time()
    safe_error = sanitize_memory_content(error_message[:300]) if error_message else None
    with get_db_connection(db_path) as conn:
        conn.execute("""
            INSERT INTO recovery_audit_log (
                audit_id, timestamp, principal, action, target_id,
                force_requested, authorized, outcome, error_message
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);
        """, (
            audit_id, now, principal or "anonymous", action, target_id,
            1 if force_requested else 0,
            1 if authorized else 0,
            outcome, safe_error
        ))
        conn.commit()
    return audit_id


_RUN_RECON_THREAD_LOCK = threading.Lock()

def _with_recon_lock(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        with _RUN_RECON_THREAD_LOCK:
            return fn(*args, **kwargs)
    return wrapper

@_with_recon_lock
def reconcile_provider_run_usage(
    run_id: str,
    provider_lookup_fn: Optional[Any] = None,
    db_path: Optional[str] = None,
    budget_path: Optional[str] = None,
    failpoint: Optional[str] = None
) -> Dict[str, Any]:
    """
    Phase 2.7: Reconciles usage and billing for an agent run using authoritative provider lookup data
    and synchronizes the financial ledger via transactional outbox journal.
    Guarantees:
    - Never accepts client-supplied token/cost claims.
    - Never auto-replays generative model prompts.
    - Idempotent: repeated calls do not double-count or alter already-confirmed runs.
    - Preserves explicit provider-reported 0 tokens.
    - Rejects negative or malformed usage.
    - Reconciles tokens_used, token_source ('provider_confirmed'), reconciliation_status ('completed'/'unresolved'/'failed').
    - Updates SQLite agent_runs and outbox journal under a durable transaction boundary.
    - Projects delta into budget.json idempotently; resumes incomplete outbox entries upon retry or restart.
    """
    eff_budget_path = budget_path or os.environ.get("BUDGET_FILE_PATH") or os.environ.get("BUDGET_FILE") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "budget.json")

    with get_db_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("PRAGMA table_info(agent_runs);")
        cols = {row[1] for row in cursor.fetchall()}

        j_table = cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='reconciliation_journal';").fetchone()

        row = cursor.execute("SELECT * FROM agent_runs WHERE run_id = ?;", (run_id,)).fetchone()
        if not row:
            return {"success": False, "error": "Run not found", "run_id": run_id}

        run_data = dict(row)
        rec_status = run_data.get("reconciliation_status") or "none"

        # Check for existing in-flight or completed outbox journal entry
        journal_row = None
        if j_table:
            journal_row = cursor.execute(
                "SELECT * FROM reconciliation_journal WHERE run_id = ? ORDER BY created_at DESC LIMIT 1;",
                (run_id,)
            ).fetchone()

        # If outbox entry was committed to DB but budget projection was pending when process crashed:
        if journal_row and journal_row["status"] == "applied_db":
            proj = project_reconciliation_to_budget(
                budget_path=eff_budget_path,
                journal_id=journal_row["journal_id"],
                run_id=run_id,
                delta_tokens=journal_row["delta_tokens"],
                delta_cost=journal_row["delta_cost"]
            )
            if failpoint == "after_budget_before_journal_complete":
                raise IOError("Crash simulated after budget projection before journal completion")

            conn.execute(
                "UPDATE reconciliation_journal SET status = 'completed', completed_at = ? WHERE journal_id = ?;",
                (time.time(), journal_row["journal_id"])
            )
            conn.commit()
            run_row = conn.execute("SELECT cost_booked, cost_source FROM agent_runs WHERE run_id = ?;", (run_id,)).fetchone()
            return {
                "success": True,
                "already_reconciled": False,
                "resumed_from_outbox": True,
                "run_id": run_id,
                "provider_request_id": journal_row["provider_request_id"],
                "tokens_used": journal_row["target_tokens"],
                "token_source": "provider_confirmed",
                "cost_source": run_row["cost_source"] if run_row else "provider_confirmed",
                "reconciliation_status": "completed",
                "cost": run_row["cost_booked"] if run_row else proj.get("budget", {}).get("estimated_cost"),
                "message": "Authoritative provider usage successfully reconciled from persistent outbox."
            }

        # Idempotency check: if already completed and outbox is drained
        if rec_status == "completed" and (not journal_row or journal_row["status"] == "completed"):
            return {
                "success": True,
                "already_reconciled": True,
                "run_id": run_id,
                "tokens_used": run_data.get("tokens_used", 0),
                "token_source": run_data.get("token_source", "provider_confirmed"),
                "cost_source": run_data.get("cost_source", "provider_confirmed"),
                "reconciliation_status": "completed",
                "cost": run_data.get("cost_booked"),
                "message": "Run usage was previously reconciled with authoritative provider data."
            }

        provider_req_id = run_data.get("provider_request_id")

        if provider_lookup_fn is None:
            if "reconciliation_status" in cols:
                conn.execute(
                    "UPDATE agent_runs SET reconciliation_status = 'unresolved' WHERE run_id = ?;",
                    (run_id,)
                )
                conn.commit()
            return {
                "success": False,
                "run_id": run_id,
                "reconciliation_status": "unresolved",
                "error": "Authoritative provider lookup function not provided. Replay of generative prompts is forbidden."
            }

        # Perform authoritative lookup
        try:
            lookup_result = provider_lookup_fn(run_id, provider_req_id)
        except Exception as lookup_err:
            if "reconciliation_status" in cols:
                conn.execute(
                    "UPDATE agent_runs SET reconciliation_status = 'unresolved' WHERE run_id = ?;",
                    (run_id,)
                )
                conn.commit()
            return {
                "success": False,
                "run_id": run_id,
                "reconciliation_status": "unresolved",
                "error": f"Authoritative provider usage lookup failed: {lookup_err}"
            }

        if not isinstance(lookup_result, dict):
            if "reconciliation_status" in cols:
                conn.execute(
                    "UPDATE agent_runs SET reconciliation_status = 'failed' WHERE run_id = ?;",
                    (run_id,)
                )
                conn.commit()
            return {
                "success": False,
                "run_id": run_id,
                "reconciliation_status": "failed",
                "error": "Malformed provider response: expected dictionary."
            }

        if not lookup_result.get("found", True):
            if "reconciliation_status" in cols:
                conn.execute(
                    "UPDATE agent_runs SET reconciliation_status = 'unresolved' WHERE run_id = ?;",
                    (run_id,)
                )
                conn.commit()
            return {
                "success": False,
                "run_id": run_id,
                "reconciliation_status": "unresolved",
                "message": lookup_result.get("message", "Provider usage data unavailable or inconclusive.")
            }

        confirmed_tokens_raw = lookup_result.get("tokens")
        if confirmed_tokens_raw is None:
            confirmed_tokens_raw = lookup_result.get("confirmed_tokens")

        if confirmed_tokens_raw is None:
            if "reconciliation_status" in cols:
                conn.execute(
                    "UPDATE agent_runs SET reconciliation_status = 'unresolved' WHERE run_id = ?;",
                    (run_id,)
                )
                conn.commit()
            return {
                "success": False,
                "run_id": run_id,
                "reconciliation_status": "unresolved",
                "error": "Provider response did not contain verified token usage."
            }

        try:
            confirmed_tokens = validate_token_count(confirmed_tokens_raw, field_name="confirmed_tokens", allow_negative=False)
        except (ValueError, TypeError) as val_err:
            if "reconciliation_status" in cols:
                conn.execute(
                    "UPDATE agent_runs SET reconciliation_status = 'failed' WHERE run_id = ?;",
                    (run_id,)
                )
                conn.commit()
            return {
                "success": False,
                "run_id": run_id,
                "reconciliation_status": "failed",
                "error": f"Malformed provider token value: {val_err}"
            }

        # Valid confirmed tokens
        resolved_req_id = lookup_result.get("provider_request_id") or provider_req_id

        prior_tokens = int(run_data.get("tokens_used", 0) or 0)
        prior_cost = float(run_data.get("cost_booked") if "cost_booked" in run_data and run_data["cost_booked"] is not None else round_cost((prior_tokens / 1_000_000.0) * 0.15, 6))
        prior_cost_source = run_data.get("cost_source")
        delta_tokens = confirmed_tokens - prior_tokens
        calc_cost = round_cost((delta_tokens / 1_000_000.0) * 0.15, 6)

        if lookup_result.get("cost") is not None:
            try:
                reported_cost = float(validate_financial_numeric(lookup_result["cost"], field_name="provider_cost", allow_negative=False))
            except (ValueError, TypeError) as cost_err:
                if "reconciliation_status" in cols:
                    conn.execute(
                        "UPDATE agent_runs SET reconciliation_status = 'failed' WHERE run_id = ?;",
                        (run_id,)
                    )
                    conn.commit()
                return {
                    "success": False,
                    "run_id": run_id,
                    "reconciliation_status": "failed",
                    "error": f"Malformed or invalid provider cost value: {cost_err}"
                }

            if lookup_result.get("is_total_cost", True):
                final_cost = round_cost(reported_cost, 6)
                delta_cost = round_cost(final_cost - prior_cost, 6)
            else:
                delta_cost = round_cost(reported_cost, 6)
                final_cost = round_cost(prior_cost + delta_cost, 6)
            new_cost_source = "provider_confirmed"
        else:
            # Missing provider cost:
            # Never assume a previously booked cost is estimated.
            # Preserve previously authoritative cost if provider supplies no replacement cost.
            if prior_cost_source in ("provider_confirmed", "manual_operator_adjustment"):
                final_cost = prior_cost
                delta_cost = 0.0
                new_cost_source = prior_cost_source
            elif prior_cost_source == "estimated" or prior_cost_source is None:
                delta_cost = calc_cost
                final_cost = round_cost(prior_cost + delta_cost, 6)
                new_cost_source = "estimated"
            else:
                final_cost = prior_cost
                delta_cost = 0.0
                new_cost_source = "unresolved"

        # Validate final calculated financial figures
        final_cost = validate_financial_numeric(final_cost, field_name="final_cost", allow_negative=False)
        delta_cost = validate_financial_numeric(delta_cost, field_name="delta_cost", allow_negative=True)

        journal_id = f"rec_{run_id}_{confirmed_tokens}_{int(time.time() * 1000)}_{uuid.uuid4().hex[:6]}"

        if failpoint == "before_db_commit":
            raise IOError("Crash simulated before SQLite commit")

        # Concurrency safety: check if another thread reconciled this run while we performed lookup
        current_run = conn.execute("SELECT tokens_used, token_source, reconciliation_status, cost_booked, cost_source FROM agent_runs WHERE run_id = ?;", (run_id,)).fetchone()
        if current_run and current_run["reconciliation_status"] == "completed":
            return {
                "success": True,
                "already_reconciled": True,
                "run_id": run_id,
                "tokens_used": current_run["tokens_used"],
                "token_source": current_run["token_source"],
                "cost_source": current_run.get("cost_source"),
                "reconciliation_status": "completed",
                "cost": current_run.get("cost_booked"),
                "message": "Run usage was previously reconciled with authoritative provider data."
            }

        # Atomically update agent_runs AND record outbox journal in SQLite
        update_fields = [
            "tokens_used = ?",
            "token_source = 'provider_confirmed'",
            "reconciliation_status = 'completed'"
        ]
        params = [confirmed_tokens]

        if "cost_booked" in cols:
            update_fields.append("cost_booked = ?")
            params.append(final_cost)

        if "cost_source" in cols:
            update_fields.append("cost_source = ?")
            params.append(new_cost_source)

        if "provider_request_id" in cols and resolved_req_id:
            update_fields.append("provider_request_id = ?")
            params.append(resolved_req_id)

        params.append(run_id)
        params.append(prior_tokens)
        sql = f"UPDATE agent_runs SET {', '.join(update_fields)} WHERE run_id = ? AND tokens_used = ?;"
        up_cur = conn.execute(sql, params)
        if up_cur.rowcount == 0:
            chk = conn.execute("SELECT tokens_used, reconciliation_status, cost_booked, cost_source FROM agent_runs WHERE run_id = ?;", (run_id,)).fetchone()
            if chk and chk["reconciliation_status"] == "completed":
                return {
                    "success": True,
                    "already_reconciled": True,
                    "run_id": run_id,
                    "tokens_used": chk["tokens_used"],
                    "token_source": "provider_confirmed",
                    "cost_source": chk.get("cost_source"),
                    "reconciliation_status": "completed",
                    "cost": chk.get("cost_booked"),
                    "message": "Run usage was previously reconciled with authoritative provider data."
                }
            raise ConcurrencyConflictError(f"Concurrent update conflict on run {run_id} during reconciliation.")

        if j_table:
            conn.execute("""
                INSERT INTO reconciliation_journal (
                    journal_id, run_id, target_tokens, delta_tokens, delta_cost,
                    status, provider_request_id, created_at, target_cost
                ) VALUES (?, ?, ?, ?, ?, 'applied_db', ?, ?, ?);
            """, (journal_id, run_id, confirmed_tokens, delta_tokens, delta_cost, resolved_req_id, time.time(), final_cost))

        conn.commit()

        if failpoint == "after_db_before_budget":
            raise IOError("Crash simulated after DB commit before budget update")

        # Project delta to budget ledger
        project_reconciliation_to_budget(
            budget_path=eff_budget_path,
            journal_id=journal_id,
            run_id=run_id,
            delta_tokens=delta_tokens,
            delta_cost=delta_cost
        )

        if failpoint == "after_budget_before_journal_complete":
            raise IOError("Crash simulated after budget projection before journal completion")

        if j_table:
            conn.execute(
                "UPDATE reconciliation_journal SET status = 'completed', completed_at = ? WHERE journal_id = ?;",
                (time.time(), journal_id)
            )
            conn.commit()

        return {
            "success": True,
            "run_id": run_id,
            "provider_request_id": resolved_req_id,
            "tokens_used": confirmed_tokens,
            "token_source": "provider_confirmed",
            "cost_source": new_cost_source,
            "reconciliation_status": "completed",
            "delta_tokens": delta_tokens,
            "delta_cost": delta_cost,
            "cost": final_cost,
            "message": "Authoritative provider usage successfully reconciled."
        }


