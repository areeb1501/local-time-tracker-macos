"""Fill a database with ~5 weeks of FICTIONAL activity, for demos and screenshots.

    TT_HOME=/tmp/tt-demo .venv/bin/python -m timetracker.demo --fresh
    (or simply:  tt demo)

Refuses to touch a database that already has events unless --fresh is given,
and --fresh refuses to run against the default ~/.timetracker.
"""
import argparse
import datetime as dt
import os
import random
import sys
import time

from . import db

HOME = "/Users/alex"
rng = random.Random(7)

# (weight, app, bundle, title, url, path) — url's domain is derived.
CHROME = ("Google Chrome", "com.google.Chrome")
ACT = {
    "acme_code": [
        (5, "Cursor", "com.todesktop.cursor", "CheckoutForm.tsx — acme-storefront", None, f"{HOME}/code/acme-storefront/src/checkout"),
        (4, "Ghostty", "com.mitchellh.ghostty", "✳ Claude Code — acme-storefront", None, f"{HOME}/code/acme-storefront"),
        (2, *CHROME, "Acme Storefront — Checkout", "http://localhost:3000/checkout", None),
        (2, *CHROME, "Fix tax rounding in checkout · Pull Request #212 · acme/storefront", "https://github.com/acme/storefront/pull/212", None),
        (1, "Figma", "com.figma.Desktop", "Acme — Checkout v2", None, None),
    ],
    "acme_admin": [
        (4, "Cursor", "com.todesktop.cursor", "OrdersTable.tsx — acme-storefront", None, f"{HOME}/code/acme-storefront/src/admin"),
        (3, "Ghostty", "com.mitchellh.ghostty", "⠹ Claude Code — admin dashboard", None, f"{HOME}/code/acme-storefront/src/admin"),
        (2, *CHROME, "Acme Admin — Orders", "http://localhost:3000/admin/orders", None),
        (1, "TablePlus", "com.tinyapp.TablePlus", "acme_prod — orders", None, None),
    ],
    "thesis_lit": [
        (3, "Preview", "com.apple.Preview", "Attention-based downscaling of climate models.pdf", None, f"{HOME}/research/thesis/papers"),
        (3, *CHROME, "Neural downscaling of precipitation — arXiv", "https://arxiv.org/abs/2604.01234", None),
        (2, *CHROME, "Google Scholar", "https://scholar.google.com/scholar?q=climate+downscaling", None),
        (1, "Obsidian", "md.obsidian", "Lit notes — thesis", None, f"{HOME}/research/thesis/notes"),
    ],
    "thesis_exp": [
        (4, "Visual Studio Code", "com.microsoft.VSCode", "train_unet.py — thesis", None, f"{HOME}/research/thesis/experiments"),
        (3, *CHROME, "experiments.ipynb - JupyterLab", "http://localhost:8888/lab/tree/experiments.ipynb", None),
        (2, "Ghostty", "com.mitchellh.ghostty", "python train_unet.py --epochs 40", None, f"{HOME}/research/thesis/experiments"),
        (1, *CHROME, "Weights & Biases — thesis-runs", "https://wandb.ai/alex/thesis-runs", None),
    ],
    "thesis_write": [
        (5, *CHROME, "Thesis — Chapter 3: Results — Overleaf", "https://www.overleaf.com/project/thesis", None),
        (1, *CHROME, "Improving academic phrasing - Claude", "https://claude.ai/chat/7e1c", None),
    ],
    "pixelkit": [
        (4, "Visual Studio Code", "com.microsoft.VSCode", "renderer.rs — pixelkit", None, f"{HOME}/code/pixelkit/src"),
        (3, "Ghostty", "com.mitchellh.ghostty", "✳ Claude Code — pixelkit", None, f"{HOME}/code/pixelkit"),
        (2, *CHROME, "Issues · pixelkit/pixelkit", "https://github.com/pixelkit/pixelkit/issues", None),
        (1, *CHROME, "pixelkit — crates.io", "https://crates.io/crates/pixelkit", None),
    ],
    "jobs": [
        (3, *CHROME, "Senior Frontend Engineer | Northwind | LinkedIn", "https://www.linkedin.com/jobs/view/4012", None),
        (2, *CHROME, "Cover letter — Northwind - Google Docs", "https://docs.google.com/document/d/abc", None),
        (1, *CHROME, "Application — Globex — Greenhouse", "https://boards.greenhouse.io/globex/jobs/77", None),
    ],
    "comms": [
        (4, "Slack", "com.tinyspeck.slackmacgap", "acme-dev (Channel) - Acme - Slack", None, None),
        (3, *CHROME, "Inbox (3) - alex@example.com - Gmail", "https://mail.google.com/mail/u/0/#inbox", None),
        (2, *CHROME, "Meet - Acme weekly sync - Google Chrome", "https://meet.google.com/abc-defg-hij", None),
        (1, "Messages", "com.apple.MobileSMS", "Messages", None, None),
    ],
    "ai_chat": [
        (3, *CHROME, "Debounce vs throttle for search input - Claude", "https://claude.ai/chat/91ab", None),
        (2, "ChatGPT", "com.openai.chat", "ChatGPT", None, None),
        (1, *CHROME, "Perplexity", "https://www.perplexity.ai/search/rust-simd", None),
    ],
    "learn": [
        (3, *CHROME, "Kubernetes Deployments | Kubernetes", "https://kubernetes.io/docs/concepts/workloads/controllers/deployment/", None),
        (2, *CHROME, "Rust async book", "https://rust-lang.github.io/async-book/", None),
    ],
    "fun": [
        (4, *CHROME, "Lo-fi beats to code to - YouTube", "https://www.youtube.com/watch?v=jfKfPfyJRdk", None),
        (3, *CHROME, "r/programming", "https://www.reddit.com/r/programming/", None),
        (2, *CHROME, "Home / X", "https://x.com/home", None),
    ],
}

