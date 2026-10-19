"""GitHub watcher + daily digest. Plain polling: NO LLM calls, so it costs no free-tier requests.

  * Watcher: notices new open PRs and newly failing CI (PRs and the default branch), once each.
    It also emits normalized `RepoEvent`s (KIND_PR_OPENED / KIND_CI_FAILED) that `main._watch_loop`
    drains onto the `EventBus`, so one handler set serves both polling and webhooks.
  * Digest:  a snapshot of every allowlisted repo (also available on demand via !digest / gh_overview).
  * PR Queue: queues newly detected PRs for automated agent review.
"""
import json
import logging
import os
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from config import Config
from ghclient import GitHub, GitHubError
from events import KIND_CI_FAILED, KIND_PR_OPENED, RepoEvent, synthetic_delivery_id
from state import State

log = logging.getLogger("watcher")

MAX_REPOS = 8


@dataclass
class QueuedPR:
    """A PR queued for automated review."""
    repo: str
    number: int
    title: str
    url: str
    author: str
    detected_at: str  # ISO format
    status: str = "pending"  # pending, reviewing, reviewed, skipped


def age(iso: str) -> str:
    try:
        delta = datetime.now(timezone.utc) - datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return "?"
    mins = int(delta.total_seconds() // 60)
    return f"{mins}m" if mins < 60 else f"{mins // 60}h" if mins < 1440 else f"{mins // 1440}d"


def ci_icon(ci: dict) -> str:
    if ci["failed"]:
        return "❌"
    if ci["pending"]:
        return "⏳"
    return "✅" if (ci["passed"] or ci["legacy"] == "success") else "➖"


def repo_section(gh: GitHub, slug: str, max_prs: int = 6) -> list[str]:
    default = gh.default_branch(slug)
    main_ci = gh.checks(slug, default)
    prs = gh.get(f"/repos/{slug}/pulls", params={"state": "open", "per_page": 15})
    issues = [i for i in gh.get(f"/repos/{slug}/issues", params={"state": "open", "per_page": 30}) if "pull_request" not in i]
    since = (datetime.now(timezone.utc) - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ")
    commits = gh.get(f"/repos/{slug}/commits", params={"sha": default, "since": since, "per_page": 30})
    lines = [
        f"**{slug}** {ci_icon(main_ci)} `{default}` · {len(commits)} commit(s) in 24h · "
        f"{len(prs)} open PR(s) · {len(issues)} open issue(s)"
    ]
    for pr in prs[:max_prs]:
        try:
            icon = ci_icon(gh.checks(slug, pr["head"]["sha"]))
        except GitHubError:
            icon = "❔"
        lines.append(f"  • #{pr['number']} {pr['title'][:70]} {icon} · {age(pr['created_at'])} · @{pr['user']['login']}")
    if len(prs) > max_prs:
        lines.append(f"  • …and {len(prs) - max_prs} more PR(s)")
    for i in issues[:3]:
        lines.append(f"  ◦ issue #{i['number']} {i['title'][:70]}")
    return lines


def overview(gh: GitHub, slugs: list[str], tz_name: str = "UTC") -> str:
    try:
        now = datetime.now(ZoneInfo(tz_name))
    except Exception:  # noqa: BLE001
        now = datetime.now(timezone.utc)
    lines = [f"📋 **Repo digest** - {now:%a %d %b %H:%M}"]
    for slug in slugs[:MAX_REPOS]:
        try:
            lines += repo_section(gh, slug)
        except GitHubError as e:
            lines.append(f"**{slug}** ⚠️ {e}")
    if len(lines) == 1:
        lines.append("No repositories configured (set GITHUB_ALLOWED_REPOS).")
    return "\n".join(lines)


def seconds_until(hhmm: str, tz_name: str, weekday: "int | None" = None) -> float:
    """Real seconds until the next HH:MM wall-clock time in tz (DST-safe). weekday: 0=Mon..6=Sun, None = any day."""
    tz = ZoneInfo(tz_name)
    now = datetime.now(tz)
    h, m = (int(x) for x in hhmm.split(":"))
    target = now.replace(hour=h, minute=m, second=0, microsecond=0)
    if weekday is not None:
        target += timedelta(days=(weekday - now.weekday()) % 7)
    if target <= now:
        target += timedelta(days=1 if weekday is None else 7)
    return (target.astimezone(timezone.utc) - now.astimezone(timezone.utc)).total_seconds()


class Watcher:
    def __init__(self, cfg: Config, state: State | None = None):
        self.cfg = cfg
        self.gh = GitHub(cfg)  # own session: safe to use from a background thread
        self.repos = sorted(cfg.github_allowed_repos)[:MAX_REPOS]
        self.path = Path(cfg.data_dir) / "watch.json"
        self._lock = threading.RLock()
        self.state: dict = {"repos": {}, "last_digest": "", "pr_queue": []}
        # State store (dedupe/caps) for proactive features; optional in the digest-only path.
        self._state = state
        # RepoEvents accumulated during poll() and handed to the async bus.
        self._pending_events: list[RepoEvent] = []
        self._events_lock = threading.Lock()
        self.last_poll = 0.0
        self.last_error = ""
        self._load()

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.github_token and self.repos)

    def _load(self) -> None:
        try:
            self.state.update(json.loads(self.path.read_text()))
        except FileNotFoundError:
            pass
        except (ValueError, OSError):
            log.warning("watch state unreadable; starting fresh")

    def save(self) -> None:
        with self._lock:  # RLock: poll() calls this while holding the lock
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.state))
            os.replace(tmp, self.path)

    def digest(self) -> str:
        with self._lock:  # the poller shares this HTTP session
            return overview(self.gh, self.repos, self.cfg.timezone)

    def poll(self) -> list[str]:
        """One pass over all repos (blocking; run it in a thread). Returns messages to send."""
        with self._lock:
            before = json.dumps(self.state, sort_keys=True)
            messages: list[str] = []
            errors = []
            for slug in self.repos:
                try:
                    messages += self._poll_repo(slug)
                except GitHubError as e:
                    errors.append(f"{slug}: {e}")
                    log.warning("watch %s failed: %s", slug, e)
                except Exception as e:  # noqa: BLE001 - odd payloads must not stop the other repos
                    errors.append(f"{slug}: unexpected {type(e).__name__}")
                    log.exception("watch %s crashed", slug)
            self.last_poll = time.time()
            self.last_error = "; ".join(errors)
            if json.dumps(self.state, sort_keys=True) != before:
                self.save()  # only touch the SD card when something changed
            return messages

    def _poll_repo(self, slug: str) -> list[str]:
        rs = self.state["repos"].setdefault(slug, {"prs": [], "ci": [], "init": False})
        quiet = not rs["init"]  # first pass only records a baseline: no flood of old PRs / failures
        out: list[str] = []

        prs = self.gh.get(f"/repos/{slug}/pulls", params={"state": "open", "per_page": 15, "sort": "updated", "direction": "desc"})
        known = set(rs["prs"])
        for pr in prs:
            if pr["number"] not in known and not quiet:
                out.append(f"🆕 **{slug}** PR #{pr['number']}: {pr['title'][:100]} by @{pr['user']['login']}\n<{pr['html_url']}>")
                # Queue the new PR for automated review
                queued = QueuedPR(
                    repo=slug,
                    number=pr["number"],
                    title=pr["title"],
                    url=pr["html_url"],
                    author=pr["user"]["login"],
                    detected_at=datetime.now(timezone.utc).isoformat(),
                )
                self.state["pr_queue"].append(asdict(queued))
                self._add_event(RepoEvent(
                    kind=KIND_PR_OPENED,
                    repo=slug,
                    delivery_id=synthetic_delivery_id(slug, KIND_PR_OPENED, pr["head"]["sha"]),
                    pr_number=pr["number"],
                    branch=pr["head"].get("ref"),
                    head_sha=pr["head"]["sha"],
                    run_id=None,
                    actor=pr["user"]["login"],
                    received_at=time.time(),
                ))
        rs["prs"] = [pr["number"] for pr in prs]

        targets = [(f"PR #{pr['number']}", pr["head"]["sha"]) for pr in prs[:8]] + [(f"`{self.gh.default_branch(slug)}`", self.gh.default_branch(slug))]
        seen = deque(rs["ci"], maxlen=400)
        for label, ref in targets:
            is_pr = label.startswith("PR #")
            pr_number = int(label.split("PR #", 1)[1]) if is_pr else None
            branch = label.strip("`") if not is_pr else None
            try:
                ci = self.gh.checks(slug, ref)
            except GitHubError as e:
                log.info("no CI info for %s %s: %s", slug, label, e)
                continue
            for f in ci["failed"]:
                key = f"{f['sha'][:12]}:{f['name']}"
                if key in seen:
                    continue
                seen.append(key)
                if not quiet:
                    out.append(f"❌ **{slug}** {label}: CI check `{f['name']}` failed\n<{f['url']}>" if f["url"] else f"❌ **{slug}** {label}: CI check `{f['name']}` failed")
                    self._add_event(RepoEvent(
                        kind=KIND_CI_FAILED,
                        repo=slug,
                        delivery_id=synthetic_delivery_id(slug, KIND_CI_FAILED, f["sha"][:12]),
                        pr_number=pr_number,
                        branch=branch,
                        head_sha=f["sha"][:12],
                        run_id=None,
                        actor=None,
                        received_at=time.time(),
                    ))
        rs["ci"] = list(seen)
        rs["init"] = True
        return out

    # -- event emission (normalized RepoEvents for the async bus) --
    def _add_event(self, event: RepoEvent) -> None:
        """Accumulate one event for the next drain. Thread-safe: poll() runs in a thread."""
        with self._events_lock:
            self._pending_events.append(event)

    def drain_events(self) -> list[RepoEvent]:
        """Return and clear the events accumulated since the last drain."""
        with self._events_lock:
            events = list(self._pending_events)
            self._pending_events.clear()
            return events

    # -- PR Queue methods for automated review --
    def get_pr_queue(self) -> list[dict]:
        """Get the current PR queue (all items)."""
        with self._lock:
            return list(self.state.get("pr_queue", []))

    def get_next_pending_pr(self) -> dict | None:
        """Get the next pending PR from the queue, mark it as 'reviewing'."""
        with self._lock:
            queue = self.state.get("pr_queue", [])
            for i, item in enumerate(queue):
                if item.get("status") == "pending":
                    queue[i]["status"] = "reviewing"
                    queue[i]["review_started_at"] = datetime.now(timezone.utc).isoformat()
                    self.save()
                    return queue[i]
            return None

    def mark_pr_reviewed(self, repo: str, number: int, success: bool = True) -> bool:
        """Mark a PR as reviewed (or skipped if failed)."""
        with self._lock:
            queue = self.state.get("pr_queue", [])
            for item in queue:
                if item.get("repo") == repo and item.get("number") == number:
                    item["status"] = "reviewed" if success else "skipped"
                    item["reviewed_at"] = datetime.now(timezone.utc).isoformat()
                    self.save()
                    return True
            return False

    def get_queue_stats(self) -> dict:
        """Get statistics about the PR queue."""
        with self._lock:
            queue = self.state.get("pr_queue", [])
            stats = {"total": len(queue), "pending": 0, "reviewing": 0, "reviewed": 0, "skipped": 0}
            for item in queue:
                status = item.get("status", "pending")
                if status in stats:
                    stats[status] += 1
            return stats
