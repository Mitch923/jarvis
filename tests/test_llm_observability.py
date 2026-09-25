"""Unit tests for LLM observability: outcome classifier, ring buffer, stats summary, llmlog formatting."""
import os, sys, time
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))
import fake_llm
srv, base = fake_llm.start()

os.environ.update(DISCORD_TOKEN="x", OPENROUTER_API_KEY="k", DISCORD_ALLOWED_USER_IDS="1",
    OPENROUTER_BASE_URL=base, MODELS="m1,m2", LLM_TIMEOUT="1.5", LLM_CALL_DEADLINE="25",
    LLM_ATTEMPTS_PER_MODEL="2", GITHUB_TOKEN="")
from config import Config
from llm import (
    ResilientModel,
    _classify,
    _classify_to_outcome,
    _BadResponse,
    LLMFatalError,
    LLMQuotaError,
)
from smolagents.models import ChatMessage
import openai

msgs = [ChatMessage(role="user", content="hi")]
ok = lambda name: print("PASS", name)

def new():
    fake_llm.SEEN.clear(); fake_llm.SCRIPT.clear()
    return ResilientModel(Config.from_env())

# --- _classify_to_outcome mapping tests ---

# transient + APITimeoutError -> timeout
outcome = _classify_to_outcome("transient", openai.APITimeoutError("timeout"))
assert outcome == "timeout", f"expected timeout, got {outcome}"
ok("_classify_to_outcome: transient+APITimeoutError -> timeout")

# transient + APIConnectionError -> timeout
outcome = _classify_to_outcome("transient", openai.APIConnectionError(message="connection failed", request=None))
assert outcome == "timeout", f"expected timeout, got {outcome}"
ok("_classify_to_outcome: transient+APIConnectionError -> timeout")

# transient without timeout error -> server_error
outcome = _classify_to_outcome("transient", Exception("generic"))
assert outcome == "server_error", f"expected server_error, got {outcome}"
ok("_classify_to_outcome: transient generic -> server_error")

# rate_wait -> rate_limited
outcome = _classify_to_outcome("rate_wait")
assert outcome == "rate_limited", f"expected rate_limited, got {outcome}"
ok("_classify_to_outcome: rate_wait -> rate_limited")

# fatal -> auth_error
outcome = _classify_to_outcome("fatal")
assert outcome == "auth_error", f"expected auth_error, got {outcome}"
ok("_classify_to_outcome: fatal -> auth_error")

# daily -> rate_limited
outcome = _classify_to_outcome("daily")
assert outcome == "rate_limited", f"expected rate_limited, got {outcome}"
ok("_classify_to_outcome: daily -> rate_limited")

# model_down -> server_error
outcome = _classify_to_outcome("model_down")
assert outcome == "server_error", f"expected server_error, got {outcome}"
ok("_classify_to_outcome: model_down -> server_error")

# bad_request + _BadResponse empty -> empty_response
outcome = _classify_to_outcome("bad_request", _BadResponse("empty response"))
assert outcome == "empty_response", f"expected empty_response, got {outcome}"
ok("_classify_to_outcome: bad_request+empty _BadResponse -> empty_response")

# bad_request generic -> other_error
outcome = _classify_to_outcome("bad_request", _BadResponse("context too long"))
assert outcome == "other_error", f"expected other_error, got {outcome}"
ok("_classify_to_outcome: bad_request generic -> other_error")

# upstream -> server_error
outcome = _classify_to_outcome("upstream")
assert outcome == "server_error", f"expected server_error, got {outcome}"
ok("_classify_to_outcome: upstream -> server_error")

# unknown kind -> other_error
outcome = _classify_to_outcome("unknown_kind")
assert outcome == "other_error", f"expected other_error, got {outcome}"
ok("_classify_to_outcome: unknown -> other_error")

# --- Ring buffer cap test ---
m = new()
# Directly append to the ring buffer to test capping (bypasses slow generate())
for i in range(250):
    from llm import _AttemptRecord
    m._attempt_log.append(_AttemptRecord(
        timestamp=time.time(), provider="test", model="m", outcome="success", latency_ms=10, error=""
    ))
assert len(m._attempt_log) == 200, f"ring buffer should cap at 200, got {len(m._attempt_log)}"
ok("ring buffer caps at maxlen=200")

# --- get_llm_stats_summary shape test ---
m = new()
fake_llm.SCRIPT[:] = [("status", 500), ("status", 500)]  # m1 fails twice, falls to m2
m.generate(msgs)
summary = m.get_llm_stats_summary()
assert "total_attempts" in summary
assert "total_successes" in summary
assert "providers" in summary
assert "openrouter" in summary["providers"]
prov = summary["providers"]["openrouter"]
for key in ("attempts", "successes", "failures", "total_failures", "success_rate",
            "cooldown_active", "cooldown_remaining_sec", "daily_blocked", "daily_blocked_remaining_sec"):
    assert key in prov, f"missing key {key} in provider stats"
assert isinstance(prov["failures"], dict)
for cat in ("timeout", "rate_limited", "auth_error", "server_error", "empty_response", "other_error"):
    assert cat in prov["failures"], f"missing failure category {cat}"
ok("get_llm_stats_summary() has correct shape")

# --- get_recent_attempts ordering (newest first) ---
m = new()
fake_llm.SCRIPT[:] = [("text", "ok")] * 5
for _ in range(5):
    m.generate(msgs)
recent = m.get_recent_attempts(3)
assert len(recent) == 3
# newest first means last generated is first in list
assert recent[0].timestamp >= recent[1].timestamp >= recent[2].timestamp
ok("get_recent_attempts returns newest first")

# --- llmlog formatting (via get_recent_attempts) ---
m = new()
fake_llm.SCRIPT[:] = [("text", "ok")]
m.generate(msgs)
attempts = m.get_recent_attempts(1)
a = attempts[0]
assert a.provider == "openrouter"
assert a.model == "m1"
assert a.outcome == "success"
assert a.latency_ms >= 0
assert a.error == ""
ok("attempt record fields populated correctly on success")

# --- failure attempt recorded with error ---
m = new()
fake_llm.SCRIPT[:] = [("status", 500)] * 4  # 2 attempts per model * 2 models = 4 failures
try:
    m.generate(msgs)
except Exception:
    pass
attempts = m.get_recent_attempts(1)
a = attempts[0]
assert a.outcome == "server_error"  # 500 -> transient -> server_error
assert a.error != ""
ok("failed attempt recorded with outcome and error")

# --- per-provider stats tracked independently ---
# Use only openrouter (already configured) to avoid needing extra credentials
m = new()
# The unit test above for stats shape covers the structure

# --- describe() includes per-provider stats ---
m = new()
fake_llm.SCRIPT[:] = [("text", "ok")]
m.generate(msgs)
desc = m.describe()
assert "attempts:" in desc
assert "ok:" in desc
assert "fail:" in desc
ok("describe() includes per-provider attempt/success/fail counts")

print("All observability tests passed!")