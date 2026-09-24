# AGENTS.md — Coding agent conventions for this repo

This repository is a Discord-driven AI agent (jarvis) for a home server. It answers questions, reads GitHub repos/issues/PRs/CI, reviews and edits code on `agent/*` branches, runs sandboxed tests, watches repos for changes, files self-review issues, and can update itself via `!update` with automatic rollback. Built on smolagents with a multi-provider free-tier LLM cascade.

## Running tests
`python tests/run_all.py` discovers `test_*.py` in `tests/`, runs each in a subprocess, prints `PASS <description>` for each, and exits 0 on success. Pass a keyword to run a subset: `python tests/run_all.py <keyword>`. No real network — tests use fakes (`tests/fake_gh.py`, `tests/fake_llm.py`, `tests/fake_docker.py`). Git tests use real local repos.

## Linting
`python -m pyflakes *.py tools/*.py` — core source only. Test files are exempt.

## Dependencies
`pip install -r requirements.txt`. Git is required. Docker or Podman is optional (only needed for `repo_test`).

## Code conventions
- Tools are decorated with `@tool` and wrapped with `@_safe` (from `make_safe` in `tools.py`).
- External content (web pages, files, issues, PR text, test output) is labeled with `untrusted()` so the model treats it as data, not instructions.
- Configuration is read only via `Config.from_env()` — never read `os.environ` directly in tools.
- Secrets never appear in `argv` or logs. The `git_auth_env` pattern in `ghclient.py` injects auth for git commands without leaking tokens.
- Agent branches must use the `agent/*` prefix (enforced by `gh.require_agent_branch`).
- PRs the bot opens on its own repo are always **draft** and titled `self: …`.
- `LOCKED_PATHS` and `LOCKED_EXISTING` in `tools/github_write.py` (after Part A) define files the agent may never modify in its own repo.

## Testing conventions
- Tests are standalone scripts, not a framework. They print `PASS <description>` and use assertions that fail hard.
- Prefer real local git repos over mocking for git-related tests.
- Use existing fakes (`fake_gh.py`, `fake_llm.py`, `fake_docker.py`) instead of hitting real network services.

## Explicitly out of bounds
- Weakening `_safe` / the `make_safe` error-handling wrapper.
- Weakening or bypassing the SSRF guard (`_check_public_url`).
- Putting secrets into the sandbox container environment.
- Bypassing `GITHUB_ALLOWED_REPOS` allowlist.
- Auto-merging PRs (no merge tool exists; human approval required).
- Removing the draft-PR default for the bot's own repo.

## Where to look more
- `docs/ARCHITECTURE.md` — system architecture and data flows
- `docs/README.md` — high-level overview (also the root `README.md`)
- `docs/CHECKOUTS_AND_SANDBOX.md` — local git checkouts and sandboxed test runner
- `docs/PROVIDERS.md` — multi-provider LLM cascade behaviour

---

> **Note:** PRs that touch this file are changing the rules the agent operates under — read the diff, not just the description.