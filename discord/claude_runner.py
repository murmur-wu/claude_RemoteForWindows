"""Async streaming wrapper around `claude --print --output-format stream-json`.

Parses claude's JSONL event stream, accumulates assistant text + tool calls,
and (optionally) emits throttled progress snapshots via `on_update`. Without
a callback, behaviour is equivalent to the old `--output-format json` mode
from the caller's perspective.
"""
from __future__ import annotations

import asyncio
import json
import logging
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable, Optional

log = logging.getLogger(__name__)

OnStartedCb = Callable[[asyncio.subprocess.Process], None]
OnUpdateCb = Callable[[str], Awaitable[None]]

PROGRESS_TOOL_LINES_MAX = 12
PROGRESS_TEXT_TAIL_MAX = 1200
DEFAULT_UPDATE_THROTTLE_SECONDS = 2.0


@dataclass
class ClaudeResult:
    ok: bool
    text: str
    session_id: str | None
    cost_usd: float | None
    duration_ms: int | None
    raw: dict | None
    error: str | None = None
    num_turns: int | None = None
    context_overflow: bool = False


def _is_context_overflow(text: str) -> bool:
    t = text.lower()
    return (
        "prompt is too long" in t
        or "context length" in t
        or "context window" in t
        or "context limit" in t
    )


def _format_tool_use(name: str, inp: dict) -> str:
    if name in ("Read", "Edit", "Write", "NotebookEdit"):
        return f"🔧 {name} `{inp.get('file_path', '?')}`"
    if name == "Bash":
        cmd = (inp.get("command") or "").splitlines()[0].strip()
        if len(cmd) > 80:
            cmd = cmd[:77] + "..."
        return f"🔧 Bash `{cmd}`"
    if name in ("Grep", "Glob"):
        return f"🔧 {name} `{inp.get('pattern', '?')}`"
    if name == "TodoWrite":
        return f"🔧 TodoWrite ({len(inp.get('todos') or [])} items)"
    if name == "WebFetch":
        return f"🔧 WebFetch `{inp.get('url', '?')}`"
    if name == "WebSearch":
        return f"🔧 WebSearch `{inp.get('query', '?')}`"
    if name == "Task":
        label = inp.get("subagent_type") or inp.get("description") or "?"
        return f"🔧 Task `{label}`"
    return f"🔧 {name}"


