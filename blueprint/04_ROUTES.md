# 04 — Routes (complete map)

Guards: `🔑` = login+role decorator (`@admin_required` /
`@annotator_required`), `🔒` = login-only, `CSRF` = POST + CSRF token.
All protected routes also require `is_active`. Annotator routes are
additionally **terms-gated** (302 → `/annotator/terms` until accepted).

## public (no auth)

| `/privacy` | GET | NDPR data-protection notice (controller/processor split, collection, lawful basis, sharing, security, retention, rights); linked from the footer |
| `/webhooks/paystack` | POST | Paystack Transfers webhook — HMAC-SHA512 signature is the only auth (400 on failure); idempotently settles one pending payment by transfer reference |
| `/client/<token>` | GET | **Client portal** (Phase 7): token-gated project dashboard — progress %, status counts, avg QA score; noindex; raw token redacted from access logs; audited |
| `/client/<token>/export.jsonl` / `.csv` | GET | Client-safe delivery of APPROVED rows (no annotator identity, no gold, no reviewer notes); token-gated, audited |
| Route | Method | Behavior |
|---|---|---|
| `/` | GET | Landing page (brand, how it works) |
| `/manifest.webmanifest` | GET | PWA manifest |
| `/sw.js` | GET | Service worker (correct JS mimetype) |
| `/offline` | GET | Offline fallback page |

## auth (no prefix)
| Route | Method | Guard | Behavior |
|---|---|---|---|
| `/login` | GET, POST | — (POST: CSRF) | Email+password; generic failure message; **lockout after 5 failures** (base 120 s, doubling to max 900 s, per email+IP); redirects admins→`/admin`, annotators→`/annotator`; annotator with unaccepted terms → terms page |
| `/logout` | POST | 🔒 CSRF | Invalidate session |
| `/account/theme` | POST | 🔒 CSRF | Persist light/dark to `users.theme` |
| `/forgot-password` | GET, POST | — | Rate-limited (3 per 15 min/address); always same response (no account enumeration); token = secrets.token_urlsafe(32), **only sha256 stored**; email via SMTP or, unset, **dev fallback: link into app log** |
| `/reset-password/<token>` | GET, POST | — | Token valid ≤ `RESET_TOKEN_MAX_AGE_SECONDS`; on success bump `auth_version`, clear token |

## admin (`/admin`, all 🔑 + CSRF on POST)

