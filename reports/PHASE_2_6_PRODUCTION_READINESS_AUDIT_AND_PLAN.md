# Phase 2.6 — Independent Production Readiness Audit & Review-Gated Implementation Plan

**Repository:** `graduationproject2024/agency-dashboard`  
**Workspace:** `C:\Users\sam20\agency-dashboard`  
**Audited Branch:** `master`  
**Current Verified HEAD:** `f6e184a88e38be091c67e33cf6c9b47db4e1aac9`  
**Previous Reported Baseline:** `27d5f90db736b1fd64be1d73e21136593fb63b48`  
**Execution Mode:** STRICT READ-ONLY AUDIT & PLANNING (Zero code mutations, zero test runs, zero git state modifications during audit)  
**Date of Audit:** October 19, 2026  
**Final Audit Verdict:** **CONDITIONAL PASS — PRODUCTION BLOCKED PENDING REVIEW-GATED REMEDIATION**

---

## 1. Executive Summary & Evidence-Based Verdict

This independent audit evaluated the autonomous software agency codebase, database schemas, API routes, failure-injection boundary handling, and test suites following the reported completion of Phase 2.5. 

### Key Findings:
1. **Core Implementation Verified:** The Phase 2.5 mechanisms (`validate_project_artifact_path`, `preview_rollback_reconciliation`, `apply_rollback_reconciliation`, `reconcile_provider_run_usage`, `reconcile_interrupted_sessions` step 5, table `rollback_recovery_records`, and associated REST routes) were independently inspected and confirmed present in `db_persistence.py` and `app.py` [`[VERIFIED FROM SOURCE]`].
2. **Crash-Consistent Filesystem Recovery Exists But Has Incomplete Cleanup:** Recovery intent is durably committed to SQLite before destructive operations, atomic file replacement (`os.replace`) is enforced, and user edits made after incidents are protected against overwrite. However, temporary staging files (`.restoring_<uuid>`) generated during restoration are abandoned on disk if a crash occurs prior to `os.replace`, and startup recovery does not prune them [`[VERIFIED FROM SOURCE]`].
3. **Severe Disconnect Between Provider Reconciliation and Ledger Accounting:** While `reconcile_provider_run_usage` reconciles SQLite `agent_runs`, it **never updates `budget.json`**. As a result, the primary financial ledger and user-facing budget dashboard permanently diverge from the reconciled database ledger. Furthermore, live provider call dispatch in `app.py` **never extracts or records `provider_request_id`** from Gemini or OpenRouter HTTP responses, leaving `agent_runs.provider_request_id` `NULL` during real production runs [`[VERIFIED FROM SOURCE]`].
4. **Authorization & Origin Defense Boundary Limitations:** The newly registered endpoints (`/preview`, `/apply`, `/reconcile_usage`) strictly enforce browser origin and CSRF policies (`is_safe_origin`), preventing cross-site browser attacks. However, there is **zero identity, authentication, or role enforcement**. Any local process or unauthenticated client capable of issuing HTTP requests to `127.0.0.1:8080` can execute arbitrary restorations or use `force=True` to overwrite user files [`[VERIFIED FROM SOURCE]`].
5. **CI Pipeline Gap:** The existing GitHub Actions workflow (`symlink_security_verify.yml`) runs exclusively on `ubuntu-latest` and only executes legacy security suites. It **does not execute** Phase 2.4-H, 2.4-I, 2.4-J, 2.4-K, or Phase 2.5 test suites, meaning none of the 385 locally passing tests are validated by automated CI [`[VERIFIED FROM SOURCE]`].

---

## 2. Actual Repository Baseline and Discrepancies

### 2.1 Git Metadata Inspection
- **Current Branch:** `master` [`[VERIFIED FROM SOURCE]`]
- **Upstream Tracking:** `origin/master` (Synchronized, up to date) [`[VERIFIED FROM SOURCE]`]
- **HEAD Commit Hash:** `f6e184a88e38be091c67e33cf6c9b47db4e1aac9` (`feat(phase-2.5): production-safe reconciliation, crash recovery & provider billing integrity`) [`[VERIFIED FROM SOURCE]`]
- **Prior Baseline Commit:** `27d5f90db736b1fd64be1d73e21136593fb63b48` (`feat(phase-2.4-k)...`) [`[VERIFIED FROM SOURCE]`]
- **Discrepancy Note:** The prompt stated the latest reported baseline was `27d5f90...`. That commit was the baseline *prior* to Phase 2.5; commit `f6e184a...` contains the Phase 2.5 commit uploaded to origin.

