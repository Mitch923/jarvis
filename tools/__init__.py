"""Agent tools package: GitHub (read + guarded write), web search/fetch, server status.

Design rules baked in here (so a confused or prompt-injected model can't break them):
  * The agent can NEVER push to a normal branch. Writes only go to branches that start
    with AGENT_BRANCH_PREFIX (default "agent/"), and there is no merge/delete tool.
  * Publishing actions (open PR, post review) - or every write, per APPROVAL_MODE - need a
    button-press from the human in Discord before they run.
  * Fetching web pages refuses private/loopback addresses so the agent can't be tricked
    into poking your router or other LAN devices.
  * Tools return "ERROR: ..." strings instead of raising, so one bad call doesn't waste a step.
"""

from tools.common import (
    RunState,
    LOCKED_PATHS,
    LOCKED_EXISTING,
    Locked,
    make_safe,
    protected_reason,
    UNTRUSTED,
)
from tools.system import system_report
from tools.web import create_web_tools, _check_public_url, fetch_page_text
from tools import web as web_module

from tools.notes import create_notes_tools
from tools.github_read import create_github_read_tools
from tools.github_write import create_github_write_tools
from tools.repo_tools import create_repo_tools

web = web_module


__all__ = [
    "build_tools",
    "RunState",
    "LOCKED_PATHS",
    "LOCKED_EXISTING",
    "Locked",
    "make_safe",
    "protected_reason",
    "UNTRUSTED",
    "system_report",
    "_check_public_url",
    "fetch_page_text",
    "web",
]


def build_tools(
    cfg,
    approve,
    memory=None,
    state=None,
    friction=None,
):
    """Build the complete tool list based on configuration."""
    from ghclient import GitHub

    state = state or RunState()
    tools = []

    # Web + system tools (always available)
    tools += create_web_tools(cfg, state, friction)

    # Memory tools (if memory is configured)
    if memory is not None:
        tools += create_notes_tools(cfg, state, approve, memory, friction)

    # GitHub tools (require token)
    if not cfg.github_token:
        import logging

        log = logging.getLogger("tools")
        log.warning("GITHUB_TOKEN not set - GitHub tools disabled")
        return tools

    gh = GitHub(cfg)

    # GitHub read tools
    tools += create_github_read_tools(cfg, gh, state, friction)

    # Local checkout tools
    if cfg.checkouts_enabled:
        tools += create_repo_tools(cfg, gh, state, friction)

    # GitHub write tools (guarded)
    if cfg.github_write:
        tools += create_github_write_tools(cfg, gh, state, approve, friction)

    return tools