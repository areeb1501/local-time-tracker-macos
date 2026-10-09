<p align="center">
  <img src="docs/media/banner.svg" alt="Local Time Tracker" width="100%">
</p>

<p align="center">
  <b>See where your time goes, without sending your data anywhere.</b><br>
  A lightweight time tracker for macOS. It runs in the background, sorts your time into projects<br>
  and shows how much of your work is AI-assisted.
</p>

<p align="center">
  <img alt="macOS" src="https://img.shields.io/badge/macOS-12%2B-000?logo=apple&logoColor=white">
  <img alt="Private" src="https://img.shields.io/badge/data-stays%20on%20your%20Mac-1f2329">
  <img alt="Lightweight" src="https://img.shields.io/badge/memory-~15%20MB-22c55e">
  <img alt="AI" src="https://img.shields.io/badge/AI-Claude%20·%20Gemini%20·%20Ollama-6b7280">
  <img alt="License" src="https://img.shields.io/badge/license-MIT-green">
  <a href="https://github.com/areeb1501/local-time-tracker-macos/actions/workflows/test.yml"><img alt="tests" src="https://github.com/areeb1501/local-time-tracker-macos/actions/workflows/test.yml/badge.svg"></a>
</p>

<p align="center">
  <a href="#-get-started">Get started</a> ·
  <a href="#-features">Features</a> ·
  <a href="#-why-this-one">Why this one</a> ·
  <a href="#%EF%B8%8F-settings">Settings</a> ·
  <a href="#-how-it-works">How it works</a> ·
  <a href="#-faq">FAQ</a>
</p>

<p align="center">
  <img src="docs/media/tour.gif" alt="A short tour of the dashboard" width="92%">
</p>

> Screenshots use made-up demo data. Run `tt demo` to explore it yourself.

---

## 🚀 Get started

```bash
curl -fsSL https://raw.githubusercontent.com/areeb1501/local-time-tracker-macos/main/install.sh | bash
```

A short setup follows. Every step is optional:

1. **Away time:** how many idle minutes count as "away" (default 5).
2. **Projects:** name what you work on, with an optional keyword or folder to match.
3. **AI:** Claude, Gemini or Ollama (fully local), or skip.
4. **Email summaries:** a free Resend key for a weekly recap.
5. **Permission:** it opens the right macOS settings page for you.

Tracking then runs in the background and starts again after every restart. Open the dashboard with `tt open`.

<p align="center">
  <img src="docs/media/onboarding.gif" alt="Install and setup in the terminal" width="85%">
</p>

---

## ✨ Features

### Your day at a glance
Active time, deep work, distractions, AI usage, and top apps and categories, for today, yesterday, 7 days or 30 days.

<p align="center"><img src="docs/media/overview.png" alt="Overview" width="92%"></p>

### Projects that sort themselves
Link a folder, website, app or window title to a project once, and matching time is filed there automatically. Edit a rule and **your whole history updates**.

<p align="center"><img src="docs/media/projects.png" alt="Projects" width="92%"></p>

### AI suggests your projects
The AI reads through your recent activity and spots recurring work that doesn't have a project yet. For each one it proposes:

- a **project name**,
- **matching rules** (sites, apps, titles), and
- the time it would cover.

It can also file unsorted time into your existing projects. You review and approve everything; nothing changes on its own.

### Nest as deep as you like
Put tasks under projects, and sub-tasks under those, as many levels as you need. Totals roll up automatically. Each project has its own page with daily time, apps, sites, AI share and a **Track this now** timer. **Archive** finished projects without losing history, and **pin** the ones you care about.

<p align="center"><img src="docs/media/project-detail.png" alt="Project page" width="92%"></p>

### How much of your work is AI
Tracks time in AI tools: Claude Code, Codex and other agents in your terminal, Cursor and other AI editors, and ChatGPT, Claude, Gemini, Grok and Perplexity. You see AI share per day, per project and per session.

<p align="center"><img src="docs/media/ai-usage.png" alt="AI usage" width="80%"></p>

### When you focus
An hourly heatmap, context switches per hour and your longest deep-work streaks.

<p align="center">
  <img src="docs/media/heatmap.png" alt="Activity heatmap" width="49%">
  <img src="docs/media/focus.png" alt="Focus" width="49%">
</p>

### Focus sessions
Start a session for a project, then see how focused you were, what pulled you away and a minute-by-minute timeline.

<p align="center">
  <img src="docs/media/sessions.png" alt="Sessions" width="49%">
  <img src="docs/media/session-detail.png" alt="Session detail" width="49%">
</p>

### Search everything
Search every window title, page and file you've had open, and assign any of it to a project in one click.

<p align="center"><img src="docs/media/search.png" alt="Search" width="85%"></p>

### Ask about your time
*"Where did my week go?"* *"When am I most focused?"* The AI sees a short summary, never your raw history. Chats stay on your Mac.

<p align="center"><img src="docs/media/ask.png" alt="Ask" width="92%"></p>

### Weekly recap by email
Daily, weekly or monthly summaries with charts, changes against the previous period and short insights. Sent through your own Resend account.

<p align="center"><img src="docs/media/email-brief.png" alt="Email summary" width="55%"></p>

### Rules, settings, light and dark
<p align="center">
  <img src="docs/media/settings.png" alt="Settings" width="49%">
  <img src="docs/media/rules.png" alt="Project rules" width="49%">
