"""SEO, AEO, GEO and Search Engine Indexing Verification Tests.

Validates sitemap.xml, robots.txt, canonical links, Open Graph metadata,
Twitter Cards, JSON-LD Schema.org graphs (Organization, WebApplication, FAQPage),
AEO question answering, and X-Robots-Tag header protections on private routes.
"""
import json
import os
import re
import sys
import xml.etree.ElementTree as ET

ROOT = os.environ.get("BRYDL_ROOT") or os.path.abspath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
)
os.chdir(ROOT)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

FAILURES = []


def expect(condition, message):
    if condition:
        print(f"   ok: {message}")
    else:
        FAILURES.append(message)
        print(f"   FAIL: {message}")


from app import create_app  # noqa: E402

app = create_app()
client = app.test_client()

# ==============================================================================
# 1. ROBOTS.TXT VERIFICATION
# ==============================================================================
print("== 1. Robots.txt ==")
r = client.get("/robots.txt")
expect(r.status_code == 200, "GET /robots.txt returns 200 OK")
expect("text/plain" in r.content_type, "robots.txt Content-Type is text/plain")
robots_text = r.get_data(as_text=True)

expect("User-agent: *" in robots_text, "Contains default User-agent: *")
expect("Allow: /" in robots_text and "Allow: /privacy" in robots_text, "Allows public routes")
expect("Disallow: /admin/" in robots_text, "Disallows /admin/")
expect("Disallow: /annotator/" in robots_text, "Disallows /annotator/")
expect("Disallow: /pm/" in robots_text, "Disallows /pm/")
expect("Disallow: /staff/" in robots_text, "Disallows /staff/")
expect("Disallow: /client/" in robots_text, "Disallows /client/")
expect("Disallow: /api/" in robots_text, "Disallows /api/")
expect("Disallow: /redlab/" in robots_text, "Disallows /redlab/")
expect("Disallow: /login" in robots_text, "Disallows /login")

# Generative & AI Search Crawlers
expect("User-agent: GPTBot" in robots_text, "Explicitly accommodates GPTBot")
expect("User-agent: OAI-SearchBot" in robots_text, "Explicitly accommodates OAI-SearchBot")
expect("User-agent: PerplexityBot" in robots_text, "Explicitly accommodates PerplexityBot")
expect("User-agent: ClaudeBot" in robots_text, "Explicitly accommodates ClaudeBot")
expect("User-agent: Google-Extended" in robots_text, "Explicitly accommodates Google-Extended")
expect("User-agent: Applebot" in robots_text, "Explicitly accommodates Applebot")

# Sitemap link in robots.txt
expect(re.search(r"Sitemap:\s*https?://.+/sitemap\.xml", robots_text) is not None,
       "Declares Sitemap URL pointing to /sitemap.xml")

# ==============================================================================
# 2. SITEMAP.XML VERIFICATION
# ==============================================================================
print("== 2. Sitemap.xml ==")
r = client.get("/sitemap.xml")
expect(r.status_code == 200, "GET /sitemap.xml returns 200 OK")
expect("xml" in r.content_type, "sitemap.xml Content-Type is application/xml or text/xml")
sitemap_xml = r.get_data(as_text=True)

try:
    root = ET.fromstring(sitemap_xml)
    expect(root.tag.endswith("urlset"), "Root tag is <urlset>")
    urls = [elem.text for elem in root.findall(".//{http://www.sitemaps.org/schemas/sitemap/0.9}loc")]
    expect(any(u.endswith("/") for u in urls), "Sitemap lists homepage /")
    expect(any(u.endswith("/privacy") for u in urls), "Sitemap lists /privacy")
    expect(all(not any(p in u for p in ("/admin", "/annotator", "/pm", "/api", "/login")) for u in urls),
           "Sitemap contains zero private routes")
except Exception as e:
    expect(False, f"Sitemap XML parsing failed: {e}")

# ==============================================================================
# 3. X-ROBOTS-TAG SECURITY & INDEXING HEADERS
# ==============================================================================
print("== 3. X-Robots-Tag Headers ==")
r_home = client.get("/")
expect(r_home.headers.get("X-Robots-Tag") == "index, follow",
       "GET / returns X-Robots-Tag: index, follow")

r_priv = client.get("/privacy")
expect(r_priv.headers.get("X-Robots-Tag") == "index, follow",
       "GET /privacy returns X-Robots-Tag: index, follow")

r_sitemap = client.get("/sitemap.xml")
expect(r_sitemap.headers.get("X-Robots-Tag") == "index, follow",
       "GET /sitemap.xml returns X-Robots-Tag: index, follow")

r_login = client.get("/login")
expect(r_login.headers.get("X-Robots-Tag") == "noindex, nofollow",
       "GET /login returns X-Robots-Tag: noindex, nofollow")

