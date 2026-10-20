"""Shared event/state layer tests: TaskQueue priority + cap, proactive notify
policy (quiet-hours batching + per-day cap), !status queue line, and the
PROACTIVE (fire-and-forget) self-review path.

Standalone script (no framework): prints PASS <desc> per check; hard-asserts
fail. Uses a fake LLM only indirectly via the shared fakes; the queue/policy
logic itself is deterministic and needs no model.
"""
import asyncio, contextlib, dataclasses, os, sys, tempfile
from types import SimpleNamespace as NS
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))
import fake_llm
_, base = fake_llm.start()

os.environ.update(
    DATA_DIR=tempfile.mkdtemp(),
    DISCORD_TOKEN="x", OPENROUTER_API_KEY="k",
    DISCORD_ALLOWED_USER_IDS="42",
    OPENROUTER_BASE_URL=base, MODELS="m1", LLM_TIMEOUT="2",
    GITHUB_TOKEN="tok", GITHUB_ALLOWED_REPOS="me/proj", GITHUB_WRITE="1",
    SELF_REPO="me/proj", AGENT_TIMEZONE="UTC",
    SELF_REVIEW_MIN_EVENTS="1", APPROVAL_TIMEOUT="3",
)  # fmt: skip
from config import Config
import main as M
from friction import Friction
ok = lambda s: print("PASS", s)


# ---------- shared fakes ----------
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


def user_msg(text, uid=42, ch=None):
    ch = ch or FakeCh(1)
    m = NS(author=NS(id=uid, bot=False), content=text, guild=None, channel=ch, mentions=[])
    m.replies = []

    async def reply(t, mention_author=False):
        m.replies.append(t)

    m.reply = reply
    return m


BASE = Config.from_env()


def _bot(data_dir, **kw):
    """A headless bot on an isolated DATA_DIR (fresh state.json + friction)."""
    bot = M.Bot(dataclasses.replace(BASE, data_dir=data_dir, **kw))
    # Pretend we're logged in so on_message() can strip mentions / check the mention.
    bot._connection.user = NS(id=999, __str__=lambda s: "bot")
    return bot


def _mk_task(cfg, priority):
    return M.Task(
        kind="user", prompt="x", message=None, channel=FakeCh(1), user_id=42,
        tool_names=set(), max_steps=1, remember=False, issue_budget=0,
        header=None, timeout=None, priority=priority,
    )


# ---------- 1. queue: USER before PROACTIVE, FIFO within a priority ----------
async def queue_priority():
    td = tempfile.mkdtemp()
    cfg = dataclasses.replace(BASE, data_dir=td, proactive_queue_max=3)
    q = M.TaskQueue(cfg, friction=Friction(td))
    p = M.TaskQueue.PROACTIVE
    u = M.TaskQueue.USER
    a = _mk_task(cfg, p)   # enqueued first, but PROACTIVE
    b = _mk_task(cfg, u)   # enqueued second, USER  -> must run first (priority)
    c = _mk_task(cfg, p)   # enqueued third, PROACTIVE
    await q.enqueue(a); await q.enqueue(b); await q.enqueue(c)
    got = [await q.pop(), await q.pop(), await q.pop()]
    assert got == [b, a, c], got
    assert b.kind == "user" and b.priority == u
    assert got[1].priority == p and got[2].priority == p
    await q.mark_done()
    ok("queue: USER outranks PROACTIVE; FIFO (by seq) within same priority")


# ---------- 2. queue: proactive cap drops the oldest and records friction ----------
async def proactive_cap():
    td = tempfile.mkdtemp()
    cfg = dataclasses.replace(BASE, data_dir=td, proactive_queue_max=2)
    f = Friction(td)
    q = M.TaskQueue(cfg, friction=f)
    p = M.TaskQueue.PROACTIVE
    x, y, z = _mk_task(cfg, p), _mk_task(cfg, p), _mk_task(cfg, p)
    await q.enqueue(x); await q.enqueue(y); await q.enqueue(z)  # x oldest, dropped at 3rd
    assert await q.proactive_pending() == 2
    assert await q.queued() == 2
    first = await q.pop()
    assert first is y  # y (seq 1) survives before z (seq 2)
    await q.mark_done()
    ev = f.events()
    assert any(e["kind"] == "proactive_queue_full" for e in ev), ev
    ok("queue: proactive cap evicts oldest; records proactive_queue_full friction")


# ---------- 3. queue bookkeeping: is_busy / queued / proactive_pending ----------
async def queue_bookkeeping():
    td = tempfile.mkdtemp()
    cfg = dataclasses.replace(BASE, data_dir=td, proactive_queue_max=10)
    q = M.TaskQueue(cfg, friction=Friction(td))
    assert await q.is_busy() is False and await q.queued() == 0 and await q.proactive_pending() == 0
    p = M.TaskQueue.PROACTIVE
    u = M.TaskQueue.USER
    await q.enqueue(_mk_task(cfg, p)); await q.enqueue(_mk_task(cfg, u)); await q.enqueue(_mk_task(cfg, u))
    assert await q.is_busy() is True
    assert await q.queued() == 3
    assert await q.proactive_pending() == 1
    # USER tasks always pop before PROACTIVE
    await q.pop()
    assert await q.queued() == 2 and await q.proactive_pending() == 1
    await q.pop()
    assert await q.queued() == 1 and await q.proactive_pending() == 1  # only the proactive left
    await q.pop()
    assert await q.queued() == 0 and await q.proactive_pending() == 0
    ok("queue: is_busy/queued/proactive_pending track state")


