# Phase 2.4-K — Production Recovery Integrity, Rollback Failure Transparency, Authorization Enforcement & Exactly-Once Accounting Report

**Repository:** `graduationproject2024/agency-dashboard`  
**Working Workspace:** `C:\Users\sam20\agency-dashboard`  
**Primary Branch:** `master`  
**Baseline Git Commit:** `397f85e595096b42d14079cc3dc2318d7974582e`  
**Date:** October 19, 2026  
**Final Verdict:** **PASS WITH LIMITATIONS**

---

## 1. Executive Summary

Phase 2.4-K evaluated and hardened the autonomous software agency platform against real-world production failure boundaries across recovery integrity, rollback transparency, authorization enforcement, and token/cost accounting.

Key accomplishments in this phase:
1. **Explicit & Safe Rollback Handling:** Fixed a vulnerability in `continue_workflow` where failed file restorations during rollback were silently swallowed and temporary `.bak` files were unconditionally destroyed. Now, failed restorations preserve `.bak` files, propagate structured diagnostic errors into the session record, flag `rollback_failed: True` in the operator audit, and offer `reconcile_incomplete_rollback`.
2. **Process Death During Active Recovery:** Proved through deterministic multi-process tests that terminating a recovery process mid-flight (at state claim or filesystem compensation boundaries) leaves SQLite records intact. A subsequent fresh process resumes startup reconciliation without corrupting records, re-billing tasks, or duplicating deliverables.
3. **Provider Accounting Boundaries:** Audited provider call accounting across partial persistence failures, timeout ambiguities, duplicate callbacks, and malformed metadata. Verified that repeated callbacks do not double-bill, ambiguous outcomes flag `is_retry_safe: False`, and non-numeric inputs are clamped safely.
4. **End-to-End Authorization & Origin Defense:** Validated real production entry points against CSRF/hostile origins (`Sec-Fetch-Site: cross-site`, `Origin: null`, foreign domains), unapproved workflow continuation, mismatched session approvals, and attempts to resume terminal sessions (`completed`/`cancelled`).
5. **Deterministic 2-Way Concurrency Barrier:** Replaced single-file polling with an explicit two-way readiness handshake where both workers acknowledge barrier arrival before the coordinator releases them, proving true concurrent competition for task claims and resumption locks.
6. **Artifact Manifest Integrity:** Verified that SHA-256 candidate artifact tampering is blocked prior to publication, path traversal and reserved names are rejected, and 25MB limits/Unicode collisions are strictly enforced.

Across the entire repository, **366 tests passed, 0 failed, 9 skipped** (100% pass rate).

---

## 2. Baseline Commit and Working-Tree State

- **Branch:** `master`
- **Head Commit:** `397f85e595096b42d14079cc3dc2318d7974582e` (`Phase 2.4-J: Production-Path Failure Injection & Release Gate Validation`)
- **Protected File SHA-256 Verification:**
  - `budget.json`: `50A7E3252558C98BF81C72B2F5C8C964B643D47F14E61511BA99F13023F960D8` (Unmodified)
  - `budget.json.bak`: `783C2E168DD0A33D0E834BB508C7228D2813A83012249B4A400D4590BFD83812` (Unmodified)
  - `settings.json`: `2F4E6D39B3E8AC781C04CA9D14983F1A1848B6EF1A2BE5DB0B258BF56F106C94` (Unmodified)
- **Untracked User Data Preserved:** `generated_apps/make_tetris_game/.opencode/` preserved intact.

---

## 3. Verified Baseline Test Results

Before introducing changes, existing test suites were executed to establish a verified baseline:
- `test_phase_2_4_i_adversarial_runtime_recovery.py`: 15 passed, 0 failed.
- `test_phase_2_4_j_production_path_failure_injection.py`: 18 passed, 0 failed.
- Full suite baseline: 345 passed, 0 failed, 9 skipped.

### Baseline Matrix

