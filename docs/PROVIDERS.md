# Multi-provider LLM

`PROVIDERS` is tried in order (default just `openrouter`); within a provider, its own `*_MODELS` list is tried in order. `!status` shows every configured provider, which model answered last, and any cooldowns or day-caps, e.g. `openrouter — ⛔ day cap, resets in 6h 12m`. **`!llmlog [N]` shows the last N provider/model attempts (default 20) with timestamp, outcome, latency, and truncated error.** Free-tier behavior, all handled automatically:

- Every HTTP request has a hard timeout; one LLM call has an overall deadline (`LLM_CALL_DEADLINE`); a whole task has `RUN_TIMEOUT`.
- **429 per-minute** → waits (honouring `Retry-After`) and retries. **429 per-day** → that provider is skipped for the rest of the day and the next provider (if any) is tried immediately — it does not wait or block the others. Only once *every* configured provider is day-capped does the bot tell you and stop.
- **5xx / timeout / dropped connection / empty or malformed reply** → retry, then next model, then next provider. **404 / 400 / 402** (model retired, context too small, needs credits) → that model is skipped for a while.
- `MODELS=openrouter/free,auto` (OpenRouter only): after the router, fall back to whichever free tool-capable models exist *right now* (list refreshed every 6 h).
- Models that answer in prose instead of calling a tool have their text used as the final answer, instead of wasting steps on parse errors.
- Every agent step is one request against a provider's own daily budget, hence `MAX_STEPS=12` and the ask-once-then-answer prompting — the more providers you configure, the more headroom you have before hitting a wall.

## Observability

The bot now records every provider/model attempt in a ring buffer (max 200 entries) with:
- **Timestamp** (UTC)
- **Provider** and **model**
- **Outcome**: `success`, `timeout`, `rate_limited`, `auth_error`, `server_error`, `empty_response`, `other_error`
- **Latency** in milliseconds
- **Truncated error** message (if any)

Per-provider rolling stats are maintained: attempts, successes, failures by category, cooldown state, and daily-block state.

### Commands

- `!status` — extended to show per-provider attempt counts, success/failure totals, and cooldown/daily-block status.
- `!llmlog [N]` — show last N attempts (default 20, max 200) with full detail.

Example `!status` LLM section:
```
**LLM**
Requests today (UTC): 47 (failed: 12)
**openrouter** — attempts: 31, ok: 24, fail: 7
• `openrouter/free` — ready
• `meta-llama/llama-3.1-8b-instruct:free` — cooling down 145s
**google** — attempts: 16, ok: 14, fail: 2
• `gemini-1.5-flash` — ready
```

Example `!llmlog 5`:
```
**LLM Attempt Log** (newest first)
`14:32:10` ✅ openrouter:meta-llama/llama-3.1-8b-instruct:free → success (842ms)
`14:31:45` 🚫 openrouter:openrouter/free → rate_limited (1200ms) — APIStatusError: 429 ...
`14:31:40` 💥 google:gemini-1.5-flash → server_error (3500ms) — APIStatusError: 503 ...
```

## Provider setup

| Provider | Get a key | Notes |
|---|---|---|
| OpenRouter | <https://openrouter.ai/keys> | `MODELS=openrouter/free,auto` covers a rotating set of free models |
| Google AI Studio | <https://aistudio.google.com/apikey> | free tier, no card |
| NVIDIA NIM | <https://build.nvidia.com> | free tier, generous rate limits |
| Ollama (yours, or another PC's) | none | point `OLLAMA_BASE_URL` at it; run it with `OLLAMA_HOST=0.0.0.0 ollama serve` so it's reachable over the LAN |

A daily quota hit on one provider falls through to the next immediately — it doesn't wait or block the others.