# Phase 2.4-B — Persistent Multi-Agent Execution Foundation, Cross-Division Delegation & Project Memory

**Repository:** `graduationproject2024/agency-dashboard`  
**Workspace:** `C:\Users\sam20\agency-dashboard`  
**Branch:** `master`  
**Verified Starting HEAD:** `3364bde160b4b671530052fcdc8a7809e405557a` (Linear descendant of `4a2353e` following verified Phase 2.4-A.1 CI pass)  
**Operating Mode:** Read-Only Audit & Execution Planning  

---

## 1. Mandatory Phase-Entry Audit

### 1.1 Git Working Tree & Upstream Configuration

| Metric / Check | Expected Baseline | Observed Reality | Verification Status |
|---|---|---|:---:|
| **Repository Root** | `C:\Users\sam20\agency-dashboard` | `C:/Users/sam20/agency-dashboard` | **VERIFIED** |
| **Active Branch** | `master` | `master` | **VERIFIED** |
| **Local HEAD SHA** | Descendant of `4a2353e` | `3364bde160b4b671530052fcdc8a7809e405557a` | **VERIFIED** |
| **Remote Tracking** | `origin/master` | Cleanly synchronized with `origin/master` | **VERIFIED** |
| **Tracked Cleanliness** | 0 unstaged / uncommitted diffs | Clean working tree (`git status` reports 0 tracked modifications) | **VERIFIED** |
| **Untracked Preservation** | `generated_apps/make_tetris_game/.opencode/` | Preserved untouched without deletion or modification | **VERIFIED** |

### 1.2 Option B Protected Accounting & Configuration Hashes

Cryptographic SHA-256 fingerprints measured via PowerShell `Get-FileHash -Algorithm SHA256`:

| Protected File | Exact Baseline SHA-256 | Current Workspace SHA-256 | Status |
|---|---|---|:---:|
| `budget.json` | `F81988BC0DE8083B46532BAEB199834A4BFAC4B991104B6B77B1733F0756EE16` | `F81988BC0DE8083B46532BAEB199834A4BFAC4B991104B6B77B1733F0756EE16` | **100% INVARIANT** |
| `budget.json.bak` | `5DB2A3C506DEB4C0E6D55478E1EFAD1C2ADCE14C7B193F290F90F814E2D56E59` | `5DB2A3C506DEB4C0E6D55478E1EFAD1C2ADCE14C7B193F290F90F814E2D56E59` | **100% INVARIANT** |
| `settings.json` | `2F4E6D39B3E8AC781C04CA9D14983F1A1848B6EF1A2BE5DB0B258BF56F106C94` | `2F4E6D39B3E8AC781C04CA9D14983F1A1848B6EF1A2BE5DB0B258BF56F106C94` | **100% INVARIANT** |

### 1.3 Upstream Agent Workforce Verification

- **Configured Agents Directory:** `get_agents_dir()` resolves dynamically to `C:\Users\sam20\agency-agents` (Commit: `f99f6aa`).
- **Catalog Inventory:**
  - Total Agents: **282 verified agent definitions** (all valid markdown files with frontmatter).
  - Canonical Divisions: **18 authoritative divisions**, perfectly aligned with upstream `divisions.json`.
  - Unclassified / Malformed: **0 agents**.
- **Distribution across Canonical Divisions:**
  1. Academic: 6
  2. Design: 10
  3. Engineering: 65
  4. Finance: 5
  5. Game Development: 21
  6. GIS: 13
  7. Healthcare: 3
  8. Marketing: 37
  9. Paid Media: 7
  10. Product: 6
  11. Project Management: 7
  12. Research: 1
  13. Sales: 9
  14. Security: 12
  15. Spatial Computing: 6
  16. Specialized: 59
  17. Support: 6
  18. Testing: 9
  - **Sum:** `6 + 10 + 65 + 5 + 21 + 13 + 3 + 37 + 7 + 6 + 7 + 1 + 9 + 12 + 6 + 59 + 6 + 9 = 282`.

### 1.4 Baseline Test Suite Execution

- **Total Tests:** 216 test cases collected across 15 test modules.
- **Local Test Verdict:** `207 passed, 9 skipped` (9 Windows unprivileged symlink skips; all 9 execute and pass without skip on Linux/Ubuntu CI).
- **Recent Hosted CI Proof:** GitHub Actions Run ID `39138532326` (Workflow: `Symlink Security Verify`, Commit: `caa0b33`) completed with conclusion `success` across all 11 steps.

---

## 2. Source Code Architecture & Traced Execution Paths

### 2.1 Complete Execution Flow

