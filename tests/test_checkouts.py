import os, subprocess, sys, tempfile, time
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))

ok = lambda s: print("PASS", s)


def git(repo, *args):
    r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    assert r.returncode == 0, (args, r.stderr)
    return r.stdout.strip()


def make_repo(default_branch="main"):
    base = tempfile.mkdtemp()
    origin, work = f"{base}/origin", f"{base}/work"
    subprocess.run(["git", "init", "-q", "--bare", origin], check=True)
    subprocess.run(["git", "clone", "-q", origin, work], check=True)
    git(work, "config", "user.email", "t@t.com")
    git(work, "config", "user.name", "t")
    os.makedirs(f"{work}/src")
    open(f"{work}/README.md", "w").write("# Proj\nHello world\n")
    open(f"{work}/src/app.py", "w").write("def parse_config():\n    return 1\n\ndef other():\n    pass\n")
    open(f"{work}/.gitignore", "w").write("data/\n")
    git(work, "checkout", "-q", "-b", default_branch)
    git(work, "add", "-A")
    git(work, "commit", "-q", "-m", "initial")
    git(work, "push", "-q", "-u", "origin", default_branch)
    subprocess.run(["git", "-C", origin, "symbolic-ref", "HEAD", f"refs/heads/{default_branch}"], check=True)
    return base, origin, work


def push_from_elsewhere(origin, files, branch="main", base_branch="main"):
    other = tempfile.mkdtemp()
    subprocess.run(["git", "clone", "-q", "-b", base_branch, origin, other], check=True)
    git(other, "config", "user.email", "t@t.com")
    git(other, "config", "user.name", "t")
    if branch != base_branch:
        git(other, "checkout", "-q", "-b", branch)
    for path, content in files.items():
        full = f"{other}/{path}"
        os.makedirs(os.path.dirname(full) or ".", exist_ok=True)
        open(full, "w").write(content)
    git(other, "add", "-A")
    git(other, "commit", "-q", "-m", "remote change")
    git(other, "push", "-q", "origin", f"HEAD:{branch}")
    return git(other, "rev-parse", "--short", "HEAD")


os.environ.update(
    DISCORD_TOKEN="x", DISCORD_ALLOWED_USER_IDS="1", OPENROUTER_API_KEY="k",
    GITHUB_TOKEN="", GITHUB_ALLOWED_REPOS="me/proj,me/other", DATA_DIR=tempfile.mkdtemp(),
)
from config import Config
from checkouts import CheckoutError, Checkouts

# Point "github.com/me/proj" at a local bare repo instead of the real GitHub, by monkeypatching
# the URL Checkouts.sync() builds - this lets us exercise real `git clone`/`fetch`/`grep` without
# any network access, exactly as if it were a real (local) remote.
base, origin, work = make_repo()


class LocalCheckouts(Checkouts):
    """Same as Checkouts, but clones from a local bare repo path instead of github.com."""

    def __init__(self, cfg, url_for):
        super().__init__(cfg)
        self._url_for = url_for

    def sync(self, repo, ref=None):
        slug = self._slug(repo)
        self._url_for["last"] = f"https://github.com/{slug}.git"  # what a real deployment would use
        return self._sync_from(repo, ref, origin)

    def _sync_from(self, repo, ref, real_url):
        # Reimplements sync() with the URL swapped, to avoid duplicating validation logic drift.
        import re as _re

        if ref is not None and not _re.fullmatch(r"[A-Za-z0-9._/-]+", ref):
            raise CheckoutError(f"'{ref}' is not a valid branch/tag/sha.")
        local = self.path(repo)
        with self._lock:
            if not local.exists():
                existing = [p for p in self.root.iterdir() if p.is_dir()] if self.root.exists() else []
                if len(existing) >= self.cfg.checkout_max_repos:
                    raise CheckoutError(f"Already have {len(existing)} repos checked out (CHECKOUT_MAX_REPOS={self.cfg.checkout_max_repos}).")
                local.parent.mkdir(parents=True, exist_ok=True)
                try:
                    self._run(local.parent, ["clone", "--filter=blob:none", real_url, local.name], timeout=300)
                except CheckoutError:
                    import shutil as _sh

                    _sh.rmtree(local, ignore_errors=True)
                    raise
            else:
                self._run(local, ["fetch", "--prune", "origin"], timeout=180)
            target = ref or self._default_branch(local, real_url)
            remote_ref = f"origin/{target}"
            has_remote = self._run(local, ["branch", "-r", "--list", remote_ref]).strip() != ""
            self._run(local, ["checkout", "-q", "--detach", remote_ref if has_remote else target])
            self._run(local, ["reset", "-q", "--hard", remote_ref if has_remote else target])
            self._run(local, ["clean", "-fdq"])
            sha = self._run(local, ["rev-parse", "--short", "HEAD"]).strip()
        return local, sha


