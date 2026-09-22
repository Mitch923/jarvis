import json, os, subprocess, sys, tempfile
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))
from run import maybe_rollback, MAX_BOOT_ATTEMPTS
ok = lambda s: print("PASS", s)

def git(repo, *args):
    r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    assert r.returncode == 0, (args, r.stderr)
    return r.stdout.strip()

def repo_with_state(state):
    d = tempfile.mkdtemp()
    subprocess.run(["git", "init", "-q", d], check=True)
    git(d, "config", "user.email", "t@t.com"); git(d, "config", "user.name", "t")
    open(f"{d}/f.txt", "w").write("old\n"); git(d, "add", "-A"); git(d, "commit", "-q", "-m", "prev")
    prev_sha = git(d, "rev-parse", "HEAD")
    open(f"{d}/f.txt", "w").write("new\n"); git(d, "add", "-A"); git(d, "commit", "-q", "-m", "new")
    os.makedirs(f"{d}/data", exist_ok=True)
    open(f"{d}/data/update.json", "w").write(json.dumps({**state, "previous": prev_sha}))
    return d, prev_sha

# no update.json at all -> nothing happens
d = tempfile.mkdtemp()
from pathlib import Path
assert maybe_rollback(Path(d)) is None
ok("no update.json -> no-op")

# state "ok" -> never touched
d, prev = repo_with_state({"state": "ok", "attempts": 0})
for _ in range(5):
    assert maybe_rollback(Path(d)) is None
assert open(f"{d}/f.txt").read() == "new\n"
ok("state=ok -> boots are never counted or rolled back")

# pending: counts up but doesn't roll back until MAX_BOOT_ATTEMPTS exceeded
d, prev = repo_with_state({"state": "pending", "attempts": 0})
for i in range(MAX_BOOT_ATTEMPTS):
    note = maybe_rollback(Path(d))
    assert note is None, (i, note)
    st = json.loads(open(f"{d}/data/update.json").read())
    assert st["attempts"] == i + 1
assert open(f"{d}/f.txt").read() == "new\n", "must not roll back before the threshold"
note = maybe_rollback(Path(d))
assert note and "rolled back" in note
assert open(f"{d}/f.txt").read() == "old\n"
st = json.loads(open(f"{d}/data/update.json").read())
assert st["state"] == "rolled_back" and st["reported"] is False
ok(f"pending -> after {MAX_BOOT_ATTEMPTS} failed boots, rolls back to the previous commit and marks unreported")

# once rolled_back, further boots are no-ops
assert maybe_rollback(Path(d)) is None
assert open(f"{d}/f.txt").read() == "old\n"
ok("rolled_back state -> subsequent boots don't touch it again")

# corrupt state file -> ignored, doesn't crash
d, _ = repo_with_state({"state": "pending", "attempts": 0})
open(f"{d}/data/update.json", "w").write("{not json")
assert maybe_rollback(Path(d)) is None
ok("corrupt update.json -> ignored, no crash")

# real subprocess boot: run.py under a broken main.py rolls back after MAX_BOOT_ATTEMPTS restarts (simulated by a wrapper loop)
d, prev = repo_with_state({"state": "pending", "attempts": 0})
import shutil
for f in ("run.py",):
    shutil.copy(str(__import__("pathlib").Path(__file__).resolve().parent.parent / f), d)
open(f"{d}/main.py", "w").write("import sys; sys.exit(1)\n")  # simulates the new code crashing at boot
for i in range(MAX_BOOT_ATTEMPTS + 1):
    r = subprocess.run([sys.executable, "run.py"], cwd=d, capture_output=True, text=True, timeout=15)
st = json.loads(open(f"{d}/data/update.json").read())
assert st["state"] == "rolled_back" and open(f"{d}/f.txt").read() == "old\n"
ok(f"end-to-end: run.py invoked {MAX_BOOT_ATTEMPTS + 1}x with a crashing main.py actually rolls back the checkout")
