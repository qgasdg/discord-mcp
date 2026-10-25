"""On-demand Discord MCP server: read channels and send messages from a Claude chat.

Unlike most Discord MCP servers, this one never logs in to the Discord gateway.
Each tool call makes a few REST API requests and returns. Nothing stays connected
in the background, so any number of Claude sessions can load it at once, and it
never competes with a running bot or a Claude Code channel session.

Tools:
  - list_servers()                                           : servers the bot is in
  - list_channels(server_id)                                 : channels and active threads
  - read_messages(channel_id, limit?, since_hours?, before?) : messages in one channel
  - read_server_recent(server_id, since_hours?, ...)         : recent messages across a server
  - send_message(channel_id, content, reply_to?)             : post as the bot

The bot token comes from DISCORD_BOT_TOKEN, or else from the same .env file the
official Claude Code Discord channel plugin uses (~/.claude/channels/discord/.env,
or $DISCORD_STATE_DIR/.env). The bot can only read servers it was invited to and
channels it has permission to view.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

import httpx
from mcp.server.fastmcp import FastMCP

__version__ = "0.1.0"

API = "https://discord.com/api/v10"
STATE_DIR = Path(os.environ.get("DISCORD_STATE_DIR") or Path.home() / ".claude" / "channels" / "discord")
ENV_FILE = STATE_DIR / ".env"
# Timestamps are shown in DISCORD_MCP_TZ (IANA name, e.g. "Asia/Seoul"), or the local timezone.
TZ = ZoneInfo(os.environ["DISCORD_MCP_TZ"]) if os.environ.get("DISCORD_MCP_TZ") else None
DISCORD_EPOCH_MS = 1420070400000
MAX_LEN = 2000  # Discord's per-message character limit

# Channel types that hold messages directly (forums and categories do not)
TYPE_LABEL = {0: "text", 2: "voice", 5: "announcement", 10: "thread", 11: "thread",
              12: "thread", 13: "stage", 15: "forum", 16: "media"}
READABLE = {0, 2, 5, 10, 11, 12, 13}

# System messages that arrive with empty content (0 = default, 19 = reply are not system)
SYSTEM_LABEL = {6: "pinned a message", 7: "joined the server", 8: "boosted the server",
                18: "started a thread", 21: "thread starter message", 46: "poll result"}

mcp = FastMCP("discord")

# httpx logs every request at INFO; keep stderr quiet
logging.getLogger("httpx").setLevel(logging.WARNING)

# Limit concurrent requests to stay clear of rate limits
_SEM = asyncio.Semaphore(4)


def _token() -> str:
    if t := os.environ.get("DISCORD_BOT_TOKEN"):
        return t.strip()
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            if line.startswith("DISCORD_BOT_TOKEN="):
                return line.split("=", 1)[1].strip()
    raise RuntimeError(f"No bot token. Set DISCORD_BOT_TOKEN or write it to {ENV_FILE}.")


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        headers={
            "Authorization": f"Bot {_token()}",
            "User-Agent": f"DiscordBot (https://github.com/qgasdg/discord-mcp, {__version__})",
        },
        timeout=20,
    )


async def _call(client: httpx.AsyncClient, method: str, path: str, **kw):
    """Call the Discord API. On 429 (rate limited), wait retry_after and retry, up to 3 tries."""
    async with _SEM:
        for _ in range(3):
            r = await client.request(method, API + path, **kw)
            if r.status_code == 429:
                await asyncio.sleep(float(r.json().get("retry_after", 1)) + 0.1)
                continue
            r.raise_for_status()
            return r.json() if r.content else None
        r.raise_for_status()


def _describe(e: Exception) -> str:
    if isinstance(e, httpx.HTTPStatusError):
        code = e.response.status_code
        hint = {401: "bot token is invalid or revoked", 403: "bot lacks permission for this channel",
                404: "ID not found"}.get(code, "")
        try:
            msg = e.response.json().get("message", "")
        except ValueError:
            msg = ""
        return f"HTTP {code} {hint} {msg}".strip()
    return f"{type(e).__name__}: {e}"[:200]


def _snowflake_since(hours: float) -> int:
    """The Discord ID for 'hours ago', used as the `after` cursor."""
    ms = int((time.time() - hours * 3600) * 1000)
    return (ms - DISCORD_EPOCH_MS) << 22


def _name(user: dict) -> str:
    return user.get("global_name") or user.get("username", "?")


def _fmt(m: dict) -> dict:
    content = m.get("content", "")
    for u in m.get("mentions", []):
        content = content.replace(f"<@{u['id']}>", f"@{_name(u)}").replace(f"<@!{u['id']}>", f"@{_name(u)}")
    row = {
        "id": m["id"],
        "time": datetime.fromisoformat(m["timestamp"]).astimezone(TZ).strftime("%Y-%m-%d %H:%M"),
        "author": _name(m["author"]),
        "content": content,
    }
    if m.get("type", 0) not in (0, 19):
        row["system"] = SYSTEM_LABEL.get(m["type"], f"type {m['type']}")
    if m["author"].get("bot"):
        row["bot"] = True
    if ref := (m.get("message_reference") or {}).get("message_id"):
        row["reply_to"] = ref
    if atts := m.get("attachments"):
        row["attachments"] = [{"filename": a["filename"], "url": a["url"]} for a in atts]
    if embeds := m.get("embeds"):
        row["embeds"] = [e.get("title") or (e.get("description") or "")[:200] for e in embeds]
    if thread := m.get("thread"):
        row["thread_id"] = thread["id"]
    return row


async def _fetch(client: httpx.AsyncClient, channel_id: str, limit: int,
                 after: Optional[int] = None, before: Optional[str] = None) -> list[dict]:
    """Page through messages 100 at a time and return them oldest first."""
    out: list[dict] = []
    cur_after, cur_before = after, before
    while len(out) < limit:
        n = min(100, limit - len(out))
        params: dict = {"limit": n}
        if cur_after is not None:
            params["after"] = str(cur_after)
        elif cur_before:
            params["before"] = cur_before
        batch = await _call(client, "GET", f"/channels/{channel_id}/messages", params=params)
        if not batch:
            break
        out.extend(batch)
        ids = [int(m["id"]) for m in batch]
        if cur_after is not None:
            cur_after = max(ids)
        else:
            cur_before = str(min(ids))
        if len(batch) < n:
            break
    out.sort(key=lambda m: int(m["id"]))
    return out


async def _channels(client: httpx.AsyncClient, server_id: str) -> list[dict]:
    chans = await _call(client, "GET", f"/guilds/{server_id}/channels")
    try:
        threads = (await _call(client, "GET", f"/guilds/{server_id}/threads/active")).get("threads", [])
    except httpx.HTTPStatusError:
        threads = []
    cats = {c["id"]: c for c in chans if c["type"] == 4}
    by_id = {c["id"]: c for c in chans}
    rows = []
    for c in sorted((c for c in chans if c["type"] != 4),
                    key=lambda c: (cats.get(c.get("parent_id"), {}).get("position", -1), c.get("position", 0))):
        rows.append({
            "id": c["id"],
            "name": c["name"],
            "type": TYPE_LABEL.get(c["type"], str(c["type"])),
            "category": cats.get(c.get("parent_id"), {}).get("name"),
            "last_message_id": c.get("last_message_id"),
        })
    for t in threads:
        rows.append({
            "id": t["id"],
            "name": t["name"],
            "type": "thread",
            "parent": by_id.get(t.get("parent_id"), {}).get("name"),
            "last_message_id": t.get("last_message_id"),
        })
    return rows


@mcp.tool()
async def list_servers() -> list[dict]:
    """List the Discord servers (guilds) the bot is a member of."""
    async with _client() as client:
        try:
            guilds = await _call(client, "GET", "/users/@me/guilds")
        except Exception as e:
            return [{"error": _describe(e)}]
    return [{"id": g["id"], "name": g["name"]} for g in guilds]


@mcp.tool()
async def list_channels(server_id: str) -> list[dict]:
    """List a server's channels and active threads, in category order.

    server_id: a server ID from list_servers
    Channels of type "forum" hold no messages themselves; read their threads instead.
    """
    async with _client() as client:
        try:
            rows = await _channels(client, server_id)
        except Exception as e:
            return [{"error": _describe(e)}]
    for r in rows:
        r.pop("last_message_id", None)
    return rows


@mcp.tool()
async def read_messages(channel_id: str, limit: int = 50, since_hours: Optional[float] = None,
                        before: Optional[str] = None) -> dict:
    """Read messages from one channel, thread, or DM, oldest first.

    channel_id : a channel or thread ID from list_channels
    limit      : maximum number of messages (default 50, max 500)
    since_hours: if set, only messages from the last N hours. e.g. 24 = the last day
    before     : read messages older than this message ID (to page further back)
    """
    limit = max(1, min(limit, 500))
    after = _snowflake_since(since_hours) if since_hours else None
    async with _client() as client:
        try:
            msgs = await _fetch(client, channel_id, limit, after=after, before=before)
        except Exception as e:
            return {"channel_id": channel_id, "error": _describe(e)}
    return {"channel_id": channel_id, "count": len(msgs), "messages": [_fmt(m) for m in msgs]}


@mcp.tool()
async def read_server_recent(server_id: str, since_hours: float = 24,
                             per_channel_limit: int = 100) -> dict:
    """Collect recent messages from every channel and active thread in a server.

    server_id        : a server ID from list_servers
    since_hours      : how many hours back to read (default 24)
    per_channel_limit: maximum messages per channel (default 100, max 500)
    Channels with no new messages are skipped without an API call.
    Channels the bot cannot view are listed under "skipped" with the reason.
    """
    per_channel_limit = max(1, min(per_channel_limit, 500))
    after = _snowflake_since(since_hours)
    async with _client() as client:
        try:
            chans = await _channels(client, server_id)
        except Exception as e:
            return {"server_id": server_id, "error": _describe(e)}
        targets = [c for c in chans
                   if c["type"] in {TYPE_LABEL[t] for t in READABLE}
                   and c.get("last_message_id") and int(c["last_message_id"]) > after]

        async def one(c: dict):
            try:
                return c, await _fetch(client, c["id"], per_channel_limit, after=after), None
            except Exception as e:
                return c, [], _describe(e)

        results = await asyncio.gather(*(one(c) for c in targets))

    channels, skipped = [], []
    for c, msgs, err in results:
        if err:
            skipped.append({"channel": c["name"], "channel_id": c["id"], "reason": err})
        elif msgs:
            channels.append({
                "channel": c["name"],
                "channel_id": c["id"],
                "category": c.get("category") or c.get("parent"),
                "count": len(msgs),
                "messages": [_fmt(m) for m in msgs],
            })
    return {"server_id": server_id, "since_hours": since_hours, "channels": channels, "skipped": skipped}


def _chunks(text: str) -> list[str]:
    """Split text to fit the 2000-character limit, preferring line breaks."""
    out = []
    while len(text) > MAX_LEN:
        cut = text.rfind("\n", 0, MAX_LEN)
        if cut <= 0:
            cut = MAX_LEN
        out.append(text[:cut])
        text = text[cut:].lstrip("\n")
    if text:
        out.append(text)
    return out


@mcp.tool()
async def send_message(channel_id: str, content: str, reply_to: Optional[str] = None) -> dict:
    """Post a message as the bot to a channel, thread, or DM.

    Always show the user the target channel and the exact text, and get their
    confirmation, before calling this.
    channel_id: target channel or thread ID
    content   : message text. Over 2000 characters, it is split at line breaks
    reply_to  : message ID to reply to (optional)
    @everyone, @here, and role mentions never ping; only user mentions do.
    """
    parts = _chunks(content.strip())
    if not parts:
        return {"error": "Message is empty."}
    sent = []
    async with _client() as client:
        try:
            ch = await _call(client, "GET", f"/channels/{channel_id}")
            for i, part in enumerate(parts):
                body: dict = {"content": part, "allowed_mentions": {"parse": ["users"], "replied_user": False}}
                if reply_to and i == 0:
                    body["message_reference"] = {"message_id": reply_to, "fail_if_not_exists": False}
                m = await _call(client, "POST", f"/channels/{channel_id}/messages", json=body)
                sent.append(m["id"])
        except Exception as e:
            return {"channel_id": channel_id, "sent": sent, "error": _describe(e)}
    guild = ch.get("guild_id") or "@me"
    return {
        "channel_id": channel_id,
        "sent": sent,
        "link": f"https://discord.com/channels/{guild}/{channel_id}/{sent[0]}",
    }


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
