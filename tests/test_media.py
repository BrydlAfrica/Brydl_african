"""Phase 8 suite: media workspaces (image labelling, audio
transcription).

Covers: magic-byte sniffing (SVG/text rejection, RIFF
disambiguation), the admin media library (auth, upload validation,
dedupe, audit), task-bound annotator serving (holder 200, others
404, unreferenced-asset probes 404), the image and audio workspaces
(rendering + submission documents with label enforcement), review
and arbitration media display, consensus conflict behaviour for
image labels, and the size-cap knob.
"""
import io
import json
import os as _os
ROOT = _os.environ.get("BRYDL_ROOT") or _os.path.abspath(
    _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), ".."))
import re
import shutil
import sys

from app import create_app
from consensus import refresh_group
from extensions import db
from models import (
    AuditLog,
    ConsensusGroup,
    MediaAsset,
    Project,
    Submission,
    Task,
    User,
    utc_now,
)
from routes.admin import _sniff_media

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


PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
WAV = b"RIFF\x24\x08\x00\x00WAVEfmt " + b"\x00" * 64
MP3 = b"ID3\x04\x00\x00\x00\x00\x00\x00" + b"\x00" * 64
WEBP = b"RIFF\x24\x00\x00\x00WEBPVP8 " + b"\x00" * 64
SVG = b'<?xml version="1.0"?><svg xmlns="http://www.w3.org/2000/svg">'
TXT = b"just some text, definitely not media"
BIG = b"\x89PNG\r\n\x1a\n" + b"\x00" * (8 * 1024 * 1024 + 1024)

shutil.rmtree("instance", ignore_errors=True)

# ---- Section 1: sniffing ----
print("== Section 1: magic-byte sniffing ==")
expect(_sniff_media(PNG) == ("image", "image/png"), "sniff: PNG")
expect(_sniff_media(WAV) == ("audio", "audio/wav"),
       "sniff: WAV (RIFF disambiguated)")
expect(_sniff_media(WEBP) == ("image", "image/webp"),
       "sniff: WebP (RIFF disambiguated)")
expect(_sniff_media(MP3) == ("audio", "audio/mpeg"), "sniff: MP3")
expect(_sniff_media(SVG) is None, "sniff: SVG rejected")
expect(_sniff_media(TXT) is None, "sniff: text rejected")
expect(_sniff_media(b"") is None, "sniff: empty rejected")

# ---- fixtures ----
with app.app_context():
    admin = User(name="Ada Admin", email="md-admin@brydl.ng",
                 role="admin")
    admin.set_password("admin-pass-1")
    holder = User(name="Ann Hold", email="hold@brydl.ng",
                  role="annotator")
    holder.set_password("hold-pass-1")
    other = User(name="Ann Other", email="other@brydl.ng",
                 role="annotator")
    other.set_password("other-pass-1")
    db.session.add_all([admin, holder, other])
    db.session.flush()
    admin_id, holder_id, other_id = admin.id, holder.id, other.id
    proj_img = Project(name="Media Image Proj", client_name="Lab",
                       task_type="image_labeling",
                       pay_per_task=0.20, instructions="i",
                       created_by=admin_id)
    proj_audio = Project(name="Media Audio Proj", client_name="Lab",
                         task_type="audio_transcription",
                         pay_per_task=0.30, instructions="i",
                         created_by=admin_id)
    db.session.add_all([proj_img, proj_audio])
    db.session.flush()
    pid, aid_proj = proj_img.id, proj_audio.id
    asset = MediaAsset(project_id=pid, kind="image",
                       content_type="image/png",
                       filename="photo.png", byte_size=len(PNG),
                       sha256="a" * 64, data=PNG,
                       uploaded_by=admin_id)
    db.session.add(asset)
    db.session.flush()
    asset_id = asset.id
    img_doc = json.dumps({"kind": "image_labeling",
                          "media_id": asset_id,
                          "prompt": "What is in this image?",
                          "labels": "cat, dog, car"})
    t_img = Task(input_data=img_doc, project_id=pid, status="assigned",
                 assigned_to=holder_id)
    t_broken = Task(input_data=json.dumps(
        {"kind": "image_labeling", "media_id": 999999,
         "prompt": "ghost"}), project_id=pid, status="assigned",
        assigned_to=holder_id)
    t_plain = Task(input_data="plain text task", project_id=pid,
                   status="assigned", assigned_to=other_id)
    db.session.add_all([t_img, t_broken, t_plain])
    db.session.commit()
    holder.record_consent(); other.record_consent()
    db.session.commit()
    t_img_id, t_broken_id, t_plain_id = (
        t_img.id, t_broken.id, t_plain.id)

admin_c = app.test_client()
login(admin_c, "md-admin@brydl.ng", "admin-pass-1")
hold_c = app.test_client()
login(hold_c, "hold@brydl.ng", "hold-pass-1")
other_c = app.test_client()
login(other_c, "other@brydl.ng", "other-pass-1")

