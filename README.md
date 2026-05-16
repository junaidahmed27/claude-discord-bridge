# Claude Code ← Discord bridge

Send a message in a Discord channel → `claude -p "<your message>"` runs on this
Mac → output is posted back in the same channel.

```
[Discord channel]  ──message──▶  bot (listener.py, LaunchAgent)
                                     │
                                     ▼
                          claude -p "..."  in your project dir
                                     │
                                     ▼
[Discord channel]  ◀──reply───  stdout (or attachment if long)
```

## What this includes

```
~/.claude-discord/
├── config.example.json   ← copy → config.json, fill in
├── listener.py           ← the bot
├── requirements.txt      ← discord.py
├── com.junaidahmed.claude-discord.plist  ← LaunchAgent
├── install.sh            ← venv + LaunchAgent installer
├── uninstall.sh
└── README.md             ← this file
```

---

## Part A — One-time Discord setup (~10 min)

### A1. Turn on Developer Mode
Discord → **Settings (gear) → Advanced → Developer Mode: ON**.
You need this to copy channel and user IDs.

### A2. Create the bot

1. Open <https://discord.com/developers/applications> → **New Application**.
2. Name: `claude-bridge` (or anything). Click **Create**.
3. Left sidebar → **Bot**.
4. **Privileged Gateway Intents** section → toggle **MESSAGE CONTENT INTENT** to ON. Save.
5. **Reset Token** → copy the token. You'll paste it into `config.json` in step C.
   *Treat this like a password. Don't commit it.*

### A3. Invite the bot to a server

1. Same app → left sidebar → **OAuth2** → **URL Generator**.
2. **SCOPES**: tick `bot`.
3. **BOT PERMISSIONS**: tick `Read Messages/View Channels`, `Send Messages`, `Read Message History`, `Attach Files`.
4. Copy the generated URL at the bottom, open it in a browser, pick a server you own (or create a new one: Discord → ⊕ → "Create my own"), and authorize.

### A4. Copy the IDs you'll need

- **Channel ID**: right-click the channel you'll use → **Copy Channel ID**.
- **Your user ID**: right-click your own name anywhere → **Copy User ID**.

---

## Part B — Local install (~2 min)

### B1. Fill in the config

```bash
cd ~/.claude-discord
cp config.example.json config.json
$EDITOR config.json
```

Set:
- `bot_token` → from A2.5
- `allowed_channel_ids` → array with the channel ID from A4 (you can add more later)
- `allowed_user_ids` → array with your user ID from A4 (anyone not in this list is ignored)
- `working_directory` → which project `claude -p` should run in, e.g. `/Users/junaidahmed/tradingagents`
- `claude_path` → leave as `/opt/homebrew/bin/claude` (verified to exist on this machine)
- `trigger_prefix` → `"!claude "` requires every triggering message to start with `!claude `; set to `""` to react to every message in the channel.

### B2. Run the installer

```bash
cd ~/.claude-discord
./install.sh
```

It creates a venv, installs `discord.py`, installs the LaunchAgent, and starts it.

### B3. Verify

```bash
tail -f ~/Library/Logs/claude-discord/listener.out.log
```

You should see `Connected as claude-bridge#NNNN. Allowed channels=... users=... Prefix='!claude '`.

If you see errors about `Privileged intent provided is not enabled` → go back to A2.4.

---

## Part C — Use it

In your Discord channel, type:

```
!claude what files have changed since yesterday?
```

(Drop the `!claude ` prefix if you set `trigger_prefix` to `""`.)

The bot will:
1. Reply with `🤖 starting <job_id> — waiting for a free slot...`
2. Run `claude -p "what files have changed since yesterday?"` in your `working_directory`
3. Edit its reply to show elapsed time
4. Post the final output below (or as a file attachment if it's long)

---

## Operations

| Want to... | Command |
|---|---|
| Tail logs | `tail -f ~/Library/Logs/claude-discord/listener.{out,err}.log` |
| Stop the listener | `launchctl bootout gui/$(id -u)/com.junaidahmed.claude-discord` |
| Start it | `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.junaidahmed.claude-discord.plist` |
| Restart after editing config | `launchctl kickstart -k gui/$(id -u)/com.junaidahmed.claude-discord` |
| Change which project `claude` runs in | Edit `working_directory` in `config.json` → restart |
| Add another channel | Append to `allowed_channel_ids` → restart |
| Uninstall | `./uninstall.sh` |

## Security notes

- **Allowlist is the only barrier.** Anyone in the allowed channel who is also in `allowed_user_ids` can run anything `claude` can. Don't share that channel with people you don't trust to operate your shell.
- **The bot token is your bot's identity.** If it leaks, anyone can post as your bot to any server it's in. Rotate via Developer Portal → Bot → **Reset Token** if exposed.
- **`config.json` is in `.gitignore`** of this directory. Keep it that way.
- **Each session has full filesystem access.** The bot runs as you. If you want sandboxing, consider running this whole thing inside a Docker container with the project mounted read-only, or pointing `working_directory` at a sandbox dir.

## Tuning

- Concurrency: `max_concurrent_sessions: 1` is the safest default — back-to-back Discord messages queue rather than running in parallel. Bump if you have headroom.
- Timeout: `session_timeout_s: 1800` kills a hung session after 30 minutes. Lower it if you only run short prompts; raise it for long-running agentic tasks.
- Prefix: prefixed mode (`"!claude "`) lets you chat freely in the channel without triggering the bot; bare mode (`""`) makes the whole channel a Claude prompt.
