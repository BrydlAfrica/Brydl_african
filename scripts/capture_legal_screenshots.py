"""Capture screenshots for Brydl Contributor Legal & Policy Pack."""
import os
import subprocess
import sys
import time
from threading import Thread
from werkzeug.serving import make_server
from playwright.sync_api import sync_playwright

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
sys.path.insert(0, ROOT)

# Run seed.py to prepare fresh database
subprocess.run([sys.executable, "seed.py"], cwd=ROOT, check=True)

from app import create_app, db
from models import User

flask_app = create_app()

with flask_app.app_context():
    # Create an annotator who hasn't accepted Version 2.1 yet
    u = User.query.filter_by(email="ann_legal_preview@brydl.com").first()
    if not u:
        u = User(
            name="Zainab Abubakar",
            email="ann_legal_preview@brydl.com",
            role="annotator",
            terms_version="",
        )
        u.set_password("PreviewPass123!")
        db.session.add(u)
        db.session.commit()
    else:
        u.terms_version = ""
        u.set_password("PreviewPass123!")
        db.session.commit()

# Start Flask WSGI server in background thread
class ServerThread(Thread):
    def __init__(self, app_to_run, host="127.0.0.1", port=5005):
        super().__init__()
        self.server = make_server(host, port, app_to_run)
        self.ctx = app_to_run.app_context()
        self.ctx.push()

    def run(self):
        self.server.serve_forever()

    def shutdown(self):
        self.server.shutdown()


server = ServerThread(flask_app, "127.0.0.1", 5005)
server.start()
time.sleep(1)

out_dir = os.path.join(ROOT, "docs", "screenshots")
os.makedirs(out_dir, exist_ok=True)

try:
    with sync_playwright() as p:
        browser = p.chromium.launch()

        # 1. Desktop Screenshots (1280x800)
        desktop_ctx = browser.new_context(viewport={"width": 1280, "height": 800})
        page = desktop_ctx.new_page()

        # Public Terms
        page.goto("http://127.0.0.1:5005/terms", wait_until="networkidle")
        page.screenshot(path=os.path.join(out_dir, "01_desktop_terms_v2_1.png"), full_page=True)
        print("Captured: 01_desktop_terms_v2_1.png")

        # Public Privacy Notice
        page.goto("http://127.0.0.1:5005/privacy", wait_until="networkidle")
        page.screenshot(path=os.path.join(out_dir, "02_desktop_privacy_v1_0.png"), full_page=True)
        print("Captured: 02_desktop_privacy_v1_0.png")

        # Public Contributor Policies
        page.goto("http://127.0.0.1:5005/policies", wait_until="networkidle")
        page.screenshot(path=os.path.join(out_dir, "03_desktop_policies_v1_0.png"), full_page=True)
        print("Captured: 03_desktop_policies_v1_0.png")

        # Annotator Terms Gate (Unaccepted)
        page.goto("http://127.0.0.1:5005/login", wait_until="networkidle")
        page.fill('input[name="email"]', "ann_legal_preview@brydl.com")
        page.fill('input[name="password"]', "PreviewPass123!")
        page.click('button[type="submit"]')
        page.wait_for_load_state("networkidle")

        # Should be gated to /annotator/terms
        page.screenshot(path=os.path.join(out_dir, "04_desktop_annotator_terms_gate.png"), full_page=True)
        print("Captured: 04_desktop_annotator_terms_gate.png")

        # Annotator Policies View
        page.goto("http://127.0.0.1:5005/annotator/policies", wait_until="networkidle")
        page.screenshot(path=os.path.join(out_dir, "05_desktop_annotator_policies.png"), full_page=True)
        print("Captured: 05_desktop_annotator_policies.png")

        # Accept terms
        page.goto("http://127.0.0.1:5005/annotator/terms", wait_until="networkidle")
        page.check('#terms-understand')
        page.click('button[type="submit"]')
        page.wait_for_load_state("networkidle")

        # Accepted confirmation on terms page
        page.goto("http://127.0.0.1:5005/annotator/terms", wait_until="networkidle")
        page.screenshot(path=os.path.join(out_dir, "06_desktop_terms_accepted_history.png"), full_page=True)
        print("Captured: 06_desktop_terms_accepted_history.png")

        desktop_ctx.close()

        # 2. Mobile Screenshots (390x844 - iPhone 12/13/14)
        mobile_ctx = browser.new_context(
            viewport={"width": 390, "height": 844},
            is_mobile=True,
            user_agent="Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1"
        )
        m_page = mobile_ctx.new_page()

        # Mobile Terms
        m_page.goto("http://127.0.0.1:5005/terms", wait_until="networkidle")
        m_page.screenshot(path=os.path.join(out_dir, "07_mobile_terms_v2_1.png"), full_page=True)
        print("Captured: 07_mobile_terms_v2_1.png")

        # Mobile Privacy Notice
        m_page.goto("http://127.0.0.1:5005/privacy", wait_until="networkidle")
        m_page.screenshot(path=os.path.join(out_dir, "08_mobile_privacy_v1_0.png"), full_page=True)
        print("Captured: 08_mobile_privacy_v1_0.png")

        # Mobile Policies
        m_page.goto("http://127.0.0.1:5005/policies", wait_until="networkidle")
        m_page.screenshot(path=os.path.join(out_dir, "09_mobile_policies_v1_0.png"), full_page=True)
        print("Captured: 09_mobile_policies_v1_0.png")

        # Mobile Login & Gating
        m_page.goto("http://127.0.0.1:5005/login", wait_until="networkidle")
        m_page.fill('input[name="email"]', "ann_legal_preview@brydl.com")
        m_page.fill('input[name="password"]', "PreviewPass123!")
        m_page.click('button[type="submit"]')
        m_page.wait_for_load_state("networkidle")
        m_page.goto("http://127.0.0.1:5005/annotator/terms", wait_until="networkidle")
        m_page.screenshot(path=os.path.join(out_dir, "10_mobile_annotator_terms_accepted.png"), full_page=True)
        print("Captured: 10_mobile_annotator_terms_accepted.png")

        mobile_ctx.close()
        browser.close()

finally:
    server.shutdown()
    print("Screenshot capture complete.")
