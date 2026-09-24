import os, re, sys, tempfile
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))
import fake_gh
srv, gh_base = fake_gh.start()
os.environ.update(DISCORD_TOKEN="x", OPENROUTER_API_KEY="k", DISCORD_ALLOWED_USER_IDS="42", GITHUB_TOKEN="tok",
    GITHUB_API_URL=gh_base, GITHUB_ALLOWED_REPOS="me/proj", DATA_DIR=tempfile.mkdtemp(), SELF_REPO="me/proj")
from config import Config
import tools as T
ok = lambda s: print("PASS", s)

asked = []; verdict = {"v": True}
def approve(t, d): asked.append((t, d)); return verdict["v"]

def make(protected="", approval="publish", self_repo="me/proj"):
    os.environ["APPROVAL_MODE"] = approval
    os.environ["SELF_REPO"] = self_repo
    os.environ["PROTECTED_PATHS"] = protected
    fake_gh.reset(); fake_gh.STATE["files"]["main"]["config.py"] = "SECRET=1\n"
    fake_gh.STATE["files"]["main"]["notes/todo.md"] = "- fix later\n"
    asked.clear(); verdict["v"] = True
    cfg = Config.from_env()
    return cfg, {t.name: t for t in T.build_tools(cfg, approve, None, T.RunState())}

# ---- protected_reason unit checks
cfg, _ = make()
assert T.protected_reason(cfg, "tools.py", exists=True)
assert T.protected_reason(cfg, "checkouts.py", exists=True)
assert T.protected_reason(cfg, "sandbox.py", exists=True)
assert T.protected_reason(cfg, "main.py", exists=True)
assert T.protected_reason(cfg, ".github/workflows/ci.yml", exists=True)
assert T.protected_reason(cfg, "requirements-dev.txt", exists=True)
assert T.protected_reason(cfg, "tests/test_llm.py", exists=True)
assert T.protected_reason(cfg, "tests/test_llm.py", exists=False) is None  # a NEW test file is fine
assert T.protected_reason(cfg, "TOOLS.PY", exists=True), "match should be case-insensitive"
assert T.protected_reason(cfg, "../../etc/passwd", exists=False)
assert T.protected_reason(cfg, "watcher.py", exists=True) is None
assert T.protected_reason(cfg, "README.md", exists=True) is None
ok("protected_reason: locked files, new tests allowed, path traversal blocked, case-insensitive")

cfg, _ = make(protected="notes/*,secrets.txt")
assert T.protected_reason(cfg, "notes/todo.md", exists=True)
assert T.protected_reason(cfg, "other.md", exists=True) is None
ok("PROTECTED_PATHS: extra user-configured globs enforced")

# ---- gh_edit_file / gh_write_file refuse locked files, only on SELF_REPO
cfg, tl = make()
r = tl["gh_edit_file"](repo="proj", path="tools.py", old_text="import", new_text="import os", branch="agent/x", commit_message="m")
assert r.startswith("ERROR:") and "locked" in r and not asked
r = tl["gh_write_file"](repo="proj", path="config.py", content="x", branch="agent/x", commit_message="m")
assert r.startswith("ERROR:") and "locked" in r
r = tl["gh_edit_file"](repo="proj", path="README.md", old_text="Proj", new_text="Project", branch="agent/x", commit_message="m")
assert r.startswith("Committed"), r
ok("gh_edit_file/gh_write_file refuse locked files on the self-repo, allow others")

# a repo that ISN'T the self-repo has no locked files at all
os.environ["GITHUB_ALLOWED_REPOS"] = "me/proj,me/other"
cfg, tl = make(self_repo="me/other")
fake_gh.STATE["files"]["main"]["tools.py"] = "print('hi')\n"  # this repo's own tools.py, unrelated to the bot's code
r = tl["gh_edit_file"](repo="proj", path="tools.py", old_text="hi", new_text="bye", branch="agent/x", commit_message="m")
assert r.startswith("Committed"), r
os.environ["GITHUB_ALLOWED_REPOS"] = "me/proj"
ok("locked-file guard only applies to SELF_REPO, not other repos")

# ---- gh_open_pr: forced draft + branch-wide file guard on self repo
cfg, tl = make()
tl["gh_edit_file"](repo="proj", path="README.md", old_text="Proj", new_text="Project", branch="agent/readme", commit_message="m")
asked.clear()
r = tl["gh_open_pr"](repo="proj", title="Docs", body="fix readme", head="agent/readme")
assert r.startswith("Opened DRAFT PR"), r
assert asked and "DRAFT: changes to me" in asked[0][1]
assert fake_gh.STATE["prs"][-1]["draft"] is True
ok("gh_open_pr: PRs on the self-repo are forced to draft, flagged in the approval prompt")

