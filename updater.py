"""Self-update: fast-forward this git checkout to origin, with gates and automatic rollback.

Human-triggered only (!update / !rollback in Discord). No agent tool can reach this module, and it
is one of the locked files the agent may not edit.

Safety net, in order:
  1. Only fast-forwards to origin/<your branch>: whatever you merged on GitHub.
  2. The commit's CI (GitHub Actions) must be green; failing or still-running CI refuses the update.
  3. The new code is smoke-tested (imports + your real .env parses) BEFORE the restart.
  4. If the smoke test fails, it resets to the previous commit and never restarts.
  5. If the new code passes but crashes at boot, run.py rolls back after a few failed starts.
"""
import base64
import json
import logging
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

log = logging.getLogger("updater")

APP_DIR = Path(__file__).resolve().parent
LAUNCHER_FILES = ("run.py", "updater.py", "pi-agent.service")  # a broken one of these can't roll itself back
SMOKE_CODE = (
    "import config, memory, ghclient, friction, llm, tools, watcher, updater, main; "
    "config.Config.from_env(); print('smoke ok')"
)


class UpdateError(Exception):
    """Safe to show to the user."""


@dataclass
class Plan:
    branch: str
    head: str
    target: str
    commits: list[str]
    files: list[str]
    slug: str

    @property
    def deps_changed(self) -> bool:
        return any(f.startswith("requirements") and f.endswith(".txt") for f in self.files)

    @property
    def launcher_changed(self) -> list[str]:
        return [f for f in self.files if f in LAUNCHER_FILES]


def slug_from_url(url: str) -> str:
    m = re.search(r"github\.com[:/]+([\w.-]+)/([\w.-]+?)(?:\.git)?/?$", url.strip())
    return f"{m.group(1)}/{m.group(2)}".lower() if m else ""


def ci_state(gh, slug: str, sha: str) -> tuple[str, str]:
    """('failing' | 'pending' | 'passing' | 'none', human-readable detail) for a commit."""
    ci = gh.checks(slug, sha)
    if ci["failed"]:
        return "failing", "failing: " + ", ".join(f["name"] for f in ci["failed"][:4])
    if ci["pending"]:
        return "pending", "still running: " + ", ".join(ci["pending"][:4])
    if ci["passed"] or ci["legacy"] == "success":
        return "passing", f"{ci['passed']} check(s) passed"
    return "none", "no CI results for this commit"


