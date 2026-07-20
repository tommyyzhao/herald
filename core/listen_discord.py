#!/usr/bin/env -S uv run python
"""
Herald Discord listener — persistent gateway client. This is the core piece:
it has no built-in knowledge of any specific module, only of prompts/reply.md
(which names the module(s) it should ground answers in).

Sits on the Discord gateway (not a poll) and watches DISCORD_CHANNEL_ID plus
any Discord thread spawned off one of the bot's messages there. Any message
you send in either place is treated as a follow-up: it's handed to a
read-only local CLI coding agent (ships wired to `codex`, see ask_codex()
below to swap in another one) along with recent thread context and
prompts/reply.md's grounding instructions, and the answer is posted back as
an actual Discord reply. Auto-joins new threads on that channel
(on_thread_create) so it reliably sees messages posted in them.

This is the one intentionally persistent process in Herald (everything else
is cron-driven via your OS scheduler — see examples/launchd/). Requires the
bot's "Message Content" privileged gateway intent enabled in the Discord
developer portal — see README.md.

Run under a process supervisor (see examples/launchd/com.herald.listener.plist.example,
KeepAlive) so a crash just restarts it; discord.py handles gateway reconnects
on its own.
"""

import asyncio
import os
import re
import sys
import traceback
from datetime import datetime

import discord

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "bin"))
from send_discord import chunk  # noqa: E402  (reuse the 2000-char splitter)


def load_env():
    env = dict(os.environ)
    p = os.path.join(ROOT, ".env.local")
    if os.path.exists(p):
        for line in open(p):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env.setdefault(k.strip(), v.strip())
    return env


ENV = load_env()
TOKEN = ENV.get("DISCORD_BOT_TOKEN")
OPERATOR_ID = ENV.get("DISCORD_DM_USER_ID", "").strip()
CHANNEL_ID = ENV.get("DISCORD_CHANNEL_ID", "").strip()
REPLY_PROMPT = open(os.path.join(ROOT, "prompts", "reply.md")).read()

if not TOKEN:
    raise SystemExit("DISCORD_BOT_TOKEN not set (.env.local)")
if not OPERATOR_ID:
    raise SystemExit("DISCORD_DM_USER_ID not set (.env.local)")
if not CHANNEL_ID:
    raise SystemExit("DISCORD_CHANNEL_ID not set (.env.local) — the listener needs a channel to watch")


def log_path():
    os.makedirs(os.path.join(ROOT, "logs"), exist_ok=True)
    return os.path.join(ROOT, "logs", f"listener-{datetime.now():%Y-%m-%d}.log")


def log(msg):
    line = f"{datetime.now():%Y-%m-%dT%H:%M:%S%z} {msg}"
    print(line, flush=True)
    with open(log_path(), "a") as f:
        f.write(line + "\n")


intents = discord.Intents.default()
intents.message_content = True  # privileged — must also be enabled in the dev portal
client = discord.Client(intents=intents)


def is_watched(channel):
    """DISCORD_CHANNEL_ID itself, or a thread spawned from one of its messages."""
    if str(channel.id) == CHANNEL_ID:
        return True
    return isinstance(channel, discord.Thread) and str(channel.parent_id) == CHANNEL_ID


REAL_MESSAGE_TYPES = (discord.MessageType.default, discord.MessageType.reply)
CHUNK_PREFIX_RE = re.compile(r"^`\[(\d+)/(\d+)\]`\s*\n?")

# A thread's own history() never includes the message it was created from
# (that message stays in the parent channel) — without resolving it
# separately, "thread off this briefing item and ask about it" has nothing to
# answer from. Cache per-thread since it's immutable and re-fetching on every
# follow-up message would be wasted API calls.
_ORIGIN_CACHE = {}


async def resolve_thread_origin(thread):
    if thread.id in _ORIGIN_CACHE:
        return _ORIGIN_CACHE[thread.id]
    parent = thread.parent
    result = (None, False)
    if parent is not None:
        try:
            origin = thread.starter_message or await parent.fetch_message(thread.id)
            content = (origin.content or "").strip()
            m = CHUNK_PREFIX_RE.match(content)
            if m and int(m.group(2)) > 1:
                # Long messages (e.g. daily briefings) get split into several
                # `[i/n]` parts by send_discord.py — stitch them back together.
                total = int(m.group(2))
                parts = {int(m.group(1)): CHUNK_PREFIX_RE.sub("", content, count=1).strip()}
                async for sib in parent.history(around=origin, limit=total * 2 + 10):
                    if sib.author.id != origin.author.id:
                        continue
                    sm = CHUNK_PREFIX_RE.match(sib.content or "")
                    if sm and int(sm.group(2)) == total:
                        parts[int(sm.group(1))] = CHUNK_PREFIX_RE.sub("", sib.content, count=1).strip()
                content = "\n".join(parts[i] for i in sorted(parts))
            result = (content or None, origin.author.id == client.user.id)
        except (discord.NotFound, discord.HTTPException) as e:
            log(f"could not resolve thread origin for {thread.id}: {e}")
    _ORIGIN_CACHE[thread.id] = result
    return result


