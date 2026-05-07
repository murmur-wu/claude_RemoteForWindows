# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Repository purpose

Bridge that lets a phone (Telegram, soon Discord) send prompts to a `claude` CLI running on the user's Windows machine, then proxies the response back. Each chat gets a persistent Claude session, sandboxed per "project" (a working directory).

## Layout

```
C:\claude\remotetools\
├── telegram\        python-telegram-bot, prefix /, msg limit 4000
└── discord\         discord.py, prefix !, msg limit 1900
```

Each platform folder is a **self-contained Python app** with its own `.env`, `requirements.txt`, `.venv\`, and `state\`. The four core modules — `claude_runner.py`, `session_store.py`, `usage_tracker.py`, `keep_awake.py` — are **byte-identical copies** in both folders. Shared logic is **intentionally not extracted to a `core/` package** — duplication is currently cheap, and an abstraction would have to anticipate a third platform that may never exist. Re-evaluate if a third variant lands.

When fixing a bug in any of the four shared modules, update **both** copies. They drift only if you let them.

## Run / develop

Both folders share the same launcher:

```powershell
cd C:\claude\remotetools\telegram   # or \discord
.\start.ps1                         # creates .venv on first run, installs deps, launches bot
```

`start.ps1` uses `$PSScriptRoot`, so it always builds `.venv\` next to itself. There is **no test suite, no linter, no build step** — just `python bot.py`.

`.env` must exist before launch (copy from `.env.example`). Required keys per platform:

- **telegram**: `TELEGRAM_BOT_TOKEN`, `ALLOWED_CHAT_IDS`, `PROJECTS`, `DEFAULT_PROJECT`
- **discord**: `DISCORD_BOT_TOKEN`, `ALLOWED_USER_IDS`, `PROJECTS`, `DEFAULT_PROJECT`

To discover your id: leave the allowlist empty, send any message to the bot, the rejection log prints the id.

For Discord, the bot also needs `MESSAGE CONTENT INTENT` enabled in the [Developer Portal](https://discord.com/developers/applications) → Bot → Privileged Gateway Intents. Without it the bot connects but `message.content` arrives empty.

## Architecture

Both bots share the same shape; the SDK and id semantics differ.

Message flow on each incoming message:

1. Whitelist check — telegram uses `chat_id`, discord uses `author.id`.
2. `usage_tracker.check_and_reserve` enforces RPM + daily-message caps atomically (keyed on the user, not the conversation).
3. A **per-conversation `asyncio.Lock`** serializes requests — critical, because `--resume` races would corrupt session continuity.
4. `claude_runner.run` spawns `claude --print --output-format json [--resume <sid>] <prompt>` in the project's cwd, captures the JSON result.
5. `session_store.set_session_id` persists the new `session_id` returned by claude — this is what makes the next message continue the same conversation.
6. Output is chunked and delivered: 4000 chars on Telegram, 1900 on Discord.

### Discord trigger model + session keys

The discord bot has **three different keys** for what telegram bundles into `chat_id`:

| concern | key | rationale |
|---|---|---|
| whitelist (`ALLOWED_USER_IDS`) | `author.id` | who is allowed to drive the bot |
| usage caps (RPM, daily, cost) | `author.id` | quota protection is per-person |
| session, lock, running/cancelled | `session_key` (see below) | each thread is an independent conversation |

`session_key` is decided by `_session_key(channel, author_id)`:
- `DMChannel` → `author.id`
- `Thread`    → `channel.id`
- regular guild channel → `None` (commands like `!cancel`/`!reset`/`!project` reject with a hint; the Claude pipeline opens a thread first via `message.create_thread()` and then uses the new thread's id)

When does `_should_respond` fire?
- DM: always
- Bot-owned thread (`channel.owner_id == self.user.id`): always
- Anywhere else: only when `self.user in message.mentions`

`<@bot_id>` mentions are stripped from the prompt before being sent to Claude (`MENTION_RE` in `bot.py`).

### Discord plumbing

- `discord.ext.commands.Bot` subclass with prefix from `COMMAND_PREFIX` (default `!`); `on_message` is overridden to route prefix→`process_commands`, otherwise gate on `_should_respond`.
- Commands are a single `RemoteCog`. The bot subclass owns the runtime state (`store`, `usage`, `runner`, `_chat_locks`, `_running`, `_cancelled`); the cog reaches into `self.bot` for those.
- `intents.message_content = True` is mandatory in code; the matching toggle in the Dev Portal is also mandatory or `message.content` arrives empty.
- `async with target.typing():` handles indicator keepalive automatically — no manual loop like in the Telegram bot.
- Reply meta is rendered as `*turns=N · 2.8s · $0.0042*` (italic, joined with `·`); `num_turns` comes from claude's JSON `num_turns` field.

### Critical: `PERMISSION_MODE`

The bot has no stdin attached to claude. **Only `auto` and `bypassPermissions` work.** `default` and `acceptEdits` will hang forever waiting for an interactive prompt nobody can answer. The `.env.example` documents this; preserve those comments if editing.

### Why `usage_tracker.py` exists

Not just rate limiting — it protects the user's Claude Max **5-hour quota**. Automation calls claude faster than humans; a runaway loop or spam can burn the daily quota in 30 minutes. Don't remove `DAILY_MESSAGE_LIMIT` or `RATE_LIMIT_PER_MINUTE` enforcement without explicit user instruction.

### `keep_awake.py`

Calls Win32 `SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)` so Windows doesn't sleep mid-request. No-op on non-Windows. Per-process — exits cleanly when the bot dies.

### Cancel / reset semantics

- `/cancel` kills the live `claude` subprocess via `_running[chat_id].kill()` and marks the chat in `_cancelled` so the result is discarded instead of delivered.
- `/reset` and `/project <name>` both **null out the stored `session_id`** — the next message starts a fresh claude conversation.

### State files

- `telegram\state\sessions.json` — `{chat_id: {session_id, project}}`, written via tmp-file rename.
- `telegram\state\usage.json` — daily message counts and cost totals, auto-resets on date change.
- Both gitignored (root `.gitignore` uses `**/state/*.json` to cover future platform folders).

## Adding a third platform

If a third variant ever appears, that's the trigger to extract `core/` (housing `claude_runner`, `session_store`, `usage_tracker`, `keep_awake`). Until then, copy-paste from either folder is the rule.

## User context

- User communicates in 繁體中文; technical terms / code / logs stay English.
- Primary shell is PowerShell on Windows 11. Use PowerShell syntax (`$env:VAR`, not `$VAR`) when suggesting commands.
- Common target project this bot drives: `C:\Web\OnlinePrint-Production` (configured via `PROJECTS=` in `.env`).