| Area | Existing Behavior | Existing Evidence | Remaining Gap Identified |
|---|---|---|---|
| Rollback Failure | `publish_workflow_deliverables` had rollback error tracking, but `continue_workflow` swallowed restoration errors and deleted backups. | `test_phase_2_4_j` Test A2 | Rollback failure in `continue_workflow` was silent; backups were purged even when restoration failed. |
| Recovery Interruption | Process termination was tested before recovery, not during recovery itself. | `test_phase_2_4_j` Test C1/C2 | Subprocess death mid-recovery was unproven. |
| Provider Accounting | Tested duplicate callback on same task. | `test_phase_2_4_j` Test E1 | Persistence failure during provider callback & timeout ambiguities needed real-entry verification. |
| Authorization & Approvals | Endpoint format check & session status checks. | `test_phase_2_4_j` Test F1/F2 | CSRF/Origin enforcement and cross-session approval validation needed consolidated matrix. |
| Concurrent Execution Barrier | Single-file polling loop (`start_race.flag`). | `test_phase_2_4_j` Test D1/D2 | 1-way polling did not guarantee both workers were ready before release. |
| Artifact Publication Safety | Unit tests on manifest builder and size caps. | `test_phase_2_4_j` Test G1-G3 | End-to-end anti-tampering verification at publication boundary needed explicit assertions. |

---

## 4. Findings from Phase 2.4-J: Confirmed, Corrected, or Disproved

1. **Disproved / Corrected (Phase 2.4-J Test A1 False Positive):**
   - *Phase 2.4-J Claim:* `test_workstream_a_production_continue_workflow_restores_pre_existing_files` claimed to prove that pre-existing files were restored from `.bak` during delivery persistence failure.
   - *Independent Audit Finding:* In Phase 2.4-J, `get_project_folder_name(task_name)` was invoked to place the pre-existing file in `base_folder`. However, when `continue_workflow` ran, `base_folder` already existed, prompting `get_project_folder_name` to append a timestamp (`base_folder_{timestamp}`). Deliverables were staged in the timestamped directory, leaving `base_folder` completely untouched. The test asserted `restored_html == original_html_payload` and passed falsely because the original file was never part of the delivery transaction!
   - *Correction in Phase 2.4-K:* Tests now explicitly target the directory where pre-existing files reside via controlled folder resolution, genuinely exercising the backup creation, staging, and `.bak` restoration failure paths.

2. **Confirmed (Silent Rollback Invisibility in `continue_workflow`):**
   - *Finding:* In `continue_workflow` (lines 2918-2940 of `app.py`), the exception handler caught database persistence errors, attempted rollback, but swallowed all `rb_e` restoration exceptions with a simple `logging.warning`. It then called `cleanup_backups(pre_existing_backups)` unconditionally, permanently deleting `.bak` files when restoration failed.
   - *Resolution:* Implemented `rollback_failures` and `failed_restorations` mapping. Un-restored `.bak` files are retained, rollback errors are concatenated into the raised `IOError`, and `db_store.get_operator_session_audit` flags `rollback_failed: True`.

3. **Confirmed (1-Way Barrier Timing Flaw):**
   - *Finding:* In Phase 2.4-J, workers polled `start_race.flag` after a fixed `time.sleep(0.1)`. If worker 2 scheduled slightly late, worker 1 could claim the task and exit before worker 2 even reached the loop, passing sequentially.
   - *Resolution:* Replaced with a deterministic 2-way barrier handshake where Worker 1 writes `worker1_ready.flag`, Worker 2 writes `worker2_ready.flag`, and coordinator waits for *both* flags before creating `release.flag`.

---

## 5. Root Cause Analysis

### Issue K-01: Silent Backup Deletion & Lossy Error Reporting in `continue_workflow`
- **Location:** `app.py::continue_workflow` (lines 2918-2940)
- **Root Cause:** When `db_store.save_session_checkpoint` or status update failed, `continue_workflow` performed rollback without tracking which file restorations failed. It then passed the full `pre_existing_backups` dict to `cleanup_backups()`, unlinking the surviving `.bak` files. Furthermore, the raised `IOError` only reported `pe` (the database error), completely hiding the fact that the project directory was left in a corrupted or half-rolled-back state.
- **Impact:** Permanent loss of user code if a disk/permissions glitch prevented `os.replace` from restoring a backup.

