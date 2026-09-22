"""Run every tests/test_*.py, each in its own process (they change os.environ).

    python tests/run_all.py            # everything
    python tests/run_all.py llm agent  # only files whose name contains one of these words

Exit code 0 = all green. A test file passes if it exits 0 and printed at least one "PASS ..." line.
Used by GitHub Actions (.github/workflows/ci.yml). No dependencies beyond the app's own.
"""
import subprocess
import sys
import time
from pathlib import Path


def main(words: list[str]) -> int:
    here = Path(__file__).resolve().parent
    files = [f for f in sorted(here.glob("test_*.py")) if not words or any(w in f.name for w in words)]
    if not files:
        print("no tests matched")
        return 1
    failed, total_checks, started = [], 0, time.time()
    for f in files:
        t0 = time.time()
        try:
            r = subprocess.run([sys.executable, str(f)], capture_output=True, text=True, timeout=900)
            out, code = r.stdout + r.stderr, r.returncode
        except subprocess.TimeoutExpired:
            out, code = "timed out after 900s", 1
        checks = sum(1 for line in out.splitlines() if line.startswith("PASS"))
        total_checks += checks
        ok = code == 0 and checks > 0
        print(f"{'ok  ' if ok else 'FAIL'} {f.name:26} {checks:3} checks {time.time() - t0:6.1f}s")
        if not ok:
            failed.append(f.name)
            print("\n".join("     | " + line for line in out.strip().splitlines()[-25:]))
    print(f"\n{total_checks} checks, {len(files) - len(failed)}/{len(files)} files green in {time.time() - started:.0f}s")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
