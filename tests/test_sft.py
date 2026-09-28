import os as _os
ROOT = _os.environ.get("BRYDL_ROOT") or _os.path.abspath(
    _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), ".."))
"""RLHF Phase 4 suite: the SFT gold-rewrite builder.

Covers: the pure harvest pipeline (unwrap/extract/harvest priority,
messages shape, JSONL export), the admin builder surface (prefill
from approved submissions and resolved consensus rulings, create with
the mandatory rewrite note, edit + lifecycle), draft-only deletion,
the export-then-freeze contract, annotator 403s, gold never leaking
to annotator surfaces, and the audit trail.
"""
import json
import re
import shutil
import sys

from decimal import Decimal

from app import create_app
from extensions import db
from models import AuditLog, ConsensusGroup, Project, SftExample, Task, User
from models import utc_now
import sft as sftmod

app = create_app()

FAILURES = []


def expect(condition, label):
    """Record one granular pass/fail check."""
    if condition:
        print(f"   ok: {label}")
    else:
        FAILURES.append(label)
        print(f"FAIL: {label}")


def get_csrf(html):
    """Pull the CSRF token out of a rendered form page."""
    m = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', html)
    return m.group(1) if m else None


def login(client, email, password):
    """POST the sign-in form."""
    r = client.get("/login")
    tk = get_csrf(r.get_data(as_text=True))
    return client.post("/login", data={"email": email,
                                       "password": password,
                                       "csrf_token": tk})


PROMPT = "Which reply better answers: how do I hard-boil an egg?"
RESP_A = "Boil them for 40 seconds."
RESP_B = ("Boil 8 minutes from cold water, then chill in ice water "
          "before peeling.")
PREF_DOC = json.dumps({"kind": "preference", "prompt": PROMPT,
                       "response_a": RESP_A, "response_b": RESP_B})
TRANSLATION_INPUT = "Greet the user in Hausa: good morning"
TRANSLATION_GOLD = "Barka da safiya"
GOLD_TASK_INPUT = "Say welcome in Hausa"
GOLD_TASK_GOLD = "Sannu da zuwa"

shutil.rmtree("instance", ignore_errors=True)
shutil.rmtree("backups", ignore_errors=True)

# ================= Section 1: pure harvest pipeline =================
print("== Section 1: sft.py unit contract ==")
expect(sftmod.unwrap_output(
    json.dumps({"kind": "translation", "text": "Barka"})
    ) == "Barka", "unwrap: translation text")
expect(sftmod.unwrap_output(
    json.dumps({"kind": "classification", "label": " negative "})
    ) == "negative", "unwrap: classification label strips")
expect(sftmod.unwrap_output(
    json.dumps({"kind": "rlhf", "rating": 4, "explanation": "clear"})
    ) == "Rated 4/5: clear", "unwrap: rlhf rating + explanation")
expect(sftmod.unwrap_output(
    json.dumps({"kind": "preference", "chosen": "b"}), PREF_DOC
    ) == RESP_B, "unwrap: preference chosen maps to response text")
expect(sftmod.unwrap_output("plain gold text") == "plain gold text",
       "unwrap: plain string passes through")
expect(sftmod.unwrap_output(
    json.dumps({"kind": "ner", "marked_entities": "Lagos, Kano"})
    ) == "Lagos, Kano", "unwrap: ner marked entities")
expect(sftmod.unwrap_output("   ") is None, "unwrap: blank is None")
expect(sftmod.extract_prompt(PREF_DOC) == PROMPT,
       "extract: preference prompt")
conv = json.dumps({"kind": "conversation", "turns": [
    {"role": "system", "text": "Be brief."},
    {"role": "user", "text": "How do I boil an egg?"},
    {"role": "assistant", "text": "Eight minutes."},
    {"role": "user", "text": "And peeling?"},
]})
expect(sftmod.extract_prompt(conv) == "And peeling?",
       "extract: conversation last user turn")
expect(sftmod.extract_prompt("raw import text") == "raw import text",
       "extract: plain input passes through")
expect(sftmod.extract_prompt("  ") is None, "extract: blank is None")
got = sftmod.harvest_completion(PREF_DOC, None, "resolved", "b", None)
expect(got == (RESP_B, "consensus"), "harvest: resolved ruling wins")
got = sftmod.harvest_completion(
    TRANSLATION_INPUT, TRANSLATION_GOLD, "pending", None,
    json.dumps({"kind": "translation", "text": TRANSLATION_GOLD}))
expect(got == (TRANSLATION_GOLD, "approved"),
       "harvest: approved beats gold")
