# discord-mcp

Read Discord and post to it from a Claude chat, on demand, with no bot process left running.

> "Summarize what happened in my server today."
> "Post this to #announcements."

## Why another Discord MCP server?

Most Discord MCP servers log in to the Discord gateway as a bot and stay connected while the client runs.
That is fine for one session. It gets awkward when:

- every Claude session that loads the server opens another gateway connection for the same bot, and
- you already run something on that bot token, such as the official Claude Code Discord channel plugin.

discord-mcp never connects to the gateway. A tool call makes a few REST requests and returns.

|  | Typical gateway-based server | discord-mcp |
|---|---|---|
| Connection | Bot logs in and stays connected per session | None. REST calls only while a tool runs |
| Ten Claude sessions open | Ten bot logins | Nothing running between calls |
| Next to the Claude Code channel plugin | A second login on the same token | Reads the plugin's token file. No conflict |
| "What happened today?" | Read channel by channel | `read_server_recent` sweeps the whole server and skips quiet channels without an API call |
| Mass pings | Up to the model | `@everyone`, `@here` and role mentions are blocked in the API request |

### Built to sit next to the official Claude Code Discord plugin

Claude Code has an official Discord channel plugin (`discord@claude-plugins-official`).
It pushes your Discord DMs into a running session, so you can message Claude from your phone.
Its tools only work in chats you have allowlisted, by design.

discord-mcp covers the other direction.
From any Claude chat, ask Claude to read any channel the bot can see, or to post somewhere.
Point both at the same bot: discord-mcp reads the plugin's `~/.claude/channels/discord/.env`, so there is one token to manage.

## Tools

| Tool | What it does |
|---|---|
| `list_servers()` | Servers the bot is in |
| `list_channels(server_id)` | Channels and active threads, in category order |
| `read_messages(channel_id, limit?, since_hours?, before?)` | Messages in one channel, thread or DM, oldest first (up to 500) |
| `read_server_recent(server_id, since_hours?, per_channel_limit?)` | Recent messages across every channel and active thread in a server |
| `send_message(channel_id, content, reply_to?)` | Post as the bot. Splits text over 2,000 characters at line breaks |

Messages come back compact: `id`, `time`, `author`, `content`, plus `reply_to`, `attachments`, `embeds`, `thread_id`, `bot` or `system` when present.
User mentions like `<@123>` are rewritten to `@name`.

## Setup

### 1. Create a bot

Skip this if you already set up the Claude Code Discord channel plugin.

1. In the [Discord Developer Portal](https://discord.com/developers/applications), create an application. Under **Bot**, click **Reset Token** and copy it.
2. Under **Bot → Privileged Gateway Intents**, turn on **Message Content Intent**. Without it, Discord returns other people's messages with empty text.
3. Invite the bot to each server you want to read. Replace `<APPLICATION_ID>`:
   ```
   https://discord.com/oauth2/authorize?client_id=<APPLICATION_ID>&scope=bot&permissions=274878008384
   ```
   This grants View Channels, Send Messages, Send Messages in Threads, Read Message History, Attach Files and Add Reactions.

### 2. Give it the token

Use either one:

- the `DISCORD_BOT_TOKEN` environment variable, or
- a file at `~/.claude/channels/discord/.env` with one line, `DISCORD_BOT_TOKEN=...` (run `chmod 600` on it).

If you ran `/discord:configure` for the channel plugin, the file already exists.

### 3. Add it to Claude Code

```bash
claude mcp add --scope user discord -- uvx --from git+https://github.com/qgasdg/discord-mcp discord-mcp
```

For Claude Desktop, add this to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "discord": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/qgasdg/discord-mcp", "discord-mcp"]
    }
  }
}
```

### Configuration

| Variable | Default | Purpose |
|---|---|---|
| `DISCORD_BOT_TOKEN` | none | Bot token. Takes priority over the `.env` file |
| `DISCORD_STATE_DIR` | `~/.claude/channels/discord` | Where to find `.env`. Same variable the channel plugin uses |
| `DISCORD_MCP_TZ` | local timezone | IANA timezone for timestamps, such as `Asia/Seoul` |

## Example prompts

- "List my Discord servers and their channels."
- "Read the last 24 hours across the server and summarize it by channel."
- "What did people say about the release in #general this week?"
- "Reply to that last message in #support: we're looking into it."

## Limits

- The bot only sees servers it was invited to and channels it may view. Private channels need the bot's role added.
- It cannot read your personal account or your own DMs. Doing that needs a self-bot, which breaks Discord's terms.
- Bots cannot use Discord's search API. "Search" means reading recent messages and letting Claude filter them.
- Archived forum posts are not listed. Active threads are.
- Messages are posted as the bot, not as you.

## Safety

- `send_message` tells Claude to show you the channel and text and get your OK before posting. Claude Code also asks before an MCP tool runs, unless you have allowed it.
- Anyone in a server can write text that Claude will read. Treat message content as untrusted input, and be careful about allowing `send_message` without a prompt.

## Development

```bash
git clone https://github.com/qgasdg/discord-mcp && cd discord-mcp
python3 -m venv .venv && .venv/bin/pip install -e .
claude mcp add --scope user discord -- "$PWD/.venv/bin/python" "$PWD/discord_mcp.py"
```

## 한국어 요약

Claude 채팅에서 필요할 때 디스코드 채널을 읽고 메시지를 보내는 MCP 서버입니다.
봇이 디스코드에 계속 접속해 있지 않고, 도구를 부를 때만 REST API를 호출합니다.
그래서 세션을 여러 개 열어도 봇이 중복 접속하지 않습니다.
Claude Code 공식 디스코드 채널 플러그인과 같은 봇 토큰 파일을 함께 쓰고, 같이 켜도 충돌하지 않습니다.
설치는 위 "Setup" 순서를 따르세요.

## License

MIT