# ---- Section 2: media library ----
print("== Section 2: admin media library ==")
expect(other_c.get(f"/admin/projects/{pid}/media").status_code == 403,
       "annotator 403 on the media library")
page = admin_c.get(f"/admin/projects/{pid}/media").get_data(as_text=True)
expect("Upload media" in page and "media_id" in page,
       "library page renders")
csrf = get_csrf(page)
r = admin_c.post(f"/admin/projects/{pid}/media", data={
    "file": (io.BytesIO(SVG), "evil.svg"), "csrf_token": csrf},
    follow_redirects=True)
expect("Unsupported file type" in r.get_data(as_text=True),
       "SVG upload rejected")
r = admin_c.post(f"/admin/projects/{pid}/media", data={
    "file": (io.BytesIO(TXT), "note.txt"), "csrf_token": csrf},
    follow_redirects=True)
expect("Unsupported file type" in r.get_data(as_text=True),
       "text upload rejected")
r = admin_c.post(f"/admin/projects/{pid}/media", data={
    "file": (io.BytesIO(BIG), "big.png"), "csrf_token": csrf},
    follow_redirects=True)
expect("MB limit" in r.get_data(as_text=True),
       "oversize upload rejected by MEDIA_MAX_BYTES")
r = admin_c.post(f"/admin/projects/{pid}/media", data={
    "file": (io.BytesIO(MP3), "clip.mp3"), "csrf_token": csrf},
    follow_redirects=True)
expect("uploaded" in r.get_data(as_text=True),
       "MP3 upload accepted (sniffed kind)")
with app.app_context():
    audio_asset = db.session.scalar(db.select(MediaAsset).where(
        MediaAsset.kind == "audio"))
    expect(audio_asset is not None
           and audio_asset.content_type == "audio/mpeg",
           "audio asset stored with sniffed content type")
    audio_id = audio_asset.id
    expect(db.session.scalar(db.select(AuditLog).where(
        AuditLog.action == "media.upload")) is not None,
        "media.upload audited")
with app.app_context():
    before_dedupe = db.session.scalar(
        db.select(db.func.count(MediaAsset.id))
    )
r = admin_c.post(f"/admin/projects/{pid}/media", data={
    "file": (io.BytesIO(MP3), "clip-again.mp3"), "csrf_token": csrf},
    follow_redirects=True)
expect("already uploaded" in r.get_data(as_text=True),
       "identical re-upload dedupes")
with app.app_context():
    after_dedupe = db.session.scalar(
        db.select(db.func.count(MediaAsset.id))
    )
    expect(before_dedupe == after_dedupe == 2,
           f"no duplicate row created ({before_dedupe}"
           f" -> {after_dedupe})")

# second audio asset in the audio project (dedupe is per project)
csrf2 = get_csrf(admin_c.get(
    f"/admin/projects/{aid_proj}/media").get_data(as_text=True))
r = admin_c.post(f"/admin/projects/{aid_proj}/media", data={
    "file": (io.BytesIO(MP3), "clip2.mp3"), "csrf_token": csrf2},
    follow_redirects=True)
expect("uploaded" in r.get_data(as_text=True),
       "same bytes accepted in a different project")
with app.app_context():
    audio2 = db.session.scalar(
        db.select(MediaAsset)
        .where(MediaAsset.project_id == aid_proj)
    )
    audio2_id = audio2.id

# ---- Section 3: serving ----
print("== Section 3: task-bound media serving ==")
r = hold_c.get(f"/annotator/task/{t_img_id}/media/{asset_id}")
expect(r.status_code == 200 and r.data[:8] == PNG[:8]
       and r.headers.get("Content-Type", "").startswith("image/png"),
       "holder gets the image bytes with the stored type")
expect(other_c.get(
    f"/annotator/task/{t_img_id}/media/{asset_id}").status_code == 404,
    "non-holder annotator 404")
expect(hold_c.get(
    f"/annotator/task/{t_plain_id}/media/{asset_id}").status_code == 404,
    "task without a media reference 404s")
expect(hold_c.get(
    f"/annotator/task/{t_img_id}/media/{audio_id}").status_code == 404,
    "asset not referenced by the task 404s (probe)")
expect(admin_c.get(f"/admin/media/{asset_id}").status_code == 200,
       "staff serve route works for admin")
expect(hold_c.get(f"/admin/media/{asset_id}").status_code == 403,
       "annotator blocked from staff serve")

# ---- Section 4: workspaces ----
print("== Section 4: annotator workspaces ==")
page = hold_c.get(f"/annotator/task/{t_img_id}").get_data(as_text=True)
expect(f'src="/annotator/task/{t_img_id}/media/{asset_id}"' in page,
       "image renders in the workspace")
expect("cat" in page and "dog" in page and "car" in page,
       "allowed labels render as choices")
r = hold_c.post(f"/annotator/task/{t_img_id}", data={
    "label_choice": "not-a-choice", "csrf_token": get_csrf(page)})
expect("label_choice" in r.get_data(as_text=True) or r.status_code == 200,
       "bogus label choice bounces")
