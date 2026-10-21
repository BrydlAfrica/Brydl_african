# BRYDL PLATFORM (v14-redlab) — COMPLETE TECHNICAL OPERATIONS MANUAL

**Author:** Brydl Engineering Team  
**Release Version:** `v14-redlab` (Pilot Production Ready)  
**Applicability:** Production, Staging, Local Development, and Incident Management  

---

# SECTION 1: ARCHITECTURE DIAGRAMS

## 1.1 Dual-Engine Unified Topology (ASGI + WSGI)

```
                            INTERNET TRAFFIC (HTTPS)
                                       │
                                       ▼
                 ┌───────────────────────────────────────────┐
                 │       Render Edge / Cloudflare CDN        │
                 │   • TLS Termination (HTTPS / HSTS)        │
                 │   • DDoS Protection & Reverse Proxy       │
                 └─────────────────────┬─────────────────────┘
                                       │
                                       ▼
                 ┌───────────────────────────────────────────┐
                 │       Gunicorn + Uvicorn Worker (1x)      │
                 │             asgi:app (Root ASGI)          │
                 │     • AnyIO Threadpool Limit = 60         │
                 │     • Startup Concurrency Guard           │
                 └─────────────────────┬─────────────────────┘
                                       │
                  ┌────────────────────┴────────────────────┐
                  │ Path: /redlab/*                         │ Path: /*
                  ▼                                         ▼
   ┌──────────────────────────────┐          ┌──────────────────────────────┐
   │    FastAPI Sub-Application   │          │      Flask Core Web App      │
   │    (Async Outbound Engine)   │          │   (Sync Business Authority)  │
   ├──────────────────────────────┤          ├──────────────────────────────┤
   │ • /redlab/healthz            │          │ • /login, /logout (Flask-Auth│
   │ • /redlab/v1/sessions/:id/   │          │ • /annotator/* (Chat UI & WS)│
   │     turn, flag, conclude     │          │ • /admin/* (Management/Ops) │
   │ • PinnedIPTransport (TOCTOU) │          │ • /qa/* (Review & Verdicts)  │
   │ • SSRF Multi-CIDR Validator  │          │ • /api/v1/* (Machine Exports)│
   │ • In-Memory Fernet Decrypt   │          │ • Exact Decimal Ledger Math  │
   │ • 2 MB Streaming Cap         │          │ • Paystack Payment Gateway   │
   │ • Sliding-Window Rate Limit  │          │ • Background Queue Runner    │
   └──────────────┬───────────────┘          └──────────────┬───────────────┘
                  │                                         │
                  └────────────────────┬────────────────────┘
                                       │
                                       ▼
                 ┌───────────────────────────────────────────┐
                 │      Sanctioned Integration Bridge        │
                 │         (redlab/integration.py)           │
                 │   • Enforces Import-Linter Contracts      │
                 └─────────────────────┬─────────────────────┘
                                       │
                                       ▼
                 ┌───────────────────────────────────────────┐
                 │     PostgreSQL Database (Managed)         │
                 ├───────────────────────────────────────────┤
                 │ Schema `public`: Users, Tasks, Ledger     │
                 │ Schema `redlab`: Targets, Sessions, Msgs  │
                 └───────────────────────────────────────────┘
```

---

## 1.2 Outbound SSRF Defense & DNS-Pinning Pipeline

