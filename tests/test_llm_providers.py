import os, sys, tempfile, time
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))
import fake_llm

# Three independent fake servers standing in for three providers. fake_llm's SCRIPT/SEEN queues
# are module-level, which is fine here: every scenario below drives requests strictly in the
# order the code would actually issue them (one provider exhausted before the next is tried),
# so the shared FIFO queue still lines up with "who gets which scripted response".
or_srv, or_base = fake_llm.start()
gg_srv, gg_base = fake_llm.start()
nv_srv, nv_base = fake_llm.start()
ol_srv, ol_base = fake_llm.start()

BASE_ENV = dict(
    DISCORD_TOKEN="x", DISCORD_ALLOWED_USER_IDS="1", GITHUB_TOKEN="",
    OPENROUTER_API_KEY="ork", OPENROUTER_BASE_URL=or_base, MODELS="or1",
    GOOGLE_API_KEY="ggk", GOOGLE_BASE_URL=gg_base, GOOGLE_MODELS="gg1",
    NVIDIA_API_KEY="nvk", NVIDIA_BASE_URL=nv_base, NVIDIA_MODELS="nv1",
    OLLAMA_BASE_URL=ol_base, OLLAMA_MODELS="ol1",
    LLM_TIMEOUT="1.5", LLM_CALL_DEADLINE="25", LLM_ATTEMPTS_PER_MODEL="2",
    DATA_DIR=tempfile.mkdtemp(),
)


def cfg_with(**overrides):
    os.environ.update(BASE_ENV)
    os.environ.update(overrides)
    from config import Config
    return Config.from_env()


def new(**overrides):
    fake_llm.SEEN.clear()
    fake_llm.SCRIPT.clear()
    from llm import ResilientModel
    return ResilientModel(cfg_with(**overrides))


from smolagents.models import ChatMessage
from llm import LLMQuotaError, LLMUnavailableError

msgs = [ChatMessage(role="user", content="hi")]
ok = lambda s: print("PASS", s)

# 1. default: only openrouter configured (PROVIDERS unset) -> behaves single-provider
m = new(PROVIDERS="")
r = m.generate(msgs)
assert r.content == "default reply" and fake_llm.SEEN[0][0] == "or1" and m.model_id == "openrouter:or1"
ok("PROVIDERS unset -> defaults to openrouter only")

# 2. multi-provider order respected, independent client per provider
m = new(PROVIDERS="google,nvidia,openrouter")
assert [p.name for p in m._providers] == ["google", "nvidia", "openrouter"]
assert m._clients["google"].base_url.host in gg_base and m._clients["nvidia"].base_url.host in nv_base and m._clients["openrouter"].base_url.host in or_base
r = m.generate(msgs)
assert fake_llm.SEEN[0][0] == "gg1" and m.model_id == "google:gg1"
ok("provider order follows PROVIDERS; each provider gets its own client/base_url")

# 3. provider A exhausts (5xx) -> falls through to provider B
m = new(PROVIDERS="openrouter,google")
fake_llm.SCRIPT[:] = [("status", 500), ("status", 500)]  # openrouter fails LLM_ATTEMPTS_PER_MODEL times
r = m.generate(msgs)
assert [s[0] for s in fake_llm.SEEN] == ["or1", "or1", "gg1"]
assert m.model_id == "google:gg1"
assert m._cooldown[("openrouter", "or1")] > time.time()
ok("provider A's model exhausts retries -> falls through to provider B")

# 4. daily cap on provider A moves to provider B immediately (NOT after exhausting attempts)
m = new(PROVIDERS="openrouter,google")
fake_llm.SCRIPT[:] = [("429_day",)]
r = m.generate(msgs)
assert [s[0] for s in fake_llm.SEEN] == ["or1", "gg1"], fake_llm.SEEN  # exactly ONE openrouter request, not two
assert m.model_id == "google:gg1"
assert m._daily_block["openrouter"] > time.time() and "google" not in m._daily_block
ok("daily cap on provider A -> immediate fallback to provider B (no wasted retries)")

# 5. provider A's daily block doesn't touch provider B's cooldown state, and persists across calls
fake_llm.SEEN.clear(); fake_llm.SCRIPT[:] = [("status", 500)]  # if this hit openrouter it would fail; must go straight to google
r = m.generate(msgs)
assert fake_llm.SEEN == [("gg1", False, "auto", None, None)] or fake_llm.SEEN[0][0] == "gg1"
ok("provider A stays day-blocked on the next call; provider B used directly, no wasted request")

# 6. every provider day-blocked -> LLMQuotaError naming all of them
m = new(PROVIDERS="openrouter,google")
fake_llm.SCRIPT[:] = [("429_day",), ("429_day",)]
try:
    m.generate(msgs)
    assert False
except LLMQuotaError as e:
    msg = str(e)
    assert "openrouter" in msg and "google" in msg, msg
    ok(f"all providers day-blocked -> {msg}")
# and it fails fast (no network) on the very next call
fake_llm.SEEN.clear()
try:
    m.generate(msgs)
    assert False
except LLMQuotaError:
    pass
assert not fake_llm.SEEN
ok("once every provider is day-blocked, later calls fail fast with zero requests")

# 7. independent cooldowns: provider A's 404 doesn't affect provider B at all
m = new(PROVIDERS="openrouter,google")
fake_llm.SCRIPT[:] = [("status", 404, "gone")]
r = m.generate(msgs)
assert ("openrouter", "or1") in m._cooldown and ("google", "gg1") not in m._cooldown
desc = m.describe()
assert "openrouter" in desc and "google" in desc and "cooling down" in desc
ok("one provider's cooldown never bleeds into another provider's model")

# 8. Ollama provider: empty API key still works (placeholder key used internally)
m = new(PROVIDERS="ollama")
r = m.generate(msgs)
assert r.content == "default reply" and fake_llm.SEEN[0][0] == "ol1" and m.model_id == "ollama:ol1"
ok("Ollama provider (no API key) works via a placeholder client credential")

# 9. only some providers configured -> unconfigured ones are simply absent, not erroring
m = new(PROVIDERS="nvidia")
assert [p.name for p in m._providers] == ["nvidia"]
r = m.generate(msgs)
assert fake_llm.SEEN[0][0] == "nv1"
ok("a single non-default provider (nvidia) works standalone")

# 10. all configured providers fail for ordinary (non-daily) reasons -> generic LLMUnavailableError,
#     not misreported as a quota problem
m = new(PROVIDERS="openrouter,google")
fake_llm.SCRIPT[:] = [("status", 500)] * 4
try:
    m.generate(msgs)
    assert False
except LLMUnavailableError as e:
    assert "quota" not in str(e).lower()
    ok(f"ordinary multi-provider outage -> LLMUnavailableError, not misreported as quota: {str(e)[:100]}")

# 11. multiple models within one provider still cascade correctly alongside other providers
m = new(PROVIDERS="openrouter,google", MODELS="or1,or2")
fake_llm.SCRIPT[:] = [("status", 500), ("status", 500), ("status", 500), ("status", 500)]
r = m.generate(msgs)
assert [s[0] for s in fake_llm.SEEN] == ["or1", "or1", "or2", "or2", "gg1"]
ok("within-provider model cascade (or1 -> or2) still happens before moving to the next provider")

print(new(PROVIDERS="openrouter,google,nvidia").describe())
