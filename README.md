# remotetools

從手機遠端指揮家裡電腦上的 [Claude Code](https://docs.claude.com/en/docs/claude-code) — 透過 Telegram 或 Discord bot 把訊息丟進本機的 `claude` CLI，回應丟回手機。

> **使用情境**：人在外面突然想到「啊，那個專案的 README 要加一段」，掏手機傳訊息給 bot，家裡電腦的 Claude Code 自動 commit。

```
你 (📱)  ──訊息──▶  Telegram / Discord  ──polling──▶  bot.py  ──subprocess──▶  claude --print
                                                         ▲                          │
                                                         └──── stdout JSON ─────────┘
                                                         │
                                                         ▼
                                                   家裡的 repo（自動 edit / git / build）
```

---

## 平台

| 平台 | 資料夾 | SDK | 訊息上限 | 指令前綴 |
|---|---|---|---|---|
| Telegram | `telegram/` | `python-telegram-bot` 21.9 | 4000 字 | `/` |
| Discord | `discord/` | `discord.py` 2.4.0 | 1900 字 | `!`（可設定） |

兩個資料夾各自獨立、各自 `.venv`、各自 `.env`，可以只跑其中一個或同時跑。

## 必要條件

- Windows（其他平台也能跑，但 `keep_awake.py` 只在 Windows 生效）
- Python 3.10+
- 已安裝並能在 PATH 上找到的 `claude` CLI（[安裝說明](https://docs.claude.com/en/docs/claude-code/setup)）
- Telegram bot token（找 [@BotFather](https://t.me/BotFather)） **或** Discord bot token（[Developer Portal](https://discord.com/developers/applications)）

---

## Quick start — Telegram

```powershell
cd telegram
copy .env.example .env
# 編輯 .env，至少填 TELEGRAM_BOT_TOKEN 跟 PROJECTS
.\start.ps1
```

第一次跑會建 `.venv` 並裝套件。啟動後：

1. `ALLOWED_CHAT_IDS` 留空 → 對 bot 傳任意訊息
2. console log 會印 `rejected message from chat_id=XXXXX (not whitelisted)`
3. 把那個數字填進 `.env` 的 `ALLOWED_CHAT_IDS`，重啟

## Quick start — Discord

Discord 多兩個必做設定：

1. Developer Portal → Bot → Privileged Gateway Intents → **打開 MESSAGE CONTENT INTENT**
   （不打開的話 bot 連得上但 `message.content` 永遠是空字串）
2. OAuth2 → URL Generator → Scopes 勾 `bot`、Permissions 勾 `Send Messages` + `Read Message History`，用產生的 URL 邀請 bot，或讓 bot 接受 DM

```powershell
cd discord
copy .env.example .env
# 編輯 .env，至少填 DISCORD_BOT_TOKEN 跟 PROJECTS
.\start.ps1
```

`ALLOWED_USER_IDS` 取得方式：Discord Settings → Advanced → 開 Developer Mode → 對自己頭像右鍵 Copy User ID，或先讓白名單留空、傳訊息看 console log。

---

## 指令

| Telegram | Discord | 說明 |
|---|---|---|
| `/status` | `!status` | 顯示 session id、當前專案、今日用量、成本 |
| `/usage` | `!usage` | 只看今日用量 |
| `/projects` | `!projects` | 列出 `.env` 裡定義的所有專案 |
| `/project <name>` | `!project <name>` | 切換 working directory（會清空 session） |
| `/cancel` | `!cancel` | 取消當前正在執行的 claude 請求 |
| `/reset` | `!reset` | 清空當前 session，下一則訊息開新對話 |
| `/help` | `!help` | 顯示指令清單 |

不以前綴開頭的純訊息 = 直接送進 `claude --print`，並用 `--resume` 接續上一個 session。

---

## ⚠️ 安全注意事項

這個 bot 等於把 Claude Code 開放執行的能力延伸到網路上。請認真看完：

### 1. 一定要設白名單
`ALLOWED_CHAT_IDS` / `ALLOWED_USER_IDS` 留空 = bot 拒絕所有人（fail-closed），這是預設且推薦的行為。**永遠不要**為了「方便測試」把白名單關掉。

### 2. `PERMISSION_MODE` 只能用 `auto` 或 `bypassPermissions`
Bot 沒有 stdin，無法回應 claude 的權限詢問：

| 模式 | 結果 |
|---|---|
| `default` | bot 卡死等 stdin |
| `acceptEdits` | 編輯自動過，但 bash 會卡死等 stdin |
| `auto` | ✅ 推薦。搭配 `~/.claude/settings.json` 的 deny 黑名單擋危險指令 |
| `bypassPermissions` | ⚠️ 全綠燈，連 deny 規則都跳過 |

### 3. 用 `~/.claude/settings.json` 的 deny 規則做最後防線
即使 `PERMISSION_MODE=auto`，settings 裡仍可以擋特定 bash pattern（如 `rm -rf /`、`curl ... | sh`、`shutdown` 等等）。Bot 用了它的權限模型，但你決定能跑什麼。

### 4. Claude Max 配額保護
`DAILY_MESSAGE_LIMIT`（預設 100）跟 `RATE_LIMIT_PER_MINUTE`（預設 6）不是反垃圾訊息——是防自己手抖或腳本爆走把 5 小時配額在半小時內燒掉。**沒有強烈理由不要關**。

### 5. `state/sessions.json` 跟 `.env` 含敏感資訊
- `.env` 有 bot token
- `state/sessions.json` 有 chat/user id 跟 claude session id
兩個都已被 `.gitignore`。要備份的話請加密。

---

## 架構

```
remotetools/
├── telegram/
├── discord/
└── CLAUDE.md      ← 給未來改 code 的 Claude agent 看
```

每個平台資料夾長這樣：

```
<platform>/
├── bot.py              ← SDK-specific 的訊息處理 + 命令
├── config.py           ← 載入 .env
├── claude_runner.py    ← async wrapper 包 `claude --print` subprocess
├── session_store.py    ← 把 session_id 寫進 state/sessions.json
├── usage_tracker.py    ← RPM + 每日訊息上限 + 成本累計
├── keep_awake.py       ← Win32 SetThreadExecutionState，防系統睡眠
├── requirements.txt
├── start.ps1           ← 建 venv、裝套件、跑 bot.py
├── .env.example
└── state/              ← 執行時自動建立
    ├── sessions.json
    └── usage.json
```

訊息進來的流程：白名單檢查 → 用量限制檢查（atomic reserve）→ per-chat asyncio lock → spawn `claude --print --output-format json --resume <sid>` → 解析 JSON → 持久化新的 session_id → 切片回傳。

每個 chat 一把獨立的 lock，避免同一個 chat 同時呼叫 claude 造成 `--resume` race。

### 為什麼 telegram/ 跟 discord/ 各有一份 claude_runner、session_store、usage_tracker、keep_awake？

刻意這樣設計。兩個平台的這四個檔案目前 **byte-identical**，但沒抽到 `core/` 共用 package。理由：

- 抽出來會讓部署從「進資料夾跑 start.ps1」變成「裝兩層東西」
- 真的有第三個平台再來抽，那時才知道介面該長怎樣
- 修 bug 時兩邊都改一下，比設計錯誤的抽象便宜

---

## 開發提示

- 沒有測試、沒有 linter、沒有 build step。`bot.py` 直接跑就是。
- 改 code 後重啟 bot 就生效；session 會延續（`session_id` 在 `state/`）。
- 想看 claude 實際被怎麼呼叫：`claude_runner.py` 的 `args` list，可以印出來除錯。
- bot 在跑的時候，Windows 不會進入睡眠（`KeepAwake`），但螢幕還是會關。

## License

[MIT](LICENSE) © 2026 murmur-wu