### 2.2 Working Tree Status
```
On branch master
Your branch is up to date with 'origin/master'.

Untracked files:
  (use "git add <file>..." to include in what will be committed)
	generated_apps/make_tetris_game/.opencode/

nothing added to commit but untracked files present (use "git add" to track)
```
- **Staged Changes:** None [`[VERIFIED FROM SOURCE]`]
- **Unstaged Changes:** None [`[VERIFIED FROM SOURCE]`]
- **Untracked Files:** `generated_apps/make_tetris_game/.opencode/agents/` (10 markdown agent definitions; user-generated project data preserved intact) [`[VERIFIED FROM SOURCE]`]

---

## 3. Protected-File Fingerprints & Workspace State

All three protected configuration and ledger files were verified byte-for-byte and hash-for-hash against established baselines:

| File | Byte Size | SHA-256 Digest | Status |
|---|---|---|---|
| `budget.json` | 14,919 bytes | `50A7E3252558C98BF81C72B2F5C8C964B643D47F14E61511BA99F13023F960D8` | **IMMUTABLE / UNTOUCHED** [`[VERIFIED FROM SOURCE]`] |
| `budget.json.bak` | 14,977 bytes | `783C2E168DD0A33D0E834BB508C7228D2813A83012249B4A400D4590BFD83812` | **IMMUTABLE / UNTOUCHED** [`[VERIFIED FROM SOURCE]`] |
| `settings.json` | 44 bytes | `2F4E6D39B3E8AC781C04CA9D14983F1A1848B6EF1A2BE5DB0B258BF56F106C94` | **IMMUTABLE / UNTOUCHED** [`[VERIFIED FROM SOURCE]`] |

### Existing Database & Backup Files on Disk:
- `agency_persistence.db`: 339,968 bytes [`[VERIFIED FROM SOURCE]`]
- `agency_workflow.db`: 0 bytes [`[VERIFIED FROM SOURCE]`]
- `test_diag.db`: 86,016 bytes [`[VERIFIED FROM SOURCE]`]
- WAL/SHM active files: None present [`[VERIFIED FROM SOURCE]`]

---

## 4. Phase 2.5 Implementation Traceability Matrix

| Function / Component | Location in Codebase | Registered Endpoint / Caller | Evidence Label | Status & Verification Notes |
|---|---|---|---|---|
| `validate_project_artifact_path` | `db_persistence.py:1932-1992` | Called by `record_incomplete_rollback`, `preview_rollback_reconciliation`, `apply_rollback_reconciliation`, and startup recovery | `VERIFIED FROM SOURCE` | Strictly rejects traversal (`..`), drive letters, UNC, ADS (`:`), Windows device names, and symlink escapes outside root. |
| `record_incomplete_rollback` | `db_persistence.py:1995-2050` | `app.py:2530` (in `publish_workflow_deliverables`) and `app.py:2958` (in `continue_workflow`) | `VERIFIED FROM SOURCE` | Durably inserts record with status `pending`, SHA digests, relative paths, and sanitized error strings. |
| `preview_rollback_reconciliation` | `db_persistence.py:2052-2187` | `app.py:3123` (`GET /api/sessions/<id>/reconcile_rollback/preview`) | `VERIFIED FROM SOURCE` | Strictly non-destructive. Calculates current SHA digests on disk and classifies records into `clean_restore`, `conflicted_newer_user_edit`, `already_restored`, or `backup_missing`. |
| `apply_rollback_reconciliation` | `db_persistence.py:2190-2440` | `app.py:3137` (`POST /api/sessions/<id>/reconcile_rollback/apply`) | `VERIFIED FROM SOURCE` | Acquires lease (`in_progress`), re-evaluates disk conflicts, refuses overwrite if conflict exists and `force=False`, executes atomic restore via `os.replace`, verifies post-restore SHA, commits `completed`, and only then removes `.bak`. |
| `reconcile_provider_run_usage` | `db_persistence.py:2443-2623` | `app.py:3174` (`POST /api/runs/<id>/reconcile_usage`) | `VERIFIED FROM SOURCE` | Authoritative provider lookup without prompt replaying. Preserves 0 tokens, rejects negative/malformed values, enforces idempotency against double-counting. |
| `reconcile_interrupted_sessions` (Step 5) | `db_persistence.py:1570-1634` | Invoked during engine startup and session initialization | `VERIFIED FROM SOURCE` | Scans in-flight recovery records (`in_progress`, `restored`). Completes record if target matches backup; resets to `pending` if not. |
| Table `rollback_recovery_records` | `db_persistence.py:223-238` | Created in `init_db()`; indexed on `session_id` and `status` | `VERIFIED FROM SOURCE` | Schema has valid check constraints and cascade delete on `workflow_sessions(session_id)`. |
| Schema migrations for `agent_runs` | `db_persistence.py:252-260` | Executed in `init_db()` | `VERIFIED FROM SOURCE` | Safely adds `token_source`, `provider_request_id`, and `reconciliation_status` via `ALTER TABLE` if missing. |

