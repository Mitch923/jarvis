# jarvis

A small Discord-driven AI agent for a home server, built on [smolagents](https://github.com/huggingface/smolagents) with free LLM providers.

**Can do:** answer questions with web search · read your GitHub repos, commits, issues, PRs and **CI status** · review PR diffs and post review comments · edit code on `agent/*` branches and open PRs · **check out repos locally for fast reads/search, and run their own tests in a sandbox before opening a PR** · **remember your preferences** · **watch your repos and DM you** about new PRs and failing builds · post a **daily digest** · **review its own pain points weekly and file issues against itself, then draft a PR when you ask it to implement one** · update itself via `!update` with automatic rollback · **fall back across multiple free LLM providers** when one runs dry · report server health.

```
main.py           Discord bot, commands, approvals (buttons), background jobs, agent runner
llm.py            multi-provider LLM cascade: timeouts, retries, 429 handling, provider/model fallback
tools/            agent tools: GitHub, CI, local checkouts, sandboxed tests, web search/fetch, memory
ghclient.py       GitHub REST client (retries, readable errors, CI helper, shared git-auth helper)
checkouts.py      local git clones of allowed repos: fast reads, full-text search (git grep)
sandbox.py        runs a repo's own tests in an ephemeral Docker/Podman container
watcher.py        PR / CI watcher and the daily digest (no LLM)
memory.py         long-term notes (JSON file)
friction.py       runtime pain-point log that feeds the weekly self-review (no LLM)
updater.py        self-update: fast-forward, CI gate, smoke test, automatic rollback
run.py            tiny launcher systemd runs; rolls back if the new code crashes on boot
config.py         environment settings
docs/             deep-dive documentation (see below)
tests/            test suite (python tests/run_all.py); also run by .github/workflows/ci.yml
```

## Setup

### 1. Discord
1. <https://discord.com/developers/applications> → **New Application** → **Bot** → *Reset Token* (copy it).
2. On the same page enable **Message Content Intent** (required, or the bot can't read messages).
3. Invite it: `https://discord.com/oauth2/authorize?client_id=<APPLICATION_ID>&scope=bot&permissions=68608`
4. Discord *Settings → Advanced → Developer Mode*, then right-click yourself → **Copy User ID**.

### 2. At least one LLM provider
`PROVIDERS` (default just `openrouter`) lists which to use, in order — see `.env.example` for every setting and current model-catalog links. All are OpenAI-compatible.

| Provider | Get a key | Notes |
|---|---|---|
| OpenRouter | <https://openrouter.ai/keys> | `MODELS=openrouter/free,auto` covers rotating free models |
| Google AI Studio | <https://aistudio.google.com/apikey> | free tier, no card |
| NVIDIA NIM | <https://build.nvidia.com> | free tier, generous rate limits |
| Ollama (yours, or another PC's) | none | point `OLLAMA_BASE_URL` at it; run with `OLLAMA_HOST=0.0.0.0 ollama serve` |

### 3. GitHub token
Create a **fine-grained** token limited to the repos you want, with: Contents *Read & write*, Pull requests *Read & write*, Issues *Read*, Metadata *Read*. For a hard guarantee, add a branch ruleset on default branches that requires PRs.

### 4. On the server
Debian/Ubuntu-family Linux works fine; 64-bit recommended.

**Deploy with `git clone`, not `scp`**, if you want `!update` and the weekly self-review to work — both need a real checkout of your own fork/repo. Push this code to your own GitHub repo first.

```bash
sudo apt install -y python3-venv git
git clone https://github.com/<you>/<your-fork>.git ~/jarvis
cd ~/jarvis
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env && chmod 600 .env && nano .env   # GITHUB_ALLOWED_REPOS required if you set a token
.venv/bin/python run.py                        # try it in the foreground first (NOT main.py - see below)
```

Optional, for `repo_test` (sandboxed test runs): install Docker or Podman. Podman supports fully rootless use; with Docker, the service user typically needs the `docker` group (equivalent to root on the machine). Skip this and `repo_test` just reports it's unavailable; everything else works normally.

Run as a service (edit the `User`/paths in the unit if you're not `mitch`):
```bash
sudo cp jarvis.service /etc/systemd/system/
sudo systemctl enable --now jarvis
journalctl -u jarvis -f
```

`jarvis.service` runs **`run.py`**, not `main.py` directly. `run.py` checks whether the last `!update` needs to be rolled back before each boot, then execs into `main.py`. Running `main.py` directly still works, it just skips that boot-time safety net.

The systemd unit's `MemoryMax` (3G by default) is sized for an 8 GB machine; turn it back down if your machine has less RAM. It only bounds this process — containers `repo_test` spawns are managed separately by Docker/Podman, with their own `SANDBOX_MEMORY` cap.

## Using it

DM the bot, `@mention` it, or (if `DISCORD_CHANNEL_IDS` is set) just talk in that channel.

| Command | |
|---|---|
| `!status` | Server health, LLM request counts / provider cooldowns, watcher, repo checkouts, config |
| `!stop` | Cancel the running task (also interrupts LLM retry waits) |
| `!reset` | Forget this channel's conversation (long-term notes are kept) |
| `!digest` | Snapshot of every allowlisted repo now |
| `!memory` · `!remember <text>` · `!forget <id>` | View / add / delete long-term notes |
| `!friction` | Recorded pain points from the last 7 days |
| `!feedback <text>` | Tell it what's annoying you; goes into the next self-review |
| `!improve` (`!improve force` to bypass the threshold) | Run the self-review now: files GitHub issues on its own repo, never PRs |
| `!implement <issue#>` | Draft a **PR** implementing one of those issues, for you to review |
| `!update` | Pull the latest commit from your GitHub repo (after checking CI) and restart |
| `!rollback` | Undo the last `!update` |

Examples: *"is CI green on PR 12?"* · *"what's going on across my repos?"* · *"remember that I prefer tabs"* · *"what changed in jarvis this week?"* · *"review PR 12 on site"* · *"fix the typo in README of site, run its tests, and open a PR"* · *"search for smolagents release notes"*

Only one task runs at a time; others queue.

## Learn more

- [Multi-provider LLM](docs/PROVIDERS.md)
- [Local checkouts & sandboxed tests](docs/CHECKOUTS_AND_SANDBOX.md)
- [Watcher & daily digest](docs/WATCHER.md)
- [Long-term memory](docs/MEMORY.md)
- [Self-improvement](docs/SELF_IMPROVEMENT.md)
- [Security model](docs/SECURITY.md)
- [Architecture](docs/ARCHITECTURE.md)

## Safety (summary)

- **Discord allowlist**: only `DISCORD_ALLOWED_USER_IDS` are answered; bot never pings `@everyone`/roles.
- **Repo allowlist**: `GITHUB_ALLOWED_REPOS` mandatory with a token; all tools, watcher, digest, checkouts refuse anything outside it.
- **GitHub writes**: only to `agent/*` branches; no merge/delete tool; PRs opened by agent are drafts; reviews are `COMMENT` only.
- **Local checkouts**: read-only from agent's view — `repo_sync` hard-resets and cleans every use; real changes go through GitHub API.
- **Sandboxed tests**: ephemeral `--rm` container, no host secrets, resource limits, optional `network=none` — see [Security model](docs/SECURITY.md) for full threat model.
- **Self-editing**: restricted by protected-files list; friction log written by runtime (not model); `!update` only fast-forwards to commits with passing CI.
- **Untrusted content**: web pages, files, issues, PR text, test output marked as data; approval buttons are the real backstop.
- **Privacy**: free-tier providers may log/train on prompts; `GITHUB_ALLOWED_REPOS` keeps sensitive repos out of reach; Ollama keeps requests on your network.

See [docs/SECURITY.md](docs/SECURITY.md) for the complete safety model.