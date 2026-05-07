"""Discord bot that proxies messages into Claude Code CLI."""
from __future__ import annotations

import asyncio
import logging
from collections import defaultdict

import discord
from discord.ext import commands

from claude_runner import ClaudeResult, ClaudeRunner
from config import Config
from keep_awake import KeepAwake
from session_store import SessionStore
from usage_tracker import UsageTracker

DISCORD_MSG_LIMIT = 1900  # 2000 hard cap; leave headroom for meta line + edits
log = logging.getLogger("remotetools")


class RemoteCog(commands.Cog):
    def __init__(self, bot: "RemoteBot") -> None:
        self.bot = bot

    async def _reject_unknown(self, ctx: commands.Context) -> bool:
        if ctx.author.id in self.bot.cfg.allowed_user_ids:
            return False
        log.warning("rejected command from user_id=%s (not whitelisted)", ctx.author.id)
        await ctx.reply(
            f"未授權。你的 user_id 是 `{ctx.author.id}`。\n"
            f"請把這個 ID 加進 .env 的 ALLOWED_USER_IDS 後重啟 bot。"
        )
        return True

    @commands.command(name="help")
    async def cmd_help(self, ctx: commands.Context) -> None:
        if await self._reject_unknown(ctx):
            return
        p = self.bot.cfg.command_prefix
        await ctx.reply(
            "Claude Code 遠端控制 bot。\n"
            f"傳訊息 = 直接送給 Claude（不以 `{p}` 開頭時）。指令：\n"
            f"`{p}status` - session / 專案 / 今日用量\n"
            f"`{p}usage` - 只看今日用量\n"
            f"`{p}projects` (`{p}list`) - 列出可用專案\n"
            f"`{p}project <name>` - 切換專案（會清空當前 session）\n"
            f"`{p}cancel` - 取消當前正在執行的請求\n"
            f"`{p}reset` - 清空 session，下一則訊息開新對話\n"
            f"`{p}help` - 顯示此說明"
        )

    @commands.command(name="projects", aliases=["list"])
    async def cmd_projects(self, ctx: commands.Context) -> None:
        if await self._reject_unknown(ctx):
            return
        lines = [f"- **{name}**: `{path}`"
                 for name, path in self.bot.cfg.projects.items()]
        await ctx.reply("可用專案：\n" + "\n".join(lines))

    @commands.command(name="project")
    async def cmd_project(self, ctx: commands.Context, name: str | None = None) -> None:
        if await self._reject_unknown(ctx):
            return
        chat_id = ctx.author.id
        if name is None:
            state = self.bot.store.get(chat_id, self.bot.cfg.default_project)
            await ctx.reply(f"當前專案：`{state.project}`")
            return
        name = name.strip()
        if name not in self.bot.cfg.projects:
            await ctx.reply(
                f"找不到專案 `{name}`。可用：{', '.join(self.bot.cfg.projects)}"
            )
            return
        self.bot.store.set_project(chat_id, name)
        await ctx.reply(
            f"已切換到 `{name}`（session 已清空，下一則訊息開新對話）"
        )

    @commands.command(name="status")
    async def cmd_status(self, ctx: commands.Context) -> None:
        if await self._reject_unknown(ctx):
            return
        chat_id = ctx.author.id
        state = self.bot.store.get(chat_id, self.bot.cfg.default_project)
        snap = self.bot.usage.snapshot(chat_id)
        sid = state.session_id or "（未建立，下一則訊息會開新對話）"
        path = self.bot.cfg.projects.get(state.project, "?")

        if snap.daily_limit > 0:
            daily_line = f"今日訊息: **{snap.messages}** / {snap.daily_limit}"
        else:
            daily_line = f"今日訊息: **{snap.messages}**（無上限）"

        rpm_line = (
            f"速率限制: {self.bot.cfg.rate_limit_per_minute} 則/分"
            if self.bot.cfg.rate_limit_per_minute > 0
            else "速率限制: 無"
        )

        await ctx.reply(
            f"user_id: `{chat_id}`\n"
            f"專案: **{state.project}**\n"
            f"路徑: `{path}`\n"
            f"session: `{sid}`\n"
            f"權限模式: `{self.bot.cfg.permission_mode}`\n"
            f"\n"
            f"📊 用量（{snap.date}）\n"
            f"{daily_line}\n"
            f"{rpm_line}\n"
            f"今日累計: **${snap.cost_usd:.4f}**（Max 配額代理指標）"
        )

    @commands.command(name="usage")
    async def cmd_usage(self, ctx: commands.Context) -> None:
        if await self._reject_unknown(ctx):
            return
        chat_id = ctx.author.id
        snap = self.bot.usage.snapshot(chat_id)
        if snap.daily_limit > 0:
            daily_line = f"今日訊息: **{snap.messages}** / {snap.daily_limit}"
        else:
            daily_line = f"今日訊息: **{snap.messages}**（無上限）"
        rpm_line = (
            f"速率限制: {self.bot.cfg.rate_limit_per_minute} 則/分"
            if self.bot.cfg.rate_limit_per_minute > 0
            else "速率限制: 無"
        )
        await ctx.reply(
            f"📊 用量（{snap.date}）\n"
            f"{daily_line}\n"
            f"{rpm_line}\n"
            f"今日累計: **${snap.cost_usd:.4f}**"
        )

    @commands.command(name="cancel")
    async def cmd_cancel(self, ctx: commands.Context) -> None:
        if await self._reject_unknown(ctx):
            return
        chat_id = ctx.author.id
        proc = self.bot._running.get(chat_id)
        if proc is None:
            await ctx.reply("目前沒有正在執行的請求。")
            return
        self.bot._cancelled.add(chat_id)
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        except Exception as e:
            log.warning("cancel kill failed: %s", e)
        await ctx.reply("⏹ 取消訊號已送出。")

    @commands.command(name="reset")
    async def cmd_reset(self, ctx: commands.Context) -> None:
        if await self._reject_unknown(ctx):
            return
        self.bot.store.reset(ctx.author.id)
        await ctx.reply("Session 已清空。下一則訊息會開新對話。")


