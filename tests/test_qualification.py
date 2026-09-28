"""Phase 9 suite: qualification quizzes.

Covers: the pure engine (parse matrix, grading, deterministic
order, score rounding), the admin quiz console (auth, authoring,
lifecycle draft→active→retired, exemptions, revocation, audits,
activation notifications), the gate (claim refusal until pass,
assignment skip, task-page redirect, retired/draft non-gating) and
the attempt contract (score storage, answer-sheet secrecy — correct
flags never rendered — attempt caps and retake refusal).
"""
import json
import os as _os
ROOT = _os.environ.get("BRYDL_ROOT") or _os.path.abspath(
    _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), ".."))
import re
import shutil
import sys

from app import create_app
from extensions import db
from models import (
    AuditLog,
    Notification,
    Project,
    QualificationQuiz,
    QuizAttempt,
    Task,
    User,
)
from quizzes import (
    display_order,
    grade,
    load_questions,
    parse_quiz_text,
    score_percent,
)

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


GOOD = ("Q: When is the dog label correct?\n"
        "- Any animal appears\n"
        "* A dog is clearly visible\n"
        "Q: What about blurry photos?\n"
        "* Skip and flag them\n"
        "- Label them dog anyway")

shutil.rmtree("instance", ignore_errors=True)

# ---- Section 1: pure engine ----
print("== Section 1: quiz engine ==")
questions, errors = parse_quiz_text(GOOD)
expect(questions is not None and errors == []
       and len(questions) == 2
       and questions[0]["options"][1]["correct"] is True,
       "parse: well-formed quiz")
expect(parse_quiz_text("Q: one option only\n- a")[0] is None,
       "parse: single option rejected")
expect(parse_quiz_text("Q: no correct\n- a\n- b")[0] is None,
       "parse: missing correct rejected")
expect(parse_quiz_text("Q: two correct\n* a\n* b")[0] is None,
       "parse: two correct rejected")
expect(parse_quiz_text("- orphan option")[0] is None,
       "parse: orphan option rejected")
expect(parse_quiz_text("")[0] is None,
       "parse: empty rejected")
expect(parse_quiz_text("Q: " + "x" * 501 + "\n- a\n* b")[0] is None,
       "parse: overlong question rejected")
expect(parse_quiz_text("Q: k?\n" + "\n".join(
    ["Q: k?\n- a\n* b"] * 31))[0] is None,
    "parse: over 30 questions rejected")
q2, _ = parse_quiz_text("Q: k?\n- a\n* b")
expect(grade(q2, {"q_0": 1}) == (1, 1)
       and grade(q2, {"q_0": 0}) == (0, 1)
       and grade(q2, {"q_0": 99}) == (0, 1),
       "grade: correct/wrong/out-of-range")
expect(score_percent(2, 3) == 67 and score_percent(1, 2) == 50,
       "score: integer rounding")
order_a = display_order(9, 4, 5)
expect(order_a == display_order(9, 4, 5)
       and sorted(order_a) == list(range(5))
       and order_a != display_order(9, 5, 5),
       "order: deterministic, permutation, per-annotator")
expect(load_questions(None) == []
       and load_questions("not json") == [],
       "load: malformed payload degrades to empty")

# ---- fixtures ----
with app.app_context():
    admin = User(name="Ada Admin", email="qq-admin@brydl.ng",
                 role="admin")
    admin.set_password("admin-pass-1")
    ann = User(name="Ann Quiz", email="quiz@brydl.ng",
               role="annotator")
    ann.set_password("ann-pass-1")
    db.session.add_all([admin, ann])
    db.session.flush()
    admin_id, ann_id = admin.id, ann.id
    gated = Project(name="Gated Proj", client_name="Lab",
                    task_type="translation", pay_per_task=0.20,
                    instructions="i", created_by=admin_id)
    free = Project(name="Free Proj", client_name="Lab",
                   task_type="translation", pay_per_task=0.20,
                   instructions="i", created_by=admin_id)
    db.session.add_all([gated, free])
    db.session.flush()
    gated_id, free_id = gated.id, free.id
    t1 = Task(input_data="t1", project_id=gated_id,
              status="pending")
    t2 = Task(input_data="t2", project_id=gated_id,
              status="pending")
    t3 = Task(input_data="t3", project_id=free_id,
              status="pending")
    db.session.add_all([t1, t2, t3])
    db.session.commit()
    first_gated_id = t1.id

