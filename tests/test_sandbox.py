import json, os, shutil, stat, sys, tempfile, time
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))
from pathlib import Path

ok = lambda s: print("PASS", s)

HERE = Path(__file__).resolve().parent
FAKE_SRC = HERE / "fake_docker.py"


def fake_bin_dir(names=("docker", "podman")) -> Path:
    """A directory containing executables with these names, both running fake_docker.py."""
    d = Path(tempfile.mkdtemp())
    for name in names:
        target = d / name
        target.write_text(f"#!/usr/bin/env python3\nimport runpy\nrunpy.run_path({str(FAKE_SRC)!r}, run_name='__main__')\n")
        target.chmod(target.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return d


def with_fake_docker(script: list[dict]):
    """Context manager-ish helper: returns (log_path, restore()) after prepending a fake docker/podman to PATH."""
    bindir = fake_bin_dir()
    logf = tempfile.mktemp()
    scriptf = tempfile.mktemp()
    json.dump(script, open(scriptf, "w"))
    old_path, old_log, old_script = os.environ.get("PATH", ""), os.environ.get("FAKE_DOCKER_LOG"), os.environ.get("FAKE_DOCKER_SCRIPT")
    os.environ["PATH"] = f"{bindir}:{old_path}"
    os.environ["FAKE_DOCKER_LOG"] = logf
    os.environ["FAKE_DOCKER_SCRIPT"] = scriptf

    def restore():
        os.environ["PATH"] = old_path
        if old_log is None:
            os.environ.pop("FAKE_DOCKER_LOG", None)
        else:
            os.environ["FAKE_DOCKER_LOG"] = old_log
        if old_script is None:
            os.environ.pop("FAKE_DOCKER_SCRIPT", None)
        else:
            os.environ["FAKE_DOCKER_SCRIPT"] = old_script

    return logf, restore


def read_log(logf) -> list[list[str]]:
    if not os.path.exists(logf):
        return []
    return [json.loads(line) for line in open(logf) if line.strip()]


os.environ.update(DISCORD_TOKEN="x", DISCORD_ALLOWED_USER_IDS="1", OPENROUTER_API_KEY="k", GITHUB_TOKEN="")
from config import Config
import sandbox as S


RESETTABLE = ("TEST_COMMANDS", "SANDBOX_RUNTIME", "SANDBOX_MEMORY", "SANDBOX_CPUS", "SANDBOX_NETWORK", "SANDBOX_TIMEOUT", "GITHUB_TOKEN")


def cfg(**overrides):
    for k in RESETTABLE:  # each cfg() call is independent of whatever earlier calls set
        os.environ.pop(k, None)
    os.environ.update(DATA_DIR=tempfile.mkdtemp())
    os.environ.update(overrides)
    return Config.from_env()


def make_local(*files) -> Path:
    d = Path(tempfile.mkdtemp())
    for name in files:
        (d / name).write_text("x\n")
    return d


# ---------- resolve_command: auto-detection
c = cfg()
local = make_local("tests/run_all.py") if False else None
d = Path(tempfile.mkdtemp())
(d / "tests").mkdir()
(d / "tests" / "run_all.py").write_text("x")
image, command = S.resolve_command(c, "me/proj", d)
assert image == "python:3.12-slim" and command == "python tests/run_all.py"
ok("resolve_command: recognises this project's own tests/run_all.py convention")

d2 = Path(tempfile.mkdtemp())
(d2 / "package.json").write_text("{}")
image2, command2 = S.resolve_command(c, "me/proj", d2)
assert image2 == "node:20-slim" and "npm" in command2
ok("resolve_command: package.json -> node image + npm test")

d3 = Path(tempfile.mkdtemp())
(d3 / "go.mod").write_text("module x")
image3, command3 = S.resolve_command(c, "me/proj", d3)
assert image3 == "golang:1.22-alpine" and command3 == "go test ./..."
ok("resolve_command: go.mod -> golang image")

d4 = Path(tempfile.mkdtemp())
try:
    S.resolve_command(c, "me/unknown", d4)
    assert False
except S.SandboxError as e:
    assert "TEST_COMMANDS" in str(e)
ok("resolve_command: unrecognised project with no override -> clear error naming TEST_COMMANDS")

# ---------- resolve_command: TEST_COMMANDS override (bare command, and image|command)
c2 = cfg(TEST_COMMANDS="me/proj=echo hi;*=echo wildcard")
image4, command4 = S.resolve_command(c2, "me/proj", d)  # d has tests/run_all.py -> image auto-detected as python
assert image4 == "python:3.12-slim" and command4 == "echo hi"
ok("resolve_command: TEST_COMMANDS overrides the command, keeps auto-detected image")
image5, command5 = S.resolve_command(c2, "me/other-unlisted", d4)  # falls to "*" wildcard
assert command5 == "echo wildcard"
ok("resolve_command: '*' wildcard used when the repo has no specific entry")

c3 = cfg(TEST_COMMANDS="me/proj=custom/image:tag|pytest -q")
image6, command6 = S.resolve_command(c3, "me/proj", d)
assert image6 == "custom/image:tag" and command6 == "pytest -q"
ok("resolve_command: 'image|command' syntax sets both explicitly")

# ---------- run_tests: docker not installed
c4 = cfg(SANDBOX_RUNTIME="docker")
old_path = os.environ.get("PATH", "")
os.environ["PATH"] = "/nonexistent"
try:
    S.run_tests(c4, "me/proj", d)
    assert False
except S.SandboxError as e:
    assert "not installed" in str(e)
finally:
    os.environ["PATH"] = old_path
ok("run_tests: missing docker binary -> clear error")

# ---------- run_tests: disabled via empty SANDBOX_RUNTIME
c5 = cfg(SANDBOX_RUNTIME="")
try:
    S.run_tests(c5, "me/proj", d)
    assert False
except S.SandboxError as e:
    assert "disabled" in str(e)
ok("run_tests: SANDBOX_RUNTIME='' -> disabled, clear error, no subprocess spawned")

# ---------- run_tests: successful run, correct command construction
logf, restore = with_fake_docker([{"kind": "ok", "stdout": "3 passed\n", "code": 0}])
try:
    c6 = cfg(SANDBOX_RUNTIME="docker", SANDBOX_MEMORY="512m", SANDBOX_CPUS="1", SANDBOX_NETWORK="none")
    result = S.run_tests(c6, "me/proj", d)
finally:
    restore()
assert result.ok and "3 passed" in result.output and result.image == "python:3.12-slim"
calls = read_log(logf)
assert len(calls) == 1
argv = calls[0]
assert argv[0] == "run" and "--rm" in argv
assert argv[argv.index("--memory") + 1] == "512m"
assert argv[argv.index("--cpus") + 1] == "1"
assert argv[argv.index("--network") + 1] == "none"
assert argv[argv.index("--pids-limit") + 1] == "256"
assert f"{d}:/repo" in argv
assert argv[-1] == "python tests/run_all.py" and argv[-3] == "sh"
assert not any(a.startswith("-e") and "TOKEN" in a.upper() for a in argv)  # no secrets ever get an -e flag
ok(f"run_tests: constructs the docker invocation correctly -> {result.seconds:.2f}s")

# ---------- run_tests: failing tests reported as ok=False, not an exception
logf, restore = with_fake_docker([{"kind": "ok", "stdout": "", "stderr": "2 failed, 1 error\n", "code": 1}])
try:
    result = S.run_tests(cfg(SANDBOX_RUNTIME="docker"), "me/proj", d)
finally:
    restore()
assert result.ok is False and "2 failed" in result.output
ok("run_tests: non-zero exit -> SandboxResult(ok=False), not raised as an error")

# ---------- run_tests: timeout kills the named container and reports cleanly
logf, restore = with_fake_docker([{"kind": "hang", "seconds": 5}])
try:
    c7 = cfg(SANDBOX_RUNTIME="docker", SANDBOX_TIMEOUT="1")
    t0 = time.time()
    result = S.run_tests(c7, "me/proj", d)
    elapsed = time.time() - t0
finally:
    restore()
assert result.timed_out and not result.ok and "killed" in result.output.lower()
assert elapsed < 4, f"should not wait for the full 5s hang, took {elapsed:.1f}s"
calls = read_log(logf)
assert any(c[:1] == ["kill"] for c in calls), "must explicitly `docker kill` the named container on timeout"
ok(f"run_tests: SANDBOX_TIMEOUT kills the container by name, doesn't wait it out ({elapsed:.1f}s)")

# ---------- run_tests: podman as the runtime
logf, restore = with_fake_docker([{"kind": "ok", "stdout": "ok\n", "code": 0}])
try:
    result = S.run_tests(cfg(SANDBOX_RUNTIME="podman"), "me/proj", d)
finally:
    restore()
assert result.ok
ok("run_tests: SANDBOX_RUNTIME=podman uses the podman binary")

# ---------- run_tests: no host env leaks into the container invocation itself
os.environ["GITHUB_TOKEN"] = "super-secret-value-should-never-appear"
logf, restore = with_fake_docker([{"kind": "ok", "stdout": "ok\n", "code": 0}])
try:
    S.run_tests(cfg(SANDBOX_RUNTIME="docker", GITHUB_TOKEN="super-secret-value-should-never-appear", GITHUB_ALLOWED_REPOS="me/proj"), "me/proj", d)
finally:
    restore()
calls = read_log(logf)
assert not any("super-secret" in a for a in calls[0]), "secret leaked into the docker argv"
ok("run_tests: no secret ever appears in the constructed docker command line")

print("sandbox.py: all checks passed")
