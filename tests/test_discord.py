import asyncio, os, sys, contextlib
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))
import fake_llm
srv, base = fake_llm.start()
import tempfile
os.environ.update(DATA_DIR=tempfile.mkdtemp(), DISCORD_TOKEN="x", OPENROUTER_API_KEY="k", DISCORD_ALLOWED_USER_IDS="42", DISCORD_CHANNEL_IDS="555",
    OPENROUTER_BASE_URL=base, MODELS="m1", LLM_TIMEOUT="2", GITHUB_TOKEN="", APPROVAL_TIMEOUT="3")
from config import Config
import main as M
ok = lambda s: print("PASS", s)

class FakeMsg:
    def __init__(self, text, view=None): self.content, self.view, self.deleted = text, view, False
    async def edit(self, content=None, view=None): self.content = content or self.content
    async def delete(self): self.deleted = True
class FakeChannel:
    def __init__(self, cid): self.id, self.sent = cid, []
    async def send(self, text, view=None):
        m = FakeMsg(text, view); self.sent.append(m); return m
    @contextlib.asynccontextmanager
    async def typing(self): yield
def user_msg(text, uid=42, ch=None, guild=None, mentions=()):
    ch = ch or FakeChannel(1)
    m = NS(author=NS(id=uid, bot=False), content=text, guild=guild, channel=ch, mentions=list(mentions))
    m.replies = []
    async def reply(t, mention_author=False): m.replies.append(t)
    m.reply = reply
    return m

async def main():
    bot = M.Bot(Config.from_env())
    bot._connection.user = NS(id=999, __str__=lambda s: "bot")   # pretend we're logged in
    me = bot._connection.user

    # unauthorised user -> silence
    m = user_msg("hello", uid=7); await bot.on_message(m); assert not m.replies and not m.channel.sent
    ok("unauthorised user ignored (no reply, no LLM call)")

    # guild message without mention, wrong channel -> ignored
    m = user_msg("hello", guild=object()); await bot.on_message(m); assert not m.replies and not m.channel.sent
    ok("guild chatter without @mention ignored")

    # DM commands
    m = user_msg("!help"); await bot.on_message(m); assert "!status" in m.replies[0]
    m = user_msg("!stop"); await bot.on_message(m); assert m.replies == ["Nothing is running."]
    m = user_msg("!status"); await bot.on_message(m); print(m.replies[0][:400]); assert "Requests today" in m.replies[0]
    ok("!help / !stop / !status")

    # DM task, full flow
    fake_llm.SCRIPT[:] = [("tool", "jarvis_status", {}), ("tool", "final_answer", {"answer": "Server is fine 🌡️"})]
    ch = FakeChannel(1); m = user_msg("how's the server?", ch=ch)
    await bot.on_message(m)
    assert m.replies and m.replies[0].startswith("Server is fine"), m.replies
    assert "openrouter:m1 · 2 step(s)" in m.replies[0]
    assert ch.sent and ch.sent[0].deleted, "status message should be removed"
    ok("DM task -> reply with footer; status message cleaned up: " + repr(m.replies[0]))

    # history is injected into the next prompt, and !reset clears it
    fake_llm.SEEN.clear(); fake_llm.SCRIPT[:] = [("text", "second")]
    await bot.on_message(user_msg("and now?", ch=ch))
    assert len(bot.history[1]) == 2
    assert "Earlier in this conversation" in bot._prompt(1, "x") and "how's the server?" in bot._prompt(1, "x")
    await bot.on_message(user_msg("!reset", ch=ch)); assert 1 not in bot.history and bot._prompt(1, "x") == "x"
    ok("per-channel history kept, then cleared by !reset")

    # @mention in a guild works; dedicated channel works without mention
    # (prose is now a thinking step, so the final answer must come from a final_answer tool call)
    fake_llm.SCRIPT[:] = [("tool", "final_answer", {"answer": "mention ok"})]
    m = user_msg(f"<@999> hi there", guild=object(), mentions=[me]); await bot.on_message(m); assert m.replies[0].startswith("mention ok")
    fake_llm.SCRIPT[:] = [("tool", "final_answer", {"answer": "channel ok"})]
    m = user_msg("hi", guild=object(), ch=FakeChannel(555)); await bot.on_message(m); assert m.replies[0].startswith("channel ok")
    ok("@mention and dedicated-channel triggers")

    # LLM failure -> friendly message
    bot.runner.model._cooldown.clear()
    fake_llm.SCRIPT[:] = [("status", 500)] * 4
    m = user_msg("hi"); await bot.on_message(m); assert "aren't cooperating" in m.replies[0]
    bot.runner.model._cooldown.clear()
    ok("LLM failure -> friendly error, bot keeps working")

    # queueing: second task while first runs
    fake_llm.SCRIPT[:] = [("hang", 1.0), ("text", "first done"), ("text", "second done")]
    ch1, ch2 = FakeChannel(1), FakeChannel(2)
    m1, m2 = user_msg("one", ch=ch1), user_msg("two", ch=ch2)
    t1 = asyncio.create_task(bot.on_message(m1)); await asyncio.sleep(0.3)
    t2 = asyncio.create_task(bot.on_message(m2)); await asyncio.gather(t1, t2)
    assert any("queued" in s.content or "next" in s.content for s in ch2.sent), [s.content for s in ch2.sent]
    assert m1.replies and m2.replies
    ok("second request queued behind the first (one agent at a time)")

    # approval buttons
    ch = FakeChannel(1)
    bot.run_ctx = M.RunContext(ch, 42, asyncio.get_running_loop())
    fut = asyncio.create_task(asyncio.to_thread(bot._approve_from_thread, "Open a pull request", "details"))
    while not ch.sent: await asyncio.sleep(0.05)
    view = ch.sent[0].view
    inter = lambda uid: NS(user=NS(id=uid), response=NS(edit_message=AsyncMock(), send_message=AsyncMock()))
    stranger = inter(7); await view.approve.callback(stranger)
    stranger.response.send_message.assert_awaited(); assert view.approved is None
    owner = inter(42); await view.approve.callback(owner)
    assert await fut is True; assert all(c.disabled for c in view.children)
    ok("approval: stranger's click rejected, owner's click approves")

    fut = asyncio.create_task(asyncio.to_thread(bot._approve_from_thread, "x", "y"))
    while len(ch.sent) < 2: await asyncio.sleep(0.05)
    await ch.sent[1].view.deny.callback(inter(42)); assert await fut is False
    ok("approval: deny -> False")

    fut = asyncio.create_task(asyncio.to_thread(bot._approve_from_thread, "x", "y"))
    assert await fut is False; assert "denied" in ch.sent[2].content
    ok("approval: timeout (3s) -> treated as denied, message updated")

    bot.run_ctx = None
    assert bot._approve_from_thread("x", "y") is False
    ok("approval with no active run -> denied")
    await bot.close()
asyncio.run(main())
