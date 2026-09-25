"""A smolagents model that cascades across several free/cheap OpenAI-compatible providers.

Providers tried in the order configured (PROVIDERS): each provider's own models are tried in
order before moving to the next provider. Built in: OpenRouter, Google AI Studio (Gemini),
NVIDIA NIM, and an optional Ollama endpoint (local or remote, e.g. another PC on your LAN) -
all speak the same OpenAI chat-completions API, so adding one more is a small, mechanical change.

What it handles, per LLM call:
  * hard HTTP timeouts (no request can hang the agent)
  * 429 rate limits: waits (honouring Retry-After) for per-minute limits,
    fails fast and remembers it for the day-cap ("free-models-per-day")
  * 5xx / timeouts / connection drops / empty or malformed replies: retry, then
    fall through to the next model, then the next provider
  * 404 / 400 / 402: model was retired or can't do what we need -> cool it down
  * a per-call deadline and an abort switch so !stop and run timeouts work
  * models that answer in prose instead of calling a tool: the prose becomes the
    final answer instead of burning steps on parse errors

The "daily cap" wording match in _classify() was written against OpenRouter's actual error
text. Google/NVIDIA may phrase their own quota errors differently; when the wording isn't
recognised, a daily cap still degrades safely to an ordinary rate-limited retry/fallback -
it just won't get the precise "resets in Xh" message.
"""
from __future__ import annotations

import datetime as dt
import logging
import random
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Any

import openai
import requests
from smolagents import OpenAIServerModel
from smolagents.models import (
    ChatMessage,
    ChatMessageToolCall,
    ChatMessageToolCallFunction,
    remove_content_after_stop_sequences,
)
from smolagents.monitoring import TokenUsage

from config import Config

log = logging.getLogger("llm")


class LLMError(Exception):
    """Base class. The agent wraps these in AgentGenerationError; the bot unwraps via __cause__."""


class LLMFatalError(LLMError):
    """Configuration problem (bad API key...). Retrying won't help."""


class LLMQuotaError(LLMError):
    """Daily free-model request cap reached."""


class LLMUnavailableError(LLMError):
    """Every model failed, or the per-call deadline passed."""


class LLMAborted(LLMError):
    """The user stopped the run."""


class _BadResponse(Exception):
    """HTTP 200, but the body was unusable (OpenRouter sometimes does this)."""


@dataclass(frozen=True)
class _AttemptRecord:
    """Single provider/model attempt record for observability."""
    timestamp: float
    provider: str
    model: str
    outcome: str  # success, timeout, rate_limited, auth_error, server_error, empty_response, other_error
    latency_ms: int
    error: str = ""


@dataclass
class _ProviderStats:
    """Rolling stats per provider."""
    attempts: int = 0
    successes: int = 0
    failures: dict[str, int] = field(default_factory=lambda: {
        "timeout": 0,
        "rate_limited": 0,
        "auth_error": 0,
        "server_error": 0,
        "empty_response": 0,
        "other_error": 0,
    })
    cooldown_until: float = 0.0
    daily_blocked_until: float = 0.0


@dataclass(frozen=True)
class _Provider:
    name: str
    base_url: str
    api_key: str  # may be "" (e.g. a local Ollama with no auth)
    models: tuple[str, ...]  # explicit list; "auto" (OpenRouter only) is expanded at call time


# How long a model is skipped after failing in each way (seconds).
_COOLDOWN = {"model_down": 30 * 60, "bad_request": 10 * 60, "upstream": 2 * 60}
_TRANSIENT_COOLDOWN = 90
_MAX_RATE_WAITS = 3  # per-minute-limit sleeps allowed within one generate() call


def _short(exc: BaseException, n: int = 200) -> str:
    text = " ".join(str(exc).split())
    return f"{type(exc).__name__}: {text[:n]}"


