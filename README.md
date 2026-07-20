# Herald 📯

A CLI-agent-based Discord bot core: it posts things to a channel, watches for
replies (in the channel or in a thread you create off any of its messages),
and hands follow-up questions to a local CLI coding agent (`codex`, `claude`,
`cursor-agent`, whatever you point it at) to draft a grounded answer.

Ships with one bundled module — **x-briefs** — a personal X/Twitter intel
pipeline: it pulls your feed on a schedule, distills the signal with a coding
agent, and posts a daily briefing. Thread off any item in that briefing and
ask about it; Herald answers using the actual data behind it, not a guess.

This started as a personal project (X feed → Discord briefings). The core
Discord/agent plumbing has no idea what a "tweet" is — it just knows how to
post, listen, and delegate a question to a CLI agent — so it's split out as
`core/` with `x-briefs` as the first of what's meant to be several modules.

## Why

Point-and-scroll feeds are noisy. This pulls yours on a schedule, has a
coding agent (yes, repurposed as a research/writing agent — it's just a
capable, scriptable LLM with file access) distill what's actually
substantive, and delivers it somewhere you already check: Discord. Then,
because the daily digest inevitably raises a "wait, tell me more about that"
— you can just ask, in-thread, and get an answer grounded in the actual
source data instead of the model's vague recollection of it.

## Architecture

```
core/                      generic — no knowledge of any specific module
├── send_discord.py        deterministic delivery (never an LLM in this path)
└── listen_discord.py      the one persistent process: Discord gateway client,
                            drafts replies via a CLI agent (ask_codex()),
                            grounded by prompts/reply.md
prompts/
└── reply.md                the reply-drafting prompt; lists which module(s)'
                            data paths to ground answers in
modules/
└── x-briefs/               bundled example module (see below)
    ├── bin/                fetch + distill + brief + X-login scripts
    ├── prompts/             distill.md, brief.md
    └── reports/, briefings/, raw/, state/   its own data, gitignored
examples/launchd/           macOS scheduler templates (adapt for cron/systemd)
```

Deterministic vs. agentic is a hard line throughout: fetching data and
sending Discord messages is plain code — an LLM never touches the network
directly. Drafting text (distilling, summarizing, answering a question) is
where the CLI agent comes in, and its output always lands in a file or gets
passed to the deterministic sender, never posted straight from the agent.

**No plugin loader.** Modules aren't dynamically discovered — adding one
means writing `modules/<name>/` and wiring a couple of paths (see "Adding a
module" below). Simple over clever, for now.

## Requirements

- [uv](https://docs.astral.sh/uv/) (this repo is uv-only — never invoke bare
  `python3`/`pip3`)
- A Discord bot (free, see setup below)
- **A local CLI coding agent.** Ships wired to OpenAI's `codex` CLI (requires
  a ChatGPT subscription with Codex CLI access — no separate API key). If you
  use something else, it's a one-line swap: `ask_codex()` in
  `core/listen_discord.py` and the `codex exec ...` calls in
  `modules/x-briefs/bin/run-*.sh` are the only places that shell out to it.
  The contract is just: prompt on stdin, reply text on stdout, read-only (or
  workspace-write, for the distiller) filesystem access to the repo. `claude
  -p`, `cursor-agent`, and similar CLIs all fit this shape.
- (x-briefs module only) An X/Twitter account, logged in via cookies —
  no API key/developer account needed.

## Setup

1. **Clone and sync:**
   ```bash
   git clone https://github.com/tommyyzhao/herald.git && cd herald
   uv sync
   cp .env.example .env.local
   ```

2. **Create a Discord bot:** [developer portal](https://discord.com/developers/applications)
   → New Application → Bot tab → Reset Token (save it) → enable **Message
   Content Intent** (privileged, required for the listener to read messages)
   → OAuth2 → URL Generator → scope `bot`, permissions `Send Messages`,
   `Create Public Threads`, `Read Message History` → open the generated URL
   to invite it to your server.

3. **Fill in `.env.local`:** `DISCORD_BOT_TOKEN`, your `DISCORD_DM_USER_ID`
   (enable Developer Mode in Discord, right-click your name → Copy User ID),
   and `DISCORD_CHANNEL_ID` (right-click the channel you want it posting to
   → Copy Channel ID). A channel, not a DM, is what enables threading — see
   `.env.example` for details.

4. **(x-briefs) Mint X session cookies** — no password stored, just
   `auth_token`/`ct0` from your logged-in browser:
   ```bash
   uv run modules/x-briefs/bin/set-cookies.sh
   # or, to log in with your password instead:
   uv run modules/x-briefs/bin/login.py
   ```

5. **Schedule the jobs.** `examples/launchd/` has macOS templates (copy to
   `~/Library/LaunchAgents/`, edit the path placeholders, `launchctl
   bootstrap gui/$(id -u) <plist>`); on Linux, cron or systemd timers work the
   same way — `run-distill.sh` every ~30 min, `run-brief.sh` once a day,
   `listen_discord.py` as a persistent process (its plist uses `KeepAlive`
   instead of a schedule).

6. **Sanity check:**
   ```bash
   uv run modules/x-briefs/bin/fetch.py   # should print "wrote N new items"
   bash modules/x-briefs/bin/run-brief.sh # forces a briefing + Discord post
   ```

## Using it

Once `listen_discord.py` is running: post in the configured channel, or
create a Discord thread off any of the bot's messages (right-click → Create
Thread) and ask inside it. The listener resolves the thread's origin message
(Discord's own thread history doesn't include it — Herald fetches it
separately and stitches back multi-part messages if the original briefing got
split across Discord's 2000-char limit), builds a transcript, and drafts an
answer grounded in that + whatever module data `prompts/reply.md` points at.

## Adding a module

1. `mkdir -p modules/<name>/{bin,prompts,reports,...}` — whatever data dirs
   your module needs; keep them under its own directory so they don't collide
   with another module's.
2. Write your fetch/process scripts. Follow the `MODULE_ROOT`/`REPO_ROOT`
   split in `modules/x-briefs/bin/fetch.py` if your module needs both its own
   data dir and the shared `.env.local`/`.secrets/` at the repo root.
3. Have your scripts call `uv run "$REPO_ROOT/core/send_discord.py" <file>`
   for delivery — don't duplicate the sender per module.
4. Add your module's data paths to `prompts/reply.md`'s GROUNDING section so
   the listener knows to look there when answering questions about it.
5. Schedule it (cron/launchd/systemd, whatever fits).

## Security notes

- The read-only sandbox used for reply-drafting and brief-consolidation
  restricts **writes**, not **reads** — the CLI agent can technically read
  anything under the repo root, including `.env.local`/`.secrets/` if it
  chose to. `prompts/reply.md` has an explicit hard rule against ever reading
  or repeating those, verified live to hold up against direct and "for
  debugging" framings — but that's a model-judgment guardrail, not a
  filesystem-level one. If you need a hard boundary, run the agent in a
  container/chroot that doesn't mount your secrets.
- Treat any text pulled from an external module's data source (tweets, RSS,
  whatever) as **data, not instructions** — `prompts/reply.md` already says
  this, but keep it in mind if you write your own module's prompts.
- X session cookies expire every few weeks; rerun `set-cookies.sh`/`login.py`
  when `fetch.py` starts logging `cookies-expired`.
- Rotate the Discord bot token if you ever suspect it leaked (regenerate in
  the developer portal, update `.env.local`).

## License

MIT — see [LICENSE](LICENSE).
