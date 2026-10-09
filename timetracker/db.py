"""Shared SQLite schema and connection helpers for the time tracker."""
import os
import sqlite3

# TT_HOME lets you run a second, isolated copy (demo data, tests) side by side.
DATA_DIR = os.path.expanduser(os.environ.get("TT_HOME", "~/.timetracker"))
DB_PATH = os.path.join(DATA_DIR, "events.db")
CONFIG_PATH = os.path.join(DATA_DIR, "config.json")   # shared: dashboard + collector

# Rules DDL lives in one place: it is interpolated into SCHEMA and reused by
# the rebuild migration in init_db() (CHECK constraints can't be altered).
RULES_DDL = """CREATE TABLE {table} (
    id         INTEGER PRIMARY KEY,
    kind       TEXT NOT NULL CHECK (kind IN ('category', 'project', 'ai')),
    category   TEXT,
    project_id INTEGER REFERENCES projects(id) ON DELETE CASCADE,
    field      TEXT NOT NULL CHECK (field IN ('app', 'bundle', 'title', 'url', 'domain', 'path')),
    match_type TEXT NOT NULL CHECK (match_type IN ('contains', 'prefix', 'equals', 'regex')),
    pattern    TEXT NOT NULL,
    priority   INTEGER NOT NULL DEFAULT 100,
    enabled    INTEGER NOT NULL DEFAULT 1
)"""

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS events (
    id           INTEGER PRIMARY KEY,
    start_ts     REAL NOT NULL,
    end_ts       REAL NOT NULL,
    app          TEXT,
    bundle_id    TEXT,
    window_title TEXT,
    url          TEXT,
    domain       TEXT,
    path         TEXT,        -- focused document / terminal cwd (POSIX), if known
    is_afk       INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_events_start ON events(start_ts);
CREATE INDEX IF NOT EXISTS idx_events_end   ON events(end_ts);

CREATE TABLE IF NOT EXISTS projects (
    id       INTEGER PRIMARY KEY,
    name     TEXT NOT NULL UNIQUE,
    kind     TEXT NOT NULL DEFAULT 'project',   -- 'project' | 'subject'
    color    TEXT,
    archived INTEGER NOT NULL DEFAULT 0,
    pinned   INTEGER NOT NULL DEFAULT 0   -- pinned projects sort first everywhere
);

-- Classification rules, editable from the dashboard.
-- kind='category' sets `category`; kind='project' sets `project_id`;
-- kind='ai' marks matching time as AI-assisted (needs neither).
{RULES_DDL.format(table="IF NOT EXISTS rules")};

-- Manual time->project assignments; override rule-based project for overlap.
CREATE TABLE IF NOT EXISTS assignments (
    id         INTEGER PRIMARY KEY,
    start_ts   REAL NOT NULL,
    end_ts     REAL NOT NULL,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    note       TEXT
);

-- Manually logged time (work done off this machine, e.g. another PC).
-- Counted into project totals; never touches the events table.
CREATE TABLE IF NOT EXISTS manual_entries (
    id         INTEGER PRIMARY KEY,
    start_ts   REAL NOT NULL,
    end_ts     REAL NOT NULL,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    note       TEXT
);

CREATE TABLE IF NOT EXISTS sessions (
    id              INTEGER PRIMARY KEY,
    project_id      INTEGER REFERENCES projects(id) ON DELETE SET NULL,
    label           TEXT,
    planned_minutes INTEGER,
    start_ts        REAL NOT NULL,
    end_ts          REAL,
    note            TEXT
);

-- The single live "track this now" timer. At most one row (id is pinned to 1);
-- stopping it writes an assignment for [start_ts, now] so the tracked window's
-- real activity is credited to the task, then this row is cleared.
CREATE TABLE IF NOT EXISTS active_timer (
    id         INTEGER PRIMARY KEY CHECK (id = 1),
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    start_ts   REAL NOT NULL,
    note       TEXT
);

-- "Ask" tab: locally saved chats with the AI analyst about usage data.
CREATE TABLE IF NOT EXISTS chat_threads (
    id         INTEGER PRIMARY KEY,
    title      TEXT NOT NULL,
    created_ts REAL NOT NULL,
    updated_ts REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS chat_messages (
    id         INTEGER PRIMARY KEY,
    thread_id  INTEGER NOT NULL REFERENCES chat_threads(id) ON DELETE CASCADE,
    role       TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    content    TEXT NOT NULL,
    created_ts REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chat_messages_thread ON chat_messages(thread_id);

-- User said "not a project" on a long unassigned block. Hides the Overview
-- reminder for overlapping visits with the same domain/app. Does not write
-- a project onto events.
CREATE TABLE IF NOT EXISTS review_skips (
    id         INTEGER PRIMARY KEY,
    start_ts   REAL NOT NULL,
    end_ts     REAL NOT NULL,
    visit_key  TEXT NOT NULL,
    created_ts REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_review_skips_range ON review_skips(start_ts, end_ts);
"""

# (category, field, match_type, pattern) applied only when rules table is empty
SEED_CATEGORY_RULES = [
    ("Coding",        "app",    "regex",    r"Cursor|Visual Studio Code|Code|DataGrip|Ghostty|Terminal|iTerm|Antigravity|Xcode|Devin|Copilot|OpenWork"),
    ("Coding",        "domain", "regex",    r"github\.com|stackoverflow\.com|gitlab\.com|vercel\.com|supabase\.com|localhost"),
    ("Databases",     "app",    "regex",    r"DB Browser|MongoDB Compass|MySQLWorkbench|TablePlus|pgAdmin"),
    ("AI Tools",      "app",    "regex",    r"ChatGPT|Claude|Ollama|MacWhisper|\bGrok\b"),
    ("AI Tools",      "domain", "regex",    r"chatgpt\.com|chat\.openai\.com|claude\.ai|perplexity\.ai|gemini\.google\.com|(^|\.)grok\.com|(^|\.)x\.ai"),
    ("Communication", "app",    "regex",    r"Slack|Mail|Messages|WhatsApp|Telegram|Discord|Zoom|Microsoft Teams"),
    ("Communication", "domain", "regex",    r"mail\.google\.com|outlook\.|web\.whatsapp\.com|meet\.google\.com|zoom\.us|slack\.com"),
    ("Docs & Writing","app",    "regex",    r"Obsidian|Notion|Microsoft Word|Pages|Google Docs|Numi"),
    ("Docs & Writing","domain", "regex",    r"docs\.google\.com|notion\.so"),
    ("Spreadsheets",  "app",    "regex",    r"Microsoft Excel|Numbers|Google Sheets"),
    ("Spreadsheets",  "domain", "regex",    r"sheets\.google\.com"),
    ("Reading & Study","app",   "regex",    r"Preview|Books|Acrobat|Kindle|Al Quran"),
    ("Reading & Study","domain","regex",    r"coursera\.org|udemy\.com|edx\.org|scholar\.google\.com|arxiv\.org|canvas\.|blackboard\."),
    ("Design",        "app",    "regex",    r"Figma|CleanShot|CompressX"),
    ("Design",        "domain", "regex",    r"figma\.com|dribbble\.com"),
    ("Entertainment", "domain", "regex",    r"youtube\.com|netflix\.com|twitter\.com|(^|\.)x\.com|instagram\.com|reddit\.com|facebook\.com|tiktok\.com|twitch\.tv"),
    ("Finance & Admin","domain","regex",    r"bank|paypal\.com|wise\.com|stripe\.com|upwork\.com|fiverr\.com"),
    ("Cloud",         "domain", "regex",    r"aws\.amazon\.com|portal\.azure\.com|azure\.microsoft\.com|console\.cloud\.google\.com|cloud\.google\.com|digitalocean\.com|hetzner\.(com|cloud)|vultr\.com|cloudflare\.com|linode\.com|render\.com|fly\.io|railway\.(app|com)|modal\.com|netlify\.(com|app)"),
    ("Cloud",         "app",    "regex",    r"Docker Desktop|Docker|OrbStack|Lens|Termius|Cyberduck|Transmit|Cloudflare WARP"),
    ("Cloud Learning","domain", "regex",    r"cloudskillsboost\.google|qwiklabs\.com|learn\.microsoft\.com|skillbuilder\.aws|acloud\.guru|cloudacademy\.com|developer\.hashicorp\.com|learn\.hashicorp\.com|kubernetes\.io|docs\.aws\.amazon\.com|learn\.cantrill\.io"),
]


def connect(readonly: bool = False) -> sqlite3.Connection:
    os.makedirs(DATA_DIR, exist_ok=True)
    if readonly and os.path.exists(DB_PATH):
        conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    else:
        conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    # Keep the -wal file from outgrowing the database between checkpoints.
    conn.execute("PRAGMA journal_size_limit=4194304")
    return conn


def init_db() -> sqlite3.Connection:
    conn = connect()
    conn.executescript(SCHEMA)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(projects)")]
    if "parent_id" not in cols:
        conn.execute("ALTER TABLE projects ADD COLUMN parent_id INTEGER REFERENCES projects(id)")
    if "archived" not in cols:
        conn.execute("ALTER TABLE projects ADD COLUMN archived INTEGER NOT NULL DEFAULT 0")
    if "pinned" not in cols:
        conn.execute("ALTER TABLE projects ADD COLUMN pinned INTEGER NOT NULL DEFAULT 0")
    if "path" not in [r[1] for r in conn.execute("PRAGMA table_info(events)")]:
        conn.execute("ALTER TABLE events ADD COLUMN path TEXT")
    # Migrations: allow 'bundle' and 'path' as rule fields, and 'ai' as a rule
    # kind ("count this as AI usage"). CHECK constraints can't be altered in
    # place, so rebuild the rules table once per migration.
    rules_sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='rules'").fetchone()[0]
    if any(t not in rules_sql for t in ("'bundle'", "'ai'", "'path'")):
        conn.executescript(f"""
            BEGIN;
            {RULES_DDL.format(table="rules_new")};
            INSERT INTO rules_new SELECT * FROM rules;
            DROP TABLE rules;
            ALTER TABLE rules_new RENAME TO rules;
            COMMIT;
        """)
    # Seed defaults first: the Codex rule below would otherwise make a fresh
    # database look non-empty and skip the whole default rule set.
    if conn.execute("SELECT COUNT(*) FROM rules").fetchone()[0] == 0:
        conn.executemany(
            "INSERT INTO rules (kind, category, field, match_type, pattern) "
            "VALUES ('category', ?, ?, ?, ?)",
            SEED_CATEGORY_RULES,
        )
    # The Codex desktop app is named "ChatGPT" since July 2026 but keeps bundle
    # id com.openai.codex — classify it as Coding before the ChatGPT app rule.
    if conn.execute("SELECT COUNT(*) FROM rules WHERE field='bundle' AND pattern LIKE '%codex%'").fetchone()[0] == 0:
        conn.execute(
            "INSERT INTO rules (kind, category, field, match_type, pattern, priority) "
            "VALUES ('category', 'Coding', 'bundle', 'contains', 'com.openai.codex', 90)")
    # Cloud + Cloud Learning categories (added 2026-07). Seed into existing DBs
    # too; each is idempotent on its category name so re-runs are no-ops.
    for _cat in ("Cloud", "Cloud Learning"):
        if conn.execute("SELECT COUNT(*) FROM rules WHERE category=?", (_cat,)).fetchone()[0] == 0:
            conn.executemany(
                "INSERT INTO rules (kind, category, field, match_type, pattern) "
                "VALUES ('category', ?, ?, ?, ?)",
                [r for r in SEED_CATEGORY_RULES if r[0] == _cat],
            )
    conn.commit()
    return conn
