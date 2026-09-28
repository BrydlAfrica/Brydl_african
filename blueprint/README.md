# Brydl Blueprint — the rebuildable specification

This folder is the **complete, self-contained specification** of the Brydl
annotation platform. Hand it to a competent developer or an AI agent and
they can rebuild the system from scratch — same stack, same features,
same guarantees — without asking a single question about what it should
be.

## File map (read 01 → 12 in order for a rebuild)

| File | Contents |
|------|----------|
| `01_PRODUCT_AND_RULES.md` | What Brydl is, who uses it, the non-negotiable rules (stack, money, scalability, style) |
| `02_ARCHITECTURE.md` | File tree, app factory, blueprints, modules, templates, static/vendor, PWA |
| `03_DATA_MODEL.md` | Every table, column, constraint, index, relationship; the 8-migration chain |
| `04_ROUTES.md` | Every HTTP route in the system with method, guard, and behavior |
| `05_WORKFLOWS.md` | Lifecycles: task, submission/review, self-claim, payout, notifications, password reset, terms gate, CSV import |
| `06_SECURITY.md` | Auth, CSRF, headers, rate limiting, lockout, audit trail, bank encryption, gold-answer isolation, drift guard |
| `07_PAYMENTS_AND_EXPORTS.md` | Money rules (Decimal end-to-end), FX, payout run, bank CSV; export formats JSONL/CSV/COCO/YOLO |
| `08_CONFIG_ENV.md` | Every environment variable, default, and behavior |
| `09_DEPLOYMENT.md` | Render layout, gunicorn, production migrations, ops procedures, open operational items |
| `10_TESTING.md` | The 20-suite battery, what each suite proves, runner rules, acceptance gates |
| `11_BUILD_HISTORY.md` | Chronological log from the first prompt to now: every feature, issue found, and fix |
| `12_AI_REBUILD_PROMPT.md` | A single distilled prompt — paste into an AI agent to rebuild the system |

## THE UPDATE PROTOCOL (standing rule)

**Every change to the system updates this blueprint in the same change.**
Concretely, any feature, fix, config knob, route, model column, or
operational decision must touch:

1. the file(s) above that describe that layer,
2. `11_BUILD_HISTORY.md` (append one dated line),
3. nothing else — keep the docs layer-accurate, not narrative.

A blueprint that lags the code is worse than none. The test battery and
this folder are versioned together in git on purpose.

## Source of truth

The code in this repository is the ground truth; the blueprint is its
faithful description. If they ever disagree, the code wins and the
blueprint is wrong — fix the blueprint immediately (that is a bug).
