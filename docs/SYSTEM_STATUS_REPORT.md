# BRYDL PLATFORM — COMPREHENSIVE SYSTEM STATUS & ARCHITECTURE REPORT (v14-redlab)

**Version:** v14-redlab (Production-Ready Pilot Release)  
**Date:** 2026-10-11  
**Architecture:** Unified Dual-Engine (Flask WSGI + FastAPI ASGI via Root ASGI Dispatcher)  
**Test Suite Status:** 52 / 52 Passing Suites (0 Failures, 0 Skips, 0 Xfails)  
**Security & Audit Status:** Pilot Approved by Independent Hostile Security Review  

---

## 1. PLATFORM OVERVIEW & MISSION

**Brydl** is an enterprise AI data annotation, RLHF evaluation, and Dynamic Red-Teaming platform specifically optimized for African languages (Hausa, Yoruba, Igbo, Swahili, etc.) and global adversarial LLM safety evaluation. 

In version **v14-redlab**, Brydl introduces **RedLab (Dynamic Red-Teaming as a Service)** — enabling human annotators to engage in live multi-turn adversarial red-teaming against client-hosted LLM endpoints to discover vulnerabilities (prompt injections, jailbreaks, canary exfiltration, tool abuse, and regional language policy evasions) with confirmed breach bounties and automated anonymized exports.

---

## 2. DUAL-ENGINE SYSTEM ARCHITECTURE

Brydl operates as a unified single-service web application composed behind a single host/port on Render:

```
                                    CLIENT / BROWSER
                                           │
                                           ▼
                       ┌────────────────────────────────────────┐
                       │     Root ASGI Entrypoint (asgi.py)      │
                       │     uvicorn.workers.UvicornWorker      │
                       └───────────────────┬────────────────────┘
                                           │
                  ┌────────────────────────┴────────────────────────┐
                  │                                                 │
          Path: /redlab/*                                     Path: /*
                  ▼                                                 ▼
  ┌───────────────────────────────┐                 ┌───────────────────────────────┐
  │      FastAPI Sub-App          │                 │          Flask Core           │
  │    (Async RedLab Engine)      │                 │    (Sync Application Core)    │
  ├───────────────────────────────┤                 ├───────────────────────────────┤
  │ • /redlab/healthz             │                 │ • /login, /logout, /auth      │
  │ • /redlab/v1/sessions/:id/turn│                 │ • /annotator/* (Task Workspace│
  │ • /redlab/v1/sessions/:id/flag│                 │ • /admin/* (Admin Dashboard)  │
  │ • /redlab/v1/sessions/:id/con │                 │ • /qa/* (Review & Arbitration)│
  │ • SSRF & DNS Pinning Guard    │                 │ • /api/v1/* (Machine Exports) │
  │ • In-Memory Fernet Decryption │                 │ • Exact Decimal Money Ledger  │
  │ • Chunked 2MB Streaming Cap   │                 │ • Paystack Payment Gateway    │
  │ • Sliding-Window Rate Limiter │                 │ • Background Job Queue Runner │
  └───────────────┬───────────────┘                 └───────────────┬───────────────┘
                  │                                                 │
                  └───────────────────────┬─────────────────────────┘
                                          │
                  ┌───────────────────────┴─────────────────────────┐
                  │          Integration Bridge Layer               │
                  │         (redlab/integration.py)                 │
                  └───────────────────────┬─────────────────────────┘
                                          │
                                          ▼
                  ┌─────────────────────────────────────────────────┐
                  │          Database (PostgreSQL / SQLite)         │
                  │  • Schema `public`: Users, Tasks, Payments      │
                  │  • Schema `redlab`: Targets, Sessions, Messages │
                  └─────────────────────────────────────────────────┘
```

---

## 3. COMPLETE FUNCTIONAL INVENTORY: WHAT WE HAVE TODAY

