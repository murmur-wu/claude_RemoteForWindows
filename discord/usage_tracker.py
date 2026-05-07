"""Per-chat daily usage tracking + sliding-window rate limiting.

Daily counters auto-reset on date change (local time).
Rate-limit timestamps live in memory only (intentionally not persisted —
restarting the bot resets the burst window).
"""
from __future__ import annotations

import json
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import date
from pathlib import Path


@dataclass
class UsageSnapshot:
    date: str
    messages: int
    daily_limit: int  # 0 = unlimited
    cost_usd: float


@dataclass
class CheckResult:
    ok: bool
    reason: str = ""           # "rate" | "daily"
    retry_after_seconds: int = 0
    daily_used: int = 0
    daily_limit: int = 0


class UsageTracker:
    def __init__(self, path: Path, daily_limit: int, rpm_limit: int) -> None:
        self._path = path
        self._daily_limit = daily_limit  # 0 = unlimited
        self._rpm_limit = rpm_limit      # 0 = unlimited
        self._lock = threading.Lock()
        self._daily: dict[str, dict] = {}
        self._timestamps: dict[str, deque] = {}
        self._load()

    # ---------- persistence ----------

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            self._daily = json.loads(self._path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            self._daily = {}

    def _save_locked(self) -> None:
        tmp = self._path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self._daily, indent=2), encoding="utf-8")
        tmp.replace(self._path)

    @staticmethod
    def _today() -> str:
        return date.today().isoformat()

    def _ensure_today_locked(self, chat_id: str) -> dict:
        today = self._today()
        entry = self._daily.get(chat_id)
        if entry is None or entry.get("date") != today:
            entry = {"date": today, "messages": 0, "cost_usd": 0.0}
            self._daily[chat_id] = entry
        return entry

    # ---------- public API ----------

    def check_and_reserve(self, chat_id: int) -> CheckResult:
        """Atomically check rate/daily limits and reserve a slot if both pass."""
        cid = str(chat_id)
        now = time.monotonic()
        with self._lock:
            # Rate limit (rolling 60s)
            if self._rpm_limit > 0:
                ts = self._timestamps.setdefault(cid, deque())
                while ts and now - ts[0] > 60:
                    ts.popleft()
                if len(ts) >= self._rpm_limit:
                    retry = max(1, int(60 - (now - ts[0])) + 1)
                    return CheckResult(False, "rate", retry)

            entry = self._ensure_today_locked(cid)
            if self._daily_limit > 0 and entry["messages"] >= self._daily_limit:
                return CheckResult(
                    False, "daily", 0,
                    daily_used=entry["messages"],
                    daily_limit=self._daily_limit,
                )

            entry["messages"] += 1
            if self._rpm_limit > 0:
                self._timestamps[cid].append(now)
            self._save_locked()
            return CheckResult(
                True,
                daily_used=entry["messages"],
                daily_limit=self._daily_limit,
            )

    def record_cost(self, chat_id: int, cost_usd: float) -> None:
        if not cost_usd:
            return
        cid = str(chat_id)
        with self._lock:
            entry = self._ensure_today_locked(cid)
            entry["cost_usd"] = float(entry.get("cost_usd", 0.0)) + float(cost_usd)
            self._save_locked()

    def snapshot(self, chat_id: int) -> UsageSnapshot:
        cid = str(chat_id)
        with self._lock:
            entry = self._ensure_today_locked(cid)
            return UsageSnapshot(
                date=entry["date"],
                messages=entry["messages"],
                daily_limit=self._daily_limit,
                cost_usd=float(entry.get("cost_usd", 0.0)),
            )
