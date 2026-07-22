"""
Runtime patches that make twikit work against X's current (mid-2026) defenses.

Two problems, both patched here:

1. x-client-transaction-id generation. twikit's regexes for the `ondemand.s`
   bundle broke when X moved to a webpack chunk index (~2026-03-18; twikit
   issue #408, PRs #410/#411) — fixed by the updated regexes below.

   A second, separate break (found 2026-07-21): X migrated its ANONYMOUS
   logged-out landing page to a Vite/rolldown bundler (`__vite__mapDeps`,
   `assets/*-<hash>.js`, served from `abs.twimg.com/x-web/x-web/`) — the
   `ondemand.s` reference doesn't exist anywhere in that page or its chunks
   at all anymore. But the AUTHENTICATED app (`x.com/` or `x.com/home` when
   the request carries real session cookies) still serves the OLD
   webpack-based bundle with `ondemand.s` intact — X has only migrated the
   logged-out variant so far. So: fetch WITH the session's real cookies
   attached, not anonymously. (twikit's own upstream `init()` already does
   this via the passed-in authenticated `session` — our patch used to
   deliberately go around it because the authenticated homepage 400'd via a
   bare browser fingerprint; that's no longer true, or at least isn't via
   curl_cffi carrying the actual cookie jar. If X starts 400ing this again,
   the fallback is to keep the anonymous fetch but point at whatever new
   endpoint still ships the legacy bundle — check `x.com/home` if `x.com/`
   ever migrates too.)

2. (Transport is swapped separately — see xtransport.CurlCffiTransport — because
   X fingerprint-blocks plain httpx.)

Remove the regex/init patch once twikit ships a release that fixes #408.
"""
import re

import bs4
from curl_cffi.requests import AsyncSession

from twikit.x_client_transaction import transaction as _tx
from twikit import user as _user_mod
from twikit import tweet as _tweet_mod

_ON_DEMAND_FILE_REGEX = re.compile(r',(\d+):["\']ondemand\.s["\']')
_ON_DEMAND_HASH_PATTERN = r',{}:["\']([0-9a-f]+)["\']'
_INDICES_REGEX = re.compile(r'\[(\d+)\],\s*16')


async def _patched_init(self, session, headers):
    """Build the transaction key from the AUTHENTICATED homepage — the
    session's real cookies are required now (see module docstring); the
    transaction key itself is still not tied to any specific request being
    signed, just to a live, cookie-bearing session."""
    cookies = dict(session.cookies) if hasattr(session, "cookies") else {}
    async with AsyncSession() as s:
        if cookies:
            s.cookies.update(cookies)
        html = (await s.get("https://x.com/", impersonate="chrome", timeout=30)).text
        soup = bs4.BeautifulSoup(html, "lxml")
        chunk = _ON_DEMAND_FILE_REGEX.search(html)
        if not chunk:
            raise Exception("ondemand.s chunk index not found in homepage")
        hash_match = re.search(_ON_DEMAND_HASH_PATTERN.format(chunk.group(1)), html)
        if not hash_match:
            raise Exception("ondemand.s hash not found in homepage")
        url = (
            "https://abs.twimg.com/responsive-web/client-web/"
            f"ondemand.s.{hash_match.group(1)}a.js"
        )
        js = (await s.get(url, impersonate="chrome", timeout=30)).text

    indices = [int(x) for x in _INDICES_REGEX.findall(js)]
    if not indices:
        raise Exception("Couldn't get KEY_BYTE indices")

    self.home_page_response = soup
    self.DEFAULT_ROW_INDEX, self.DEFAULT_KEY_BYTES_INDICES = indices[0], indices[1:]
    self.key = self.get_key(self.home_page_response)
    self.key_bytes = self.get_key_bytes(self.key)
    self.animation_key = self.get_animation_key(self.key_bytes, self.home_page_response)


# --- Schema-drift tolerance -------------------------------------------------
# twikit reads dozens of `legacy[...]` keys with [] indexing; X periodically
# drops fields (e.g. `withheld_in_countries`), which crashes parsing of an
# entire timeline. Wrap the legacy dict so missing keys yield None instead.

class _Defaulting(dict):
    # Missing key -> empty defaulting dict, so even chained access like
    # legacy['entities']['description']['urls'] never raises KeyError.
    def __missing__(self, key):
        return _Defaulting()


def _deep_default(obj):
    if isinstance(obj, _Defaulting):
        return obj
    if isinstance(obj, dict):
        return _Defaulting({k: _deep_default(v) for k, v in obj.items()})
    if isinstance(obj, list):
        return [_deep_default(v) for v in obj]
    return obj


def _wrap_legacy(data):
    if isinstance(data, dict) and isinstance(data.get("legacy"), dict):
        wrapped = dict(data)
        wrapped["legacy"] = _deep_default(data["legacy"])
        return wrapped
    return data


_orig_user_init = _user_mod.User.__init__
_orig_tweet_init = _tweet_mod.Tweet.__init__


def _safe_user_init(self, client, data):
    _orig_user_init(self, client, _wrap_legacy(data))


def _safe_tweet_init(self, client, data, user=None):
    _orig_tweet_init(self, client, _wrap_legacy(data), user)


def apply():
    """Patch twikit's ClientTransaction + parsers for X's current responses."""
    _tx.ON_DEMAND_FILE_REGEX = _ON_DEMAND_FILE_REGEX
    _tx.INDICES_REGEX = _INDICES_REGEX
    _tx.ClientTransaction.init = _patched_init
    _user_mod.User.__init__ = _safe_user_init
    _tweet_mod.Tweet.__init__ = _safe_tweet_init