got = sftmod.harvest_completion(
    GOLD_TASK_INPUT, GOLD_TASK_GOLD, None, None, None)
expect(got == (GOLD_TASK_GOLD, "gold"), "harvest: gold fallback")
expect(sftmod.harvest_completion("x", None, None, None, None)
       == (None, "manual"), "harvest: nothing -> manual")
msgs = sftmod.to_messages("q", "a", "be nice")
expect(msgs == {"messages": [
    {"role": "system", "content": "be nice"},
    {"role": "user", "content": "q"},
    {"role": "assistant", "content": "a"}]},
    "messages: system + user + assistant order")
expect("system" not in json.dumps(
    sftmod.to_messages("q", "a")), "messages: system omitted when blank")
body = sftmod.export_jsonl([{
    "id": 7, "task_id": 3, "project_id": 1,
    "prompt": "q", "completion": "a",
    "system_prompt": None, "updated_at": utc_now(),
}])
rec = json.loads(body)
expect(rec["messages"][-1] == {"role": "assistant", "content": "a"}
       and rec["id"] == 7 and rec["task_id"] == 3,
       "export: JSONL record shape")
expect(body.endswith("\n") and sftmod.export_jsonl([]) == "",
       "export: newline-terminated, empty list -> empty body")

# ================= Fixtures =================
with app.app_context():
    admin = User(name="Ada Admin", email="sft-admin@brydl.ng",
                 role="admin")
    admin.set_password("admin-pass-1")
    ann = User(name="Ann Four", email="four@brydl.ng", role="annotator",
               terms_version="1.0")
    ann.set_password("annot-pass-4")
    db.session.add_all([admin, ann])
    db.session.flush()
    admin_id, ann_id = admin.id, ann.id

    p1 = Project(name="Hausa SFT", client_name="Lab",
                 task_type="translation", pay_per_task=Decimal("0.25"),
                 required_annotations=1, instructions="Translate.",
                 created_by=admin_id)
    p2 = Project(name="Egg RLHF", client_name="Lab",
                 task_type="preference", pay_per_task=Decimal("0.30"),
                 required_annotations=2, instructions="Pick better.",
                 created_by=admin_id)
    db.session.add_all([p1, p2])
    db.session.flush()
    p1_id, p2_id = p1.id, p2.id

    t_trans = Task(input_data=TRANSLATION_INPUT, project_id=p1_id,
                   expected_output=TRANSLATION_GOLD, status="approved",
                   assigned_to=ann_id)
    t_gold = Task(input_data=GOLD_TASK_INPUT, project_id=p1_id,
                  expected_output=GOLD_TASK_GOLD, status="assigned",
                  assigned_to=ann_id)
    t_pref = Task(input_data=PREF_DOC, project_id=p2_id,
                  status="approved", assigned_to=ann_id)
    db.session.add_all([t_trans, t_gold, t_pref])
    db.session.flush()

    from models import Submission
    sub = Submission(
        task_id=t_trans.id, annotator_id=ann_id,
        output_data=json.dumps(
            {"kind": "translation", "text": TRANSLATION_GOLD}),
        qa_score=95.0, status="approved",
        submitted_at=utc_now(), reviewed_at=utc_now())
    db.session.add(sub)
    db.session.flush()
    sub_id = sub.id

    grp = ConsensusGroup(
        project_id=p2_id, required_count=2, status="resolved",
        decided_choice="b", decided_note="B is actually correct.",
        resolved_by=admin_id, resolved_at=utc_now())
    db.session.add(grp)
    db.session.flush()
    t_pref.consensus_group_id = grp.id
    db.session.commit()
    trans_id, gold_id, pref_id = t_trans.id, t_gold.id, t_pref.id
    grp_id = grp.id

admin_c = app.test_client()
login(admin_c, "sft-admin@brydl.ng", "admin-pass-1")
ann_c = app.test_client()
login(ann_c, "four@brydl.ng", "annot-pass-4")

# ================= Section 2: access control + gold leak =================
print("== Section 2: access + gold secrecy ==")
expect(ann_c.get("/admin/sft").status_code == 403,
       "annotator blocked from SFT list")
expect(ann_c.get(f"/admin/sft/new?task_id={trans_id}").status_code == 403,
       "annotator blocked from SFT builder")
task_page = ann_c.get(f"/annotator/task/{gold_id}").get_data(as_text=True)
expect(task_page and GOLD_TASK_GOLD not in task_page,
       "gold answer never renders on the annotator task page")
missing = admin_c.get("/admin/sft/new?task_id=999999")
expect(missing.status_code == 404, "unknown task id on builder 404s")