def _render_progress(header: str, tool_lines: list[str], current_text: str) -> str:
    parts: list[str] = [header]
    if tool_lines:
        if len(tool_lines) <= PROGRESS_TOOL_LINES_MAX:
            shown = tool_lines
            prefix = ""
        else:
            shown = tool_lines[-PROGRESS_TOOL_LINES_MAX:]
            prefix = f"…（前 {len(tool_lines) - PROGRESS_TOOL_LINES_MAX} 筆省略）\n"
        parts.append(prefix + "\n".join(shown))
    if current_text:
        tail = current_text[-PROGRESS_TEXT_TAIL_MAX:]
        if len(current_text) > PROGRESS_TEXT_TAIL_MAX:
            tail = "…" + tail
        parts.append(f"📝 {tail}")
    return "\n\n".join(parts)


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
        on_update: Optional[OnUpdateCb] = None,
        update_header: str = "執行中…",
        update_throttle_seconds: float = DEFAULT_UPDATE_THROTTLE_SECONDS,
        model_override: str | None = None,
    ) -> ClaudeResult:
        model = model_override or self._model

        args: list[str] = [
            self._exe,
            "--print",
            "--verbose",
            "--output-format", "stream-json",
            "--permission-mode", self._permission_mode,
        ]
        if resume_session_id:
            args.extend(["--resume", resume_session_id])
        if model:
            args.extend(["--model", model])
        if self._max_budget > 0:
            args.extend(["--max-budget-usd", str(self._max_budget)])
        args.append(prompt)

        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                cwd=str(cwd),
                stdin=asyncio.subprocess.DEVNULL,
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

        tool_lines: list[str] = []
        current_text: list[str] = []
        final_payload: dict | None = None
        last_update = 0.0

        async def maybe_emit(force: bool = False) -> None:
            nonlocal last_update
            if on_update is None:
                return
            now = time.monotonic()
            if not force and now - last_update < update_throttle_seconds:
                return
            last_update = now
            snapshot = _render_progress(update_header, tool_lines, "".join(current_text))
            try:
                await on_update(snapshot)
            except Exception as e:
                log.warning("on_update raised: %s", e)

        stderr_chunks: list[bytes] = []

        async def drain_stderr() -> None:
            assert proc.stderr is not None
            while True:
                chunk = await proc.stderr.read(4096)
                if not chunk:
                    break
                stderr_chunks.append(chunk)

        stderr_task = asyncio.create_task(drain_stderr())

        async def read_loop() -> None:
            assert proc.stdout is not None
            nonlocal final_payload
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                try:
                    evt = json.loads(line)
                except json.JSONDecodeError:
                    log.warning("non-JSON stream line: %r", line[:200])
                    continue
                t = evt.get("type")
                if t == "assistant":
                    msg = evt.get("message", {}) or {}
                    for part in msg.get("content", []) or []:
                        ptype = part.get("type")
                        if ptype == "text":
                            text = part.get("text") or ""
                            if text:
                                if current_text and not current_text[-1].endswith("\n"):
                                    current_text.append("\n")
                                current_text.append(text)
                        elif ptype == "tool_use":
                            tool_lines.append(
                                _format_tool_use(
                                    part.get("name", "?"), part.get("input") or {}
                                )
                            )
                    await maybe_emit()
                elif t == "result":
                    final_payload = evt
                    break

        try:
            await asyncio.wait_for(read_loop(), timeout=self._timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            await stderr_task
            return ClaudeResult(
                False, "", None, None, None, None,
                error=f"claude timed out after {self._timeout}s",
            )

        await stderr_task
        await proc.wait()

        stderr_text = b"".join(stderr_chunks).decode("utf-8", errors="replace").strip()
        prompt_preview = (prompt[:120] + "…") if len(prompt) > 120 else prompt

        if final_payload is not None:
            is_error = bool(final_payload.get("is_error"))
            text = str(final_payload.get("result", "")).strip()
            if proc.returncode != 0 or is_error:
                log.warning(
                    "claude finished with issues: returncode=%s is_error=%s "
                    "subtype=%s stop_reason=%s api_error_status=%s "
                    "permission_denials=%s num_turns=%s duration_ms=%s "
                    "result_text=%r prompt=%r stderr=%r",
                    proc.returncode, is_error, final_payload.get("subtype"),
                    final_payload.get("stop_reason"),
                    final_payload.get("api_error_status"),
                    final_payload.get("permission_denials"),
                    final_payload.get("num_turns"),
                    final_payload.get("duration_ms"),
                    text[:800], prompt_preview, stderr_text[:500],
                )
            return ClaudeResult(
                ok=not is_error and proc.returncode == 0,
                text=text,
                session_id=final_payload.get("session_id"),
                cost_usd=final_payload.get("total_cost_usd"),
                duration_ms=final_payload.get("duration_ms"),
                raw=final_payload,
                num_turns=final_payload.get("num_turns"),
                error=(
                    f"claude error (subtype={final_payload.get('subtype')}, "
                    f"returncode={proc.returncode}):\n"
                    f"{text[:1200] or stderr_text[:1200] or '(no detail)'}"
                    if is_error or proc.returncode != 0
                    else None
                ),
                context_overflow=is_error and _is_context_overflow(text),
            )

        if proc.returncode != 0:
            log.warning(
                "claude exited non-zero with no result event: returncode=%s "
                "cwd=%s resume=%s prompt=%r stderr=%r",
                proc.returncode, cwd, resume_session_id, prompt_preview,
                stderr_text[:1000],
            )
            return ClaudeResult(
                False, "", None, None, None, None,
                error=(
                    f"claude exited {proc.returncode}\n"
                    f"stderr:\n{stderr_text[:1500] or '(empty)'}"
                ),
            )

        log.warning(
            "claude stream ended without result event: returncode=%s prompt=%r stderr=%r",
            proc.returncode, prompt_preview, stderr_text[:500],
        )
        return ClaudeResult(
            False, "", None, None, None, None,
            error=f"claude stream ended without result event (returncode={proc.returncode})",
        )