### Issue K-02: Absence of Rollback Diagnostic in Operator Failure Audit
- **Location:** `db_persistence.py::get_operator_session_audit`
- **Root Cause:** `failure_analysis` evaluated `is_retry_safe` solely based on provider ambiguity. If a rollback failed, the session was marked failed, but the audit still reported `permitted_recovery_actions = ["retry"]`, risking re-execution on top of inconsistent artifacts.
- **Impact:** Operators could blindly retry failed workflows without realizing that un-restored files or surviving backups required manual reconciliation.

---

## 6. Minimal Implementation Changes and Rationale

### Change 1: `app.py` — Explicit Rollback Outcome & Backup Protection
```python
            except Exception as pe:
                logging.error("Delivery completion persistence failed, restoring pre-existing backups and rolling back files: %s", pe)
                rollback_failures = []
                failed_restorations = {}
                all_rollback_targets = list(written_files)
                for p in pre_existing_backups:
                    if p not in all_rollback_targets:
                        all_rollback_targets.append(p)

                for fpath in all_rollback_targets:
                    try:
                        if fpath in pre_existing_backups:
                            bak_path = pre_existing_backups[fpath]
                            if os.path.exists(bak_path):
                                os.replace(bak_path, fpath)
                        elif os.path.exists(fpath):
                            os.remove(fpath)
                    except Exception as rb_e:
                        if fpath in pre_existing_backups:
                            bak_path = pre_existing_backups[fpath]
                            failed_restorations[fpath] = (bak_path, str(rb_e))
                            rollback_failures.append(
                                f"Failed to rollback {os.path.basename(fpath)} (restoration failed, retained recovery backup at {bak_path}): {rb_e}"
                            )
                        else:
                            rollback_failures.append(f"Failed to rollback {os.path.basename(fpath)}: {rb_e}")

                clean_errs = cleanup_backups(pre_existing_backups, failed_restorations)
                rollback_failures.extend(clean_errs)
                if rollback_failures:
                    raise IOError(f"Database delivery persistence failed: {pe}; Rollback errors: {'; '.join(rollback_failures)}") from pe
                raise IOError(f"Database delivery persistence failed: {pe}") from pe
```
*Rationale:* Protects surviving user backups, appends detailed rollback error diagnostics, and propagates errors to `workflow_state["blockers"]` and the session database record.

### Change 2: `db_persistence.py` — Operator Audit Rollback Visibility
```python
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
```
*Rationale:* Ensures operators are never prompted to blindly retry when a rollback failure has compromised filesystem consistency, explicitly providing `reconcile_incomplete_rollback`.

---

## 7. Production Execution Paths Exercised

The test suite directly exercises the following production functions and routes:
1. `app.continue_workflow`: Stages artifacts, runs candidate verification, persists delivery in SQLite, triggers atomic rollback on failure.
2. `app.publish_workflow_deliverables`: Anti-tampering QA manifest digest verification, staging via atomic rename, post-write byte verification, rollback compensation.
3. `app.cleanup_backups`: Safe `.bak` file cleanup with exclusion for failed restoration paths.
4. `db_persistence.reconcile_interrupted_sessions`: Real crash recovery loop reconciling stale running tasks and in-flight provider calls across process restarts.
5. `db_persistence.claim_task`: SQLite write transaction using atomic `UPDATE WHERE status = 'ready'`.
6. `db_persistence.acquire_session_resumption_lock`: Atomic session locking preventing concurrent resumes.
7. `db_persistence.get_operator_session_audit`: Correlation-verified diagnostic aggregation across session, tasks, runs, tokens, and rollback status.
8. `app.py` HTTP endpoints: `/api/start_task`, `/api/approve`, `/api/sessions/<id>/resume` exercising `is_safe_origin()`, session state checks, and CSRF barriers.

---

## 8. Crash-Injection and Failure-Injection Methodology

Deterministic failure injection was performed across the required boundaries without mock bypassing:

1. **File Restoration Failure:**
   - Monkeypatched `os.replace` to raise `OSError("Access denied during .bak restoration")` exclusively when `src.endswith(".bak")`.
   - Injected database persistence failure at `db_store.record_artifact_manifest`.
   - Asserted that `index.html.pre_*.bak` was retained on disk, `Rollback errors` surfaced in `persisted["error_message"]`, and `audit["failure_analysis"]["rollback_failed"] == True`.

