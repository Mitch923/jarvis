"""Minimal GitHub REST client (requests only) shared by the tools and the watcher."""
import base64
import logging
import os
import re
import time
from typing import Optional
from urllib.parse import quote

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from config import Config

log = logging.getLogger("github")


def git_auth_env(token: str, url: str) -> dict:
    """Env vars that make `git` send `token` as a GitHub HTTPS Basic-auth header, but only for
    an https://github.com/... URL - via GIT_CONFIG_* rather than the URL or argv, so the token
    is never written into .git/config, shown in `ps`, or logged in shell history. Shared by
    updater.py (updating this checkout) and checkouts.py (cloning/syncing other repos)."""
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    if token and url.startswith("https://github.com/"):
        basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        env.update(GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0="http.https://github.com/.extraheader", GIT_CONFIG_VALUE_0=f"AUTHORIZATION: basic {basic}")
    return env


def clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... [truncated, {len(text) - limit} more characters]"


class GitHubError(Exception):
    def __init__(self, message: str, status: int = 0):
        super().__init__(message)
        self.status = status


_CI_FAIL = {"failure", "timed_out", "startup_failure"}
_SLUG = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
_BRANCH = re.compile(r"[A-Za-z0-9._/-]+")


class GitHub:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.base = cfg.github_api_url
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "jarvis",
            }
        )
        if cfg.github_token:
            self.session.headers["Authorization"] = f"Bearer {cfg.github_token}"
        retry = Retry(total=2, backoff_factor=0.7, status_forcelist=(502, 503, 504), allowed_methods=frozenset({"GET"}))
        self.session.mount("https://", HTTPAdapter(max_retries=retry))
        self.session.mount("http://", HTTPAdapter(max_retries=retry))
        self._owner = cfg.github_owner
        self._default_branches: dict[str, str] = {}

    # -- transport
    def call(self, method: str, path: str, **kwargs):
        try:
            r = self.session.request(method, self.base + path, timeout=(5, 20), **kwargs)
        except requests.Timeout as e:
            raise GitHubError("GitHub request timed out") from e
        except requests.RequestException as e:
            raise GitHubError(f"GitHub network error ({type(e).__name__})") from e
        if r.status_code >= 400:
            raise GitHubError(self._explain(r), r.status_code)
        return r.json() if r.content else None

    def get(self, path: str, **kw):
        return self.call("GET", path, **kw)

    @staticmethod
    def _explain(r: requests.Response) -> str:
        try:
            body = r.json()
            msg = body.get("message", "")
            if body.get("errors"):
                msg += f" {body['errors']}"
        except ValueError:
            msg = r.text[:200]
        code = r.status_code
        if code == 401:
            return "GitHub auth failed - the token is invalid or expired."
        if code == 403 and r.headers.get("x-ratelimit-remaining") == "0":
            reset = int(r.headers.get("x-ratelimit-reset", "0"))
            return f"GitHub rate limit hit; resets in {max(0, reset - int(time.time())) // 60} min."
        if code == 403:
            return f"Forbidden - the token lacks permission for this action. ({msg})"
        if code == 404:
            return "Not found (wrong name/path/ref, or the token has no access to it)."
        return f"GitHub error {code}: {msg}"

    # -- helpers
    def owner(self) -> str:
        if not self._owner:
            self._owner = self.get("/user")["login"]
        return self._owner

    def slug(self, repo: str) -> str:
        repo = repo.strip().removeprefix("https://github.com/").strip("/").removesuffix(".git")
        if "/" not in repo:
            repo = f"{self.owner()}/{repo}"
        if not _SLUG.fullmatch(repo):
            raise GitHubError(f"'{repo}' is not a valid repo name (use owner/name).")
        allowed = self.cfg.github_allowed_repos
        if allowed and repo.lower() not in allowed:
            raise GitHubError(f"{repo} is not in the allowed repository list.")
        return repo

    def default_branch(self, slug: str) -> str:
        if slug not in self._default_branches:
            self._default_branches[slug] = self.get(f"/repos/{slug}")["default_branch"]
        return self._default_branches[slug]

    def branch_exists(self, slug: str, branch: str) -> bool:
        try:
            self.get(f"/repos/{slug}/git/ref/heads/{quote(branch, safe='/')}")
            return True
        except GitHubError as e:
            if e.status == 404:
                return False
            raise

    def create_branch(self, slug: str, branch: str) -> None:
        base = self.default_branch(slug)
        sha = self.get(f"/repos/{slug}/git/ref/heads/{quote(base, safe='/')}")["object"]["sha"]
        self.call("POST", f"/repos/{slug}/git/refs", json={"ref": f"refs/heads/{branch}", "sha": sha})

    def file_at(self, slug: str, path: str, ref: Optional[str]) -> tuple[Optional[str], Optional[str]]:
        """(text, blob_sha) or (None, None) if the file doesn't exist there."""
        try:
            data = self.get(f"/repos/{slug}/contents/{quote(path.strip('/'))}", params={"ref": ref} if ref else {})
        except GitHubError as e:
            if e.status == 404:
                return None, None
            raise
        if isinstance(data, list):
            raise GitHubError(f"'{path}' is a directory, not a file.")
        return self.decode(data), data["sha"]

    @staticmethod
    def decode(data: dict) -> str:
        if data.get("encoding") != "base64" or not data.get("content"):
            if data.get("size", 0) == 0:
                return ""
            raise GitHubError("File is too large (>1 MB) for the contents API.")
        try:
            return base64.b64decode(data["content"]).decode("utf-8")
        except UnicodeDecodeError as e:
            raise GitHubError("File is binary (not UTF-8 text).") from e

    def require_agent_branch(self, branch: str) -> str:
        branch = branch.strip()
        prefix = self.cfg.branch_prefix
        if not branch.startswith(prefix) or not _BRANCH.fullmatch(branch) or ".." in branch:
            raise GitHubError(
                f"Writes are only allowed on branches starting with '{prefix}' (e.g. '{prefix}fix-typo'). "
                "Never write to a default or existing branch."
            )
        return branch


    # -- CI
    def checks(self, slug: str, ref: str) -> dict:
        """CI picture for a branch/sha: failed / pending / passed, from check runs + legacy commit statuses."""
        ref_q = quote(ref, safe="/")
        runs = self.get(f"/repos/{slug}/commits/{ref_q}/check-runs", params={"per_page": 50}).get("check_runs", [])
        out: dict = {"sha": "", "failed": [], "pending": [], "passed": 0, "legacy": None}
        for r in runs:
            out["sha"] = r.get("head_sha") or out["sha"]
            if r.get("status") != "completed":
                out["pending"].append(r["name"])
            elif r.get("conclusion") in _CI_FAIL:
                out["failed"].append(
                    {
                        "name": r["name"],
                        "url": r.get("html_url", ""),
                        "summary": ((r.get("output") or {}).get("summary") or "")[:300],
                        "sha": r.get("head_sha", ""),
                    }
                )
            elif r.get("conclusion") in ("success", "neutral", "skipped"):
                out["passed"] += 1
        try:  # older CI systems report via "commit statuses" instead of check runs
            st = self.get(f"/repos/{slug}/commits/{ref_q}/status")
            if st.get("total_count", 0) > 0:
                out["legacy"] = st["state"]
                out["sha"] = out["sha"] or st.get("sha", "")
                for s in st.get("statuses", []):
                    if s["state"] in ("failure", "error"):
                        out["failed"].append(
                            {"name": s["context"], "url": s.get("target_url") or "", "summary": s.get("description") or "", "sha": st.get("sha", "")}
                        )
                if st["state"] == "pending":
                    out["pending"].append("commit status")
        except GitHubError:
            pass  # best effort
        return out