### 3.1 Authentication, Security & Role Boundaries
* **Session Security:** Flask-Login session cookies signed via `URLSafeTimedSerializer` with SHA-1 HMAC signatures and strict HttpOnly/SameSite cookies.
* **CSRF Defense:** Flask-WTF CSRF protection enforced across all POST/PUT/DELETE forms and RedLab JSON API headers (`X-CSRFToken`).
* **5 Distinct User Roles:**
  1. `admin`: Global system oversight, billing, target LLM registration, user management, and quota limits.
  2. `pm`: Project creation, budget allocation, guideline uploads, and batch task creation.
  3. `qa`: Submission review queue, breach verification, quality rubric scoring, and payout approval.
  4. `annotator`: Task workspace, Hausa character toolbar, interactive red-team chat, and payout ledger.
  5. `client`: Read-only project metrics, progress reporting, and dataset exports.
* **Content Security Policy (CSP):** Strict CSP headers with zero inline JavaScript for chat (`static/js/redteam_chat.js` loaded via external script).

### 3.2 Supported Annotation & Evaluation Workflows (6 Task Types)
1. **Text Classification & Sentiment:** Multi-class classification with customizable categories.
2. **SFT (Supervised Fine-Tuning) Generation:** African-language prompt and response authoring.
3. **RLHF Preference Ranking:** Side-by-side model comparison with criterion ranking (Helpfulness, Truthfulness, Safety).
4. **Image & Audio Labeling:** Multimodal asset classification and transcription.
5. **Gold Standard Consensus:** Hidden ground-truth tasks ensuring automated annotator quality scoring.
6. **Dynamic Red-Teaming (`dynamic_red_team`):** Multi-turn adversarial chat against live target models.

### 3.3 Dynamic Red-Teaming Engine (RedLab v14)
* **Async Turn API (`POST /redlab/v1/sessions/:id/turn`):** Non-blocking chat endpoint supporting up to 40 sequential turns per session and up to 4,000 characters per message.
* **Idempotent Dispatch:** Client-supplied `client_turn_id` prevents duplicate model invocations or double-writes on network retries.
* **MultiFernet Zero-Downtime Cryptography (`redlab/crypto.py`):** Client API keys encrypted at rest using AES-128-CBC + HMAC-SHA256 with rotation support (`REDLAB_FERNET_KEYS`).
* **SSRF Guard & DNS TOCTOU Pinning (`redlab/ssrf.py` & `redlab/connector.py`):**
  - Full blocklist for RFC 1918, CGNAT (`100.64.0.0/10`), loopback, cloud metadata (`169.254.169.254`), `0.0.0.0/8`, limited broadcast, multicast, and IPv4-mapped IPv6 (`::ffff:0:0/96`).
  - Pre-resolves target IPs and pins outbound TCP sockets via `PinnedIPTransport` and `PinnedNetworkBackend`.
* **Streaming Payload Limit & Timeouts:** Streaming response chunk verification aborting immediately if payloads exceed 2 MB without memory buffering; 15-second read timeout and 5-second connect timeout.
* **Curated Seed Starter Prompts (`redlab/seeds.py`):** 12 curated attack templates covering Prompt Injection, Jailbreak/DAN, Canary Exfiltration, Tool Abuse, Hausa Evasion, Yoruba Evasion, Swahili Evasion, and French Evasion.

### 3.4 QA Review, Arbitration & Dual-Component Financial Ledger
* **Dual-Component Payout Calculation:**
  $$\text{Payout} = \text{Base Pay} + (\text{Confirmed Breaches} \times \text{Breach Bounty})$$
* **Exact Decimal Arithmetic:** All currency calculations use Python `Decimal` and SQL `NUMERIC(10,2)` (zero floating-point math).
* **Payment Gateway & Batch Settlement:** Paystack transfer recipient integration and automated batch payment generation.
* **QA Review Queue:** Reviewers inspect transcripts, toggle breach flags, verify canaries, and approve/reject submissions with dispute arbitration.

