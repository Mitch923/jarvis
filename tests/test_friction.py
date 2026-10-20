import sys, tempfile, time
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))
from friction import Friction, MAX_BYTES, MAX_ROTATED
ok = lambda s: print("PASS", s)

d = tempfile.mkdtemp(); f = Friction(d)
f.record("tool_error", tool="gh_browse", detail="ERROR: Not found")
f.record("tool_error", tool="gh_browse", detail="ERROR: Not found again")
f.record("llm_transient", model="m1", detail="timeout")
f.record("run", model="m1", detail="ok", steps=3)
f.record("run", model="m1", detail="max_steps", steps=8)
f.record("user_note", detail="the bot keeps misreading my repo names")
report, n, has_notes = f.summary(days=7)
print(report)
assert n == 4 and has_notes and "gh_browse ×2" in report and "Runs: 2 (ok 1 times" in report.replace(",", "") or "Runs: 2" in report
assert 'owner feedback: "the bot keeps misreading' in report
ok("summary: groups repeats, counts runs, lists user feedback verbatim")

# secrets scrubbed even from friction details
f.record("tool_error", tool="x", detail="failed with token ghp_abcdefghijklmnop1234 in header")
report2, _, _ = f.summary(days=7)
assert "ghp_" not in report2 and "[redacted]" in report2
ok("secret-looking strings redacted from friction details")

# since= filters
events = f.events(days=7)
mid = events[len(events)//2]["t"]
assert len(f.events(days=7, since=mid)) < len(events)
ok("since= narrows the window")

# never raises
import logging
logging.getLogger("friction").disabled = True
f.path = "/definitely/not/writable/friction.jsonl"
f.record("tool_error", tool="x", detail="y")  # must not throw
logging.getLogger("friction").disabled = False
ok("record() never raises even when the path is bad")

# rotation
d2 = tempfile.mkdtemp(); f2 = Friction(d2)
big = "x" * 300
i = 0
while not f2.path.exists() or f2.path.stat().st_size <= MAX_BYTES:
    f2.record("tool_error", tool="t", detail=big); i += 1
    assert i < 5000, "rotation threshold never reached"
size_before_rotate = f2.path.stat().st_size
assert size_before_rotate > MAX_BYTES
f2.record("tool_error", tool="t", detail=big)  # this call should trigger the rotation
assert f2.path.with_suffix(".jsonl.1").exists()
assert f2.path.stat().st_size < size_before_rotate
ok(f"friction log rotates past {MAX_BYTES} bytes ({i} events)")
assert len(f2.events(days=7)) > 0
ok("events readable across the rotated + current file")

# rotation cap: force many more rotations than MAX_ROTATED and assert the oldest are deleted
from pathlib import Path
import friction as _friction

d3 = tempfile.mkdtemp(); f3 = Friction(d3)
real_max = _friction.MAX_BYTES
_friction.MAX_BYTES = 2_000  # tiny threshold so a dozen rotations happen in a second
try:
    for _ in range((MAX_ROTATED + 5) * 60):
        f3.record("tool_error", tool="t", detail=big)
finally:
    _friction.MAX_BYTES = real_max
rotated = list(Path(d3).glob("friction.jsonl.*"))
assert len(rotated) <= MAX_ROTATED, f"{len(rotated)} rotated files kept, cap is {MAX_ROTATED}"
ok(f"rotation keeps at most {MAX_ROTATED} rotated files ({len(rotated)} kept after {MAX_ROTATED + 5}+ rotations)")

# events() must still see the older rotations, not just .1 and the current file
newest_two = [Path(d3) / "friction.jsonl", Path(d3) / "friction.jsonl.1"]
readable = sum(len(p.read_text(encoding="utf-8").splitlines()) for p in newest_two if p.exists())
assert len(f3.events(days=7)) > readable, "events() is missing the older rotated files"
ok("events() reads every rotated file, not just .1")
