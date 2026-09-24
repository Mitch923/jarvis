import asyncio, os, sys, time, threading
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))
import fake_llm
srv, base = fake_llm.start()
import tempfile
os.environ.update(DATA_DIR=tempfile.mkdtemp(), DISCORD_TOKEN="x", OPENROUTER_API_KEY="k", DISCORD_ALLOWED_USER_IDS="1", OPENROUTER_BASE_URL=base,
    MODELS="m1,m2", LLM_TIMEOUT="1.5", LLM_CALL_DEADLINE="60", RUN_TIMEOUT="30", MAX_STEPS="3", GITHUB_TOKEN="")
from config import Config
import main as M
from llm import *

cfg = Config.from_env()
runner = M.AgentRunner(cfg, lambda t, d: False)
steps_seen = []
cb = lambda step, agent=None: steps_seen.append((step.step_number, [t.name for t in (step.tool_calls or [])]))
ok = lambda s: print("PASS", s)
def reset(): fake_llm.SEEN.clear(); fake_llm.SCRIPT.clear(); steps_seen.clear()

async def scenario():
    # 1. tool call then final_answer
    reset(); fake_llm.SCRIPT[:] = [("tool", "jarvis_status", {}), ("tool", "final_answer", {"answer": "All good"})]
    r = await runner.run("how is the server?", cb)
    assert r.text == "All good" and r.steps == 2 and r.model == "openrouter:m1", r
    assert steps_seen == [(1, ["jarvis_status"]), (2, ["final_answer"])], steps_seen
    ok(f"tool step + final_answer  -> {r}")

    # 2. plain prose answer becomes the final answer in ONE step
    reset(); fake_llm.SCRIPT[:] = [("text", "Hello there!")]
    r = await runner.run("hi", cb); assert r.text == "Hello there!" and r.steps == 1
    ok("prose answer accepted as final answer")

    # 3. LLM totally down -> clean LLMUnavailableError, not a generic crash
    reset(); fake_llm.SCRIPT[:] = [("status", 500)] * 6
    try: await runner.run("hi", cb); assert False
    except LLMUnavailableError as e: ok("LLM down -> LLMUnavailableError surfaced: " + M.explain_error(e, cfg).splitlines()[0])
    runner.model._cooldown.clear()

    # 4. daily quota
    reset(); fake_llm.SCRIPT[:] = [("429_day",)]
    try: await runner.run("hi", cb); assert False
    except LLMQuotaError as e: ok("quota -> " + M.explain_error(e, cfg))
    runner.model._daily_block.clear()

    # 5. !stop during a long rate-limit wait
    reset(); fake_llm.SCRIPT[:] = [("429_minute", 25)]
    asyncio.get_running_loop().call_later(1.0, runner.stop)
    t = time.time()
    try: await runner.run("hi", cb); assert False
    except (LLMAborted, M.RunStopped) as e: ok(f"stop works ({time.time()-t:.1f}s) -> {M.explain_error(e, cfg)}")

    # 6. run timeout (RUN_TIMEOUT=3) while LLM is rate-limiting for 25s
    import dataclasses
    short = M.AgentRunner(dataclasses.replace(cfg, run_timeout=3), lambda t, d: False)
    reset(); fake_llm.SCRIPT[:] = [("429_minute", 25)]
    t = time.time()
    try: await short.run("hi", cb); assert False
    except M.RunTimeout: ok(f"run timeout after {time.time()-t:.1f}s -> {M.explain_error(M.RunTimeout(), cfg)}")

    # 7. max steps: model keeps calling tools forever
    reset(); fake_llm.SCRIPT[:] = [("tool", "jarvis_status", {})] * 3 + [("text", "Best summary I could do")]
    r = await runner.run("loop", cb); ok(f"max_steps reached -> graceful final answer: {r.text!r} ({r.steps} steps)")

    # 8. tool error string doesn't crash the run; unknown tool -> error fed back, run continues
    reset(); fake_llm.SCRIPT[:] = [("tool", "does_not_exist", {}), ("tool", "final_answer", {"answer": "recovered"})]
    r = await runner.run("x", cb); assert r.text == "recovered"; ok("unknown tool call is recoverable")

    # 9. queue/lock: model state is clean after failures
    reset(); r = await runner.run("hi", cb); assert r.text == "default reply"; ok("runner healthy afterwards")
asyncio.run(scenario())

# chunker
from main import chunk_message
long = "para\n" * 3000
parts = chunk_message(long); assert all(len(p) <= 2000 for p in parts) and "".join(parts).count("para") == 3000
code = "intro\n```python\n" + "\n".join(f"x{i} = {i}" for i in range(400)) + "\n```\noutro"
parts = chunk_message(code)
assert all(len(p) <= 2000 for p in parts), [len(p) for p in parts]
assert all(p.count("```") % 2 == 0 for p in parts), "unbalanced fences"
assert parts[1].startswith("```python")
mono = "y" * 5000; parts = chunk_message(mono); assert all(len(p) <= 2000 for p in parts) and sum(len(p) for p in parts) == 5000
assert chunk_message("") == ["(empty reply)"] and chunk_message("short") == ["short"]
ok(f"chunker: {len(parts)} parts for long single line; code fences stay balanced")