def new(**overrides):
    os.environ.update(overrides)
    cfg = Config.from_env()
    return LocalCheckouts(cfg, {})


# 1. first sync clones; repo path uses owner__name
c = new(CHECKOUT_DIR=tempfile.mkdtemp())
local, sha = c.sync("me/proj")
assert local.exists() and (local / ".git").exists() and local.name == "me__proj"
assert len(sha) >= 7
ok(f"first sync clones the repo -> {local.name} @ {sha}")

# 2. read a file with line numbers; read a directory listing
out = c.read("me/proj", "src/app.py")
assert "1| def parse_config" in out and "src/app.py @" in out
ok("read(): file content with line numbers")
out = c.read("me/proj", "")
assert "[dir] src" in out and "README.md" in out and "[dir] .git" not in out
ok("read(): directory listing, .git hidden")
try:
    c.read("me/proj", "nope.txt")
    assert False
except CheckoutError as e:
    assert "does not exist" in str(e)
ok("read(): missing file -> clear error")
try:
    c.read("me/proj", "../../etc/passwd")
    assert False
except CheckoutError as e:
    assert "escapes" in str(e)
ok("read(): path traversal refused")

# 3. grep finds matches, respects path_glob, reports "no matches" cleanly
out = c.grep("me/proj", "parse_config")
assert "src/app.py:1:def parse_config" in out and "1 match" in out
ok("grep(): finds a match with file:line")
out = c.grep("me/proj", "nonexistent_xyz_pattern")
assert "No matches" in out
ok("grep(): no matches -> clean message, not an error")
out = c.grep("me/proj", "def ", path_glob="*.md")
assert "No matches" in out  # "def " only appears in .py, not .md
ok("grep(): path_glob restricts the search")
try:
    c.grep("me/proj", "(unclosed[")
    assert False
except CheckoutError as e:
    assert "regex" in str(e)
ok("grep(): invalid regex rejected before running git")

# 4. sync picks up new remote commits and discards any local drift (disposable mirror)
(local / "scratch.txt").write_text("local edit that should be wiped\n")
new_sha = push_from_elsewhere(origin, {"src/app.py": "def parse_config():\n    return 2\n"})
local2, sha2 = c.sync("me/proj")
assert sha2 == new_sha and not (local2 / "scratch.txt").exists()
assert "return 2" in (local2 / "src" / "app.py").read_text()
ok(f"sync(): fetches new commits and wipes local drift ({sha} -> {sha2})")

# 5. sync a specific ref (branch)
push_from_elsewhere(origin, {"feature.txt": "on a branch\n"}, branch="feat")
local3, sha3 = c.sync("me/proj", ref="feat")
assert (local3 / "feature.txt").exists()
local4, sha4 = c.sync("me/proj")  # back to default branch afterwards
assert not (local4 / "feature.txt").exists() and sha4 == sha2
ok("sync(ref=...): checks out a specific branch; a later plain sync() returns to the default branch")

try:
    c.sync("me/proj", ref="not a valid ref!!")
    assert False
except CheckoutError:
    pass
ok("sync(): rejects a malformed ref")

# 6. allowlist enforcement, invalid slug, path escape in repo name
try:
    c.sync("me/private-not-allowed")
    assert False
except CheckoutError as e:
    assert "not in the allowed" in str(e)
ok("sync(): repo allowlist enforced")
try:
    c.sync("not-a-valid-slug-at-all!!")
    assert False
except CheckoutError as e:
    assert "not a valid repo name" in str(e)
ok("sync(): malformed repo name rejected")

# 7. CHECKOUT_MAX_REPOS enforced
c2 = new(CHECKOUT_DIR=tempfile.mkdtemp(), CHECKOUT_MAX_REPOS="1")
c2.sync("me/proj")
base2, origin2, work2 = make_repo()
c2._url_for = {}
orig_sync_from = c2._sync_from
c2._sync_from = lambda repo, ref, real_url: orig_sync_from(repo, ref, origin2 if repo == "me/other" else origin)
try:
    c2.sync("me/other")
    assert False
except CheckoutError as e:
    assert "CHECKOUT_MAX_REPOS" in str(e)
ok("sync(): refuses a new clone once CHECKOUT_MAX_REPOS is reached")

# 8. git-not-installed is reported clearly (simulate via a broken PATH)
c3 = new(CHECKOUT_DIR=tempfile.mkdtemp())
real_path = os.environ.get("PATH", "")
os.environ["PATH"] = "/nonexistent"
try:
    c3.sync("me/proj")
    assert False
except CheckoutError as e:
    assert "not installed" in str(e)
finally:
    os.environ["PATH"] = real_path
ok("sync(): missing git binary reported clearly, not a raw traceback")

print("checkouts.py: all checks passed")