---

## 5. Findings Ranked by Severity

### [P1-01] Missing Dual-Ledger Sync: Provider Usage Reconciliation Never Updates `budget.json`
- **Severity:** P1 (High — Financial & Ledger Integrity)
- **Location:** `db_persistence.py:2596-2612` and `app.py:3174-3203`
- **Evidence:** `VERIFIED FROM SOURCE`
- **Concrete Scenario:** An agent run fails with a provider timeout or ambiguous error. The run originally recorded 0 or estimated tokens in `budget.json`. Later, the operator invokes `POST /api/runs/<run_id>/reconcile_usage` with authoritative usage of 5,000 tokens ($0.10 cost). `reconcile_provider_run_usage` updates SQLite table `agent_runs` to `tokens_used = 5000` and `token_source = 'provider_confirmed'`. However, `budget.json` is never updated. The dashboard UI (`GET /api/budget`, `GET /api/state`) continues to report the stale budget data. SQLite and `budget.json` permanently disagree.
- **Minimum Safe Remediation:** When `reconcile_provider_run_usage` resolves confirmed usage, calculate the delta between the previous token count and the confirmed count, and invoke `record_token_usage` in `app.py` under `budget_lock` with source `"provider_confirmed"`, adjusting both total tokens and estimated cost.

### [P1-02] Provider Request ID Disconnect: Live HTTP Adapters Never Extract `provider_request_id`
- **Severity:** P1 (High — Observability & Reconciliation Failure)
- **Location:** `app.py:1458-1474` (`call_agent_gemini`), `app.py:1597-1620` (`call_agent`), and `db_persistence.py:872-910` (`complete_task_with_agent_run`)
- **Evidence:** `VERIFIED FROM SOURCE`
- **Concrete Scenario:** In live execution, `call_agent_gemini` receives a response from Google Gemini containing header `x-goog-request-id` (or OpenRouter response containing `"id": "gen-..."`), but ignores it. `set_last_agent_execution` only accepts `(tokens_used, token_source, provider)`. `complete_task_with_agent_run` does not accept a `provider_request_id` parameter. Consequently, all production rows in `agent_runs` have `provider_request_id = NULL`. If a run fails and needs out-of-band reconciliation, the lookup function cannot query provider APIs by request ID because the ID was never captured.
- **Minimum Safe Remediation:** 
  1. Update `call_agent_gemini` and OpenRouter adapters to extract the request ID from response headers/body.
  2. Pass `provider_request_id` through `set_last_agent_execution` into `complete_task_with_agent_run` and `record_agent_run`.