def _retry_after(exc: BaseException) -> float | None:
    """Seconds to wait according to Retry-After / X-RateLimit-Reset, if present."""
    headers: dict[str, Any] = dict(getattr(getattr(exc, "response", None), "headers", None) or {})
    headers = {k.lower(): v for k, v in headers.items()}
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        meta = ((body.get("error") or {}).get("metadata") or {}).get("headers") or {}
        headers.update({str(k).lower(): v for k, v in meta.items()})

    if headers.get("retry-after"):
        try:
            return float(headers["retry-after"])
        except ValueError:
            pass
    reset = headers.get("x-ratelimit-reset")
    if reset:
        try:
            value = float(reset)
            value = value / 1000 if value > 1e12 else value  # ms epoch -> s
            if value > 1e9:
                return max(0.0, value - time.time())
        except ValueError:
            pass
    return None


def _classify(exc: BaseException) -> tuple[str, float | None]:
    """Map an exception to (kind, retry_after)."""
    if isinstance(exc, _BadResponse):
        return "transient", None
    if isinstance(exc, openai.APIStatusError):
        code, msg = exc.status_code, str(exc).lower()
        if code == 401:
            return "fatal", None
        if code in (402, 404):
            return "model_down", None  # retired / needs credits / no provider for our params
        if code == 429:
            # "resource_exhausted" is Google's own gRPC-derived error name for its daily quota;
            # generic "quota" is deliberately NOT matched here since some providers use that word
            # for ordinary per-minute limits too, and misclassifying those as "daily" is worse
            # than just falling through to the generic rate-limit retry below.
            if "per-day" in msg or "per day" in msg or "daily" in msg or "resource_exhausted" in msg:
                return "daily", None
            if "upstream" in msg or "provider" in msg:
                return "upstream", None
            return "rate_wait", _retry_after(exc)
        if code in (400, 403, 413, 422):
            return "bad_request", None  # context too long, moderation, unsupported params...
        return "transient", _retry_after(exc)  # 408, 5xx...
    if isinstance(exc, openai.APIError):
        return "transient", None  # timeouts and connection errors
    return "bug", None  # our own mistake: don't swallow it


def _classify_to_outcome(kind: str, exc: BaseException | None = None) -> str:
    """Map _classify kind to observability outcome category."""
    if kind == "transient":
        # Check if it's a timeout specifically (APITimeoutError or connection error)
        if exc and isinstance(exc, (openai.APITimeoutError, openai.APIConnectionError)):
            return "timeout"
        return "server_error"
    if kind == "rate_wait":
        return "rate_limited"
    if kind == "fatal":
        return "auth_error"
    if kind == "daily":
        return "rate_limited"  # daily cap is a form of rate limiting
    if kind == "model_down":
        return "server_error"
    if kind == "bad_request":
        # Could be empty response or other client error
        if exc and isinstance(exc, _BadResponse) and "empty" in str(exc).lower():
            return "empty_response"
        return "other_error"
    if kind == "upstream":
        return "server_error"
    return "other_error"


