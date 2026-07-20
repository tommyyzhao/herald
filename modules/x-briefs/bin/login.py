#!/usr/bin/env -S uv run python
"""
Herald x-briefs module: login — one-time (and occasional refresh) X cookie
minting via twikit.

twikit authenticates as your real account, then saves session cookies so the
30-min fetcher never needs your password. Cookies last weeks; rerun this when
fetch.py starts logging "cookies-expired".

Usage:
    uv run modules/x-briefs/bin/login.py
    # reads X_USERNAME / X_EMAIL / X_PASSWORD from env or .env.local if set,
    # otherwise prompts. Handles 2FA / confirmation prompts interactively.

Credentials are used only to log in; they are NOT stored. Only the resulting
cookie file (.secrets/x-twikit-cookies.json, gitignored) is written.
"""

import asyncio
import getpass
import os
import sys

MODULE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # modules/x-briefs
REPO_ROOT = os.path.dirname(os.path.dirname(MODULE_ROOT))                  # repo root


def load_env():
    env = {}
    p = os.path.join(REPO_ROOT, ".env.local")
    if os.path.exists(p):
        for line in open(p):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    return env


ENV = load_env()
COOKIES_PATH = os.path.join(REPO_ROOT, ENV.get("X_COOKIES_PATH", ".secrets/x-twikit-cookies.json"))


async def main():
    try:
        from twikit import Client
    except ImportError:
        print("twikit not installed. Run: uv sync")
        return 1

    # Fix twikit 2.3.3's broken x-client-transaction-id generation (see twikit_patch).
    import twikit_patch
    twikit_patch.apply()

    username = os.environ.get("X_USERNAME") or ENV.get("X_USERNAME") or input("X username (without @): ").strip()
    email = os.environ.get("X_EMAIL") or ENV.get("X_EMAIL") or input("X email: ").strip()
    password = os.environ.get("X_PASSWORD") or ENV.get("X_PASSWORD") or getpass.getpass("X password: ")

    os.makedirs(os.path.dirname(COOKIES_PATH), exist_ok=True)
    client = Client("en-US")
    print("Logging in (you may be prompted for a 2FA/confirmation code)...")
    await client.login(
        auth_info_1=username,
        auth_info_2=email,
        password=password,
        cookies_file=COOKIES_PATH,
    )
    client.save_cookies(COOKIES_PATH)
    try:
        os.chmod(COOKIES_PATH, 0o600)
    except OSError:
        pass
    print(f"Cookies saved -> {COOKIES_PATH}")

    # Smoke test: pull one home-timeline item to confirm the session works.
    try:
        home = await client.get_latest_timeline(count=1)
        if home:
            t = home[0]
            who = getattr(getattr(t, "user", None), "screen_name", "?")
            print(f"OK — home timeline reachable (top item from @{who}).")
        else:
            print("Logged in, but home timeline returned empty (still usable).")
    except Exception as e:
        print(f"Logged in, but smoke test failed: {e}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
