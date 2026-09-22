import os, sys
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))
import fake_gh
srv, gh_base = fake_gh.start()
import tempfile
os.environ.update(DATA_DIR=tempfile.mkdtemp(), DISCORD_TOKEN="x", OPENROUTER_API_KEY="k", DISCORD_ALLOWED_USER_IDS="1", GITHUB_TOKEN="tok", GITHUB_API_URL=gh_base, GITHUB_ALLOWED_REPOS="me/proj")
from config import Config
import tools as T

asked = []
answer = {"v": True}
def approve(title, detail): asked.append((title, detail)); return answer["v"]
def make(mode):
    os.environ["APPROVAL_MODE"] = mode
    fake_gh.reset(); asked.clear(); answer["v"] = True
    return {t.name: t for t in T.build_tools(Config.from_env(), approve)}
ok = lambda s: print("PASS", s)

tl = make("publish")
print("tools:", sorted(tl))
print(tl["gh_repos"]())
print(tl["gh_browse"](repo="proj"))
out = tl["gh_browse"](repo="proj", path="README.md"); assert "   2| Hello wrold" in out, out
ok("browse dir + numbered file")
assert "Not found" in tl["gh_browse"](repo="proj", path="nope.md")
assert "not in the allowed" in tl["gh_browse"](repo="private")
ok("404 / 403 turned into readable errors")
print(tl["gh_commits"](repo="proj")); print(tl["gh_issues"](repo="proj")); print(tl["gh_pr"](repo="proj", number=7)[:300])

# --- write guardrails
r = tl["gh_edit_file"](repo="proj", path="README.md", old_text="Hello", new_text="Hi", branch="main", commit_message="x")
assert r.startswith("ERROR: Writes are only allowed"), r
r = tl["gh_edit_file"](repo="proj", path="README.md", old_text="wrold", new_text="world", branch="agent/fix", commit_message="x")
assert "matches 2 places" in r, r
r = tl["gh_edit_file"](repo="proj", path="README.md", old_text="nothere", new_text="x", branch="agent/fix", commit_message="x")
assert "not found" in r, r
ok("branch guard, ambiguous match, missing match")

r = tl["gh_edit_file"](repo="proj", path="README.md", old_text="Hello wrold\n", new_text="Hello world\n", branch="agent/fix", commit_message="Fix typo")
print(r); assert r.startswith("Committed c0ffee1")
assert fake_gh.STATE["files"]["agent/fix"]["README.md"].splitlines()[1] == "Hello world"
assert fake_gh.STATE["files"]["main"]["README.md"].splitlines()[1] == "Hello wrold"
assert not asked, "publish mode must not ask for commits"
ok("edit auto-created branch, main untouched, no approval in 'publish' mode")

# second edit on existing branch (sha comes from the branch)
r = tl["gh_edit_file"](repo="proj", path="README.md", old_text="again", new_text="once more", branch="agent/fix", commit_message="2")
assert r.startswith("Committed"), r
r = tl["gh_write_file"](repo="proj", path="docs/new.md", content="# new\n", branch="agent/fix", commit_message="add doc")
assert r.startswith("Committed"), r
ok("second edit + new file on existing branch")

r = tl["gh_open_pr"](repo="proj", title="Fix typo", body="Fixes a typo", head="agent/fix")
assert "Opened PR #7" in r, r; assert asked and asked[0][0] == "Open a pull request"; print(asked[0][1])
assert "Opened by pi-agent" in fake_gh.STATE["prs"][0]["body"]
ok("PR opened after approval, footer added")

asked.clear(); answer["v"] = False
r = tl["gh_review_pr"](repo="proj", number=7, body="Looks fine")
assert r.startswith("DENIED"); assert not fake_gh.STATE["reviews"]
ok("denied review is NOT posted")
answer["v"] = True
r = tl["gh_review_pr"](repo="proj", number=7, body="Looks fine")
assert "Review posted" in r and fake_gh.STATE["reviews"][0]["event"] == "COMMENT"
ok("approved review posted as COMMENT only")

# --- approval mode = all
tl = make("all"); answer["v"] = False
r = tl["gh_edit_file"](repo="proj", path="README.md", old_text="Bye", new_text="Ciao", branch="agent/b", commit_message="m")
assert r.startswith("DENIED") and "agent/b" not in fake_gh.STATE["branches"], (r, fake_gh.STATE["branches"])
print(asked[0][1])
ok("mode=all: denied commit creates no branch and no commit")

# --- approval mode = none
tl = make("none"); r = tl["gh_edit_file"](repo="proj", path="README.md", old_text="Bye", new_text="Ciao", branch="agent/c", commit_message="m")
assert r.startswith("Committed") and not asked
ok("mode=none")

# --- read-only + allowlist
os.environ["GITHUB_WRITE"] = "0"; tl = make("publish"); assert "gh_edit_file" not in tl and "gh_browse" in tl
ok("GITHUB_WRITE=0 removes write tools")
os.environ["GITHUB_WRITE"] = "1"; os.environ["GITHUB_ALLOWED_REPOS"] = "me/other"; tl = make("publish")
assert "not in the allowed" in tl["gh_browse"](repo="proj"); os.environ["GITHUB_ALLOWED_REPOS"] = "me/proj"
ok("allowlist enforced")

# --- no token -> no github tools
os.environ["GITHUB_TOKEN"] = ""; tl = {t.name for t in T.build_tools(Config.from_env(), approve)}
assert tl == {"web_search", "visit_webpage", "pi_status"}, tl
ok("no token -> only web + status tools")

# --- web safety
tl = make("publish")
for bad in ["http://127.0.0.1:8000/", "http://localhost/", "http://192.168.1.1/", "http://169.254.169.254/latest/meta-data", "file:///etc/passwd", "http://[::1]/"]:
    r = tl["visit_webpage"](url=bad); assert r.startswith("ERROR"), (bad, r)
ok("private / loopback / metadata / file:// URLs refused")

# page parsing via local server (public check patched out only for this test)
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
class P(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self):
        if self.path == "/redir": self.send_response(302); self.send_header("Location", "/page"); self.end_headers(); return
        html = b"<html><head><style>x{}</style><script>evil()</script></head><body><nav>menu</nav><h1>Title</h1><p>Hello <b>world</b></p>" + b"<p>filler</p>" * 5000 + b"</body></html>"
        self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8"); self.end_headers(); self.wfile.write(html)
s2 = HTTPServer(("127.0.0.1", 0), P); threading.Thread(target=s2.serve_forever, daemon=True).start()
orig = T._check_public_url; T._check_public_url = lambda url: None
txt = T.fetch_page_text(f"http://127.0.0.1:{s2.server_address[1]}/redir", 2000)
T._check_public_url = orig
assert "# Title" in txt and "**world**" in txt and "evil()" not in txt and "menu" not in txt and "truncated" in txt and len(txt) < 2200, txt[:300]
ok("HTML -> markdown, scripts/nav stripped, redirect followed, truncated")

print(tl["pi_status"]())