### [P2-01] Missing Identity & Access Control on High-Privilege Recovery Routes
- **Severity:** P2 (Medium — Application Security)
- **Location:** `app.py:3137-3172` (`apply_rollback_reconciliation_endpoint`)
- **Evidence:** `VERIFIED FROM SOURCE`
- **Concrete Scenario:** The endpoint `POST /api/sessions/<session_id>/reconcile_rollback/apply` validates `is_safe_origin(request)`, preventing CSRF from foreign browser origins. However, it performs no authentication (no API key, session cookie, or bearer token). A malicious local process, compromised local port, or misconfigured reverse proxy can issue a POST request with `{"force": true}`, which will permanently overwrite uncommitted user code with backup copies without logging who authorized the action.
- **Minimum Safe Remediation:** Require an authorization token or secret header (such as an operator token or environment-configured auth key) for state-altering reconciliation operations, and write an immutable operator audit record to `execution_checkpoints` documenting the principal who invoked `force=True`.

### [P2-02] Orphaned Temporary Files on Process Kill During Active Restoration
- **Severity:** P2 (Medium — Storage & File Hygiene)
- **Location:** `db_persistence.py:2346-2351` (`apply_rollback_reconciliation`)
- **Evidence:** `VERIFIED FROM SOURCE`
- **Concrete Scenario:** During restoration, code executes:
  ```python
  temp_restore = target_path + f".restoring_{uuid.uuid4().hex[:6]}"
  with open(temp_restore, "wb") as rf:
      rf.write(backup_bytes)
      ...
  os.replace(temp_restore, target_path)
  ```
  If the operating system kills the process between `open()` and `os.replace()`, the `.restoring_*` file remains permanently on disk in the project directory. In `reconcile_interrupted_sessions` (Step 5), the startup routine checks whether `target_path` matches `backup_path`, but never cleans up orphaned `.restoring_*` files.
- **Minimum Safe Remediation:** In `reconcile_interrupted_sessions`, scan the directory of each in-flight recovery record and unlink any matching `.restoring_*` files older than 60 seconds.

### [P2-03] GitHub Actions CI Does Not Execute Production Recovery or Acceptance Tests
- **Severity:** P2 (Medium — CI / Quality Gate Gap)
- **Location:** `.github/workflows/symlink_security_verify.yml:165-168`
- **Evidence:** `VERIFIED FROM SOURCE`
- **Concrete Scenario:** The CI workflow runs on `ubuntu-latest` and only invokes a specific list of legacy test files. The entire suite of modern phase test suites (`test_phase_2_4_h_independent_acceptance.py`, `test_phase_2_4_i_adversarial_runtime_recovery.py`, `test_phase_2_4_j_production_path_failure_injection.py`, `test_phase_2_4_k_production_recovery_and_accounting.py`, and `test_phase_2_5_reconciliation_and_billing_integrity.py`) is completely omitted from the CI pipeline. A breaking regression pushed to `master` will pass CI unnoticed.
- **Minimum Safe Remediation:** Add a new job or update the existing CI workflow to run `pytest tests/ -v` or explicitly include the Phase 2.4 and 2.5 acceptance suites on both Ubuntu and Windows matrix runners.

### [P3-01] Missing Regex Validation on Run Identifier in Reconciliation Endpoint
- **Severity:** P3 (Low — Input Validation Consistency)
- **Location:** `app.py:3174-3183` (`reconcile_run_usage_endpoint`)
- **Evidence:** `VERIFIED FROM SOURCE`
- **Concrete Scenario:** `session_id` is validated via `is_valid_session_id` against `^[a-zA-Z0-9_\-]{1,64}$`. Conversely, `run_id` on line 3181 is only checked with `if not run_id or not isinstance(run_id, str): return 400`. While SQLite parameterized queries prevent SQL injection, excessively long strings or malformed inputs are not rejected early at the HTTP boundary.
- **Minimum Safe Remediation:** Introduce `is_valid_run_id` with regex `^[a-zA-Z0-9_\-]{1,64}$` to mirror session identifier validation.

---

## 6. Crash-Consistency State-Transition Analysis