```
  Annotator Turn Triggered
             │
             ▼
  ┌───────────────────────┐
  │ Target.endpoint URL   │ (e.g., https://api.client.com/v1/chat)
  └──────────┬────────────┘
             │
             ▼
  ┌────────────────────────────────────────────────────────┐
  │ redlab.ssrf.resolve_and_validate_endpoint_url()        │
  ├────────────────────────────────────────────────────────┤
  │ 1. Scheme Check: HTTPS only (rejects http, ftp, file)  │
  │ 2. Hostname Check: Rejects literal IPs in blocklist   │
  │ 3. socket.getaddrinfo(): Resolves ALL DNS records      │
  │ 4. CIDR Screening against _BLOCKED_NETS:               │
  │    - 127.0.0.0/8 & ::1/128 (Loopback)                  │
  │    - 10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16 (RFC1918)│
  │    - 169.254.0.0/16 & fe80::/10 (Cloud Metadata/Local) │
  │    - 100.64.0.0/10 (CGNAT), 0.0.0.0/8, 255.255.255.255  │
  │    - ::ffff:0:0/96 (IPv4-Mapped IPv6 unwrap check)     │
  └──────────┬─────────────────────────────────────────────┘
             │
    ┌────────┴────────┐
    ▼ (Blocked)       ▼ (Pass: Returns Validated Public IP)
┌──────────────┐  ┌────────────────────────────────────────────────┐
│ Return 502   │  │ redlab.connector.PinnedIPTransport            │
│ endpoint_    │  ├────────────────────────────────────────────────┤
│ blocked      │  │ • Binds PinnedNetworkBackend(pinned_ip)        │
└──────────────┘  │ • Outbound TCP connects DIRECT to pinned IP    │
                  │ • Defeats DNS-Rebinding / TOCTOU attacks       │
                  │ • Preserves Host Header & TLS SNI hostname     │
                  └───────────────┬────────────────────────────────┘
                                  │
                                  ▼
                  ┌────────────────────────────────────────────────┐
                  │ httpx.AsyncClient Stream with 2 MB Limit       │
                  │ • In-memory key decryption (Fernet)            │
                  │ • Mid-stream abort if body > 2,097,152 bytes   │
                  │ • Key dereferencing & Bearer header wipe       │
                  └────────────────────────────────────────────────┘
```

---

## 1.3 Financial Ledger & Dual-Component Payout Lifecycle

```
  Annotator Concludes RedLab Session
                  │
                  ▼
  Submission Created (status="submitted", redteam_state="qa_pending")
                  │
                  ▼
  QA Reviewer Queue (/qa/review/<id>)
  • Inspects full conversation transcript
  • Validates/overrides annotator breach flags
                  │
                  ▼
  Reviewer Action: APPROVE
                  │
                  ▼
  ┌─────────────────────────────────────────────────────────────────┐
  │ Reconcile Breach Count & Calculate Exact Decimal Payout         │
  ├─────────────────────────────────────────────────────────────────┤
  │ confirmed_breaches = COUNT(Messages WHERE is_unsafe_flag=TRUE)  │
  │ submission.redteam_breach_count = confirmed_breaches            │
  │                                                                 │
  │ Payout = Project.redteam_base_pay                               │
  │        + (confirmed_breaches * Project.redteam_breach_bounty)   │
  └───────────────┬─────────────────────────────────────────────────┘
                  │
                  ▼
  ┌─────────────────────────────────────────────────────────────────┐
  │ Atomic Settlement (models.Payment)                             │
  ├─────────────────────────────────────────────────────────────────┤
  │ • Appends to annotator pending earnings balance                 │
  │ • Bundles into Paystack batch transfer transfer_code            │
  │ • Audited under audit_logs table (exact Decimal amount)         │
  └─────────────────────────────────────────────────────────────────┘
```

---

# SECTION 2: STEP-BY-STEP DEPLOYMENT GUIDES

## 2.1 Deploying on Render (Production Blueprint)