# Weekday / weekend mix of activities (weights).
WEEKDAY = {"acme_code": 22, "acme_admin": 10, "thesis_lit": 7, "thesis_exp": 9, "thesis_write": 6,
           "pixelkit": 6, "jobs": 3, "comms": 12, "ai_chat": 6, "learn": 3, "fun": 6}
WEEKEND = {"pixelkit": 18, "thesis_write": 8, "thesis_exp": 6, "learn": 6, "ai_chat": 3, "fun": 12, "jobs": 4}


def pick(weights):
    keys = list(weights)
    return rng.choices(keys, [weights[k] for k in keys])[0]


def domain(url):
    if not url:
        return None
    from urllib.parse import urlparse
    h = urlparse(url).hostname or ""
    return h[4:] if h.startswith("www.") else h


def gen_day(day, now, events):
    weekend = day.weekday() >= 5
    if weekend and rng.random() < 0.35:
        return
    mix = WEEKEND if weekend else WEEKDAY
    t = dt.datetime.combine(day, dt.time(9 if not weekend else 11, rng.randint(0, 40))).timestamp()
    end = dt.datetime.combine(day, dt.time(18 if not weekend else 15, rng.randint(0, 59))).timestamp()
    if rng.random() < 0.3:
        end += 3600 * rng.uniform(1.5, 3)        # some evenings
    lunch = dt.datetime.combine(day, dt.time(13, 0)).timestamp()
    lunched = False
    while t < min(end, now - 60):
        if not lunched and t > lunch:
            span = rng.uniform(30, 55) * 60
            events.append((t, t + span, "AFK", "", "", None, None, None, 1))
            t += span
            lunched = True
            continue
        act = pick(mix)
        block = rng.uniform(4, 45 if act.startswith(("acme", "thesis", "pixel")) else 15) * 60
        stop = min(t + block, end, now - 60)
        while t < stop:
            _, app, bundle, title, url, path = rng.choices(ACT[act], [a[0] for a in ACT[act]])[0]
            if act in ("acme_code", "acme_admin", "pixelkit") and rng.random() < 0.08:
                # a brief detour mid-task — fragmentation is part of the story
                _, app, bundle, title, url, path = rng.choice(ACT["comms"] + ACT["fun"])
            dur = min(rng.uniform(20, 300), stop - t)
            events.append((t, t + dur, app, bundle, title, url, domain(url), path, 0))
            t += dur
        if rng.random() < 0.06:
            span = rng.uniform(6, 20) * 60
            events.append((t, t + span, "AFK", "", "", None, None, None, 1))
            t += span


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fresh", action="store_true", help="delete the database in TT_HOME first")
    ap.add_argument("--days", type=int, default=35)
    a = ap.parse_args()
    if a.fresh:
        if os.path.realpath(db.DATA_DIR) == os.path.realpath(os.path.expanduser("~/.timetracker")):
            sys.exit("refusing to wipe your real ~/.timetracker — set TT_HOME to a scratch dir")
        for suf in ("", "-wal", "-shm"):
            if os.path.exists(db.DB_PATH + suf):
                os.remove(db.DB_PATH + suf)
    conn = db.init_db()
    if conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]:
        sys.exit(f"{db.DB_PATH} already has events — use --fresh with a scratch TT_HOME")

    def project(name, kind="project", parent=None, color=None, pinned=0, archived=0):
        return conn.execute("INSERT INTO projects (name, kind, color, parent_id, pinned, archived) VALUES (?,?,?,?,?,?)",
                            (name, kind, color, parent, pinned, archived)).lastrowid

    def rule(pid, field, pattern, prio=50, match="contains"):
        conn.execute("INSERT INTO rules (kind, project_id, field, match_type, pattern, priority) VALUES ('project',?,?,?,?,?)",
                     (pid, field, match, pattern, prio))

    acme = project("Acme Storefront", color="#6366f1", pinned=1)
    checkout = project("Checkout v2", "task", acme)
    admin = project("Admin dashboard", "task", acme)
    thesis = project("MSc Thesis", "subject", color="#10b981")
    lit = project("Literature review", "task", thesis)
    exp = project("Experiments", "task", thesis)
    write = project("Writing", "task", thesis)
    pix = project("pixelkit (open source)", color="#f59e0b")
    jobs = project("Job search", color="#ec4899")
    hack = project("Hackathon 2026", color="#94a3b8", archived=1)

    rule(checkout, "path", "acme-storefront/src/checkout", 40)
    rule(checkout, "title", "checkout", 45)
    rule(admin, "path", "acme-storefront/src/admin", 40)
    rule(admin, "title", "admin", 45)
    rule(acme, "path", "acme-storefront")
    rule(acme, "url", "github.com/acme/")
    rule(acme, "title", "Acme")
    rule(lit, "domain", "arxiv.org", 40)
    rule(lit, "domain", "scholar.google.com", 40)
    rule(lit, "path", "research/thesis/papers", 40)
    rule(lit, "path", "research/thesis/notes", 40)
    rule(exp, "path", "research/thesis/experiments", 40)
    rule(exp, "domain", "wandb.ai", 40)
    rule(exp, "title", "experiments.ipynb", 40)
    rule(write, "domain", "overleaf.com", 40)
    rule(pix, "path", "pixelkit")
    rule(pix, "url", "pixelkit")
    rule(jobs, "domain", "linkedin.com")
    rule(jobs, "domain", "greenhouse.io")
    rule(jobs, "title", "Cover letter")
    rule(hack, "path", "hackweek")

    now = time.time()
    today = dt.date.today()
    events = []
    for i in range(a.days, -1, -1):
        gen_day(today - dt.timedelta(days=i), now, events)
    # an archived hackathon weekend, early in the history
    hd = today - dt.timedelta(days=a.days - 2)
    t = dt.datetime.combine(hd, dt.time(10)).timestamp()
    for _ in range(90):
        app, bundle, title, path = rng.choice([
            ("Cursor", "com.todesktop.cursor", "main.py — hackweek", f"{HOME}/code/hackweek"),
            ("Ghostty", "com.mitchellh.ghostty", "✳ Claude Code — hackweek", f"{HOME}/code/hackweek")])
        d = rng.uniform(60, 300)
        events.append((t, t + d, app, bundle, title, None, None, path, 0))
        t += d
    # keep the stream gap-free in case the hackathon block overlaps a normal day
    events.sort()
    clean, last = [], 0
    for e in events:
        if e[0] >= last:
            clean.append(e)
            last = e[1]
    conn.executemany("INSERT INTO events (start_ts,end_ts,app,bundle_id,window_title,url,domain,path,is_afk)"
                     " VALUES (?,?,?,?,?,?,?,?,?)", clean)

    # offline time, a couple of focus sessions, and a saved Ask-tab chat
    y = dt.datetime.combine(today - dt.timedelta(days=2), dt.time(16)).timestamp()
    conn.execute("INSERT INTO manual_entries (start_ts,end_ts,project_id,note) VALUES (?,?,?,?)",
                 (y, y + 3600, thesis, "Supervisor meeting (in person)"))
    # sessions start where that kind of work actually began, so focus % is realistic
    for d_ago, pid, label, mins, needle in ((1, exp, "Run ablations", 50, "thesis/experiments"),
                                            (0, checkout, "Ship tax fix", 40, "acme-storefront/src/checkout")):
        lo = dt.datetime.combine(today - dt.timedelta(days=d_ago), dt.time(0)).timestamp()
        starts = [e[0] for e in clean if lo <= e[0] < lo + 86400 and e[7] and needle in e[7]]
        if starts:
            s = starts[0]
            conn.execute("INSERT INTO sessions (project_id,label,planned_minutes,start_ts,end_ts) VALUES (?,?,?,?,?)",
                         (pid, label, mins, s, min(s + mins * 60, now - 120)))
    th = conn.execute("INSERT INTO chat_threads (title,created_ts,updated_ts) VALUES (?,?,?)",
                      ("Where did my week go?", now - 7200, now - 7000)).lastrowid
    conn.executemany("INSERT INTO chat_messages (thread_id,role,content,created_ts) VALUES (?,?,?,?)", [
        (th, "user", "Where did my week go, and what should I cut?", now - 7200),
        (th, "assistant",
         "**Acme Storefront** took the biggest share this week — mostly **Checkout v2** in Cursor and "
         "Claude Code. Your thesis got its best hours in the mornings.\n\n"
         "- 🎯 Deep-work blocks were longest before lunch — protect 9–12 for experiments.\n"
         "- 📉 YouTube/Reddit crept into afternoons right after Slack checks.\n"
         "- 🤖 Roughly a third of coding time was AI-assisted (Claude Code + Cursor).", now - 7000),
    ])
    conn.commit()
    tracked = sum(e[1] - e[0] for e in clean if not e[8]) / 3600
    print(f"demo data: {len(clean):,} events · {tracked:,.0f} h · {a.days} days → {db.DB_PATH}")


if __name__ == "__main__":
    main()
