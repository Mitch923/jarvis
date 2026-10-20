"""Normalized GitHub events + an async bus they flow through.

Features 2-5 (CI auto-fix, auto-review) and the future webhook receiver all
emit the same `RepoEvent` shape onto one `EventBus`, so the same handler code
works whether the trigger came from polling (the watcher) or a webhook.

The bus is deliberately a plain `asyncio.Queue`: one producer may `put`, one
consumer `get`s. Polling and webhooks run in the asyncio context, so both can
drive the same queue. The watcher emits polled events with *synthetic* delivery
ids (`poll:{repo}:{kind}:{sha}`) so that, together with the real
`X-GitHub-Delivery` ids from webhooks, every event has a unique key that the
state store can dedupe on (see state.py `seen_deliveries`).
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass

__all__ = ["RepoEvent", "EventBus"]

# The full set of event kinds. Handlers switch on `kind`; anything unknown is
# ignored (202/200 in the webhook, skipped here).
KIND_PING = "ping"
KIND_PR_OPENED = "pr_opened"
KIND_PR_UPDATED = "pr_updated"
KIND_PR_READY = "pr_ready"
KIND_CI_FAILED = "ci_failed"
KIND_CI_PASSED = "ci_passed"

VALID_KINDS = frozenset(
    (KIND_PING, KIND_PR_OPENED, KIND_PR_UPDATED, KIND_PR_READY, KIND_CI_FAILED, KIND_CI_PASSED)
)


def synthetic_delivery_id(repo: str, kind: str, sha: str | None = None) -> str:
    """Delivery id for a *polled* event. A webhook's id is the raw X-GitHub-Delivery value."""
    return f"poll:{repo}:{kind}:{sha or 'x'}"


@dataclass(frozen=True)
class RepoEvent:
    """One normalized GitHub event, regardless of how it arrived (poll or webhook)."""

    kind: str  # see KIND_* above
    repo: str  # "owner/name"
    delivery_id: str  # X-GitHub-Delivery, or synthetic poll id
    pr_number: int | None
    branch: str | None
    head_sha: str | None
    run_id: int | None
    actor: str | None
    received_at: float  # time.time() at first reception
    # Only set by the webhook receiver for pull_request events. For fork PRs this is
    # the fork's "owner/name" (and differs from `repo`); handlers refuse to push to a
    # fork when `head_repo is not None and head_repo != repo`.
    head_repo: str | None = None


class EventBus:
    """A tiny async queue of RepoEvent. Producers put; the single consumer gets."""

    def __init__(self) -> None:
        # Safe to construct outside a running loop: CPython's queue binds to the
        # loop that first drives it (verified across this env's 3.12).
        self._q: asyncio.Queue = asyncio.Queue()

    async def put(self, event: RepoEvent) -> None:
        await self._q.put(event)

    async def get(self, timeout: float | None = None) -> RepoEvent | None:
        try:
            return await asyncio.wait_for(self._q.get(), timeout)
        except asyncio.TimeoutError:
            return None

    def size(self) -> int:
        return self._q.qsize()

    def drain(self) -> list[RepoEvent]:
        """Remove everything currently queued, returning it (for shutdown/tests)."""
        out: list[RepoEvent] = []
        while not self._q.empty():
            try:
                out.append(self._q.get_nowait())
            except asyncio.QueueEmpty:
                break
        return out

    async def close(self) -> None:
        self.drain()
        # Convoluted shutdown: the consumer is a task we cancel separately in
        # Bot.close(), so there is nothing else to wait on here.
