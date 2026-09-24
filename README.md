# jarvis

A small Discord-driven AI agent for a home server, built on [smolagents](https://github.com/huggingface/smolagents) with free LLM providers.

**Can do:** answer questions with web search · read your GitHub repos, commits, issues, PRs and **CI status** · review PR diffs and post review comments · edit code on `agent/*` branches and open PRs · **check out repos locally for fast reads/search, and run their own tests in a sandbox before opening a PR** · **remember your preferences** · **watch your repos and DM you** about new PRs and failing builds · post a **daily digest** · **review its own pain points weekly and file issues against itself, then draft a PR when you ask it to implement one** · update itself via `!update` with automatic rollback · **fall back across multiple free LLM providers** when one runs dry · report server health.

```
main.py      Discord bot, commands, approvals (buttons), background jobs, agent runner
llm.py       multi-provider LLM cascade: timeouts, retries, 429 handling, provider/model fallback
tools.py     agent tools: GitHub, CI, local checkouts, sandboxed tests, web search/fetch, memory
ghclient.py  GitHub REST client (retries, readable errors, CI helper, shared git-auth helper)
checkouts.py local git clones of allowed repos: fast reads, full-text search (git grep)
sandbox.py   runs a repo's own tests in an ephemeral Docker/Podman container
watcher.py   PR / CI watcher and the daily digest (no LLM)
memory.py    long-term notes (JSON file)
friction.py  runtime pain-point log that feeds the weekly self-review (no LLM)
updater.py   self-update: fast-forward, CI gate, smoke test, automatic rollback
run.py       tiny launcher systemd runs; rolls back if the new code crashes on boot
config.py    environment settings
tests/       test suite (python tests/run_all.py); also run by .github/workflows/ci.yml
```

## Setup

### 1. Discord
1. <https://discord.com/developers/applications> → **New Application** → **Bot** → *Reset Token* (copy it).
2. On the same page enable **Message Content Intent** (required, or the bot can't read messages).
3. Invite it: `https://discord.com/oauth2/authorize?client_id=<APPLICATION_ID>&scope=bot&permissions=68608`
4. Discord *Settings → Advanced → Developer Mode*, then right-click yourself → **Copy User ID**.

### 2. At least one LLM provider
`PROVIDERS` (default just `openrouter`) lists which of these to use, in order - see `.env.example` for every setting and current model-catalog links. All are OpenAI-compatible, so adding another later is a small change.

| Provider | Get a key | Notes |
|---|---|---|
| OpenRouter | <https://openrouter.ai/keys> | `MODELS=openrouter/free,auto` covers a rotating set of free models |
| Google AI Studio | <https://aistudio.google.com/apikey> | free tier, no card |
| NVIDIA NIM | <https://build.nvidia.com> | free tier, generous rate limits |
| Ollama (yours, or another PC's) | none | point `OLLAMA_BASE_URL` at it; run it with `OLLAMA_HOST=0.0.0.0 ollama serve` so it's reachable over the LAN |

A daily quota hit on one provider falls through to the next immediately - it doesn't wait or block the others.

### 3. GitHub token
Create a **fine-grained** token (Settings → Developer settings) limited to the repos you want, with: Contents *Read & write*, Pull requests *Read & write*, Issues *Read*, Metadata *Read*.
For a hard guarantee, also add a branch ruleset on your default branches that requires PRs. The code already refuses writes outside `agent/*`, but server-side rules can't be bypassed by a bug.

### 4. On the server
Debian/Ubuntu-family Linux works fine; 64-bit is worth it if your hardware supports it, since some dependencies lack prebuilt 32-bit wheels.

**Deploy with `git clone`, not `scp`**, if you want `!update` and the weekly self-review to work - both need a real checkout of your own fork/repo so `git fetch`/`git log` and `SELF_REPO` auto-detection have something to work with. Push this code to your own GitHub repo first.

```bash
sudo apt install -y python3-venv git
git clone https://github.com/<you>/<your-fork>.git ~/jarvis
cd ~/jarvis
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env && chmod 600 .env && nano .env   # GITHUB_ALLOWED_REPOS is required if you set a token
.venv/bin/python run.py                        # try it in the foreground first (NOT main.py - see below)
```

Optional, for `repo_test` (sandboxed test runs - see below): install Docker or Podman. Podman supports fully rootless use; with Docker, the service user typically needs the `docker` group, which is worth knowing is equivalent to root on the machine - see the note in `jarvis.service`. Skip this and `repo_test` just reports it's unavailable; everything else works normally.

Run as a service (edit the `User`/paths in the unit if you're not `mitch`):
```bash
sudo cp jarvis.service /etc/systemd/system/
sudo systemctl enable --now jarvis
journalctl -u jarvis -f
```

`jarvis.service` and the instructions above run **`run.py`**, not `main.py` directly. `run.py` is a tiny wrapper: it checks whether the last `!update` needs to be rolled back before each boot, then execs into `main.py`. Running `main.py` directly still works, it just skips that boot-time safety net.

The systemd unit's `MemoryMax` (3G by default) is sized for an 8 GB machine; turn it back down if your machine has less RAM. It only bounds this process - containers `repo_test` spawns are managed separately by Docker/Podman, with their own `SANDBOX_MEMORY` cap.

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

### Multi-provider LLM
`PROVIDERS` is tried in order (default just `openrouter`); within a provider, its own `*_MODELS` list is tried in order. `!status` shows every configured provider, which model answered last, and any cooldowns or day-caps, e.g. `openrouter — ⛔ day cap, resets in 6h 12m`. Free-tier behavior, all handled automatically:
- Every HTTP request has a hard timeout; one LLM call has an overall deadline (`LLM_CALL_DEADLINE`); a whole task has `RUN_TIMEOUT`.
- **429 per-minute** → waits (honouring `Retry-After`) and retries. **429 per-day** → that provider is skipped for the rest of the day and the next provider (if any) is tried immediately - it does not wait or block the others. Only once *every* configured provider is day-capped does the bot tell you and stop.
- **5xx / timeout / dropped connection / empty or malformed reply** → retry, then next model, then next provider. **404 / 400 / 402** (model retired, context too small, needs credits) → that model is skipped for a while.
- `MODELS=openrouter/free,auto` (OpenRouter only): after the router, fall back to whichever free tool-capable models exist *right now* (list refreshed every 6 h).
- Models that answer in prose instead of calling a tool have their text used as the final answer, instead of wasting steps on parse errors.
- Every agent step is one request against a provider's own daily budget, hence `MAX_STEPS=12` and the ask-once-then-answer prompting - the more providers you configure, the more headroom you have before hitting a wall.

### Local checkouts and sandboxed tests
`CHECKOUTS_ENABLED=1` (default, needs a GitHub token) adds `repo_sync`/`repo_read`/`repo_grep`: real `git clone`s of allowed repos under `CHECKOUT_DIR`, giving fast local file reads and full-text search (`git grep` - no extra dependency like ripgrep). Every checkout is a **disposable mirror** - hard-reset and cleaned on every sync, so it can never drift or carry local edits. This is *not* a second way to write code: all commits still go through `gh_edit_file`/`gh_write_file` (the GitHub API), with the same branch guard and protected-file checks as always. `CHECKOUT_MAX_REPOS` caps how many repos get cloned at once.

`repo_test` (needs Docker or Podman - `SANDBOX_RUNTIME`) runs a repo's own test suite in a fresh, resource-limited (`SANDBOX_MEMORY`/`SANDBOX_CPUS`/`SANDBOX_TIMEOUT`), `--rm` container before you commit to trusting a change - the agent is told to run it before opening a pull request, and `!implement` does this automatically. It auto-detects the test command from common project files (this repo's own `tests/run_all.py` convention, `requirements.txt`, `package.json`, `go.mod`, `Cargo.toml`); override per repo with `TEST_COMMANDS` if that doesn't fit. None of the bot's own secrets (GitHub token, LLM keys, Discord token) ever reach the container - Docker doesn't forward the host environment by default, and this code never adds one. Network access is on by default (`SANDBOX_NETWORK=bridge`, since most projects need it to install dependencies); set it to `none` to fully air-gap a repo with vendored or no dependencies. See `sandbox.py`'s docstring for the full, honestly-stated security posture (in short: strong isolation of the host and your credentials; the container can write to its own throwaway checkout, which is expected and cleaned up on the next sync).

### Watcher and daily digest
Plain GitHub polling in the background, with **no LLM calls**, so they don't touch your provider budgets. Both cover the repos in `GITHUB_ALLOWED_REPOS`.
- Every `WATCH_INTERVAL` seconds (default 300) it DMs you (or posts to `NOTIFY_CHANNEL_ID`) about **new open PRs** and **newly failing CI checks** on open PRs and default branches, once per failure. The first poll after a fresh start only records a baseline, so you aren't flooded with old items.
- At `DIGEST_TIME` (default 08:00 in `AGENT_TIMEZONE`, so set that) it sends the same snapshot as `!digest`: default-branch CI, commits in 24 h, open PRs with CI state, open issues.
- Cost on GitHub's side is roughly 10 requests per repo per poll (limit 5,000/hour). State lives in `data/watch.json` and is only written when something changes.

### Long-term memory
Short notes in `data/memory.json` (plain JSON, max `MEMORY_MAX_NOTES`, 300 chars each) are added to the agent's prompt in every chat. Save with `!remember`, or just tell the agent "remember that…". Secret-looking strings (API keys) are refused. **If a task has already read web or GitHub content, the agent must ask you (✅/✖️) before saving or deleting a note**, so a malicious page can't plant a permanent instruction.

### Self-improvement
Requires `SELF_REPO` set (or auto-detected from `git clone`), that repo listed in `GITHUB_ALLOWED_REPOS`, and `GITHUB_WRITE=1`. `!status` shows whether it's active and why not if it isn't.

- **Friction log** (`friction.py`, `data/friction.jsonl`): the *runtime* - not the model - records tool errors, LLM provider/model fallbacks, timeouts, step-limit hits, `!stop`, and denied actions. This is deliberate: letting the model self-report "pain points" is unreliable and a malicious web page could plant fake ones. Your own `!feedback` is recorded the same way and carries the most weight.
- **Weekly self-review** (`SELF_REVIEW_DAY`/`TIME`, default Sunday 09:00): summarizes that log and, if there's enough to act on (`SELF_REVIEW_MIN_EVENTS`, or any `!feedback` at all), runs a read-only agent task that skims the relevant code (`repo_read`/`repo_grep` if checkouts are enabled, else `gh_browse`) and **files GitHub issues** - specific, with evidence, at most 3 per run, skipping near-duplicates. It never opens PRs or edits files. Run it on demand with `!improve` (`!improve force` to ignore the threshold).
- **`!implement <n>`**: a separate, focused task reads issue `n` and opens a **draft** pull request implementing it, running `repo_test` on its own branch first if available (a failing result doesn't block the PR - it just says so honestly in the PR body, since you decide) and adding a new test file if behaviour changed. You review and merge (or close) it like any other PR - nothing here merges automatically.
- **Protected files**: in its own repo, the agent can never edit `tools.py`, `main.py`, `config.py`, `ghclient.py`, `checkouts.py`, `sandbox.py`, `run.py`, `updater.py`, `jarvis.service`, `requirements*.txt`, `.github/*`, or existing files under `tests/` (new test files are fine) - the files that hold the safety logic, the launcher, and the CI that judges its own PRs. `PROTECTED_PATHS` adds more. This is checked on every edit and again on the full PR diff, so it can't be routed around through another tool or a branch it didn't create through `gh_edit_file`. A human makes those changes.
- **`!update`**: fetches your repo, refuses if there are local changes or the history has diverged, **checks that GitHub Actions CI passed** on the target commit (skips the update if it's failing or still running), then fast-forwards, reinstalls dependencies if `requirements.txt` changed, and runs a smoke test (`import` every module + parse your real `.env`) **before** restarting - if that fails, it resets to the previous commit and never restarts. You approve before anything happens. If the new code passes the smoke test but then crashes at boot, `run.py` retries a few times and then rolls back automatically. `!rollback` reverts the last update by hand at any time.
- Turn it off entirely with `SELF_REVIEW_DAY=off` (review) or by leaving `SELF_REPO` unset / out of `GITHUB_ALLOWED_REPOS` (review, `!implement`, and `!update`'s CI-aware fast-forwarding all need it - `!update` still works generically on any git checkout without it, just without the "is this my own repo" framing).

## Safety model
- **Discord allowlist**: only `DISCORD_ALLOWED_USER_IDS` are answered; everyone else is silently ignored. The bot can never ping `@everyone`/roles; alerts can ping only you.
- **Repo allowlist**: with a GitHub token set, `GITHUB_ALLOWED_REPOS` is mandatory. Every tool, the watcher, the digest, and local checkouts refuse anything outside it (code search included).
- **GitHub writes** only go to `agent/*` branches; there is no merge or delete tool. Opening a PR / posting a review needs you to press ✅ (see `APPROVAL_MODE`). Reviews are `COMMENT` only, never approve. PRs the agent opens on its own repo are always drafts.
- **Local checkouts are read-only from the agent's point of view**: `repo_sync` hard-resets and cleans on every use, so nothing written there (by the agent, or by a container's install/test step) can persist or drift. All real changes still go through the GitHub API write path above.
- **Sandboxed tests** run in an ephemeral, `--rm` container with no host secrets, resource limits, and (optionally) no network - see the "Local checkouts and sandboxed tests" section above for the full, plainly-stated threat model. Note that with Docker (not Podman), the service account typically needs `docker` group membership, which is effectively root on the host.
- **Self-editing** is restricted to whatever isn't in the protected-files list (above), and the friction log that drives it is written by the runtime, not the model, specifically so a poisoned web page or issue can't talk the agent into "noticing" a fake problem. `!update` only ever fast-forwards to a commit that's already on GitHub with passing CI - it can't run arbitrary code you haven't already merged.
- **Untrusted content**: web pages, files, issues, PR text and test output are marked as data and the system prompt tells the model not to obey them. That reduces, but cannot eliminate, prompt-injection risk; the approval buttons are the real backstop.
- **Web fetching** refuses private, loopback and link-local addresses (your router, other LAN devices, cloud metadata). It resolves DNS once per hop, so it is not a defence against a determined DNS-rebinding attacker.
- **Privacy**: free-tier LLM providers may log or train on prompts. Anything the agent reads from a private repo is sent to whichever provider handles that request. The mandatory `GITHUB_ALLOWED_REPOS` allowlist is how you keep sensitive repos out of reach: only list repos you're comfortable sending to them. If you add Ollama pointed at your own hardware, that provider's requests never leave your network.
- `AGENT_TYPE=code` makes the model write Python that runs *on the server* in smolagents' restricted interpreter, which is not a security boundary. Leave it on `tool` unless you understand that.

