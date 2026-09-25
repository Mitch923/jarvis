"""Runs a repo's own test suite inside an ephemeral, resource-limited container (Docker or
Podman) - never on the host directly. Pairs with checkouts.py: repo_test syncs a repo/branch
locally, then hands that checkout to a container to install and test.

Security posture, honestly stated:
  * --rm, uniquely named, ephemeral: nothing persists after the run, and a timeout explicitly
    kills the named container (subprocess timeouts alone don't reliably stop a detached container)
  * CPU/memory/process-count limits (SANDBOX_CPUS/SANDBOX_MEMORY/--pids-limit)
  * none of the bot's own environment reaches the container - no GitHub token, no LLM API keys,
    no Discord token. Docker doesn't forward the host shell's env by default; this module never
    adds any `-e` flag that would.
  * the container gets read-write access to its OWN checkout directory (so `pip install`/`npm
    install`/build artifacts have somewhere to go) but NOTHING else on the host. That checkout is
    a disposable mirror - checkouts.py hard-resets and cleans it on every future sync - so anything
    written there is expected to vanish before it's used again, not something worth protecting.
  * network access is OFF by default (SANDBOX_NETWORK=none). Only repos explicitly listed in
    SANDBOX_NETWORK_ALLOWED_REPOS may use the configured network mode (bridge or none). This is
    a default-deny posture: most projects don't need network access during test runs if they
    vendor dependencies or have none; those that do (e.g. need to install from PyPI/npm) must be
    explicitly opted in. Either way the container has no secrets worth exfiltrating.
  * NOT included: a read-only root filesystem. That would break ordinary system-wide `pip
    install`/`npm install` (they write under /usr or /root), and reaching that level of isolation
    properly (a venv, a locked-down image per language) was judged not worth the complexity for a
    personal tool where the main risk is host filesystem and credential exposure - both are
    already excluded above.

This only shells out to the `docker`/`podman` CLI (not any SDK), so machines without either still
run every other feature normally; repo_test just reports that it's unavailable.
"""
import logging
import shutil
import subprocess
import time
import uuid
from pathlib import Path

from config import Config
from ghclient import clip

log = logging.getLogger("sandbox")

# (marker files that must ALL exist at the repo root, shell command to run if none of the
# repo-specific TEST_COMMANDS entries match). Checked in order; first full match wins. This list
# only needs to cover common cases reasonably well - TEST_COMMANDS overrides it per repo.
AUTO_DETECT: list[tuple[tuple[str, ...], str]] = [
    (("tests/run_all.py",), "python tests/run_all.py"),
    (("pyproject.toml",), "pip install -q --root-user-action=ignore . 2>/dev/null; pip install -q --root-user-action=ignore pytest; python -m pytest -q"),
    (("requirements.txt",), "pip install -q --root-user-action=ignore -r requirements.txt; python -m pytest -q || python -m unittest discover -q"),
    (("package.json",), "(npm ci --silent 2>/dev/null || npm install --silent) && npm test --silent"),
    (("go.mod",), "go test ./..."),
    (("Cargo.toml",), "cargo test --quiet"),
]

LANGUAGE_IMAGES: list[tuple[tuple[str, ...], str]] = [
    (("requirements.txt", "pyproject.toml", "setup.py", "setup.cfg", "Pipfile", "tests/run_all.py"), "python:3.12-slim"),
    (("package.json",), "node:20-slim"),
    (("go.mod",), "golang:1.22-alpine"),
    (("Cargo.toml",), "rust:1.82-slim"),
]

DEFAULT_IMAGE = "python:3.12-slim"


class SandboxError(Exception):
    pass


class SandboxResult:
    def __init__(self, ok: bool, output: str, image: str, command: str, seconds: float, timed_out: bool = False):
        self.ok = ok
        self.output = output
        self.image = image
        self.command = command
        self.seconds = seconds
        self.timed_out = timed_out


def _which(runtime: str) -> str:
    path = shutil.which(runtime)
    if not path:
        raise SandboxError(
            f"'{runtime}' is not installed or not on PATH. Install Docker (or Podman, with "
            f"SANDBOX_RUNTIME=podman) to enable repo_test, or set SANDBOX_RUNTIME= to disable it."
        )
    return path


