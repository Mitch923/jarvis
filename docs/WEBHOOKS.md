# GitHub webhook receiver (Feature 1 / PR B)

jarvis has a small [aiohttp](https://aiohttp.readthedocs.io/) HTTP server — `webhooks.py:WebhookServer` — that listens for GitHub webhooks on `POST /webhook/github`. It lives **inside the same asyncio event loop as the Discord bot**: it is built in `Bot.__init__`, started in `Bot.setup_hook()`, and stopped in `Bot.close()`. So it shares the bot's event loop with no extra process, and `!stop` / shutdown handle it cleanly.

## Why webhooks instead of just polling

The [watcher](WATCHER.md) polls GitHub every `WATCH_INTERVAL` seconds (default 300 s), which means a new PR or a newly-failing check can take up to five minutes to surface. Webhooks cut that to seconds: GitHub POSTs the event the moment it happens. They are an **add-on, not a replacement** — polling keeps running as a fallback and a safety net, and the two are deduplicated by delivery id so you never get the same alert twice.

## What it accepts (and ignores)

The receiver reads only scalar fields from untrusted payloads (it never executes anything) and maps a small, allowlisted set of GitHub event names to the shared `RepoEvent` types:

| GitHub event | action/conclusion | `RepoEvent` kind | Notes |
|---|---|---|---|
| `ping` | — | `KIND_PING` | connection test; DMs you **once** ("webhook connected") and never again |
| `pull_request` | `opened`, `reopened` | `KIND_PR_OPENED` | |
| | `ready_for_review` | `KIND_PR_READY` | |
| | `synchronize` | `KIND_PR_UPDATED` | |
| `workflow_run` | `completed`, conclusion `success` | `KIND_CI_PASSED` | |
| | `completed`, conclusion `failure` / `timed_out` | `KIND_CI_FAILED` | |

Everything else — other `pull_request` actions (closed, merged, …), in-flight `workflow_run` states (queued / in_progress / canceled), and any unknown event — is **silently dropped** with a `202 Accepted` and no event. Unrecognized noise stays quiet.

## The request pipeline (fast, no side effects in the hot path)

`_github` returns a status code immediately and never calls an LLM or touches Discord in the request path. `_process` runs synchronously in this order:

1. **Size cap** — body over **1 MiB** is rejected with `413 Request Entity Too Large`. The read is capped at `MAX_BODY + 1` bytes so an oversized payload is never buffered in full.
2. **Signature** — HMAC-SHA256 over the **raw bytes**, compared with `hmac.compare_digest` against the `X-Hub-Signature-256=sha256=<hex>` header. Bad/missing signature → `401 Unauthorized` (this check comes *before* JSON parsing, so a forged body never gets parsed).
3. **JSON parse** — non-JSON payloads are dropped with `202 Accepted`.
4. **Delivery-id dedupe** — `state.seen_deliveries`; a replayed `X-GitHub-Delivery` id returns `200 OK` and produces **no** event (GitHub retries on timeout).
5. **Repo allowlist** — the base repo `owner/name` must be in `GITHUB_ALLOWED_REPOS`; otherwise `202 Accepted`, no event. Same allowlist gate as every other path.
6. **Map to `RepoEvent`** and push onto the event bus; the status is `200 OK` for `ping` and `202 Accepted` for everything else.

Because all of this is local (HMAC, JSON, a state-file write, an in-memory queue), a request completes in well under **100 ms** with zero network round-trips — that is the whole point of returning before doing any work.

Slow work happens downstream in the event consumer (`main._task_for_event`): diagnosis, auto-review, and auto-fix run as PROACTIVE tasks, behind the [priority queue](ARCHITECTURE.md) and the [quiet-hours / per-day notify policy](ARCHITECTURE.md). The one-time "webhook connected" DM is fire-and-forget (`asyncio.create_task`) so it never blocks the HTTP response.

## Enabling it

```env
# .env
WEBHOOK_ENABLED=1        # 0 = default: nothing binds a port
WEBHOOK_BIND=127.0.0.1   # keep this; do not open 0.0.0.0 on a router
WEBHOOK_PORT=8787
WEBHOOK_SECRET=<shared secret>   # REQUIRED when WEBHOOK_ENABLED=1
```

`WEBHOOK_ENABLED=1` without a `WEBHOOK_SECRET` fails startup with a clear `ConfigError`, so you can't silently run an unauthenticated endpoint.

## Wiring GitHub → your server

1. **Expose the port safely.** Do **not** open `WEBHOOK_PORT` on your router/firewall. Keep `WEBHOOK_BIND=127.0.0.1` and front it with one of:
   - **Tailscale** (recommended on a home server): `tailscale serve 8787` on the machine running jarvis, then point the webhook URL at your Tailscale URL. This is the least-exposed option.
   - **A reverse proxy / tunnel** (e.g. Cloudflare Tunnel) terminating on `127.0.0.1:8787`.
2. **Set the shared secret.** Generate a random secret and put it in `WEBHOOK_SECRET`.
3. **Restart** the service (`systemctl restart jarvis`) and confirm `!status` shows the webhook line as **up · listening on 127.0.0.1:8787**.
4. **Configure GitHub.** In each allowed repo: *Settings → Webhooks → Add webhook*.
   - **URL**: your tunnel/serve URL + `/webhook/github` (must end in that path).
   - **Payload**: `POST /webhook/github`.
   - **Secret**: paste your shared secret.
   - **Events**: subscribe to `ping`, `pull_request`, `push` (for `workflow_run`), and any others you want — GitHub only POSTs the events you subscribe to, and unrecognized ones are dropped anyway.
   - Save. GitHub fires a `ping`; you should get a one-time "🔔 Webhook connected" DM.

You only need a webhook on repos in `GITHUB_ALLOWED_REPOS`; anything outside the allowlist is dropped server-side.

## Health check

The server also serves `GET /healthz` (returns `200`). Use it behind your tunnel to confirm the receiver is alive — and, because it's independent of the Discord loop, to tell a healthy HTTP server from one that's up but the bot is hung.

## Status & lifecycle

- **`!status`** reports the receiver: `off` (when `WEBHOOK_ENABLED=0` or not started), `enabled but not listening` (e.g. port in use — the bot keeps running), or `up · listening on <bind>:<port> · last event <N> s ago`.
- **`start()`** never raises on bind failure: if the port is taken it logs a clear message and leaves the receiver off, so the rest of the bot is unaffected. It also returns early and binds nothing when disabled.
- **`stop()`** releases the runner/site and is safe to call when the receiver was never started.
- **Replay-safe across restarts**: `seen_deliveries` survives a restart (file-backed in `data/state.json`), so a delivery that arrives after a reboot but was already seen is still deduped.

## Polling fallback & dedupe

Webhooks are an acceleration layer. If GitHub fails to POST, your tunnel goes down, or you simply leave polling on, the watcher keeps surfacing new PRs and failing checks within `WATCH_INTERVAL`. Dedupe is by `X-GitHub-Delivery` id (the watcher emits events through the same bus/state path), so an event seen via webhook is not reported twice by polling.

## Security notes (short)

- Only `DISCORD_ALLOWED_USER_IDS` get replies; the webhook itself is a **server-side** endpoint that verifies HMAC and the repo allowlist — it sends nothing to anyone on Discord unless a mapped event triggers a PROACTIVE notify (which respects quiet hours and the per-day cap).
- The receiver only *reads* payload scalars; it never executes code or runs an LLM in the HTTP request path.
- Keep `WEBHOOK_BIND=127.0.0.1` and put Tailscale/tunnel in front — do not expose `8787` on a router.

See [Security model](SECURITY.md) for the full threat-model entry on webhooks and [Architecture](ARCHITECTURE.md) for where events flow into the rest of the system.