Access split (Phase 6): review + quality = admin/**qa**
(`@qa_required`); conflicts/arbitration + audit trail =
admin/**auditor** (`@auditor_required`); everything else (projects,
tasks, payments, team, SFT) stays admin-only. `/` routes each staff
role to their queue (qa → review, auditor → conflicts).
| Route | Methods | Behavior |
|---|---|---|
| `/`, `/dashboard` | GET | KPIs: projects, tasks by status, annotators, pending review, pending payouts |
| `/projects` | GET | List (paginated 20) w/ status, pay, deadline, task counts |
| `/projects/new`, `/projects/<id>/edit` | GET, POST | ProjectForm (name, client, task_type, pay_per_task DecimalField, **required_annotations** 1/2/3, **deadline**, instructions, status) |
| `/projects/<id>/pause` | POST | Toggle active/paused (paused blocks claims + submissions) |
| `/tasks/import` | GET, POST | Paste/import CSV text → **preview**: header validation, row-level errors, limits (IMPORT_MAX_ROWS=2000, 2 MB) |
| `/tasks/import/confirm` | POST | Commit the previewed batch |
| `/tasks/import/template` | GET | Download CSV template |
| `/tasks/upload` | GET, POST | File upload variant of import (same validation) |
| `/tasks` | GET | Filterable list (project, status, annotator; paginated; zero-N+1) |
| `/tasks/assign` | POST | Assign task(s) to annotator (bulk checkboxes or single); fires `task.assigned` notification; **skips consensus siblings** the annotator already holds (blindness guard) with a warning flash |
| `/tasks/<id>/unassign` | POST | Return task to pool (only while assigned) |
| `/review` | GET | Submitted-work queue (filter by project/annotator/score; paginated) |
| `/review/<submission_id>` | GET, POST | Detail: input, output, **gold match indicator**, QA form (score 0–100, notes ≤2000, approve/reject + return-for-revision checkbox); preference tasks render prompt + canonical A/B columns and the choice + justification readably |
| `/review/bulk` | POST | Approve/reject many; optional return-for-revision; per-annotator rejection notifications; **escalation sweep** |
| `/review/conflicts` | GET | Blind-consensus rounds list (filter pending/agreed/conflict/resolved; paginated): per round every sibling's annotator, canonical verdict + justification |
| `/review/conflicts/<id>` | GET, POST | **Arbitration**: sibling verdicts side by side; POST (CSRF) records the BINDING ruling (canonical choice + note + auditor + time), freezes the group as resolved; audited `consensus.resolve`; re-arbitration refused |
| `/sft` | GET | **SFT builder list**: status filter tabs (draft/ready/exported with counts), project filter, 20/page; export panel (POST JSONL) + harvest-by-task-id box |
| `/sft/new` | GET, POST | New pair prefilled from `?task_id=` harvest (consensus ruling > approved submission > gold key); POST (CSRF) validates, enforces the rewrite note, audited `sft.create` |
| `/sft/<id>` | GET, POST | View/edit pair with rendered preview + immutable harvested source side-by-side; exported pairs frozen (POST refused); audited `sft.update` |
| `/sft/<id>/delete` | POST | Draft-only delete (CSRF form); ready/exported refuse; audited `sft.delete` |
| `/projects/<id>/client-tokens` | POST | Issue a client-portal link (label only); raw link flashed ONCE; only sha256 stored; audited |
| `/client-tokens/<id>/deactivate` | POST | Instant revocation (CSRF); audited |
| `/media/<id>` | GET | Serve stored media bytes (admin/qa/auditor); content_type sniffed at upload; Cache-Control private |
| `/projects/<id>/media` | GET, POST | Media library (admin): upload (magic-byte sniff, MEDIA_MAX_BYTES cap, sha256 per-project dedupe), asset grid with paste-in import JSON; audited `media.upload` |
| `/projects/<id>/quiz` | GET, POST | Quiz console (admin): author/edit the project's single quiz (paste format `Q:` / `- ` / `* `), pass score %, attempts; draft-only edits; attempts table + qualified roster + grant-exemption select |
| `/projects/<id>/quiz/activate` | POST | Draft → active: starts the gate; every active annotator notified; audited |
| `/projects/<id>/quiz/retire` | POST | Active → retired: gate lifted, history kept; audited |
| `/projects/<id>/quiz/grant` | POST | Admin-granted exemption (no answer sheet, granted_by recorded); refused when already qualified; audited |
| `/projects/<id>/quiz/revoke` | POST | Remove an annotator's passed/exempt attempts (failed history kept); audited |
| `/sft/export` | POST | Download ready pairs as SFT JSONL (chat `messages` records) and freeze them `exported` in the same transaction; capped by SFT_EXPORT_MAX_ROWS; audited `sft.export`; GET → 405 |
| `/quality` | GET | Per-annotator (last 30 days): tasks done, approved/rejected, **approval rate**, **avg QA score**, **gold accuracy** (output label vs gold, strip+lower); paginated |
| `/payments` | GET | Pending + history payout runs |
| `/payments/generate` | POST | Sweep approved-unpaid submissions into a Payment row per annotator (Decimal math, FX via USD_TO_NGN_RATE) |
| `/payments/<id>/mark-paid` | POST | Mark paid (method, paid_at); fires `payout.paid` notification |
| `/payments/bank-batch` | POST | **Bank CSV**: ref, name, email, bank, **decrypted NUBAN**, account name, NGN + USD amounts, rate, period; audited `payment.export_batch` |
| `/audit` | GET | Audit trail viewer: filter by actor/text, paginated |
| `/users` | GET | Team page: role, status, **bank masked last4 or "Not provided"**, last login, terms state |
| `/users/create` | POST | Create annotator/admin (temp password shown once; account starts terms-unaccepted) |
| `/users/<id>/toggle-active` | POST | Deactivate/reactivate (deactivation bumps target's auth_version) |
| `/users/<id>/reset-password` | POST | Admin reset: temp password, bumps auth_version; audited |
| `/projects/<id>/export` | GET, POST | Export page → file: formats **jsonl / csv / coco / yolo**; scope approved/reviewed/all; gold only if ticked; audited `project.export` |

## annotator (`/annotator`, all 🔑 + terms-gated; POST = CSRF)
| Route | Methods | Behavior |
|---|---|---|
| `/`, `/dashboard` | GET | Active task, stats, **Claim next task** button + daily-cap badge (`n/CLAIM_DAILY_CAP`), project deadlines, pending pool visibility |
| `/tasks` | GET | Alias → history |
| `/task/<id>` | GET, POST | Workspace: instructions + input rendered by task_type (preference: prompt + Response A/B side-by-side cards, per-annotator deterministic position flip, chosen radio + required justification ≥ `PREFERENCE_MIN_JUSTIFICATION` chars); POST submits answer → task `submitted`; stored output is CANONICAL (choice mapped back through the flip, `seen_order` recorded server-side). Media docs (Phase 8): image shows the picture + label radios from the doc's `labels` (free-text fallback when absent) + optional notes → `{kind: image_labeling, label, notes?}`; audio shows the player + required transcript → `{kind: audio_transcription, transcript}`; dangling media refs degrade to the plain-text fallback |
| `/task/<tid>/media/<aid>` | GET | **Task-bound media** (Phase 8): serves the asset bytes only when the task's doc references aid, the asset is in the task's project and the task is assigned to the caller — any mismatch is 404 (ids cannot be probed) |
| `/quiz/<id>` | GET, POST | Take the project's qualification quiz: questions in the annotator's deterministic order (blake2b quiz+annotator), options only — correct flags never leave the server; POST grades server-side, stores the attempt, flashes the score; attempt cap + retake refusal enforced |
| `/claim` | POST | **Self-claim oldest pending task from the oldest active project**; race-safe conditional UPDATE; daily cap; paused-project refusal; audits `task.claim`; flash result; PRG redirect |
| `/notifications` | GET | 50 newest; **viewing marks all read** (single conditional UPDATE) |
| `/history` | GET | Their submissions (status, score, notes; paginated) |
| `/profile` | GET, POST | Name; **password change** (current-password check); masked bank display |
| `/profile/bank-details` | POST | Save (bank, account_type, NUBAN validated 10 digits + checksum-normalized, account name) **or remove**; encrypted via set_bank_account; audited `user.bank_details_update` |
| `/terms` | GET | Terms page (version from TERMS_VERSION) |
| `/terms/accept` | POST | Record version+timestamp; releases the gate |
| `/continue` | POST | Resume-after-terms landing (returns to where they were) |

## Error pages
Branded 403 / 404 / 405 / 500; 500 never leaks internals (logged).

**API blueprint (`/api/v1`, Bearer-key auth; no cookies/CSRF/session path):**

| path | method | purpose |
| --- | --- | --- |
| `/ping` | GET | open uptime probe (no information disclosure) |
| `/me` | GET | identify the key + project binding + rate limit |
| `/projects/<id>` | GET | progress summary (client_portal.progress_summary math) |
| `/projects/<id>/results` | GET | paginated approved, client-safe rows (same client_safe_record contract as the portal export: no annotator identity, no gold, no reviewer notes) |
| `/projects/<id>/tasks` | POST | push a batch of tasks into the project (≤ UPLOAD_BATCH_LIMIT; validation, audit; paused projects 403) |

Auth: `Authorization: Bearer brydl_…`; missing/unknown/revoked keys 401;
foreign/unknown projects uniform 404; per-key minute window 429.

