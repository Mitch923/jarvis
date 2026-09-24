import os, sys, time, threading
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))
import fake_llm
srv, base = fake_llm.start()

os.environ.update(DISCORD_TOKEN="x", OPENROUTER_API_KEY="k", DISCORD_ALLOWED_USER_IDS="1",
    OPENROUTER_BASE_URL=base, MODELS="m1,m2", LLM_TIMEOUT="1.5", LLM_CALL_DEADLINE="25",
    LLM_ATTEMPTS_PER_MODEL="2", GITHUB_TOKEN="")
from config import Config
from llm import *
from smolagents.models import ChatMessage

def new():
    fake_llm.SEEN.clear(); fake_llm.SCRIPT.clear()
    return ResilientModel(Config.from_env())
msgs = [ChatMessage(role="user", content="hi")]
ok = lambda name: print("PASS", name)

# 1. plain success, params sent
m = new(); r = m.generate(msgs)
assert r.content == "default reply"; assert fake_llm.SEEN[0][0] == "m1"; assert fake_llm.SEEN[0][3] == 2048
ok("plain success + max_tokens forwarded")

# 2. 500 twice on m1 -> falls to m2
m = new(); fake_llm.SCRIPT[:] = [("status", 500), ("status", 500)]
t = time.time(); r = m.generate(msgs); 
assert [s[0] for s in fake_llm.SEEN] == ["m1", "m1", "m2"], fake_llm.SEEN
assert m.model_id == "openrouter:m2"; assert m._cooldown[("openrouter", "m1")] > time.time()
ok(f"5xx x2 -> fallback to m2 ({time.time()-t:.1f}s)")

# 3. cooldown: next call skips m1
fake_llm.SEEN.clear(); m.generate(msgs); assert fake_llm.SEEN[0][0] == "m2"
ok("cooled-down model skipped on next call")

# 4. 200 with error body then success on retry
m = new(); fake_llm.SCRIPT[:] = [("200_error",)]
r = m.generate(msgs); assert r.content == "default reply"; assert len(fake_llm.SEEN) == 2
ok("HTTP 200 + error body treated as failure, retried")

# 5. empty response -> retry
m = new(); fake_llm.SCRIPT[:] = [("empty",)]
r = m.generate(msgs); assert r.content == "default reply"
ok("empty completion retried")

# 6. per-minute 429 waits Retry-After then same model succeeds
m = new(); fake_llm.SCRIPT[:] = [("429_minute", 2)]
t = time.time(); r = m.generate(msgs); dt_ = time.time() - t
assert [s[0] for s in fake_llm.SEEN] == ["m1", "m1"] and 1.8 < dt_ < 4, (fake_llm.SEEN, dt_)
ok(f"429 per-minute honours Retry-After ({dt_:.1f}s, same model)")

# 7. daily 429 -> quota error immediately, then fail-fast without hitting network
m = new(); fake_llm.SCRIPT[:] = [("429_day",)]
try: m.generate(msgs); assert False
except LLMQuotaError as e: print("   msg:", e)
n = len(fake_llm.SEEN)
try: m.generate(msgs); assert False
except LLMQuotaError: pass
assert len(fake_llm.SEEN) == n
ok("daily cap -> LLMQuotaError, later calls fail fast with zero requests")

# 8. upstream 429 -> straight to next model
m = new(); fake_llm.SCRIPT[:] = [("429_upstream",)]
r = m.generate(msgs); assert [s[0] for s in fake_llm.SEEN] == ["m1", "m2"]
ok("upstream 429 -> next model without retrying")

# 9. 404 model gone -> long cooldown, next model
m = new(); fake_llm.SCRIPT[:] = [("status", 404, "No endpoints found")]
r = m.generate(msgs); assert m._cooldown[("openrouter", "m1")] - time.time() > 1000
ok("404 -> 30 min cooldown, next model")

# 10. 401 -> fatal
m = new(); fake_llm.SCRIPT[:] = [("status", 401, "bad key")]
try: m.generate(msgs); assert False
except LLMFatalError: pass
ok("401 -> LLMFatalError")

# 11. timeout (server hangs 4s, client timeout 1.5s) -> retry/fallback
m = new(); fake_llm.SCRIPT[:] = [("hang", 4)]
t = time.time(); r = m.generate(msgs); dt_ = time.time() - t
assert r.content == "default reply" and dt_ < 6, dt_
ok(f"HTTP timeout enforced and retried ({dt_:.1f}s)")

# 12. everything fails -> LLMUnavailableError
m = new(); fake_llm.SCRIPT[:] = [("status", 500)] * 4
try: m.generate(msgs); assert False
except LLMUnavailableError as e: print("   msg:", str(e)[:120])
ok("all models failing -> LLMUnavailableError")

# 13. abort during backoff
m = new(); fake_llm.SCRIPT[:] = [("429_minute", 20)]
threading.Timer(1.0, m.stop).start()
t = time.time()
try: m.generate(msgs); assert False
except LLMAborted: pass
assert time.time() - t < 4
ok(f"abort interrupts a 20s rate-limit wait ({time.time()-t:.1f}s)")

# 14. deadline
os.environ["LLM_CALL_DEADLINE"] = "3"
m = ResilientModel(Config.from_env()); fake_llm.SEEN.clear(); fake_llm.SCRIPT[:] = [("429_minute", 25)]
t = time.time()
try: m.generate(msgs); assert False
except LLMUnavailableError as e: print("   msg:", e)
assert time.time() - t < 4
ok("per-call deadline respected")
os.environ["LLM_CALL_DEADLINE"] = "25"

# 15. tool calling: native tool call, prose -> final_answer, JSON-in-text -> tool call
from smolagents import tool
@tool
def jarvis_status() -> str:
    """Status.

    """
    return "hot"
@tool
def final_answer(answer: str) -> str:
    """Final.

    Args:
        answer: text
    """
    return answer
tools = [jarvis_status, final_answer]
m = new(); fake_llm.SCRIPT[:] = [("tool", "jarvis_status", {})]
r = m.generate(msgs, tools_to_call_from=tools)
assert r.tool_calls[0].function.name == "jarvis_status"
assert fake_llm.SEEN[0][1] is True and fake_llm.SEEN[0][2] == "auto" and fake_llm.SEEN[0][4] is None
ok("native tool call passes through; tool_choice=auto; no stop param")
m = new(); fake_llm.SCRIPT[:] = [("text", "Just prose answer")]
r = m.generate(msgs, tools_to_call_from=tools)
assert r.tool_calls[0].function.name == "final_answer" and r.tool_calls[0].function.arguments == {"answer": "Just prose answer"}
ok("prose reply converted to final_answer")
m = new(); fake_llm.SCRIPT[:] = [("json_text", "jarvis_status", {})]
r = m.generate(msgs, tools_to_call_from=tools)
assert r.tool_calls[0].function.name == "jarvis_status"
ok("JSON-in-text tool call recovered")
m = new(); fake_llm.SCRIPT[:] = [("json_text", "nonexistent_tool", {})]
r = m.generate(msgs, tools_to_call_from=tools)
assert r.tool_calls[0].function.name == "final_answer"
ok("unknown tool name in text -> treated as prose")

print(new().describe())
