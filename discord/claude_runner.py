"""Async wrapper around `claude --print` subprocess."""
from __future__ import annotations

import asyncio
import json
import logging
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

log = logging.getLogger(__name__)

OnStartedCb = Callable[[asyncio.subprocess.Process], None]


@dataclass
class ClaudeResult:
    ok: bool
    text: str
    session_id: str | None
    cost_usd: float | None
    duration_ms: int | None
    raw: dict | None
    error: str | None = None


class ClaudeRunner:
    def __init__(
        self,
        permission_mode: str,
        timeout_seconds: int,
        claude_model: str,
        max_budget_usd: float,
    ) -> None:
        exe = shutil.which("claude") or shutil.which("claude.exe")
        if not exe:
            raise SystemExit("`claude` CLI not found on PATH")
        self._exe = exe
        self._permission_mode = permission_mode
        self._timeout = timeout_seconds
        self._model = claude_model
        self._max_budget = max_budget_usd

    async def run(
        self,
        prompt: str,
        cwd: Path,
        resume_session_id: str | None,
        on_started: Optional[OnStartedCb] = None,
    ) -> ClaudeResult:
        args: list[str] = [
            self._exe,
            "--print",
            "--output-format", "json",
            "--permission-mode", self._permission_mode,
        ]
        if resume_session_id:
            args.extend(["--resume", resume_session_id])
        if self._model:
            args.extend(["--model", self._model])
        if self._max_budget > 0:
            args.extend(["--max-budget-usd", str(self._max_budget)])
        args.append(prompt)

        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                cwd=str(cwd),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as e:
            return ClaudeResult(False, "", None, None, None, None, error=f"spawn failed: {e}")

        if on_started is not None:
            try:
                on_started(proc)
            except Exception as e:
                log.warning("on_started callback raised: %s", e)

        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(), timeout=self._timeout
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return ClaudeResult(
                False, "", None, None, None, None,
                error=f"claude timed out after {self._timeout}s",
            )

        stdout = stdout_bytes.decode("utf-8", errors="replace")
        stderr = stderr_bytes.decode("utf-8", errors="replace")

        if proc.returncode != 0:
            return ClaudeResult(
                False, "", None, None, None, None,
                error=f"claude exited {proc.returncode}\nstderr:\n{stderr.strip()[:1500]}",
            )

        try:
            payload = json.loads(stdout)
        except json.JSONDecodeError:
            return ClaudeResult(
                False, "", None, None, None, None,
                error=f"claude returned non-JSON:\n{stdout[:1500]}",
            )

        return ClaudeResult(
            ok=True,
            text=str(payload.get("result", "")).strip(),
            session_id=payload.get("session_id"),
            cost_usd=payload.get("total_cost_usd"),
            duration_ms=payload.get("duration_ms"),
            raw=payload,
        )
