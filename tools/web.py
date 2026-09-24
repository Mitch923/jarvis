"""Web safety utilities and tools."""
import re
import socket
import ipaddress
from urllib.parse import urljoin, urlparse

import requests
from smolagents import tool

from tools.common import RunState, make_safe, UNTRUSTED


_STRIP_BLOCKS = re.compile(r"(?is)<(script|style|noscript|svg|nav|footer|iframe)\b.*?</\1>")


def _check_public_url(url: str) -> None:
    """Refuse anything that isn't http(s) to a public IP (blocks LAN / localhost / metadata IPs)."""
    parts = urlparse(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("only http(s) URLs are allowed")
    try:
        infos = socket.getaddrinfo(parts.hostname, parts.port or (443 if parts.scheme == "https" else 80))
    except socket.gaierror as e:
        raise ValueError(f"cannot resolve host {parts.hostname}") from e
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global:
            raise ValueError(f"refusing to fetch non-public address ({parts.hostname} -> {ip})")


def fetch_page_text(url: str, limit: int) -> str:
    for _ in range(5):  # follow redirects manually so every hop is checked
        _check_public_url(url)
        r = requests.get(
            url,
            timeout=(5, 15),
            stream=True,
            allow_redirects=False,
            headers={"User-Agent": "Mozilla/5.0 (compatible; jarvis)"},
        )
        if 300 <= r.status_code < 400 and r.headers.get("location"):
            url = urljoin(url, r.headers["location"])
            r.close()
            continue
        break
    else:
        raise ValueError("too many redirects")

    try:
        if r.status_code >= 400:
            raise ValueError(f"HTTP {r.status_code}")
        ctype = r.headers.get("content-type", "").lower()
        if not ctype.startswith(("text/", "application/xhtml", "application/json", "application/xml")):
            raise ValueError(f"unsupported content type: {ctype or 'unknown'}")
        raw = r.raw.read(1_000_000, decode_content=True)  # cap download at ~1 MB
    finally:
        r.close()

    text = raw.decode(r.encoding or "utf-8", errors="replace")
    if "html" in ctype:
        from markdownify import markdownify  # imported lazily: only needed here

        text = markdownify(_STRIP_BLOCKS.sub("", text), heading_style="ATX")
    return clip(re.sub(r"\n{3,}", "\n\n", text).strip(), limit)


def _format_files(files: list[dict], per_file: int = 2000) -> str:
    out = []
    for f in files:
        head = f"### {f['filename']} [{f['status']}] +{f.get('additions', 0)} -{f.get('deletions', 0)}"
        patch = f.get("patch")
        out.append(head + ("\n" + clip(patch, per_file) if patch else "\n(no textual diff)"))
    return "\n\n".join(out)


def _unified(old: str, new: str, path: str, limit: int = 1400) -> str:
    import difflib

    diff = "".join(
        difflib.unified_diff(old.splitlines(True), new.splitlines(True), f"a/{path}", f"b/{path}", n=2)
    )
    return "```diff\n" + clip(diff, limit).replace("```", "'''") + "\n```"


def clip(text: str, limit: int) -> str:
    """Truncate text to limit characters."""
    if len(text) <= limit:
        return text
    return text[:limit] + "... [truncated]"


def create_web_tools(cfg, state: RunState, friction=None):
    """Create web and system tools."""
    limit_chars = cfg.tool_output_chars
    _safe = make_safe(friction.record if friction else None)
    tools = []

    def untrusted(text: str) -> str:
        """Label text from outside as data, and remember that this task has seen some."""
        state.tainted = True
        return UNTRUSTED + text

    @tool
    @_safe
    def web_search(query: str, max_results: int = 5) -> str:
        """Search the web (DuckDuckGo). Returns titles, URLs and snippets. Use visit_webpage to read a result.

        Args:
            query: What to search for.
            max_results: Number of results, 1-8.
        """
        from ddgs import DDGS

        try:
            results = DDGS(timeout=10).text(query, max_results=max(1, min(int(max_results), 8)))
        except Exception as e:  # noqa: BLE001 - ddgs raises many types (rate limit, no results...)
            return f"ERROR: search failed ({type(e).__name__}: {str(e)[:150]}). Rephrase, or retry once."
        if not results:
            return "No results. Try different keywords."
        lines = [
            f"{i}. {r.get('title', '')}\n   {r.get('href', '')}\n   {(r.get('body') or '')[:300]}"
            for i, r in enumerate(results, 1)
        ]
        return untrusted(clip("\n".join(lines), limit_chars))

    @tool
    @_safe
    def visit_webpage(url: str) -> str:
        """Fetch a public web page and return its text as Markdown (truncated).

        Args:
            url: Full http(s) URL.
        """
        try:
            return untrusted(fetch_page_text(url, limit_chars))
        except (ValueError, requests.RequestException) as e:
            return f"ERROR: could not fetch page: {e}"

    @tool
    @_safe
    def jarvis_status() -> str:
        """Current date/time and server health: CPU temperature, load, memory, disk, uptime."""
        from tools.system import system_report

        return system_report(cfg.timezone)

    tools += [web_search, visit_webpage, jarvis_status]
    return tools