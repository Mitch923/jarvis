#!/usr/bin/env python3
"""Fake docker/podman CLI. Logs every invocation's argv (one JSON array per line) to
FAKE_DOCKER_LOG, and pops one scripted response off the JSON queue at FAKE_DOCKER_SCRIPT for
each `run` call. `kill <name>` always succeeds. With no script left, returns a default success.
"""
import json
import os
import sys
import time

LOG = os.environ.get("FAKE_DOCKER_LOG")
SCRIPT = os.environ.get("FAKE_DOCKER_SCRIPT")


def main() -> None:
    argv = sys.argv[1:]
    if LOG:
        with open(LOG, "a") as f:
            f.write(json.dumps(argv) + "\n")

    if argv[:1] == ["kill"]:
        sys.exit(0)

    queue = []
    if SCRIPT and os.path.exists(SCRIPT):
        try:
            queue = json.load(open(SCRIPT))
        except ValueError:
            queue = []
    step = queue.pop(0) if queue else {"kind": "ok", "stdout": "default output", "code": 0}
    if SCRIPT:
        json.dump(queue, open(SCRIPT, "w"))

    kind = step.get("kind", "ok")
    if kind == "hang":
        time.sleep(step.get("seconds", 30))
        sys.exit(0)
    sys.stdout.write(step.get("stdout", ""))
    sys.stderr.write(step.get("stderr", ""))
    sys.exit(step.get("code", 0))


if __name__ == "__main__":
    main()
