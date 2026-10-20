"""GitHub webhook receiver (Feature 1 / PR B) tests.

Standalone script (no framework): prints PASS <desc> per check; hard-asserts fail.
No real network, no GitHub, no Discord, no LLM. The handler logic is pure
(HMAC, JSON, state file, in-memory queue). The only real I/O is an in-process
aiohttp client talking to the receiver on a localhost port (latency / port tests).

    python tests/run_all.py webhook
"""
import asyncio
import contextlib
import dataclasses
import hashlib
import hmac
import json
import os
import socket
import sys
import tempfile
import time
from types import SimpleNamespace as NS
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import aiohttp


def _free_port() -> int:
    """Bind an ephemeral socket to grab a free localhost port, then release it."""
    s = socket.socket(family=socket.AF_INET, type=socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    try:
        return s.getsockname()[1]
    finally:
        s.close()


# A real (faked) LLM server so `import main` / the Bot don't need a model.
import fake_llm

_, _base = fake_llm.start()

DATA_DIR = tempfile.mkdtemp()
os.environ.update(
    DATA_DIR=DATA_DIR,
    # Discord
    DISCORD_TOKEN="x",
    DISCORD_ALLOWED_USER_IDS="42",
    # LLM
    OPENROUTER_API_KEY="k",
    OPENROUTER_BASE_URL=_base,
    MODELS="m1",
    LLM_TIMEOUT="2",
    # GitHub
    GITHUB_TOKEN="tok",
    GITHUB_ALLOWED_REPOS="me/proj",
    GITHUB_WRITE="1",
    SELF_REPO="me/proj",
    AGENT_TIMEZONE="UTC",
    # Webhook
    WEBHOOK_ENABLED="1",
    WEBHOOK_BIND="127.0.0.1",
    WEBHOOK_PORT="8787",
    WEBHOOK_SECRET="my-secret",
)  # fmt: skip

from config import Config  # noqa: E402
import main as M  # noqa: E402
from events import EventBus, RepoEvent  # noqa: E402
from state import State  # noqa: E402
import webhooks  # noqa: E402

ok = lambda s: print("PASS", s)
cfg = Config.from_env()
SECRET = "my-secret"


def sign(body: bytes, secret: str = SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def payload_json(**kw) -> bytes:
    d = {"repository": {"full_name": "me/proj"}}
    d.update(kw)
    return json.dumps(d).encode("utf-8")


def new_server(enabled: bool = True, port: int | None = None) -> webhooks.WebhookServer:
    c = dataclasses.replace(cfg, webhook_enabled=enabled)
    if port is not None:
        c = dataclasses.replace(c, webhook_port=port)
    return webhooks.WebhookServer(c, State(c), EventBus(), notify=None)


# ---------- signature / auth ----------

def test_missing_signature():
    state = State(cfg)
    bus = EventBus()
    w = webhooks.WebhookServer(cfg, state, bus, notify=None)
    body = payload_json(action="opened", pull_request={"number": 1})
    status, ev = w._process(body, {}, "pull_request", "DEL-miss")
    assert status == 401 and ev is None, (status, ev)
    ok("webhook: missing signature header -> 401, no event")


def test_bad_signature():
    state = State(cfg)
    bus = EventBus()
    w = webhooks.WebhookServer(cfg, state, bus, notify=None)
    body = payload_json(action="opened", pull_request={"number": 1})
    status, ev = w._process(body, {"X-Hub-Signature-256": sign(body, "wrong-secret")}, "pull_request", "DEL-bad")
    assert status == 401 and ev is None, (status, ev)
    ok("webhook: bad signature -> 401, no event")


# ---------- a valid event ----------

def test_pr_opened():
    state = State(cfg)
    bus = EventBus()
    w = webhooks.WebhookServer(cfg, state, bus, notify=None)
    body = payload_json(
        action="opened",
        pull_request={"number": 1, "head": {"ref": "agent/b", "sha": "abc",
                                           "repo": {"full_name": "me/proj"}}},
        actor={"login": "mitch"},
    )
    status, ev = w._process(body, {"X-Hub-Signature-256": sign(body)}, "pull_request", "DEL-open")
    assert status == 202 and ev is not None, (status, ev)
    assert ev.kind == "pr_opened" and ev.repo == "me/proj" and ev.pr_number == 1
    assert ev.branch == "agent/b" and ev.head_sha == "abc" and ev.actor == "mitch"
    assert ev.head_repo == "me/proj", ev.head_repo  # non-fork: head == base
    ok("webhook: pull_request/opened -> 202 + pr_opened event (fields mapped)")


def test_fork_pr_sets_head_repo():
    state = State(cfg)
    bus = EventBus()
    w = webhooks.WebhookServer(cfg, state, bus, notify=None)
    body = payload_json(
        action="opened",
        pull_request={"number": 2, "head": {"ref": "feature", "sha": "fff",
                                           "repo": {"full_name": "some/fork"}}},
    )
    status, ev = w._process(body, {"X-Hub-Signature-256": sign(body)}, "pull_request", "DEL-fork")
    assert status == 202 and ev is not None
    assert ev.kind == "pr_opened" and ev.head_repo == "some/fork" and ev.repo == "me/proj"
    ok("webhook: fork PR -> head_repo set to fork (differs from base repo)")


# ---------- idempotent replay ----------

def test_replay_dedupe():
    state = State(cfg)
    bus = EventBus()
    w = webhooks.WebhookServer(cfg, state, bus, notify=None)
    body = payload_json(action="opened", pull_request={"number": 1})
    hdrs = {"X-Hub-Signature-256": sign(body)}
    s1, ev1 = w._process(body, hdrs, "pull_request", "DEL-replay")
    s2, ev2 = w._process(body, hdrs, "pull_request", "DEL-replay")  # replay
    assert s1 == 202 and ev1 is not None, (s1, ev1)
    assert s2 == 200 and ev2 is None, (s2, ev2)
    ok("webhook: replayed delivery -> exactly one event (1st 202+event, replay 200+None)")


# ---------- allowlist ----------

def test_non_allowlisted_repo():
    state = State(cfg)
    bus = EventBus()
    w = webhooks.WebhookServer(cfg, state, bus, notify=None)
    body = payload_json(action="opened", pull_request={"number": 1},
                        repository={"full_name": "evil/other"})
    status, ev = w._process(body, {"X-Hub-Signature-256": sign(body)}, "pull_request", "DEL-evil")
    assert status == 202 and ev is None, (status, ev)
    ok("webhook: non-allowlisted repo -> no event, no DM (GITHUB_ALLOWED_REPOS)")


# ---------- oversize ----------

def test_oversize_body():
    state = State(cfg)
    bus = EventBus()
    w = webhooks.WebhookServer(cfg, state, bus, notify=None)
    body = b"x" * (webhooks.MAX_BODY + 1)
    status, ev = w._process(body, {}, "pull_request", "DEL-big")
    assert status == 413 and ev is None, (status, ev)
    ok("webhook: body over 1 MiB -> 413, no event")


# ---------- event mapping ----------

def test_event_mapping():
    state = State(cfg)
    bus = EventBus()
    w = webhooks.WebhookServer(cfg, state, bus, notify=None)

    # ping -> 200 + KIND_PING
    b = payload_json(action="x")
    s, e = w._process(b, {"X-Hub-Signature-256": sign(b)}, "ping", "D-ping")
    assert s == 200 and e is not None and e.kind == "ping", (s, e)
    ok("webhook: ping -> 200 + KIND_PING")

    # pull_request synchronize -> pr_updated
    b = payload_json(action="synchronize", pull_request={"number": 2})
    s, e = w._process(b, {"X-Hub-Signature-256": sign(b)}, "pull_request", "D-sync")
    assert s == 202 and e is not None and e.kind == "pr_updated", (s, e)
    ok("webhook: pull_request/synchronize -> pr_updated")

    # pull_request ready_for_review -> pr_ready
    b = payload_json(action="ready_for_review", pull_request={"number": 3})
    s, e = w._process(b, {"X-Hub-Signature-256": sign(b)}, "pull_request", "D-ready")
    assert s == 202 and e is not None and e.kind == "pr_ready", (s, e)
    ok("webhook: pull_request/ready_for_review -> pr_ready")

    # workflow_run completed/failure -> ci_failed
    b = payload_json(action="completed",
                     workflow_run={"id": 99, "action": "completed", "conclusion": "failure",
                                  "head_sha": "deadbeef", "head_branch": "agent/fix-1"})
    s, e = w._process(b, {"X-Hub-Signature-256": sign(b)}, "workflow_run", "D-fail")
    assert s == 202 and e is not None and e.kind == "ci_failed", (s, e)
    assert e.run_id == 99 and e.head_sha == "deadbeef" and e.branch == "agent/fix-1", (e.run_id, e.head_sha)
    ok("webhook: workflow_run/completed/failure -> ci_failed (run_id, head_sha, branch)")

    # workflow_run completed/success -> ci_passed
    b = payload_json(action="completed",
                     workflow_run={"id": 100, "action": "completed", "conclusion": "success",
                                  "head_sha": "cafebabe"})
    s, e = w._process(b, {"X-Hub-Signature-256": sign(b)}, "workflow_run", "D-pass")
    assert s == 202 and e is not None and e.kind == "ci_passed" and e.run_id == 100, (s, e)
    ok("webhook: workflow_run/completed/success -> ci_passed")

    # workflow_run in_progress -> ignored (no event)
    b = payload_json(action="in_progress", workflow_run={"id": 101, "action": "in_progress", "conclusion": ""})
    s, e = w._process(b, {"X-Hub-Signature-256": sign(b)}, "workflow_run", "D-inprog")
    assert s == 202 and e is None, (s, e)
    ok("webhook: workflow_run/in_progress -> no event (ignored)")

    # unknown event type -> 202, no event
    b = payload_json(action="x")
    s, e = w._process(b, {"X-Hub-Signature-256": sign(b)}, "some_other_event", "D-unk")
    assert s == 202 and e is None, (s, e)
    ok("webhook: unrecognized event type -> 202, no event")


# ---------- handler latency (no network / LLM in the handler) ----------

async def test_http_roundtrip_latency():
    port = _free_port()
    w = new_server(port=port)
    await w.start()
    assert w.started
    body = payload_json(action="opened", pull_request={"number": 1})
    hdrs = {"X-GitHub-Event": "pull_request", "X-Hub-Signature-256": sign(body)}
    url = f"http://127.0.0.1:{port}/webhook/github"
    async with aiohttp.ClientSession() as client:
        # warm-up so we measure steady-state, not first-request setup
        async with client.post(url, data=body, headers={**hdrs, "X-GitHub-Delivery": "D-warm"}) as r:
            await r.read()
        t0 = time.monotonic()
        async with client.post(url, data=body, headers={**hdrs, "X-GitHub-Delivery": "D-meas"}) as r:
            await r.read()
            assert r.status == 202, r.status
        dt = time.monotonic() - t0
    await w.stop()
    assert dt < 0.1, f"HTTP round-trip {dt * 1000:.1f}ms >= 100ms"
    ok(f"webhook: real HTTP handler round-trip {dt * 1000:.1f}ms (<100ms, no network/LLM inside)")


async def test_disabled_no_bind():
    w = new_server(enabled=False)
    assert not w.started
    await w.start()
    assert not w.started and w._runner is None and w._site is None, (w.started, w._runner)
    ok("webhook: WEBHOOK_ENABLED=false -> nothing starts, no port bound")


async def test_port_in_use_continues():
    port = _free_port()
    first = new_server(port=port)
    await first.start()
    assert first.started, "first server should bind the free port"
    # second server on the same port must NOT raise and must not start
    second = new_server(port=port)
    try:
        await second.start()
    except Exception:  # noqa: BLE001 - must never crash the bot
        raise AssertionError("port-in-use must not raise")
    assert not second.started, "second server must not claim the port"
    await first.stop()
    ok("webhook: port already in use -> logs error, does not crash, no double bind")


# ---------- !status reports receiver state + last-event ----------

class FakeCh:
    def __init__(self, cid):
        self.id, self.sent = cid, []

    async def send(self, text, view=None):
        m = NS(content=text, deleted=False, replies=[])
        self.sent.append(m)
        return m

    @contextlib.asynccontextmanager
    async def typing(self):
        yield


def user_msg(text, uid=42):
    m = NS(author=NS(id=uid, bot=False), content=text, guild=None, channel=None, mentions=[])
    m.replies = []
    async def reply(t, mention_author=False):
        m.replies.append(t)
    m.reply = reply
    return m


def _bot(data_dir, port=None, webhook_enabled=None):
    c = dataclasses.replace(cfg, data_dir=data_dir)
    if port is not None:
        c = dataclasses.replace(c, webhook_port=port)
    if webhook_enabled is not None:
        c = dataclasses.replace(c, webhook_enabled=webhook_enabled)
    bot = M.Bot(c)
    bot._connection.user = NS(id=999, __str__=lambda s: "bot")
    return bot


async def _start_bot_webhook(bot):
    bot.webhook = webhooks.WebhookServer(bot.cfg, bot.state, bot.bus, notify=lambda: None)
    await bot.webhook.start()
    assert bot.webhook.started
    return bot


async def status_webhook_line():
    port = _free_port()
    bot = _bot(tempfile.mkdtemp(), port=port)
    await _start_bot_webhook(bot)
    body = payload_json(action="opened", pull_request={"number": 1})
    async with aiohttp.ClientSession() as client:
        async with client.post(
            f"http://127.0.0.1:{port}/webhook/github", data=body,
            headers={"X-GitHub-Event": "pull_request",
                     "X-Hub-Signature-256": sign(body), "X-GitHub-Delivery": "D-status"},
        ) as r:
            assert r.status == 202, r.status
    m = user_msg("!status")
    await bot.on_message(m)
    r = m.replies[0]
    assert "Webhook" in r and "up · listening on" in r and "last event" in r, r
    await bot.webhook.stop()
    ok("!status: reports webhook up + listening address + last-event timestamp")


async def status_webhook_off():
    # WEBHOOK_ENABLED=0 -> nothing binds; !status must say "off".
    bot = _bot(tempfile.mkdtemp(), webhook_enabled=False)
    m = user_msg("!status")
    await bot.on_message(m)
    r = m.replies[0]
    assert "Webhook" in r and "off" in r, r
    ok("!status: reports webhook off when WEBHOOK_ENABLED=0")


async def main():
    test_missing_signature()
    test_bad_signature()
    test_pr_opened()
    test_fork_pr_sets_head_repo()
    test_replay_dedupe()
    test_non_allowlisted_repo()
    test_oversize_body()
    test_event_mapping()
    await test_disabled_no_bind()
    await test_port_in_use_continues()
    await test_http_roundtrip_latency()
    await status_webhook_line()
    await status_webhook_off()
    ok("webhook: all receiver checks passed")


if __name__ == "__main__":
    asyncio.run(main())
