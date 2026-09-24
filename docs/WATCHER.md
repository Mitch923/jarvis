# Watcher and daily digest

Plain GitHub polling in the background, with **no LLM calls**, so they don't touch your provider budgets. Both cover the repos in `GITHUB_ALLOWED_REPOS`.

- Every `WATCH_INTERVAL` seconds (default 300) it DMs you (or posts to `NOTIFY_CHANNEL_ID`) about **new open PRs** and **newly failing CI checks** on open PRs and default branches, once per failure. The first poll after a fresh start only records a baseline, so you aren't flooded with old items.
- At `DIGEST_TIME` (default 08:00 in `AGENT_TIMEZONE`, so set that) it sends the same snapshot as `!digest`: default-branch CI, commits in 24 h, open PRs with CI state, open issues.
- Cost on GitHub's side is roughly 10 requests per repo per poll (limit 5,000/hour). State lives in `data/watch.json` and is only written when something changes.