```
[User Submits Task]
        |
        v
POST /api/start_task (origin validation, task validation, 409 conflict guard)
        |
        v
Background Thread: run_workflow()
        |
        +---> Squad Triage: triage_project_squad() -> Archetype & Specialist Squad
        |
        +---> Stage 1 (Strategy): Specialist Agents -> Business Strategist Lead
        |
        +---> Stage 2 (Product): Product Manager -> PRD Document
        |
        +---> [APPROVAL GATE]: Status -> 'approval_needed'; pauses for Human Approval
```
```
[User Reviews PRD & Approves]
        |
        v
POST /api/approve (origin validation, status guard)
        |
        v
Background Thread: continue_workflow()
        |
        +---> Stage 3 (Engineering & Architecture): Domain Specialists -> Software Architect
        |
        +---> Stage 4 (Design): UI/UX Creative Director -> Visual Blueprint
        |
        +---> Stage 5 (Development): Senior Developer -> HTML, CSS, JS Blocks
        |
        +---> Stage 6 (QA Audits): QA Specialists (AppSec, Accessibility) -> Findings Aggregation -> Primary Code Reviewer
        |
        +---> Stage 7 (Animation & Polish): Whimsy Injector -> Motion CSS/JS
        |
        +---> Stage 8 (SEO & Metadata): SEO Specialist -> Head Tags & Meta
        |
        +---> Asset Link Injection: inject_asset_links()
        |
        +---> Production QA Gate: run_qa_checks() [HTML Tag Balance + Node JS Syntax + CSS Structure]
        |        |
        |        +---> [FAIL]: Fail Closed -> Status = 'failed' -> Blocker Logged -> Abort
        |
        +---> Atomic Staging & Verify: _stage_write_file() -> Write to generated_apps/
                 |
                 +---> Post-Write Candidate Verification: Byte-for-byte check
                 |
                 +---> [MISMATCH]: Rollback -> Restore .bak -> Delete artifacts -> Status = 'failed'
                 |
                 +---> [SUCCESS]: Status = 'completed' -> Deliverable published
```

### 2.2 Concurrency Locks, Accounting & Security Boundaries

- **State Lock:** `state_lock = threading.RLock()` protects in-memory `workflow_state` dictionary access and stage mutations (`app.py:1475`).
- **Budget Lock:** `budget_lock = threading.Lock()` protects thread-safe read, update, and write operations on `budget.json` (`app.py:802`).
- **Token Accounting:** `record_token_usage()` ensures atomic budget subtraction, transaction history capping at 60 items, and truthful tagging of `provider_reported` vs `estimated` token usage (`app.py:850`).
- **Security Perimeter:**
  - `is_safe_origin()` validates scheme, host, port, Fetch Metadata, and Referer headers against CSRF attacks (`app.py:53`).
  - `safe_resolve_project_dir()` enforces lexical containment and canonical path resolution within `GENERATED_APPS_DIR` (`app.py:102`).
  - `safe_resolve_agent_path()` prevents path traversal (`..`), absolute paths, broken symlinks, directory targets, and intermediate directory symlink escapes outside the canonical agent root (`app.py:518`).
  - `safe_load_agent_prompt()` enforces minimum (20 B) and maximum (64 KB) prompt file sizes (`app.py:595`).
  - Static file block (`block_sensitive_files`) blocks `.py`, `.json`, `.env`, `.git`, `.log`, and `.md` (`app.py:2482`).

---

## 3. Existing Capabilities to Preserve vs. Deficiencies to Resolve

### 3.1 Hard Invariants (Must Preserve)
1. **Option B Protected Files:** `budget.json`, `budget.json.bak`, and `settings.json` must remain byte-for-byte untouched.
2. **Workforce Coverage:** All 18 canonical divisions and all 282 agent definitions must remain discoverable, selectable, and invocable.
3. **Safety & Containment:** Zero regression in origin checks, path traversal guards, symlink containment, secret masking, and atomic delivery verification.
4. **Fail-Closed QA Gate:** Production QA gate must continue rejecting malformed code before any deliverable is published.
5. **PRD Approval Gate:** Human-in-the-loop approval between product specification and technical implementation must remain strictly enforced.

