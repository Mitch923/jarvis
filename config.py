"""Configuration, loaded once from environment variables (or a .env file)."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


def _str(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _int(name: str, default: int) -> int:
    raw = _str(name)
    return int(raw) if raw else default


def _float(name: str, default: float) -> float:
    raw = _str(name)
    return float(raw) if raw else default


def _bool(name: str, default: bool) -> bool:
    raw = _str(name).lower()
    return default if not raw else raw in {"1", "true", "yes", "on"}


def _csv(name: str) -> tuple[str, ...]:
    return tuple(p.strip() for p in _str(name).split(",") if p.strip())


class ConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class Config:
    # --- Discord ---
    discord_token: str
    allowed_user_ids: frozenset[int]  # ONLY these users can talk to the bot
    primary_user_id: int  # first ID listed: receives watcher alerts and the daily digest
    channel_ids: frozenset[int]  # channels where the bot answers without @mention

    # --- LLM providers ---
    # Tried in this order; each provider tries its own models in order before the next provider
    # is tried. All are OpenAI-compatible endpoints, so adding a new one is a small change.
    providers: tuple[str, ...]
    openrouter_key: str
    openrouter_base: str
    models: tuple[str, ...]  # tried in order; "auto" expands to discovered free models
    auto_models_limit: int
    google_key: str
    google_base: str
    google_models: tuple[str, ...]
    nvidia_key: str
    nvidia_base: str
    nvidia_models: tuple[str, ...]
    ollama_base: str  # e.g. http://192.168.1.50:11434/v1; required if "ollama" is in PROVIDERS
    ollama_models: tuple[str, ...]
    llm_timeout: float  # seconds per HTTP request
    llm_call_deadline: float  # seconds for one generate() incl. all retries/fallbacks
    llm_attempts_per_model: int
    max_tokens: int
    temperature: float

    # --- Agent ---
    agent_type: str  # "tool" (safer) or "code"
    max_steps: int
    run_timeout: float  # wall-clock seconds for one whole task
    history_turns: int
    tool_output_chars: int
    timezone: str

    # --- GitHub ---
    github_token: str
    github_api_url: str
    github_owner: str
    github_allowed_repos: frozenset[str]
    github_write: bool
    branch_prefix: str
    approval_mode: str  # "all" | "publish" | "none"
    approval_timeout: float

    # --- Background jobs (no LLM involved, so they cost no free-tier requests) ---
    notify_channel_id: int  # 0 = DM the primary user
    watch_interval: float  # seconds between GitHub polls; 0 = watcher off
    digest_time: str  # "HH:MM" in AGENT_TIMEZONE; "" = no daily digest

    # --- Self-improvement ---
    self_repo: str  # this bot's own GitHub repo (owner/name); blank = detect from the git checkout
    self_review_day: int  # 0=Mon .. 6=Sun, -1 = weekly review off
    self_review_time: str
    self_review_min_events: int  # fewer problem events than this -> skip the (LLM-costing) review
    improve_max_steps: int  # step budget for self-review / implement runs
    protected_paths: tuple[str, ...]  # extra globs the agent may never edit in its own repo

    # --- Storage ---
    data_dir: str
    memory_max_notes: int

    # --- Local checkouts + sandboxed tests ---
    checkouts_enabled: bool  # repo_sync/repo_grep/repo_read tools; needs GITHUB_TOKEN
    checkout_dir: str
    checkout_max_repos: int
    sandbox_runtime: str  # "docker", "podman", or "" to disable repo_test specifically
    sandbox_timeout: float
    sandbox_memory: str  # e.g. "1g", passed straight to the container runtime's --memory
    sandbox_cpus: str
    sandbox_network: str  # "bridge" (installs work, but the test run can reach the network) or "none"
    sandbox_network_allowed_repos: frozenset[str]  # repos that may use bridge network; others forced to "none"
    test_commands: dict[str, str]  # "owner/repo" -> shell command (or "image|command"); "*" -> fallback

    @classmethod
    def from_env(cls) -> "Config":
        load_dotenv()

        missing = [k for k in ("DISCORD_TOKEN", "DISCORD_ALLOWED_USER_IDS") if not _str(k)]
        if missing:
            raise ConfigError(f"Missing required settings: {', '.join(missing)} (see .env.example)")

        try:
            allowed_list = [int(x) for x in _csv("DISCORD_ALLOWED_USER_IDS")]
            channels = frozenset(int(x) for x in _csv("DISCORD_CHANNEL_IDS"))
            notify_channel = _int("NOTIFY_CHANNEL_ID", 0)
        except ValueError as e:
            raise ConfigError(f"Discord IDs must be numbers: {e}") from e

        repos = frozenset(r.lower().removeprefix("https://github.com/").strip("/") for r in _csv("GITHUB_ALLOWED_REPOS"))
        bad = [r for r in repos if not re.fullmatch(r"[a-z0-9_.-]+/[a-z0-9_.-]+", r)]
        if bad:
            raise ConfigError(f"GITHUB_ALLOWED_REPOS entries must look like owner/name: {', '.join(bad)}")
        if _str("GITHUB_TOKEN") and not repos and not _bool("GITHUB_ALLOW_ALL", False):
            raise ConfigError(
                "GITHUB_TOKEN is set but GITHUB_ALLOWED_REPOS is empty. List the repos the agent may see "
                "(free models can log prompts), or set GITHUB_ALLOW_ALL=1 to accept the risk."
            )

        self_repo = _str("SELF_REPO").lower().removeprefix("https://github.com/").strip("/")
        if self_repo and not re.fullmatch(r"[a-z0-9_.-]+/[a-z0-9_.-]+", self_repo):
            raise ConfigError("SELF_REPO must look like owner/name")
        days = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
        day = _str("SELF_REVIEW_DAY", "sun").lower()[:3]
        if day != "off" and day not in days:
            raise ConfigError("SELF_REVIEW_DAY must be mon..sun or off")
        review_time = _str("SELF_REVIEW_TIME", "09:00")
        if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", review_time):
            raise ConfigError("SELF_REVIEW_TIME must be HH:MM (24h)")

        digest = _str("DIGEST_TIME", "08:00")
        if digest and not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", digest):
            raise ConfigError("DIGEST_TIME must be HH:MM (24h), or empty to disable")

        agent_type = _str("AGENT_TYPE", "tool").lower()
        if agent_type not in {"tool", "code"}:
            raise ConfigError("AGENT_TYPE must be 'tool' or 'code'")

        approval = _str("APPROVAL_MODE", "publish").lower()
        if approval not in {"all", "publish", "none"}:
            raise ConfigError("APPROVAL_MODE must be 'all', 'publish' or 'none'")

        prefix = _str("AGENT_BRANCH_PREFIX", "agent/")
        if not prefix:
            raise ConfigError("AGENT_BRANCH_PREFIX must not be empty (it protects your real branches)")

        known_providers = ("openrouter", "google", "nvidia", "ollama")
        providers = _csv("PROVIDERS") or ("openrouter",)
        bad = [p for p in providers if p not in known_providers]
        if bad:
            raise ConfigError(f"PROVIDERS entries must be one of {', '.join(known_providers)}: {', '.join(bad)}")
        if len(set(providers)) != len(providers):
            raise ConfigError(f"PROVIDERS lists a provider more than once: {providers}")
        requires = {"openrouter": "OPENROUTER_API_KEY", "google": "GOOGLE_API_KEY", "nvidia": "NVIDIA_API_KEY", "ollama": "OLLAMA_BASE_URL"}
        unset = [f"{p} needs {requires[p]}" for p in providers if not _str(requires[p])]
        if unset:
            raise ConfigError(f"PROVIDERS lists a provider with no credentials set: {'; '.join(unset)}")

        sandbox_runtime = _str("SANDBOX_RUNTIME", "docker").lower()
        if sandbox_runtime not in ("docker", "podman", ""):
            raise ConfigError("SANDBOX_RUNTIME must be 'docker', 'podman', or empty to disable repo_test")
        sandbox_network = _str("SANDBOX_NETWORK", "none").lower()
        if sandbox_network not in ("bridge", "none"):
            raise ConfigError("SANDBOX_NETWORK must be 'bridge' or 'none'")
        sandbox_network_allowed_repos = frozenset(
            r.lower().removeprefix("https://github.com/").strip("/") for r in _csv("SANDBOX_NETWORK_ALLOWED_REPOS")
        )
        bad = [r for r in sandbox_network_allowed_repos if not re.fullmatch(r"[a-z0-9_.-]+/[a-z0-9_.-]+", r)]
        if bad:
            raise ConfigError(f"SANDBOX_NETWORK_ALLOWED_REPOS entries must look like owner/name: {', '.join(bad)}")

        test_commands: dict[str, str] = {}
        for pair in _str("TEST_COMMANDS").split(";"):
            if not pair.strip():
                continue
            if "=" not in pair:
                raise ConfigError(f"TEST_COMMANDS entry has no '=': {pair!r} (format: owner/repo=command;...)")
            repo, cmd = pair.split("=", 1)
            test_commands[repo.strip().lower()] = cmd.strip()

        data_dir = _str("DATA_DIR") or str(Path(__file__).resolve().parent / "data")

        return cls(
            discord_token=_str("DISCORD_TOKEN"),
            allowed_user_ids=frozenset(allowed_list),
            primary_user_id=allowed_list[0],
            channel_ids=channels,
            providers=providers,
            openrouter_key=_str("OPENROUTER_API_KEY"),
            openrouter_base=_str("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1").rstrip("/"),
            models=_csv("MODELS") or ("openrouter/free", "auto"),
            auto_models_limit=_int("AUTO_MODELS_LIMIT", 4),
            google_key=_str("GOOGLE_API_KEY"),
            google_base=_str("GOOGLE_BASE_URL", "https://generativelanguage.googleapis.com/v1beta/openai").rstrip("/"),
            google_models=_csv("GOOGLE_MODELS") or ("gemini-2.5-flash", "gemini-2.0-flash"),
            nvidia_key=_str("NVIDIA_API_KEY"),
            nvidia_base=_str("NVIDIA_BASE_URL", "https://integrate.api.nvidia.com/v1").rstrip("/"),
            nvidia_models=_csv("NVIDIA_MODELS") or ("meta/llama-3.3-70b-instruct",),
            ollama_base=_str("OLLAMA_BASE_URL").rstrip("/"),
            ollama_models=_csv("OLLAMA_MODELS") or ("llama3.1",),
            llm_timeout=_float("LLM_TIMEOUT", 60),
            llm_call_deadline=_float("LLM_CALL_DEADLINE", 150),
            llm_attempts_per_model=max(1, _int("LLM_ATTEMPTS_PER_MODEL", 2)),
            max_tokens=_int("LLM_MAX_TOKENS", 2048),
            temperature=_float("LLM_TEMPERATURE", 0.2),
            agent_type=agent_type,
            max_steps=_int("MAX_STEPS", 12),
            run_timeout=_float("RUN_TIMEOUT", 420),
            history_turns=_int("HISTORY_TURNS", 10),
            tool_output_chars=_int("TOOL_OUTPUT_CHARS", 16000),
            timezone=_str("AGENT_TIMEZONE", "UTC"),
            github_token=_str("GITHUB_TOKEN"),
            github_api_url=_str("GITHUB_API_URL", "https://api.github.com").rstrip("/"),
            github_owner=_str("GITHUB_OWNER"),
            github_allowed_repos=repos,
            github_write=_bool("GITHUB_WRITE", True),
            branch_prefix=prefix,
            approval_mode=approval,
            approval_timeout=_float("APPROVAL_TIMEOUT", 180),
            notify_channel_id=notify_channel,
            watch_interval=0 if not _float("WATCH_INTERVAL", 300) else max(60.0, _float("WATCH_INTERVAL", 300)),
            digest_time=digest,
            self_repo=self_repo,
            self_review_day=-1 if day == "off" else days.index(day),
            self_review_time=review_time,
            self_review_min_events=_int("SELF_REVIEW_MIN_EVENTS", 5),
            improve_max_steps=_int("IMPROVE_MAX_STEPS", 20),
            protected_paths=tuple(p.lower() for p in _csv("PROTECTED_PATHS")),
            data_dir=data_dir,
            memory_max_notes=_int("MEMORY_MAX_NOTES", 40),
            checkouts_enabled=_bool("CHECKOUTS_ENABLED", True),
            checkout_dir=_str("CHECKOUT_DIR") or str(Path(data_dir) / "repos"),
            checkout_max_repos=_int("CHECKOUT_MAX_REPOS", 6),
            sandbox_runtime=sandbox_runtime,
            sandbox_timeout=_float("SANDBOX_TIMEOUT", 300),
            sandbox_memory=_str("SANDBOX_MEMORY", "1g"),
            sandbox_cpus=_str("SANDBOX_CPUS", "1.5"),
            sandbox_network=sandbox_network,
            sandbox_network_allowed_repos=sandbox_network_allowed_repos,
            test_commands=test_commands,
        )
