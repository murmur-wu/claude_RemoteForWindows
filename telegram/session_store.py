"""Persist per-chat Claude session state to JSON."""
from __future__ import annotations

import json
import threading
from dataclasses import asdict, dataclass, fields
from pathlib import Path


@dataclass
class ChatState:
    session_id: str | None = None
    project: str = "default"
    last_prompt: str | None = None
    model: str | None = None


_VALID_FIELDS = {f.name for f in fields(ChatState)}


class SessionStore:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._data: dict[str, ChatState] = {}
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return
        for chat_id, payload in raw.items():
            # tolerate unknown keys (forward/backward compat)
            clean = {k: v for k, v in payload.items() if k in _VALID_FIELDS}
            self._data[chat_id] = ChatState(**clean)

    def _save_locked(self) -> None:
        tmp = self._path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps({k: asdict(v) for k, v in self._data.items()}, indent=2),
            encoding="utf-8",
        )
        tmp.replace(self._path)

    def get(self, chat_id: int, default_project: str) -> ChatState:
        with self._lock:
            state = self._data.get(str(chat_id))
            if state is None:
                state = ChatState(project=default_project)
                self._data[str(chat_id)] = state
                self._save_locked()
            return ChatState(
                session_id=state.session_id,
                project=state.project,
                last_prompt=state.last_prompt,
                model=state.model,
            )

    def set_session_id(self, chat_id: int, session_id: str) -> None:
        with self._lock:
            state = self._data.setdefault(str(chat_id), ChatState())
            state.session_id = session_id
            self._save_locked()

    def set_project(self, chat_id: int, project: str) -> None:
        with self._lock:
            state = self._data.setdefault(str(chat_id), ChatState())
            state.project = project
            state.session_id = None
            self._save_locked()

    def set_last_prompt(self, chat_id: int, prompt: str) -> None:
        with self._lock:
            state = self._data.setdefault(str(chat_id), ChatState())
            state.last_prompt = prompt
            self._save_locked()

    def set_model(self, chat_id: int, model: str | None) -> None:
        with self._lock:
            state = self._data.setdefault(str(chat_id), ChatState())
            state.model = model
            self._save_locked()

    def reset(self, chat_id: int) -> None:
        with self._lock:
            state = self._data.setdefault(str(chat_id), ChatState())
            state.session_id = None
            self._save_locked()