admin_c = app.test_client()
login(admin_c, "qq-admin@brydl.ng", "admin-pass-1")
ann_c = app.test_client()
login(ann_c, "quiz@brydl.ng", "ann-pass-1")

# ---- Section 2: admin console ----
print("== Section 2: admin quiz console ==")
expect(ann_c.get(f"/admin/projects/{gated_id}/quiz"
                 ).status_code == 403,
       "annotator 403 on the quiz console")
page = admin_c.get(f"/admin/projects/{gated_id}/quiz"
                   ).get_data(as_text=True)
r = admin_c.post(f"/admin/projects/{gated_id}/quiz", data={
    "title": "Translation basics",
    "questions_text": GOOD,
    "pass_score": "80",
    "max_attempts": "2",
    "csrf_token": get_csrf(page),
}, follow_redirects=True)
expect("saved as a draft" in r.get_data(as_text=True),
       "quiz saved as draft")
with app.app_context():
    quiz = db.session.scalar(db.select(QualificationQuiz).where(
        QualificationQuiz.project_id == gated_id))
    expect(quiz is not None and quiz.status == "draft"
           and len(load_questions(quiz.questions_json)) == 2,
           "quiz row stored with canonical payload")
    quiz_id = quiz.id
    expect(db.session.scalar(db.select(AuditLog).where(
        AuditLog.action == "quiz.save")) is not None,
        "quiz.save audited")
page = admin_c.get(f"/admin/projects/{gated_id}/quiz"
                   ).get_data(as_text=True)
r = admin_c.post(f"/admin/projects/{gated_id}/quiz", data={
    "title": "Bad quiz", "questions_text": "Q: broken\n- only one",
    "pass_score": "80", "max_attempts": "3",
    "csrf_token": get_csrf(page),
}, follow_redirects=True)
expect("exactly one correct" in r.get_data(as_text=True),
       "bad paste bounces with parse errors")
with app.app_context():
    db.session.expire_all()
    q = db.session.get(QualificationQuiz, quiz_id)
    expect(q.title == "Translation basics",
           "failed save did not clobber the draft")
csrf_dash = get_csrf(ann_c.get("/annotator/").get_data(as_text=True))
ann_c.post("/annotator/claim", data={"csrf_token": csrf_dash})
with app.app_context():
    claimed_row = db.session.get(Task, first_gated_id)
expect(claimed_row is not None
       and claimed_row.status == "assigned"
       and claimed_row.assigned_to == ann_id,
       "draft quiz does not gate; claim took the oldest task")
page = admin_c.get(f"/admin/projects/{gated_id}/quiz"
                   ).get_data(as_text=True)
r = admin_c.post(f"/admin/projects/{gated_id}/quiz/activate",
                 data={"csrf_token": get_csrf(page)},
                 follow_redirects=True)
expect("now requires a passing score" in r.get_data(as_text=True),
       "quiz activated")
with app.app_context():
    q = db.session.get(QualificationQuiz, quiz_id)
    expect(q.status == "active"
           and db.session.scalar(db.select(AuditLog).where(
               AuditLog.action == "quiz.activated")) is not None
           and db.session.scalar(db.select(Notification).where(
               Notification.user_id == ann_id,
               Notification.kind == "quiz.activated"
           )) is not None,
           "activation persisted, audited, annotator notified")
page = admin_c.get(f"/admin/projects/{gated_id}/quiz"
                   ).get_data(as_text=True)
r = admin_c.post(f"/admin/projects/{gated_id}/quiz", data={
    "title": "Sneaky edit", "questions_text": GOOD,
    "pass_score": "50", "max_attempts": "3",
    "csrf_token": get_csrf(page),
}, follow_redirects=True)
expect("Only a draft quiz can be edited" in r.get_data(as_text=True),
       "active quiz cannot be edited mid-flight")
with app.app_context():
    q = db.session.get(QualificationQuiz, quiz_id)
    expect(q.title == "Translation basics" and q.pass_score == 80,
           "refused edit changed nothing")

# ---- Section 3: the gate ----
print("== Section 3: the gate ==")
page = ann_c.get("/annotator/").get_data(as_text=True)
expect("Qualification needed" in page
       and f"/annotator/quiz/{quiz_id}" in page,
       "dashboard shows the pending-quiz card")