# ================= Section 3: harvest prefill + create =================
print("== Section 3: prefill + create with rewrite note ==")
prefill = admin_c.get(f"/admin/sft/new?task_id={trans_id}"
                      ).get_data(as_text=True)
expect(TRANSLATION_GOLD in prefill and TRANSLATION_INPUT in prefill,
       "builder prefills harvested prompt + completion")
expect('value="approved"' in prefill or "approved" in prefill,
       "prefill names its source")
page = admin_c.post("/admin/sft/new", data={
    "task_id": str(trans_id),
    "prompt": TRANSLATION_INPUT,
    "completion": TRANSLATION_GOLD,
    "status": "ready",
    "csrf_token": get_csrf(admin_c.get("/admin/sft").get_data(
        as_text=True)),
})
expect(page.status_code == 302, "clean create redirects")
with app.app_context():
    row = db.session.get(SftExample, 1)
    expect(row is not None and row.source == "approved"
           and row.status == "ready"
           and row.completion == TRANSLATION_GOLD
           and row.source_completion == TRANSLATION_GOLD,
           "row carries source=approved, ready, unrewritten")
    ex1_id = row.id
expect(admin_c.get(f"/admin/sft/{ex1_id}").get_data(
    as_text=True).count("matches the harvest") == 1,
    "detail states completion matches the harvest")

# rewritten completion WITHOUT note is rejected
before = admin_c.get("/admin/sft").get_data(as_text=True)
reject = admin_c.post("/admin/sft/new", data={
    "task_id": str(trans_id),
    "prompt": TRANSLATION_INPUT,
    "completion": TRANSLATION_GOLD + " — polished",
    "edit_note": "",
    "status": "draft",
    "csrf_token": get_csrf(admin_c.get("/admin/sft").get_data(
        as_text=True)),
})
expect("rewrite note is required" in reject.get_data(as_text=True),
       "rewrite without note is bounced on the form")
with app.app_context():
    count = db.session.scalar(
        db.select(db.func.count(SftExample.id)))
expect(count == 1, "bounced rewrite created no row")

# rewritten completion WITH note is accepted
ok = admin_c.post("/admin/sft/new", data={
    "task_id": str(pref_id),
    "prompt": PROMPT,
    "completion": RESP_B + "\n\n(Step 4: peel under cold water.)",
    "edit_note": ("Added the missing peeling step the winning response"
                  " skipped."),
    "status": "ready",
    "csrf_token": get_csrf(admin_c.get("/admin/sft").get_data(
        as_text=True)),
})
expect(ok.status_code == 302, "noted rewrite creates")
with app.app_context():
    row2 = db.session.scalar(db.select(SftExample).order_by(
        SftExample.id.desc()).limit(1))
    ex2_id = row2.id
    expect(row2.source == "consensus"
           and row2.source_completion == RESP_B
           and row2.completion != row2.source_completion
           and row2.edit_note and row2.status == "ready",
           "consensus-harvested rewrite: source/anchor/note/status")
detail = admin_c.get(f"/admin/sft/{ex2_id}").get_data(as_text=True)
expect("rewritten" in detail.lower() and RESP_B in detail,
       "detail marks the rewrite + shows the immutable source")

# oversize completion rejected via the knob
from config import SFT_MAX_TEXT_CHARS
big = admin_c.post("/admin/sft/new", data={
    "task_id": str(trans_id),
    "prompt": "p",
    "completion": "x" * (SFT_MAX_TEXT_CHARS + 1),
    "status": "draft",
    "csrf_token": get_csrf(admin_c.get("/admin/sft").get_data(
        as_text=True)),
})
expect("characters or less" in big.get_data(as_text=True),
       "oversize completion bounces on the knob")
probe = admin_c.post("/admin/sft/new", data={
    "task_id": "999999",
    "prompt": "p", "completion": "c", "status": "draft",
    "csrf_token": get_csrf(admin_c.get("/admin/sft").get_data(
        as_text=True)),
})
expect(probe.status_code == 404, "posted bogus task id probe 404s")

# ================= Section 4: lifecycle =================
print("== Section 4: lifecycle — edit, delete, export-freeze ==")
edit = admin_c.post(f"/admin/sft/{ex2_id}", data={
    "task_id": str(pref_id),
    "prompt": PROMPT,
    "completion": RESP_B + "\n\n(Steps 3-5: boil, chill, peel.)",
    "edit_note": "Tightened into explicit numbered steps.",
    "status": "draft",
    "csrf_token": get_csrf(admin_c.get(f"/admin/sft/{ex2_id}"
                                       ).get_data(as_text=True)),
})
expect(edit.status_code == 302, "edit redirects")
with app.app_context():
    row2 = db.session.get(SftExample, ex2_id)
    expect(row2.status == "draft"
           and "chill" in row2.completion,
           "edit persisted (status + completion)")
    audit_update = db.session.scalar(db.select(AuditLog).where(
        AuditLog.action == "sft.update",
        AuditLog.entity_id == ex2_id)) is not None