async def build_transcript(message):
    channel = message.channel
    lines = []
    if isinstance(channel, discord.Thread):
        origin_text, origin_is_bot = await resolve_thread_origin(channel)
        if origin_text:
            who = "you" if origin_is_bot else "them"
            lines.append(f"[{who}] (message this thread was created from) {origin_text}")
    history = [
        m async for m in channel.history(limit=20, before=message)
        if m.type in REAL_MESSAGE_TYPES
    ]
    history.reverse()
    history.append(message)
    for m in history:
        who = "you" if m.author.id == client.user.id else "them"
        text = (m.content or "").strip()
        if text:
            lines.append(f"[{who}] {text}")
    return "\n".join(lines)


async def ask_codex(transcript):
    """Swap this out for whatever local CLI coding agent you use — the only
    contract is stdin-in (the prompt), stdout-out (the reply text), read-only
    filesystem access rooted at ROOT for grounding. e.g. `claude -p --model
    sonnet` or `cursor-agent --mode read-only` both fit the same shape."""
    prompt = (
        f"{REPLY_PROMPT}\n\n"
        f"--- runtime metadata, not part of the conversation ---\n"
        f"Current date (PT): {datetime.now():%Y-%m-%d}\n"
        f"--- end metadata ---\n\n"
        f"THREAD TRANSCRIPT (oldest first, respond to the last [them] line):\n{transcript}"
    )
    proc = await asyncio.create_subprocess_exec(
        "codex", "exec", "--skip-git-repo-check", "--sandbox", "read-only",
        "-m", "gpt-5.6-terra", "-c", 'model_reasoning_effort="medium"',
        cwd=ROOT,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate(prompt.encode())
    with open(log_path(), "a") as f:
        f.write(err.decode(errors="replace"))
    if proc.returncode != 0 or not out.strip():
        raise RuntimeError(f"codex exited {proc.returncode} with empty/failed output")
    return out.decode(errors="replace").strip()


@client.event
async def on_ready():
    log(f"connected as {client.user} (id={client.user.id}), watching channel {CHANNEL_ID}")


@client.event
async def on_thread_create(thread):
    if str(thread.parent_id) != CHANNEL_ID:
        return
    try:
        await thread.join()
        log(f"joined new thread {thread.id} ({thread.name!r})")
    except discord.HTTPException as e:
        log(f"failed to join thread {thread.id}: {e}")

    # Discord auto-names the thread from the raw origin message text, which
    # for a chunked briefing message is a literal `[2/5]`-prefixed fragment —
    # clean it up so the thread list is actually readable.
    try:
        origin_text, _ = await resolve_thread_origin(thread)
        if origin_text:
            clean = origin_text.strip().lstrip("#*").strip()
            clean = clean.splitlines()[0][:90] if clean else ""
            if clean and clean != thread.name:
                await thread.edit(name=clean)
    except discord.HTTPException as e:
        log(f"failed to rename thread {thread.id}: {e}")


async def send_reply(message, text):
    """Reply threaded when possible; some message types (system notices like
    "started a thread") reject message_reference and must fall back to a
    plain send in the same channel. suppress_embeds: the reply text already
    carries any URL, no need for Discord's link-preview card too."""
    try:
        return await message.reply(text, suppress_embeds=True)
    except discord.HTTPException:
        return await message.channel.send(text, suppress_embeds=True)


# Discord dispatches on_message as independent concurrent tasks, so two rapid
# messages in the same channel/thread would otherwise fire two overlapping
# codex calls that can't see each other and can reply out of order. Serialize
# per-channel (each thread has its own id, so separate conversations still
# run concurrently).
_CHANNEL_LOCKS = {}


def _lock_for(channel_id):
    lock = _CHANNEL_LOCKS.get(channel_id)
    if lock is None:
        lock = _CHANNEL_LOCKS[channel_id] = asyncio.Lock()
    return lock


@client.event
async def on_message(message):
    if message.author.id == client.user.id:
        return
    if message.type not in REAL_MESSAGE_TYPES:
        return  # e.g. "started a thread" system notices, not real questions
    if not is_watched(message.channel):
        return
    if str(message.author.id) != OPERATOR_ID:
        return
    if not (message.content or "").strip():
        return  # attachment-only / empty message — nothing to answer

    async with _lock_for(message.channel.id):
        log(f"question: {message.content[:200]!r}")
        try:
            async with message.channel.typing():
                transcript = await build_transcript(message)
                reply_text = await ask_codex(transcript)
            parts = chunk(reply_text)
            prefix = lambda i, n: f"`[{i}/{n}]`\n" if n > 1 else ""  # noqa: E731
            await send_reply(message, prefix(1, len(parts)) + parts[0])
            for i, part in enumerate(parts[1:], start=2):
                await message.channel.send(prefix(i, len(parts)) + part, suppress_embeds=True)
            log(f"replied ({len(parts)} part(s))")
        except Exception:
            tb = traceback.format_exc()
            log(f"ERROR answering message {message.id}:\n{tb}")
            try:
                await send_reply(
                    message, "Hit an error answering that — check `logs/listener-*.log`."
                )
            except Exception:
                log("also failed to send the error fallback")


if __name__ == "__main__":
    client.run(TOKEN, log_handler=None)
