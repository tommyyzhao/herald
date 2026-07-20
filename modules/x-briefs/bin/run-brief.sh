#!/usr/bin/env bash
# Herald x-briefs module: scheduled job — consolidate today's distills (codex
# gpt-5.6-terra medium) -> briefing file -> deterministic Discord post ->
# daily git commit. Run this on whatever schedule you like (see
# examples/launchd/com.herald.brief.plist.example for a once-daily example).
set -uo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"       # modules/x-briefs
REPO_ROOT="$(cd "$ROOT/../.." && pwd)"         # repo root, for core/send_discord.py
cd "$ROOT"
mkdir -p logs briefings
DATE="$(date +%Y-%m-%d)"
LOG="logs/brief-$DATE.log"
echo "=== $(date +%Y-%m-%dT%H:%M:%S%z) run-brief ===" >>"$LOG"

REPORT_DIR="reports/$DATE"
BRIEF="briefings/$DATE.md"

# Gather today's chunk reports.
shopt -s nullglob
CHUNKS=("$REPORT_DIR"/*.md)
shopt -u nullglob

if [ ${#CHUNKS[@]} -eq 0 ]; then
  printf '# 📯 Herald Daily Briefing — %s\n\nQuiet day — no distill reports captured.\n\n— 📯 Herald\n' "$DATE" >"$BRIEF"
  echo "no chunks; wrote quiet-day briefing" >>"$LOG"
else
  PROMPT="$(cat prompts/brief.md)

TODAY: $DATE
CHUNK REPORTS (read all):
$(printf '%s\n' "${CHUNKS[@]}")"

  # Codex gpt-5.6-terra, medium reasoning, READ-ONLY — emits briefing to stdout only.
  RAWOUT="$(echo "$PROMPT" | codex exec --skip-git-repo-check --sandbox read-only \
    -m gpt-5.6-terra -c model_reasoning_effort='"medium"' 2>>"$LOG")"

  # Extract between sentinels deterministically.
  uv run python - "$BRIEF" >>"$LOG" 2>&1 <<PY
import re, sys
raw = """$RAWOUT"""
m = re.search(r"===BRIEF-START===\s*(.*?)\s*===BRIEF-END===", raw, re.S)
body = m.group(1).strip() if m else raw.strip()
if not body:
    body = "# 📯 Herald Daily Briefing — $DATE\n\nBriefing generation returned empty.\n\n— 📯 Herald"
open(sys.argv[1], "w").write(body + "\n")
print("wrote briefing len", len(body))
PY
  echo "consolidated ${#CHUNKS[@]} chunks -> $BRIEF" >>"$LOG"
fi

# Deterministic Discord delivery (no agent) — send_discord.py is core/, shared
# across every module, not duplicated per-module.
if uv run "$REPO_ROOT/core/send_discord.py" "$BRIEF" >>"$LOG" 2>&1; then
  echo "discord sent" >>"$LOG"
else
  echo "WARN: discord send failed (see log)" >>"$LOG"
fi

# Daily history commit of the durable data (reports + briefings).
git add reports briefings 2>>"$LOG"
if ! git diff --cached --quiet 2>/dev/null; then
  git commit -q -m "data: $DATE intel reports + daily briefing" >>"$LOG" 2>&1 \
    && echo "committed daily data" >>"$LOG"
fi
exit 0
