#!/usr/bin/env -S uv run python
"""
x-briefs watcher: viral — fires an immediate Discord alert when the most
recent fetch contains a post crossing a very high signal threshold, instead
of waiting for the next scheduled distill/brief. A concrete, working example
of the core/watch_runner.py contract: run this on a tight interval (a few
minutes — see examples/launchd/com.herald.watch.plist.example), much
tighter than x-briefs' own 30-min distill cadence, and it reuses whatever
fetch.py already pulled — no separate credentials or API calls of its own.

State is the set of tweet ids already alerted on (capped), so re-running
against the same fetch doesn't re-fire, and a fetch with nothing new above
the bar just persists state and stays quiet.
"""
import glob
import json
import os
import sys

MODULE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

VIRAL_THRESHOLD = 500  # deliberately a high bar — this is for "drop everything", not routine signal
STATE_CAP = 500  # cap the alerted-id list so state doesn't grow unbounded


# Mirrors fetch.py's signal_score() exactly — kept as a tiny standalone copy
# rather than importing fetch.py (a script with real import-time side
# effects: loads .env.local, builds every search query) just for one formula.
# If you tune fetch.py's scoring, update this too.
def signal_score(t):
    eng = t["likes"] + 3 * t["retweets"] + 0.5 * t["replies"]
    cred = min((t.get("followers") or 0) / 100000, 5)
    return eng * (1 + cred * 0.3)


def latest_raw_file():
    files = sorted(glob.glob(os.path.join(MODULE_ROOT, "raw", "*", "*.json")))
    return files[-1] if files else None


def main():
    prior = json.load(sys.stdin)
    alerted = set(prior) if isinstance(prior, list) else set()

    path = latest_raw_file()
    if not path:
        print(json.dumps({"fire": False, "message": None, "state": list(alerted)}))
        return

    data = json.load(open(path))
    tweets = data.get("tweets", [])

    best = None
    for t in tweets:
        if not t.get("id") or t["id"] in alerted:
            continue
        if signal_score(t) >= VIRAL_THRESHOLD and (best is None or signal_score(t) > signal_score(best)):
            best = t

    if best is None:
        print(json.dumps({"fire": False, "message": None, "state": list(alerted)}))
        return

    alerted.add(best["id"])
    trimmed = list(alerted)[-STATE_CAP:]
    message = (
        f"**Viral signal:** {best['text'][:200]} — @{best['user']} "
        f"({best['likes']} likes, {best['retweets']} RT) — {best['url']}"
    )
    print(json.dumps({"fire": True, "message": message, "state": trimmed}))


if __name__ == "__main__":
    main()
