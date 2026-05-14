"""Telegram bot that proxies messages into Claude Code CLI."""
from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from html import escape as html_escape

from telegram import BotCommand, Update
from telegram.constants import ChatAction, ParseMode
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

BOT_COMMANDS = [
    BotCommand("status", "顯示 session / 專案 / 模型 / 今日用量"),
    BotCommand("usage", "只顯示今日用量"),
    BotCommand("projects", "列出可用專案"),
    BotCommand("project", "切換專案，用法: /project <name>"),
    BotCommand("model", "顯示/切換模型，用法: /model [name|default]"),
    BotCommand("cancel", "取消當前執行中的請求"),
    BotCommand("reset", "清空 session，下一則訊息開新對話"),
    BotCommand("retry", "重跑上一則 prompt（會清掉 session 開新對話）"),
    BotCommand("help", "顯示指令說明"),
]


async def _register_commands(app: "Application") -> None:
    await app.bot.set_my_commands(BOT_COMMANDS)
    log.info("registered %d bot commands to Telegram menu", len(BOT_COMMANDS))

from claude_runner import ClaudeResult, ClaudeRunner
from config import Config
from keep_awake import KeepAwake
from session_store import SessionStore
from usage_tracker import UsageTracker

TELEGRAM_MSG_LIMIT = 4000  # 4096 hard cap; leave headroom for escapes
log = logging.getLogger("remotetools")


