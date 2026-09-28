# 03 — Data model

Single `models.py`. Seven tables. `utc_now()` supplies every timestamp.
`money(value)` is the Decimal normalizer used at every money boundary.

## users

| Column | Type / constraints | Notes |
|---|---|---|
| id | Integer PK | |
| name | String(120), NOT NULL | |
| email | String(255), NOT NULL, **unique**, indexed | login identity |
| password_hash | String(255), NOT NULL | Werkzeug pbkdf2/scrypt |
| role | String(20), NOT NULL, default "annotator", **CHECK role IN ('admin','qa','auditor','annotator')** (`ck_users_role`) | qa = review queue, auditor = arbitration + audit |
| is_active | Boolean, NOT NULL, default True | deactivation kills sessions (auth_version) |
| created_at | DateTime, NOT NULL, default utc_now | |
| terms_version | String(20), nullable | accepted version; NULL = not accepted |
| terms_accepted_at | DateTime, nullable | |
| theme | String(10), NOT NULL, default "light" | |
| reset_token_hash | String(64), nullable, **indexed** | sha256 of the emailed token |
| reset_token_expires | DateTime, nullable | |
| bank_name | String(120), nullable | |
| bank_account_number | String(512), nullable | **Fernet ciphertext** (security_crypto); legacy plaintext tolerated on read |
| bank_account_last4 | String(4), nullable | masked display without decryption |
| bank_account_name | String(120), nullable | |
| bank_account_type | String(10), nullable | "savings" / "current" |
| phone | String(24), nullable | |
| auth_version | Integer, NOT NULL, default 0 | bump ⇒ all sessions invalidated |

Methods: `set_password` / `check_password`, `accept_terms()`,
`set_bank_account(plain)` (encrypts + stores last4),
`get_bank_account_number()` (decrypts; legacy-plaintext safe).

## projects

| Column | Type / constraints | Notes |
|---|---|---|
| id | Integer PK | |
| name | String(200), NOT NULL | |
| client_name | String(200), NOT NULL | |
| task_type | String(50), NOT NULL | data-driven (sentiment, classification, …); UI renders by this |
| pay_per_task | **Numeric(10,2)**, NOT NULL | USD |
| deadline | Date, nullable | Tier 1 |
| instructions | Text, NOT NULL | shown to annotators |
| rubric_dimensions | Text, nullable | RLHF Phase 5: newline-separated dimension names (≤ MAX_RUBRIC_DIMENSIONS, each ≤ MAX_RUBRIC_DIM_NAME, validated by rubric.py); NULL/empty = rubric off |
| status | String(20), NOT NULL, default "active", CHECK status IN ('active','paused','completed') (`ck_projects_status`), indexed | paused blocks claims/submissions |
| created_by | Integer FK users.id, nullable | |
| created_at | DateTime, NOT NULL, default utc_now | |

## tasks

| Column | Type / constraints | Notes |
|---|---|---|
| id | Integer PK | |
| project_id | Integer FK projects.id, NOT NULL, indexed | |
| input_data | Text, NOT NULL | compact JSON: `{"kind": ..., "text"/"payload": ...}` |
| expected_output | Text, nullable | **gold answer — admin-only, never rendered to annotators** |
| status | String(20), NOT NULL, default "pending", CHECK status IN ('pending','assigned','submitted','approved','rejected') (`ck_tasks_status`), indexed | |
| claimed_at | DateTime, nullable | set ONLY by self-claim |
| assigned_to | Integer FK users.id, nullable, indexed | NULL ⇔ status == 'pending' (**`ck_tasks_assignment` invariant**) |
| created_at | DateTime, NOT NULL, default utc_now | |

## submissions

