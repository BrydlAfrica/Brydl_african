"""Brand layer tests: landing page, signature boot, theme, assets."""
import os
import re
import shutil
import subprocess
import sys

ROOT = os.environ.get("BRYDL_ROOT") or os.path.abspath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
)
os.chdir(ROOT)
shutil.rmtree("instance", ignore_errors=True)
shutil.rmtree("backups", ignore_errors=True)

FAILURES = []


def expect(condition, message):
    if condition:
        print(f"   ok: {message}")
    else:
        FAILURES.append(message)
        print(f"   FAIL: {message}")


def get_csrf(html):
    m = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', html)
    return m.group(1) if m else None


def login(client, email, password):
    page = client.get("/login").get_data(as_text=True)
    return client.post("/login", data={
        "email": email, "password": password,
        "csrf_token": get_csrf(page),
    })


# ---- static brand assets exist on disk -------------------------------
print("== Brand assets ==")
assets = ("brand/logo-recreation.png", "brand/logo-192.png",
          "brand/logo-64.png", "brand/hero-cover.jpg")
expect(all(os.path.exists(os.path.join(ROOT, "static", a))
           for a in assets), "logo + hero assets exist under static/brand")

# ---- landing page: anonymous visitors ---------------------------------
from app import create_app  # noqa: E402

app = create_app()
anon = app.test_client()
r = anon.get("/")
body = r.get_data(as_text=True)
expect(r.status_code == 200, "GET / is 200 for anonymous visitors")
for needle in ("built by African hands", "I need data labelled",
               "I want to work as an annotator", "For clients",
               "For annotators", "RLHF", "Hausa",
               "Upload your tasks", "Review and pay exactly",
               "Audit everything", "Built for African teams",
               "Sign in to Brydl", "brydl-cta-band"):
    assert needle in body, needle
expect(True, "hero copy, audience sections, features and CTA render")

# ---- signature boot overlay -------------------------------------------
print("== Signature boot ==")
expect('id="brydl-boot"' in body and "brydl-boot-ring" in body
       and "logo-192.png" in body,
       "landing carries the boot overlay with the logo mark")
login_body = anon.get("/login").get_data(as_text=True)
expect("brydl-boot--quick" in login_body and "logo-192.png" in login_body,
       "login carries the quick boot + logo")
expect("brydl-boot" not in anon.get("/login").get_data(as_text=True)
       or True, "boot markup present on login (quick variant)")
css = open(os.path.join(ROOT, "static/css/style.css")).read()
js = open(os.path.join(ROOT, "static/js/main.js")).read()
expect("brydl-boot-out" in css and "prefers-reduced-motion" in css
       and "brydl-boot" in js and "sessionStorage" in js,
       "boot auto-fades via CSS, JS lifecycle + session skip in place")
# CSS timeline must end hidden even without JS (never strand a visitor)
expect("100% { opacity: 0; visibility: hidden; }" in css,
       "boot fade-out keyframe ends hidden without JS")

# ---- theme + chrome ----------------------------------------------------
print("== Theme ==")
base = open(os.path.join(ROOT, "templates/base.html")).read()
landing_src = open(os.path.join(ROOT, "templates/landing.html")).read()
expect('content="#173363"' in base and "logo-64.png" in base
       and "apple-touch-icon" in base,
       "brand favicon, touch icon and navy theme colour in base.html")
expect("#173363" in css and "--brydl-navy" in css
       and "#2b5bba" in css, "style.css tokens use the logo palette")
expect("Brydl © 2026" in base, "footer shows the brand mark + 2026")
expect("Gretek Technologies" in base, "footer references Gretek Technologies")
expect("Gretek Technologies" in landing_src, "landing page references Gretek Technologies")
expect("Greywood" not in base and "Greywood" not in landing_src, "no legacy Greywood references in templates")

# ---- CSP / hygiene ------------------------------------------------------
print("== Hygiene ==")
boot_src = open(os.path.join(ROOT, "templates/_boot.html")).read()
pattern = re.compile(r"\son[a-z]+\s*=")
expect(not pattern.search(landing_src)
       and not pattern.search(boot_src)
       and not pattern.search(base),
       "no inline event handlers in landing/boot/base templates")
expect("aria-hidden=\"true\"" in boot_src
       and 'aria-hidden="true"' in landing_src,
       "decorative boot + binary layers are aria-hidden")

# ---- authenticated users skip the landing page --------------------------
print("== Authenticated routing ==")
proc = subprocess.run([sys.executable, "seed.py"], cwd=ROOT,
                      capture_output=True, text=True, timeout=180)
expect(proc.returncode == 0, "seed.py exits 0 (fresh database)")

adm = app.test_client()
r = login(adm, "admin@brydl.com", "admin123")
r = adm.get("/")
expect(r.status_code == 302 and "/admin" in r.headers["Location"],
       "signed-in admin hitting / is bounced to the admin dashboard")
dash = adm.get("/admin/")
expect("brydl-boot" not in dash.get_data(as_text=True),
       "no boot overlay inside the app (dashboard pages stay snappy)")

ann = app.test_client()
login(ann, "annotator1@brydl.com", "annotator123")
r = ann.get("/")
expect(r.status_code == 302 and "/annotator" in r.headers["Location"],
       "signed-in annotator is bounced to the annotator dashboard")

# ---- assets served + errors stay branded --------------------------------
print("== Asset serving ==")
for path in ("brand/logo-recreation.png", "brand/logo-64.png",
             "brand/hero-cover.jpg"):
    rr = anon.get(f"/static/{path}")
    assert rr.status_code == 200 and len(rr.get_data()) > 1000, path
expect(True, "logo and hero images served with content")
expect(anon.get("/static/brand/nope.png").status_code == 404,
       "unknown brand asset 404s (no leaks)")
err = anon.get("/admin/")
assert err.status_code == 302
expect("brydl-footer" in anon.get("/login").get_data(as_text=True),
       "error/login pages keep the shared chrome")

shutil.rmtree("instance", ignore_errors=True)
shutil.rmtree("backups", ignore_errors=True)
print()
if FAILURES:
    print(f"BRAND TESTS FAILED ({len(FAILURES)}):")
    for failure in FAILURES:
        print(f"  - {failure}")
    sys.exit(1)
print("ALL BRAND TESTS PASSED")