def _detect_image(local: Path) -> str:
    for markers, image in LANGUAGE_IMAGES:
        if any((local / m).exists() for m in markers):
            return image
    return DEFAULT_IMAGE


def resolve_command(cfg: Config, repo_slug: str, local: Path) -> tuple[str, str]:
    """(image, shell_command) for `repo_slug` at checkout `local`.

    A TEST_COMMANDS entry for this repo (or the "*" fallback) always wins; its value is either a
    bare command (auto-detected image is kept) or "image:tag|command" to set both explicitly.
    Otherwise the first AUTO_DETECT pattern whose marker files are all present is used.
    """
    override = cfg.test_commands.get(repo_slug.lower()) or cfg.test_commands.get("*")
    image = _detect_image(local)
    if override:
        if "|" in override:
            explicit_image, cmd = override.split("|", 1)
            return explicit_image.strip(), cmd.strip()
        return image, override
    for markers, cmd in AUTO_DETECT:
        if all((local / m).exists() for m in markers):
            return image, cmd
    raise SandboxError(
        f"Don't know how to test {repo_slug}: no recognised project files and no TEST_COMMANDS "
        f"entry. Add one, e.g. TEST_COMMANDS={repo_slug}=python -m pytest -q "
        f"(or {repo_slug}=some/image:tag|<command> to also pick the image)."
    )


def run_tests(cfg: Config, repo_slug: str, local: Path) -> SandboxResult:
    """Run `repo_slug`'s tests, as checked out at `local`, inside a fresh container."""
    if not cfg.sandbox_runtime:
        raise SandboxError("Sandboxed test runs are disabled (SANDBOX_RUNTIME is empty).")
    runtime = _which(cfg.sandbox_runtime)
    image, command = resolve_command(cfg, repo_slug, local)
    name = f"jarvis-test-{uuid.uuid4().hex[:12]}"

    # Network mode: default-deny. Only repos in SANDBOX_NETWORK_ALLOWED_REPOS get the configured network (bridge/none).
    # All other repos are forced to "none" (air-gapped).
    repo_slug_lower = repo_slug.lower()
    if repo_slug_lower in cfg.sandbox_network_allowed_repos:
        network_mode = cfg.sandbox_network
        log.info("sandbox: %s network allowed (config=%s)", repo_slug, network_mode)
    else:
        network_mode = "none"
        log.info("sandbox: %s network denied (default-deny, not in SANDBOX_NETWORK_ALLOWED_REPOS)", repo_slug)

    args = [
        runtime, "run", "--rm", "--name", name,
        "--memory", cfg.sandbox_memory,
        "--cpus", cfg.sandbox_cpus,
        "--pids-limit", "256",
        "--network", network_mode,
        "-v", f"{local}:/repo",
        "--workdir", "/repo",
        "--tmpfs", "/tmp:rw,size=512m",
        "-e", "HOME=/tmp",
        image,
        "sh", "-c", command,
    ]  # fmt: skip
    log.info("sandbox: %s in %s (%s, network=%s)", repo_slug, image, cfg.sandbox_runtime, network_mode)

    t0 = time.monotonic()
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=cfg.sandbox_timeout)
    except subprocess.TimeoutExpired as e:
        # subprocess.run() only kills the CLI client on timeout; the container can keep running
        # under the daemon unless we explicitly kill it by name too. Best-effort: this is itself
        # time-boxed and never allowed to raise past the timeout error we're already reporting.
        try:
            subprocess.run([runtime, "kill", name], capture_output=True, timeout=15)
        except Exception:  # noqa: BLE001
            log.exception("could not kill timed-out sandbox container %s", name)
        partial = (e.stdout or "") + (e.stderr or "") if isinstance(e.stdout, str) else ""
        return SandboxResult(
            ok=False,
            output=clip(partial, 4000) + f"\n[killed: exceeded {cfg.sandbox_timeout:.0f}s]",
            image=image,
            command=command,
            seconds=time.monotonic() - t0,
            timed_out=True,
        )
    except FileNotFoundError as e:
        raise SandboxError(f"'{runtime}' failed to run: {e}") from e

    output = (r.stdout or "") + (r.stderr or "")
    return SandboxResult(ok=r.returncode == 0, output=clip(output, 6000), image=image, command=command, seconds=time.monotonic() - t0)