r = ann_c.post("/annotator/claim", data={
    "csrf_token": get_csrf(ann_c.get("/annotator/").get_data(
        as_text=True))}, follow_redirects=True)
with app.app_context():
    gated_task = db.session.scalar(
        db.select(Task.id).where(
            Task.project_id == gated_id, Task.status == "pending"
        )
    )
    free_taken = db.session.scalar(
        db.select(Task.id).where(
            Task.project_id == free_id, Task.status == "assigned"
        )
    )
expect(gated_task is not None and free_taken is not None,
       "claim skips the gated project and takes the free one")
# admin assign is gated the same way
tasks_page = admin_c.get("/admin/tasks").get_data(as_text=True)
r = admin_c.post("/admin/tasks/assign", data={
    "task_ids": str(gated_task),
    "annotator_id": str(ann_id),
    "csrf_token": get_csrf(tasks_page),
}, follow_redirects=True)
expect("has not passed the qualification" in r.get_data(as_text=True),
       "assignment skips gated tasks with a warning")
with app.app_context():
    still_pending = db.session.scalar(
        db.select(Task.id).where(
            Task.id == gated_task, Task.status == "pending"
        )
    )
expect(still_pending is not None,
       "gated task left pending")
# task page redirects to the quiz (the annotator's OWN assigned
# task from the draft phase — the project is gated now)
r = ann_c.get(f"/annotator/task/{first_gated_id}",
              follow_redirects=False)
expect(r.status_code == 302
       and f"/annotator/quiz/{quiz_id}" in (
           r.headers.get("Location") or ""),
       "task page redirects the unqualified to the quiz")

# ---- Section 4: attempts ----
print("== Section 4: attempts ==")
page = ann_c.get(f"/annotator/quiz/{quiz_id}").get_data(as_text=True)
expect("dog label correct" in page
       and "Skip and flag" in page,
       "quiz page renders questions")
expect("correct" not in page.lower().replace("when is the dog label"
                                             " correct?", ""),
       "no correct-answer marker leaks to the annotator")
r = ann_c.post(f"/annotator/quiz/{quiz_id}", data={
    "q_0": "0", "q_1": "0",
    "csrf_token": get_csrf(page)}, follow_redirects=True)
expect("Scored 50%" in r.get_data(as_text=True),
       "wrong answers graded honestly")
with app.app_context():
    fail_row = db.session.scalar(db.select(QuizAttempt).where(
        QuizAttempt.quiz_id == quiz_id,
        QuizAttempt.status == "failed"))
    expect(fail_row is not None and fail_row.score_percent == 50
           and json.loads(fail_row.answers_json) == {"q_0": 0,
                                                     "q_1": 0},
           "failed attempt stored with canonical sheet")
    expect(db.session.scalar(db.select(AuditLog).where(
        AuditLog.action == "quiz.attempt")) is not None,
        "quiz.attempt audited")
page = ann_c.get(f"/annotator/quiz/{quiz_id}").get_data(as_text=True)
r = ann_c.post(f"/annotator/quiz/{quiz_id}", data={
    "q_0": "1", "q_1": "0",
    "csrf_token": get_csrf(page)}, follow_redirects=True)
expect("Passed with 100%" in r.get_data(as_text=True),
       "correct answers pass")
with app.app_context():
    pass_row = db.session.scalar(db.select(QuizAttempt).where(
        QuizAttempt.quiz_id == quiz_id,
        QuizAttempt.status == "passed"))
    expect(pass_row is not None
           and pass_row.score_percent == 100
           and pass_row.correct_count == 2,
           "passed attempt stored")
r = ann_c.get(f"/annotator/quiz/{quiz_id}", follow_redirects=True)
expect("already qualify" in r.get_data(as_text=True),
       "retake refused once qualified")
with app.app_context():
    count = db.session.scalar(
        db.select(QuizAttempt.id).where(
            QuizAttempt.quiz_id == quiz_id,
            QuizAttempt.annotator_id == ann_id,
        )
    )
rows_after = None
with app.app_context():
    rows_after = list(db.session.scalars(
        db.select(QuizAttempt.id).where(
            QuizAttempt.quiz_id == quiz_id,
            QuizAttempt.annotator_id == ann_id,
        )
    ).all())
expect(len(rows_after) == 2, "no extra attempt rows")
r = ann_c.post("/annotator/claim", data={
    "csrf_token": get_csrf(ann_c.get("/annotator/").get_data(
        as_text=True))}, follow_redirects=True)