class Updater:
    def __init__(self, app_dir: Path = APP_DIR, github_token: str = "", smoke_cmd: Optional[list[str]] = None, pip: Optional[Callable] = None):
        self.app_dir = Path(app_dir)
        self.token = github_token
        self.smoke_cmd = smoke_cmd or [sys.executable, "-c", SMOKE_CODE]
        self._pip = pip or self._default_pip
        self.state_path = self.app_dir / "data" / "update.json"

    # ------------------------------------------------------------------ git plumbing

    def _env(self, auth: bool) -> dict:
        env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
        if auth and self.token and self._git_ok("remote", "get-url", "origin") and self._origin_url().startswith("https://github.com/"):
            basic = base64.b64encode(f"x-access-token:{self.token}".encode()).decode()  # via env, not argv: not visible in `ps`
            env.update(GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0="http.https://github.com/.extraheader", GIT_CONFIG_VALUE_0=f"AUTHORIZATION: basic {basic}")
        return env

    def _run(self, cmd: list[str], timeout: float, auth: bool = False) -> subprocess.CompletedProcess:
        try:
            return subprocess.run(cmd, cwd=self.app_dir, capture_output=True, text=True, timeout=timeout, env=self._env(auth))
        except subprocess.TimeoutExpired as e:
            raise UpdateError(f"`{' '.join(cmd[:3])}` timed out after {timeout:.0f}s") from e
        except FileNotFoundError as e:
            raise UpdateError(f"`{cmd[0]}` is not installed (sudo apt install git)") from e

    def _git(self, *args: str, timeout: float = 120, auth: bool = False) -> str:
        r = self._run(["git", *args], timeout, auth)
        if r.returncode != 0:
            raise UpdateError(f"git {args[0]} failed: {(r.stderr or r.stdout).strip()[-300:]}")
        return r.stdout.strip()

    def _git_ok(self, *args: str) -> bool:
        try:
            return subprocess.run(["git", *args], cwd=self.app_dir, capture_output=True, timeout=60).returncode == 0
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return False

    def _origin_url(self) -> str:
        return self._git("remote", "get-url", "origin")

    def is_checkout(self) -> bool:
        return (self.app_dir / ".git").exists()

    def origin_slug(self) -> str:
        try:
            return slug_from_url(self._origin_url()) if self.is_checkout() else ""
        except UpdateError:
            return ""

    def current(self) -> str:
        return self._git("rev-parse", "--short", "HEAD")

    # ------------------------------------------------------------------ state file

    def read_state(self) -> dict:
        try:
            return json.loads(self.state_path.read_text())
        except (OSError, ValueError):
            return {}

    def write_state(self, st: dict) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(st))
        os.replace(tmp, self.state_path)

    # ------------------------------------------------------------------ plan / apply / rollback

    def plan(self) -> Optional[Plan]:
        """What an update would do. None = already up to date. Raises UpdateError if it can't proceed."""
        if not self.is_checkout():
            raise UpdateError("This folder isn't a git checkout, so I can't update myself. Clone your repo on the Pi (see README).")
        dirty = self._git("status", "--porcelain")
        if dirty:
            raise UpdateError("There are uncommitted local changes in my folder; refusing to update:\n" + dirty[:300])
        branch = self._git("rev-parse", "--abbrev-ref", "HEAD")
        if branch == "HEAD":
            raise UpdateError("The checkout is on a detached HEAD; check out a branch first.")
        self._git("fetch", "origin", f"+refs/heads/{branch}:refs/remotes/origin/{branch}", timeout=180, auth=True)
        head, target = self._git("rev-parse", "HEAD"), self._git("rev-parse", f"origin/{branch}")
        if head == target:
            return None
        if not self._git_ok("merge-base", "--is-ancestor", head, target):
            raise UpdateError("My local branch has diverged from origin (not a simple fast-forward). Fix it by hand.")
        commits = self._git("log", "--oneline", "--no-decorate", f"{head}..{target}").splitlines()
        files = self._git("diff", "--name-only", head, target).splitlines()
        return Plan(branch, head, target, commits, files, self.origin_slug())

    def apply(self, plan: Plan) -> None:
        """Fast-forward, install deps if needed, smoke-test. On any failure, roll back and raise."""
        st = {"state": "applying", "previous": plan.head, "target": plan.target, "attempts": 0, "time": int(time.time())}
        self.write_state(st)
        try:
            self._git("merge", "--ff-only", plan.target)
            if plan.deps_changed:
                self._pip()
            self._smoke()
        except Exception as e:  # noqa: BLE001 - anything at all: put the old code back
            self._reset(plan.head)
            self.write_state({**st, "state": "rolled_back", "reported": True, "reason": str(e)[:300]})
            raise UpdateError(f"Update failed and was rolled back to {plan.head[:7]}: {e}") from e
        self.write_state({**st, "state": "pending"})  # becomes "ok" once the new code logs in to Discord

    def rollback(self) -> str:
        """Manual !rollback: return to the commit before the last update. Returns that commit's short sha."""
        st = self.read_state()
        prev = st.get("previous")
        if not prev or st.get("state") == "rolled_back":
            raise UpdateError("Nothing to roll back to (no update has been applied since the last rollback).")
        if not self.is_checkout():
            raise UpdateError("Not a git checkout.")
        self._reset(prev)
        self.write_state({**st, "state": "rolled_back", "reported": True, "reason": "manual rollback"})
        return prev[:7]

    def report_boot(self) -> Optional[str]:
        """Called once the bot is logged in. Marks a pending update healthy; reports rollbacks."""
        st = self.read_state()
        if st.get("state") == "pending":
            self.write_state({**st, "state": "ok", "healthy_at": int(time.time())})
            return f"✅ Update complete: now running `{self.current()}` (was `{st.get('previous', '?')[:7]}`). `!rollback` undoes it."
        if st.get("state") == "rolled_back" and not st.get("reported"):
            self.write_state({**st, "reported": True})
            return f"⚠️ The update to `{st.get('target', '?')[:7]}` failed to start and was rolled back to `{st.get('previous', '?')[:7]}`. Reason: {st.get('reason', '?')}"
        return None

    # ------------------------------------------------------------------ steps

    def _reset(self, sha: str) -> None:
        r = self._run(["git", "reset", "--hard", sha], 60)
        if r.returncode != 0:
            log.error("rollback reset failed: %s", r.stderr)
            raise UpdateError(f"could not reset to {sha[:7]}: {r.stderr.strip()[-200:]}")

    def _default_pip(self) -> None:
        r = self._run([sys.executable, "-m", "pip", "install", "-r", "requirements.txt"], 1800)
        if r.returncode != 0:
            raise UpdateError("pip install failed: " + (r.stderr or r.stdout).strip()[-300:])

    def _smoke(self) -> None:
        r = self._run(self.smoke_cmd, 240)
        if r.returncode != 0:
            raise UpdateError("smoke test failed: " + (r.stderr or r.stdout).strip()[-300:])
