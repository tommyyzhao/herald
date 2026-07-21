#!/usr/bin/env -S uv run python
"""
Herald Discord sender — deterministic, no agent.

Generic delivery primitive: reads a text file and posts it to a Discord
channel via the bot token (or DMs DISCORD_DM_USER_ID if DISCORD_CHANNEL_ID
isn't set). Splits to Discord's 2000-char limit on line boundaries,
suppresses link-preview embeds (the text already carries the URL), and
respects 429 rate limits. Lives in core/ because it has no opinion about
which module produced the file — every module's run script calls this with
its own output path.

Usage:
    uv run core/send_discord.py path/to/file.md
"""

import json
import os
import sys
import time
import urllib.request
import urllib.error

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
API = "https://discord.com/api/v10"
LIMIT = 1900  # under Discord's 2000 hard cap, leaves room for chunk headers
SUPPRESS_EMBEDS = 1 << 2  # message flag: no link-preview cards (URLs are already visible as text)


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


def api(method, path, token, payload=None):
    url = API + path
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bot {token}")
    req.add_header("Content-Type", "application/json")
    req.add_header("User-Agent", "Herald (github.com/tommyyzhao/herald)")
    for attempt in range(5):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                body = r.read().decode()
                return json.loads(body) if body else {}
        except urllib.error.HTTPError as e:
            body = e.read().decode()
            if e.code == 429:
                try:
                    retry = float(json.loads(body).get("retry_after", 2))
                except Exception:
                    retry = 2.0
                time.sleep(retry + 0.5)
                continue
            raise SystemExit(f"Discord {method} {path} -> {e.code}: {body}")
    raise SystemExit(f"Discord {method} {path}: rate-limited after retries")


def chunk(text, limit=LIMIT):
    out, cur = [], ""
    for line in text.split("\n"):
        # hard-split any single oversized line
        while len(line) > limit:
            out.append(line[:limit])
            line = line[limit:]
        if len(cur) + len(line) + 1 > limit:
            if cur:
                out.append(cur)
            cur = line
        else:
            cur = (cur + "\n" + line) if cur else line
    if cur:
        out.append(cur)
    return out or ["(empty briefing)"]


def send(text, env=None):
    """Post text to DISCORD_CHANNEL_ID (or DM DISCORD_DM_USER_ID as a
    fallback), chunked to Discord's limit. The reusable primitive — both
    main() (file-based CLI use) and core/watch_runner.py (in-memory alert
    text, no temp file) call this."""
    env = env if env is not None else load_env()
    token = env.get("DISCORD_BOT_TOKEN")
    if not token:
        raise SystemExit("DISCORD_BOT_TOKEN not set (.env.local)")

    channel_id = env.get("DISCORD_CHANNEL_ID", "").strip()
    if not channel_id:
        user_id = env.get("DISCORD_DM_USER_ID", "").strip()
        if not user_id:
            raise SystemExit("set DISCORD_CHANNEL_ID or DISCORD_DM_USER_ID")
        dm = api("POST", "/users/@me/channels", token, {"recipient_id": user_id})
        channel_id = dm["id"]

    parts = chunk(text)
    for i, part in enumerate(parts, 1):
        prefix = f"`[{i}/{len(parts)}]`\n" if len(parts) > 1 else ""
        api("POST", f"/channels/{channel_id}/messages", token,
            {"content": prefix + part, "flags": SUPPRESS_EMBEDS})
        time.sleep(0.4)
    return len(parts)


def main():
    if len(sys.argv) < 2:
        raise SystemExit("usage: uv run core/send_discord.py path/to/file.md")
    path = sys.argv[1]
    if not os.path.exists(path):
        raise SystemExit(f"briefing not found: {path}")
    text = open(path).read().strip()
    if not text:
        raise SystemExit(f"briefing empty: {path}")

    n = send(text)
    print(f"[discord] sent {n} message(s)")


if __name__ == "__main__":
    main()