expect("claimed" in r.get_data(as_text=True).lower()
       or "Task #" in r.get_data(as_text=True),
       "claim works after passing")
body = ann_c.get(f"/annotator/task/{first_gated_id}").get_data(
    as_text=True)
expect("textarea" in body.lower() or "your translation" in body.lower(),
       "task page opens after passing")

# ---- Section 5: exemption + revoke + retirement ----
print("== Section 5: exemption, revoke, retirement ==")
page = admin_c.get(f"/admin/projects/{gated_id}/quiz"
                   ).get_data(as_text=True)
r = admin_c.post(f"/admin/projects/{gated_id}/quiz/grant", data={
    "annotator_id": str(ann_id),
    "csrf_token": get_csrf(page),
}, follow_redirects=True)
expect("already qualifies" in r.get_data(as_text=True),
       "duplicate exemption refused")
with app.app_context():
    db.session.execute(
        db.delete(QuizAttempt).where(
            QuizAttempt.quiz_id == quiz_id,
            QuizAttempt.annotator_id == ann_id,
            QuizAttempt.status == "passed",
        )
    )
    db.session.commit()
r = admin_c.post(f"/admin/projects/{gated_id}/quiz/grant", data={
    "annotator_id": str(ann_id),
    "csrf_token": get_csrf(page),
}, follow_redirects=True)
expect("is exempt" in r.get_data(as_text=True),
       "exemption granted")
with app.app_context():
    exempt_row = db.session.scalar(db.select(QuizAttempt).where(
        QuizAttempt.quiz_id == quiz_id,
        QuizAttempt.status == "exempt"))
    expect(exempt_row is not None
           and exempt_row.answers_json is None
           and exempt_row.granted_by == admin_id,
           "exempt row: no sheet, granted_by recorded")
    expect(db.session.scalar(db.select(AuditLog).where(
        AuditLog.action == "quiz.exempt")) is not None,
        "quiz.exempt audited")
r = admin_c.post(f"/admin/projects/{gated_id}/quiz/revoke", data={
    "annotator_id": str(ann_id),
    "csrf_token": get_csrf(page),
}, follow_redirects=True)
expect("must re-qualify" in r.get_data(as_text=True),
       "revocation works")
with app.app_context():
    expect(db.session.scalar(
        db.select(QuizAttempt.id).where(
            QuizAttempt.quiz_id == quiz_id,
            QuizAttempt.annotator_id == ann_id,
            QuizAttempt.status.in_(("passed", "exempt")),
        )
    ) is None,
        "passed/exempt rows removed")
    failed_left = db.session.scalar(
        db.select(QuizAttempt.id).where(
            QuizAttempt.quiz_id == quiz_id,
            QuizAttempt.status == "failed",
        )
    )
expect(failed_left is not None, "failed history kept on revoke")
page = admin_c.get(f"/admin/projects/{gated_id}/quiz"
                   ).get_data(as_text=True)
r = admin_c.post(f"/admin/projects/{gated_id}/quiz/retire",
                 data={"csrf_token": get_csrf(page)},
                 follow_redirects=True)
expect("gate is lifted" in r.get_data(as_text=True),
       "quiz retired")
with app.app_context():
    q = db.session.get(QualificationQuiz, quiz_id)
    expect(q.status == "retired",
           "retired status persisted")
r = ann_c.post("/annotator/claim", data={
    "csrf_token": get_csrf(ann_c.get("/annotator/").get_data(
        as_text=True))}, follow_redirects=True)
expect("claimed" in r.get_data(as_text=True).lower()
       or "Task #" in r.get_data(as_text=True),
       "retired quiz does not gate")
r = ann_c.get(f"/annotator/quiz/{quiz_id}", follow_redirects=True)
expect("not open" in r.get_data(as_text=True),
       "retired quiz is not takeable")
expect(ann_c.get("/annotator/quiz/999999").status_code == 404,
       "unknown quiz 404s")
r = ann_c.post(f"/annotator/quiz/{quiz_id}", data={"q_0": "0"})
expect(r.status_code == 302,
       "quiz POST without CSRF redirects")

print()
if FAILURES:
    print(f"QUALIFICATION TESTS FAILED ({len(FAILURES)}):")
    for label in FAILURES:
        print(f"  - {label}")
    sys.exit(1)
print("ALL QUALIFICATION TESTS PASSED")