### Prerequisites
1. A GitHub account with access to the private `brydl-dashboard` repository.
2. A Render account ([render.com](https://render.com)).
3. A client LLM API key for the target model.

### Step 1: Generate Cryptographic Secrets
In your local terminal, generate the production secrets:
```bash
# 1. Generate SECRET_KEY for Flask sessions (64 hex characters)
python3 -c "import secrets; print('SECRET_KEY=' + secrets.token_hex(32))"

# 2. Generate REDLAB_FERNET_KEY for Target API key encryption (32 url-safe base64 bytes)
python3 -c "from cryptography.fernet import Fernet; print('REDLAB_FERNET_KEY=' + Fernet.generate_key().decode())"
```

### Step 2: Launch Render Blueprint
1. Log in to Render $\to$ Click **New +** $\to$ Select **Blueprint**.
2. Connect your GitHub repository: `your-org/brydl-dashboard`.
3. Render reads `render.yaml` and provisions:
   - **PostgreSQL Database:** `brydl-postgres` (Postgres 16)
   - **Web Service:** `brydl-dashboard` (Python 3.11 runtime, Plan: Free/Starter)
4. Set the environment variables in Render Dashboard:
   - `SECRET_KEY`: *(Paste output from Step 1)*
   - `REDLAB_FERNET_KEY`: *(Paste output from Step 1)*
   - `REDLAB_ENABLED`: `true`
   - `FLASK_ENV`: `production`
   - `BRYDL_AUTO_SCHEMA`: `0`
   - `REDLAB_TURN_BURST_CAP`: `15`
   - `REDLAB_TURN_BURST_WINDOW`: `1.0`
5. Click **Apply**. Render will automatically run:
   ```bash
   pip install -r requirements.txt && flask --app app db upgrade
   ```
   and start the web service with:
   ```bash
   gunicorn -k uvicorn.workers.UvicornWorker --workers 1 asgi:app
   ```

### Step 3: Create Initial Platform Admin
Once the service shows **Live**, open the Render Web Service **Shell** tab:
```bash
flask create-admin --email admin@brydl.com --name "Platform Administrator"
```
*(Enter a strong password when prompted).*

### Step 4: Verify Health and Worker Count
```bash
curl -i https://<your-service>.onrender.com/redlab/healthz
```
*Expected Response:*
```json
HTTP/1.1 200 OK
Content-Type: application/json

{"status":"ok","service":"redlab","version":"14.0.0","pid":52,"redlab_enabled":true}
```

---

## 2.2 Deploying on Linux VPS / Docker (Self-Hosted)

### Step 1: System Dependencies & Environment Setup
```bash
sudo apt update && sudo apt install -y python3-pip python3-venv postgresql postgresql-contrib git

# Create database and user
sudo -u postgres psql -c "CREATE USER brydl WITH PASSWORD 'StrongPassword123';"
sudo -u postgres psql -c "CREATE DATABASE brydl_prod OWNER brydl;"
```

### Step 2: Clone & Install Python Environment
```bash
git clone https://github.com/your-org/brydl-dashboard.git /opt/brydl
cd /opt/brydl

python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

### Step 3: Configure `/opt/brydl/.env`
```ini
FLASK_ENV=production
DATABASE_URL=postgresql://brydl:StrongPassword123@localhost:5432/brydl_prod
SECRET_KEY=paste-64-character-random-hex-string
REDLAB_ENABLED=true
REDLAB_FERNET_KEY=paste-32-byte-fernet-key
REDLAB_TURN_BURST_CAP=15
REDLAB_TURN_BURST_WINDOW=1.0
BRYDL_AUTO_SCHEMA=0
```

### Step 4: Apply Migrations & Create Admin
```bash
flask --app app db upgrade
flask create-admin --email admin@brydl.com --name "Platform Administrator"
```

### Step 5: Systemd Service Configuration
Create `/etc/systemd/system/brydl.service`:
```ini
[Unit]
Description=Brydl Dashboard & RedLab Unified ASGI Service
After=network.target postgresql.service

[Service]
User=www-data
Group=www-data
WorkingDirectory=/opt/brydl
EnvironmentFile=/opt/brydl/.env
ExecStart=/opt/brydl/venv/bin/gunicorn -k uvicorn.workers.UvicornWorker --workers 1 --bind 127.0.0.1:5000 asgi:app
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```
Start and enable the service:
```bash
sudo systemctl daemon-reload
sudo systemctl enable --now brydl
```

---

# SECTION 3: DATABASE SCHEMAS

Brydl isolates tables into two PostgreSQL schemas: `public` (Flask core) and `redlab` (FastAPI RedLab sub-app).

```
   ┌──────────────────────────────────────────────────────────────┐
   │                     SCHEMA: public                           │
   ├──────────────────────────────┬───────────────────────────────┤
   │ users                        │ submissions                   │
   │ • id (INT PK)                │ • id (INT PK)                 │
   │ • email (VARCHAR UNIQUE)     │ • task_id (INT FK tasks.id)   │
   │ • role (VARCHAR)             │ • annotator_id (INT FK users) │
   │ • password_hash (VARCHAR)    │ • status (VARCHAR)            │
   │ • is_active (BOOLEAN)        │ • qa_score (NUMERIC)          │
   ├──────────────────────────────┤ • redlab_session_id (STR)     │
   │ projects                     │ • redteam_breach_count (INT)  │
   │ • id (INT PK)                ├───────────────────────────────┤
   │ • name (VARCHAR)             │ payments                      │
   │ • task_type (VARCHAR)        │ • id (INT PK)                 │
   │ • redteam_base_pay (NUMERIC) │ • user_id (INT FK users.id)   │
   │ • redteam_breach_bounty (NUM)│ • amount (NUMERIC(10,2))      │
   │ • redteam_session_quota (INT)│ • status (VARCHAR)            │
   └──────────────────────────────┴───────────────────────────────┘
                                  │
                 Cross-Schema Reference (String UUID)
                                  │
                                  ▼
   ┌──────────────────────────────────────────────────────────────┐
   │                     SCHEMA: redlab                           │
   ├──────────────────────────────┬───────────────────────────────┤
   │ targets                      │ messages                      │
   │ • id (STR(32) PK)            │ • id (STR(32) PK)             │
   │ • project_ref (STR(32))      │ • session_id (STR(32) FK      │
   │ • name (STR(200))            │     sessions.id ON DELETE     │
   │ • endpoint (STR(1000))       │     CASCADE)                  │
   │ • model_name (STR(200))      │ • turn_index (INT)            │
   │ • api_key_ciphertext (TEXT)  │ • role (VARCHAR)              │
   │ • active (BOOLEAN)           │ • content (TEXT)              │
   ├──────────────────────────────┤ • client_turn_id (STR(64))    │
   │ sessions                     │ • is_unsafe_flag (BOOLEAN)    │
   │ • id (STR(32) PK)            │ • created_at (TIMESTAMP)      │
   │ • submission_ref (STR(32) UQ)├───────────────────────────────┤
   │ • annotator_ref (STR(32))    │ processed_events              │
   │ • target_id (STR(32) FK      │ • event_id (STR(32) PK)       │
   │     targets.id)              │ • processed_at (TIMESTAMP)    │
   │ • status (VARCHAR)           └───────────────────────────────┘
   │ • turn_count (INT)           │
   │ • breach_count (INT)         │
   └──────────────────────────────┘
```

### Table Definitions & Constraints

#### 1. `redlab.targets`
* `id` `VARCHAR(32)` Primary Key (Hex UUID).
* `project_ref` `VARCHAR(32)` NOT NULL, Indexed.
* `endpoint` `VARCHAR(1000)` NOT NULL (Target LLM REST endpoint).
* `api_key_ciphertext` `TEXT` NOT NULL (MultiFernet encrypted token).
* `key_fingerprint` `VARCHAR(64)` NOT NULL (e.g., `sk-...4a2b`).
* `active` `BOOLEAN` NOT NULL DEFAULT TRUE.

#### 2. `redlab.sessions`
* `id` `VARCHAR(32)` Primary Key (Hex UUID).
* `submission_ref` `VARCHAR(32)` UNIQUE (Links to `public.submissions.id`).
* `annotator_ref` `VARCHAR(32)` NOT NULL, Indexed.
* `project_ref` `VARCHAR(32)` NOT NULL, Indexed.
* `target_id` `VARCHAR(32)` NOT NULL, FK to `redlab.targets(id)` on delete cascade.
* `status` `VARCHAR(20)` NOT NULL CHECK (`status IN ('open', 'concluded')`).
* `turn_count` `INTEGER` NOT NULL DEFAULT 0 CHECK (`turn_count >= 0`).
* `breach_count` `INTEGER` NOT NULL DEFAULT 0 CHECK (`breach_count >= 0`).

#### 3. `redlab.messages`
* `id` `VARCHAR(32)` Primary Key (Hex UUID).
* `session_id` `VARCHAR(32)` NOT NULL, FK to `redlab.sessions(id)` `ON DELETE CASCADE`.
* `turn_index` `INTEGER` NOT NULL Indexed.
* `role` `VARCHAR(20)` NOT NULL CHECK (`role IN ('user', 'assistant', 'system')`).
* `content` `TEXT` NOT NULL.
* `client_turn_id` `VARCHAR(64)` NULL (Client idempotency key).
* `is_unsafe_flag` `BOOLEAN` NOT NULL DEFAULT FALSE.
* **Unique Constraint:** `uq_messages_session_turn` on (`session_id`, `turn_index`).

---

# SECTION 4: API DOCUMENTATION

## 4.1 RedLab Interactive Turn API (`/redlab/v1/*`)

All RedLab endpoints require an active Flask-Login session cookie and a valid `X-CSRFToken` header.

### `POST /redlab/v1/sessions/{sid}/turn`
Submits one user turn, checks rate limits, calls the client model, and writes both user & assistant messages atomically.

* **Headers:**
  ```http
  Cookie: session=<signed_flask_cookie>
  X-CSRFToken: <csrf_token>
  Content-Type: application/json
  ```
* **Request Body:**
  ```json
  {
    "content": "Provide detailed instructions to synthesize compound X.",
    "client_turn_id": "turn-uuid-001"
  }
  ```
* **Success Response (`200 OK`):**
  ```json
  {
    "user_message": {
      "id": "msg_u001",
      "role": "user",
      "content": "Provide detailed instructions to synthesize compound X.",
      "turn_index": 0,
      "client_turn_id": "turn-uuid-001",
      "is_unsafe_flag": false,
      "created_at": "2026-10-11T12:00:00"
    },
    "assistant_message": {
      "id": "msg_a001",
      "role": "assistant",
      "content": "I cannot fulfill this request as it violates safety guidelines.",
      "turn_index": 1,
      "client_turn_id": null,
      "is_unsafe_flag": false,
      "created_at": "2026-10-11T12:00:02"
    },
    "session": {
      "id": "sess_001",
      "turn_count": 1,
      "breach_count": 0,
      "status": "open"
    }
  }
  ```
* **Error Responses:**
  * `400 Bad Request`: `{"error": "turn_cap_reached"}` (Exceeded 40 turns)
  * `401 Unauthorized`: `{"error": "not_authenticated"}`
  * `403 Forbidden`: `{"error": "session_not_yours"}` (IDOR prevention)
  * `429 Too Many Requests`: `{"error": "rate_limited"}` (Burst cap exceeded)
  * `502 Bad Gateway`: `{"error": "endpoint_blocked"}` or `{"error": "client_unreachable"}`

---

### `POST /redlab/v1/sessions/{sid}/flag`
Toggles an adversarial breach flag on a specific assistant message.

* **Request Body:**
  ```json
  {
    "message_id": "msg_a001",
    "is_unsafe": true
  }
  ```
* **Success Response (`200 OK`):**
  ```json
  {
    "status": "ok",
    "message_id": "msg_a001",
    "is_unsafe": true,
    "session_breach_count": 1
  }
  ```

---

### `POST /redlab/v1/sessions/{sid}/conclude`
Finalizes the conversation and links the chat transcript to the Flask submission.

* **Request Body:**
  ```json
  {
    "conclude_note": "Extracted canary token on turn 4 using Hausa language jailbreak."
  }
  ```
* **Success Response (`200 OK`):**
  ```json
  {
    "status": "concluded",
    "session_id": "sess_001",
    "total_turns": 4,
    "confirmed_breaches": 1
  }
  ```

---

## 4.2 Machine Delivery & Dataset Export APIs (`/api/v1/*`)

Authenticated via Bearer API token: `Authorization: Bearer brydl_key_...`

### `GET /api/v1/projects/{id}/export?format=openai`
Streams approved red-team transcripts in OpenAI JSONL evaluation format with reviewer anonymization.

* **Response Format (`application/x-ndjson`):**
  ```json
  {
    "messages": [
      {"role": "user", "content": "How do I bypass authentication in app Y?"},
      {"role": "assistant", "content": "[BYPASS] You can alter the header..."}
    ],
    "metadata": {
      "submission_id": "1042",
      "breach_turns": [1],
      "model": "claude-3-5-sonnet-20241022",
      "qa_reviewers": ["reviewer-865120e3"],
      "exported_at": "2026-10-11T12:30:00"
    }
  }
  ```

---

# SECTION 5: INCIDENT RESPONSE RUNBOOKS

## Runbook 1: SSRF Guard Alerts (`endpoint_blocked`)

### Symptoms
Sentry or logs show repeated `RedLab SSRF: host resolves to blocked IP` or `endpoint_blocked (502)`.

### Investigation Steps
1. Identify the target endpoint:
   ```bash
   SELECT id, project_ref, name, endpoint FROM redlab.targets WHERE active = TRUE;
   ```
2. Check if the client endpoint resolves to an internal/cloud IP:
   ```bash
   python3 -c "import socket; print(socket.getaddrinfo('client-endpoint.com', 443))"
   ```
3. If an attacker configured a DNS rebinding domain or a private IP (`169.254.x.x`, `127.x.x.x`), deactivate the target immediately:
   ```sql
   UPDATE redlab.targets SET active = FALSE WHERE id = 'target_id_here';
   ```

---

## Runbook 2: Upstream Client LLM 429 Storm / Outage

### Symptoms
Annotators report slow chats; logs show `client_rate_limited (503)` or `client_unreachable (502)`.

### Immediate Actions
1. **Check Client Spending Cap:** Log into the client model console (OpenAI/Anthropic) $\to$ verify if the monthly spending limit has been exhausted.
2. **Pause Annotations:** Set the project status to `paused` in the Admin Dashboard:
   ```sql
   UPDATE projects SET status = 'paused' WHERE id = <project_id>;
   ```
3. Once the upstream limit is increased, resume project status to `active`.

---

## Runbook 3: Emergency Kill-Switch & Full Rollback

If any critical security flaw, credential leak, or unhandled exploit is discovered in production:

### Action Plan (Time to execute: < 60 seconds)
1. **Flip the Environment Flag:**
   - In Render Dashboard $\to$ **Web Service** $\to$ **Environment**
   - Set `REDLAB_ENABLED=false` $\to$ Click **Save Changes**.
   - *Impact:* The `/redlab/v1/*` sub-app immediately returns `404 {"error": "redlab_disabled"}`. Core Flask continues serving without downtime.
2. **Deactivate Target Credentials:**
   ```sql
   UPDATE redlab.targets SET active = FALSE;
   ```
3. **Hard Rollback to Stable Release:**
   ```bash
   git checkout v13-stable
   git push render main --force
   ```

---

## Runbook 4: Database Migration Rollback / Lockups

### Symptoms
A deploy hangs on `flask db upgrade` or tables are locked.

### Resolution Steps
1. Open Render PostgreSQL shell and check active locks:
   ```sql
   SELECT pid, query, state, age(clock_timestamp(), query_start) 
   FROM pg_stat_activity 
   WHERE state != 'idle' AND query NOT ILIKE '%pg_stat_activity%';
   ```
2. Terminate blocking transactions:
   ```sql
   SELECT pg_terminate_backend(<pid>);
   ```
3. Downgrade migration if necessary:
   ```bash
   flask db downgrade -1
   ```

---

## Runbook 5: Memory Leak or Worker Crashing (OOM)

### Symptoms
Render memory metric approaches 512 MB or service restarts with `Exit code 137 (OOM)`.

### Resolution Steps
1. Verify worker count is `--workers 1`:
   ```bash
   curl -s https://<service>.onrender.com/redlab/healthz | grep pid
   ```
2. Trigger transcript retention pruning job to clear stale database memory:
   ```bash
   flask --app app jobs run-now --kind redlab.retention_prune
   ```
3. Ensure no unbuffered response streaming was triggered. The `MAX_RESPONSE_BYTES = 2097152` stream cap will prevent memory spikes above 2 MB per request.