```
                              [Incomplete Rollback Detected]
                                            │
                                            ▼
                                   ┌─────────────────┐
                                   │ status: pending │
                                   └────────┬────────┘
                                            │ (apply_rollback_reconciliation claims lease)
                                            ▼
                                ┌──────────────────────┐
                                │ status: in_progress  │
                                └───────────┬──────────┘
                                            │
                    ┌───────────────────────┴───────────────────────┐
                    │                                               │
      (target file edited & force=False)              (safe to restore or force=True)
                    │                                               │
                    ▼                                               ▼
         ┌────────────────────┐                          ┌────────────────────┐
         │ status: conflicted │                          │  temp file staged  │
         └────────────────────┘                          └──────────┬─────────┘
                                                                    │ (os.replace atomic swap)
                                                                    ▼
                                                         ┌────────────────────┐
                                                         │  status: restored  │
                                                         └──────────┬─────────┘
                                                                    │ (SQLite commit)
                                                                    ▼
                                                         ┌────────────────────┐
                                                         │ status: completed  │
                                                         └──────────┬─────────┘
                                                                    │ (unlink .bak)
                                                                    ▼
                                                         [Backup safely purged]
```

### Boundary Evaluations:
1. **Crash during `in_progress` prior to `os.replace`:**
   - Disk target file remains untouched (either original candidate or user work). Backup file `.bak` is preserved.
   - Upon restart, `reconcile_interrupted_sessions` discovers `status = 'in_progress'`. It compares target SHA with backup SHA, sees they do not match, and resets the record to `status = 'pending'`, allowing safe operator re-evaluation. `[VERIFIED FROM SOURCE & TEST]`
2. **Crash after `os.replace` prior to SQLite `status = 'completed'`:**
   - Disk target file has already been replaced with restored backup bytes.
   - Upon restart, `reconcile_interrupted_sessions` discovers `status IN ('in_progress', 'restored')`. It reads the target file, discovers that `target_sha == backup_sha`, updates the database record to `status = 'completed'`, and safely unlinks `.bak`. No file corruption occurs. `[VERIFIED FROM SOURCE & TEST]`
3. **Crash after `status = 'completed'` prior to `.bak` unlink:**
   - Target file is restored, database record is `completed`.
   - The backup file `.bak` lingers on disk. A repeated call to `apply_rollback_reconciliation` detects all records are `completed` and returns early. The backup file is harmless but slightly untidy. `[VERIFIED FROM SOURCE]`

---

## 7. Provider Usage and Billing Integrity Analysis

### Guarantee Verification:
- **No Generative Replaying:** In `reconcile_provider_run_usage` (lines 2484-2498), if `provider_lookup_fn` is missing, the function immediately returns with `reconciliation_status: 'unresolved'` and an explicit message: *"Replay of generative prompts is forbidden."* Under no branch does it invoke `call_agent` or re-prompt the LLM. `[VERIFIED FROM SOURCE]`
- **Explicit Zero Tokens:** Lines 2563-2593 handle `confirmed_tokens = int(confirmed_tokens_raw)`. If the raw token count is `0`, it is parsed as integer `0` (not falsy or `None`), stored as `tokens_used = 0`, and tagged as `token_source = 'provider_confirmed'`. `[VERIFIED FROM SOURCE & TEST]`
- **Negative & Malformed Rejection:** Negative integers and non-integer strings are intercepted before database execution and recorded as `reconciliation_status: 'failed'`. `[VERIFIED FROM SOURCE & TEST]`
- **Deduplication & Idempotency:** If `agent_runs.reconciliation_status == 'completed'`, line 2471 returns immediately with `already_reconciled: True` without recalculating or modifying the record. `[VERIFIED FROM SOURCE & TEST]`
- **Identified Deficiency:** The lack of integration between `reconcile_provider_run_usage` and `budget.json` (Finding P1-01) violates the platform's unified accounting principle.

---

## 8. Authorization and Application Security Analysis

- **CSRF & Origin Policy:** `is_safe_origin` enforces:
  - `Sec-Fetch-Site: cross-site` is rejected immediately.
  - `Origin` must match scheme (`http`/`https`), host (`127.0.0.1`/`localhost`), and port (`8080`).
  - `Referer` is validated if `Origin` is missing.
  - Form posts (`urlencoded`, `multipart`) without origin/referer are blocked.
  - Validated thoroughly in `test_phase_2_4_k` and `test_phase_2_5`. `[VERIFIED FROM SOURCE & TEST]`