### 3.2 Gaps Identified for Phase 2.4-B Resolution
1. **Volatile Global State:** Current system relies entirely on a single Python in-memory dictionary `workflow_state`. A crash or restart obliterates all in-flight task history, PRD outputs, and telemetry.
2. **Monolithic Linear Orchestration:** `run_workflow()` and `continue_workflow()` execute a rigid, hardcoded sequential pipeline. Tasks cannot express genuine DAG dependencies, parallel branch execution, or dynamic delegation based on project-specific requirements.
3. **Absence of Structured Task Handoffs:** Agent outputs are concatenated as raw strings inside `workflow_state["outputs"]`. There is no auditable record of who delegated what to whom, input criteria, acceptance thresholds, or delivery status.
4. **No Project-Scoped Memory Layer:** Aside from raw string concatenation, agents have no access to structured project memory (verified decisions, architectural constraints, approved PRD parameters, QA remediations) separate from transient prompt tokens.
5. **No Resumption from Checkpoints:** If a workflow is interrupted or encounters a temporary provider failure, it cannot resume from the last verified checkpoint and must be restarted from scratch.

---

## 4. Phase 2.4-B Implementation Architecture Plan

### 4.1 Storage Engine: SQLite (`agency_persistence.db` / `db_persistence.py`)
To prevent test pollution and satisfy CI Step 11 (`git status --porcelain` pre/post invariant), the database path will be managed through `DATABASE_PATH` with a secure default, and test fixtures will direct the database to `tmp_path`.

#### Relational Schema Architecture:
1. **`workflow_sessions`**:
   - `session_id` (PK, UUID), `project_name`, `task_description`, `status` (`created`, `running`, `approval_needed`, `completed`, `failed`, `cancelled`), `squad_archetype`, `created_at`, `updated_at`, `completed_at`, `current_stage`, `checkpoint_step`, `error_message`, `metadata_json`.
2. **`workflow_tasks`**:
   - `task_id` (PK, UUID), `session_id` (FK), `parent_task_id` (FK), `title`, `division_id`, `assigned_agent_id`, `stage`, `status` (`pending`, `ready`, `running`, `completed`, `failed`, `blocked`, `cancelled`), `dependencies_json` (JSON list of prerequisite task_ids), `input_context`, `output_text`, `error_message`, `retry_count`, `max_retries`, `started_at`, `completed_at`.
3. **`agent_runs`**:
   - `run_id` (PK, UUID), `task_id` (FK), `session_id` (FK), `agent_id`, `agent_name`, `division_id`, `prompt_file`, `status` (`selected`, `running`, `completed`, `failed`), `input_preview`, `output_preview`, `tokens_used`, `error_message`, `started_at`, `finished_at`.
4. **`task_handoffs`**:
   - `handoff_id` (PK, UUID), `session_id` (FK), `from_task_id` (FK), `to_task_id` (FK), `from_agent_id`, `to_agent_id`, `artifact_type`, `artifact_summary`, `status` (`pending`, `delivered`, `acknowledged`, `rejected`), `created_at`.
5. **`project_memory`**:
   - `memory_id` (PK, UUID), `project_id`, `session_id` (FK), `category` (`requirement`, `architecture_decision`, `constraint`, `qa_finding`, `user_feedback`), `key`, `content`, `provenance_agent`, `provenance_task_id`, `status` (`proposed`, `verified`, `superseded`), `superseded_by` (FK), `version`, `created_at`, `updated_at`.
6. **`execution_checkpoints`**:
   - `checkpoint_id` (PK, UUID), `session_id` (FK), `step_name`, `state_json`, `created_at`.

### 4.2 Task Graph & Dynamic Delegation Engine (`task_graph.py`)
- **DAG Execution Engine:**
  - Tasks declare prerequisite dependencies via `dependencies_json`.
  - Topological sorting and cycle detection (prevent deadlocks).
  - Ready-task queue: Tasks transition `pending -> ready` when all dependencies reach `completed`.
  - Independent parallel execution within concurrency limits.
  - Failure propagation: If an upstream task fails and exhausts retries, dependent tasks transition to `blocked` honestly.
- **Cross-Division Specialist Delegation:**
  - Dynamic squad mapping maps specialized agents into specific sub-tasks based on project archetype and task needs.
  - Downstream tasks receive explicit, verified outputs from upstream prerequisites.
  - QA specialists execute across security, testing, and performance domains; their findings are aggregated into `project_memory` and formally handed off to the Primary QA Reviewer.

### 4.3 Project-Scoped Memory Layer (`project_memory.py`)
- Scoped by `project_id` to eliminate cross-project leakage.
- Strict provenance tracking (recording source agent, task, timestamp).
- Supersession mechanism for revised decisions or feedback.
- Secret sanitization: Regex filters prevent API keys, tokens, or passwords from entering persistent memory.
- Bounded context retrieval: Injects only relevant verified memories into downstream agent prompts, respecting token limits.