r_admin = client.get("/admin/")
expect(r_admin.headers.get("X-Robots-Tag") == "noindex, nofollow",
       "GET /admin/ returns X-Robots-Tag: noindex, nofollow")

# ==============================================================================
# 4. LANDING PAGE METADATA, OPEN GRAPH & TWITTER CARDS
# ==============================================================================
print("== 4. Landing Page Metadata & Social Cards ==")
landing_html = r_home.get_data(as_text=True)

expect('<link rel="canonical"' in landing_html, "Canonical link tag present")
expect('<meta name="description"' in landing_html, "Meta description tag present")
expect('property="og:title"' in landing_html, "Open Graph og:title present")
expect('property="og:description"' in landing_html, "Open Graph og:description present")
expect('property="og:image"' in landing_html, "Open Graph og:image present")
expect('property="og:url"' in landing_html, "Open Graph og:url present")
expect('property="og:site_name" content="Brydl AI"' in landing_html, "Open Graph og:site_name is Brydl AI")
expect('name="twitter:card" content="summary_large_image"' in landing_html, "Twitter summary_large_image card present")
expect('name="twitter:title"' in landing_html, "Twitter title present")
expect('name="twitter:description"' in landing_html, "Twitter description present")
expect('name="twitter:image"' in landing_html, "Twitter image present")

# ==============================================================================
# 5. STRUCTURED DATA (JSON-LD) VALIDATION
# ==============================================================================
print("== 5. JSON-LD Structured Data ==")
json_ld_matches = re.findall(r'<script type="application/ld\+json">(.*?)</script>', landing_html, re.DOTALL)
expect(len(json_ld_matches) > 0, "Landing page contains JSON-LD script block")

found_org = False
found_webapp = False
found_faq = False
faq_questions = []

for block in json_ld_matches:
    try:
        data = json.loads(block.strip())
        graph = data.get("@graph", [data])
        for item in graph:
            item_type = item.get("@type")
            if item_type == "Organization":
                found_org = True
                expect(item.get("name") == "Gretek Technologies", "Organization name is Gretek Technologies")
            elif item_type in ("WebApplication", "SoftwareApplication"):
                found_webapp = True
                expect("Brydl" in item.get("name"), "SoftwareApplication name contains Brydl")
                expect("featureList" in item, "SoftwareApplication contains featureList")
            elif item_type == "FAQPage":
                found_faq = True
                for q in item.get("mainEntity", []):
                    faq_questions.append(q.get("name"))
    except Exception as e:
        expect(False, f"JSON-LD syntax error: {e}")

expect(found_org, "JSON-LD contains Organization entity for Gretek Technologies")
expect(found_webapp, "JSON-LD contains WebApplication entity for Brydl AI")
expect(found_faq, "JSON-LD contains FAQPage entity")

# Verify the 6 core AEO questions
expected_questions = [
    "What is Brydl?",
    "What does Brydl do?",
    "Who is Brydl designed for?",
    "What problems does Brydl solve?",
    "Why should someone choose Brydl?",
    "Where can someone access Brydl?",
]
for q in expected_questions:
    expect(q in faq_questions, f"FAQPage schema contains question: '{q}'")

# ==============================================================================
# 6. AEO FAQ SECTION RENDERING
# ==============================================================================
print("== 6. AEO FAQ HTML Section ==")
expect('id="faq"' in landing_html, "HTML contains FAQ section (#faq)")
for q in expected_questions:
    expect(q in landing_html, f"Landing HTML renders FAQ question: '{q}'")

expect("Gretek Technologies" in landing_html, "Landing HTML references Gretek Technologies")
expect("Greywood" not in landing_html, "Landing HTML contains no legacy Greywood references")

# ==============================================================================
# 7. PRIVACY PAGE METADATA & STRUCTURED DATA
# ==============================================================================
print("== 7. Privacy Page Metadata ==")
priv_html = r_priv.get_data(as_text=True)
expect('<link rel="canonical"' in priv_html, "Privacy page contains canonical link")
expect('<meta name="description"' in priv_html, "Privacy page contains meta description")
expect("NDPR" in priv_html, "Privacy page references NDPR")
priv_json_ld = re.findall(r'<script type="application/ld\+json">(.*?)</script>', priv_html, re.DOTALL)
expect(len(priv_json_ld) > 0, "Privacy page contains JSON-LD structured data")

print()
if FAILURES:
    print(f"SEO / AEO / GEO TESTS FAILED ({len(FAILURES)}):")
    for failure in FAILURES:
        print(f"  - {failure}")
    sys.exit(1)
print("ALL SEO / AEO / GEO TESTS PASSED (100%)")