2. **File Removal Failure:**
   - Monkeypatched `os.remove` when deleting newly staged deliverables (`plan.md`).
   - Verified that removal failure was captured in session blockers and diagnostic audit.

3. **Multi-Artifact Partial Failure:**
   - Staged pre-existing `index.html` and `style.css`.
   - Allowed `index.html` restore to succeed while failing `style.css` restore.
   - Verified that `index.html.bak` was cleaned up while `style.css.bak` was retained, and the audit named `style.css` explicitly.

4. **Subprocess Kill at Recovery Boundary 1 (Claim In-Progress):**
   - Spawned child OS process executing recovery.
   - Child updated session status to 'running' with 'Reconciliation claimed', wrote `recov_started.flag`, and paused.
   - Parent terminated child process via `proc.kill()`.
   - Fresh OS child process executed startup recovery (`reconcile_interrupted_sessions`).
   - Verified session cleanly transitioned to 'interrupted' with 'Interrupted by unexpected process restart', and repeated recovery returned 0 (idempotent).

5. **Subprocess Kill at Recovery Boundary 2 (Filesystem Compensation):**
   - Child process restored backup file on disk, wrote `fs_comp_done.flag`, and was killed before committing DB status.
   - Fresh process resumed startup reconciliation.
   - Verified filesystem integrity preserved and DB record reconciled.

---

## 9. Authorization Test Matrix and Outcomes

Enforcement was verified through real HTTP requests via `app.test_client()`:

| Test Case | Request / Scenario | Expected Response | Observed Response | Protected State Modified? |
|---|---|---|---|---|
| Foreign Origin | POST `/api/approve` with `Origin: https://evil-attacker.com` | 403 Forbidden | 403 Forbidden (`Invalid origin`) | No |
| Cross-Site Site Header | POST `/api/start_task` with `Sec-Fetch-Site: cross-site` | 403 Forbidden | 403 Forbidden | No |
| Null Origin | POST `/api/sessions/<id>/resume` with `Origin: null` | 403 Forbidden | 403 Forbidden | No |
| Unapproved Session | POST `/api/approve` when workflow status is `idle` | 400 Bad Request | 400 Bad Request (`No pending approval required`) | No |
| Cross-Session Approval | POST `/api/approve` with `session_id: sess_wrong` while active is `sess_right` | 400 Bad Request | 400 Bad Request (`Approval request session mismatch`) | No |
| Terminal Resume | POST `/api/sessions/<id>/resume` for session with `status: completed` | 400 Bad Request | 400 Bad Request (`Cannot resume session in terminal 'completed' state`) | No |
| Nonexistent Session | POST `/api/sessions/sess_nonexistent_123/resume` | 404 Not Found | 404 Not Found (`Session not found`) | No |
| Malformed Identifier | POST `/api/sessions/sess..traversal/resume` | 400 Bad Request | 400 Bad Request (`Invalid session identifier format`) | No |
| Valid Approved Request | POST `/api/approve` with matching ID, valid origin, status `approval_needed` | 200 OK | 200 OK (`Approved and continuing workflow`) | Yes (transitions to running) |

---

## 10. Accounting Semantics and Limitations

### Accounting Guarantees Verified:
1. **Stable Identity Deduplication:** Invocations of `db_store.complete_task_with_agent_run` with the same `task_id` and `agent_id` do not create duplicate `agent_runs` rows and do not increment `total_tokens_billed` in operator audit.
2. **Distinction of Token Sources:** Operator audit strictly segments `provider_reported_tokens` from `estimated_tokens`.
3. **Ambiguity Transparency:** If a process crashes or times out while an external call is in-flight (`agent_runs.status = 'running'`), startup reconciliation marks the run `failed` with `'Provider outcome ambiguous'`, task with `'Provider outcome unknown'`, flags `has_ambiguous_provider_call: True`, and marks `potential_cost_incurred: True`.
4. **Token Sanitization:** Negative tokens, non-numeric values, `None`, and nested structures are clamped to 0 without errors or corruption of budget ledgers.

