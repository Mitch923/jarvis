# Documentation index

- [Multi-provider LLM](PROVIDERS.md) — provider cascade, fallbacks, rate-limit handling
- [Local checkouts & sandboxed tests](CHECKOUTS_AND_SANDBOX.md) — `repo_sync`/`repo_read`/`repo_grep`, `repo_test` in Docker/Podman
- [Watcher & daily digest](WATCHER.md) — background GitHub polling, DM notifications, scheduled digest
- [GitHub webhook receiver](WEBHOOKS.md) — inbound `POST /webhook/github`, HMAC + delivery-id dedupe, Tailscale/tunnel exposure, polling fallback
- [Long-term memory](MEMORY.md) — `data/memory.json`, `!remember`/`!forget`, approval before write
- [Self-improvement](SELF_IMPROVEMENT.md) — friction log, weekly review (`!improve`), `!implement`, protected files, `!update`/`!rollback`
- [Security model](SECURITY.md) — allowlists, branch guards, protected files, sandbox isolation, SSRF, privacy
- [Architecture](ARCHITECTURE.md) — request flow, module graph, shared event/state layer (queue + proactive notify), approval flow, self-improvement loop, guardrails table