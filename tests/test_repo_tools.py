import json, os, shutil, stat, subprocess, sys, tempfile, time
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))
from pathlib import Path

import fake_gh

gsrv, gh_base = fake_gh.start()
ok = lambda s: print("PASS", s)


def git(repo, *args):
    r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    assert r.returncode == 0, (args, r.stderr)
    return r.stdout.strip()


def make_repo():
    base = tempfile.mkdtemp()
    origin, work = f"{base}/origin", f"{base}/work"
    subprocess.run(["git", "init", "-q", "--bare", origin], check=True)
    subprocess.run(["git", "clone", "-q", origin, work], check=True)
    git(work, "config", "user.email", "t@t.com")
    git(work, "config", "user.name", "t")
    os.makedirs(f"{work}/tests")
    open(f"{work}/README.md", "w").write("# Proj\nHello world\n")
    open(f"{work}/tests/run_all.py", "w").write("print('PASS everything')\n")
    open(f"{work}/.gitignore", "w").write("data/\n")
    git(work, "checkout", "-q", "-b", "main")
    git(work, "add", "-A")
    git(work, "commit", "-q", "-m", "initial")
    git(work, "push", "-q", "-u", "origin", "main")
    subprocess.run(["git", "-C", origin, "symbolic-ref", "HEAD", "refs/heads/main"], check=True)
    return base, origin, work


base, origin, work = make_repo()

os.environ.update(
    DISCORD_TOKEN="x", DISCORD_ALLOWED_USER_IDS="1", OPENROUTER_API_KEY="k",
    GITHUB_TOKEN="tok", GITHUB_API_URL=gh_base, GITHUB_ALLOWED_REPOS="me/proj",
    DATA_DIR=tempfile.mkdtemp(), APPROVAL_MODE="publish",
)  # fmt: skip
from config import Config
import checkouts as checkouts_module
import tools as T


class LocalCheckouts(checkouts_module.Checkouts):
    """Same as Checkouts, but clones from a local bare repo instead of github.com - lets the real
    build_tools()-constructed repo_sync/repo_read/repo_grep/repo_test tools be exercised end to
    end without any network access."""

    def sync(self, repo, ref=None):
        import re as _re

        if ref is not None and not _re.fullmatch(r"[A-Za-z0-9._/-]+", ref):
            raise checkouts_module.CheckoutError(f"'{ref}' is not a valid branch/tag/sha.")
        local = self.path(repo)
        with self._lock:
            if not local.exists():
                local.parent.mkdir(parents=True, exist_ok=True)
                self._run(local.parent, ["clone", "--filter=blob:none", origin, local.name], timeout=300)
            else:
                self._run(local, ["fetch", "--prune", "origin"], timeout=180)
            target = ref or self._default_branch(local, origin)
            remote_ref = f"origin/{target}"
            has_remote = self._run(local, ["branch", "-r", "--list", remote_ref]).strip() != ""
            self._run(local, ["checkout", "-q", "--detach", remote_ref if has_remote else target])
            self._run(local, ["reset", "-q", "--hard", remote_ref if has_remote else target])
            self._run(local, ["clean", "-fdq"])
            sha = self._run(local, ["rev-parse", "--short", "HEAD"]).strip()
        return local, sha


def fake_bin_dir() -> Path:
    d = Path(tempfile.mkdtemp())
    src = Path(__file__).resolve().parent / "fake_docker.py"
    for name in ("docker", "podman"):
        target = d / name
        target.write_text(f"#!/usr/bin/env python3\nimport runpy\nrunpy.run_path({str(src)!r}, run_name='__main__')\n")
        target.chmod(target.stat().st_mode | stat.S_IEXEC)
    return d


def build(**overrides):
    checkouts_module.Checkouts = LocalCheckouts  # tools.py did `from checkouts import Checkouts`...
    T.Checkouts = LocalCheckouts  # ...so patch the name bound in tools.py's own namespace
    os.environ.update(overrides)
    cfg = Config.from_env()
    return {t.name: t for t in T.build_tools(cfg, lambda a, b: True, None, T.RunState())}, cfg


# 1. tool availability follows config
tl, _ = build(CHECKOUTS_ENABLED="1", SANDBOX_RUNTIME="docker")
assert {"repo_sync", "repo_read", "repo_grep", "repo_test"} <= tl.keys()
ok("checkouts + sandbox enabled -> all four repo_* tools present")

