#!/usr/bin/env -S uv run python
"""
Herald x-briefs module: fetch — deterministic X pull via twikit (cookie auth).

Pulls your home ("Following") timeline + a topical dev/AI search set, dedupes
against recently-seen IDs, ranks by signal, and writes the new items to
raw/YYYY-MM-DD/HHMM.json for the codex distiller to consume.

Designed to be cron-safe: if cookies are missing or expired it writes a clear
marker file and exits 0 (so the distill step no-ops instead of erroring) — the
pipeline self-heals the moment fresh cookies appear (run bin/login.py).
"""

import asyncio
import json
import os
import sys
from datetime import datetime, timezone

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

# Pull sizes. Home + roster carry NO engagement floor (two-tier policy) so deep,
# low-virality technical posts survive; broad discovery queries carry a modest
# min_faves floor baked into the query string.
HOME_COUNT = 60
ACCOUNT_COUNT = 40   # per OR-grouped roster query
BROAD_COUNT = 25     # per broad discovery query

# ---------------------------------------------------------------------------
# Curated roster — researchers / SWE / agentic-coding builders (TPOT-leaning).
# Pulled DIRECTLY with no engagement floor, so a 30-like banger from a
# researcher isn't filtered out the way min_faves would. EDIT THIS LIST — it's
# the highest-leverage knob. Weighted to AI/ML research, SWE craft & languages,
# and agentic coding (systems/infra de-emphasized per your prefs).
ROSTER = [
    # AI/ML research & deep technical
    "karpathy", "jxmnop", "giffmana", "_jasonwei", "main_horse", "nrehiew_",
    "teortaxesTex", "kalomaze", "danielhanchen", "Tim_Dettmers", "vikhyatk",
    "_akhaliq", "arankomatsuzaki", "soumithchintala", "typedfemale", "qtnx_",
    # SWE craft & languages
    "matklad", "andrewkelley", "mitchellh", "hillelogram", "ID_AA_Carmack",
    # Agentic coding / dev tools
    "simonw",
]


def _chunk(seq, n):
    return [seq[i:i + n] for i in range(0, len(seq), n)]


# OR-group the roster into a few queries (1 API call covers ~8 accounts) to keep
# the per-run request count modest. No min_faves → all their recent posts.
ROSTER_QUERIES = [
    ("(" + " OR ".join(f"from:{h}" for h in grp) + ") -filter:replies", "roster")
    for grp in _chunk(ROSTER, 8)
]

# Broad discovery beyond the roster. TWO-TIER: these carry a modest min_faves
# floor. Aimed at AI/ML research, technique, SWE craft, agentic tooling, and
# genuinely-notable releases. Tuned to surface things EARLY (Latest sort).
BROAD_QUERIES = [
    ("arxiv.org (LLM OR transformer OR diffusion OR agents OR reasoning) min_faves:20 -filter:replies", "ai-research"),
    ("(attention OR quantization OR inference OR RLHF OR finetuning OR kernel OR CUDA OR tokenizer) (trick OR speedup OR ablation OR result) min_faves:25 -filter:replies", "ai-technique"),
    ("(compiler OR type system OR zig OR rust OR borrow checker OR async runtime) (design OR perf OR technique) min_faves:25 -filter:replies", "swe-craft"),
    ("(coding agent OR agentic OR claude code OR codex OR cursor OR context window) (technique OR pattern OR eval OR harness) min_faves:20 -filter:replies", "agentic-tools"),
    ("open weights model release min_faves:60 -filter:replies", "release"),
]

# ---------------------------------------------------------------------------
# OPTIONAL EXTRA LANE — the pattern above (a second roster + topic-query set,
# source-tagged distinctly) is how the author tracks their own stack's
# maintainers/topics as a "🔧 Your Stack" briefing section. Add your own here
# the same way: a *_ROSTER list, a *_ROSTER_QUERIES OR-grouping, a *_TOPICS
# list of topic searches, then fold them into SEARCH_SOURCES below. Nothing
# shipped by default — the distill/brief prompts don't reference any specific
# extra-lane tag, so add matching instructions to modules/x-briefs/prompts/
# brief.md if you want it broken out into its own briefing section.

# All non-home sources, fetched after the home timeline.
SEARCH_SOURCES = (
    [(q, lbl, "Latest", ACCOUNT_COUNT) for q, lbl in ROSTER_QUERIES]
    + [(q, lbl, "Latest", BROAD_COUNT) for q, lbl in BROAD_QUERIES]
)

SEEN_PATH = os.path.join(MODULE_ROOT, "state", "seen-ids.json")
SEEN_CAP = 50000  # rolling window of seen tweet IDs (~weeks of coverage; ~1MB)


def load_seen():
    if os.path.exists(SEEN_PATH):
        try:
            return set(json.load(open(SEEN_PATH)))
        except Exception:
            return set()
    return set()


def save_seen(ids):
    os.makedirs(os.path.dirname(SEEN_PATH), exist_ok=True)
    # Trim to the newest SEEN_CAP. Tweet IDs are snowflakes (monotonic with
    # time), so the numerically-largest IDs are the most recent — a plain set
    # has no order, so sort by id to keep the right ones.
    def _key(x):
        try:
            return int(x)
        except (TypeError, ValueError):
            return 0
    trimmed = sorted(ids, key=_key)[-SEEN_CAP:]
    json.dump(trimmed, open(SEEN_PATH, "w"))