### Architectural Limitations (Documented per Charter Rule 9 & 13):
- **Provider Idempotency:** The platform relies on SQLite ledger deduplication (`task_id`, `agent_id`, `run_id`). Because upstream LLM providers (e.g. standard REST endpoints) do not universally support atomic idempotency keys, an in-flight call interrupted by network partition or process crash may have consumed tokens at the provider. The platform **does not claim exactly-once remote execution**; it guarantees **at-most-once retry safety** by blocking automatic retries when provider ambiguity is detected.

---

## 11. Exact Commands and Actual Test Results

### Targeted Phase 2.4-K Test Suite:
Command:
```bash
python -m pytest tests/test_phase_2_4_k_production_recovery_and_accounting.py -v
```
Results:
- **21 passed, 0 failed, 0 skipped in 11.37s**

### Regression Suites:
1. **Phase 2.4-J Suite:**
   ```bash
   python -m pytest tests/test_phase_2_4_j_production_path_failure_injection.py -v
   ```
   - **18 passed, 0 failed in 11.77s**

2. **Phase 2.4-I Suite:**
   ```bash
   python -m pytest tests/test_phase_2_4_i_adversarial_runtime_recovery.py -v
   ```
   - **15 passed, 0 failed in 11.37s**

3. **Workflow Integration & Acceptance Suites:**
   ```bash
   python -m pytest tests/test_workflow_integration.py -v
   ```
   - **18 passed, 0 failed in 27.58s**
   ```bash
   python -m pytest tests/test_verified_runtime_and_self_healing.py tests/test_production_reliability_acceptance.py -v
   ```
   - **18 passed, 0 failed in 18.55s**

### Full Repository Regression:
Command:
```bash
python -m pytest -q
```
Results:
- **366 passed, 9 skipped, 0 failed in 177.34s (02:57)**

### Breakdown by Category:
- Tests Actually Executed: 366
- Tests Passed: 366
- Tests Failed: 0
- Tests Skipped: 9 (Platform/browser Playwright UI tests requiring live display server)
- Tests Not Executed: 0
- Platform-Dependent Tests: None failed; all Windows MAX_PATH and reserved device checks verified.
- Manual Checks: Protected file hashes verified byte-for-byte.

---

## 12. Changed-File List

1. **`app.py`:**
   - Modified `continue_workflow` delivery persistence exception handler to record failed rollback attempts (`failed_restorations`), exclude them from `cleanup_backups` to preserve recovery backups on disk, and surface rollback errors in the raised `IOError`.
2. **`db_persistence.py`:**
   - Modified `get_operator_session_audit` to inspect session `error_message` for rollback errors, set `rollback_failed: True` and `is_retry_safe: False` in `failure_analysis`, and provide `reconcile_incomplete_rollback` in `permitted_recovery_actions`.
3. **`tests/test_phase_2_4_k_production_recovery_and_accounting.py`:**
   - Authored new 21-test production reliability and accounting validation suite.
4. **`reports/PHASE_2_4_K_PRODUCTION_RECOVERY_AND_ACCOUNTING_REPORT.md`:**
   - Complete Phase 2.4-K engineering audit report.

---

## 13. Remaining Risks and Unresolved Limitations

1. **Remote Provider Idempotency:** If an external LLM provider consumes tokens during an HTTP request that drops before response receipt, the local ledger cannot determine the exact tokens consumed without provider billing API integration. The system safely fails closed by marking the call ambiguous and blocking blind retries.
2. **Manual Reconciliation of Preserved Backups:** When a rollback restoration fails, the `.bak` file is retained on disk and flagged in the operator audit. Automated programmatic compensation to force-overwrite or restore locked files is intentionally avoided to prevent destroying newer user edits.
3. **Multi-Host Shared Storage:** The SQLite WAL concurrency model and atomic file replacement assume local filesystem semantics (`os.replace` atomic guarantees). Distributed filesystems (NFS/SMB) may exhibit non-atomic rename behavior under extreme network partition.

---

## 14. Recommended Next Action

**Next Phase Recommendation:** **Phase 2.5 — Operator Automated Reconciliation & Remote Provider Billing Reconciliation**
- Build the automated reconciliation tooling for sessions flagged with `reconcile_incomplete_rollback` to allow operators to safely inspect and promote retained `.bak` files.
- Integrate asynchronous provider usage polling for ambiguous in-flight runs to retrieve actual provider token counts once connectivity is restored.