# ---------- mock clock so quiet-hours is deterministic ----------
class _MockDT(datetime):
    _t = datetime(2026, 3, 15, 12, 30, tzinfo=timezone.utc)

    @classmethod
    def set(cls, t):
        cls._t = t

    @classmethod
    def now(cls, tz=None):
        return cls._t.astimezone(tz if tz is not None else timezone.utc)


# ---------- 4. notify policy: quiet-hours batching + flush ----------
async def quiet_hours_batching():
    bot = _bot(tempfile.mkdtemp(), quiet_hours="22:00-04:00", proactive_per_day=0)
    sent = []

    async def _fake_notify(text):
        sent.append(text)

    bot._notify = _fake_notify
    _MockDT.set(datetime(2026, 3, 15, 23, 30, tzinfo=timezone.utc))  # 23:30 is inside 22:00-04:00 (cross-midnight)
    M.datetime = _MockDT
    assert bot._in_quiet_hours() is True
    await bot._notify_proactive("alert-a")
    await bot._notify_proactive("alert-b")
    assert not sent, "no delivery while in quiet hours"
    assert bot._pending_alerts == ["alert-a", "alert-b"]
    # while still in quiet hours the flush loop must hold
    await bot._flush_pending_alerts()
    assert not sent and len(bot._pending_alerts) == 2
    _MockDT.set(datetime(2026, 3, 15, 5, 0, tzinfo=timezone.utc))  # window opened
    assert bot._in_quiet_hours() is False
    await bot._flush_pending_alerts()
    assert sent == ["alert-a\nalert-b"]  # batched into one send
    assert not bot._pending_alerts
    ok("quiet hours: proactive alerts batched and flushed when the window opens")


# ---------- 5. notify policy: per-day cap ----------
async def per_day_cap():
    bot = _bot(tempfile.mkdtemp(), quiet_hours="", proactive_per_day=2)
    sent = []

    async def _fake_notify(text):
        sent.append(text)

    bot._notify = _fake_notify
    _MockDT.set(datetime.now(timezone.utc))  # today_start = real today; real logs count
    M.datetime = _MockDT
    assert bot._cap_ok() and bot._cap_headroom() == 2
    await bot._notify_proactive("one")
    assert sent == ["one"] and bot._cap_headroom() == 1
    await bot._notify_proactive("two")
    assert len(sent) == 2 and bot._cap_headroom() == 0
    assert not bot._cap_ok()
    await bot._notify_proactive("three")  # over cap -> dropped, not delivered
    assert len(sent) == 2, sent
    ev = bot.friction.events()
    assert any(e["kind"] == "proactive_cap" for e in ev), ev
    ok("per-day proactive cap: drops the excess and records proactive_cap friction")


# ---------- 6. !status renders the new queue line ----------
async def status_queue_line():
    bot = _bot(tempfile.mkdtemp(), proactive_per_day=3)
    _MockDT.set(datetime.now(timezone.utc))
    M.datetime = _MockDT
    # fresh queue
    m = user_msg("!status")
    await bot.on_message(m)
    r = m.replies[0]
    assert "queued" in r and "proactive pending" in r, r
    assert "0 queued" in r and "no per-day cap" not in r and "3 proactive left today" in r
    # now enqueue one task -> busy + 1 queued
    task = _mk_task(bot.cfg, M.TaskQueue.USER)
    task.done = M.asyncio.Event()
    await bot.queue.enqueue(task)
    m = user_msg("!status")
    await bot.on_message(m)
    r = m.replies[0]
    assert "busy" in r and "1 queued" in r and "3 proactive left today" in r, r
    ok("!status: shows queue busy/idle, queued count, proactive pending, per-day headroom")


# ---------- 7. self-review: PROACTIVE path is fire-and-forget ----------
async def self_review_proactive():
    bot = _bot(tempfile.mkdtemp())
    _MockDT.set(datetime.now(timezone.utc))
    M.datetime = _MockDT
    async def _notify_target():
        return FakeCh(1)  # stub: channel only; no discord hit

    bot._notify_target = _notify_target
    bot.runner.friction.record("llm_error", detail="boom")  # 1 problem event >= min(1)
    ret = await bot.do_self_review(force=False, message=None)
    assert ret is None, ret  # None means a task was queued (not the skip string)
    assert await bot.queue.proactive_pending() == 1
    assert await bot.queue.queued() == 1
    assert bot.watcher.state.get("last_review_ts") is not None
    ok("self-review: PROACTIVE path enqueues without awaiting (fire-and-forget)")


async def main():
    await queue_priority()
    await proactive_cap()
    await queue_bookkeeping()
    try:
        await quiet_hours_batching()
        await per_day_cap()
        await status_queue_line()
        await self_review_proactive()
    finally:
        M.datetime = datetime
    ok("shared-infra: queue priority, cap, notify policy, !status, proactive self-review")


asyncio.run(main())
