"""GitHub write tools."""
import base64
from typing import Optional
from urllib.parse import quote

from smolagents import tool

from tools.common import RunState, Locked, make_safe, protected_reason
from tools.web import clip


def create_github_write_tools(cfg, gh, state: RunState, approve, friction=None):
    """Create GitHub write tools (guarded)."""
    _safe = make_safe(friction.record if friction else None)
    tools = []
    denied = "DENIED: the user declined this action. Do not retry it; tell the user and ask what they'd like instead."

    def guard_path(slug: str, path: str, exists: bool) -> None:
        """In the bot's OWN repo, refuse locked files (see LOCKED_PATHS)."""
        if cfg.self_repo and slug == cfg.self_repo:
            reason = protected_reason(cfg, path, exists)
            if reason:
                raise Locked(reason)

    def needs_approval(kind: str) -> bool:
        """kind is 'commit' (branch write) or 'publish' (PR / review comment)."""
        return cfg.approval_mode == "all" or (cfg.approval_mode == "publish" and kind == "publish")

    def commit(slug: str, path: str, new_text: str, old_text: Optional[str], sha: Optional[str], branch: str, message: str) -> str:
        if needs_approval("commit"):
            from tools.web import _unified

            detail = f"`{slug}` on `{branch}` - `{path}`\nMessage: {message}\n" + _unified(old_text or "", new_text, path)
            if not approve("Commit to a branch", detail):
                return denied
        if not gh.branch_exists(slug, branch):
            gh.create_branch(slug, branch)
        body = {"message": message[:200], "content": base64.b64encode(new_text.encode()).decode(), "branch": branch}
        if sha:
            body["sha"] = sha
        res = gh.call("PUT", f"/repos/{slug}/contents/{quote(path.strip('/'))}", json=body)
        return f"Committed {res['commit']['sha'][:7]} to {slug}@{branch}: {path}. Open a PR with gh_open_pr when the edits are done."

    @tool
    @_safe
    def gh_edit_file(repo: str, path: str, old_text: str, new_text: str, branch: str, commit_message: str) -> str:
        """Edit a file by replacing one exact piece of text, and commit it to an agent branch (created if missing).

        Args:
            repo: 'owner/name' or just 'name'.
            path: File path inside the repo.
            old_text: Exact existing text to replace (must match once, whitespace included). Copy it from gh_browse.
            new_text: Replacement text.
            branch: Branch to commit to; must start with the agent prefix (e.g. 'agent/fix-readme').
            commit_message: Short commit message.
        """
        slug, branch = gh.slug(repo), gh.require_agent_branch(branch)
        ref = branch if gh.branch_exists(slug, branch) else gh.default_branch(slug)
        guard_path(slug, path, exists=True)
        text, sha = gh.file_at(slug, path, ref)
        if text is None:
            return f"ERROR: {path} does not exist on {ref}. Use gh_write_file to create new files."
        n = text.count(old_text)
        if n == 0:
            return "ERROR: old_text was not found. Re-read the file with gh_browse and copy the text exactly."
        if n > 1:
            return f"ERROR: old_text matches {n} places. Include more surrounding lines so it matches once."
        return commit(slug, path, text.replace(old_text, new_text, 1), text, sha, branch, commit_message)

    @tool
    @_safe
    def gh_write_file(repo: str, path: str, content: str, branch: str, commit_message: str) -> str:
        """Create a new file (or fully overwrite one) and commit it to an agent branch. Prefer gh_edit_file for small changes.

        Args:
            repo: 'owner/name' or just 'name'.
            path: File path inside the repo.
            content: Complete new file content.
            branch: Branch to commit to; must start with the agent prefix (e.g. 'agent/add-docs').
            commit_message: Short commit message.
        """
        slug, branch = gh.slug(repo), gh.require_agent_branch(branch)
        ref = branch if gh.branch_exists(slug, branch) else gh.default_branch(slug)
        old, sha = gh.file_at(slug, path, ref)
        guard_path(slug, path, exists=old is not None)
        return commit(slug, path, content, old, sha, branch, commit_message)

    @tool
    @_safe
    def gh_open_pr(repo: str, title: str, body: str, head: str) -> str:
        """Open a pull request from an agent branch into the default branch. You cannot merge it.

        Args:
            repo: 'owner/name' or just 'name'.
            title: PR title.
            body: PR description (what changed and why).
            head: The agent branch with the commits (must start with the agent prefix).
        """
        slug, head = gh.slug(repo), gh.require_agent_branch(head)
        base = gh.default_branch(slug)
        cmp = gh.get(f"/repos/{slug}/compare/{quote(base, safe='/')}...{quote(head, safe='/')}")
        if not cmp["files"]:
            return f"ERROR: {head} has no changes compared to {base}."
        if cfg.self_repo and slug == cfg.self_repo:  # defence in depth: judge the branch, not just our own commits
            for f in cmp["files"]:
                reason = protected_reason(cfg, f["filename"], exists=f["status"] != "added")
                if reason:
                    raise Locked(f"This branch changes a locked file, so I can't open a PR for it. {reason}")
        files = "\n".join(f"  {f['filename']} +{f['additions']} -{f['deletions']}" for f in cmp["files"][:15])
        draft = bool(cfg.self_repo and slug == cfg.self_repo)  # changes to the bot itself always start as drafts
        body = clip(body, 3000) + "\n\n---\n_Opened by jarvis (LLM-written). Please review before merging._"
        if needs_approval("publish"):
            detail = f"`{slug}`: `{head}` -> `{base}`{' (DRAFT: changes to me)' if draft else ''}\n**{title[:120]}**\n```\n{files}\n```\n{clip(body, 500)}"
            if not approve("Open a pull request", detail):
                return denied
        pr = gh.call("POST", f"/repos/{slug}/pulls", json={"title": title[:200], "body": body, "head": head, "base": base, "draft": draft})
        return f"Opened {'DRAFT ' if draft else ''}PR #{pr['number']}: {pr['html_url']}"

    @tool
    @_safe
    def gh_review_pr(repo: str, number: int, body: str) -> str:
        """Post a review comment on a pull request (comment only - it never approves or requests changes).

        Args:
            repo: 'owner/name' or just 'name'.
            number: Pull request number.
            body: The review text (Markdown).
        """
        slug = gh.slug(repo)
        body = clip(body, 6000) + "\n\n_Automated review by jarvis._"
        res = gh.call("POST", f"/repos/{slug}/pulls/{int(number)}/reviews", json={"body": body, "event": "COMMENT"})
        return f"Review posted: {res.get('html_url', '(no url)')}"

    @tool
    @_safe
    def gh_open_issue(repo: str, title: str, body: str) -> str:
        """File a GitHub issue on YOUR OWN source repo. Only works during the weekly self-review, a few per run.

        Args:
            repo: Your own repo, 'owner/name'.
            title: Short, specific title.
            body: Evidence (counts, error text), the file(s) involved, and the change you propose.
        """
        import re

        slug = gh.slug(repo)
        if not cfg.self_repo or slug != cfg.self_repo:
            return "ERROR: issues can only be filed on your own source repository."
        if state.issue_budget <= 0:
            return "ERROR: you can't file issues in this task (or the per-run limit is used up)."
        title = " ".join(title.split())[:110]
        norm = lambda t: re.sub(r"[^a-z0-9]+", " ", t.lower()).strip()  # noqa: E731
        for existing in gh.get(f"/repos/{slug}/issues", params={"state": "open", "per_page": 50}):
            if norm(title) in norm(existing["title"]) or norm(existing["title"]) in norm(title):
                return f"ERROR: a similar open issue already exists (#{existing['number']}). Skip this one."
        if cfg.approval_mode == "all" and not approve("File an issue", f"`{slug}`\n**{title}**"):
            return denied
        body = clip(body, 4000) + "\n\n---\n_Filed by the bot's weekly self-review from runtime friction data. A human decides whether to act._"
        issue = gh.call("POST", f"/repos/{slug}/issues", json={"title": f"[self-review] {title}", "body": body, "labels": ["self-review"]})
        state.issue_budget -= 1
        return f"Filed issue #{issue['number']}: {issue['html_url']} ({state.issue_budget} more allowed)"

    tools += [gh_edit_file, gh_write_file, gh_open_pr, gh_review_pr, gh_open_issue]
    return tools