expect(audit_update, "sft.update audited")

# draft deletes; ready refuses
gone = admin_c.post(f"/admin/sft/{ex2_id}/delete", data={
    "csrf_token": get_csrf(admin_c.get(f"/admin/sft/{ex2_id}"
                                       ).get_data(as_text=True))})
expect(gone.status_code == 302, "draft delete redirects")
with app.app_context():
    expect(db.session.get(SftExample, ex2_id) is None,
           "draft row actually deleted")
    audit_del = db.session.scalar(db.select(AuditLog).where(
        AuditLog.action == "sft.delete")) is not None
expect(audit_del, "sft.delete audited")
refused = admin_c.post(f"/admin/sft/{ex1_id}/delete", data={
    "csrf_token": get_csrf(admin_c.get("/admin/sft").get_data(
        as_text=True))})
expect(refused.status_code == 302, "ready delete redirects with flash")
with app.app_context():
    expect(db.session.get(SftExample, ex1_id) is not None,
           "ready row survives delete attempt")

# ================= Section 5: export + freeze =================
print("== Section 5: export JSONL + terminal freeze ==")
no_csrf = admin_c.post("/admin/sft/export", data={
    "project": str(p1_id), "status": "ready"}, follow_redirects=True)
expect("Export request was invalid" in no_csrf.get_data(as_text=True),
       "export without CSRF bounces with nothing exported")
resp = admin_c.post("/admin/sft/export", data={
    "project": str(p1_id), "status": "ready",
    "csrf_token": get_csrf(admin_c.get("/admin/sft").get_data(
        as_text=True))})
expect(resp.status_code == 200
       and "attachment" in resp.headers.get("Content-Disposition", "")
       and resp.headers["Content-Disposition"].endswith(".jsonl"),
       "export downloads a .jsonl attachment")
lines = [json.loads(ln) for ln in resp.get_data(as_text=True)
         .splitlines() if ln.strip()]
expect(len(lines) == 1 and lines[0]["task_id"] == trans_id
       and lines[0]["messages"][0]["role"] == "user"
       and lines[0]["messages"][-1]["content"] == TRANSLATION_GOLD,
       "export body: ready row as chat messages")
with app.app_context():
    ex1 = db.session.get(SftExample, ex1_id)
    expect(ex1.status == "exported", "exported row frozen in DB")
    audit_exp = db.session.scalar(db.select(AuditLog).where(
        AuditLog.action == "sft.export",
        AuditLog.entity_id == ex1_id)) is not None
expect(audit_exp, "sft.export audited")
second = admin_c.post("/admin/sft/export", data={
    "project": str(p1_id), "status": "ready",
    "csrf_token": get_csrf(admin_c.get("/admin/sft").get_data(
        as_text=True))}, follow_redirects=True)
expect("No ready pairs matched" in second.get_data(as_text=True),
       "re-export with nothing ready flashes empty")
frozen_post = admin_c.post(f"/admin/sft/{ex1_id}", data={
    "task_id": str(trans_id),
    "prompt": "tampered", "completion": "tampered",
    "status": "ready",
    "csrf_token": get_csrf(admin_c.get(f"/admin/sft/{ex1_id}"
                                       ).get_data(as_text=True))},
    follow_redirects=True)
expect("frozen" in frozen_post.get_data(as_text=True),
       "editing an exported pair flashes the freeze")
with app.app_context():
    ex1 = db.session.get(SftExample, ex1_id)
    expect(ex1.prompt == TRANSLATION_INPUT
           and ex1.completion == TRANSLATION_GOLD
           and ex1.status == "exported",
           "frozen pair untouched by the tampering POST")
get_export = admin_c.get("/admin/sft/export")
expect(get_export.status_code == 405, "GET on export is 405")

# list page shows the rows + counts
listing = admin_c.get("/admin/sft").get_data(as_text=True)
expect("SFT builder" in listing and "Pair #" in listing
       and "exported" in listing,
       "builder list renders pairs + status counts")

print()
if FAILURES:
    print(f"SFT TESTS FAILED ({len(FAILURES)}):")
    for label in FAILURES:
        print(f"  - {label}")
    sys.exit(1)
print("ALL SFT TESTS PASSED")