tl, _ = build(CHECKOUTS_ENABLED="1", SANDBOX_RUNTIME="")
assert {"repo_sync", "repo_read", "repo_grep"} <= tl.keys() and "repo_test" not in tl
ok("SANDBOX_RUNTIME='' -> repo_test absent, but sync/read/grep still work")

tl, _ = build(CHECKOUTS_ENABLED="0")
assert not ({"repo_sync", "repo_read", "repo_grep", "repo_test"} & tl.keys())
ok("CHECKOUTS_ENABLED=0 -> none of the repo_* tools present")

# 2. repo_sync / repo_read / repo_grep against a real local git repo
tl, cfg = build(CHECKOUTS_ENABLED="1", SANDBOX_RUNTIME="docker", CHECKOUT_DIR=tempfile.mkdtemp())
r = tl["repo_sync"](repo="proj")
assert "synced @" in r and "me/proj" in r
ok(f"repo_sync -> {r}")

r = tl["repo_read"](repo="proj", path="README.md")
assert "1| # Proj" in r
ok("repo_read: file with line numbers")
r = tl["repo_read"](repo="proj")
assert "[dir] tests" in r and "README.md" in r
ok("repo_read: directory listing")

r = tl["repo_grep"](repo="proj", pattern="Hello")
assert "README.md:2:Hello world" in r
ok("repo_grep: finds a match with file:line via real git grep")
r = tl["repo_grep"](repo="proj", pattern="zzz_no_such_thing")
assert "No matches" in r
ok("repo_grep: no matches -> clean message")

# 3. allowlist and bad-name errors surface as ERROR strings, not exceptions
r = tl["repo_read"](repo="not-allowed/other")
assert r.startswith("ERROR:") and "not in the allowed" in r
ok("repo_read: disallowed repo -> ERROR string")

# 4. repo_test: happy path, via the fake docker CLI
bindir = fake_bin_dir()
logf, scriptf = tempfile.mktemp(), tempfile.mktemp()
json.dump([{"kind": "ok", "stdout": "PASS everything\n", "code": 0}], open(scriptf, "w"))
old_path = os.environ.get("PATH", "")
os.environ["PATH"] = f"{bindir}:{old_path}"
os.environ["FAKE_DOCKER_LOG"] = logf
os.environ["FAKE_DOCKER_SCRIPT"] = scriptf
try:
    r = tl["repo_test"](repo="proj")
finally:
    os.environ["PATH"] = old_path
assert "tests PASSED" in r and "PASS everything" in r and "me/proj @" in r
ok(f"repo_test: end-to-end pass via build_tools()'s own tool -> {[l for l in r.splitlines() if 'tests PASSED' in l][0]}")

# 5. repo_test: failing tests reported in the reply text, not raised as an error
json.dump([{"kind": "ok", "stdout": "", "stderr": "1 failed\n", "code": 1}], open(scriptf, "w"))
os.environ["PATH"] = f"{bindir}:{old_path}"
try:
    r = tl["repo_test"](repo="proj")
finally:
    os.environ["PATH"] = old_path
assert not r.startswith("ERROR:") and "tests FAILED" in r and "1 failed" in r
ok("repo_test: a real test failure is reported in the text, not treated as a tool error")

# 6. repo_test: docker missing (but git still available) -> a clean ERROR string, not a crash.
# Build a private bin dir containing ONLY git (no docker/podman). Narrowing PATH to the git
# directory (the old approach) is not enough on runners that ship docker in /usr/bin; a
# self-contained dir guarantees the restricted PATH is what the check intends.
git_src_str = subprocess.run(["which", "git"], capture_output=True, text=True).stdout.strip()
if not git_src_str:
    raise AssertionError("git not found on PATH")
git_only_bin = Path(tempfile.mkdtemp())
shutil.copy2(Path(git_src_str), git_only_bin / "git")
(git_only_bin / "git").chmod(stat.S_IEXEC)
os.environ["PATH"] = str(git_only_bin)  # git available, but no docker/podman on PATH
try:
    r = tl["repo_test"](repo="proj")
finally:
    os.environ["PATH"] = old_path
assert r.startswith("ERROR:") and "not installed" in r and "docker" in r
ok("repo_test: missing docker (git still present) -> ERROR string, agent can react instead of the task crashing")

print("repo tool wiring: all checks passed")
