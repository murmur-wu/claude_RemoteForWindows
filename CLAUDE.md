# CLAUDE.md

本檔案為 Claude Code (claude.ai/code) 處理本 repo 時的指引。

## 專案目的

讓手機（Telegram、Discord）把 prompt 送到本機運行的 `claude` CLI，再把回應送回手機。每段對話有獨立且持續存活的 Claude session，並可依 "project"（一個 working directory）切換。

## 結構

```
C:\claude\remotetools\
├── telegram\        python-telegram-bot, prefix /, msg limit 4000
└── discord\         discord.py, prefix !, msg limit 1900
```

每個平台資料夾都是 **獨立的 Python app**，各有自己的 `.env`、`requirements.txt`、`.venv\`、`state\`。四個共用模組——`claude_runner.py`、`session_store.py`、`usage_tracker.py`、`keep_awake.py`——在兩邊是 **byte-identical 的複製**。**刻意不抽到 `core/` 共用 package**——目前複製成本低，過早抽象會被迫去猜「可能永遠不會出現」的第三平台介面。等真的出現第三平台再評估。

修四個共用模組的任何 bug 時，**兩邊都要改**，否則只會 drift。

## 執行 / 開發

兩個資料夾共用相同 launcher：

```powershell
cd C:\claude\remotetools\telegram   # 或 \discord
.\start.ps1                         # 首次跑會建 .venv、裝相依、啟動 bot
```

`start.ps1` 用 `$PSScriptRoot`，所以一定把 `.venv\` 建在自己旁邊。**沒有測試、沒有 linter、沒有 build step**——直接 `python bot.py`。

啟動前 `.env` 必須存在（從 `.env.example` 複製）。每平台必填 key：

- **telegram**：`TELEGRAM_BOT_TOKEN`、`ALLOWED_CHAT_IDS`、`PROJECTS`、`DEFAULT_PROJECT`
- **discord**：`DISCORD_BOT_TOKEN`、`ALLOWED_USER_IDS`、`PROJECTS`、`DEFAULT_PROJECT`

取得自己 id 的方法：把 allowlist 留空、傳任意訊息給 bot，rejection log 會印出 id。

Discord 還必須在 [Developer Portal](https://discord.com/developers/applications) → Bot → Privileged Gateway Intents 開啟 `MESSAGE CONTENT INTENT`。沒開的話 bot 連得上，但 `message.content` 一律會是空字串。

## 架構

兩個 bot 形狀相同；SDK 與 id 語意不同。

每則訊息進來的流程：

1. 白名單檢查——telegram 用 `chat_id`，discord 用 `author.id`。
2. `usage_tracker.check_and_reserve` 原子地檢查 RPM + 每日訊息上限（key 在 user，不在 conversation）。
3. **per-conversation 的 `asyncio.Lock`** 把同一段對話的請求序列化——關鍵防線，否則 `--resume` race 會弄壞 session 連續性。
4. `claude_runner.run` 在 project 的 cwd 底下 spawn `claude --print --verbose --output-format stream-json [--resume <sid>] [--model <name>] <prompt>`，逐行 parse JSONL 事件流（`assistant` / `result`），內部累積 tool calls + assistant text。
5. 若 caller 給了 `on_update` callback，runner 用 2 秒節流（`update_throttle_seconds`，預設 2.0）呼叫它推 progress snapshot——bot 把 callback 包成 `placeholder.edit(content=...)` 實作 streaming UI。沒給 callback 就跟舊版「跑完才回」等價。
6. `session_store.set_session_id` 持久化 claude `result` event 的 `session_id`；`set_last_prompt` 同步存原始 prompt（給 `!retry` 用）。
7. 輸出切片送出：Telegram 4000 字、Discord 1900 字。

### Discord 觸發模型 + session keys

discord bot 把 telegram 的單一 `chat_id` 拆成 **三組 key**：

| 用途 | key | 理由 |
|---|---|---|
| 白名單（`ALLOWED_USER_IDS`） | `author.id` | 誰能驅動 bot |
| 用量上限（RPM、每日、成本） | `author.id` | 配額保護是 per-person |
| session、lock、running/cancelled | `session_key`（見下） | 每個 thread 是獨立對話 |

`session_key` 由 `_session_key(channel, author_id)` 決定：
- `DMChannel` → `author.id`
- `Thread`    → `channel.id`
- 一般 guild channel → `None`（`!cancel`/`!reset`/`!project` 等指令會 reject 並提示；Claude pipeline 會先用 `message.create_thread()` 開 thread，再用新 thread 的 id 當 session_key）

`_should_respond` 何時觸發？
- DM：永遠
- bot 自己建的 thread（`channel.owner_id == self.user.id`）：永遠
- 其他地方：僅當 `self.user in message.mentions`

`<@bot_id>` mention 會在 prompt 送進 Claude 前剝掉（`bot.py` 的 `MENTION_RE`）。

### Discord 細節

- `discord.ext.commands.Bot` 的 subclass，prefix 來自 `COMMAND_PREFIX`（預設 `!`）；`on_message` 被 override 來路由 prefix→`process_commands`，否則經 `_should_respond` 判斷。
- 所有指令集中在 `RemoteCog`。Bot subclass 持有 runtime state（`store`、`usage`、`runner`、`_chat_locks`、`_running`、`_cancelled`）；cog 透過 `self.bot` 拿。
- `intents.message_content = True` 在 code 是必須；Dev Portal 對應 toggle 也必須打開，否則 `message.content` 永遠是空字串。
- `async with target.typing():` 自動處理 typing indicator keepalive——不像 Telegram 那邊得手動 loop。
- 回覆 meta 渲染成 `*turns=N · 2.8s · $0.0042*`（italic、`·` 連接）；`num_turns` 來自 claude `result` event 的 `num_turns` 欄位。
- Streaming：placeholder 訊息會在 claude 跑的過程被 `_on_update` 每 ~2 秒 edit 一次，呈現「最近 12 筆 tool 呼叫 + claude 當前累積 text 末尾 1200 字」；跑完後被 `_deliver_result` 覆蓋為最終答覆。附件下載的 warning 是另外 `target.send`，不會覆蓋 placeholder。

### 重要：`PERMISSION_MODE`

bot 沒有把 stdin 接到 claude，**也沒人能在手機端按 `/permissions Always Allow`**——所以「會問問題的模式」都會卡。

- `default` / `acceptEdits`：永遠卡在等 interactive prompt（沒人能回答）→ **不要用**
- `auto`：依 `~/.claude/settings.json` 的 allow/deny 規則決定。**沒在 allow 內的工具（含未授權目錄的 Write）會被擋，claude 回 `permission_denied` 後當次任務失敗。** 適合「settings.json allow 表已經很完整」的設定狀況；不適合「希望 bot 摸到啥都能跑」的手機遠端 use case。
- `bypassPermissions`：完全跳過權限檢查 → **手機遠端的推薦模式**。失去 settings.json 的 deny safety net，所以這個選擇明確承擔資安信任（個人裝置、白名單已鎖 `ALLOWED_USER_IDS` 的場景 OK）。

`.env.example` 有記載這權衡，預設 `bypassPermissions`；編輯時請保留註解。

### `usage_tracker.py` 為什麼存在

不只是 rate limit——它保護用戶的 Claude Max **5 小時配額**。自動化呼叫 claude 比人手快很多；一個失控 loop 或被洗版能在 30 分鐘內燒完每日配額。**沒有明確指示時不要拿掉** `DAILY_MESSAGE_LIMIT` 或 `RATE_LIMIT_PER_MINUTE` 的執行。

### `keep_awake.py`

呼叫 Win32 `SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)`，避免 Windows 在請求中途睡眠。非 Windows 平台 no-op。Per-process——bot 死了就乾淨退回正常 sleep 行為。

### Cancel / reset / retry / model 語意

- `/cancel`（discord 是 `!cancel`）會用 `_running[key].kill()` 殺掉正在跑的 `claude` subprocess，並把該對話標進 `_cancelled`，讓結果直接丟掉、不送出。
- `/reset` 跟 `/project <name>` 都會 **把存好的 `session_id` 清成 None**——下一則訊息開全新 claude 對話。`last_prompt` 跟 `model` 不會被清（語意：「重置 session」不等於「忘記模型偏好」）。
- `/retry`（discord `!retry`）：reset session + 重跑 `state.last_prompt`，相當於同 prompt 開全新 session。沒有 `last_prompt`（純附件訊息或從未送過 prompt）時報錯。
- `/model [name]`（discord `!model`）：無參數顯示當前模型 + 來源（session override / .env / CLI 預設）；接受 `opus` / `sonnet` / `haiku` alias 或 `claude-...` 完整 ID；用 `default`（或 `-` / `reset` / `clear`）清回 .env 設定。切模型**不**重置 session（`claude --resume` 跨 model 沿用對話脈絡）。

### State 檔

- `<platform>\state\sessions.json`——`{key: {session_id, project, last_prompt, model}}`，用 tmp file rename 寫入。`session_store._load` 用 `_VALID_FIELDS` filter 容忍未來新舊欄位差異（舊資料缺欄位用 dataclass default，未知 key 直接丟掉）。
- `<platform>\state\usage.json`——每日訊息數、累計成本，依日期自動 reset。
- `discord\state\uploads\<session_key>\<message_id>_<filename>`——Discord 附件下載地（25 MB/檔上限）。telegram 那邊還沒做附件支援，沒這個目錄。
- 上面三類都被 gitignored：JSON 經 `**/state/*.json`，uploads 經 `**/state/uploads/`。

## 跑成 Windows 服務

`services\` 內有用 NSSM 包成服務的腳本。流程：

1. 從 https://nssm.cc/download 下載 win64 zip，把 `nssm.exe` 丟進 `services\`（也可用 `-NssmPath` 指定別處）。
2. 兩個 bot 都要先各跑一次 `start.ps1` 把 `.venv\` 跟 `.env` 建好。
3. **以系統管理員身分**開 PowerShell：
   ```powershell
   cd C:\claude\remotetools\services
   .\install-service.ps1                 # 兩個都裝
   .\install-service.ps1 -Bot telegram   # 只裝其一
   ```
   會跳 `Get-Credential` 視窗——**必須輸入登入用戶的 Windows 帳密**，不能用 LocalSystem。原因：`claude` CLI 認證在 `%USERPROFILE%\.claude\`，SYSTEM 帳號完全找不到。

裝完會註冊成：
- `ClaudeRemoteTelegram`
- `ClaudeRemoteDiscord`

特性：boot auto-start、crash 後 5 秒重啟、stdout/stderr 寫到 `services\logs\<bot>.{out,err}.log`、10MB 自動 rotate。

操作：
```powershell
Get-Service ClaudeRemote*
Restart-Service ClaudeRemoteTelegram
Get-Content services\logs\telegram.out.log -Tail 50 -Wait
.\uninstall-service.ps1
```

改 `.env` 後要 `Restart-Service` 才會生效。`nssm.exe` 跟 `services\logs\` 都已 gitignored。

## 加第三個平台時

真的出現第三個平台，就是抽 `core/`（容納 `claude_runner`、`session_store`、`usage_tracker`、`keep_awake`）的時機。在那之前，從任一邊複製貼上即可。

## 用戶資訊

- 用戶以繁體中文溝通；技術詞 / code / log 保留英文。
- 主要 shell 是 Windows 11 的 PowerShell。建議命令請用 PowerShell 語法（`$env:VAR`，不是 `$VAR`）。
- 此 bot 常驅動的目標 project：`C:\Web\OnlinePrint-Production`（在 `.env` 的 `PROJECTS=` 設定）。
