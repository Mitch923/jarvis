# jarvis

A small Discord-driven AI agent for a Raspberry Pi 3B+, built on [smolagents](https://github.com/huggingface/smolagents) with OpenRouter free models.

**Can do:** answer questions with web search · read your GitHub repos, commits, issues, PRs and **CI status** · review PR diffs and post review comments · edit code on `agent/*` branches and open PRs · **remember your preferences** · **watch your repos and DM you** about new PRs and failing builds · post a **daily digest** · **review its own pain points weekly and file issues against itself, then draft a PR when you ask it to implement one** · update itself via `!update` with automatic rollback · report Pi health.

```
main.py     Discord bot, commands, approvals (buttons), background jobs, agent runner
llm.py      OpenRouter model: timeouts, retries, 429 handling, model fallback, free-model discovery
tools.py    agent tools: GitHub, CI, web search / fetch, memory, self-review issues, Pi status
ghclient.py GitHub REST client (retries, readable errors, CI helper)
watcher.py  PR / CI watcher and the daily digest (no LLM)
memory.py   long-term notes (JSON file)
friction.py runtime pain-point log that feeds the weekly self-review (no LLM)
updater.py  self-update: fast-forward, CI gate, smoke test, automatic rollback
run.py      tiny launcher systemd runs; rolls back if the new code crashes on boot
config.py   environment settings
tests/      test suite (python tests/run_all.py); also run by .github/workflows/ci.yml
```

## Setup

### 1. Discord
1. <https://discord.com/developers/applications> → **New Application** → **Bot** → *Reset Token* (copy it).
2. On the same page enable **Message Content Intent** (required, or the bot can't read messages).
3. Invite it: `https://discord.com/oauth2/authorize?client_id=<APPLICATION_ID>&scope=bot&permissions=68608`
4. Discord *Settings → Advanced → Developer Mode*, then right-click yourself → **Copy User ID**.

### 2. GitHub token
Create a **fine-grained** token (Settings → Developer settings) limited to the repos you want, with: Contents *Read & write*, Pull requests *Read & write*, Issues *Read*, Metadata *Read*.
For a hard guarantee, also add a branch ruleset on your default branches that requires PRs. The code already refuses writes outside `agent/*`, but server-side rules can't be bypassed by a bug.

### 3. On the Pi
Use **Raspberry Pi OS Lite 64-bit (Bookworm or newer)**: the 32-bit OS often lacks prebuilt wheels for `ddgs`' dependencies.

**Deploy with `git clone`, not `scp`**, if you want `!update` and the weekly self-review to work - both need a real checkout of your own fork/repo so `git fetch`/`git log` and `SELF_REPO` auto-detection have something to work with. Push this code to your own GitHub repo first.

```bash
sudo apt install -y python3-venv git
git clone https://github.com/<you>/<your-fork>.git ~/jarvis
cd ~/jarvis
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt   # takes a few minutes on a 3B+
cp .env.example .env && chmod 600 .env && nano .env   # GITHUB_ALLOWED_REPOS is required if you set a token
.venv/bin/python run.py                        # try it in the foreground first (NOT main.py - see below)
```

Run as a service (edit the `User`/paths in the unit if you're not `pi`):
```bash
sudo cp jarvis.service /etc/systemd/system/
sudo systemctl enable --now jarvis
journalctl -u jarvis -f
```

`jarvis.service` and the instructions above run **`run.py`**, not `main.py` directly. `run.py` is a tiny wrapper: it checks whether the last `!update` needs to be rolled back before each boot, then execs into `main.py`. Running `main.py` directly still works, it just skips that boot-time safety net.

Idle memory is roughly 100-150 MB; the unit caps it at 600 MB so a runaway restarts instead of freezing the Pi.

## Using it
DM the bot, `@mention` it, or (if `DISCORD_CHANNEL_IDS` is set) just talk in that channel.

| Command | |
|---|---|
| `!status` | Pi health, LLM request count / model cooldowns, watcher, config |
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

Examples: *"is CI green on PR 12?"* · *"what's going on across my repos?"* · *"remember that I prefer tabs"* · *"what changed in jarvis this week?"* · *"review PR 12 on site"* · *"fix the typo in README of site and open a PR"* · *"search for smolagents release notes"*

Only one task runs at a time; others queue.

### Watcher and daily digest
Plain GitHub polling in the background, with **no LLM calls**, so they don't touch your free-tier request budget. Both cover the repos in `GITHUB_ALLOWED_REPOS`.
- Every `WATCH_INTERVAL` seconds (default 300) it DMs you (or posts to `NOTIFY_CHANNEL_ID`) about **new open PRs** and **newly failing CI checks** on open PRs and default branches, once per failure. The first poll after a fresh start only records a baseline, so you aren't flooded with old items.
- At `DIGEST_TIME` (default 08:00 in `AGENT_TIMEZONE`, so set that) it sends the same snapshot as `!digest`: default-branch CI, commits in 24 h, open PRs with CI state, open issues.
- Cost on GitHub's side is roughly 10 requests per repo per poll (limit 5,000/hour). State lives in `data/watch.json` and is only written when something changes.

### Long-term memory
Short notes in `data/memory.json` (plain JSON, max `MEMORY_MAX_NOTES`, 300 chars each) are added to the agent's prompt in every chat. Save with `!remember`, or just tell the agent "remember that…". Secret-looking strings (API keys) are refused. **If a task has already read web or GitHub content, the agent must ask you (✅/✖️) before saving or deleting a note**, so a malicious page can't plant a permanent instruction.

### Self-improvement
Requires `SELF_REPO` set (or auto-detected from `git clone`), that repo listed in `GITHUB_ALLOWED_REPOS`, and `GITHUB_WRITE=1`. `!status` shows whether it's active and why not if it isn't.

- **Friction log** (`friction.py`, `data/friction.jsonl`): the *runtime* - not the model - records tool errors, LLM fallbacks/timeouts, step-limit hits, `!stop`, and denied actions. This is deliberate: letting the model self-report "pain points" is unreliable and a malicious web page could plant fake ones. Your own `!feedback` is recorded the same way and carries the most weight.
- **Weekly self-review** (`SELF_REVIEW_DAY`/`TIME`, default Sunday 09:00): summarizes that log and, if there's enough to act on (`SELF_REVIEW_MIN_EVENTS`, or any `!feedback` at all), runs a read-only agent task that skims the relevant code and **files GitHub issues** - specific, with evidence, at most 3 per run, skipping near-duplicates. It never opens PRs or edits files. Run it on demand with `!improve` (`!improve force` to ignore the threshold).
- **`!implement <n>`**: a separate, focused task reads issue `n` and opens a **draft** pull request implementing it, adding a new test file if behaviour changed. You review and merge (or close) it like any other PR - nothing here merges automatically.
- **Protected files**: in its own repo, the agent can never edit `tools.py`, `main.py`, `config.py`, `ghclient.py`, `run.py`, `updater.py`, `jarvis.service`, `requirements*.txt`, `.github/*`, or existing files under `tests/` (new test files are fine) - the files that hold the safety logic, the launcher, and the CI that judges its own PRs. `PROTECTED_PATHS` adds more. This is checked on every edit and again on the full PR diff, so it can't be routed around through another tool or a branch it didn't create through `gh_edit_file`. A human makes those changes.
- **`!update`**: fetches your repo, refuses if there are local changes or the history has diverged, **checks that GitHub Actions CI passed** on the target commit (skips the update if it's failing or still running), then fast-forwards, reinstalls dependencies if `requirements.txt` changed, and runs a smoke test (`import` every module + parse your real `.env`) **before** restarting - if that fails, it resets to the previous commit and never restarts. You approve before anything happens. If the new code passes the smoke test but then crashes at boot, `run.py` retries a few times and then rolls back automatically. `!rollback` reverts the last update by hand at any time.
- Turn it off entirely with `SELF_REVIEW_DAY=off` (review) or by leaving `SELF_REPO` unset / out of `GITHUB_ALLOWED_REPOS` (review, `!implement`, and `!update`'s CI-aware fast-forwarding all need it - `!update` still works generically on any git checkout without it, just without the "is this my own repo" framing).

## How the free tier is handled
- Every HTTP request has a hard timeout; one LLM call has an overall deadline; a whole task has `RUN_TIMEOUT`.
- **429 per-minute** → waits (honouring `Retry-After`) and retries. **429 per-day** → stops immediately, tells you when it resets, and doesn't spend further requests until then.
- **5xx / timeout / dropped connection / empty or malformed reply** → retry, then next model. **404 / 400 / 402** (model retired, context too small, needs credits) → that model is skipped for a while.
- `MODELS=openrouter/free,auto`: after the router, fall back to whichever free tool-capable models exist *right now* (list refreshed every 6 h). The Discord footer shows which model actually answered.
- Models that answer in prose instead of calling a tool have their text used as the final answer, instead of wasting steps on parse errors.
- Every agent step is one request against a small daily budget, hence `MAX_STEPS=8` and the ask-once-then-answer prompting.

## Safety model
- **Discord allowlist**: only `DISCORD_ALLOWED_USER_IDS` are answered; everyone else is silently ignored. The bot can never ping `@everyone`/roles; alerts can ping only you.
- **Repo allowlist**: with a GitHub token set, `GITHUB_ALLOWED_REPOS` is mandatory. Every tool, the watcher and the digest refuse anything outside it (code search included).
- **GitHub writes** only go to `agent/*` branches; there is no merge or delete tool. Opening a PR / posting a review needs you to press ✅ (see `APPROVAL_MODE`). Reviews are `COMMENT` only, never approve. PRs the agent opens on its own repo are always drafts.
- **Self-editing** is restricted to whatever isn't in the protected-files list (above), and the friction log that drives it is written by the runtime, not the model, specifically so a poisoned web page or issue can't talk the agent into "noticing" a fake problem. `!update` only ever fast-forwards to a commit that's already on GitHub with passing CI - it can't run arbitrary code you haven't already merged.
- **Untrusted content**: web pages, files, issues and PR text are marked as data and the system prompt tells the model not to obey them. That reduces, but cannot eliminate, prompt-injection risk; the approval buttons are the real backstop.
- **Web fetching** refuses private, loopback and link-local addresses (your router, other LAN devices, cloud metadata). It resolves DNS once per hop, so it is not a defence against a determined DNS-rebinding attacker.
- **Privacy**: free OpenRouter models may log or train on prompts. Anything the agent reads from a private repo is sent to them. The mandatory `GITHUB_ALLOWED_REPOS` allowlist is how you keep sensitive repos out of reach: only list repos you're comfortable sending to them.
- `AGENT_TYPE=code` makes the model write Python that runs *on the Pi* in smolagents' restricted interpreter, which is not a security boundary. Leave it on `tool` unless you understand that.
