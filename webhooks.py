"""GitHub webhook receiver (Feature 1 / PR B).

A small aiohttp server that listens for GitHub webhooks on ``POST /webhook/github``
and normalizes them into ``RepoEvent`` objects on the shared ``EventBus``.

The handler is deliberately fast and side-effect-free beyond the event bus: it
verifies the HMAC-SHA256 signature over the *raw* bytes, dedupes by delivery id,
allowlists the repo, maps to a ``RepoEvent``, and returns a status code immediately.
It never calls an LLM and never talks to Discord in the request path — the
one-time "webhook connected" DM is fire-and-forget. All slow work (diagnosis,
review, fixing) lives downstream in the event consumer (``main._task_for_event``).

Deployment (docs/WEBHOOKS.md): bind to 127.0.0.1 by default and expose via
Tailscale or a reverse proxy rather than opening a router port.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import time

import aiohttp.web

from config import Config
from events import (
    EventBus,
    RepoEvent,
    KIND_CI_FAILED,
    KIND_CI_PASSED,
    KIND_PR_OPENED,
    KIND_PR_READY,
    KIND_PR_UPDATED,
    KIND_PING,
)
from state import State

log = logging.getLogger(__name__)

MAX_BODY = 1 << 20  # 1 MiB cap per spec — read is capped at MAX_BODY + 1 so a
# malformed oversized payload can never be buffered in full.

# GitHub pull_request actions that map to a RepoEvent kind. Anything else is
# ignored (202) so PRs closing, merging, etc. stay silent.
_PR_KINDS = {
    "opened": KIND_PR_OPENED,
    "reopened": KIND_PR_OPENED,
    "synchronize": KIND_PR_UPDATED,
    "ready_for_review": KIND_PR_READY,
}

# workflow_run conclusions -> RepoEvent kind. Only `completed` actions are
# handled; in_progress / queued / canceled are ignored.
_FAILED_CONCLUSIONS = {"failure", "timed_out"}
_SUCCESS_CONCLUSIONS = {"success"}


def _login(actor) -> str | None:
    """GitHub payloads nest the actor as {"login": "..."}; tolerate missing/odd shape."""
    return actor.get("login") if isinstance(actor, dict) else None


class WebhookServer:
    """The aiohttp-backed GitHub webhook receiver.

    Built to live inside the Discord bot's existing event loop (started in
    ``Bot.setup_hook``, stopped in ``Bot.close``). ``start`` never raises on a
    bind failure — it logs a clear message and leaves ``started`` False so the
    bot keeps running without the receiver.
    """

    def __init__(self, cfg: Config, state: State, bus: EventBus, notify=None):
        self._cfg = cfg
        self._state = state
        self._bus = bus
        # async (text) -> None, used only for the one-time "webhook connected" DM.
        # Routed through the proactive notify policy (quiet hours + per-day cap).
        self._notify = notify
        self._last_event: float | None = None
        self._ping_dmmed: bool = False
        self._started: bool = False
        self._runner: aiohttp.web.AppRunner | None = None
        self._site: aiohttp.web.TCPSite | None = None
        self._app = aiohttp.web.Application()
        self._app.router.add_get("/healthz", self._healthz)
        self._app.router.add_post("/webhook/github", self._github)

    # ---------- public state ----------

    @property
    def started(self) -> bool:
        return self._started

    @property
    def last_event(self):
        return self._last_event

    # ---------- lifecycle ----------

    async def start(self) -> None:
        """Bind and serve in the current event loop. Returns early when
        ``webhook_enabled`` is false (nothing binds a port). Never raises on a
        bind failure — logs and continues without the receiver."""
        if not self._cfg.webhook_enabled:
            return
        self._runner = aiohttp.web.AppRunner(self._app)
        await self._runner.setup()
        self._site = aiohttp.web.TCPSite(
            self._runner, self._cfg.webhook_bind, self._cfg.webhook_port
        )
        try:
            await self._site.start()
        except OSError as exc:
            # port in use / bind failure: clean up and keep the bot alive
            await self._runner.cleanup()
            self._runner = None
            self._site = None
            log.error(
                "webhook receiver could not bind %s:%s (%s); continuing without the receiver",
                self._cfg.webhook_bind,
                self._cfg.webhook_port,
                exc,
            )
            return
        self._started = True
        log.info(
            "webhook receiver listening on %s:%s",
            self._cfg.webhook_bind,
            self._cfg.webhook_port,
        )

    async def stop(self) -> None:
        """Stop the HTTP server and release the runner. Safe to call when the
        server was never started."""
        if self._site is not None:
            await self._site.stop()
            self._site = None
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
        self._started = False

    # ---------- HTTP handlers ----------

    async def _healthz(self, request):
        return aiohttp.web.Response(status=200)

    async def _github(self, request):
        # Read the raw body, capped so an oversized payload is never buffered.
        body = await request.content.read(MAX_BODY + 1)
        status, event = self._process(
            body,
            request.headers,
            request.headers.get("X-GitHub-Event", ""),
            request.headers.get("X-GitHub-Delivery", ""),
        )
        if event is not None:
            self._last_event = event.received_at
            await self._bus.put(event)
            if event.kind == KIND_PING and not self._ping_dmmed:
                self._ping_dmmed = True
                # Fire-and-forget: the DM must never block the HTTP response.
                asyncio.create_task(self._dm_ping(event))
        return aiohttp.web.Response(status=status)

    async def _dm_ping(self, event):
        """One-time 'webhook connected' notification. Routed through the
        proactive notify policy (quiet hours + per-day cap)."""
        if self._notify is None:
            return
        try:
            await self._notify(
                f"🔔 Webhook connected — receiving GitHub events for {event.repo}"
            )
        except Exception:  # never crash the bot over a DM
            log.exception("failed to send 'webhook connected' DM")

    # ---------- request pipeline (sync, no network, no LLM) ----------

    def _process(
        self, body: bytes, headers: dict, event_name: str, delivery_id: str
    ) -> tuple[int, RepoEvent | None]:
        """Run the webhook request pipeline. Returns (http_status, event_or_None).

        Order matters (spec §3.1): signature check is BEFORE JSON parsing; the
        size cap is checked first; dedupe and allowlist come next; the mapped
        event is returned last. No network or LLM calls — this is the only
        work the request handler does, which is why it returns in <100 ms.
        """
        # 1. Reject bodies over the 1 MB cap (read above already capped at MAX_BODY+1).
        if len(body) > MAX_BODY:
            return 413, None
        # 2. Verify the HMAC-SHA256 signature over the RAW bytes.
        expected = hmac.new(
            self._cfg.webhook_secret.encode("utf-8"), body, hashlib.sha256
        ).hexdigest()
        actual = headers.get("X-Hub-Signature-256", "")
        if actual.startswith("sha256="):
            actual = actual[7:]
        if not hmac.compare_digest(actual, expected):
            log.warning(
                "webhook POST /webhook/github: rejecting request (bad or missing signature)"
            )
            return 401, None
        # 3. Parse the JSON payload (untrusted data — read scalars only).
        try:
            payload = json.loads(body)
        except (ValueError, TypeError):
            log.debug("webhook POST /webhook/github: dropping non-JSON payload")
            return 202, None
        # 4. Dedupe by delivery id — a replay returns 200 and produces no event.
        if not self._state.record_delivery(delivery_id):
            log.debug("webhook POST /webhook/github: replayed delivery %s", delivery_id)
            return 200, None
        # 5. Allowlist the repo (every path refuses repos outside the allowlist).
        repo = self._repo_name(payload)
        if repo is None or repo.lower() not in self._cfg.github_allowed_repos:
            log.debug(
                "webhook POST /webhook/github: %r not in GITHUB_ALLOWED_REPOS; dropping", repo
            )
            return 202, None
        # 6. Map to a RepoEvent.
        event = self._map_event(event_name, payload, repo, delivery_id)
        if event is None:
            log.debug(
                "webhook POST /webhook/github: event kind %r not handled; dropping",
                event_name,
            )
            return 202, None
        return (200 if event.kind == KIND_PING else 202), event

    @staticmethod
    def _repo_name(payload):
        """The "owner/name" of the base repo from the payload, if present."""
        r = payload.get("repository") if isinstance(payload, dict) else None
        return (r.get("full_name") if isinstance(r, dict) else None) or None

    def _map_event(self, event_name, payload, repo, delivery_id):
        """Map a GitHub event name + payload to a ``RepoEvent``, or None to ignore.

        Payloads are untrusted data: we only READ scalar fields (login, sha,
        ref, number, id, conclusion, action), never execute anything.
        """
        name = (event_name or "").lower()
        now = time.time()
        actor = _login(payload.get("actor")) if isinstance(payload, dict) else None

        if name == "ping":
            return RepoEvent(
                kind=KIND_PING,
                repo=repo,
                delivery_id=delivery_id,
                pr_number=None,
                branch=None,
                head_sha=None,
                run_id=None,
                actor=actor,
                received_at=now,
            )

        if name == "pull_request":
            kind = _PR_KINDS.get((payload.get("action") or "").lower())
            if kind is None:
                return None
            pr = payload.get("pull_request") if isinstance(payload.get("pull_request"), dict) else {}
            head = pr.get("head") if isinstance(pr.get("head"), dict) else {}
            head_repo_raw = head.get("repo") if isinstance(head.get("repo"), dict) else {}
            # head_repo is the fork's "owner/name" for fork PRs (differs from `repo`).
            head_repo = head_repo_raw.get("full_name") if isinstance(head_repo_raw, dict) else None
            return RepoEvent(
                kind=kind,
                repo=repo,
                delivery_id=delivery_id,
                pr_number=pr.get("number"),
                branch=head.get("ref"),
                head_sha=head.get("sha"),
                head_repo=head_repo,
                run_id=None,
                actor=actor,
                received_at=now,
            )

        if name == "workflow_run":
            run = (
                payload.get("workflow_run")
                if isinstance(payload.get("workflow_run"), dict)
                else {}
            )
            action = (run.get("action") or payload.get("action") or "").lower()
            if action != "completed":
                return None
            conclusion = (run.get("conclusion") or "").lower()
            if conclusion in _FAILED_CONCLUSIONS:
                kind = KIND_CI_FAILED
            elif conclusion in _SUCCESS_CONCLUSIONS:
                kind = KIND_CI_PASSED
            else:
                return None
            return RepoEvent(
                kind=kind,
                repo=repo,
                delivery_id=delivery_id,
                pr_number=None,
                branch=run.get("head_branch") or run.get("base_branch"),
                head_sha=run.get("head_sha"),
                run_id=run.get("id"),
                actor=actor,
                received_at=now,
            )

        return None  # unrecognized / unhandled kind -> 202, no event
