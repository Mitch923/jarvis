# Watcher and daily digest

Plain GitHub polling in the background, with **no LLM calls**, so they don't touch your provider budgets. Both cover the repos in `GITHUB_ALLOWED_REPOS`.

- Every `WATCH_INTERVAL` seconds (default 300) it DMs you (or posts to `NOTIFY_CHANNEL_ID`) about **new open PRs** and **newly failing CI checks** on open PRs and default branches, once per failure. The first poll after a fresh start only records a baseline, so you aren't flooded with old items.
- At `DIGEST_TIME` (default 08:00 in `AGENT_TIMEZONE`, so set that) it sends the same snapshot as `!digest`: default-branch CI, commits in 24 h, open PRs with CI state, open issues.
- Cost on GitHub's side is roughly 10 requests per repo per poll (limit 5,000/hour). State lives in `data/watch.json` and is only written when something changes.

## Webhook acceleration (optional)

The [webhook receiver](WEBHOOKS.md) is a faster, event-driven alternative to polling — GitHub POSTs new PRs / failing-check events to `POST /webhook/github` in seconds instead of waiting up to `WATCH_INTERVAL` (default 300 s) of polling. The two are **independent but deduped**:

- Webhooks and the watcher emit the *same* `RepoEvent` types through the same shared bus/state path.
- Dedupe is by the GitHub `X-GitHub-Delivery` id (`state.seen_deliveries`, file-backed, survives restarts). A delivery that arrives via webhook is already marked seen, so a later poll of the same PR/failure won't re-alert you — and vice versa.
- The watcher keeps running as a fallback even when `WEBHOOK_ENABLED=1` (it also covers anything webhooks don't — and it costs no LLM). Leave it on; it's the safety net.

If you leave `WEBHOOK_ENABLED=0` (the default), webhooks are off entirely and everything goes through polling, exactly as it always has.