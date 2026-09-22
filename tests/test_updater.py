import json, os, shutil, subprocess, sys, tempfile, time
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))
from updater import Updater, UpdateError, slug_from_url, ci_state
ok = lambda s: print("PASS", s)

def git(repo, *args):
    r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
    assert r.returncode == 0, (args, r.stderr)
    return r.stdout.strip()

def make_repo():
    base = tempfile.mkdtemp()
    origin, work = f"{base}/origin", f"{base}/work"
    subprocess.run(["git", "init", "-q", "--bare", origin], check=True)
    subprocess.run(["git", "clone", "-q", origin, work], check=True)
    git(work, "config", "user.email", "t@t.com"); git(work, "config", "user.name", "t")
    os.makedirs(f"{work}/tests")
    open(f"{work}/requirements.txt", "w").write("smolagents\n")
    open(f"{work}/tools.py", "w").write("x = 1\n")
    open(f"{work}/README.md", "w").write("hi\n")
    open(f"{work}/.gitignore", "w").write("data/\n")  # mirrors the real repo: state/DB files must not count as dirty
    git(work, "checkout", "-q", "-b", "main")
    git(work, "add", "-A"); git(work, "commit", "-q", "-m", "initial")
    git(work, "push", "-q", "-u", "origin", "main")
    return base, origin, work

def push_from_elsewhere(base, origin, files: dict, branch="main"):
    """Simulate 'someone merged a PR': commit from a THIRD clone and push, without touching `work`."""
    other = tempfile.mkdtemp()  # unique dir per call: push_from_elsewhere may be called more than once per base
    subprocess.run(["git", "clone", "-q", "-b", branch, origin, other], check=True)
    git(other, "config", "user.email", "t@t.com"); git(other, "config", "user.name", "t")
    for path, content in files.items():
        full = f"{other}/{path}"; os.makedirs(os.path.dirname(full) or ".", exist_ok=True)
        open(full, "w").write(content)
    git(other, "add", "-A"); git(other, "commit", "-q", "-m", "remote change")
    git(other, "push", "-q", "origin", f"HEAD:{branch}")
    return git(other, "rev-parse", "HEAD")

OK_SMOKE = [sys.executable, "-c", "print('smoke ok')"]
FAIL_SMOKE = [sys.executable, "-c", "import sys; sys.exit(1)"]

# ---------- slug_from_url
assert slug_from_url("https://github.com/me/proj.git") == "me/proj"
assert slug_from_url("git@github.com:me/proj.git") == "me/proj"
assert slug_from_url("https://github.com/me/proj") == "me/proj"
assert slug_from_url("/local/path") == ""
ok("slug_from_url: https, ssh, no-.git, non-github")

# ---------- not a git checkout
d = tempfile.mkdtemp(); u = Updater(d)
try: u.plan(); assert False
except UpdateError as e: assert "clone your repo" in str(e).lower() or "git checkout" in str(e).lower()
ok("plan(): not a git checkout -> clear error")

# ---------- up to date
base, origin, work = make_repo(); u = Updater(work, smoke_cmd=OK_SMOKE)
assert u.plan() is None
ok("plan(): up to date -> None")

# ---------- new commit available, apply succeeds, deps_changed / launcher_changed flags
sha = push_from_elsewhere(base, origin, {"tools.py": "x = 2\n", "requirements.txt": "smolagents\nnew-dep\n"})
plan = u.plan()
assert plan and plan.target == sha and "requirements.txt" in plan.files and plan.deps_changed and not plan.launcher_changed
assert len(plan.commits) == 1
ok(f"plan(): detects 1 new commit, deps_changed=True -> {plan.commits}")

installed = []
u2 = Updater(work, smoke_cmd=OK_SMOKE, pip=lambda: installed.append(True))
before = u2.current()
u2.apply(plan)
assert u2.current() == sha[:7] and installed == [True]
assert open(f"{work}/tools.py").read() == "x = 2\n"
st = u2.read_state(); assert st["state"] == "pending" and st["previous"].startswith(before)
ok("apply(): fast-forwards, installs deps (requirements changed), state -> pending")

