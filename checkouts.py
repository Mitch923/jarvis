"""Local git checkouts of allowed repos: fast reads and full-text search, and the repo state that
repo_test (sandbox.py) runs against.

Deliberately NOT a second way to write code: every checkout here is disposable and read-only from
the agent's point of view (synced with `git fetch` + `git reset --hard`, so it can never carry
local edits or drift). All commits still go through the GitHub API in tools.py/ghclient.py, with
its branch guard and protected-file checks - this module only adds faster reading, real full-text
search (via `git grep`, so no extra dependency like ripgrep), and something for repo_test to run
the repo's own tests against.
"""
import logging
import os
import re
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Optional

from config import Config
from ghclient import git_auth_env

log = logging.getLogger("checkouts")

_SLUG = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
_REF = re.compile(r"[A-Za-z0-9._/-]+")  # branch, tag or short/full sha


class CheckoutError(Exception):
    pass


class Checkouts:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.root = Path(cfg.checkout_dir)
        self._lock = threading.Lock()  # one git operation at a time per process; repos are cheap, concurrency isn't needed

    # ------------------------------------------------------------------ paths / validation

    def _slug(self, repo: str) -> str:
        repo = repo.strip().removeprefix("https://github.com/").strip("/").removesuffix(".git")
        if "/" not in repo and self.cfg.github_owner:
            repo = f"{self.cfg.github_owner}/{repo}"
        if not _SLUG.fullmatch(repo):
            raise CheckoutError(f"'{repo}' is not a valid repo name (use owner/name).")
        allowed = self.cfg.github_allowed_repos
        if allowed and repo.lower() not in allowed:
            raise CheckoutError(f"{repo} is not in the allowed repository list.")
        return repo.lower()

    def path(self, repo: str) -> Path:
        return self.root / self._slug(repo).replace("/", "__")

    @staticmethod
    def _safe_rel(path: str) -> str:
        """A path relative to a repo root, refusing anything that could escape it."""
        p = path.strip().replace("\\", "/").lstrip("/")
        import posixpath

        norm = posixpath.normpath(p) if p else "."
        if norm == ".." or norm.startswith("../"):
            raise CheckoutError(f"'{path}' escapes the repository.")
        return "" if norm == "." else norm

    # ------------------------------------------------------------------ git plumbing

    def _run(self, cwd: Path, args: list[str], auth_url: str = "", timeout: float = 180) -> str:
        env = git_auth_env(self.cfg.github_token, auth_url) if auth_url else {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
        try:
            r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=timeout, env=env)
        except subprocess.TimeoutExpired as e:
            raise CheckoutError(f"git {args[0]} timed out after {timeout:.0f}s") from e
        except FileNotFoundError as e:
            raise CheckoutError("git is not installed on this machine.") from e
        if r.returncode != 0:
            raise CheckoutError(f"git {args[0]} failed: {(r.stderr or r.stdout).strip()[-300:]}")
        return r.stdout

    # ------------------------------------------------------------------ sync

    def sync(self, repo: str, ref: Optional[str] = None) -> tuple[Path, str]:
        """Clone (if missing) or fetch, then hard-reset to `ref` (default branch if omitted).
        Returns (local path, short sha actually checked out)."""
        if ref is not None and not _REF.fullmatch(ref):
            raise CheckoutError(f"'{ref}' is not a valid branch/tag/sha.")
        slug = self._slug(repo)
        local = self.path(repo)
        url = f"https://github.com/{slug}.git"

        with self._lock:
            if not local.exists():
                existing = [p for p in self.root.iterdir() if p.is_dir()] if self.root.exists() else []
                if len(existing) >= self.cfg.checkout_max_repos:
                    raise CheckoutError(
                        f"Already have {len(existing)} repos checked out (CHECKOUT_MAX_REPOS={self.cfg.checkout_max_repos}). "
                        "Ask the owner to raise the limit or free space, or work from the GitHub tools instead."
                    )
                local.parent.mkdir(parents=True, exist_ok=True)
                try:
                    self._run(local.parent, ["clone", "--filter=blob:none", url, local.name], auth_url=url, timeout=300)
                except CheckoutError:
                    shutil.rmtree(local, ignore_errors=True)  # never leave a half-cloned directory behind
                    raise
            else:
                self._run(local, ["fetch", "--prune", "origin"], auth_url=url, timeout=180)

            target = ref or self._default_branch(local, url)
            # Accept either a remote branch or an exact sha/tag already present locally.
            remote_ref = f"origin/{target}"
            has_remote = self._run(local, ["branch", "-r", "--list", remote_ref]).strip() != ""
            self._run(local, ["checkout", "-q", "--detach", remote_ref if has_remote else target])
            self._run(local, ["reset", "-q", "--hard", remote_ref if has_remote else target])
            self._run(local, ["clean", "-fdq"])  # disposable mirror: never carries local changes
            sha = self._run(local, ["rev-parse", "--short", "HEAD"]).strip()
        return local, sha

    def _default_branch(self, local: Path, url: str) -> str:
        out = self._run(local, ["remote", "show", "origin"], auth_url=url, timeout=30)
        m = re.search(r"HEAD branch:\s*(\S+)", out)
        if not m or m.group(1) == "(unknown)":
            raise CheckoutError("Could not determine the default branch.")
        return m.group(1)

    def touch(self, repo: str) -> None:
        """Bump a repo's mtime so simple LRU bookkeeping (not currently automatic) has something to use."""
        p = self.path(repo)
        if p.exists():
            os.utime(p, None)

    # ------------------------------------------------------------------ read / search

    def read(self, repo: str, rel_path: str, start_line: int = 1, max_lines: int = 400) -> str:
        local, sha = self.sync(repo)
        rel = self._safe_rel(rel_path)
        target = local / rel if rel else local
        if not target.exists():
            raise CheckoutError(f"'{rel_path or '.'}' does not exist in {repo} @ {sha}.")
        if target.is_dir():
            rows = sorted(target.iterdir(), key=lambda e: (not e.is_dir(), e.name.lower()))
            lines = [("[dir] " if e.is_dir() else "") + e.name + ("" if e.is_dir() else f" ({e.stat().st_size} B)") for e in rows if e.name != ".git"]
            return f"{repo}/{rel} @ {sha}\n" + "\n".join(lines)
        try:
            text = target.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            raise CheckoutError(f"'{rel_path}' is not a UTF-8 text file.") from None
        all_lines = text.splitlines()
        start = max(1, int(start_line))
        chunk = all_lines[start - 1 : start - 1 + max(1, int(max_lines))]
        body = "\n".join(f"{start + i:>5}| {line}" for i, line in enumerate(chunk))
        return f"{repo}:{rel} @ {sha} - lines {start}-{start + len(chunk) - 1} of {len(all_lines)}\n{body}"

    def grep(self, repo: str, pattern: str, path_glob: str = "", max_results: int = 60) -> str:
        local, sha = self.sync(repo)
        try:
            re.compile(pattern)
        except re.error as e:
            raise CheckoutError(f"'{pattern}' is not a valid regex: {e}") from None
        args = ["git", "grep", "-n", "-I", "--no-color", "-e", pattern]
        if path_glob:
            args += ["--", path_glob]
        try:
            r = subprocess.run(args, cwd=local, capture_output=True, text=True, timeout=60)
        except subprocess.TimeoutExpired as e:
            raise CheckoutError("search timed out after 60s") from e
        if r.returncode == 1:  # git grep's documented "no matches" exit code - not an error
            return f"No matches for '{pattern}' in {repo} @ {sha}."
        if r.returncode not in (0, 1):
            raise CheckoutError(f"search failed: {(r.stderr or r.stdout).strip()[-300:]}")
        lines = r.stdout.splitlines()
        body = "\n".join(lines[:max_results])
        more = f"\n... and {len(lines) - max_results} more matches" if len(lines) > max_results else ""
        return f"{repo} @ {sha}: {len(lines)} match(es) for '{pattern}'\n{body}{more}"