r = hold_c.post(f"/annotator/task/{t_img_id}", data={
    "label_choice": "dog", "notes": "A dog in a garden.",
    "csrf_token": get_csrf(page)})
expect(r.status_code == 302, "image submission accepted")
with app.app_context():
    sub = db.session.scalar(db.select(Submission).where(
        Submission.task_id == t_img_id))
    doc = json.loads(sub.output_data)
    expect(doc["kind"] == "image_labeling" and doc["label"] == "dog"
           and doc["notes"] == "A dog in a garden.",
           "image output document carries label + notes")

page = hold_c.get(f"/annotator/task/{t_broken_id}").get_data(as_text=True)
expect("name=\"label_choice\"" in page and "/media/" not in page
       and 'type="radio"' not in page,
       "dangling media reference degrades to free-text label")
r = hold_c.post(f"/annotator/task/{t_broken_id}", data={
    "label_choice": "fallback label",
    "csrf_token": get_csrf(page)})
expect(r.status_code == 302, "fallback label submission accepted")

with app.app_context():
    audio_doc = json.dumps({"kind": "audio_transcription",
                            "media_id": audio2_id,
                            "prompt": "Transcribe this clip."})
    t_audio = Task(input_data=audio_doc, project_id=aid_proj,
                   status="assigned", assigned_to=other_id)
    db.session.add(t_audio)
    db.session.commit()
    t_audio_id = t_audio.id
page = other_c.get(f"/annotator/task/{t_audio_id}").get_data(as_text=True)
expect(f'src="/annotator/task/{t_audio_id}/media/{audio2_id}"' in page,
       "audio player renders in the workspace")
expect("Transcribe this clip." in page, "audio prompt renders")
r = other_c.post(f"/annotator/task/{t_audio_id}", data={
    "transcript": "   ", "csrf_token": get_csrf(page)})
expect("Write what you hear" in r.get_data(as_text=True),
       "blank transcript bounces")
r = other_c.post(f"/annotator/task/{t_audio_id}", data={
    "transcript": "Barka da safiya",
    "csrf_token": get_csrf(other_c.get(
        f"/annotator/task/{t_audio_id}").get_data(as_text=True))})
expect(r.status_code == 302, "transcript accepted")
with app.app_context():
    sub = db.session.scalar(db.select(Submission).where(
        Submission.task_id == t_audio_id))
    doc = json.loads(sub.output_data)
    expect(doc == {"kind": "audio_transcription",
                   "transcript": "Barka da safiya"},
           "audio output document exact")

# ---- Section 5: review + arbitration display ----
print("== Section 5: review + arbitration ==")
with app.app_context():
    img_sub = db.session.scalar(db.select(Submission).where(
        Submission.task_id == t_img_id))
    img_sub_id = img_sub.id
detail = admin_c.get(f"/admin/review/{img_sub_id}").get_data(as_text=True)
expect(f"/admin/media/{asset_id}" in detail and "dog" in detail,
       "review detail shows the image + answer")
with app.app_context():
    grp = ConsensusGroup(project_id=pid)
    db.session.add(grp)
    db.session.flush()
    fresh = db.session.get(Task, t_img_id)
    fresh.consensus_group_id = grp.id
    db.session.commit()
    grp_id = grp.id
arb = admin_c.get(f"/admin/review/conflicts/{grp_id}").get_data(
    as_text=True)
expect(f"/admin/media/{asset_id}" in arb,
       "arbitration shows the round image")

# ---- Section 6: consensus behavior ----
print("== Section 6: consensus key behavior ==")
with app.app_context():
    grp2 = ConsensusGroup(project_id=pid)
    db.session.add(grp2)
    db.session.flush()
    s1 = Task(input_data=img_doc, project_id=pid, status="submitted",
              assigned_to=holder_id, consensus_group_id=grp2.id)
    s2 = Task(input_data=img_doc, project_id=pid, status="submitted",
              assigned_to=other_id, consensus_group_id=grp2.id)
    db.session.add_all([s1, s2])
    db.session.flush()
    db.session.add_all([
        Submission(task_id=s1.id, annotator_id=holder_id,
                   output_data=json.dumps(
                       {"kind": "image_labeling", "label": "cat"}),
                   status="submitted", submitted_at=utc_now()),
        Submission(task_id=s2.id, annotator_id=other_id,
                   output_data=json.dumps(
                       {"kind": "image_labeling", "label": "dog"}),
                   status="submitted", submitted_at=utc_now()),
    ])
    db.session.commit()
    refresh_group(grp2.id)
    db.session.commit()
    final_status = db.session.get(ConsensusGroup, grp2.id).status
expect(final_status == "conflict",
       "divergent image labels flag arbitration")

print()
if FAILURES:
    print(f"MEDIA TESTS FAILED ({len(FAILURES)}):")
    for label in FAILURES:
        print(f"  - {label}")
    sys.exit(1)
print("ALL MEDIA TESTS PASSED")
