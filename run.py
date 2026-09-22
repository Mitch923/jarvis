"""Launcher. systemd runs THIS, not main.py. Keep it tiny, dumb and standard-library only.

Its one job: if an update was applied and the new code keeps failing to start, put the previous
version back before starting again. (updater.py marks the update "ok" once the bot has logged in.)
It is a locked file: the agent may not edit it, and !update warns before installing a change to it.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
MAX_BOOT_ATTEMPTS = 3  # boots that may fail after an update before we roll back


def maybe_rollback(app_dir: Path = HERE) -> "str | None":
    """Count this boot; after too many failed ones, `git reset --hard` to the pre-update commit."""
    path = app_dir / "data" / "update.json"
    try:
        st = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    if st.get("state") not in ("pending", "applying"):
        return None
    st["attempts"] = int(st.get("attempts", 0)) + 1
    note = None
    if st["attempts"] > MAX_BOOT_ATTEMPTS and st.get("previous"):
        r = subprocess.run(["git", "-C", str(app_dir), "reset", "--hard", st["previous"]], capture_output=True, text=True, timeout=60)
        st.update(state="rolled_back", reported=False, reason=f"the new version failed to start {MAX_BOOT_ATTEMPTS} times")
        note = f"rolled back to {st['previous'][:7]} (git exit {r.returncode})"
    try:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(st))
        os.replace(tmp, path)
    except OSError:
        pass
    return note


def main() -> None:
    note = maybe_rollback()
    if note:
        print(f"run.py: {note}", file=sys.stderr, flush=True)
    os.execv(sys.executable, [sys.executable, str(HERE / "main.py")])


if __name__ == "__main__":
    main()
