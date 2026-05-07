"""Load configuration from .env."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def _parse_int_set(raw: str) -> set[int]:
    out: set[int] = set()
    for piece in raw.split(","):
        piece = piece.strip()
        if not piece:
            continue
        try:
            out.add(int(piece))
        except ValueError:
            raise SystemExit(f"ALLOWED_CHAT_IDS contains non-integer: {piece!r}")
    return out


def _parse_projects(raw: str) -> dict[str, Path]:
    out: dict[str, Path] = {}
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        if "=" not in entry:
            raise SystemExit(f"PROJECTS entry missing '=': {entry!r}")
        name, path = entry.split("=", 1)
        name = name.strip()
        path = Path(path.strip()).expanduser().resolve()
        out[name] = path
    return out


@dataclass(frozen=True)
class Config:
    telegram_token: str
    allowed_chat_ids: set[int]
    projects: dict[str, Path]
    default_project: str
    permission_mode: str
    timeout_seconds: int
    claude_model: str
    max_budget_usd: float
    daily_message_limit: int
    rate_limit_per_minute: int
    state_dir: Path = field(default_factory=lambda: Path(__file__).parent / "state")

    @classmethod
    def load(cls) -> "Config":
        token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        if not token or token.startswith("123456:"):
            raise SystemExit("TELEGRAM_BOT_TOKEN not set in .env")

        projects = _parse_projects(os.environ.get("PROJECTS", ""))
        if not projects:
            raise SystemExit("PROJECTS not set in .env")

        default_project = os.environ.get("DEFAULT_PROJECT", "default").strip()
        if default_project not in projects:
            raise SystemExit(
                f"DEFAULT_PROJECT={default_project!r} not in PROJECTS keys: {list(projects)}"
            )

        cfg = cls(
            telegram_token=token,
            allowed_chat_ids=_parse_int_set(os.environ.get("ALLOWED_CHAT_IDS", "")),
            projects=projects,
            default_project=default_project,
            permission_mode=os.environ.get("PERMISSION_MODE", "acceptEdits").strip(),
            timeout_seconds=int(os.environ.get("CLAUDE_TIMEOUT_SECONDS", "600")),
            claude_model=os.environ.get("CLAUDE_MODEL", "").strip(),
            max_budget_usd=float(os.environ.get("MAX_BUDGET_USD", "0") or 0),
            daily_message_limit=int(os.environ.get("DAILY_MESSAGE_LIMIT", "100") or 0),
            rate_limit_per_minute=int(os.environ.get("RATE_LIMIT_PER_MINUTE", "6") or 0),
        )
        cfg.state_dir.mkdir(parents=True, exist_ok=True)
        return cfg