| Column | Type / constraints | Notes |
|---|---|---|
| id | Integer PK | |
| task_id | Integer FK tasks.id, NOT NULL, indexed | |
| annotator_id | Integer FK users.id, NOT NULL, indexed | pseudonymous id in exports |
| output_data | Text, NOT NULL | compact JSON `{"kind","label","text",…}` |
| qa_score | Float, nullable | 0–100 |
| qa_notes | Text, nullable | reviewer feedback (≤ MAX_QA_NOTES_CHARS) |
| status | String(20), NOT NULL, default "submitted", CHECK status IN ('submitted','approved','rejected') (`ck_submissions_status`), indexed | |
| submitted_at | DateTime, NOT NULL, default utc_now | |
| reviewed_at | DateTime, nullable | |
| payment_id | Integer FK payments.id, nullable, indexed | set when swept into a payout run |

## payments

| Column | Type / constraints | Notes |
|---|---|---|
| id | Integer PK | |
| annotator_id | Integer FK users.id, NOT NULL, indexed | |
| amount | **Numeric(10,2)**, NOT NULL | USD ledger |
| currency | String(3), NOT NULL, default "USD" | |
| period_start / period_end | DateTime, NOT NULL | payout window |
| tasks_count | Integer, NOT NULL, default 0 | |
| status | String(20), NOT NULL, default "pending", CHECK status IN ('pending','paid') (`ck_payments_status`), indexed | |
| payment_method | String(50), nullable | e.g. "bank_transfer" |
| paid_at | DateTime, nullable | |
| paystack_reference | String(100), nullable, indexed | transfer idempotency reference (Tier 2) |

## audit_logs (append-only)

| Column | Type | Notes |
|---|---|---|
| id | Integer PK | |
| user_id | Integer FK users.id, nullable, indexed | actor |
| actor_name | String(120), NOT NULL, default "system" | denormalized |
| action | String(100), NOT NULL, indexed | e.g. `review.verdict`, `task.claim`, `payment.export_batch`, `project.export`, `user.bank_details_update`, `review.bulk`, `payment.paid` |
| entity_type / entity_id | String(50)/Integer, nullable | |
| detail | Text, nullable | JSON; **counts only, never data** (no PII, no answers) |
| ip_address / user_agent | String(45)/String(200), nullable | |
| created_at | DateTime NOT NULL default utc_now, indexed | |

## notifications

| Column | Type | Notes |
|---|---|---|
| id | Integer PK | |
| user_id | Integer FK users.id, NOT NULL, indexed | |
| kind | String(40), NOT NULL, CHECK kind IN ('task.assigned','task.rejected','task.escalated','payout.paid') (`ck_notifications_kind`) | |
| payload | Text, nullable | JSON |
| read_at | DateTime, nullable | |
| created_at | DateTime, NOT NULL default utc_now, indexed | |

Composite indexes: `ix_notifications_user_read (user_id, read_at)`,
`ix_notifications_user_created (user_id, created_at)`.

## consensus_groups (RLHF blind rounds)

| Column | Type / constraints | Notes |
|---|---|---|
| id | Integer PK | |
| project_id | Integer FK projects.id, NOT NULL, indexed | |
| required_count | Integer, NOT NULL, default 2 | siblings needed |
| status | String(20), NOT NULL default "pending", indexed, CHECK status IN ('pending','agreed','conflict','resolved') (`ck_consensus_status`) | resolved is terminal + frozen |
| decided_choice | String(100), nullable | the binding ruling |
| decided_note | Text, nullable | auditor's reasoning |
| resolved_by / resolved_at | FK users / DateTime, nullable | who ruled, when |
| created_at | DateTime, NOT NULL, default utc_now | |

`projects.required_annotations` Integer NOT NULL default 1, CHECK IN
(1,2,3) (`ck_projects_consensus_depth`). `tasks.consensus_group_id`
FK → consensus_groups.id, nullable, indexed (siblings of one blind
round). Depth 1 = the grouping is entirely absent.

## sft_examples (RLHF SFT gold-rewrite pairs)

