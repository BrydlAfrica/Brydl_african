# 07 — Payments & Exports

## Money rules (non-negotiable)

- Columns: `Numeric(10,2)` (`projects.pay_per_task`, `payments.amount`).
- `models.money(value)` normalizes every Decimal at every boundary
  (creation, aggregation, display, export).
- Forms: `DecimalField` (0.05–5.00 validated range for pay_per_task).
- Ledger currency USD; Naira display via `USD_TO_NGN_RATE` (env,
  default 1600, `config._env_decimal`). Naira display only — never a
  Naira ledger column.
- No floats touch money (Float is used only for `qa_score`).

## Paystack Transfers (Tier 2, optional)

- PAYSTACK_SECRET_KEY set = the Payments page shows a "Pay via
  Paystack" action per pending payout. Unset = feature hidden, the
  bank-batch CSV flow remains the payout path.
- Flow: recipient code created on demand from the stored (decrypted,
  in-request only) bank details — bank name fuzzy-matched against
  Paystack's NGN bank list — then cached on
  users.paystack_recipient_code. Amount: stored USD -> NGN at the
  fixed rate -> KOBO, one rounded Decimal boundary. Reference
  "brydl-p<id>-<ts>" stored on payments.paystack_reference (indexed).
- Webhook POST /webhooks/paystack (public): HMAC-SHA512 signature
  over the raw body is the ONLY auth (400 otherwise).
  transfer.success / transfer.failed settle ONE pending payment by
  reference; anything else is acknowledged 200 and ignored — retries
  are idempotent, double-settling impossible.
- Audit: payout.paystack / payout.paystack_failed / payout.webhook.
  Account numbers never enter logs, flashes or audit detail.

## Payout run

- Generate: per annotator, sweep approved submissions with
  `payment_id IS NULL` → Payment(annotator, amount=Σ pay_per_task,
  tasks_count, period_start/end, status="pending"). Audited
  `payment.generate`.
- Mark paid: status→paid, payment_method, paid_at, `payout.paid`
  notification. Audited `payment.paid`.
- Bank CSV (`/admin/payments/bank-batch`): columns
  `BRYDL-PAY-<id>, name, email, bank_name, <decrypted NUBAN>,
  account_name, <NGN amount>, <USD amount>, <rate>, tasks_count,
  period, narration`. Audited `payment.export_batch` (count, totals,
  rate — no rows). **This is the single decryption surface.**

## Project export formats

All exports: admin-only + CSRF, scope = approved | reviewed | all,
gold answers included only when the checkbox is ticked (and never in
COCO/YOLO), streamed with `yield_per=500`, pseudonymous annotator ids,
audited `project.export`, formula-guarded CSV.

### JSONL (default)
One JSON object per submission:
`{task_id, input, output, status, qa_score, annotator_id,
submitted_at, reviewed_at, is_gold[, gold]}` — `input`/`output` are
the stored JSON objects parsed safe (legacy plaintext wrapped as
`{"raw": ...}`).

### CSV
Same fields flattened; JSON embedded as compact strings; UTF-8 BOM for
Excel; `Content-Disposition: attachment`.

### COCO JSON
Schema-valid COCO: `info`, `licenses`, `images`, `annotations`,
`categories`.
- `images`: one per task — `id = task_id`, `file_name =
  "task_<id>.txt"`, `width/height = 0` (text era), plus non-standard
  `brydl_text` carrying the full input JSON so nothing is lost.
- `annotations`: one per submission — `image_id`, `category_id`,
  placeholder `segmentation []`, `area 0.0`, `bbox [0,0,0,0]`,
  `iscrowd 0`, and a `brydl` attributes block (status, qa_score,
  annotator_id).
- `categories`: distinct labels from `output_data.label` (fallback
  `kind`, fallback "task"), sorted, ids from 1.

### YOLO text (ZIP)
- `classes.txt`: sorted labels, one per line, index = category id − 1.
- One `task_<id>.txt` per task: one line per annotation,
  `<class_index> 0.5 0.5 1.0 1.0` — **full-frame placeholder boxes**
  until media tasks carry real geometry (stated in the in-zip README).
- `README.txt`: the convention statement.
- `brydl-manifest.json`: project name + per-task
  `{task_id, labels_file, annotations}`.
- Content-Type `application/zip`, filename stamped UTC.

Media-task upgrade path: when `task_type` media lands, `images` gain
real width/height/paths, placeholders become real boxes, YOLO zips
include image files — the format contracts above do not change.