</p>
<p align="center">
  <img src="docs/media/overview-dark.png" alt="Dark mode overview" width="49%">
  <img src="docs/media/project-detail-dark.png" alt="Dark mode project" width="49%">
</p>

---

## 🌟 Why this one

- **Private:** one file on your Mac and a dashboard that only runs locally. Nothing is uploaded unless you turn on email or cloud AI.
- **Lightweight:** about 15 MB of memory and near-zero CPU. No fan noise, no battery drain.
- **Automatic:** no timers to start. Rules and AI suggestions keep projects organised.
- **Accurate:** away time isn't counted, but watching a lecture or video is.
- **Yours:** a plain SQLite file and a local API for your own reports.

| | **Local Time Tracker** | Rize | RescueTime | ActivityWatch | Toggl |
|---|:---:|:---:|:---:|:---:|:---:|
| Data stays on your Mac | ✅ | ❌ | ❌ | ✅ | ❌ |
| Tracks automatically | ✅ | ✅ | ✅ | ✅ | ❌ |
| ~15 MB footprint | ✅ | ❌ | ❌ | ➖ | ❌ |
| Projects by folder / site rules | ✅ | ➖ | ❌ | ❌ | ❌ |
| AI-suggested projects and rules | ✅ | ➖ | ❌ | ❌ | ❌ |
| AI-usage tracking | ✅ | ❌ | ❌ | ❌ | ❌ |
| Free and open source | ✅ | ❌ | ❌ | ✅ | ➖ |

**In daily use for over two months:** 91 days, 100,000+ activity records and ~720 hours tracked across work, studies, side projects and learning. The collector has run 15 days without a restart at ~15 MB.

---

## ⚙️ Settings

Everything is editable in the dashboard, or from the terminal:

<p align="center"><img src="docs/media/config.png" alt="tt config" width="85%"></p>

| Command | Does |
|---|---|
| `tt config` | Show all settings (keys hidden) |
| `tt config email` | Set up Resend email summaries and send a test |
| `tt config ai` | Choose Claude, Gemini or Ollama |
| `tt config tracking` | Change the away threshold |
| `tt config projects` | Add projects and matching rules |
| `tt config set KEY VALUE` | Change one setting |
| `tt config edit` | Open the settings file |

Changes apply immediately.

| Everyday commands | |
|---|---|
| `tt open` | Open the dashboard |
| `tt status` | Check it's running |
| `tt stop` / `tt start` | Pause or resume tracking |
| `tt report --period week` | Email a summary now |
| `tt demo` | Explore with demo data |
| `tt update` / `tt uninstall` | Update or remove (your data is kept) |

---

## 🛠 How it works

```text
background helper  ──►  one file on your Mac  ──►  your dashboard
(what's in front?)      (~2 MB per week)           (projects, charts, insights)
```

1. A tiny background helper notes the app, window or browser tab in front every few seconds.
2. Notes go into a local database in `~/.timetracker`.
3. The dashboard at `http://127.0.0.1:8321` turns them into projects, charts and insights.

Email and AI are optional and only run when you use them.

<details>
<summary><b>For developers</b></summary>

```
bin/tt            the `tt` command
install.sh        installer
timetracker/      the app (Python package)
  collector.py      background helper (launchd agent)
  dashboard.py      local API (FastAPI) + static/ UI
  classify.py       rules → categories, projects, AI usage
  ai_categorize.py  AI suggestions and chat (Claude / Gemini / Ollama)
  report.py         email summaries (Resend)
  onboard.py · config.py · demo.py · db.py
tests/            unit tests
docs/media/       README images
scripts/media/    scripts that regenerate them
```

- Classification runs when data is read, so editing a rule reclassifies all history.
- AI output is schema-constrained JSON and is only applied after you approve it.
- `TT_HOME`, `TT_PORT` and `TT_LABEL` let you run an isolated copy.
- Permissions: Accessibility (window titles) and Automation per browser (tab URLs). No Screen Recording.
- Architecture notes: [`CLAUDE.md`](CLAUDE.md).

```bash
git clone https://github.com/areeb1501/local-time-tracker-macos.git && cd local-time-tracker-macos
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m unittest discover -s tests -t .
TT_HOME=/tmp/tt-dev .venv/bin/python -m timetracker.demo --fresh
TT_HOME=/tmp/tt-dev TT_PORT=8399 .venv/bin/python -m timetracker.dashboard
```
</details>

---

## ❓ FAQ

**Will it slow my Mac down?** No. It uses about 15 MB of memory and near-zero CPU.

**Does anything leave my Mac?** Only if you turn on email summaries or a cloud AI. With Ollama, even the AI runs locally.

**Do I need an AI key?** No. AI adds suggestions and chat; everything else works without it.

**Windows or Linux?** Not yet. It's macOS only.

**Is it safe to run the installer again, or over an existing install?** Yes. Existing data is never replaced; it's backed up to `~/.timetracker/backups/` first. If a tracker is already running, the new copy installs but won't start a second one. If a different `tt` command already exists, it installs as `ltt` instead.

**How do I remove it?** Run `tt uninstall`. Your data stays in `~/.timetracker` until you delete it.

---

<p align="center">MIT licensed · by <a href="https://github.com/areeb1501">Areeb Pasha</a></p>