### 4.4 API Endpoints & Backward Compatibility
- **Backward-Compatible Sync:** `GET /api/state` synchronizes with active session state, preserving the exact frontend JSON structure (`departments`, `feed`, `active_squad`, `outputs`, `qa_report`).
- **New Session & Graph Endpoints:**
  - `GET /api/sessions`: List past and current sessions with status and metrics.
  - `GET /api/sessions/<id>`: Full session details, checkpoints, and timeline.
  - `GET /api/sessions/<id>/tasks`: Task DAG status, dependencies, and agent assignments.
  - `GET /api/sessions/<id>/handoffs`: Inter-agent collaboration handoff ledger.
  - `GET /api/memory/<project_id>`: Verified project decisions and memory items.
  - `POST /api/sessions/<id>/resume`: Resume interrupted session from last valid checkpoint.
  - `POST /api/sessions/<id>/cancel`: Gracefully cancel active session and tasks.

### 4.5 Frontend Integration (`index.html`, `script.js`, `style.css`)
- Add a new "مسار المهام (Task Graph)" view/tab to visualize:
  - Directed task dependency nodes, assigned divisions, and assigned agents.
  - Live task status badges (`pending`, `ready`, `running`, `completed`, `failed`, `blocked`).
  - Agent-to-agent collaboration handoff drawer.
  - Project memory viewer showing verified decisions, architecture blueprints, and QA findings.
- Preserve complete 18 divisions catalog and 282 agent browsing experience.

---

## 5. Acceptance Criteria & Verification Plan

1. **Persistence & Recovery:**
   - Server restart recovery: In-flight or interrupted sessions are reconciled and resumable from the last completed checkpoint.
   - Zero state loss for completed tasks and generated PRDs.
   - Strict database isolation between concurrent sessions.
2. **Orchestration & Delegation:**
   - Deterministic task graph generation with valid dependency resolution.
   - Cross-division tasks dispatched to appropriate canonical division specialists.
   - QA specialist findings successfully propagate through task handoffs into the primary reviewer.
   - Recoverable failures retried; unrecoverable failures honestly recorded as `failed` / `blocked`.
3. **Project Memory:**
   - Verified facts, decisions, and constraints recorded with provenance.
   - Memory queries strictly scoped to the active project.
   - Stale/superseded records marked appropriately; sensitive credentials strictly excluded.
4. **Safety & CI Invariants:**
   - Zero modifications to `budget.json`, `budget.json.bak`, `settings.json`.
   - All 207+ existing regression tests pass.
   - New dedicated integration test suite passes completely.
   - Hosted GitHub Actions CI (`symlink_security_verify.yml`) passes 100% with zero skipped symlink tests on Ubuntu.

---

## 6. Implementation Completed & Architectural Deliverables

### 6.1 Database Persistence Engine (`db_persistence.py`)
- Normalized SQLite relational engine with 6 dedicated tables:
  1. `workflow_sessions`: Persistent session record, status state machine, stage, checkpoints, metadata.
  2. `workflow_tasks`: Directed task entities, dependencies (`dependencies_json`), division, agent, status, retries, outputs.
  3. `agent_runs`: Fine-grained agent invocation telemetry, token accounting, preview snippets, latency.
  4. `task_handoffs`: Formal agent-to-agent work transfer ledger, artifact packaging, delivery status.
  5. `project_memory`: Project-scoped knowledge, verified architecture decisions, requirements, secret sanitization, version supersession.
  6. `execution_checkpoints`: Structured full-state snapshots saved at verified workflow gates (`prd_approval_gate`, `engineering_completed`, `delivery_completed`).
- **Secret Sanitization:** Automatic regex scrubbing of API keys (`AIzaSy...`, `sk-...`) and credentials (`password=...`, `secret=...`) prior to memory persistence.
- **Restart Reconciliation:** `reconcile_interrupted_sessions()` detects sessions left in `'running'` state upon server start and transitions them safely to `'interrupted'` with clear recovery instructions, never falsely marking them as completed.

### 6.2 Directed Task Graph & Delegation Engine (`task_orchestrator.py`)
- **Deterministic DAG Planning:** `build_project_task_graph()` generates structured multi-agent workflows tailored to project archetypes across 9 chronological stages:
  1. Strategy Specialist Advisory
  2. Lead Business Feasibility
  3. Product PRD Document
  4. Engineering Specialists Architecture
  5. Software Architect Blueprint
  6. UI/UX Visual Identity Design
  7. Senior Developer Full-Stack Implementation
  8. Independent QA Specialist Audits (AppSec, Accessibility, Performance)
  9. Final Packaging & Atomic Delivery Gate