- **Secret Redaction:** `sanitize_memory_content` and regex masking in `get_safe_settings_view` ensure that API keys (`AIzaSy...`, `sk-or-...`) are redacted in logs, audit payloads, and checkpoints. `[VERIFIED FROM SOURCE & TEST]`
- **Identified Deficiency:** As noted in Finding P2-01, Origin checks protect browsers from cross-site forged requests, but do not provide user authentication or prevent local unauthorized requests.

---

## 9. Test Validity and CI Evidence Analysis

### Local Test Execution Evidence (from previous test run logs):
- **Phase 2.5 Acceptance Suite:** `tests/test_phase_2_5_reconciliation_and_billing_integrity.py`
  - 19 passed, 0 failed in 16.65s [`[VERIFIED FROM EXISTING TEST/CI EVIDENCE]`]
- **Phase 2.4-K Acceptance Suite:** `tests/test_phase_2_4_k_production_recovery_and_accounting.py`
  - 21 passed, 0 failed in 25.71s [`[VERIFIED FROM EXISTING TEST/CI EVIDENCE]`]
- **Full Repository Suite:**
  - 385 passed, 9 skipped, 0 failed in 384.02s [`[VERIFIED FROM EXISTING TEST/CI EVIDENCE]`]

### Analysis of Test Mechanics:
- **Subprocess Killing:** `test_workstream_b_crash_before_filesystem_restore...` and `test_workstream_b_crash_after_restore...` spawn real Python subprocesses using `subprocess.Popen([sys.executable, ...])`, synchronize via filesystem flags, execute `proc.kill()`, and assert post-kill recovery via independent processes. These are genuine process-boundary tests. `[VERIFIED FROM SOURCE]`
- **Test-Only Mocks vs Production Code:** Test `test_workstream_c_provider_reconciliation_confirmed_usage` passes a custom `mock_provider_lookup` callback into `reconcile_provider_run_usage`. While this accurately tests the reconciliation engine logic, it masked the fact that production HTTP adapters in `app.py` never capture `provider_request_id`. `[VERIFIED FROM SOURCE]`
- **Skipped Tests:** 9 tests skip due to Playwright/browser dependencies not installed in the Python 3.8 environment. This is expected and documented. `[VERIFIED FROM EXISTING TEST/CI EVIDENCE]`

---

## 10. Minimal Prioritized Remediation Roadmap (Review-Gated)

Remediation must proceed in strict dependency order during a future authorized implementation phase:

### Phase 2.6-A: Core Billing Ledger Synchronization & Provider Request Capture
1. **Fix P1-02 (Provider ID Capture):**
   - In `app.py::call_agent_gemini`, extract `res.headers.get("x-goog-request-id")` or response JSON `id`.
   - In `app.py::call_agent` (OpenRouter), extract `res_json.get("id")`.
   - Update `set_last_agent_execution` and `complete_task_with_agent_run` to persist `provider_request_id` in `agent_runs`.
2. **Fix P1-01 (Dual-Ledger Sync):**
   - In `db_persistence.py::reconcile_provider_run_usage`, return the token delta.
   - In `app.py::reconcile_run_usage_endpoint`, call `record_token_usage` with the delta and `source="provider_confirmed"` to update `budget.json`.

### Phase 2.6-B: Crash Hygiene & Endpoint Input Validation
3. **Fix P2-02 (Orphaned Temp File Cleanup):**
   - In `db_persistence.py::reconcile_interrupted_sessions`, scan for and remove lingering `.restoring_*` files in target project directories during startup recovery.
4. **Fix P3-01 (Run Identifier Validation):**
   - Define `is_valid_run_id` in `app.py` and enforce regex validation on `/api/runs/<run_id>/reconcile_usage`.

### Phase 2.6-C: Security & Audit Logging on Force Overrides
5. **Fix P2-01 (Audited Force Overrides):**
   - Record an immutable checkpoint event in SQLite whenever `force=True` is applied, capturing the timestamp, target paths, overwritten SHAs, and request origin.

### Phase 2.6-D: CI Pipeline Modernization
6. **Fix P2-03 (Automated CI Coverage):**
   - Update `.github/workflows/symlink_security_verify.yml` to include acceptance test suites for Phase 2.4-H/I/J/K and Phase 2.5 on both Ubuntu and Windows matrix runners.