### 3.5 Delivery & Machine Export APIs
* **OpenAI JSONL Format (`GET /api/v1/projects/:id/export?format=openai`):** Formats approved transcripts into `{"messages": [...], "metadata": {"submission_id": "...", "breach_turns": [...], "model": "...", "qa_reviewers": ["reviewer-xxxx"]}}`.
* **ShareGPT JSONL Format (`GET /api/v1/projects/:id/export?format=sharegpt`):** Formats transcripts into `{"conversations": [{"from": "human"|"gpt", "value": "..."}]}`.
* **QA Reviewer Anonymization:** Reviewers deduplicated and masked with ephemeral per-export HMAC tokens (`f"reviewer-{hmac(...)}"`) preventing staff identification and cross-export correlation.

### 3.6 Ops, Telemetry & Compliance
* **Database-Backed Job Queue (`jobs.py`):** Durable background worker queue executing email notifications, Paystack reconciliations, and retention sweeps.
* **Sentry Telemetry with Credential Scrubbing (`app.py`):** Multi-framework error tracking (Flask + Starlette + FastAPI) with automatic filtering of `Authorization`, `Cookie`, `X-CSRFToken`, `X-API-Key`, and extra variables.
* **Transcript Retention & DB Cascades (`redlab/retention.py`):** Automated retention pruning (`prune_expired_transcripts(days=90)`) with database-level `ON DELETE CASCADE`.

---

## 4. WHAT IS DEFERRED / NOT IN v14 (ROADMAP FOR v15 & CLIENT #2)

| Category | Item | Current v14 Status | Planned Next Milestone |
| :--- | :--- | :--- | :--- |
| **Scaling** | Redis-Backed Rate Limiting | Single-worker in-memory sliding window ($N=1$). | **Pilot Week 2 / Client #2** |
| **Scaling** | Global Target Concurrency Cap | Per-session rate limits (15 turns/min). | **Client #2** |
| **Features** | Conversation Tree Forking | Linear chat history (up to 40 turns). | **v15 Feature Release** |
| **Features** | Token-by-Token SSE Streaming | Atomic turn request/response. | **v15 Feature Release** |
| **Features** | Multi-Model Arena (Side-by-Side) | 1 target model per session. | **v15 Feature Release** |
| **Features** | Automated LLM-as-a-Judge | Human QA reviewer verification. | **v15 Feature Release** |
| **Automation**| Live Vendor Dollar Billing Sync | Manual checks + internal session quotas. | **v15 Roadmap** |
| **Security**  | External Accredited Pen-Test | Hostile internal audit & mutation tests. | **Pre-Open Production** |

---

## 5. TEST SUITE & VERIFICATION HEALTH (52 / 52 SUITES)

