import asyncio, contextlib, dataclasses, json, os, subprocess, sys, tempfile, time
from types import SimpleNamespace as NS
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))
import fake_gh, fake_llm
gsrv, gh_base = fake_gh.start(); lsrv, llm_base = fake_llm.start()


def git(repo, *args):
    r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    assert r.returncode == 0, (args, r.stderr)
    return r.stdout.strip()


def make_repo():
    """origin (bare) + work (checkout, remote url rewritten to look like a github.com repo)."""
    base = tempfile.mkdtemp()
    origin, work = f"{base}/origin", f"{base}/work"
    subprocess.run(["git", "init", "-q", "--bare", origin], check=True)
    subprocess.run(["git", "clone", "-q", origin, work], check=True)
    git(work, "config", "user.email", "t@t.com")
    git(work, "config", "user.name", "t")
    open(f"{work}/.gitignore", "w").write("data/\n")
    open(f"{work}/f.txt", "w").write("v1\n")
    git(work, "checkout", "-q", "-b", "main")
    git(work, "add", "-A")
    git(work, "commit", "-q", "-m", "initial")
    git(work, "push", "-q", "-u", "origin", "main")
    return base, origin, work


def push_from_elsewhere(origin, files, branch="main"):
    other = tempfile.mkdtemp()
    subprocess.run(["git", "clone", "-q", "-b", branch, origin, other], check=True)
    git(other, "config", "user.email", "t@t.com")
    git(other, "config", "user.name", "t")
    for path, content in files.items():
        open(f"{other}/{path}", "w").write(content)
    git(other, "add", "-A")
    git(other, "commit", "-q", "-m", "remote change")
    git(other, "push", "-q", "origin", f"HEAD:{branch}")
    return git(other, "rev-parse", "HEAD")


DATA = tempfile.mkdtemp()
os.environ.update(
    DISCORD_TOKEN="x", OPENROUTER_API_KEY="k", DISCORD_ALLOWED_USER_IDS="42", OPENROUTER_BASE_URL=llm_base,
    MODELS="m1", GITHUB_TOKEN="tok", GITHUB_API_URL=gh_base, GITHUB_ALLOWED_REPOS="me/proj", SELF_REPO="me/proj",
    DATA_DIR=DATA, SELF_REVIEW_MIN_EVENTS="2", IMPROVE_MAX_STEPS="4", APPROVAL_MODE="publish", APPROVAL_TIMEOUT="4",
)  # fmt: skip
from config import Config
import main as M
from updater import Updater

ok = lambda s: print("PASS", s)


class FakeMsg:
    async def edit(self, **k):
        pass

    async def delete(self):
        pass


class Target:
    def __init__(self):
        self.sent = []

    async def send(self, text, view=None, allowed_mentions=None):
        self.sent.append((text, view))
        return FakeMsg()

    @contextlib.asynccontextmanager
    async def typing(self):
        yield


def user_msg(text, ch=None):
    ch = ch or Target()
    m = NS(author=NS(id=42, bot=False), content=text, guild=None, channel=ch, mentions=[], id=1)
    m.replies = []

    async def reply(t, mention_author=False):
        m.replies.append(t)

    m.reply = reply
    return m


async def click(bot, approve: bool, wait: float = 3.0, channel=None) -> bool:
    """Wait for an approval prompt to appear (on `channel`, or bot.run_ctx's channel) and click it."""
    t0 = time.monotonic()
    while time.monotonic() - t0 < wait:
        ch = channel or (bot.run_ctx.channel if bot.run_ctx is not None else None)
        if ch and ch.sent and ch.sent[-1][1] is not None:
            view = ch.sent[-1][1]
            button = view.approve if approve else view.deny
            inter = NS(user=NS(id=42), response=NS(edit_message=_noop, send_message=_noop))
            await button.callback(inter)
            return True
        await asyncio.sleep(0.02)
    return False


async def _noop(*a, **k):
    pass