---

## 11. Proposed Future Test Matrix

For the future implementation phase, the following new tests must be added to a dedicated suite (`tests/test_phase_2_6_production_readiness_remediation.py`):

| Test ID | Target Component | Scenario | Expected Outcome |
|---|---|---|---|
| `test_p26_live_gemini_request_id_capture` | `app.py::call_agent_gemini` | Mock HTTP 200 response with `x-goog-request-id: req_test_123` | `get_last_agent_execution()["provider_request_id"] == "req_test_123"` and persisted to `agent_runs` |
| `test_p26_live_openrouter_request_id_capture` | `app.py::call_agent` | Mock HTTP 200 response with `"id": "gen_or_456"` | `get_last_agent_execution()["provider_request_id"] == "gen_or_456"` and persisted to `agent_runs` |
| `test_p26_provider_reconciliation_syncs_budget_json` | `app.py::reconcile_run_usage_endpoint` | Run initially recorded with 500 estimated tokens. Reconciled with 2,500 confirmed tokens. | `agent_runs` shows 2,500; `budget.json` total tokens increased by delta (+2,000) and cost adjusted. |
| `test_p26_startup_cleans_orphaned_restoring_files` | `db_persistence.py::reconcile_interrupted_sessions` | Create `.restoring_ab12` file in app directory; trigger startup reconciliation. | Orphaned `.restoring_*` file unlinked; valid backup and target files untouched. |
| `test_p26_reconcile_run_endpoint_rejects_malformed_run_id` | `app.py::reconcile_run_usage_endpoint` | POST to `/api/runs/../../etc/passwd/reconcile_usage` | HTTP 400 Bad Request with `"Invalid run identifier format"`. |
| `test_p26_force_reconciliation_records_audit_trail` | `app.py::apply_rollback_reconciliation_endpoint` | Execute reconciliation with `force=True` on conflicted file. | Execution succeeds; checkpoint audit log contains `"operator_force_override": True` with pre/post SHAs. |

---

## 12. Explicit Limitations & Unverified Items

1. **Remote GitHub Actions Execution:** GitHub repository history and CI execution status could not be queried via remote network API (network disabled in read-only mode). CI assertions are based on local workflow file inspection (`.github/workflows/symlink_security_verify.yml`) [`[REPORTED BUT NOT INDEPENDENTLY VERIFIED]`].
2. **Paid Provider API Validation:** Live API calls to Google Gemini or OpenRouter were not dispatched. Validation is based strictly on static code analysis of request/response parsing logic [`[VERIFIED FROM SOURCE]`].
3. **Real Browser UI Rendering:** Browser smoke tests requiring Chromium/Playwright were skipped as Playwright browser binaries are not installed in the Python 3.8 environment [`[VERIFIED FROM EXISTING TEST/CI EVIDENCE]`].

---

## 13. Review Checklist for Owner Approval Before Implementation

Before proceeding to any code modifications in a future phase, the owner should review and confirm:

- [ ] **Acknowledge P1-01:** Approve synchronizing `budget.json` when provider usage reconciliation changes token totals.
- [ ] **Acknowledge P1-02:** Approve updating `call_agent_gemini` and OpenRouter adapters to capture and persist HTTP request IDs.
- [ ] **Acknowledge P2-01:** Confirm whether the dashboard requires an authentication token/key for `POST /reconcile_rollback/apply` or if localhost network isolation is accepted with audit logging.
- [ ] **Acknowledge P2-02 & P3-01:** Approve cleanup of orphaned `.restoring_*` files on startup and strict `is_valid_run_id` regex validation.
- [ ] **Acknowledge P2-03:** Approve updating `.github/workflows/symlink_security_verify.yml` to run the modern test suites in CI.
- [ ] **Data Preservation Confirmation:** Confirm that `budget.json`, `budget.json.bak`, `settings.json`, and `generated_apps/make_tetris_game/.opencode/` must remain protected and byte-accurate across future phases.

---
*End of Phase 2.6 Independent Audit Report. Workspace remains 100% unmodified and read-only.*
