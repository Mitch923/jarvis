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
  ├─► events.py
  ├─► state.py
  ├─► config.py
  └─► ghclient.py
```

**Near-collisions to be aware of:**
- `checkouts.py` vs `tools/repo_tools.py` — the latter is the tool wrapper around the former
- `memory.py` vs `tools/notes.py` — the latter is the tool wrapper around the former

## Shared event/state layer

One process drives everything: a single consumer (`events.py:TaskQueue._task_loop`)
runs all queued tasks one at a time, and `state.py` is a file-backed JSON store
(one section per feature, pruned, survives restarts).

**`TaskQueue` (single-consumer, priority).** Every task carries a `priority`
(`USER` = 0, `PROACTIVE` = 1) and a monotonically increasing `seq`. The consumer
always pops the highest-priority, oldest-`seq` task first, so a user's live request
preempts the background work queued behind it; within a priority level it is FIFO.
`is_busy()` reports whether a task is running; `queued()` and `proactive_pending()`
report backlog; `mark_done()` / `close()` bookend a task. USER tasks reply to their
own message; PROACTIVE tasks go through the notify policy below.

**PROACTIVE queue cap** (`PROACTIVE_QUEUE_MAX`, default 10). When the background
queue already holds that many PROACTIVE tasks, a newly enqueued one evicts the
*oldest* PROACTIVE task (by `seq`) and records a `proactive_queue_full` friction
event. USER tasks are never evicted this way — only the background layer sheds load.

**Proactive notify policy** (quiet hours + per-day cap, in `main.py`).
`_notify_proactive` is the only path for background alerts (weekly self-review,
daily digest, fix attempts, queue-full). Two gates, checked in order:
- *Per-day cap* (`PROACTIVE_MAX_PER_DAY`; 0 = no cap) — counted against
  `state.proactive_log`. Over the cap, the alert is dropped (not sent) and recorded
  as a `proactive_cap` friction event.
- *Quiet hours* (`QUIET_HOURS`, a 24h window that may cross midnight, e.g.
  `22:00-04:00`). Inside the window, alerts buffer into `_pending_alerts` and the
  `_alert_flush_loop` job joins them and sends **one** combined message when the
  window reopens, so the user gets a single digest rather than a 3 a.m. ping.

User-initiated task results bypass both gates — a user's `!improve` or task
answer always delivers.

**`!status` queue line.** Now reports
`**Queue**: **{busy/idle}** · {N} queued · {M} proactive pending · {K} proactive
left today`, so background congestion is visible at a glance.

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