def extract(t, source):
    u = getattr(t, "user", None)
    return {
        "id": str(getattr(t, "id", "")),
        "text": " ".join((getattr(t, "text", "") or "").split()),
        "user": getattr(u, "screen_name", "") if u else "",
        "name": getattr(u, "name", "") if u else "",
        "followers": getattr(u, "followers_count", 0) if u else 0,
        "verified": getattr(u, "is_blue_verified", False) if u else False,
        "likes": getattr(t, "favorite_count", 0) or 0,
        "retweets": getattr(t, "retweet_count", 0) or 0,
        "replies": getattr(t, "reply_count", 0) or 0,
        "views": getattr(t, "view_count", None),
        "created_at": str(getattr(t, "created_at", "")),
        "url": f"https://x.com/{getattr(u, 'screen_name', 'i')}/status/{getattr(t, 'id', '')}",
        "source": source,
    }


def signal_score(t):
    eng = t["likes"] + 3 * t["retweets"] + 0.5 * t["replies"]
    cred = min((t["followers"] or 0) / 100000, 5)
    return eng * (1 + cred * 0.3)


def write_marker(reason):
    stamp = datetime.now()
    d = os.path.join(MODULE_ROOT, "raw", stamp.strftime("%Y-%m-%d"))
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, stamp.strftime("%H%M") + ".json")
    json.dump({"error": reason, "ts": stamp.isoformat(), "tweets": []}, open(path, "w"))
    print(f"[fetch] {reason} -> wrote marker {path}", file=sys.stderr)
    print(path)  # stdout: the written path, for the orchestrator


async def run():
    if not os.path.exists(COOKIES_PATH):
        write_marker("cookies-missing (run bin/login.py)")
        return 0

    try:
        from twikit import Client
    except ImportError:
        write_marker("twikit-not-installed (run: uv sync)")
        return 0

    # Make twikit work against X's current defenses: patch transaction-id
    # generation (twikit_patch) and route the wire through curl_cffi's browser
    # fingerprint (xtransport) since X 400s plain httpx.
    import twikit_patch
    from xtransport import CurlCffiTransport
    twikit_patch.apply()

    # Defense-in-depth: re-sanitize the cookie file on load (strip any non-hex
    # chars a bad paste may have left) before handing it to twikit.
    try:
        import re as _re
        _ck = json.load(open(COOKIES_PATH))
        _clean = {k: _re.sub(r"[^0-9a-f]", "", str(v).lower()) for k, v in _ck.items()}
        if _clean != _ck:
            json.dump(_clean, open(COOKIES_PATH, "w"))
    except Exception as e:
        print(f"[fetch] cookie sanitize skipped: {e}", file=sys.stderr)

    client = Client("en-US", transport=CurlCffiTransport())
    try:
        client.load_cookies(COOKIES_PATH)
    except Exception as e:
        write_marker(f"cookie-load-failed: {e}")
        return 0

    seen = load_seen()
    collected = {}

    async def add(tweets, source):
        for t in tweets:
            d = extract(t, source)
            if not d["id"] or d["id"] in seen or d["id"] in collected:
                continue
            collected[d["id"]] = d

    # 1) Home / "Following" timeline — your actual feed.
    try:
        home = await client.get_latest_timeline(count=HOME_COUNT)
        await add(home, "home")
    except Exception as e:
        # Auth failures here usually mean expired cookies — surface clearly.
        msg = str(e).lower()
        if "401" in msg or "unauth" in msg or "could not authenticate" in msg or "forbidden" in msg:
            write_marker(f"cookies-expired (run bin/login.py): {e}")
            return 0
        print(f"[fetch] home timeline failed: {e}", file=sys.stderr)

    # 2) Curated roster (no floor) + broad discovery (modest floor in-query).
    #    One retry per source to smooth X's transient 404/rate-limit blips.
    for query, label, product, count in SEARCH_SOURCES:
        for attempt in (1, 2):
            try:
                res = await client.search_tweet(query, product=product, count=count)
                await add(res, label)
                break
            except Exception as e:
                if attempt == 1:
                    await asyncio.sleep(3)
                    continue
                print(f"[fetch] search '{label}' failed: {e}", file=sys.stderr)
        await asyncio.sleep(2)  # spread the burst to stay under X's search window

    # Order by recency (you want to spot things early); tweet IDs are snowflakes
    # (monotonic with time), so higher id = newer. Engagement is only a tiebreak.
    # The distiller selects on substance, not like-count.
    def _recency(t):
        try:
            return (int(t["id"]), signal_score(t))
        except (ValueError, KeyError):
            return (0, signal_score(t))
    items = sorted(collected.values(), key=_recency, reverse=True)

    stamp = datetime.now()
    d = os.path.join(MODULE_ROOT, "raw", stamp.strftime("%Y-%m-%d"))
    os.makedirs(d, exist_ok=True)
    out = os.path.join(d, stamp.strftime("%H%M") + ".json")
    json.dump(
        {"ts": stamp.isoformat(), "tz": stamp.astimezone().tzname(),
         "count": len(items), "tweets": items},
        open(out, "w"), ensure_ascii=False, indent=1,
    )

    # Mark these IDs seen so future runs don't re-surface them.
    for tid in collected:
        seen.add(tid)
    save_seen(seen)

    print(f"[fetch] wrote {len(items)} new items -> {out}", file=sys.stderr)
    print(out)  # stdout: the written path, for the orchestrator
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