| Column | Type / constraints | Notes |
|---|---|---|
| id | Integer PK | |
| task_id | Integer FK tasks.id, NOT NULL, indexed | provenance — never dangles |
| project_id | Integer FK projects.id, NOT NULL, indexed | denormalized for list/export filters |
| prompt | Text, NOT NULL | user-side prompt (raw at rest) |
| source_completion | Text, NOT NULL | machine-harvested completion, immutable audit anchor |
| completion | Text, NOT NULL | the editable gold that exports carry |
| system_prompt | Text, nullable | optional system message in the export |
| edit_note | Text, nullable | REQUIRED whenever completion ≠ source_completion |
| status | String(20), NOT NULL default "draft", indexed, CHECK IN ('draft','ready','exported') (`ck_sft_status`) | exported is terminal + frozen |
| source | String(20), NOT NULL default "manual", CHECK IN ('consensus','approved','gold','manual') (`ck_sft_source`) | harvest provenance |
| created_by | Integer FK users.id, NOT NULL | admin who created |
| created_at / updated_at | DateTime, NOT NULL default utc_now | updated_at indexed by list order |

## client_access_tokens (Phase 7, client portal)

| Column | Type / constraints | Notes |
|---|---|---|
| id | Integer PK | |
| project_id | Integer FK projects.id, NOT NULL, indexed | the one project the link exposes |
| label | String(120), NOT NULL | who the link is for |
| token_hash | String(64), NOT NULL, UNIQUE, indexed | sha256 of the raw token — raw shown once, never stored |
| is_active | Boolean, NOT NULL default true | instant revocation |
| created_by | Integer FK users.id, NOT NULL | issuing admin |
| created_at / expires_at / last_used_at / last_used_ip | DateTime/DateTime/DateTime/String(64), nullable except created_at | usage trail |

## media_assets (Phase 8, media workspaces)

| column | type | notes |
| --- | --- | --- |
| id | PK | |
| project_id | FK projects.id, idx | owning project |
| kind | String(10) | ck: image \| audio |
| content_type | String(100) | sniffed allowlist value, served verbatim |
| filename | String(255) | original upload name (display only) |
| byte_size | Integer | ck > 0 |
| sha256 | String(64) | upload digest |
| data | LargeBinary | **bytes in-row (documented deviation)** — $0, inside pg_dump, survives redeploys; object-store (R2) later is additive |
| uploaded_by | FK users.id | admin uploader |
| created_at | DateTime UTC | |

Unique `ix_media_assets_project_sha (project_id, sha256)` — identical
files reuse the existing row per project. Tasks reference an asset via
their input document (`{"kind": "image_labeling"|"audio_transcription",
"media_id": N, "prompt"?, "labels"?}`); SVG is never accepted
(magic-byte sniffing, not extensions).

## qualification_quizzes (Phase 9, per-project gate)

| column | type | notes |
| --- | --- | --- |
| id | PK | |
| project_id | FK projects.id, **UNIQUE** | one quiz per project |
| title | String(200) | |
| questions_json | Text | canonical payload (quizzes.py owns parse/grade); correct flags never rendered |
| pass_score | Integer | ck 1-100 percent |
| max_attempts | Integer | ck 1-20 (default 3) |
| status | String(20), idx | ck draft \| active \| retired; active = gating |
| created_by | FK users.id | |
| created_at / updated_at | DateTime UTC | |

Index `ix_qualification_quizzes_project_status (project_id, status)`.
Lifecycle: draft (editable, not gating) → active (gates claim,
assignment, task page) → retired (gate lifted, history kept,
re-draftable).

## quiz_attempts (Phase 9)

| column | type | notes |
| --- | --- | --- |
| id | PK | |
| quiz_id | FK qualification_quizzes.id, idx | |
| annotator_id | FK users.id, idx | |
| status | String(20) | ck passed \| failed \| exempt |
| correct_count / total_count / score_percent | Integer, nullable | exempt rows: NULL |
| answers_json | Text, nullable | canonical {"q_<i>": <option>}; exempt rows: NULL |
| granted_by | FK users.id, nullable | admin who vouched an exemption |
| created_at | DateTime UTC, idx | |

