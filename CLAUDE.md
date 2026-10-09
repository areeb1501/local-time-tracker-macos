# Time Tracker — agent context

Fully-local macOS time tracker. Tracks the frontmost app / window / browser
tab, classifies time into categories and projects, and serves a dashboard.
Run `tt info` for a short orientation (paths, live status, database stats);
this file is the deep reference behind it — written for AI coding agents
(Claude Code, Codex, Cursor…) and humans alike.

## Architecture

```
collector.py (launchd daemon, 3s poll) ──▶ ~/.timetracker/events.db (SQLite)
                                                    ▲            ▲
dashboard.py (FastAPI, always on :8321) ────────────┘            │
report.py (email briefs via Resend) ─────────────────────────────┘
```

Classification is **query-time**: raw events never store category/project.
Rules + manual assignments are applied on read (`classify.py`), so editing a
rule retroactively reclassifies all history. Never denormalize category or
project onto the events table.

## Repository layout

```
bin/tt                 CLI (zsh) — renders launchd plists, wraps every command
install.sh             one-line installer (curl | bash)
timetracker/           the Python package (run modules with `python -m timetracker.<name>`)
  collector.py  db.py  classify.py  dashboard.py  ai_categorize.py  report.py
  onboard.py  config.py  demo.py  static/
tests/                 unittest suite: python -m unittest discover -s tests -t .
docs/media/            README screenshots & GIFs (demo data only)
scripts/media/         Playwright scripts that regenerate docs/media
```

Modules import each other relatively (`from . import db`). launchd runs
`python -m timetracker.collector` / `uvicorn timetracker.dashboard:app` with
`PYTHONPATH` = the install dir.

## Files (in `timetracker/` unless noted)

