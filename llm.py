"""A smolagents model for OpenRouter's free tier that survives its flakiness.

What it handles, per LLM call:
  * hard HTTP timeouts (no request can hang the agent)
  * 429 rate limits: waits (honouring Retry-After) for per-minute limits,
    fails fast and remembers it for the day-cap ("free-models-per-day")
  * 5xx / timeouts / connection drops / empty or malformed replies: retry, then
    fall through to the next model in the list
  * 404 / 400 / 402: model was retired or can't do what we need -> cool it down
  * a per-call deadline and an abort switch so !stop and run timeouts work
  * models that answer in prose instead of calling a tool: the prose becomes the
    final answer instead of burning steps on parse errors
"""
from __future__ import annotations

import datetime as dt
import logging
import random
import threading
import time
import uuid
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
            if "per-day" in msg or "per day" in msg or "daily" in msg:
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


class ResilientModel(OpenAIServerModel):
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._configured = list(cfg.models)
        self._discovered: list[str] = []
        self._discovered_at = 0.0
        self._cooldown: dict[str, float] = {}
        self._daily_block_until = 0.0
        self._day = dt.datetime.now(dt.timezone.utc).date()
        self.requests_today = 0
        self.failures_today = 0
        self.last_error = ""
        self.abort = threading.Event()
        self.on_event = None  # optional callable(kind, model=..., detail=...) for the friction log

        first = next((m for m in self._configured if m != "auto"), "openrouter/free")
        super().__init__(
            model_id=first,
            api_base=cfg.openrouter_base,
            api_key=cfg.openrouter_key,
            client_kwargs={
                "timeout": cfg.llm_timeout,
                "max_retries": 0,  # all retry logic lives here
                "default_headers": {"X-Title": "pi-agent"},
            },
            retry=False,  # disable smolagents' own (rate-limit-only, unbounded-sleep) retry
            temperature=cfg.temperature,
            max_tokens=cfg.max_tokens,
        )

    # ------------------------------------------------------------------ public

    def begin_run(self) -> None:
        self.abort.clear()

    def stop(self) -> None:
        self.abort.set()

    def models(self) -> list[str]:
        """Configured models in order; the token 'auto' expands to discovered free models."""
        out: list[str] = []
        for m in self._configured:
            for candidate in (self._discovered if m == "auto" else [m]):
                if candidate not in out:
                    out.append(candidate)
        return out or ["openrouter/free"]

    def describe(self) -> str:
        self._rollover()
        now = time.time()
        lines = [f"Requests today (UTC): {self.requests_today} (failed: {self.failures_today})"]
        if self._daily_block_until > now:
            lines.append(f"⛔ Daily free quota exhausted, resets in {self._fmt_wait(self._daily_block_until - now)}")
        for m in self.models():
            left = self._cooldown.get(m, 0) - now
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

        for model_id in self._candidates():
            attempt = 0
            while attempt < self.cfg.llm_attempts_per_model:
                self._check_abort(deadline)
                try:
                    self.requests_today += 1
                    return self._call(model_id, messages, stop_sequences, response_format, tools_to_call_from, **kwargs)
                except Exception as exc:  # noqa: BLE001 - classified below
                    kind, wait = _classify(exc)
                    if kind == "bug":
                        raise
                    self.failures_today += 1
                    self.last_error = f"{model_id}: {_short(exc, 120)}"
                    errors.append(f"{model_id}: {_short(exc, 120)}")
                    log.warning("LLM call failed [%s/%s]: %s", model_id, kind, _short(exc))
                    self._emit(f"llm_{kind}", model_id, _short(exc, 150))

                    if kind == "fatal":
                        raise LLMFatalError(
                            "OpenRouter rejected the API key (401). Check OPENROUTER_API_KEY."
                        ) from exc
                    if kind == "daily":
                        self._block_until_midnight()
                        raise LLMQuotaError(
                            f"Daily free-model request cap reached. Resets in {self._fmt_wait(self._daily_block_until - time.time())}."
                        ) from exc
                    if kind == "rate_wait":  # account-wide per-minute limit: waiting is the only fix
                        rate_waits += 1
                        if rate_waits > _MAX_RATE_WAITS:
                            raise LLMUnavailableError("Still rate limited after waiting. Try again in a minute.") from exc
                        self._sleep(min(max(wait if wait is not None else 6.0, 1.0), 30.0), deadline)
                        continue  # doesn't consume an attempt

                    attempt += 1
                    if kind in _COOLDOWN:  # model-specific problem: move on immediately
                        self._cooldown[model_id] = time.time() + _COOLDOWN[kind]
                        break
                    if attempt >= self.cfg.llm_attempts_per_model:
                        self._cooldown[model_id] = time.time() + _TRANSIENT_COOLDOWN
                        break
                    self._sleep(min(2**attempt + random.random(), 8.0), deadline)

        raise LLMUnavailableError("All models failed. " + " | ".join(errors[-3:]))

    # ---------------------------------------------------------------- internals

    def _call(self, model_id, messages, stop_sequences, response_format, tools, **kwargs) -> ChatMessage:
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
        resp = self.client.chat.completions.create(**params)

        choices = getattr(resp, "choices", None)
        if not choices:  # OpenRouter can return HTTP 200 with {"error": {...}}
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
        self.model_id = model_id  # so agent logs and the friction log show which model actually answered
        if tools and not out.tool_calls:
            out = self._prose_to_tool_call(out, tools)
        return out

    def _prose_to_tool_call(self, msg: ChatMessage, tools: list) -> ChatMessage:
        names = {t.name for t in tools}
        try:  # some models print the call as JSON text instead of using the tool API
            parsed = self.parse_tool_calls(msg)
            if parsed.tool_calls and parsed.tool_calls[0].function.name in names:
                return parsed
        except Exception:  # noqa: BLE001
            pass
        msg.tool_calls = None
        if "final_answer" in names and msg.content:
            self._emit("prose_final", self.model_id, "model answered in prose instead of calling a tool")
            msg.tool_calls = [
                ChatMessageToolCall(
                    id=f"call_{uuid.uuid4().hex[:12]}",
                    type="function",
                    function=ChatMessageToolCallFunction(name="final_answer", arguments={"answer": msg.content.strip()}),
                )
            ]
        return msg

    def _emit(self, kind: str, model: str, detail: str) -> None:
        if self.on_event:
            try:
                self.on_event(kind, model=model, detail=detail)
            except Exception:  # noqa: BLE001 - diagnostics must never break a call
                log.exception("friction hook failed")

    def _candidates(self) -> list[str]:
        models, now = self.models(), time.time()
        ready = [m for m in models if self._cooldown.get(m, 0) <= now]
        # If everything is cooling down, try the one that recovers first rather than failing instantly.
        return ready or [min(models, key=lambda m: self._cooldown.get(m, 0))]

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
        left = self._daily_block_until - time.time()
        if left > 0:
            raise LLMQuotaError(f"Daily free-model request cap reached. Resets in {self._fmt_wait(left)}.")

    def _block_until_midnight(self) -> None:
        now = dt.datetime.now(dt.timezone.utc)
        tomorrow = (now + dt.timedelta(days=1)).replace(hour=0, minute=0, second=5, microsecond=0)
        self._daily_block_until = tomorrow.timestamp()

    @staticmethod
    def _fmt_wait(seconds: float) -> str:
        h, m = divmod(int(max(seconds, 0)) // 60, 60)
        return f"{h}h {m}m" if h else f"{m}m"

    # ----------------------------------------------------- free-model discovery

    def _maybe_refresh_models(self, max_age: float = 6 * 3600) -> None:
        if "auto" not in self._configured or time.time() - self._discovered_at < max_age:
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