class ResilientModel(OpenAIServerModel):
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._providers = self._build_providers(cfg)
        if not self._providers:
            raise LLMFatalError("No usable LLM provider configured (check PROVIDERS and its API key).")
        self._discovered: list[str] = []  # OpenRouter "auto" expansion only
        self._discovered_at = 0.0
        self._cooldown: dict[tuple[str, str], float] = {}  # (provider, model) -> until timestamp
        self._daily_block: dict[str, float] = {}  # provider -> until timestamp
        self._day = dt.datetime.now(dt.timezone.utc).date()
        self.requests_today = 0
        self.failures_today = 0
        self.last_error = ""
        self._prose_count = 0  # consecutive prose-only replies ("thinking" streak) in this run
        self._PROSE_LIMIT = 3  # after this many, force a final_answer (last resort)
        self.abort = threading.Event()
        self.on_event = None  # optional callable(kind, model=..., detail=...) for the friction log

        # Observability: ring buffer of attempts and per-provider rolling stats
        self._attempt_log: deque[_AttemptRecord] = deque(maxlen=200)
        self._provider_stats: dict[str, _ProviderStats] = {p.name: _ProviderStats() for p in self._providers}

        primary = self._providers[0]
        placeholder = next((m for m in primary.models if m != "auto"), "model")
        super().__init__(
            model_id=f"{primary.name}:{placeholder}",
            api_base=primary.base_url,
            api_key=primary.api_key or "not-needed",  # the openai client rejects "" / None outright
            client_kwargs={
                "timeout": cfg.llm_timeout,
                "max_retries": 0,  # all retry logic lives here
                "default_headers": {"X-Title": "jarvis"},
            },
            retry=False,  # disable smolagents' own (rate-limit-only, unbounded-sleep) retry
            temperature=cfg.temperature,
            max_tokens=cfg.max_tokens,
        )
        # One openai.OpenAI client per provider (base_url/api_key are fixed per client in that SDK).
        self._clients: dict[str, openai.OpenAI] = {primary.name: self.client}
        for p in self._providers[1:]:
            self._clients[p.name] = openai.OpenAI(
                base_url=p.base_url,
                api_key=p.api_key or "not-needed",
                timeout=cfg.llm_timeout,
                max_retries=0,
                default_headers={"X-Title": "jarvis"},
            )

    @staticmethod
    def _build_providers(cfg: Config) -> list[_Provider]:
        by_name = {
            "openrouter": _Provider("openrouter", cfg.openrouter_base, cfg.openrouter_key, cfg.models),
            "google": _Provider("google", cfg.google_base, cfg.google_key, cfg.google_models),
            "nvidia": _Provider("nvidia", cfg.nvidia_base, cfg.nvidia_key, cfg.nvidia_models),
            "ollama": _Provider("ollama", cfg.ollama_base, "", cfg.ollama_models),
        }
        return [by_name[name] for name in cfg.providers if name in by_name]

    # ------------------------------------------------------------------ public

    def begin_run(self) -> None:
        self.abort.clear()
        self._prose_count = 0  # fresh run -> fresh thinking streak

    def stop(self) -> None:
        self.abort.set()

    def describe(self) -> str:
        self._rollover()
        now = time.time()
        lines = [f"Requests today (UTC): {self.requests_today} (failed: {self.failures_today})"]
        for p in self._providers:
            blocked = self._daily_block.get(p.name, 0) - now
            stats = self._provider_stats.get(p.name)
            if stats:
                total_failures = sum(stats.failures.values())
                lines.append(
                    f"**{p.name}** — attempts: {stats.attempts}, ok: {stats.successes}, "
                    f"fail: {total_failures}" + (f" — ⛔ day cap, resets in {self._fmt_wait(blocked)}" if blocked > 0 else "")
                )
            else:
                lines.append(f"**{p.name}**" + (f" — ⛔ day cap, resets in {self._fmt_wait(blocked)}" if blocked > 0 else ""))
            for m in self._provider_models(p.name):
                left = self._cooldown.get((p.name, m), 0) - now
                lines.append(f"• `{m}` — " + (f"cooling down {int(left)}s" if left > 0 else "ready"))
        if self.last_error:
            lines.append(f"Last error: {self.last_error}")
        return "\n".join(lines)

    # ------------------------------------------------------------- generate()

    def generate(  # type: ignore[override]
        self,
        messages: list[ChatMessage | dict],
        stop_sequences: list[str] | None = None,
        response_format: dict[str, str] | None = None,
        tools_to_call_from: list | None = None,
        **kwargs: Any,
    ) -> ChatMessage:
        self._rollover()
        self._check_daily_block()
        self._maybe_refresh_models()

        deadline = time.monotonic() + self.cfg.llm_call_deadline
        errors: list[str] = []
        rate_waits = 0

        for provider, model_id in self._candidates():
            if self._daily_block.get(provider, 0) > time.time():
                continue  # this provider got day-blocked earlier within this same call
            attempt = 0
            while attempt < self.cfg.llm_attempts_per_model:
                self._check_abort(deadline)
                start = time.perf_counter()
                try:
                    self.requests_today += 1
                    result = self._call(provider, model_id, messages, stop_sequences, response_format, tools_to_call_from, **kwargs)
                    latency_ms = int((time.perf_counter() - start) * 1000)
                    self._record_attempt(provider, model_id, "success", latency_ms, "")
                    return result
                except Exception as exc:  # noqa: BLE001 - classified below
                    latency_ms = int((time.perf_counter() - start) * 1000)
                    kind, wait = _classify(exc)
                    if kind == "bug":
                        raise
                    outcome = _classify_to_outcome(kind, exc)
                    error_msg = _short(exc, 150)
                    self._record_attempt(provider, model_id, outcome, latency_ms, error_msg)
                    label = f"{provider}:{model_id}"
                    self.failures_today += 1
                    self.last_error = f"{label}: {_short(exc, 120)}"
                    errors.append(f"{label}: {_short(exc, 120)}")
                    log.warning("LLM call failed [%s/%s]: %s", label, kind, _short(exc))
                    self._emit(f"llm_{kind}", label, _short(exc, 150))

                    if kind == "fatal":
                        raise LLMFatalError(f"{provider} rejected its API key (401). Check that provider's API key setting.") from exc
                    if kind == "daily":
                        self._daily_block[provider] = self._midnight_utc()
                        break  # try the next candidate - a different provider, if any is configured
                    if kind == "rate_wait":  # account-wide per-minute limit: waiting is the only fix
                        rate_waits += 1
                        if rate_waits > _MAX_RATE_WAITS:
                            raise LLMUnavailableError("Still rate limited after waiting. Try again in a minute.") from exc
                        self._sleep(min(max(wait if wait is not None else 6.0, 1.0), 30.0), deadline)
                        continue  # doesn't consume an attempt

                    attempt += 1
                    if kind in _COOLDOWN:  # model-specific problem: move on immediately
                        self._cooldown[(provider, model_id)] = time.time() + _COOLDOWN[kind]
                        break
                    if attempt >= self.cfg.llm_attempts_per_model:
                        self._cooldown[(provider, model_id)] = time.time() + _TRANSIENT_COOLDOWN
                        break
                    self._sleep(min(2**attempt + random.random(), 8.0), deadline)

        # If we get here, every candidate failed. If that's because every provider is now
        # day-blocked, say so with reset times; otherwise it's an ordinary outage.
        self._check_daily_block()
        raise LLMUnavailableError("All providers/models failed. " + " | ".join(errors[-3:]))

    # ---------------------------------------------------------------- internals

    def _call(self, provider, model_id, messages, stop_sequences, response_format, tools, **kwargs) -> ChatMessage:
        params = self._prepare_completion_kwargs(
            messages=messages,
            # Native tool calling doesn't need stop sequences; skip them there.
            stop_sequences=None if tools else stop_sequences,
            response_format=response_format,
            tools_to_call_from=tools,
            model=model_id,
            custom_role_conversions=self.custom_role_conversions,
            convert_images_to_image_urls=True,
            # "required" narrows the provider pool a lot on OpenRouter; "auto" + our
            # prose->final_answer fallback is far more reliable on free models.
            tool_choice="auto",
            **kwargs,
        )
        resp = self._clients[provider].chat.completions.create(**params)

        choices = getattr(resp, "choices", None)
        if not choices:  # some providers can return HTTP 200 with {"error": {...}}
            err = getattr(resp, "error", None) or (getattr(resp, "model_extra", None) or {}).get("error")
            raise _BadResponse(f"no choices in response ({err})")
        msg = choices[0].message
        content = remove_content_after_stop_sequences(msg.content, stop_sequences)
        if not msg.tool_calls and not (content and content.strip()):
            raise _BadResponse("empty response")

        usage = getattr(resp, "usage", None)
        out = ChatMessage(
            role=msg.role or "assistant",
            content=content,
            tool_calls=msg.tool_calls or None,
            raw=resp,
            token_usage=TokenUsage(input_tokens=usage.prompt_tokens, output_tokens=usage.completion_tokens)
            if usage
            else None,
        )
        self.model_id = f"{provider}:{model_id}"  # so agent logs and the friction log show which model answered
        if out.tool_calls:
            self._prose_count = 0  # a real tool call ends any thinking streak
        elif tools and out.content and out.content.strip():
            out = self._handle_prose(out, tools)
        return out

    def _handle_prose(self, msg: ChatMessage, tools: list) -> ChatMessage:
        """Resolve a prose-only reply while tool calling is enabled.

        Returns either a real tool call (the prose encoded a known tool, or a
        forced final_answer as a last resort) or the prose itself -- a "thinking"
        step that the agent re-prompts on. A short streak of consecutive prose
        replies prevents the run from stalling forever without a tool call.
        """
        names = {t.name for t in tools}
        try:  # some models print the call as JSON text instead of using the tool API
            parsed = self.parse_tool_calls(msg)
            if parsed.tool_calls and parsed.tool_calls[0].function.name in names:
                self._prose_count = 0  # a real (in-prose) tool call resets the streak
                return parsed
        except Exception:  # noqa: BLE001
            pass
        msg.tool_calls = None
        if (
            "final_answer" in names
            and self._prose_count + 1 >= self._PROSE_LIMIT
            and msg.content and msg.content.strip()
        ):
            # last resort: the model has been talking in prose the whole time.
            self._prose_count = self._PROSE_LIMIT
            self._emit("prose_final", self.model_id,
                       f"consecutive prose replies {self._PROSE_LIMIT}; forcing final_answer")
            msg.tool_calls = [
                ChatMessageToolCall(
                    id=f"call_{uuid.uuid4().hex[:12]}",
                    type="function",
                    function=ChatMessageToolCallFunction(
                        name="final_answer", arguments={"answer": msg.content.strip()}),
                )
            ]
        else:
            self._prose_count += 1  # thinking step; the agent re-prompts on the next loop
        return msg

    def _emit(self, kind: str, model: str, detail: str) -> None:
        if self.on_event:
            try:
                self.on_event(kind, model=model, detail=detail)
            except Exception:  # noqa: BLE001 - diagnostics must never break a call
                log.exception("friction hook failed")

    def _record_attempt(self, provider: str, model: str, outcome: str, latency_ms: int, error: str) -> None:
        """Record an attempt in the ring buffer and update per-provider stats."""
        now = time.time()
        record = _AttemptRecord(
            timestamp=now,
            provider=provider,
            model=model,
            outcome=outcome,
            latency_ms=latency_ms,
            error=error,
        )
        self._attempt_log.append(record)
        stats = self._provider_stats.get(provider)
        if stats is None:
            stats = _ProviderStats()
            self._provider_stats[provider] = stats
        stats.attempts += 1
        if outcome == "success":
            stats.successes += 1
        else:
            if outcome in stats.failures:
                stats.failures[outcome] += 1
            else:
                stats.failures["other_error"] += 1
        # Update cooldown state from internal tracking
        for (p, m), until in self._cooldown.items():
            if p == provider and until > stats.cooldown_until:
                stats.cooldown_until = until
        if self._daily_block.get(provider, 0) > stats.daily_blocked_until:
            stats.daily_blocked_until = self._daily_block[provider]

    def get_llm_stats_summary(self) -> dict[str, Any]:
        """Return a summary of per-provider LLM stats for observability."""
        now = time.time()
        summary = {
            "total_attempts": sum(s.attempts for s in self._provider_stats.values()),
            "total_successes": sum(s.successes for s in self._provider_stats.values()),
            "providers": {},
        }
        for name, stats in self._provider_stats.items():
            failures = dict(stats.failures)
            total_failures = sum(failures.values())
            cooldown_remaining = max(0.0, stats.cooldown_until - now)
            daily_blocked_remaining = max(0.0, stats.daily_blocked_until - now)
            summary["providers"][name] = {
                "attempts": stats.attempts,
                "successes": stats.successes,
                "failures": failures,
                "total_failures": total_failures,
                "success_rate": stats.successes / stats.attempts if stats.attempts > 0 else 0.0,
                "cooldown_active": cooldown_remaining > 0,
                "cooldown_remaining_sec": round(cooldown_remaining, 1),
                "daily_blocked": daily_blocked_remaining > 0,
                "daily_blocked_remaining_sec": round(daily_blocked_remaining, 1),
            }
        return summary

    def get_recent_attempts(self, n: int = 20) -> list[_AttemptRecord]:
        """Return the last N attempt records (newest first)."""
        return list(self._attempt_log)[-n:][::-1]

    def _provider_models(self, name: str) -> list[str]:
        """This provider's configured models, with OpenRouter's 'auto' expanded to discovered ones."""
        p = next((p for p in self._providers if p.name == name), None)
        if p is None:
            return []
        if name != "openrouter":
            return list(p.models)
        out: list[str] = []
        for m in p.models:
            for candidate in (self._discovered if m == "auto" else [m]):
                if candidate not in out:
                    out.append(candidate)
        return out or ["openrouter/free"]

    def _candidate_pairs(self) -> list[tuple[str, str]]:
        """All (provider, model) pairs in try-order, skipping providers that are day-blocked."""
        now = time.time()
        return [
            (p.name, m)
            for p in self._providers
            if self._daily_block.get(p.name, 0) <= now
            for m in self._provider_models(p.name)
        ]

    def _candidates(self) -> list[tuple[str, str]]:
        pairs, now = self._candidate_pairs(), time.time()
        ready = [pr for pr in pairs if self._cooldown.get(pr, 0) <= now]
        if ready:
            return ready
        # Nothing not-blocked is ready: try the one that recovers first rather than failing instantly.
        return [min(pairs, key=lambda pr: self._cooldown.get(pr, 0))] if pairs else []

    def _check_abort(self, deadline: float) -> None:
        if self.abort.is_set():
            raise LLMAborted("Stopped.")
        if time.monotonic() > deadline:
            raise LLMUnavailableError(f"Gave up after {self.cfg.llm_call_deadline:.0f}s waiting for the LLM.")

    def _sleep(self, seconds: float, deadline: float) -> None:
        if time.monotonic() + seconds > deadline:
            raise LLMUnavailableError(f"Gave up after {self.cfg.llm_call_deadline:.0f}s waiting for the LLM.")
        if self.abort.wait(seconds):  # interruptible sleep
            raise LLMAborted("Stopped.")

    def _rollover(self) -> None:
        today = dt.datetime.now(dt.timezone.utc).date()
        if today != self._day:
            self._day, self.requests_today, self.failures_today = today, 0, 0

    def _check_daily_block(self) -> None:
        now = time.time()
        if all(self._daily_block.get(p.name, 0) > now for p in self._providers):
            raise LLMQuotaError(f"Daily free quota reached on every configured provider. {self._daily_status()}")

    def _daily_status(self) -> str:
        now = time.time()
        parts = [f"{p.name} resets in {self._fmt_wait(self._daily_block[p.name] - now)}" for p in self._providers if self._daily_block.get(p.name, 0) > now]
        return "; ".join(parts)

    @staticmethod
    def _midnight_utc() -> float:
        now = dt.datetime.now(dt.timezone.utc)
        tomorrow = (now + dt.timedelta(days=1)).replace(hour=0, minute=0, second=5, microsecond=0)
        return tomorrow.timestamp()

    @staticmethod
    def _fmt_wait(seconds: float) -> str:
        h, m = divmod(int(max(seconds, 0)) // 60, 60)
        return f"{h}h {m}m" if h else f"{m}m"

    # ----------------------------------------------------- free-model discovery (OpenRouter only)

    def _maybe_refresh_models(self, max_age: float = 6 * 3600) -> None:
        p = next((p for p in self._providers if p.name == "openrouter"), None)
        if p is None or "auto" not in p.models or time.time() - self._discovered_at < max_age:
            return
        self._discovered_at = time.time()  # set first: a failure shouldn't retry on every call
        try:
            self._discovered = self._discover()
            log.info("Discovered free tool-capable models: %s", self._discovered)
        except Exception as exc:  # noqa: BLE001
            log.warning("Free-model discovery failed (%s); keeping previous list", _short(exc))

    def _discover(self) -> list[str]:
        r = requests.get(f"{self.cfg.openrouter_base}/models", timeout=15)
        r.raise_for_status()
        found = []
        for m in r.json().get("data", []):
            pricing = m.get("pricing") or {}
            is_free = str(pricing.get("prompt")) == "0" and str(pricing.get("completion")) == "0"
            has_tools = "tools" in (m.get("supported_parameters") or [])
            big_enough = (m.get("context_length") or 0) >= 32_000  # agent prompts + tool schemas are large
            model_id = m.get("id", "")
            if is_free and has_tools and big_enough and not model_id.startswith("openrouter/"):
                found.append((m.get("created") or 0, model_id))
        found.sort(reverse=True)  # newest first
        return [model_id for _, model_id in found[: self.cfg.auto_models_limit]]
