"""Tests for prose response handling in llm.py.

Verifies that prose-only responses (no tool_calls, just text) are handled
gracefully before they can cause AgentParsingError downstream.
"""
import os, sys
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))
import fake_llm
srv, base = fake_llm.start()

os.environ.update(DISCORD_TOKEN="x", OPENROUTER_API_KEY="k", DISCORD_ALLOWED_USER_IDS="1",
    OPENROUTER_BASE_URL=base, MODELS="m1,m2", LLM_TIMEOUT="1.5", LLM_CALL_DEADLINE="25",
    LLM_ATTEMPTS_PER_MODEL="2", GITHUB_TOKEN="")
from config import Config
from llm import *
from smolagents import tool
from smolagents.models import ChatMessage

def new():
    fake_llm.SEEN.clear(); fake_llm.SCRIPT.clear()
    return ResilientModel(Config.from_env())

msgs = [ChatMessage(role="user", content="hi")]
ok = lambda name: print("PASS", name)

@tool
def jarvis_status() -> str:
    """Status."""
    return "hot"

@tool
def final_answer(answer: str) -> str:
    """Final."""
    return answer

tools = [jarvis_status, final_answer]

# 1. Prose-only response with tools enabled returns a thinking step (no tool call)
m = new(); fake_llm.SCRIPT[:] = [("text", "I'm thinking about this")]
r = m.generate(msgs, tools_to_call_from=tools)
assert r.tool_calls in (None, []), f"expected no tool_calls, got {r.tool_calls}"
assert r.content == "I'm thinking about this"
assert m._prose_count == 1
ok("prose-only reply -> thinking step (no tool call, not final)")

# 2. After _PROSE_LIMIT consecutive prose replies, final_answer is forced
m = new()
for i in range(m._PROSE_LIMIT):
    fake_llm.SCRIPT[:] = [("text", f"thinking {i}")]
    r = m.generate(msgs, tools_to_call_from=tools)
    if i < m._PROSE_LIMIT - 1:
        assert r.tool_calls in (None, []), f"step {i}: expected no tool_calls"
    else:
        assert r.tool_calls[0].function.name == "final_answer"
        assert r.tool_calls[0].function.arguments == {"answer": f"thinking {i}"}
ok(f"prose x{m._PROSE_LIMIT} -> forced final_answer (last-resort cap)")

# 3. A real tool call after prose resets the streak
m = new()
fake_llm.SCRIPT[:] = [("text", "thinking")]
m.generate(msgs, tools_to_call_from=tools)
fake_llm.SCRIPT[:] = [("tool", "jarvis_status", {})]
r = m.generate(msgs, tools_to_call_from=tools)
assert r.tool_calls[0].function.name == "jarvis_status"
assert m._prose_count == 0
ok("tool call after prose thinking resets the streak")

# 4. JSON-in-text tool call recovered from prose
m = new(); fake_llm.SCRIPT[:] = [("json_text", "jarvis_status", {})]
r = m.generate(msgs, tools_to_call_from=tools)
assert r.tool_calls[0].function.name == "jarvis_status"
ok("JSON-in-text tool call recovered from prose")

# 5. Unknown tool name in text -> thinking step (not a tool call)
m = new(); fake_llm.SCRIPT[:] = [("json_text", "nonexistent_tool", {})]
r = m.generate(msgs, tools_to_call_from=tools)
assert r.tool_calls in (None, []) and r.content and m._prose_count == 1
ok("unknown tool name in text -> thinking step")

# 6. Prose handling does NOT trigger when tools are not provided
m = new(); fake_llm.SCRIPT[:] = [("text", "plain answer")]
r = m.generate(msgs)
assert r.content == "plain answer" and r.tool_calls in (None, [])
ok("prose without tools -> plain content passthrough")

# 7. Empty response is still retried (not treated as prose)
m = new(); fake_llm.SCRIPT[:] = [("empty",), ("text", "recovered")]
r = m.generate(msgs, tools_to_call_from=tools)
assert r.content == "recovered"
ok("empty response retried, then recovered")

# 8. Native tool call still passes through unchanged
m = new(); fake_llm.SCRIPT[:] = [("tool", "jarvis_status", {})]
r = m.generate(msgs, tools_to_call_from=tools)
assert r.tool_calls[0].function.name == "jarvis_status"
assert fake_llm.SEEN[0][1] is True and fake_llm.SEEN[0][2] == "auto"
ok("native tool call passes through; tool_choice=auto")

print(new().describe())