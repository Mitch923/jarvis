# Architecture

## Request flow

```
Discord message
      │
      ▼
Bot.on_message (main.py)
      │
      ▼
AgentRunner.run
      │
      ▼
ResilientModel.generate (llm.py) ──► multi-provider cascade
      │
      ├─► tool calls (tools.py)
      │       ├─► gh_*  ──► ghclient.py ──► GitHub REST
      │       ├─► repo_* ──► checkouts.py ──► local git
      │       ├─► repo_test ──► sandbox.py ──► Docker/Podman
      │       ├─► web_search / web_fetch
      │       ├─► memory_* ──► memory.py
      │       └─► approve() ──► Bot._approve_from_thread
      │                          │
      │                          ▼
      │                   asyncio.run_coroutine_threadsafe
      │                          │
      │                          ▼
      │                   Bot._ask ──► Discord button UI
      │                          │
      │                          ▼
      │                   result back to tool
      │
      ▼
reply to Discord
```

## Module graph

```
main.py
  ├─► llm.py
  ├─► tools.py
  │     ├─► ghclient.py
  │     ├─► checkouts.py
  │     ├─► sandbox.py
  │     ├─► friction.py
  │     ├─► memory.py
  │     └─► (web search/fetch)
  ├─► checkouts.py
  ├─► sandbox.py
  ├─► friction.py
  ├─► memory.py
  ├─► watcher.py
  ├─► updater.py
  ├─► config.py
  └─► ghclient.py
```

**Near-collisions to be aware of:**
- `checkouts.py` vs `tools/repo_tools.py` — the latter is the tool wrapper around the former
- `memory.py` vs `tools/notes.py` — the latter is the tool wrapper around the former

## Approval flow

```
tool.execute()
      │
      ▼
approve(action, args)
      │
      ▼
Bot._approve_from_thread (via asyncio.run_coroutine_threadsafe)
      │
      ▼
Bot._ask ──► Discord button view (✅/✖️)
      │
      ▼
user presses button
      │
      ▼
interaction callback sets future result
      │
      ▼
future resolves → approve() returns Approved/Denied
      │
      ▼
tool continues or aborts
```

## Self-improvement loop

```
friction.py (runtime log) ──► data/friction.jsonl
                                    │
                                    ▼
                    weekly review (SELF_REVIEW_DAY/TIME)
                         │  or  !improve [force]
                         ▼
              read-only agent: repo_read/repo_grep or gh_browse
                         │
                         ▼
              files GitHub issues (≤3, specific, evidence, skip dupes)
                         │
                         ▼
              !implement <issue#> ──► focused task opens draft PR
                         │                     │
                         │                     ▼
                         │           repo_test on agent branch
                         │                     │
                         │                     ▼
                         │           PR body reports test result honestly
                         │                     │
                         ▼                     ▼
              human reviews PR ──► merges (or closes)
                         │
                         ▼
              !update
              ├─► fetch repo
              ├─► refuse if local changes / diverged
              ├─► check GitHub Actions CI passed on target commit
              ├─► fast-forward
              ├─► reinstall deps if requirements.txt changed
              ├─► smoke test: import all modules + parse .env
              │       │
              │       ├─► pass → restart
              │       └─► fail → reset to previous commit, no restart
              │
              ▼
              run.py boot-time: if new code crashes on boot → retry → rollback
```

## Guardrails quick-reference

| Guard | Enforced by | What it prevents |
|-------|-------------|------------------|
| Repo allowlist | `GITHUB_ALLOWED_REPOS` in config, checked in ghclient.py, checkouts.py, watcher.py | Access to repos not explicitly listed |
| Branch guard | `gh_edit_file` / `gh_write_file` only create `agent/*` branches | Direct commits to default/main, arbitrary branch names |
| Protected files | `PROTECTED_PATHS` checked in tools.py and again on full PR diff | Agent editing safety-critical files (tools.py, main.py, config.py, ghclient.py, checkouts.py, sandbox.py, run.py, updater.py, jarvis.service, requirements*.txt, .github/*, tests/*) |
| SSRF guard | `web_fetch` in tools.py refuses private/loopback/link-local IPs | Fetching from internal services, cloud metadata, LAN devices |
| Sandbox isolation | `sandbox.py`: `--rm`, no host env, resource limits, optional `network=none` | Container escaping, secret leakage, resource exhaustion |
| Secret handling | `.env` never forwarded to containers; Discord/GitHub/LLM keys never in sandbox | Credential exposure to untrusted test code |