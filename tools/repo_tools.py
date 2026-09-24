"""Local checkout tools (repo_sync, repo_read, repo_grep, repo_test)."""
from typing import Optional

from smolagents import tool

from tools.common import RunState, make_safe, UNTRUSTED
from tools.web import clip


def create_repo_tools(cfg, gh, state: RunState, friction=None):
    """Create local checkout tools."""
    from checkouts import Checkouts
    from sandbox import run_tests as run_sandboxed_tests

    limit_chars = cfg.tool_output_chars
    _safe = make_safe(friction.record if friction else None)
    tools = []

    checkouts = Checkouts(cfg)

    @tool
    @_safe
    def repo_sync(repo: str, ref: Optional[str] = None) -> str:
        """Clone or update a local checkout of a repo, so repo_read/repo_grep/repo_test have something to work with. Not required first - they sync automatically - but useful to pre-fetch or to switch which branch is checked out.

        Args:
            repo: 'owner/name' or just 'name'.
            ref: Branch, tag or commit SHA. Defaults to the default branch.
        """
        slug = gh.slug(repo)  # single source of truth for name/owner resolution and the allowlist
        local, sha = checkouts.sync(slug, ref)
        return f"{slug} synced @ {sha}" + (f" (ref: {ref})" if ref else " (default branch)") + f" -> {local}"

    @tool
    @_safe
    def repo_read(repo: str, path: str = "", start_line: int = 1, max_lines: int = 400) -> str:
        """List a directory or read a file (with line numbers) from a LOCAL checkout - faster than gh_browse and not paginated the same way. Syncs the repo first if needed.

        Args:
            repo: 'owner/name' or just 'name'.
            path: Path inside the repo; empty for the root directory.
            start_line: First line to show when reading a file (1-based).
            max_lines: Max lines to show when reading a file.
        """
        slug = gh.slug(repo)
        return untrusted(clip(checkouts.read(slug, path, start_line, max_lines), limit_chars))

    @tool
    @_safe
    def repo_grep(repo: str, pattern: str, path_glob: str = "") -> str:
        """Full-text regex search across a whole local checkout - use this instead of gh_search_code when you need every match, not just GitHub's indexed results. Syncs the repo first if needed.

        Args:
            repo: 'owner/name' or just 'name'.
            pattern: Regular expression to search for (e.g. 'def parse_config\\(').
            path_glob: Optional glob to restrict the search, e.g. '*.py' or 'src/*'.
        """
        slug = gh.slug(repo)
        return untrusted(clip(checkouts.grep(slug, pattern, path_glob), limit_chars))

    tools += [repo_sync, repo_read, repo_grep]

    if cfg.sandbox_runtime:

        @tool
        @_safe
        def repo_test(repo: str, ref: Optional[str] = None) -> str:
            """Run the repo's own test suite in an isolated, network-limited container. Use this to check an edit BEFORE opening a pull request - pass the branch you just committed to as ref. Auto-detects the test command from common project files; falls back to a TEST_COMMANDS entry if configured.

            Args:
                repo: 'owner/name' or just 'name'.
                ref: Branch, tag or commit SHA to test. Defaults to the default branch.
            """
            slug = gh.slug(repo)
            local, sha = checkouts.sync(slug, ref)
            result = run_sandboxed_tests(cfg, slug, local)
            verdict = "TIMED OUT" if result.timed_out else "PASSED" if result.ok else "FAILED"
            return untrusted(clip(f"{slug} @ {sha} - tests {verdict} ({result.seconds:.0f}s, image {result.image})\n$ {result.command}\n{result.output}", limit_chars))

        tools += [repo_test]

    return tools


def untrusted(text: str) -> str:
    """Label text from outside as data."""
    # Note: state.tainted is not set here because repo tools read local checkouts, not external content
    return UNTRUSTED + text