# branch touches a locked file even though gh_edit_file was bypassed (simulate direct API tampering)
fake_gh.STATE["files"]["agent/sneaky"] = dict(fake_gh.STATE["files"]["main"]); fake_gh.STATE["branches"]["agent/sneaky"] = "sha-x"
fake_gh.STATE["files"]["agent/sneaky"]["tools.py"] = "TAMPERED\n"
r = tl["gh_open_pr"](repo="proj", title="sneaky", body="x", head="agent/sneaky")
assert r.startswith("ERROR:") and "locked" in r and not fake_gh.STATE["prs"][-1].get("title") == "sneaky"
ok("gh_open_pr: refuses to open a PR whose diff touches a locked file, even if committed another way")

# non-self repo PRs are NOT forced to draft
cfg2, tl2 = make(self_repo="me/other")
tl2["gh_edit_file"](repo="proj", path="README.md", old_text="Proj", new_text="Project2", branch="agent/r2", commit_message="m")
asked.clear()
r = tl2["gh_open_pr"](repo="proj", title="t", body="b", head="agent/r2")
assert r.startswith("Opened PR") and not r.startswith("Opened DRAFT")
assert fake_gh.STATE["prs"][-1]["draft"] is False
ok("gh_open_pr: normal (non-draft) on a repo that isn't the self-repo")

# ---- gh_issue / gh_open_issue
cfg, tl = make()
fake_gh.STATE["issues"] = {1: {"number": 1, "title": "Bug", "state": "open", "user": {"login": "x"}, "labels": [{"name": "bug"}], "body": "desc here"}}
fake_gh.STATE["issue_comments"] = {1: [{"user": {"login": "y"}, "body": "me too"}]}
out = tl["gh_issue"](repo="proj", number=1); print(out)
assert "Bug" in out and "desc here" in out and "me too" in out
ok("gh_issue reads title/body/comments")

state = T.RunState()  # fresh state, issue_budget=0 by default
tl2 = {t.name: t for t in T.build_tools(cfg, approve, None, state)}
assert tl2["gh_open_issue"](repo="proj", title="X", body="Y").startswith("ERROR") and "can't file issues" in tl2["gh_open_issue"](repo="proj", title="X", body="Y")
ok("gh_open_issue refuses when issue_budget is 0 (normal chat tasks)")

state.issue_budget = 2
r = tl2["gh_open_issue"](repo="proj", title="Flaky retries in llm.py", body="Evidence: 12 llm_transient events this week.")
assert "Filed issue #" in r and state.issue_budget == 1, r
assert fake_gh.STATE["issues"][max(fake_gh.STATE["issues"])]["labels"] == ["self-review"]
ok(f"gh_open_issue files on the self-repo, decrements budget -> {r}")

r = tl2["gh_open_issue"](repo="proj", title="Flaky Retries In Llm.Py!!", body="dup")  # near-duplicate title
assert "already exists" in r and state.issue_budget == 1
ok("gh_open_issue skips near-duplicate open issues (case/punctuation-insensitive)")

os.environ["GITHUB_ALLOWED_REPOS"] = "me/proj,me/other"  # "me/other" is allowed but is NOT the self-repo
cfg4, tl4 = make(self_repo="me/proj")
os.environ["GITHUB_ALLOWED_REPOS"] = "me/proj,me/other"; cfg4 = Config.from_env()
tl4 = {t.name: t for t in T.build_tools(cfg4, approve, None, T.RunState(issue_budget=1))}
r = tl4["gh_open_issue"](repo="other", title="X", body="Y")
assert "own source repository" in r, r
os.environ["GITHUB_ALLOWED_REPOS"] = "me/proj"
ok("gh_open_issue refuses repos other than SELF_REPO (even if allowlisted)")

for _ in range(3):
    r = tl2["gh_open_issue"](repo="proj", title=f"Distinct issue {_}", body="b")
assert state.issue_budget == 0 and "can't file issues" in tl2["gh_open_issue"](repo="proj", title="one more", body="b")
ok("gh_open_issue: per-run budget enforced (can't file unlimited issues)")

# mode=all requires approval to file an issue too
cfg3, tl3 = make(approval="all")
state3 = T.RunState(issue_budget=2)
tl3 = {t.name: t for t in T.build_tools(cfg3, approve, None, state3)}
verdict["v"] = False; asked.clear()
r = tl3["gh_open_issue"](repo="proj", title="Needs approval", body="b")
assert r.startswith("DENIED") and asked
ok("APPROVAL_MODE=all: filing an issue needs approval too")
