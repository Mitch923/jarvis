"""JSON state store for proactive features (features 2-5).

A single JSON file in DATA_DIR, one section per feature, so each can be
added/changed independently as it lands. The store is a file-backed dict with a
process-local lock (safe for single-writer; the bot has one process, and
webhook + poll run in the same process). All sections are pruned so the file
stays small (SD-card friendly, like watch.json).

Sections:
  seen_deliveries  {delivery_id: iso-ts}   webhook/poll event dedup
  proactive_log    [iso-ts]               count proactive actions per day
  fix_attempts     {repo#pr: int}         attempts before "give up"
  reviewed         {repo#pr: head_sha}    auto-review memory
"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

from config import Config

__all__ = ["State"]

DEFAULT_DATA = {
    "seen_deliveries": {},
    "proactive_log": [],
    "fix_attempts": {},
    "reviewed": {},
}

_SEEN_TTL_S = 7 * 86400  # keep delivery ids for a week
_SEEN_MAX = 5000         # hard cap on seen deliveries
_LOG_MAX = 5000          # hard cap on proactive log entries


def utc_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_ts(ts: str) -> float | None:
    try:
        dt = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except ValueError:
        return None


class State:
    """Load/save a JSON file in DATA_DIR with one section per proactive feature."""

    def __init__(self, cfg: Config):
        self._cfg = cfg
        self._path = Path(cfg.data_dir) / "state.json"
        self._lock = threading.Lock()
        self._data = {k: (list(v) if isinstance(v, list) else dict(v)) for k, v in DEFAULT_DATA.items()}
        self._load()

    def _load(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with open(self._path) as f:
                raw = json.load(f)
            if isinstance(raw, dict):
                for k in DEFAULT_DATA:
                    if k in raw:
                        v = raw[k]
                        if isinstance(v, dict) and isinstance(DEFAULT_DATA[k], dict):
                            self._data[k] = v
                        elif isinstance(v, list) and isinstance(DEFAULT_DATA[k], list):
                            self._data[k] = v
        except (OSError, json.JSONDecodeError):
            self._save_backup()
            self._data = {
                k: (list(v) if isinstance(v, list) else dict(v)) for k, v in DEFAULT_DATA.items()
            }

    def _save_backup(self) -> None:
        try:
            if self._path.exists():
                self._path.rename(self._path.with_suffix(".corrupt"))
        except OSError:
            pass

    def _save(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".tmp")
            with open(tmp, "w") as f:
                json.dump(self._data, f, indent=2)
            os.replace(tmp, self._path)
        except OSError:
            pass  # SD-card writes can flake; never crash the bot over state

    # ---------- delivery dedup ----------
    def record_delivery(self, delivery_id: str) -> bool:
        """Record a delivery id; True if new (first time), False if already seen."""
        with self._lock:
            if delivery_id in self._data["seen_deliveries"]:
                return False
            self._data["seen_deliveries"][delivery_id] = utc_iso()
            self._prune_seen()
            self._save()
            return True

    def seen_delivery(self, delivery_id: str) -> bool:
        with self._lock:
            return delivery_id in self._data["seen_deliveries"]

    def _prune_seen(self) -> None:
        now = datetime.now(timezone.utc).timestamp()
        cutoff = now - _SEEN_TTL_S
        kept = {
            k: v for k, v in self._data["seen_deliveries"].items()
            if (t := _parse_ts(v)) is not None and t >= cutoff
        }
        if len(kept) > _SEEN_MAX:  # keep the most-recent (we lost ordering; just cap)
            kept = dict(list(kept.items())[-_SEEN_MAX:])
        self._data["seen_deliveries"] = kept

    # ---------- proactive action logging ----------
    def record_proactive(self) -> None:
        """Log that a proactive action ran (for the per-day cap)."""
        with self._lock:
            self._data["proactive_log"].append(utc_iso())
            if len(self._data["proactive_log"]) > _LOG_MAX:
                self._data["proactive_log"] = self._data["proactive_log"][-_LOG_MAX:]
            self._save()

    def count_proactive_since(self, since_iso: str) -> int:
        """Count proactive actions logged at/after since_iso (ISO-8601 UTC)."""
        with self._lock:
            since = _parse_ts(since_iso)
            if since is None:
                return len(self._data["proactive_log"])
            return sum(1 for t in self._data["proactive_log"] if (x := _parse_ts(t)) is not None and x >= since)

    # ---------- fix attempts ----------
    def increment_fix_attempts(self, key: str) -> int:
        """key = 'owner/repo#PRnum'; returns the new count (>=1)."""
        with self._lock:
            n = int(self._data["fix_attempts"].get(key, 0)) + 1
            self._data["fix_attempts"][key] = n
            self._save()
            return n

    def get_fix_attempts(self, key: str) -> int:
        with self._lock:
            return int(self._data["fix_attempts"].get(key, 0))

    def reset_fix_attempts(self, key: str) -> None:
        with self._lock:
            if key in self._data["fix_attempts"]:
                self._data["fix_attempts"][key] = 0
                self._save()

    # ---------- reviewed (auto-review) ----------
    def mark_reviewed(self, repo: str, pr: int, head_sha: str) -> None:
        with self._lock:
            self._data["reviewed"][f"{repo}#{pr}"] = head_sha
            self._save()

    def last_reviewed(self, repo: str, pr: int) -> str | None:
        with self._lock:
            return self._data["reviewed"].get(f"{repo}#{pr}")

    # ---------- accessors (read-only copies) ----------
    def data(self) -> dict:
        with self._lock:
            return {
                k: (dict(v) if isinstance(v, dict) else list(v)) for k, v in self._data.items()
            }

    def reset(self) -> None:
        """Replace the file contents with defaults (tests)."""
        with self._lock:
            self._data = {
                k: (list(v) if isinstance(v, list) else dict(v)) for k, v in DEFAULT_DATA.items()
            }
            self._save()

    def path(self) -> Path:
        return self._path