async def main():
    fake_gh.reset()
    fake_gh.STATE["issues"] = {}
    cfg = Config.from_env()
    bot = M.Bot(cfg)
    bot._connection.user = NS(id=999)

    # ---------- !status / !help mention self features; self_ready reflects config
    assert bot.self_ready is True
    m = user_msg("!status")
    await bot.on_message(m)
    assert "me/proj" in m.replies[0] and "update state" in m.replies[0]
    m = user_msg("!help")
    await bot.on_message(m)
    assert "!improve" in m.replies[0] and "!update" in m.replies[0]
    ok("!status / !help show self-improvement info")

    # ---------- !friction (before any feedback/friction exists)
    m = user_msg("!friction")
    await bot.on_message(m)
    assert "problem event" in m.replies[0]
    ok("!friction shows the log")

    # ---------- !improve: below threshold, no user feedback yet -> no LLM call
    fake_llm.SEEN.clear()
    m = user_msg("!improve")
    await bot.on_message(m)
    assert "nothing to review" in m.replies[0].lower() and not fake_llm.SEEN
    ok("!improve below SELF_REVIEW_MIN_EVENTS with no feedback -> skipped, no LLM cost")

    # ---------- !feedback: a user note alone is enough to justify a review, even under threshold
    m = user_msg("!feedback the bot is too chatty")
    await bot.on_message(m)
    assert "Noted" in m.replies[0]
    report, n, has_notes = bot.runner.friction.summary(days=7)
    assert has_notes and "too chatty" in report
    m = user_msg("!feedback")
    await bot.on_message(m)
    assert "Usage" in m.replies[0]
    fake_llm.SCRIPT[:] = [("text", "Nothing actionable yet.")]
    m = user_msg("!improve")
    await bot.on_message(m)
    assert "Self-review" in m.replies[0] and fake_llm.SEEN
    ok("!feedback records a user_note; its presence alone triggers a review below the event threshold")

    # push it over threshold with objective (non-user-note) friction, in a fresh review window
    bot.watcher.state["last_review_ts"] = ""
    bot.runner.friction.record("tool_error", tool="gh_browse", detail="ERROR: boom")
    bot.runner.friction.record("llm_transient", model="m1", detail="timeout")
    fake_llm.SCRIPT[:] = [
        ("tool", "gh_issues", {"repo": "proj"}),
        ("tool", "gh_open_issue", {"repo": "proj", "title": "Flaky gh_browse", "body": "2 errors this week."}),
        ("tool", "final_answer", {"answer": "Filed 1 issue: #101 Flaky gh_browse"}),
    ]
    m = user_msg("!improve")
    await bot.on_message(m)
    print(m.replies[0])
    assert "Self-review" in m.replies[0] and "Filed" in m.replies[0]
    assert any(i["title"] == "[self-review] Flaky gh_browse" for i in fake_gh.STATE["issues"].values())
    assert bot.watcher.state.get("last_review_ts")
    ok("!improve over threshold -> runs review, files an issue, records last_review_ts")

    # review-mode agent only gets read + gh_open_issue tools, never write tools
    agent = bot.runner.make_agent(lambda *a, **k: None, tool_names=M.REVIEW_TOOLS, max_steps=4)
    names = set(agent.tools.keys())  # smolagents always adds "final_answer" itself
    assert names == M.REVIEW_TOOLS | {"final_answer"} and "gh_edit_file" not in names and "gh_open_pr" not in names
    ok(f"review-mode agent's tool set is exactly {sorted(names)}")

    # ---------- !improve force bypasses the threshold
    fake_llm.SCRIPT[:] = [("text", "Nothing new to file.")]
    bot.watcher.state["last_review_ts"] = ""
    m = user_msg("!improve force")
    await bot.on_message(m)
    assert "Self-review" in m.replies[0]
    ok("!improve force runs even under threshold")

    # ---------- !implement: happy path opens a draft PR
    fake_gh.STATE["issues"][5] = {"number": 5, "title": "Fix flaky retries", "state": "open", "user": {"login": "x"}, "labels": [], "body": "retries are flaky"}
    fake_llm.SCRIPT[:] = [
        ("tool", "gh_issue", {"repo": "proj", "number": 5}),
        ("tool", "gh_edit_file", {"repo": "proj", "path": "README.md", "old_text": "Bye", "new_text": "Bye for now", "branch": "agent/self-5-retry", "commit_message": "fix retries"}),
        ("tool", "gh_open_pr", {"repo": "proj", "title": "self: fix flaky retries", "body": "Closes #5", "head": "agent/self-5-retry"}),
        ("tool", "final_answer", {"answer": "Opened a draft PR"}),
    ]
    m = user_msg("!implement 5")
    task = asyncio.create_task(bot.on_message(m))
    got_prompt = await click(bot, approve=True)
    await task
    assert got_prompt and "Opened a draft PR" in m.replies[0] and fake_gh.STATE["prs"][-1]["draft"] is True
    ok("!implement <n> reads the issue, edits, opens a DRAFT PR (after the owner approves opening it)")

    implement_tools = set(bot.runner.make_agent(lambda *a, **k: None, tool_names=M.IMPLEMENT_TOOLS, max_steps=4).tools.keys())
    assert implement_tools == M.IMPLEMENT_TOOLS | {"final_answer"} and "gh_open_issue" not in implement_tools
    ok(f"implement-mode agent's tool set is exactly {sorted(implement_tools)}")

    m = user_msg("!implement")
    await bot.on_message(m)
    assert "Usage" in m.replies[0]
    m = user_msg("!implement abc")
    await bot.on_message(m)
    assert "Usage" in m.replies[0]
    ok("!implement without a valid issue number -> usage message, no run")

    # ---------- self_ready = False path
    cfg2 = dataclasses.replace(Config.from_env(), self_repo="")
    bot2 = M.Bot(cfg2)
    bot2._connection.user = NS(id=999)
    assert not bot2.self_ready
    m = user_msg("!improve")
    await bot2.on_message(m)
    assert "SELF_REPO" in m.replies[0] or "don't know" in m.replies[0].lower()
    m = user_msg("!implement 5")
    await bot2.on_message(m)
    assert "SELF_REPO" in m.replies[0] or "don't know" in m.replies[0].lower()
    ok("self_ready=False -> !improve/!implement explain why instead of running")

    # ================= !update / !rollback against a REAL local git repo =================
    base, origin, work = make_repo()
    upd_cfg = dataclasses.replace(cfg, self_repo="")  # keep this bot focused on the update flow
    bot3 = M.Bot(upd_cfg)
    bot3._connection.user = NS(id=999)
    bot3.updater = Updater(work, github_token="", smoke_cmd=[sys.executable, "-c", "print('ok')"], pip=lambda: None)

    m = user_msg("!update")
    await bot3.on_message(m)
    assert m.replies == ["Already up to date at `" + bot3.updater.current() + "`."]
    ok("!update: already up to date -> says so immediately, no approval prompt")

    # a merged PR: new commit lands on origin
    sha = push_from_elsewhere(origin, {"f.txt": "v2\n"})
    ch = Target()
    m = user_msg("!update", ch=ch)
    task = asyncio.create_task(bot3.on_message(m))
    got_prompt = await click(bot3, approve=False, channel=ch)
    await task
    assert got_prompt and "Cancelled" in m.replies[-1]
    assert bot3.updater.current() != sha[:7]
    ok("!update: denying the approval leaves the checkout untouched")

    ch = Target()
    m = user_msg("!update", ch=ch)
    task = asyncio.create_task(bot3.on_message(m))
    got_prompt = await click(bot3, approve=True, channel=ch)
    await asyncio.sleep(0.1)
    await task
    assert got_prompt and bot3.updater.current() == sha[:7]
    assert bot3.restart_requested is True
    assert bot3.updater.read_state()["state"] == "pending"
    ok(f"!update: approved -> fast-forwards to {sha[:7]}, sets restart_requested, state=pending")

    # on_ready reports the pending update as healthy
    bot4 = M.Bot(upd_cfg)
    bot4._connection.user = NS(id=999)
    bot4.updater = Updater(work, github_token="")
    dm = Target()
    bot4.get_user = lambda uid: dm if uid == 42 else None
    await bot4.on_ready()
    assert dm.sent and "Update complete" in dm.sent[0][0]
    ok("on_ready(): reports a completed update to the owner")

    # !rollback undoes it
    ch = Target()
    m = user_msg("!rollback", ch=ch)
    task = asyncio.create_task(bot3.on_message(m))
    got_prompt = await click(bot3, approve=True, channel=ch)
    await asyncio.sleep(0.1)
    await task
    assert got_prompt and open(f"{work}/f.txt").read() == "v1\n"
    ok("!rollback: reverts the working tree to the pre-update commit")

    ch = Target()
    m = user_msg("!rollback", ch=ch)
    await bot3.on_message(m)
    assert "Nothing to roll back" in m.replies[0]
    ok("!rollback: nothing left to roll back to -> clear message, no crash")

    # !update while busy is refused
    bot5 = M.Bot(upd_cfg)
    bot5._connection.user = NS(id=999)
    bot5.updater = Updater(work, github_token="")
    async with bot5.lock:
        m = user_msg("!update")
        await bot5.on_message(m)
    assert "middle of a task" in m.replies[0]
    ok("!update refused while a task is already running")

    # failing CI blocks the update
    base2, origin2, work2 = make_repo()
    bot6 = M.Bot(dataclasses.replace(upd_cfg, github_token="tok"))
    bot6._connection.user = NS(id=999)
    bot6.updater = Updater(work2, github_token="tok")
    bot6.updater.origin_slug = lambda: "me/proj"  # keep the real (local) origin remote usable for git fetch; fake only the GitHub identity
    sha2 = push_from_elsewhere(origin2, {"f.txt": "v2\n"})
    fake_gh.STATE["checks"][sha2] = [{"name": "tests", "status": "completed", "conclusion": "failure", "head_sha": sha2, "html_url": "https://x/r"}]
    m = user_msg("!update")
    await bot6.on_message(m)
    assert "CI is failing" in m.replies[0]
    assert bot6.updater.current() != sha2[:7]
    ok("!update: refuses when GitHub CI for the target commit is failing")

    await bot.close()
    await bot2.close()
    await bot3.close()
    await bot4.close()
    await bot5.close()
    await bot6.close()


asyncio.run(main())
