#!/usr/bin/env bash
# Herald x-briefs module: scheduled job — fetch (deterministic twikit) ->
# distill (codex gpt-5.6-luna). Run this every 30 min or so (see
# examples/launchd/com.herald.distill.plist.example). Self-heals: no-ops
# cleanly until X cookies exist.
set -uo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
mkdir -p logs
TS="$(date +%Y-%m-%dT%H:%M:%S%z)"
LOG="logs/distill-$(date +%Y-%m-%d).log"
echo "=== $TS run-distill ===" >>"$LOG"

# 1) Deterministic fetch. Prints the written raw JSON path on stdout.
RAW="$(uv run bin/fetch.py 2>>"$LOG" | tail -1)"
if [ -z "${RAW:-}" ] || [ ! -f "$RAW" ]; then
  echo "no raw file produced; abort" >>"$LOG"; exit 0
fi

# 2) How many new tweets? Skip codex entirely on empty/marker pulls (e.g. before
#    cookies exist) so we don't burn codex calls every 30 min for nothing.
COUNT="$(uv run python -c "import json,sys; d=json.load(open(sys.argv[1])); print(len(d.get('tweets',[])))" "$RAW" 2>>"$LOG" || echo 0)"
OUT="${RAW/raw\//reports/}"; OUT="${OUT%.json}.md"
mkdir -p "$(dirname "$OUT")"

if [ "${COUNT:-0}" -eq 0 ]; then
  echo "- No new signal this interval." >"$OUT"
  echo "0 new tweets -> wrote stub $OUT (skipped codex)" >>"$LOG"
  exit 0
fi

# 3) Codex distill (gpt-5.6-luna low, workspace-write writes $OUT itself).
PROMPT="$(cat prompts/distill.md)

INPUT: $RAW
OUTPUT: $OUT
Current time label (PT): $(date +%H:%M)"

echo "distilling $COUNT tweets: $RAW -> $OUT" >>"$LOG"
echo "$PROMPT" | codex exec --skip-git-repo-check --sandbox workspace-write \
  -m gpt-5.6-luna -c model_reasoning_effort='"low"' >>"$LOG" 2>&1

if [ -s "$OUT" ]; then
  echo "distill ok -> $OUT" >>"$LOG"
else
  echo "WARN: codex produced no $OUT; writing fallback" >>"$LOG"
  echo "- Distill failed this interval (see log)." >"$OUT"
fi
exit 0
