#!/usr/bin/env -S uv run python
"""
Herald watcher runner — the condition-watcher interrupt path.

Everything else in Herald waits for its scheduled batch (a 30-min distill, a
once-daily brief). This is the exception: run a single watcher on a tight
interval (see examples/launchd/com.herald.watch.plist.example — every few
minutes, not every 30) and, if it fires, post an alert *immediately* via
core/send_discord.py, bypassing whatever digest schedule the owning module
runs on. "Breaking" is defined by the watcher's fire logic — a deterministic
threshold/delta check — never a model guess, and the watcher itself never
touches the network: this script owns delivery, same deterministic-only
discipline as send_discord.py.

Watcher script contract (see modules/x-briefs/watchers/viral.py for a real
example):
    - A plain script, no framework, no imports of this file. Invoked with no
      arguments; reads its PRIOR state as JSON from stdin (or the JSON value
      `null` on first run / after a reset).
    - Prints exactly one line of JSON to stdout:
          {"fire": bool, "message": string or null, "state": <anything>}
    - `state` is persisted regardless of `fire` — this is what lets a watcher
      compare against its last observation and fire only on a real delta,
      instead of re-alerting an unchanged condition every tick. Keep it small
      (this isn't a database) and JSON-serializable.
    - A watcher that exits non-zero or prints something that isn't valid JSON
      is logged and its state is left untouched — skip a tick rather than
      silently corrupt state or advance past a broken check. A watcher that
      goes quiet when its own check fails would otherwise look healthy while
      broken; don't let that happen silently.

Usage:
    uv run core/watch_runner.py <path/to/watcher.py> <path/to/state.json>
"""

import json
import os
import subprocess
import sys
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # repo root
sys.path.insert(0, os.path.join(ROOT, "core"))
import send_discord  # noqa: E402


def log(label, msg):
    line = f"{datetime.now():%Y-%m-%dT%H:%M:%S%z} {msg}"
    print(line, flush=True)
    log_dir = os.path.join(ROOT, "logs")
    os.makedirs(log_dir, exist_ok=True)
    with open(os.path.join(log_dir, f"watch-{label}.log"), "a") as f:
        f.write(line + "\n")


def main():
    if len(sys.argv) < 3:
        raise SystemExit("usage: uv run core/watch_runner.py <watcher.py> <state.json>")
    watcher_path, state_path = sys.argv[1], sys.argv[2]
    label = os.path.splitext(os.path.basename(watcher_path))[0]

    prior_state = None
    if os.path.exists(state_path):
        try:
            prior_state = json.load(open(state_path))
        except (json.JSONDecodeError, OSError) as e:
            log(label, f"WARN: could not read prior state ({e}); treating as None")

    proc = subprocess.run(
        ["uv", "run", watcher_path],
        input=json.dumps(prior_state),
        capture_output=True,
        text=True,
        cwd=ROOT,
    )
    if proc.returncode != 0:
        log(label, f"ERROR: watcher exited {proc.returncode}: {proc.stderr.strip()[:500]}")
        return 1

    try:
        result = json.loads(proc.stdout.strip())
        fire = bool(result.get("fire"))
        message = result.get("message")
        new_state = result.get("state")
    except (json.JSONDecodeError, AttributeError) as e:
        log(label, f"ERROR: bad watcher output ({e}): {proc.stdout[:500]!r}")
        return 1

    os.makedirs(os.path.dirname(state_path) or ".", exist_ok=True)
    json.dump(new_state, open(state_path, "w"))

    if not fire:
        log(label, "no fire")
        return 0

    if not message:
        log(label, "WARN: fire=true but no message; skipping send")
        return 0

    env = send_discord.load_env()
    operator_id = env.get("DISCORD_DM_USER_ID", "").strip()
    prefix = f"🚨 <@{operator_id}> " if operator_id else "🚨 "
    try:
        n = send_discord.send(prefix + message, env)
        log(label, f"FIRED — sent {n} message(s): {message[:200]!r}")
    except SystemExit as e:
        log(label, f"ERROR: fired but send failed: {e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
