"""Pain points, recorded by the RUNTIME (never written by the model), for the weekly self-review.

Why not let the model note its own problems? Model-written notes are unreliable and a web page
can plant them. These events are objective: a tool returned ERROR, an LLM call failed over to
another model, a task timed out, the user said !stop, and so on. Small, rotating, secret-scrubbed.
"""
import json
import logging
import os
import threading
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from memory import SECRET_RE

log = logging.getLogger("friction")

MAX_BYTES = 256_000  # then rotate; keeps roughly the last few weeks and never grows without bound


class Friction:
    def __init__(self, data_dir: str):
        self.path = Path(data_dir) / "friction.jsonl"
        self._lock = threading.Lock()

    def record(self, kind: str, *, tool: str = "", model: str = "", detail: str = "", steps: Optional[int] = None) -> None:
        """Append one event. Never raises: logging a problem must not cause another one."""
        try:
            entry = {
                "t": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "kind": kind,
                "tool": tool,
                "model": model,
                "detail": SECRET_RE.sub("[redacted]", " ".join(str(detail).split()))[:200],
            }
            if steps is not None:
                entry["steps"] = steps
            with self._lock:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                if self.path.exists() and self.path.stat().st_size > MAX_BYTES:
                    os.replace(self.path, self.path.with_suffix(".jsonl.1"))
                with self.path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception:  # noqa: BLE001
            log.exception("could not record friction event")

    def events(self, days: float = 7, since: Optional[str] = None) -> list[dict]:
        """Events from the last `days`, optionally only those after ISO timestamp `since`."""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
        if since and since > cutoff:
            cutoff = since
        out: list[dict] = []
        with self._lock:
            for p in (self.path.with_suffix(".jsonl.1"), self.path):
                try:
                    lines = p.read_text(encoding="utf-8").splitlines()
                except OSError:
                    continue
                for line in lines:
                    try:
                        e = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(e, dict) and e.get("t", "") > cutoff:
                        out.append(e)
        return out

    def summary(self, days: float = 7, since: Optional[str] = None, max_lines: int = 12) -> tuple[str, int, bool]:
        """(report text, number of problem events, whether the user left feedback notes)."""
        events = self.events(days, since)
        runs = [e for e in events if e["kind"] == "run"]
        problems = [e for e in events if e["kind"] != "run"]
        outcomes = Counter(e["detail"] for e in runs)
        steps = [e["steps"] for e in runs if isinstance(e.get("steps"), int)]
        head = f"Runs: {len(runs)}" + (
            " (" + ", ".join(f"{n} {k}" for k, n in outcomes.most_common()) + f") · avg {sum(steps) / len(steps):.1f} steps" if runs else ""
        )
        groups: dict[tuple[str, str], list[dict]] = {}
        for e in problems:
            if e["kind"] == "user_note":
                continue  # listed one by one below: the owner's own words are the strongest signal
            groups.setdefault((e["kind"], e.get("tool") or e.get("model") or ""), []).append(e)
        lines = []
        for (kind, who), items in sorted(groups.items(), key=lambda kv: -len(kv[1]))[:max_lines]:
            last = items[-1]["detail"][:140]
            lines.append(f"- {kind}{' · ' + who if who else ''} ×{len(items)}" + (f' - last: "{last}"' if last else ""))
        notes = [f'- owner feedback: "{e["detail"]}"' for e in problems if e["kind"] == "user_note"][-5:]
        lines += notes
        text = head + "\n" + ("Friction:\n" + "\n".join(lines) if lines else "No friction recorded.")
        return text, len(problems), any(e["kind"] == "user_note" for e in problems)