```text
Brydl Dashboard test battery (root: /home/user/brydl-dashboard)
Python: Python 3.13.16
----------------------------------------------------------------
PASS  test_admin_dash.py           ALL ADMIN DASH TESTS PASSED
PASS  test_annotator.py            ALL ANNOTATOR TESTS PASSED
PASS  test_annotator_experience.py ALL ANNOTATOR EXPERIENCE TESTS PASSED
PASS  test_api.py                  ALL API TESTS PASSED
PASS  test_audit.py                ALL AUDIT TESTS PASSED
PASS  test_auditor_arbitration.py  ALL AUDITOR ARBITRATION TESTS PASSED
PASS  test_brand.py                ALL BRAND TESTS PASSED
PASS  test_char_bar.py             ALL HAUSA CHAR-BAR TESTS PASSED
PASS  test_client_portal.py        ALL CLIENT PORTAL TESTS PASSED
PASS  test_consensus.py            ALL CONSENSUS TESTS PASSED
PASS  test_deploy.py               ALL DEPLOY TESTS PASSED
PASS  test_deploy_smoke.py         ALL DEPLOY + SENTRY SMOKE TESTS PASSED
PASS  test_email_optin.py          ALL SMTP OPT-IN TESTS PASSED
PASS  test_export.py               ALL EXPORT TESTS PASSED
PASS  test_hardening.py            ALL HARDENING TESTS PASSED
PASS  test_import.py               ALL CSV IMPORT TESTS PASSED
PASS  test_jobs.py                 ALL JOB TESTS PASSED
PASS  test_media.py                ALL MEDIA TESTS PASSED
PASS  test_migrations.py           ALL MIGRATION TESTS PASSED
PASS  test_ngn_display.py          ALL NGN DISPLAY TESTS PASSED
PASS  test_offline.py              ALL OFFLINE PWA TESTS PASSED
PASS  test_password_reset.py       ALL PASSWORD RESET TESTS PASSED
PASS  test_payments.py             ALL PAYMENTS TESTS PASSED
PASS  test_paystack.py             ALL PAYSTACK TESTS PASSED
PASS  test_phase10_security_idor.py ALL 16 SECURITY, IDOR & CONCURRENCY CHECKS PASSED
PASS  test_phase1_asgi.py          ALL 31 Phase 1 ASGI CHECKS PASSED
PASS  test_phase2_models.py        ALL 48 Phase 2 MODEL/CRYPTO CHECKS PASSED
PASS  test_phase3_connector.py     ALL Phase 3 CONNECTOR & SSRF CHECKS PASSED
PASS  test_phase4_turn_api.py      ALL PHASE 4 TURN-API CHECKS PASSED
PASS  test_phase5_events.py        ALL PHASE 5 EVENT CHECKS PASSED
PASS  test_phase6_annotator_ui.py  ALL PHASE 6 ANNOTATOR-UI CHECKS PASSED
PASS  test_phase7_qa_payouts.py    ALL PHASE 7 QA-PAYOUT CHECKS PASSED (36 checks)
PASS  test_phase8_exports_admin.py ALL PHASE 8 EXPORTS & ADMIN QUOTA CHECKS PASSED (43 checks)
PASS  test_phase9_hardening.py     ALL PHASE 9 HARDENING & LINT CHECKS PASSED (14 checks)
PASS  test_pm.py                   ALL PM TESTS PASSED
PASS  test_pm_quiz.py              ALL PM QUIZ PREVIEW/ROSTER TESTS PASSED
PASS  test_qualification.py        ALL QUALIFICATION TESTS PASSED
PASS  test_rendering.py            ALL RENDERING TESTS PASSED
PASS  test_review.py               ALL QA REVIEW TESTS PASSED
PASS  test_rlhf_preference.py      ALL RLHF PREFERENCE TESTS PASSED
PASS  test_roles.py                ALL ROLES TESTS PASSED
PASS  test_rubric.py               ALL RUBRIC TESTS PASSED
PASS  test_scalability.py          ALL SCALABILITY TESTS PASSED
PASS  test_settings.py             ALL SETTINGS TESTS PASSED
PASS  test_sft.py                  ALL SFT TESTS PASSED
PASS  test_staff.py                ALL STAFF TESTS PASSED
PASS  test_stress60.py             ALL STRESS + FAULT-ISOLATION TESTS PASSED
PASS  test_system_check.py         ALL SYSTEM CHECKS PASSED
PASS  test_system_fixes.py         ALL SYSTEM FIXES TESTS PASSED
PASS  test_tasks.py                ALL TASK MANAGEMENT TESTS PASSED
PASS  test_tier1.py                ALL TIER 1 TESTS PASSED
PASS  test_user_mgmt.py            ALL USER MANAGEMENT TESTS PASSED
----------------------------------------------------------------
RESULT: ALL SUITES PASSED (52 / 52 clean)
```

---

## 6. PILOT CUTOVER STATUS

* **Target Pilot Client:** Single enterprise LLM evaluation client.
* **Pre-Launch Guardrails:**
  - Deployment worker count: `--workers 1` verified in `render.yaml` and `Procfile`.
  - Client monthly budget limit: **$20.00** hard limit set on vendor console.
  - Sentry Alert Rule: `RedLab-500-Error-Spike` active.
  - Rollback Tag: `v13-stable` verified.
  - Kill-Switch: `REDLAB_ENABLED=false` provides instant cutover.
