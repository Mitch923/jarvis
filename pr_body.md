## Summary

Fixes four issues:

1. **Text truncation in Discord messages** - The `_ask()` approval method now uses `chunk_message()` to properly split long messages instead of silently truncating at 1900 characters.

2. **PR commenting tool approval** - `gh_review_pr` no longer requires approval in `approval_mode=publish` since it only posts COMMENT events (never APPROVE/REQUEST_CHANGES), making it a low-impact action.

3. **Tool feedback** - Root cause was the unnecessary approval on `gh_review_pr` causing DENIED/retry loops. Now fixed. Verified approval flow works correctly for other tools (e.g., `gh_open_pr` still requires approval).

4. **Automated PR tracking** - Watcher now queues newly detected PRs for automated review. Added three new tools:
   - `gh_pr_queue_status` - Show queue statistics
   - `gh_next_pr_for_review` - Get next pending PR (marks as "reviewing")
   - `gh_mark_pr_reviewed` - Mark PR as reviewed/skipped after posting review

## Testing
- All 234 tests pass (16/16 files green)
- Linting clean (`pyflakes *.py tools/*.py`)

## Files Changed
- `main.py` - Fix approval message chunking
- `tools/github_write.py` - Remove approval from gh_review_pr
- `tools/github_read.py` - Add PR queue tools
- `watcher.py` - Add PR queue mechanism
- `tests/test_tools.py` - Update test for new gh_review_pr behavior