| File | What it is |
|---|---|
| `collector.py` | Daemon: frontmost app via `CGWindowListCopyWindowInfo`, window title via AX API, browser tab via `osascript`, AFK/lock/sleep handling (incl. `display_sleep_prevented()` so playing video isn't billed to AFK), heartbeat-merged writes |
| `db.py` | Schema + `init_db()` (also runs migrations, e.g. `projects.parent_id`) + seed category rules. `active_timer` holds the single live "track this now" timer |
| `classify.py` | Rule matcher, `project_spans()` (manual assignments override rules), `group_events()` (Rize-style visits, 180s gap tolerance), `unassigned_prompt_blocks()` (Overview reminder for completed long unassigned visits; coalesces Meet + recorder fragments; never writes a project onto events), `coding_tool()` (agents from terminal/editor titles), `is_ai()`/`AI_TOOLS`/`load_ai_rules()` (AI-usage detection: agents, AI IDEs, AI sites/apps, user 'ai' rules). Perf machinery (see gotcha below): `AssignmentSweep`, per-request memo caches on `categorize()`/`rule_project()`/`tool_and_ai()` |
| `dashboard.py` | FastAPI: overview (single-scan bundle of summary+coding_tools+ai_usage+distractions+focus — what the Overview tab calls; keep its output in lockstep with the standalone endpoints. `distractions` = time in `distraction_categories` from config.json (default Entertainment, editable in Settings → Time tracking), broken down by domain/app + a daily trend), summary, heatmap, visits, ai_usage, focus (fragmentation), search, project_report, project_detail, sessions (+ `/api/session_detail` — one-scan drill-in bundle: list-row metrics + category/app/site/project/title/AI-tool breakdowns, focus facts, timeline, visit blocks; keep its metrics in lockstep with `_session_metrics`), projects/rules/assignments CRUD + projects/merge + projects/{id}/archive|unarchive, live timer (`/api/timer` start/stop), AI propose/apply/suggest/find_project, brief, settings, chat (Ask tab: `/api/chat` + `/api/chat/threads` CRUD; `usage_digest()` builds a ~4-5KB text digest of 7d/30d usage + rhythm + top titles that goes into the chat system prompt — the model never sees raw events). Project totals roll up the **whole subtree** (any depth) via `child_map()`/`subtree_ids()` |
| `ai_categorize.py` | AI categorizer: model registry (Claude Haiku/Sonnet, Gemini, Ollama), `call_llm(model, system, user, schema)` (schema-forced JSON), `chat_llm(model, system, messages)` (free-text multi-turn, used by the Ask tab), `propose()` — proposals only, writes happen via `/api/ai/apply` after user approval |
| `report.py` | Email briefs (today/yesterday/week/month): inline-CSS charts, prev-period deltas, AI insights; the month brief adds stat tiles + week-by-week bars (`month_extras_html`) and deeper splits; Resend API (**must send a real User-Agent — default urllib UA gets 403**) |
| `static/index.html` | Entire UI, single file, vanilla JS + inline CSS, no build step. Rendering is stale-while-revalidate: each tab = fetch + pure `build*Html(data)` fn; `swr()` paints cached data instantly on tab/range *navigation* then refetches, re-renders of the on-screen view keep the old DOM until fresh HTML is ready (no stale flash after mutations), and `paint()` skips the DOM write entirely when HTML is unchanged (kills auto-refresh flicker). `/api/projects` loads in parallel via `projectsReady`; Activity paging/toggles and Settings rule-edit mode rebuild locally from `dataCache` without refetching. Build fns must (re)install the `window._*` vars their inline handlers index into. Small cache entries persist to localStorage (`tt_swr_v1`, 3-day TTL; activity/ask excluded) so app reopen paints instantly before revalidating; after first paint, `prefetchRanges()` warms the other ranges + projects report once per load; tab/range navigation crossfades via the View Transitions API (120ms) — `paint(html, {nav, then})` runs `then` after the DOM write since transitions write async. Ask tab (`#/ask/<thread?>`): optimistic send via `paintAsk()`, `mdLite()` renders reply markdown, range buttons hidden. Hash routing: `#/overview?range=today`, `#/projects`, `#/projects/<id>` (old `#/project/<id>` still works), `#/sessions`, `#/sessions/<id>` (session drill-in), `#/activity`, `#/settings`. Installable as a PWA (`static/manifest.json` served at `/manifest.json` so scope covers `/`, icons in `static/icon-*.png`; **no service worker on purpose** — offline caching would show stale data and the app needs the local server anyway) |
| `bin/tt` | Control script: setup/config/start/stop/restart/status/info/open/log/report/demo/update/uninstall. Renders the launchd plists itself, so paths always match the install dir (symlinked to `~/.local/bin/tt` by `install.sh`) |
| `install.sh` | One-line installer: checks macOS + Python ≥3.10, downloads the repo, private venv, links `tt`, runs onboarding (prompts read `/dev/tty` so `curl \| bash` works). Root of the repo |
| `onboard.py` | `tt setup` wizard, split into sections (tracking, projects, ai, email, permissions). `--only <section>` powers `tt config email|ai|…`; `--test-email` sends a Resend test; `--yes` for non-interactive |
| `config.py` | `tt config` list/get/set/unset/edit over config.json, with a typed `SETTINGS` registry (secrets masked on output). Dashboard + collector re-read config live |
| `demo.py` | Fictional ~5-week dataset for `tt demo` / screenshots. Refuses to wipe the default data dir |

## Data & config (in `$TT_HOME`, default ~/.timetracker)

- `events.db` — tables: `events`, `projects` (arbitrary-depth `parent_id`
  hierarchy; kind 'project'|'subject'|'task'; ≤8 **unarchived** children per
  parent, enforced by `MAX_CHILDREN` in dashboard.py — a "task"/assignment/
  feature is just a child project; `archived` flag: archive takes the whole
  subtree, unarchive also clears ancestors; archived projects keep all history
  and still roll up into parent totals, they're only hidden from pickers and
  the main Projects list — they live in the 📦 Archived section; `pinned` flag (📌, `/api/projects/{id}/pin|unpin`): pinned projects sort first in the Projects table/bars, Overview by-project, pickers and Settings, and show in the report even with no time), `rules` (kind 'category'|'project'|'ai'; priority asc, first
  match wins; 'ai' = count matching time as AI usage), `assignments` (manual/AI
  project overrides — also how the live timer credits a task's tracked window),
  `manual_entries` (offline logged time), `sessions`, `active_timer` (≤1 row),
  `chat_threads`/`chat_messages` (Ask-tab chats, saved locally; FK cascade on
  thread delete), `review_skips` (user dismissed a long unassigned block —
  hides the Overview reminder for overlapping same-key visits; never a project)
- `config.json` (chmod 600) — `resend_api_key`, `anthropic_api_key`, `gemini_api_key`,
  `ai_model`, `email_from`, `email_to`, `weekly_email` (loads the Monday agent), `afk_idle_minutes` (idle→AFK threshold,
  default 5), `unassigned_prompt_minutes` (Overview reminder for completed
  blocks with no project, default 1; meetings included even when the URL is
  opaque). **Never commit or print keys.**
- `collector.log`, `dashboard.log`, `report.log`

## Services (launchd, run at login)

Labels use the prefix `$TT_LABEL` (default `io.github.localtimetracker`):

| Label | What |
|---|---|
| `<prefix>.collector` | collector daemon (KeepAlive, ProcessType Background) |
| `<prefix>.dashboard` | uvicorn on 127.0.0.1:`$TT_PORT` (default 8321; KeepAlive, ProcessType Interactive) |
| `<prefix>.weekly` | `report.py --period week`, Mondays 08:00 (only if `weekly_email` is true) |

Env overrides `TT_HOME`, `TT_PORT`, `TT_LABEL` let you run an isolated second
copy (tests, demos) next to a real install.

## How to make changes

1. Edit code.
2. Backend change → `tt restart`. UI-only change → just reload
   the browser; `index.html` is served from disk per request.
3. Verify: `bin/tt status`, `curl http://127.0.0.1:8321/api/summary?start=...&end=...`,
   and syntax-check the inline JS with `node --check` on the extracted `<script>`.
4. Schema changes go in `db.py` as idempotent migrations inside `init_db()`
   (PRAGMA table_info check → ALTER), because both collector and dashboard call it.

## Gotchas (learned the hard way)

- **Never use `NSWorkspace.frontmostApplication()` in the daemon** — it goes
  stale without a run loop. Use the CGWindowList approach in `frontmost_app()`.
- macOS TCC: Accessibility must be granted to the real Python binary behind the venv (window titles); per-browser
  Automation prompts on first osascript use. Screen Recording is NOT needed.
- Time accuracy invariants: idle-to-AFK threshold is configurable
  (`afk_idle_minutes` in config.json, default 5; collector re-reads it every
  ~60s, no restart); screen lock → AFK immediately; tick gap >9s (sleep)
  discarded. Don't break these. Reading/quiz time has no input, so the threshold
  is the main lever — raising it credits more silent-presence time (capped per
  absence by the threshold), lowering it is stricter. Exposed in Settings →
  Time tracking (`GET /api/settings`, saved via `/api/ai/settings`).
- AFK backdates to `self.last_present` (last tick with input OR media), not
  `now - idle`, so time credited during the grace window / a lecture is never
  wiped when it ends.
- Passive video/lecture watching (no input) is kept out of AFK by two in-process
  signals (ctypes, no subprocess), gated on a browser/`VIDEO_PLAYER_APPS` player
  being frontmost: `display_sleep_prevented()` (IOKit `PreventUserIdleDisplaySleep`
  power assertion — held only while a video plays) OR `audio_playing()`
  (CoreAudio `kAudioDevicePropertyDeviceIsRunningSomewhere` on the default output).
  NOTE: Chromium holds the audio device "running" whenever a tab has a media
  element (so `audio_playing()` can read True with nothing audible) and drops the
  display assertion intermittently — hence both are ORed, gated on a browser, and
  capped at `MEDIA_WATCH_MAX_SECS` (2h) of no input so a stuck signal can't
  inflate forever.
- Resource budget is a hard requirement: collector must stay ~1 process,
  <50MB, ~0% CPU. No extra daemons, no polling web requests in the collector.
- Design goal #1: never inflate time and never heat the machine.
- Anthropic API: structured outputs via `output_config={"format": {...}}`;
  handle `stop_reason == "refusal"`. Models: claude-haiku-4-5 (default),
  claude-sonnet-5.
- Single-instance lock: collector takes flock on `$TT_HOME/collector.lock`.
- **launchd QoS**: the dashboard plist must keep `ProcessType: Interactive` —
  `Background` pins uvicorn to efficiency cores with darwin throttling and
  every request runs ~4-5× slower. The **collector's** plist stays
  `Background` on purpose (resource budget). `tt` renders both; plist
  changes need `tt stop && tt start` (kickstart alone doesn't re-read them).
- The Overview tab makes ONE `/api/overview` call (plus heatmap). Don't split
  it back into parallel per-card fetches: concurrent CPU-bound endpoints
  serialize on the GIL, so the page pays the sum of all of them.
- **Aggregation perf invariants** (query-time classification is CPU-bound, not
  DB-bound; ~8k events/week): hot loops (summary, overview, visits,
  project_report, project_detail, _session_metrics) iterate events **in
  ascending start_ts order** and use `classify.AssignmentSweep` — one O(events+assignments) sweep
  instead of rescanning every assignment per event. Keep the ORDER BY if you
  touch those queries. Rule verdicts are memoized per request in plain dicts
  passed as `cache=`/`rule_cache=` (keyed on the event's field tuple). Those
  dicts come from `classify.shared_cache(kind, rules)`, which **persists across
  requests** but is keyed on a fingerprint of the exact rule list (ids,
  patterns, priorities, project names), so any rule/project edit swaps in a
  fresh dict — never hand a plain long-lived dict to `cache=`. Cold
  classification of all history is ~1.7M regex checks (~3s), so
  `warm_rule_caches()` refills the caches in a background thread at startup
  and after rule create/update/delete and project create/merge/delete; call
  `warm_in_background()` from any new endpoint that changes rules or project
  names. Warm all-time overview ≈0.5s, project_report ≈0.25s (was ~3.5s).
  `tool_and_ai()`'s cache key ignores is_afk, so only pass it non-AFK rows.
  `project_spans(start=, end=)` clips without copying the row. `visits`
  classifies each event once in the ascending scan (visits interleave event
  ids, so the sweep can't run per-visit). The big read endpoints (overview,
  visits) return `JSONResponse(...)` directly — FastAPI's jsonable_encoder
  walk costs ~60ms on an ~800KB payload; keep payloads plain dict/list/num.
- **File paths**: `events.path` holds the focused window's
  `AXDocument` (open document, or a terminal's cwd in Ghostty/Terminal), read
  from the AX window element the collector already holds each tick. No
  subprocess. Office apps that don't publish it get one osascript
  (`office_doc_path`), only when the window changes. A browser `file://` URL
  counts as a path at query time (`classify.event_path`). Rule field `path`: new
  path rules default to priority 20, so they beat title/URL keyword rules.
  `load_rules` also appends implicit rules (`implicit: True`, priority 10000):
  a path containing a folder named exactly like a project links to it. Don't
  widen title rules to match paths ("pid", "job" would pull in random folders).
  `_event_key` includes the path, so the same title in two folders doesn't
  share a cached verdict.
- Agent spinner frames include `◐◑◒◓` as well as braille/star. Keep
  `_AGENT_SPINNER_PREFIX` (collector) and `_CLAUDE_TITLE` (classify) in sync, or
  every frame becomes a new event row.
- WAL is capped via `PRAGMA journal_size_limit` in db.connect(); DB is ~1.8MB
  per week of events.
