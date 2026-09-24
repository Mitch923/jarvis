# Documentation index

- [Multi-provider LLM](PROVIDERS.md) — provider cascade, fallbacks, rate-limit handling
- [Local checkouts & sandboxed tests](CHECKOUTS_AND_SANDBOX.md) — `repo_sync`/`repo_read`/`repo_grep`, `repo_test` in Docker/Podman
- [Watcher & daily digest](WATCHER.md) — background GitHub polling, DM notifications, scheduled digest
- [Long-term memory](MEMORY.md) — `data/memory.json`, `!remember`/`!forget`, approval before write
- [Self-improvement](SELF_IMPROVEMENT.md) — friction log, weekly review (`!improve`), `!implement`, protected files, `!update`/`!rollback`
- [Security model](SECURITY.md) — allowlists, branch guards, protected files, sandbox isolation, SSRF, privacy
- [Architecture](ARCHITECTURE.md) — request flow, module graph, approval flow, self-improvement loop, guardrails table