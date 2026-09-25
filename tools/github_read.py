"""GitHub read tools."""
from urllib.parse import quote
from typing import Optional

from smolagents import tool

from tools.common import RunState, make_safe, UNTRUSTED
from tools.web import _format_files, clip


def create_github_read_tools(cfg, gh, state: RunState, friction=None):
    """Create GitHub read-only tools."""
    limit_chars = cfg.tool_output_chars
    _safe = make_safe(friction.record if friction else None)
    tools = []

    def untrusted(text: str) -> str:
        """Label text from outside as data, and remember that this task has seen some."""
        state.tainted = True
        return UNTRUSTED + text

    @tool
    @_safe
    def gh_repos(limit: int = 20) -> str:
        """List GitHub repositories you can access, most recently pushed first.

        Args:
            limit: Max repos to return (1-50).
        """
        data = gh.get("/user/repos", params={"sort": "pushed", "per_page": max(1, min(int(limit), 50))})
        allowed = cfg.github_allowed_repos
        rows = [
            f"{r['full_name']}{' (private)' if r['private'] else ''} - {r.get('description') or 'no description'}"
            f" [default: {r['default_branch']}, pushed {r['pushed_at'][:10]}]"
            for r in data
            if not allowed or r["full_name"].lower() in allowed
        ]
        return clip("\n".join(rows) or "No repositories found.", limit_chars)

    @tool
    @_safe
    def gh_browse(repo: str, path: str = "", ref: Optional[str] = None, start_line: int = 1, max_lines: int = 250) -> str:
        """List a directory or read a file (with line numbers) in a GitHub repo.

        Args:
            repo: 'owner/name', or just 'name' for your own repos.
            path: Path inside the repo; empty for the root directory.
            ref: Branch, tag or commit SHA. Defaults to the default branch.
            start_line: First line to show when reading a file (1-based).
            max_lines: Max lines to show when reading a file.
        """
        slug = gh.slug(repo)
        p = path.strip("/")
        data = gh.get(f"/repos/{slug}/contents/{quote(p)}".rstrip("/"), params={"ref": ref} if ref else {})
        if isinstance(data, list):
            data.sort(key=lambda i: (i["type"] != "dir", i["name"].lower()))
            rows = [
                f"{'[dir] ' if i['type'] == 'dir' else ''}{i['path']}" + (f" ({i['size']} B)" if i["type"] == "file" else "")
                for i in data
            ]
            return clip(f"{slug}/{p or ''} @ {ref or 'default branch'}\n" + "\n".join(rows), limit_chars)
        lines = gh.decode(data).splitlines()
        start = max(1, int(start_line))
        chunk = lines[start - 1 : start - 1 + max(1, int(max_lines))]
        body = "\n".join(f"{start + i:>4}| {line}" for i, line in enumerate(chunk))
        head = f"{slug}:{p} @ {ref or 'default branch'} - lines {start}-{start + len(chunk) - 1} of {len(lines)}"
        return untrusted(clip(head + "\n" + body, limit_chars))

    @tool
    @_safe
    def gh_search_code(query: str, repo: Optional[str] = None) -> str:
        """Search code in the allowed repos (or one given repo). Returns matching file paths.

        Args:
            query: Search terms (e.g. 'def parse_config').
            repo: Optional 'owner/name' to restrict the search to.
        """
        allowed = cfg.github_allowed_repos
        if repo:
            scopes = [f"repo:{gh.slug(repo)}"]
        elif allowed:
            scopes = [f"repo:{r}" for r in sorted(allowed)[:5]]  # one search per repo: qualifiers can't be relied on to OR
        else:
            scopes = [f"user:{gh.owner()}"]
        rows = []
        for scope in scopes:
            data = gh.get("/search/code", params={"q": f"{query} {scope}", "per_page": 15})
            for i in data.get("items", []):
                name = i["repository"]["full_name"]
                if not allowed or name.lower() in allowed:
                    rows.append(f"{name}:{i['path']}")
        return clip("\n".join(rows[:20]) or "No matches.", limit_chars)

    @tool
    @_safe
    def gh_commits(repo: str, branch: Optional[str] = None, limit: int = 10) -> str:
        """List recent commits on a branch.

        Args:
            repo: 'owner/name' or just 'name'.
            branch: Branch name; defaults to the default branch.
            limit: Number of commits (1-30).
        """
        slug = gh.slug(repo)
        params = {"per_page": max(1, min(int(limit), 30))}
        if branch:
            params["sha"] = branch
        data = gh.get(f"/repos/{slug}/commits", params=params)
        rows = [
            f"{c['sha'][:7]} {c['commit']['author']['date'][:10]} {(c.get('author') or {}).get('login') or c['commit']['author']['name']}: "
            f"{c['commit']['message'].splitlines()[0][:100]}"
            for c in data
        ]
        return clip("\n".join(rows) or "No commits.", limit_chars)

    @tool
    @_safe
    def gh_diff(repo: str, base: str, head: Optional[str] = None) -> str:
        """Show changes: one commit (give only a SHA), or everything between two refs (base and head).

        Args:
            repo: 'owner/name' or just 'name'.
            base: A commit SHA (alone), or the base branch/tag/SHA of a comparison.
            head: Head branch/tag/SHA to compare against base. Omit to show the single commit `base`.
        """
        slug = gh.slug(repo)
        if head:
            data = gh.get(f"/repos/{slug}/compare/{quote(base, safe='/')}...{quote(head, safe='/')}")
            header = f"{head} is {data['ahead_by']} ahead / {data['behind_by']} behind {base} ({len(data['files'])} files)"
        else:
            data = gh.get(f"/repos/{slug}/commits/{quote(base, safe='/')}")
            header = f"{data['sha'][:7]}: {data['commit']['message'].splitlines()[0]}\n{len(data['files'])} files"
        return untrusted(clip(header + "\n\n" + _format_files(data["files"]), limit_chars))

    @tool
    @_safe
    def gh_prs(repo: str, state: str = "open", limit: int = 15) -> str:
        """List pull requests.

        Args:
            repo: 'owner/name' or just 'name'.
            state: 'open', 'closed' or 'all'.
            limit: Max number to return (1-30).
        """
        slug = gh.slug(repo)
        data = gh.get(f"/repos/{slug}/pulls", params={"state": state, "per_page": max(1, min(int(limit), 30))})
        rows = [
            f"#{p['number']} {p['title']} ({p['head']['ref']} -> {p['base']['ref']}) by {p['user']['login']}, updated {p['updated_at'][:10]}"
            for p in data
        ]
        return clip("\n".join(rows) or f"No {state} pull requests.", limit_chars)

    @tool
    @_safe
    def gh_pr(repo: str, number: int) -> str:
        """Get a pull request's description and its diff, for reviewing.

        Args:
            repo: 'owner/name' or just 'name'.
            number: Pull request number.
        """
        slug = gh.slug(repo)
        pr = gh.get(f"/repos/{slug}/pulls/{int(number)}")
        files = gh.get(f"/repos/{slug}/pulls/{int(number)}/files", params={"per_page": 40})
        head = (
            f"#{pr['number']} {pr['title']} [{pr['state']}{', merged' if pr.get('merged') else ''}] by {pr['user']['login']}\n"
            f"{pr['head']['ref']} -> {pr['base']['ref']}, {pr['commits']} commits, {pr['changed_files']} files, +{pr['additions']} -{pr['deletions']}\n"
            f"Description: {clip(pr.get('body') or '(none)', 1200)}"
        )
        return untrusted(clip(head + "\n\n" + _format_files(files), limit_chars))

    @tool
    @_safe
    def gh_issues(repo: str, state: str = "open", limit: int = 15) -> str:
        """List issues (pull requests excluded).

        Args:
            repo: 'owner/name' or just 'name'.
            state: 'open', 'closed' or 'all'.
            limit: Max number to return (1-30).
        """
        slug = gh.slug(repo)
        data = gh.get(f"/repos/{slug}/issues", params={"state": state, "per_page": 50})
        rows = [
            f"#{i['number']} {i['title']} by {i['user']['login']} [{', '.join(lb['name'] for lb in i['labels']) or 'no labels'}]"
            for i in data
            if "pull_request" not in i
        ][: max(1, min(int(limit), 30))]
        return untrusted(clip("\n".join(rows) or f"No {state} issues.", limit_chars))

    @tool
    @_safe
    def gh_ci(repo: str, ref: Optional[str] = None) -> str:
        """Show CI / checks status for a branch, commit or pull request, with failed check names, links and summaries.

        Args:
            repo: 'owner/name' or just 'name'.
            ref: Branch, commit SHA, or a PR number like '12' or '#12'. Defaults to the default branch.
        """
        slug = gh.slug(repo)
        ref = (ref or "").strip()
        if ref.lstrip("#").isdigit():
            n = int(ref.lstrip("#"))
            pr = gh.get(f"/repos/{slug}/pulls/{n}")
            label, ref = f"PR #{n} ({pr['head']['ref']})", pr["head"]["sha"]
        else:
            ref = ref or gh.default_branch(slug)
            label = ref
        ci = gh.checks(slug, ref)
        ok = ci["passed"] or ci["legacy"] == "success"
        verdict = "FAILING" if ci["failed"] else "RUNNING" if ci["pending"] else "PASSING" if ok else "NO CI RESULTS"
        lines = [f"{slug} {label} @ {ci['sha'][:7]}: {verdict} ({ci['passed']} passed, {len(ci['pending'])} running, {len(ci['failed'])} failed)"]
        for f in ci["failed"][:8]:
            lines.append(f"FAILED {f['name']} {f['url']}" + (f"\n  {f['summary']}" if f["summary"] else ""))
        if ci["pending"]:
            lines.append("Running: " + ", ".join(ci["pending"][:8]))
        return untrusted(clip("\n".join(lines), limit_chars))

    @tool
    @_safe
    def gh_overview(repo: Optional[str] = None) -> str:
        """One-call status snapshot: default-branch CI, commits in 24h, open PRs with CI state, open issues. Prefer this for "what's going on?" questions.

        Args:
            repo: 'owner/name' or 'name'. Omit to cover all allowed repos.
        """
        from watcher import overview

        if repo:
            slugs = [gh.slug(repo)]
        elif cfg.github_allowed_repos:
            slugs = sorted(cfg.github_allowed_repos)
        else:
            return "ERROR: name a repo (no allowlist is configured)."
        return untrusted(clip(overview(gh, slugs, cfg.timezone), limit_chars))

    @tool
    @_safe
    def gh_issue(repo: str, number: int) -> str:
        """Read one issue: title, state, labels, description and the latest comments.

        Args:
            repo: 'owner/name' or just 'name'.
            number: Issue number.
        """
        slug = gh.slug(repo)
        i = gh.get(f"/repos/{slug}/issues/{int(number)}")
        comments = gh.get(f"/repos/{slug}/issues/{int(number)}/comments", params={"per_page": 30})[-5:]
        head = f"#{i['number']} {i['title']} [{i['state']}] by {i['user']['login']}; labels: {', '.join(lb['name'] for lb in i['labels']) or 'none'}"
        talk = "\n".join(f"@{c['user']['login']}: {clip(c['body'] or '', 600)}" for c in comments)
        return untrusted(clip(head + "\n\n" + clip(i.get("body") or "(no description)", 3000) + ("\n\nComments:\n" + talk if talk else ""), limit_chars))

    @tool
    @_safe
    def gh_pr_queue_status() -> str:
        """Show statistics of the PR review queue (pending, reviewing, reviewed, skipped)."""
        # Access watcher state via the shared watch.json file
        import json
        from pathlib import Path
        from config import Config
        cfg = Config.from_env()
        watch_path = Path(cfg.data_dir) / "watch.json"
        if not watch_path.exists():
            return "PR queue is empty (no watch.json found)."
        try:
            state = json.loads(watch_path.read_text())
        except (ValueError, OSError):
            return "ERROR: could not read watch state."
        queue = state.get("pr_queue", [])
        stats = {"total": len(queue), "pending": 0, "reviewing": 0, "reviewed": 0, "skipped": 0}
        for item in queue:
            status = item.get("status", "pending")
            if status in stats:
                stats[status] += 1
        lines = [f"PR Review Queue: {stats['total']} total"]
        for k, v in stats.items():
            if k != "total":
                lines.append(f"  {k}: {v}")
        return "\n".join(lines)

    @tool
    @_safe
    def gh_next_pr_for_review() -> str:
        """Get the next pending PR from the queue for automated review. Returns PR details and marks it as 'reviewing'.

        Use gh_review_pr to post your review after analyzing the PR.
        """
        import json
        from pathlib import Path
        from config import Config
        cfg = Config.from_env()
        watch_path = Path(cfg.data_dir) / "watch.json"
        if not watch_path.exists():
            return "No PRs in queue (no watch.json found)."
        try:
            state = json.loads(watch_path.read_text())
        except (ValueError, OSError):
            return "ERROR: could not read watch state."
        queue = state.get("pr_queue", [])
        for i, item in enumerate(queue):
            if item.get("status") == "pending":
                # Mark as reviewing
                from datetime import datetime, timezone
                queue[i]["status"] = "reviewing"
                queue[i]["review_started_at"] = datetime.now(timezone.utc).isoformat()
                state["pr_queue"] = queue
                watch_path.write_text(json.dumps(state))
                return (f"Next PR for review:\n"
                        f"  Repo: {item['repo']}\n"
                        f"  PR: #{item['number']}\n"
                        f"  Title: {item['title']}\n"
                        f"  Author: @{item['author']}\n"
                        f"  URL: {item['url']}\n"
                        f"  Detected: {item['detected_at']}\n\n"
                        f"Use gh_pr to read the PR details, then gh_review_pr to post your review.")
        return "No pending PRs in queue."

    @tool
    @_safe
    def gh_mark_pr_reviewed(repo: str, number: int, success: bool = True) -> str:
        """Mark a PR as reviewed (or skipped) in the queue after posting a review.

        Args:
            repo: 'owner/name' or just 'name'.
            number: Pull request number.
            success: True if review was posted successfully, False to mark as skipped.
        """
        import json
        from pathlib import Path
        from datetime import datetime, timezone
        from config import Config
        cfg = Config.from_env()
        watch_path = Path(cfg.data_dir) / "watch.json"
        if not watch_path.exists():
            return "ERROR: no watch.json found."
        try:
            state = json.loads(watch_path.read_text())
        except (ValueError, OSError):
            return "ERROR: could not read watch state."
        queue = state.get("pr_queue", [])
        for item in queue:
            if item.get("repo") == repo and item.get("number") == number:
                item["status"] = "reviewed" if success else "skipped"
                item["reviewed_at"] = datetime.now(timezone.utc).isoformat()
                state["pr_queue"] = queue
                watch_path.write_text(json.dumps(state))
                return f"Marked PR #{number} in {repo} as {'reviewed' if success else 'skipped'}."
        return f"ERROR: PR #{number} in {repo} not found in queue."

    tools += [gh_repos, gh_browse, gh_search_code, gh_commits, gh_diff, gh_prs, gh_pr, gh_issues, gh_issue, gh_ci, gh_overview, gh_pr_queue_status, gh_next_pr_for_review, gh_mark_pr_reviewed]
    return tools