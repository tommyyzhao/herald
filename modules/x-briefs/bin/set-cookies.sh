#!/usr/bin/env bash
# Mint X cookies for twikit from your already-logged-in browser session.
# Prompts are hidden (read -s) so nothing sensitive echoes or hits a chat log.
#
# Where to find the values (Chrome/Arc/Brave, logged into x.com):
#   DevTools (Cmd+Opt+I) → Application → Storage → Cookies → https://x.com
#   copy the VALUE of `auth_token` and `ct0`.
set -euo pipefail

MODULE_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
REPO_ROOT="$(cd "$MODULE_ROOT/../.." && pwd)"
cd "$MODULE_ROOT"
DEST="$REPO_ROOT/.secrets/x-twikit-cookies.json"
mkdir -p "$REPO_ROOT/.secrets"

read -rsp "Paste X auth_token value: " AUTH; echo
read -rsp "Paste X ct0 value:        " CT0;  echo

uv run python - "$DEST" "$AUTH" "$CT0" <<'PY'
import json, os, re, sys
dest = sys.argv[1]
# Strip everything that isn't a hex char — terminal paste can inject ANSI escape
# sequences (e.g. "\x1b[" from bracketed paste) that corrupt the Cookie header.
auth = re.sub(r'[^0-9a-f]', '', sys.argv[2].strip().lower())
ct0  = re.sub(r'[^0-9a-f]', '', sys.argv[3].strip().lower())
if len(auth) != 40:
    raise SystemExit(f"auth_token should be 40 hex chars, got {len(auth)} — re-copy it")
if len(ct0) < 150:
    raise SystemExit(f"ct0 should be ~160 hex chars, got {len(ct0)} — re-copy it")
json.dump({"auth_token": auth, "ct0": ct0}, open(dest, "w"))
os.chmod(dest, 0o600)
print(f"wrote {dest} (auth_token={len(auth)} hex, ct0={len(ct0)} hex, perms 600)")
PY

echo "Done. Verify with:  (cd $MODULE_ROOT && uv run bin/fetch.py)"