class Bot:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.store = SessionStore(cfg.state_dir / "sessions.json")
        self.usage = UsageTracker(
            path=cfg.state_dir / "usage.json",
            daily_limit=cfg.daily_message_limit,
            rpm_limit=cfg.rate_limit_per_minute,
        )
        self.runner = ClaudeRunner(
            permission_mode=cfg.permission_mode,
            timeout_seconds=cfg.timeout_seconds,
            claude_model=cfg.claude_model,
            max_budget_usd=cfg.max_budget_usd,
        )
        self._chat_locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._running: dict[int, asyncio.subprocess.Process] = {}
        self._cancelled: set[int] = set()

    # ---------- access control ----------

    def _is_allowed(self, chat_id: int) -> bool:
        if not self.cfg.allowed_chat_ids:
            return False
        return chat_id in self.cfg.allowed_chat_ids

    async def _reject_unknown(self, update: Update) -> bool:
        chat_id = update.effective_chat.id
        if self._is_allowed(chat_id):
            return False
        log.warning("rejected message from chat_id=%s (not whitelisted)", chat_id)
        await update.effective_message.reply_text(
            f"未授權。你的 chat_id 是 <code>{chat_id}</code>。\n"
            f"請把這個 ID 加進 .env 的 ALLOWED_CHAT_IDS 後重啟 bot。",
            parse_mode=ParseMode.HTML,
        )
        return True

    # ---------- commands ----------

    async def cmd_start(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if await self._reject_unknown(update):
            return
        await update.message.reply_text(
            "Claude Code 遠端控制 bot 啟動。\n"
            "傳訊息 = 直接送給 Claude。指令：\n"
            "/status - session / 專案 / 模型 / 今日用量\n"
            "/usage - 只看今日用量\n"
            "/projects (/list) - 列出可用專案\n"
            "/project <name> - 切換專案（會清空當前 session）\n"
            "/model [name] - 顯示/切換模型（opus/sonnet/haiku 或 claude-...；`default` 回 .env 設定）\n"
            "/cancel - 取消當前正在執行的請求\n"
            "/reset - 清空 session，下一則訊息開新對話\n"
            "/retry - 重跑上一則 prompt（會清掉 session 開新對話）\n"
            "/help - 顯示此說明"
        )

    async def cmd_help(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        await self.cmd_start(update, ctx)

    async def cmd_projects(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if await self._reject_unknown(update):
            return
        lines = [f"- <b>{html_escape(name)}</b>: <code>{html_escape(str(path))}</code>"
                 for name, path in self.cfg.projects.items()]
        await update.message.reply_text(
            "可用專案：\n" + "\n".join(lines), parse_mode=ParseMode.HTML
        )

    async def cmd_project(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if await self._reject_unknown(update):
            return
        chat_id = update.effective_chat.id
        if not ctx.args:
            state = self.store.get(chat_id, self.cfg.default_project)
            await update.message.reply_text(f"當前專案：{state.project}")
            return
        name = ctx.args[0].strip()
        if name not in self.cfg.projects:
            await update.message.reply_text(
                f"找不到專案 {name!r}。可用：{', '.join(self.cfg.projects)}"
            )
            return
        self.store.set_project(chat_id, name)
        await update.message.reply_text(
            f"已切換到 {name}（session 已清空，下一則訊息開新對話）"
        )

    async def cmd_status(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if await self._reject_unknown(update):
            return
        chat_id = update.effective_chat.id
        state = self.store.get(chat_id, self.cfg.default_project)
        snap = self.usage.snapshot(chat_id)
        sid = state.session_id or "（未建立，下一則訊息會開新對話）"
        path = self.cfg.projects.get(state.project, "?")
        model = state.model or self.cfg.claude_model or "(CLI 預設)"

        if snap.daily_limit > 0:
            daily_line = f"今日訊息: <b>{snap.messages}</b> / {snap.daily_limit}"
        else:
            daily_line = f"今日訊息: <b>{snap.messages}</b>（無上限）"

        rpm_line = (
            f"速率限制: {self.cfg.rate_limit_per_minute} 則/分"
            if self.cfg.rate_limit_per_minute > 0
            else "速率限制: 無"
        )

        await update.message.reply_text(
            f"chat_id: <code>{chat_id}</code>\n"
            f"專案: <b>{html_escape(state.project)}</b>\n"
            f"路徑: <code>{html_escape(str(path))}</code>\n"
            f"模型: <code>{html_escape(model)}</code>\n"
            f"session: <code>{html_escape(str(sid))}</code>\n"
            f"權限模式: <code>{html_escape(self.cfg.permission_mode)}</code>\n"
            f"\n"
            f"📊 用量（{snap.date}）\n"
            f"{daily_line}\n"
            f"{rpm_line}\n"
            f"今日累計: <b>${snap.cost_usd:.4f}</b>（Max 配額代理指標）",
            parse_mode=ParseMode.HTML,
        )

    async def cmd_model(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if await self._reject_unknown(update):
            return
        chat_id = update.effective_chat.id
        if not ctx.args:
            state = self.store.get(chat_id, self.cfg.default_project)
            effective = state.model or self.cfg.claude_model or "(claude CLI 預設)"
            source = (
                "session override" if state.model
                else ".env" if self.cfg.claude_model
                else "CLI 預設"
            )
            await update.message.reply_text(
                f"當前模型：<code>{html_escape(effective)}</code>（來源：{source}）\n"
                f"切換：<code>/model &lt;opus|sonnet|haiku|claude-...&gt;</code>\n"
                f"重置回 .env 設定：<code>/model default</code>",
                parse_mode=ParseMode.HTML,
            )
            return
        name_norm = ctx.args[0].strip().lower()
        if name_norm in ("default", "reset", "clear", "-"):
            self.store.set_model(chat_id, None)
            fallback = self.cfg.claude_model or "(claude CLI 預設)"
            await update.message.reply_text(
                f"已重置模型為 .env 設定：{fallback}"
            )
            return
        if not (name_norm in ("opus", "sonnet", "haiku") or name_norm.startswith("claude-")):
            await update.message.reply_text(
                f"無效的模型名稱 {name_norm!r}。請用 opus / sonnet / haiku，"
                f"或完整 ID（claude-...）。"
            )
            return
        self.store.set_model(chat_id, name_norm)
        await update.message.reply_text(
            f"已切換到 {name_norm}（session 保留，下次呼叫起套用）"
        )

    async def cmd_retry(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if await self._reject_unknown(update):
            return
        chat_id = update.effective_chat.id
        state = self.store.get(chat_id, self.cfg.default_project)
        if not state.last_prompt:
            await update.message.reply_text("沒有上一則 prompt 可重跑。")
            return
        self.store.reset(chat_id)
        await self._run_prompt(update, ctx, chat_id, state.last_prompt)

    async def cmd_usage(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if await self._reject_unknown(update):
            return
        chat_id = update.effective_chat.id
        snap = self.usage.snapshot(chat_id)
        if snap.daily_limit > 0:
            daily_line = f"今日訊息: <b>{snap.messages}</b> / {snap.daily_limit}"
        else:
            daily_line = f"今日訊息: <b>{snap.messages}</b>（無上限）"
        rpm_line = (
            f"速率限制: {self.cfg.rate_limit_per_minute} 則/分"
            if self.cfg.rate_limit_per_minute > 0
            else "速率限制: 無"
        )
        await update.message.reply_text(
            f"📊 用量（{snap.date}）\n"
            f"{daily_line}\n"
            f"{rpm_line}\n"
            f"今日累計: <b>${snap.cost_usd:.4f}</b>",
            parse_mode=ParseMode.HTML,
        )

    async def cmd_cancel(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if await self._reject_unknown(update):
            return
        chat_id = update.effective_chat.id
        proc = self._running.get(chat_id)
        if proc is None:
            await update.message.reply_text("目前沒有正在執行的請求。")
            return
        self._cancelled.add(chat_id)
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        except Exception as e:
            log.warning("cancel kill failed: %s", e)
        await update.message.reply_text("⏹ 取消訊號已送出。")

    async def cmd_reset(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if await self._reject_unknown(update):
            return
        chat_id = update.effective_chat.id
        self.store.reset(chat_id)
        await update.message.reply_text("Session 已清空。下一則訊息會開新對話。")

    # ---------- main message flow ----------

    async def on_message(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if await self._reject_unknown(update):
            return
        chat_id = update.effective_chat.id
        prompt = (update.message.text or "").strip()
        if not prompt:
            return
        await self._run_prompt(update, ctx, chat_id, prompt)

    async def _run_prompt(
        self,
        update: Update,
        ctx: ContextTypes.DEFAULT_TYPE,
        chat_id: int,
        prompt: str,
    ) -> None:
        check = self.usage.check_and_reserve(chat_id)
        if not check.ok:
            if check.reason == "rate":
                await update.message.reply_text(
                    f"太頻繁了。請等 {check.retry_after_seconds} 秒後再試。"
                )
            elif check.reason == "daily":
                await update.message.reply_text(
                    f"今日額度已滿（{check.daily_used}/{check.daily_limit}）。"
                    f"明天 0 點重置，或在 .env 改 DAILY_MESSAGE_LIMIT。"
                )
            return

        async with self._chat_locks[chat_id]:
            state = self.store.get(chat_id, self.cfg.default_project)
            cwd = self.cfg.projects[state.project]

            await update.effective_chat.send_action(ChatAction.TYPING)
            header = f"執行中… ({state.project})"
            placeholder = await update.message.reply_text(header)

            self.store.set_last_prompt(chat_id, prompt)

            keepalive = asyncio.create_task(self._keep_typing(update.effective_chat.id, ctx))

            last_rendered: list[str] = [""]

            async def _on_update(snapshot: str) -> None:
                content = snapshot if len(snapshot) <= TELEGRAM_MSG_LIMIT \
                    else snapshot[:TELEGRAM_MSG_LIMIT - 1] + "…"
                if content == last_rendered[0]:
                    return
                last_rendered[0] = content
                try:
                    await placeholder.edit_text(content)
                except BadRequest as e:
                    if "not modified" not in str(e).lower():
                        log.warning("progress edit failed: %s", e)
                except Exception as e:
                    log.warning("progress edit failed: %s", e)

            def _on_started(p: asyncio.subprocess.Process) -> None:
                self._running[chat_id] = p

            try:
                result = await self.runner.run(
                    prompt=prompt,
                    cwd=cwd,
                    resume_session_id=state.session_id,
                    on_started=_on_started,
                    on_update=_on_update,
                    update_header=header,
                    model_override=state.model,
                )
            finally:
                keepalive.cancel()
                self._running.pop(chat_id, None)

            if chat_id in self._cancelled:
                self._cancelled.discard(chat_id)
                try:
                    await placeholder.edit_text("⏹ 已取消")
                except Exception as e:
                    log.warning("cancel placeholder edit failed: %s", e)
                return

            if result.session_id:
                self.store.set_session_id(chat_id, result.session_id)
            if result.ok and result.cost_usd:
                self.usage.record_cost(chat_id, result.cost_usd)

            await self._deliver_result(update, placeholder, state.project, result)

    async def _keep_typing(self, chat_id: int, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        try:
            while True:
                await ctx.bot.send_chat_action(chat_id, ChatAction.TYPING)
                await asyncio.sleep(4)
        except asyncio.CancelledError:
            return
        except Exception as e:
            log.warning("typing keepalive failed: %s", e)

    async def _deliver_result(
        self,
        update: Update,
        placeholder,
        project: str,
        result: ClaudeResult,
    ) -> None:
        if not result.ok:
            if result.context_overflow:
                await placeholder.edit_text(
                    "⚠️ 對話累積已超過 context 上限，claude 拒絕了這則 prompt。\n"
                    "用 /reset 開新對話，或 /retry 自動 reset 並重跑同一個 prompt。"
                )
                return
            await placeholder.edit_text(f"錯誤：\n{result.error}"[:TELEGRAM_MSG_LIMIT])
            return

        body = result.text or "(claude 沒有回傳文字內容)"
        meta_parts = []
        if result.cost_usd is not None:
            meta_parts.append(f"${result.cost_usd:.4f}")
        if result.duration_ms is not None:
            meta_parts.append(f"{result.duration_ms / 1000:.1f}s")
        meta = f"\n\n— {project} | {' | '.join(meta_parts)}" if meta_parts else f"\n\n— {project}"

        chunks = self._chunk_text(body, TELEGRAM_MSG_LIMIT - len(meta))
        chunks[-1] = chunks[-1] + meta

        # Replace placeholder with first chunk; reply with the rest.
        await placeholder.edit_text(chunks[0])
        for extra in chunks[1:]:
            await update.message.reply_text(extra)

    @staticmethod
    def _chunk_text(text: str, limit: int) -> list[str]:
        if len(text) <= limit:
            return [text]
        chunks: list[str] = []
        remaining = text
        while len(remaining) > limit:
            cut = remaining.rfind("\n", 0, limit)
            if cut < limit // 2:
                cut = limit
            chunks.append(remaining[:cut])
            remaining = remaining[cut:].lstrip("\n")
        if remaining:
            chunks.append(remaining)
        return chunks

    # ---------- error handler ----------

    async def on_error(self, update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        log.exception("unhandled error", exc_info=context.error)
        if isinstance(update, Update) and update.effective_message:
            try:
                await update.effective_message.reply_text(
                    f"Bot 內部錯誤：{context.error}"[:TELEGRAM_MSG_LIMIT]
                )
            except Exception:
                pass


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s | %(message)s",
    )
    cfg = Config.load()
    bot = Bot(cfg)

    if not cfg.allowed_chat_ids:
        log.warning(
            "ALLOWED_CHAT_IDS is empty — bot will reject all messages. "
            "Send any message to bot to discover your chat_id from rejection log."
        )
    else:
        log.info("whitelist: %s", sorted(cfg.allowed_chat_ids))
    log.info("projects: %s", {k: str(v) for k, v in cfg.projects.items()})

    app = (
        Application.builder()
        .token(cfg.telegram_token)
        .post_init(_register_commands)
        .build()
    )
    app.add_handler(CommandHandler("start", bot.cmd_start))
    app.add_handler(CommandHandler("help", bot.cmd_help))
    app.add_handler(CommandHandler(["projects", "list"], bot.cmd_projects))
    app.add_handler(CommandHandler("project", bot.cmd_project))
    app.add_handler(CommandHandler("status", bot.cmd_status))
    app.add_handler(CommandHandler("usage", bot.cmd_usage))
    app.add_handler(CommandHandler("model", bot.cmd_model))
    app.add_handler(CommandHandler("cancel", bot.cmd_cancel))
    app.add_handler(CommandHandler("reset", bot.cmd_reset))
    app.add_handler(CommandHandler("retry", bot.cmd_retry))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, bot.on_message))
    app.add_error_handler(bot.on_error)

    log.info("bot starting (polling)")
    with KeepAwake():
        app.run_polling(allowed_updates=["message"])


if __name__ == "__main__":
    main()