class RemoteBot(commands.Bot):
    def __init__(self, cfg: Config) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(
            command_prefix=cfg.command_prefix,
            intents=intents,
            help_command=None,
        )
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

    async def setup_hook(self) -> None:
        await self.add_cog(RemoteCog(self))

    async def on_ready(self) -> None:
        log.info("logged in as %s (id=%s)", self.user, self.user.id if self.user else "?")

    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot:
            return
        # Prefix command? hand to commands framework.
        if message.content.startswith(self.cfg.command_prefix):
            await self.process_commands(message)
            return
        # Otherwise treat as Claude prompt.
        await self._handle_claude(message)

    async def _handle_claude(self, message: discord.Message) -> None:
        chat_id = message.author.id

        if chat_id not in self.cfg.allowed_user_ids:
            log.warning("rejected message from user_id=%s (not whitelisted)", chat_id)
            try:
                await message.reply(
                    f"未授權。你的 user_id 是 `{chat_id}`。\n"
                    f"請把這個 ID 加進 .env 的 ALLOWED_USER_IDS 後重啟 bot。"
                )
            except discord.HTTPException as e:
                log.warning("reject reply failed: %s", e)
            return

        prompt = (message.content or "").strip()
        if not prompt:
            return

        check = self.usage.check_and_reserve(chat_id)
        if not check.ok:
            if check.reason == "rate":
                await message.reply(
                    f"太頻繁了。請等 {check.retry_after_seconds} 秒後再試。"
                )
            elif check.reason == "daily":
                await message.reply(
                    f"今日額度已滿（{check.daily_used}/{check.daily_limit}）。"
                    f"明天 0 點重置，或在 .env 改 DAILY_MESSAGE_LIMIT。"
                )
            return

        async with self._chat_locks[chat_id]:
            state = self.store.get(chat_id, self.cfg.default_project)
            cwd = self.cfg.projects[state.project]

            placeholder = await message.reply(f"執行中… ({state.project})")

            def _on_started(p: asyncio.subprocess.Process) -> None:
                self._running[chat_id] = p

            try:
                async with message.channel.typing():
                    result = await self.runner.run(
                        prompt=prompt,
                        cwd=cwd,
                        resume_session_id=state.session_id,
                        on_started=_on_started,
                    )
            finally:
                self._running.pop(chat_id, None)

            if chat_id in self._cancelled:
                self._cancelled.discard(chat_id)
                try:
                    await placeholder.edit(content="⏹ 已取消")
                except discord.HTTPException as e:
                    log.warning("cancel placeholder edit failed: %s", e)
                return

            if result.session_id:
                self.store.set_session_id(chat_id, result.session_id)
            if result.ok and result.cost_usd:
                self.usage.record_cost(chat_id, result.cost_usd)

            await self._deliver_result(message, placeholder, state.project, result)

    async def _deliver_result(
        self,
        message: discord.Message,
        placeholder: discord.Message,
        project: str,
        result: ClaudeResult,
    ) -> None:
        if not result.ok:
            await placeholder.edit(
                content=f"錯誤：\n{result.error}"[:DISCORD_MSG_LIMIT]
            )
            return

        body = result.text or "(claude 沒有回傳文字內容)"
        meta_parts = []
        if result.cost_usd is not None:
            meta_parts.append(f"${result.cost_usd:.4f}")
        if result.duration_ms is not None:
            meta_parts.append(f"{result.duration_ms / 1000:.1f}s")
        meta = (
            f"\n\n— {project} | {' | '.join(meta_parts)}"
            if meta_parts else f"\n\n— {project}"
        )

        chunks = self._chunk_text(body, DISCORD_MSG_LIMIT - len(meta))
        chunks[-1] = chunks[-1] + meta

        await placeholder.edit(content=chunks[0])
        for extra in chunks[1:]:
            await message.channel.send(extra)

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

    async def on_command_error(
        self, ctx: commands.Context, error: commands.CommandError
    ) -> None:
        if isinstance(error, commands.CommandNotFound):
            return
        log.exception("command error", exc_info=error)
        try:
            await ctx.reply(f"指令錯誤：{error}"[:DISCORD_MSG_LIMIT])
        except discord.HTTPException:
            pass


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s | %(message)s",
    )
    cfg = Config.load()

    if not cfg.allowed_user_ids:
        log.warning(
            "ALLOWED_USER_IDS is empty — bot will reject all messages. "
            "Send any message to bot to discover your user_id from rejection log."
        )
    else:
        log.info("whitelist: %s", sorted(cfg.allowed_user_ids))
    log.info("projects: %s", {k: str(v) for k, v in cfg.projects.items()})
    log.info("command prefix: %r", cfg.command_prefix)

    bot = RemoteBot(cfg)
    log.info("bot starting")
    with KeepAwake():
        bot.run(cfg.discord_token, log_handler=None)


if __name__ == "__main__":
    main()