Index `ix_quiz_attempts_annotator_quiz (annotator_id, quiz_id, status)`
— gate check + attempts counter in one seek.

## api_tokens (Phase 10, machine API)

| column | type | notes |
| --- | --- | --- |
| id | PK | |
| project_id | FK projects.id, idx | scoped exactly one project (foreign keys 404) |
| label | String(120) | human-readable |
| key_hash | String(64), **unique**, idx | sha256 only; raw key never stored |
| key_prefix | String(16) | `brydl_xxxx…` for the admin console |
| created_by | FK users.id | |
| created_at / last_used_at / revoked_at | DateTime UTC | throttled usage accounting; revoked keys 401 |
| request_count | Integer | |

Index `ix_api_tokens_project_id`. Key lifecycle: issue (raw shown once)
→ active → revoke. No expiry column: revocation is explicit and
instant.

## background_jobs (Phase 10, DB-backed queue)

| column | type | notes |
| --- | --- | --- |
| id | PK | |
| kind | String(50), idx | runner key: email.deliver, paystack.reconcile |
| payload | Text, nullable | small JSON; identifiers only — NO secrets, bank data, raw tokens |
| status | String(20), idx | queued → running → done | failed |
| attempts | Integer | starts 1 |
| max_attempts | Integer | default 5, capped 20 (ck) |
| run_after | DateTime UTC | next-attempt anchor (exponential backoff on failure) |
| last_error | Text, nullable | first 500 chars |
| created_at / updated_at | DateTime UTC | |

Composite index `(status, run_after)` is the poller's single seek.
Status ck covers queued/running/done/failed. Claiming is a conditional
UPDATE (queued→running) so web-embedded and dedicated runners share
the queue safely; boot healing requeues rows left 'running' by a
killed process. No new Redis, no new database, no daemon outside the
web service (a dedicated worker.py entrypoint is provided for future
scale, optional).

## payments.created_at (Phase 10)

Backfilled from COALESCE(paid_at, period_end) for existing rows —
used by the Paystack reconciliation sweep's age cutoff.

## Migration chain (17, linear, Alembic)

1. `8bc840d0317e` initial schema
2. `4af7971b8512` password-reset token columns
3. `041200d1ec79` annotator payout bank details
4. `f7353e97b8cc` index on users.reset_token_hash
5. `736be0df0ee9` users.auth_version (session invalidation)
6. `aadccd5f13e2` payout account_type + phone
7. `c3f81b92ad04` **Tier 1**: projects.deadline, tasks.claimed_at, notifications table + indexes/check
8. `e91c73a40b57` bank ciphertext: widen bank_account_number to String(512), add bank_account_last4
9. `b2f4c9d61a83` consensus: consensus_groups table, tasks.consensus_group_id FK + index, projects.required_annotations + check
10. `c4e8a1f37d52` SFT builder: sft_examples table + status/source checks + task/project/status indexes
11. `d7f2b9e46a10` rubric: projects.rubric_dimensions (nullable Text)
12. `e5c7d2f84b19` roles: ck_users_role widened to admin/qa/auditor/annotator
13. `f1a94c3b7e20` paystack: users.paystack_recipient_code, payments.paystack_reference + index
14. `a9b3d7c15e40` client portal: client_access_tokens (hash-only tokens)
15. `b8e40c92f7d1` media_assets (Phase 8): bytes in-row, unique project+sha256
16. `e2f6a9c4d8b1` qualification quizzes (Phase 9): qualification_quizzes + quiz_attempts
17. `c7d21b8a45e9` api + jobs (Phase 10): api_tokens + background_jobs, payments.created_at backfill

Rules: SQLite uses `batch_alter_table`; fresh chain upgrades cleanly;
`flask db check` reports no drift against `models.py`.