# ---------- report_boot: pending -> ok
u2.report_boot()
assert u2.read_state()["state"] == "ok"
assert u2.report_boot() is None  # only reports once
ok("report_boot(): pending -> ok, only reports once")

# ---------- failing smoke test rolls back automatically
sha2 = push_from_elsewhere(base, origin, {"tools.py": "x = 3\n"})
plan2 = u.plan(); assert plan2.target == sha2
u3 = Updater(work, smoke_cmd=FAIL_SMOKE)
before2 = u3.current()
try: u3.apply(plan2); assert False
except UpdateError as e: assert "rolled back" in str(e)
assert u3.current() == before2, "must be back on the pre-update commit"
assert open(f"{work}/tools.py").read() == "x = 2\n"
st = u3.read_state(); assert st["state"] == "rolled_back"
ok("apply(): failing smoke test -> automatic rollback, file contents restored")

# apply()'s own smoke-test rollback is already reported synchronously via the raised UpdateError
# (the caller shows it to the user right away), so report_boot() has nothing further to say here.
assert u3.report_boot() is None
ok("report_boot(): a rollback already reported via the raised error is not reported again")

# ---------- failing pip install also rolls back
sha3 = push_from_elsewhere(base, origin, {"requirements.txt": "smolagents\nbroken-dep\n"})
plan3 = u.plan()
def bad_pip(): raise UpdateError("pip explode")
u4 = Updater(work, smoke_cmd=OK_SMOKE, pip=bad_pip)
before3 = u4.current()
try: u4.apply(plan3); assert False
except UpdateError as e: assert "pip explode" in str(e)
assert u4.current() == before3
ok("apply(): pip failure also triggers rollback")

# ---------- manual rollback
sha4 = push_from_elsewhere(base, origin, {"tools.py": "x = 4\n"})
plan4 = u.plan()
u5 = Updater(work, smoke_cmd=OK_SMOKE, pip=lambda: None)
before4 = u5.current()
u5.apply(plan4)
assert u5.current() == sha4[:7]
back = u5.rollback()
assert back == before4 and u5.current() == before4
try: u5.rollback(); assert False  # can't roll back twice
except UpdateError as e: assert "nothing to roll back" in str(e).lower()
ok("rollback(): manual rollback works, and can't be repeated")

# ---------- dirty working tree refused
open(f"{work}/scratch.txt", "w").write("oops\n")
try: u.plan(); assert False
except UpdateError as e: assert "uncommitted" in str(e).lower()
os.remove(f"{work}/scratch.txt")
ok("plan(): refuses with uncommitted local changes")

# ---------- diverged history (not fast-forward)
base2, origin2, work2 = make_repo()
git(work2, "commit", "--allow-empty", "-q", "-m", "local only")  # a LOCAL commit not on origin
push_from_elsewhere(base2, origin2, {"tools.py": "remote too\n"})  # AND origin also moved
u6 = Updater(work2)
try: u6.plan(); assert False
except UpdateError as e: assert "diverged" in str(e).lower()
ok("plan(): diverged / non-fast-forward history refused")

# ---------- detached HEAD
base3, origin3, work3 = make_repo()
sha0 = git(work3, "rev-parse", "HEAD")
git(work3, "checkout", "-q", sha0)
u7 = Updater(work3)
try: u7.plan(); assert False
except UpdateError as e: assert "detached" in str(e).lower()
ok("plan(): detached HEAD refused")

# ---------- state persists across a fresh Updater instance (simulates a restart)
base4, origin4, work4 = make_repo(); u8 = Updater(work4, smoke_cmd=OK_SMOKE, pip=lambda: None)
sha5 = push_from_elsewhere(base4, origin4, {"tools.py": "x = 9\n"})
u8.apply(u8.plan())
u9 = Updater(work4)  # brand-new instance, same folder
assert u9.report_boot() is not None
ok("update state survives a process restart (read from disk)")

print(subprocess.run(["python3", "-c", "import sys; sys.path.insert(0,'.'); from updater import Updater; print('module self-check ok')"], cwd=str(__import__('pathlib').Path(__file__).resolve().parent.parent), capture_output=True, text=True).stdout)