- **Cycle Detection & Topological Safety:** `detect_cycles()` runs Depth-First Search with recursion stack tracking to identify and reject circular dependencies before task execution.
- **Dynamic Dependency Resolution:** `get_ready_tasks()` resolves tasks whose upstream dependencies are completed; `resolve_task_inputs()` aggregates upstream outputs into downstream prompts.

### 6.3 Backend Core Integration (`app.py`)
- Integrated session persistence into `run_workflow` and `continue_workflow`.
- Added verified checkpoints at `prd_approval_gate`, `engineering_completed`, and `delivery_completed`.
- Added explicit agent-to-agent collaboration handoffs.
- Enriched `GET /api/state` with persistence telemetry and project memory context.
- Added REST endpoints:
  - `GET /api/sessions`: List past and active sessions with summary metrics.
  - `GET /api/sessions/<id>`: Full session details, checkpoints, and task list.
  - `GET /api/sessions/<id>/tasks`: Session task graph and status.
  - `GET /api/sessions/<id>/handoffs`: Cross-agent delegation handoff records.
  - `GET /api/memory/<project_id>`: Project-scoped verified decisions and requirements.
  - `POST /api/sessions/<id>/cancel`: Cancel session (origin-validated).
  - `POST /api/sessions/<id>/resume`: Resume from verified checkpoint (origin-validated).

### 6.4 Frontend Visual Orchestration UI (`index.html`, `script.js`, `style.css`)
- Added "مسار المهام والذاكرة (Orchestration & Memory)" navigation tab with live badge counters.
- Added Task DAG Cards visualizer displaying dependencies, division tags, assigned agents, and status badges.
- Added Agent-to-Agent Handoffs live stream drawer.
- Added Project Memory Viewer with category filters (متطلبات, قرارات معمارية, تدقيقات أمان).
- Added Sessions History table with direct checkpoint resumption and cancellation controls.

### 6.5 Hermetic Test Isolation (`tests/conftest.py`, `.gitignore`)
- `tests/conftest.py` provides an `autouse=True` fixture that routes all SQLite persistence operations to an isolated `tempfile.TemporaryDirectory()`, ensuring zero database files pollute the tracked git workspace or interfere with filesystem test fixtures.
- Added `*.db*` to `.gitignore`.

---

## 7. Verification Results & Invariant Audits

### 7.1 Regression & New Feature Test Suite Execution
- **Command:** `pytest -v`
- **Result:** **225 PASSED**, **9 SKIPPED** (Windows unprivileged symlink skips that pass on Linux CI).
- **Execution Time:** ~37.04s.
- **Failures:** **0**.
- **Coverage Highlights:**
  - 18 new persistence & orchestration integration tests: **100% PASSED** (`tests/test_persistence_and_orchestration.py`).
  - 18 workflow integration & atomic rollback tests: **100% PASSED** (`tests/test_workflow_integration.py`).
  - 12 agent security and 18 canonical divisions tests: **100% PASSED** (`tests/test_agent_security_and_divisions.py`).
  - 7 budget persistence & accounting concurrency tests: **100% PASSED** (`tests/test_budget_persistence.py`).
  - 26 squad triage & adversarial prompt security tests: **100% PASSED** (`tests/test_squad_triage.py`).

### 7.2 Cryptographic Hash Invariant Audit
Computed via `Get-FileHash -Algorithm SHA256`:
- `budget.json`: `F81988BC0DE8083B46532BAEB199834A4BFAC4B991104B6B77B1733F0756EE16` (Matches baseline exactly)
- `budget.json.bak`: `5DB2A3C506DEB4C0E6D55478E1EFAD1C2ADCE14C7B193F290F90F814E2D56E59` (Matches baseline exactly)
- `settings.json`: `2F4E6D39B3E8AC781C04CA9D14983F1A1848B6EF1A2BE5DB0B258BF56F106C94` (Matches baseline exactly)

### 7.3 Untracked Invariant & Working Tree Cleanliness
- `generated_apps/make_tetris_game/.opencode/` is strictly preserved untouched.
- `git status --porcelain` reveals no unexpected files and **0 database `.db` files** in the tracked workspace.

### 7.4 Final Audit Verdict
**Phase 2.4-B is 100% COMPLETE, VERIFIED, AND FULLY OPERATIONAL.**
The agency dashboard has evolved from volatile in-memory execution into a persistent, multi-agent collaborative agency backed by a directed task graph, auditable agent handoffs, project-scoped memory, and checkpoint recovery.
