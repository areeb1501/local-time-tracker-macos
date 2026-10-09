#!/usr/bin/env python3
"""Local dashboard for the time tracker. Runs on demand:

    .venv/bin/python -m timetracker.dashboard   # http://127.0.0.1:8321
"""
import datetime
import os
import threading
import time
from collections import defaultdict
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from . import ai_categorize
from . import classify
from . import db

app = FastAPI(title="Time Tracker")
STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

# Cap on sub-projects/tasks directly under any one project.
MAX_CHILDREN = 8

db.init_db().close()   # schema + migrations run once at startup


def conn():
    return db.connect()


# --------------------------------------------------------------------------
# Rule-verdict cache warming. Classifying all history cold is ~1.7M regex
# checks (~3s); classify.shared_cache keeps verdicts across requests, and this
# fills it in the background at startup and after any rule/project edit, so
# pages never pay that cost in the foreground.
# --------------------------------------------------------------------------
_warm_lock = threading.Lock()
_warm_again = threading.Event()


def warm_rule_caches():
    """Classify every distinct event field tuple once. One pass at a time; an
    edit that lands mid-pass schedules exactly one more pass."""
    if not _warm_lock.acquire(blocking=False):
        _warm_again.set()
        return
    try:
        while True:
            _warm_again.clear()
            c = conn()
            try:
                cat_rules, proj_rules = classify.load_rules(c)
                ai_rules = classify.load_ai_rules(c)
                cc = classify.shared_cache("cat", cat_rules)
                rc = classify.shared_cache("proj", proj_rules)
                tc = classify.shared_cache("ai", ai_rules)
                for row in c.execute(
                        "SELECT DISTINCT app, bundle_id, window_title, url, domain, path, "
                        "0 AS is_afk FROM events WHERE is_afk = 0"):
                    classify.categorize(row, cat_rules, cc)
                    classify.rule_project(row, proj_rules, rc)
                    classify.tool_and_ai(row, ai_rules, tc)
            finally:
                c.close()
            if not _warm_again.is_set():
                break
    finally:
        _warm_lock.release()


def warm_in_background():
    threading.Thread(target=warm_rule_caches, daemon=True).start()


@app.on_event("startup")
def _warm_on_startup():
    warm_in_background()


# --------------------------------------------------------------------------
# Shared aggregation helpers
# --------------------------------------------------------------------------
def top(d, n=None):
    """name→seconds dict to a sorted [{name, seconds}] list, biggest first."""
    out = sorted(({"name": k, "seconds": round(v)} for k, v in d.items()),
                 key=lambda x: -x["seconds"])
    return out[:n] if n else out


def child_map(projs):
    """{parent_id: [child_id, ...]} for a projects dict (id->row), skipping any
    parent_id that dangles outside the set."""
    kids = defaultdict(list)
    for pid, p in projs.items():
        par = p.get("parent_id")
        if par in projs:
            kids[par].append(pid)
    return kids


def subtree_ids(kids, root):
    """All ids in the subtree rooted at `root` (root included), any depth."""
    out, stack = [], [root]
    while stack:
        n = stack.pop()
        out.append(n)
        stack.extend(kids.get(n, []))
    return out


def clipped_events(c, start, end):
    """Non-AFK events overlapping [start, end], yielded as (row, s, e) with
    s/e clipped to the range. This is the app-wide clipping contract."""
    for row in c.execute(
            "SELECT * FROM events WHERE end_ts > ? AND start_ts < ? AND is_afk = 0 "
            "ORDER BY start_ts", (start, end)):
        s, e = max(row["start_ts"], start), min(row["end_ts"], end)
        if e > s:
            yield row, s, e


def day_start():
    """Local midnight today as a unix timestamp."""
    return datetime.datetime.now().replace(
        hour=0, minute=0, second=0, microsecond=0).timestamp()


def report_windows():
    today0 = day_start()
    return {"today": today0, "week": today0 - 6 * 86400,
            "month": today0 - 29 * 86400, "all": 0}


DEFAULT_DISTRACTION_CATEGORIES = ["Entertainment"]


def distraction_categories():
    """Categories whose time counts as a distraction on the Overview tab.
    Configurable in config.json (`distraction_categories`); like every other
    classification knob it's applied at query time, so edits are retroactive."""
    cats = ai_categorize.load_config().get("distraction_categories")
    return set(cats or DEFAULT_DISTRACTION_CATEGORIES)


def day_series(days, today0):
    """(date-key, label) for each of the last `days` local days, oldest first."""
    for i in range(days):
        d = datetime.datetime.fromtimestamp(today0 - (days - 1 - i) * 86400)
        yield d.strftime("%Y-%m-%d"), d.strftime("%b %-d")


# --------------------------------------------------------------------------
# Summary / aggregation
# --------------------------------------------------------------------------
@app.get("/api/summary")
def summary(start: float, end: float):
    c = conn()
    cat_rules, proj_rules = classify.load_rules(c)
    assignments = classify.load_assignments(c, start, end)
    rows = c.execute(
        "SELECT * FROM events WHERE end_ts > ? AND start_ts < ? ORDER BY start_ts",
        (start, end),
    ).fetchall()

    by_app, by_cat, by_domain = defaultdict(float), defaultdict(float), defaultdict(float)
    by_proj = defaultdict(float)
    uncategorized = defaultdict(float)
    timeline = []
    afk_total = active_total = 0.0

    sweep = classify.AssignmentSweep(assignments)
    ccache, rcache = classify.shared_cache("cat", cat_rules), classify.shared_cache("proj", proj_rules)
    for row in rows:
        s, e = max(row["start_ts"], start), min(row["end_ts"], end)
        dur = e - s
        if dur <= 0:
            continue
        cat = classify.categorize(row, cat_rules, ccache)
        spans = classify.project_spans(row, proj_rules, sweep.overlapping(s, e),
                                       start=s, end=e, rule_cache=rcache)
        if row["is_afk"]:
            afk_total += dur
        else:
            active_total += dur
            by_app[row["app"] or "?"] += dur
            by_cat[cat] += dur
            if row["domain"]:
                by_domain[row["domain"]] += dur
            if cat == "Uncategorized":
                uncategorized[row["app"] or "?"] += dur
            for secs, pid, pname in spans:
                if pid is not None:
                    by_proj[pid] += secs
        if end - start <= 2 * 86400:
            proj_names = [p for _, _, p in spans if p]
            timeline.append({
                "start": s, "end": e, "app": row["app"],
                "title": row["window_title"], "url": row["url"],
                "category": cat, "afk": bool(row["is_afk"]),
                "project": proj_names[0] if proj_names else None,
            })

    # Manually logged time (other machines) counts toward project totals.
    for m in c.execute("SELECT * FROM manual_entries WHERE end_ts > ? AND start_ts < ?",
                       (start, end)):
        dur = min(m["end_ts"], end) - max(m["start_ts"], start)
        if dur > 0:
            by_proj[m["project_id"]] += dur

    # Roll sub-projects (courses, tasks, …) up into their parents, any depth:
    # a top-level project's total includes every descendant's time; each direct
    # child shown carries its own whole-subtree total.
    projs = {r["id"]: dict(r) for r in c.execute("SELECT * FROM projects")}
    kids = child_map(projs)

    def subtree_secs(pid):
        return sum(by_proj.get(n, 0) for n in subtree_ids(kids, pid))

    proj_tree = []
    for pid, p in projs.items():
        if p.get("parent_id") in projs:
            continue
        total = subtree_secs(pid)
        if total <= 0:
            continue
        children = [{"name": projs[k]["name"], "seconds": round(subtree_secs(k))}
                    for k in kids.get(pid, [])]
        children = [k for k in children if k["seconds"] > 0]
        proj_tree.append({"name": p["name"], "seconds": round(total),
                          "pinned": bool(p.get("pinned")),
                          "children": sorted(children, key=lambda x: -x["seconds"])})
    proj_tree.sort(key=lambda x: (not x["pinned"], -x["seconds"]))

    return {
        "active_seconds": round(active_total),
        "afk_seconds": round(afk_total),
        "by_app": top(by_app, 30),
        "by_category": top(by_cat, 30),
        "by_project": proj_tree,
        "by_domain": top(by_domain, 20),
        "uncategorized_top": top(uncategorized, 10),
        "timeline": timeline,
    }


@app.get("/api/overview")
def overview(start: float, end: float, trend_days: int = 14):
    """Everything the Overview tab needs in one scan: summary, coding tools,
    AI usage (incl. daily trend), and focus metrics. The standalone endpoints
    each rescan and reclassify the same range; when the UI called four of them
    concurrently the GIL serialized them, so the page paid the sum. One pass,
    one request. Outputs match the standalone endpoints exactly.
    Direct JSONResponse skips jsonable_encoder on this large payload."""
    return JSONResponse(_overview_data(start, end, trend_days))


def _overview_data(start: float, end: float, trend_days: int = 14):
    c = conn()
    cat_rules, proj_rules = classify.load_rules(c)
    ai_rules = classify.load_ai_rules(c)
    dcats = distraction_categories()
    assignments = classify.load_assignments(c, start, end)
    today0 = day_start()
    now = time.time()
    trend_start = today0 - (trend_days - 1) * 86400 if trend_days else start
    scan_lo = min(start, trend_start)
    scan_hi = max(end, now) if trend_days else end
    want_timeline = end - start <= 2 * 86400

    by_app, by_cat, by_domain = defaultdict(float), defaultdict(float), defaultdict(float)
    by_proj, uncategorized = defaultdict(float), defaultdict(float)
    timeline = []
    afk_total = active_total = 0.0
    tool_totals, tool_samples = defaultdict(float), defaultdict(set)
    ai_secs = coding = coding_ai = 0.0
    ai_by_tool = defaultdict(float)
    distraction_secs = 0.0
    by_distraction = defaultdict(float)
    day_ai, day_active = defaultdict(float), defaultdict(float)
    day_distraction = defaultdict(float)
    switches, prev_key = 0, None
    focus_rows = []

    sweep = classify.AssignmentSweep(assignments)
    ccache, rcache, tcache = classify.shared_cache("cat", cat_rules), classify.shared_cache("proj", proj_rules), classify.shared_cache("ai", ai_rules)
    event_unassigned = {}

    rows = c.execute(
        "SELECT * FROM events WHERE end_ts > ? AND start_ts < ? ORDER BY start_ts",
        (scan_lo, scan_hi)).fetchall()

    for row in rows:
        row_ai = None
        cat = None
        s, e = max(row["start_ts"], start), min(row["end_ts"], end)
        if e > s:
            dur = e - s
            cat = classify.categorize(row, cat_rules, ccache)
            spans = classify.project_spans(row, proj_rules, sweep.overlapping(s, e),
                                           start=s, end=e, rule_cache=rcache)
            if row["is_afk"]:
                afk_total += dur
            else:
                active_total += dur
                by_app[row["app"] or "?"] += dur
                by_cat[cat] += dur
                if row["domain"]:
                    by_domain[row["domain"]] += dur
                if cat == "Uncategorized":
                    uncategorized[row["app"] or "?"] += dur
                if cat in dcats:
                    distraction_secs += dur
                    by_distraction[row["domain"] or row["app"] or "?"] += dur
                for secs, pid, _ in spans:
                    if pid is not None:
                        by_proj[pid] += secs
                event_unassigned[row["id"]] = sum(
                    secs for secs, pid, _ in spans if pid is None)
                focus_rows.append(row)
                key = row["domain"] or row["app"] or "?"
                if prev_key is not None and key != prev_key:
                    switches += 1
                prev_key = key
                tool, row_ai = classify.tool_and_ai(row, ai_rules, tcache)
                if tool:
                    coding += dur
                    tool_totals[tool] += dur
                    t = row["window_title"]
                    if t and len(tool_samples[tool]) < 4:
                        tool_samples[tool].add(t)
                if row_ai:
                    ai_secs += dur
                    ai_by_tool[classify.ai_label(row, tool)] += dur
                    if tool:
                        coding_ai += dur
            if want_timeline:
                proj_names = [p for _, _, p in spans if p]
                timeline.append({
                    "start": s, "end": e, "app": row["app"],
                    "title": row["window_title"], "url": row["url"],
                    "category": cat, "afk": bool(row["is_afk"]),
                    "project": proj_names[0] if proj_names else None,
                })
        if trend_days and not row["is_afk"]:
            lo, hi = max(row["start_ts"], trend_start), min(row["end_ts"], now)
            if hi > lo:
                if row_ai is None:
                    row_ai = classify.tool_and_ai(row, ai_rules, tcache)[1]
                if cat is None:
                    cat = classify.categorize(row, cat_rules, ccache)
                day = datetime.datetime.fromtimestamp(lo).strftime("%Y-%m-%d")
                day_active[day] += hi - lo
                if row_ai:
                    day_ai[day] += hi - lo
                if cat in dcats:
                    day_distraction[day] += hi - lo

    # project roll-up + manual entries, same as summary()
    for m in c.execute("SELECT * FROM manual_entries WHERE end_ts > ? AND start_ts < ?",
                       (start, end)):
        dur = min(m["end_ts"], end) - max(m["start_ts"], start)
        if dur > 0:
            by_proj[m["project_id"]] += dur
    projs = {r["id"]: dict(r) for r in c.execute("SELECT * FROM projects")}
    kids = child_map(projs)

    def subtree_secs(pid):
        return sum(by_proj.get(n, 0) for n in subtree_ids(kids, pid))

    proj_tree = []
    for pid, p in projs.items():
        if p.get("parent_id") in projs:
            continue
        total = subtree_secs(pid)
        if total <= 0:
            continue
        children = [{"name": projs[k]["name"], "seconds": round(subtree_secs(k))}
                    for k in kids.get(pid, [])]
        children = [k for k in children if k["seconds"] > 0]
        proj_tree.append({"name": p["name"], "seconds": round(total),
                          "pinned": bool(p.get("pinned")),
                          "children": sorted(children, key=lambda x: -x["seconds"])})
    proj_tree.sort(key=lambda x: (not x["pinned"], -x["seconds"]))

    visits_ = classify.group_events(focus_rows)
    cfg = ai_categorize.load_config()
    prompt_secs = float(cfg.get("unassigned_prompt_minutes") or 1) * 60
    skips = [dict(r) for r in c.execute(
        "SELECT start_ts, end_ts, visit_key FROM review_skips "
        "WHERE end_ts > ? AND start_ts < ?", (start, end))]
    unassigned_blocks = classify.unassigned_prompt_blocks(
        visits_, event_unassigned, skips, now, min_seconds=prompt_secs)
    deep = [v for v in visits_ if v["active_seconds"] >= 25 * 60]
    deep_seconds = sum(v["active_seconds"] for v in deep)
    hours = active_total / 3600
    daily = [{"date": key, "label": label,
              "ai": round(day_ai.get(key, 0)), "active": round(day_active.get(key, 0)),
              "pct": round(100 * day_ai.get(key, 0) / day_active[key])
                     if day_active.get(key) else 0}
             for key, label in day_series(trend_days, today0)]
    d_daily = [{"date": key, "label": label,
                "seconds": round(day_distraction.get(key, 0)),
                "active": round(day_active.get(key, 0)),
                "pct": round(100 * day_distraction.get(key, 0) / day_active[key])
                       if day_active.get(key) else 0}
               for key, label in day_series(trend_days, today0)]
    tools = sorted(({"name": k, "seconds": round(v), "samples": sorted(tool_samples[k])}
                    for k, v in tool_totals.items()), key=lambda x: -x["seconds"])

    return {
        "summary": {
            "active_seconds": round(active_total), "afk_seconds": round(afk_total),
            "by_app": top(by_app, 30), "by_category": top(by_cat, 30),
            "by_project": proj_tree, "by_domain": top(by_domain, 20),
            "uncategorized_top": top(uncategorized, 10), "timeline": timeline,
        },
        "coding_tools": {"tools": tools,
                         "total_seconds": round(sum(tool_totals.values()))},
        "ai_usage": {
            "active_seconds": round(active_total), "ai_seconds": round(ai_secs),
            "pct": round(100 * ai_secs / active_total) if active_total else 0,
            "coding_seconds": round(coding),
            "coding_ai_pct": round(100 * coding_ai / coding) if coding else 0,
            "by_tool": top(ai_by_tool), "daily": daily,
        },
        "distractions": {
            "active_seconds": round(active_total),
            "seconds": round(distraction_secs),
            "pct": round(100 * distraction_secs / active_total) if active_total else 0,
            "by_item": top(by_distraction, 12),
            "daily": d_daily,
            "categories": sorted(dcats),
        },
        "unassigned_blocks": unassigned_blocks,
        "unassigned_prompt_minutes": round(prompt_secs / 60, 1),
        "focus": {
            "active_seconds": round(active_total),
            "switches": switches,
            "switches_per_hour": round(switches / hours, 1) if hours > 0.1 else None,
            "deep_seconds": round(deep_seconds),
            "deep_blocks": len(deep),
            "longest_streak_seconds": round(max(
                (v["active_seconds"] for v in visits_), default=0)),
            "focus_score": round(100 * deep_seconds / active_total)
                           if active_total >= 1800 else None,
            "deep_threshold_minutes": 25,
        },
    }


@app.get("/api/coding_tools")
def coding_tools(start: float, end: float):
    """Time per coding tool / agent (Claude Code, Codex, Cursor, …) in a range."""
    c = conn()
    totals = defaultdict(float)
    samples = defaultdict(set)
    for row, s, e in clipped_events(c, start, end):
        tool = classify.coding_tool(row)
        if not tool:
            continue
        totals[tool] += e - s
        t = row["window_title"]
        if t and len(samples[tool]) < 4:
            samples[tool].add(t)
    out = [{"name": k, "seconds": round(v), "samples": sorted(samples[k])}
           for k, v in totals.items()]
    out.sort(key=lambda x: -x["seconds"])
    return {"tools": out, "total_seconds": round(sum(totals.values()))}


@app.get("/api/ai_usage")
def ai_usage(start: float, end: float, trend_days: int = 14):
    """AI-assisted time vs overall active time, per tool, with a daily trend.
    trend_days=0 skips the trend (report.py uses this)."""
    c = conn()
    ai_rules = classify.load_ai_rules(c)
    today0 = day_start()
    now = time.time()
    trend_start = today0 - (trend_days - 1) * 86400 if trend_days else start

    active = ai = coding = coding_ai = 0.0
    by_tool = defaultdict(float)
    day_ai, day_active = defaultdict(float), defaultdict(float)
    tcache = classify.shared_cache("ai", ai_rules)
    # one scan covers both the requested range and the trend window
    for row, _, _ in clipped_events(c, min(start, trend_start),
                                    max(end, now) if trend_days else end):
        tool, row_ai = classify.tool_and_ai(row, ai_rules, tcache)
        lo, hi = max(row["start_ts"], start), min(row["end_ts"], end)
        if hi > lo:
            dur = hi - lo
            active += dur
            if tool:
                coding += dur
            if row_ai:
                ai += dur
                by_tool[classify.ai_label(row, tool)] += dur
                if tool:
                    coding_ai += dur
        if trend_days:
            lo, hi = max(row["start_ts"], trend_start), min(row["end_ts"], now)
            if hi > lo:
                day = datetime.datetime.fromtimestamp(lo).strftime("%Y-%m-%d")
                day_active[day] += hi - lo
                if row_ai:
                    day_ai[day] += hi - lo

    daily = [{"date": key, "label": label,
              "ai": round(day_ai.get(key, 0)), "active": round(day_active.get(key, 0)),
              "pct": round(100 * day_ai.get(key, 0) / day_active[key])
                     if day_active.get(key) else 0}
             for key, label in day_series(trend_days, today0)]

    return {
        "active_seconds": round(active), "ai_seconds": round(ai),
        "pct": round(100 * ai / active) if active else 0,
        "coding_seconds": round(coding),
        "coding_ai_pct": round(100 * coding_ai / coding) if coding else 0,
        "by_tool": top(by_tool), "daily": daily,
    }


@app.get("/api/focus")
def focus(start: float, end: float, deep_minutes: int = 25):
    """Fragmentation metrics: context switches, deep-work blocks, longest streak."""
    c = conn()
    rows = []
    active = 0.0
    switches = 0
    prev_key = None
    for row, s, e in clipped_events(c, start, end):
        rows.append(row)
        active += e - s
        key = row["domain"] or row["app"] or "?"
        if prev_key is not None and key != prev_key:
            switches += 1
        prev_key = key
    visits = classify.group_events(rows)
    deep = [v for v in visits if v["active_seconds"] >= deep_minutes * 60]
    deep_seconds = sum(v["active_seconds"] for v in deep)
    longest = max((v["active_seconds"] for v in visits), default=0)
    hours = active / 3600
    return {
        "active_seconds": round(active),
        "switches": switches,
        "switches_per_hour": round(switches / hours, 1) if hours > 0.1 else None,
        "deep_seconds": round(deep_seconds),
        "deep_blocks": len(deep),
        "longest_streak_seconds": round(longest),
        "focus_score": round(100 * deep_seconds / active) if active >= 1800 else None,
        "deep_threshold_minutes": deep_minutes,
    }


@app.get("/api/search")
def search(q: str, limit: int = 5000):
    """Search window titles / URLs / domains / app names across all history.
    Matching events are grouped into visits (same as the Activity tab) so
    thousands of 3s events read as a handful of sessions."""
    c = conn()
    q = q.strip()
    if not q:
        return {"query": q, "count": 0, "total_seconds": 0, "visits": []}
    where = ("is_afk = 0 AND (window_title LIKE ? OR url LIKE ? "
             "OR domain LIKE ? OR app LIKE ?)")
    params = [f"%{q}%"] * 4
    agg = c.execute(
        f"SELECT COUNT(*) n, COALESCE(SUM(end_ts - start_ts), 0) t "
        f"FROM events WHERE {where}", params).fetchone()
    # newest `limit` matches, re-sorted ascending for grouping
    rows = c.execute(
        f"SELECT * FROM events WHERE {where} ORDER BY start_ts DESC LIMIT ?",
        params + [limit]).fetchall()
    rows = list(reversed(rows))
    visits = classify.group_events(rows, gap_tolerance=600)
    visits.reverse()  # newest first
    return {"query": q, "count": agg["n"], "total_seconds": round(agg["t"]),
            "events_grouped": len(rows),
            "visits": [{"start_ts": v["start_ts"], "end_ts": v["end_ts"],
                        "active_seconds": round(v["active_seconds"]),
                        "app": v["app"], "domain": v["domain"], "url": v["url"],
                        "titles": v["titles"], "event_ids": v["event_ids"]}
                       for v in visits]}


@app.get("/api/events")
def events(start: float, end: float, limit: int = 500):
    c = conn()
    cat_rules, proj_rules = classify.load_rules(c)
    assignments = classify.load_assignments(c, start, end)
    rows = c.execute(
        "SELECT * FROM events WHERE end_ts > ? AND start_ts < ? AND is_afk = 0 "
        "ORDER BY start_ts DESC LIMIT ?",
        (start, end, limit),
    ).fetchall()
    out = []
    for row in rows:
        spans = classify.project_spans(row, proj_rules, assignments)
        projects = sorted({p for _, _, p in spans if p})
        out.append({
            "id": row["id"], "start": row["start_ts"], "end": row["end_ts"],
            "seconds": round(row["end_ts"] - row["start_ts"]),
            "app": row["app"], "title": row["window_title"], "url": row["url"],
            "domain": row["domain"],
            "category": classify.categorize(row, cat_rules),
            "projects": projects,
        })
    return out


@app.delete("/api/events/{eid}")
def delete_event(eid: int):
    c = conn()
    c.execute("DELETE FROM events WHERE id=?", (eid,))
    c.commit()
    return {"ok": True}


class EventsDeleteIn(BaseModel):
    ids: list[int]


@app.post("/api/events/delete")
def delete_events(p: EventsDeleteIn):
    c = conn()
    c.executemany("DELETE FROM events WHERE id=?", [(i,) for i in p.ids])
    c.commit()
    return {"deleted": len(p.ids)}


# --------------------------------------------------------------------------
# Projects
# --------------------------------------------------------------------------
class ProjectIn(BaseModel):
    name: str
    kind: str = "project"
    color: Optional[str] = None
    parent_id: Optional[int] = None


@app.get("/api/projects")
def list_projects():
    return [dict(r) for r in conn().execute("SELECT * FROM projects ORDER BY name")]


@app.post("/api/projects")
def create_project(p: ProjectIn):
    c = conn()
    if p.parent_id is not None:
        if not c.execute("SELECT 1 FROM projects WHERE id=?", (p.parent_id,)).fetchone():
            raise HTTPException(404, "no such parent project")
        n = c.execute("SELECT COUNT(*) FROM projects WHERE parent_id=? AND archived=0",
                      (p.parent_id,)).fetchone()[0]
        if n >= MAX_CHILDREN:
            raise HTTPException(422, f"a project can have at most {MAX_CHILDREN} sub-projects")
    try:
        cur = c.execute("INSERT INTO projects (name, kind, color, parent_id) VALUES (?,?,?,?)",
                        (p.name.strip(), p.kind, p.color, p.parent_id))
        c.commit()
    except Exception:
        raise HTTPException(409, "A project with that name already exists")
    warm_in_background()
    return {"id": cur.lastrowid}


class MergeIn(BaseModel):
    source_id: int
    target_id: int


@app.post("/api/projects/merge")
def merge_projects(p: MergeIn):
    """Move all rules, assignments, logged time, sessions and sub-projects
    from source into target, then delete source."""
    if p.source_id == p.target_id:
        raise HTTPException(422, "pick two different projects")
    c = conn()
    projs = {r["id"]: dict(r) for r in c.execute("SELECT * FROM projects")}
    if p.source_id not in projs or p.target_id not in projs:
        raise HTTPException(404, "no such project")
    # If the target is a child of the source, lift it out first so it doesn't
    # end up as its own parent.
    if projs[p.target_id].get("parent_id") == p.source_id:
        c.execute("UPDATE projects SET parent_id=? WHERE id=?",
                  (projs[p.source_id].get("parent_id"), p.target_id))
    moved = {}
    for table in ("rules", "assignments", "manual_entries", "sessions"):
        moved[table] = c.execute(
            f"UPDATE {table} SET project_id=? WHERE project_id=?",
            (p.target_id, p.source_id)).rowcount
    moved["subprojects"] = c.execute(
        "UPDATE projects SET parent_id=? WHERE parent_id=?",
        (p.target_id, p.source_id)).rowcount
    # Drop rules that became exact duplicates after the move.
    c.execute("""DELETE FROM rules WHERE id NOT IN (
        SELECT MIN(id) FROM rules GROUP BY kind, IFNULL(category, ''),
        IFNULL(project_id, -1), field, match_type, pattern)""")
    c.execute("DELETE FROM projects WHERE id=?", (p.source_id,))
    c.commit()
    warm_in_background()
    return {"ok": True, "moved": moved,
            "source": projs[p.source_id]["name"],
            "target": projs[p.target_id]["name"]}


@app.delete("/api/projects/{pid}")
def delete_project(pid: int):
    c = conn()
    c.execute("DELETE FROM projects WHERE id=?", (pid,))
    c.commit()
    warm_in_background()
    return {"ok": True}


def _set_archived(pid: int, flag: int):
    """Archive/unarchive a project. Archiving takes the whole subtree with it;
    unarchiving also clears ancestors so the project is reachable again.
    History, rules and roll-up totals are untouched either way."""
    c = conn()
    projs = {r["id"]: dict(r) for r in c.execute("SELECT id, parent_id FROM projects")}
    if pid not in projs:
        raise HTTPException(404, "no such project")
    ids = subtree_ids(child_map(projs), pid)
    if not flag:
        cur = projs[pid].get("parent_id")
        while cur in projs:
            ids.append(cur)
            cur = projs[cur].get("parent_id")
    c.execute(f"UPDATE projects SET archived=? WHERE id IN ({','.join('?' * len(ids))})",
              [flag] + ids)
    c.commit()
    return {"ok": True, "count": len(ids)}


@app.post("/api/projects/{pid}/archive")
def archive_project(pid: int):
    return _set_archived(pid, 1)


@app.post("/api/projects/{pid}/unarchive")
def unarchive_project(pid: int):
    return _set_archived(pid, 0)


def _set_pinned(pid: int, flag: int):
    """Pinned projects sort to the top of every project list and picker."""
    c = conn()
    if not c.execute("UPDATE projects SET pinned=? WHERE id=?", (flag, pid)).rowcount:
        raise HTTPException(404, "no such project")
    c.commit()
    return {"ok": True}


@app.post("/api/projects/{pid}/pin")
def pin_project(pid: int):
    return _set_pinned(pid, 1)


@app.post("/api/projects/{pid}/unpin")
def unpin_project(pid: int):
    return _set_pinned(pid, 0)


# --------------------------------------------------------------------------
# Manual time entries (work done off this machine)
# --------------------------------------------------------------------------
class ManualEntryIn(BaseModel):
    project_id: int
    start_ts: float
    end_ts: float
    note: Optional[str] = None


@app.get("/api/manual_entries")
def list_manual_entries(project_id: Optional[int] = None):
    c = conn()
    q = ("SELECT m.*, p.name AS project_name FROM manual_entries m "
         "JOIN projects p ON p.id = m.project_id ")
    if project_id is not None:
        projs = {r["id"]: dict(r) for r in c.execute("SELECT id, parent_id FROM projects")}
        ids = subtree_ids(child_map(projs), project_id)   # project + all descendants
        q += f"WHERE m.project_id IN ({','.join('?' * len(ids))}) "
        rows = c.execute(q + "ORDER BY m.start_ts DESC", ids)
    else:
        rows = c.execute(q + "ORDER BY m.start_ts DESC")
    return [dict(r) for r in rows]


@app.post("/api/manual_entries")
def create_manual_entry(m: ManualEntryIn):
    if m.end_ts <= m.start_ts:
        raise HTTPException(422, "end must be after start")
    c = conn()
    if not c.execute("SELECT 1 FROM projects WHERE id=?", (m.project_id,)).fetchone():
        raise HTTPException(404, "no such project")
    cur = c.execute(
        "INSERT INTO manual_entries (start_ts, end_ts, project_id, note) VALUES (?,?,?,?)",
        (m.start_ts, m.end_ts, m.project_id, m.note))
    c.commit()
    return {"id": cur.lastrowid}


@app.delete("/api/manual_entries/{mid}")
def delete_manual_entry(mid: int):
    c = conn()
    c.execute("DELETE FROM manual_entries WHERE id=?", (mid,))
    c.commit()
    return {"ok": True}


# --------------------------------------------------------------------------
# Rules
# --------------------------------------------------------------------------
class RuleIn(BaseModel):
    kind: str                      # 'category' | 'project' | 'ai'
    category: Optional[str] = None
    project_id: Optional[int] = None
    field: str                     # 'app' | 'bundle' | 'title' | 'url' | 'domain' | 'path'
    match_type: str                # 'contains' | 'prefix' | 'equals' | 'regex'
    pattern: str
    priority: int = 100
    enabled: bool = True


@app.get("/api/rules")
def list_rules():
    return [dict(r) for r in conn().execute(
        "SELECT r.*, p.name AS project_name FROM rules r "
        "LEFT JOIN projects p ON p.id = r.project_id ORDER BY r.kind, r.priority, r.id")]


@app.post("/api/rules")
def create_rule(r: RuleIn):
    if r.kind == "project" and not r.project_id:
        raise HTTPException(422, "project rules need project_id")
    if r.kind == "category" and not r.category:
        raise HTTPException(422, "category rules need a category name")
    if r.kind not in ("project", "category", "ai"):
        raise HTTPException(422, "kind must be project, category or ai")
    c = conn()
    cur = c.execute(
        "INSERT INTO rules (kind, category, project_id, field, match_type, pattern, priority, enabled) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (r.kind, r.category, r.project_id, r.field, r.match_type, r.pattern.strip(),
         r.priority, int(r.enabled)))
    c.commit()
    classify._compiled.cache_clear()
    warm_in_background()
    return {"id": cur.lastrowid}


@app.put("/api/rules/{rid}")
def update_rule(rid: int, r: RuleIn):
    if r.kind == "project" and not r.project_id:
        raise HTTPException(422, "project rules need project_id")
    if r.kind == "category" and not r.category:
        raise HTTPException(422, "category rules need a category name")
    if r.kind not in ("project", "category", "ai"):
        raise HTTPException(422, "kind must be project, category or ai")
    c = conn()
    cur = c.execute(
        "UPDATE rules SET kind=?, category=?, project_id=?, field=?, match_type=?, "
        "pattern=?, priority=?, enabled=? WHERE id=?",
        (r.kind, r.category, r.project_id, r.field, r.match_type, r.pattern.strip(),
         r.priority, int(r.enabled), rid))
    c.commit()
    classify._compiled.cache_clear()
    warm_in_background()
    if cur.rowcount == 0:
        raise HTTPException(404, "no such rule")
    return {"ok": True}


@app.delete("/api/rules/{rid}")
def delete_rule(rid: int):
    c = conn()
    c.execute("DELETE FROM rules WHERE id=?", (rid,))
    c.commit()
    classify._compiled.cache_clear()
    warm_in_background()
    return {"ok": True}


# --------------------------------------------------------------------------
# Manual assignments
# --------------------------------------------------------------------------
class AssignmentIn(BaseModel):
    start_ts: float
    end_ts: float
    project_id: int
    note: Optional[str] = None


@app.get("/api/assignments")
def list_assignments(start: float, end: float):
    return classify.load_assignments(conn(), start, end)


@app.post("/api/assignments")
def create_assignment(a: AssignmentIn):
    if a.end_ts <= a.start_ts:
        raise HTTPException(422, "end must be after start")
    c = conn()
    cur = c.execute(
        "INSERT INTO assignments (start_ts, end_ts, project_id, note) VALUES (?,?,?,?)",
        (a.start_ts, a.end_ts, a.project_id, a.note))
    c.commit()
    return {"id": cur.lastrowid}


@app.delete("/api/assignments/{aid}")
def delete_assignment(aid: int):
    c = conn()
    c.execute("DELETE FROM assignments WHERE id=?", (aid,))
    c.commit()
    return {"ok": True}


# --------------------------------------------------------------------------
# Live "track this now" timer. One at a time; stopping it credits the tracked
# window's real activity to the chosen task via an assignment (no double count,
# and the task's page then shows which apps/sites the time was spent in).
# --------------------------------------------------------------------------
class TimerIn(BaseModel):
    project_id: int
    note: Optional[str] = None


@app.get("/api/timer")
def get_timer():
    r = conn().execute(
        "SELECT t.*, p.name AS project_name FROM active_timer t "
        "JOIN projects p ON p.id = t.project_id WHERE t.id = 1").fetchone()
    return dict(r) if r else None


@app.post("/api/timer/start")
def start_timer(t: TimerIn):
    c = conn()
    if c.execute("SELECT 1 FROM active_timer WHERE id = 1").fetchone():
        raise HTTPException(409, "A timer is already running - stop it first")
    if not c.execute("SELECT 1 FROM projects WHERE id=?", (t.project_id,)).fetchone():
        raise HTTPException(404, "no such project")
    c.execute("INSERT INTO active_timer (id, project_id, start_ts, note) VALUES (1,?,?,?)",
              (t.project_id, time.time(), t.note))
    c.commit()
    return {"ok": True}


@app.post("/api/timer/stop")
def stop_timer():
    c = conn()
    row = c.execute("SELECT * FROM active_timer WHERE id = 1").fetchone()
    if not row:
        raise HTTPException(404, "no timer running")
    now = time.time()
    aid = None
    # Only write an assignment if the timer ran long enough to be meaningful.
    if now - row["start_ts"] >= 1:
        aid = c.execute(
            "INSERT INTO assignments (start_ts, end_ts, project_id, note) VALUES (?,?,?,?)",
            (row["start_ts"], now, row["project_id"], row["note"])).lastrowid
    c.execute("DELETE FROM active_timer WHERE id = 1")
    c.commit()
    return {"ok": True, "assignment_id": aid,
            "seconds": round(now - row["start_ts"])}


# --------------------------------------------------------------------------
# Study / focus sessions
# --------------------------------------------------------------------------
class SessionIn(BaseModel):
    project_id: Optional[int] = None
    label: Optional[str] = None
    planned_minutes: Optional[int] = None


def _session_metrics(c, s, rules=None):
    """rules is an optional preloaded (proj_rules, ai_rules) pair so callers
    with many sessions don't reload them per session."""
    start = s["start_ts"]
    end = s["end_ts"] or time.time()
    proj_rules, ai_rules = rules or (classify.load_rules(c)[1],
                                     classify.load_ai_rules(c))
    assignments = classify.load_assignments(c, start, end)
    targets = set()
    if s["project_id"] is not None:
        projs = {r["id"]: dict(r) for r in c.execute("SELECT id, parent_id FROM projects")}
        targets = set(subtree_ids(child_map(projs), s["project_id"]))   # whole subtree, any depth
    rows = c.execute(
        "SELECT * FROM events WHERE end_ts > ? AND start_ts < ? ORDER BY start_ts",
        (start, end)).fetchall()

    focus = afk = other = active = ai = 0.0
    distractions = defaultdict(float)
    sweep = classify.AssignmentSweep(assignments)
    tcache = classify.shared_cache("ai", ai_rules)
    for row in rows:
        lo, hi = max(row["start_ts"], start), min(row["end_ts"], end)
        dur = hi - lo
        if dur <= 0:
            continue
        if row["is_afk"]:
            afk += dur
            continue
        active += dur
        if classify.tool_and_ai(row, ai_rules, tcache)[1]:
            ai += dur
        matched = 0.0
        if targets:
            for secs, pid, _ in classify.project_spans(
                    row, proj_rules, sweep.overlapping(lo, hi), start=lo, end=hi):
                if pid in targets:
                    matched += secs
        focus += matched
        if dur - matched > 1:
            key = row["domain"] or row["app"] or "?"
            distractions[key] += dur - matched
            other += dur - matched

    total = end - start
    return {
        "duration_seconds": round(total),
        "focus_seconds": round(focus),
        "afk_seconds": round(afk),
        "other_seconds": round(other),
        "ai_seconds": round(ai),
        "ai_pct": round(100 * ai / active) if active > 0 else None,
        "focus_pct": round(100 * focus / total) if total > 0 and s["project_id"] else None,
        "top_distractions": top(distractions, 8),
    }


def _session_rules(c):
    return (classify.load_rules(c)[1], classify.load_ai_rules(c))


@app.get("/api/sessions")
def list_sessions(limit: int = 50, since: Optional[float] = None):
    c = conn()
    q = ("SELECT s.*, p.name AS project_name FROM sessions s "
         "LEFT JOIN projects p ON p.id = s.project_id ")
    params = []
    if since is not None:
        q += "WHERE s.start_ts >= ? "
        params.append(since)
    rows = c.execute(q + "ORDER BY s.start_ts DESC LIMIT ?", params + [limit]).fetchall()
    rules = _session_rules(c)
    return [{**dict(r), "metrics": _session_metrics(c, r, rules)} for r in rows]


@app.post("/api/sessions")
def start_session(s: SessionIn):
    c = conn()
    open_row = c.execute("SELECT id FROM sessions WHERE end_ts IS NULL").fetchone()
    if open_row:
        raise HTTPException(409, "A session is already running - stop it first")
    cur = c.execute(
        "INSERT INTO sessions (project_id, label, planned_minutes, start_ts) VALUES (?,?,?,?)",
        (s.project_id, s.label, s.planned_minutes, time.time()))
    c.commit()
    return {"id": cur.lastrowid}


@app.post("/api/sessions/{sid}/stop")
def stop_session(sid: int):
    c = conn()
    c.execute("UPDATE sessions SET end_ts=? WHERE id=? AND end_ts IS NULL", (time.time(), sid))
    c.commit()
    return {"ok": True}


@app.delete("/api/sessions/{sid}")
def delete_session(sid: int):
    c = conn()
    c.execute("DELETE FROM sessions WHERE id=?", (sid,))
    c.commit()
    return {"ok": True}


@app.get("/api/session_detail")
def session_detail(id: int):
    """Everything the session drill-in page needs in one scan: the metrics the
    list row shows, plus breakdowns (category/app/site/project/titles/AI tools),
    focus facts (switches, streaks, deep blocks), a timeline, and the visit
    blocks the session was actually spent in."""
    c = conn()
    s = c.execute(
        "SELECT s.*, p.name AS project_name FROM sessions s "
        "LEFT JOIN projects p ON p.id = s.project_id WHERE s.id = ?",
        (id,)).fetchone()
    if not s:
        raise HTTPException(404, "No such session")
    start = s["start_ts"]
    end = s["end_ts"] or time.time()
    cat_rules, proj_rules = classify.load_rules(c)
    ai_rules = classify.load_ai_rules(c)
    assignments = classify.load_assignments(c, start, end)
    dcats = distraction_categories()
    targets = set()
    if s["project_id"] is not None:
        projs = {r["id"]: dict(r) for r in c.execute("SELECT id, parent_id FROM projects")}
        targets = set(subtree_ids(child_map(projs), s["project_id"]))

    by_cat, by_app, by_domain = defaultdict(float), defaultdict(float), defaultdict(float)
    by_title, by_proj_name, ai_by_tool = defaultdict(float), defaultdict(float), defaultdict(float)
    distractions = defaultdict(float)
    focus = afk = active = ai = other = 0.0
    switches, prev_key = 0, None
    timeline, focus_rows = [], []

    sweep = classify.AssignmentSweep(assignments)
    ccache, rcache, tcache = classify.shared_cache("cat", cat_rules), classify.shared_cache("proj", proj_rules), classify.shared_cache("ai", ai_rules)
    for row in c.execute(
            "SELECT * FROM events WHERE end_ts > ? AND start_ts < ? ORDER BY start_ts",
            (start, end)):
        lo, hi = max(row["start_ts"], start), min(row["end_ts"], end)
        dur = hi - lo
        if dur <= 0:
            continue
        cat = classify.categorize(row, cat_rules, ccache)
        spans = classify.project_spans(row, proj_rules, sweep.overlapping(lo, hi),
                                       start=lo, end=hi, rule_cache=rcache)
        if row["is_afk"]:
            afk += dur
        else:
            active += dur
            focus_rows.append(row)
            by_cat[cat] += dur
            by_app[row["app"] or "?"] += dur
            if row["domain"]:
                by_domain[row["domain"]] += dur
            title = row["window_title"] or row["url"]
            if title:
                by_title[title] += dur
            key = row["domain"] or row["app"] or "?"
            if prev_key is not None and key != prev_key:
                switches += 1
            prev_key = key
            tool, row_ai = classify.tool_and_ai(row, ai_rules, tcache)
            if row_ai:
                ai += dur
                ai_by_tool[classify.ai_label(row, tool)] += dur
            matched = 0.0
            for secs, pid, pname in spans:
                if pname:
                    by_proj_name[pname] += secs
                if pid in targets:
                    matched += secs
            focus += matched
            # "Pulled away" matches _session_metrics when the session has a
            # target project; with no target, fall back to distraction-category
            # time so the page still names what ate the session.
            if targets:
                if dur - matched > 1:
                    distractions[key] += dur - matched
                    other += dur - matched
            elif cat in dcats:
                distractions[key] += dur
                other += dur
        proj_names = [p for _, _, p in spans if p]
        timeline.append({
            "start": lo, "end": hi, "app": row["app"],
            "title": row["window_title"], "url": row["url"],
            "category": cat if not row["is_afk"] else "AFK",
            "afk": bool(row["is_afk"]),
            "project": proj_names[0] if proj_names else None,
        })

    visits_ = classify.group_events(focus_rows)
    deep = [v for v in visits_ if v["active_seconds"] >= 25 * 60]
    total = end - start
    hours = active / 3600
    return JSONResponse({
        "session": {"id": s["id"], "label": s["label"],
                    "project_id": s["project_id"], "project_name": s["project_name"],
                    "planned_minutes": s["planned_minutes"],
                    "start_ts": s["start_ts"], "end_ts": s["end_ts"],
                    "running": s["end_ts"] is None},
        "metrics": {
            "duration_seconds": round(total),
            "active_seconds": round(active),
            "focus_seconds": round(focus),
            "afk_seconds": round(afk),
            "other_seconds": round(other),
            "ai_seconds": round(ai),
            "ai_pct": round(100 * ai / active) if active > 0 else None,
            "focus_pct": round(100 * focus / total) if total > 0 and s["project_id"] else None,
            "top_distractions": top(distractions, 8),
        },
        "focus_facts": {
            "switches": switches,
            "switches_per_hour": round(switches / hours, 1) if hours > 0.1 else None,
            "longest_streak_seconds": round(max(
                (v["active_seconds"] for v in visits_), default=0)),
            "deep_seconds": round(sum(v["active_seconds"] for v in deep)),
            "deep_blocks": len(deep),
            "deep_threshold_minutes": 25,
        },
        "by_category": top(by_cat),
        "by_project": top(by_proj_name),
        "by_app": top(by_app, 12),
        "by_domain": top(by_domain, 12),
        "by_title": top(by_title, 12),
        "ai_by_tool": top(ai_by_tool, 8),
        "timeline": timeline,
        "visits": [{"app": v["app"], "domain": v["domain"],
                    "start_ts": v["start_ts"], "end_ts": v["end_ts"],
                    "active_seconds": round(v["active_seconds"]),
                    "titles": v["titles"]}
                   for v in reversed(visits_)],
    })


# --------------------------------------------------------------------------
# Grouped visits, AI categorizer, project report, email brief, settings
# --------------------------------------------------------------------------
@app.get("/api/visits")
def visits(start: float, end: float):
    c = conn()
    cat_rules, proj_rules = classify.load_rules(c)
    assignments = classify.load_assignments(c, start, end)
    rows = c.execute(
        "SELECT * FROM events WHERE end_ts > ? AND start_ts < ? AND is_afk = 0 ORDER BY start_ts",
        (start, end)).fetchall()
    # Classify each event once in this ascending pass (visits interleave event
    # ids, so the sweep can't run inside the per-visit loop), then aggregate.
    sweep = classify.AssignmentSweep(assignments)
    ccache, rcache = classify.shared_cache("cat", cat_rules), classify.shared_cache("proj", proj_rules)
    ev_cat, ev_projs = {}, {}
    for r in rows:
        ev_cat[r["id"]] = classify.categorize(r, cat_rules, ccache)
        names = set()
        for _, _pid, pname in classify.project_spans(
                r, proj_rules, sweep.overlapping(r["start_ts"], r["end_ts"]),
                rule_cache=rcache):
            if pname:
                names.add(pname)
        ev_projs[r["id"]] = names
    out = []
    for v in classify.group_events(rows):
        projects, categories = set(), set()
        for eid in v["event_ids"]:
            categories.add(ev_cat[eid])
            projects |= ev_projs[eid]
        out.append({**v, "active_seconds": round(v["active_seconds"]),
                    "projects": sorted(projects), "categories": sorted(categories)})
    out.reverse()
    # Direct JSONResponse skips FastAPI's jsonable_encoder walk (~60ms on this
    # payload); everything here is already plain dict/list/str/num.
    return JSONResponse(out)


# --------------------------------------------------------------------------
# Ask tab: locally saved chats with an AI analyst over the usage data
# --------------------------------------------------------------------------
CHAT_SYSTEM = """You are the built-in analyst of a fully-local time tracker running on the user's Mac.
Below is an auto-generated digest of their real tracked computer usage.
Ground every claim in this data — cite actual hours, percentages, app/site/project names.
Never invent numbers or activity that isn't in the digest. If the data can't answer
something, say so plainly and suggest what to track or ask instead.

The user asks things like: what am I good at, where does my time go, how focused am I,
what business or career opportunities fit my demonstrated skills. Be direct, specific
and honest — including about weaknesses (heavy entertainment time, fragmented focus,
low deep-work share). When suggesting ideas (business, learning, portfolio), tie each
one to concrete evidence in the data, and prefer a few well-argued ideas over a long list.

Answer in the language the user writes in. Format tightly: short paragraphs, **bold**
for key numbers and takeaways, "- " bullets for lists. No markdown tables or headings."""


def _fmt_h(secs):
    return f"{secs / 3600:.1f}h"


def _digest_range(label, o):
    s, ai, fo, ct = o["summary"], o["ai_usage"], o["focus"], o["coding_tools"]

    def line(items, n):
        return ", ".join(f'{i["name"]} {_fmt_h(i["seconds"])}' for i in items[:n])

    proj = ", ".join(
        f'{p["name"]} {_fmt_h(p["seconds"])}'
        + (f' ({line(p["children"], 4)})' if p.get("children") else "")
        for p in s["by_project"][:10])
    parts = [
        f"{label}: active {_fmt_h(s['active_seconds'])}, away {_fmt_h(s['afk_seconds'])}, "
        f"AI-assisted {ai['pct']}% ({_fmt_h(ai['ai_seconds'])}), coding {_fmt_h(ai['coding_seconds'])}",
        f" categories: {line(s['by_category'], 10)}",
        f" top apps: {line(s['by_app'], 12)}",
        f" top sites: {line(s['by_domain'], 12)}",
    ]
    if proj:
        parts.append(f" projects: {proj}")
    if ct["tools"]:
        parts.append(f" coding tools/agents: {line(ct['tools'], 8)}")
    score = fo["focus_score"] if fo["focus_score"] is not None else "?"
    parts.append(
        f" focus: deep work {_fmt_h(fo['deep_seconds'])} in {fo['deep_blocks']} blocks, "
        f"longest streak {_fmt_h(fo['longest_streak_seconds'])}, "
        f"{fo['switches_per_hour'] or '?'} switches/hour, focus score {score}/100")
    return "\n".join(parts)


def usage_digest(c):
    """Compact text digest of tracked usage for the chat model's system prompt."""
    now = time.time()
    first = c.execute("SELECT MIN(start_ts) FROM events").fetchone()[0] or now
    today = datetime.datetime.now().strftime("%Y-%m-%d (%a)")
    since = datetime.datetime.fromtimestamp(first).strftime("%Y-%m-%d")
    week = _overview_data(now - 7 * 86400, now, trend_days=0)
    month = _overview_data(now - 30 * 86400, now, trend_days=0)

    hm = heatmap(4)
    day_names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    hours = [(h, sum(hm["grid"][d][h] for d in range(7))) for h in range(24)]
    days = [(d, sum(hm["grid"][d])) for d in range(7)]
    top_hours = ", ".join(f"{h:02d}:00" for h, v in sorted(hours, key=lambda x: -x[1])[:4] if v > 0)
    top_days = ", ".join(day_names[d] for d, v in sorted(days, key=lambda x: -x[1])[:3] if v > 0)

    titles = c.execute(
        "SELECT window_title t, SUM(end_ts - start_ts) s FROM events "
        "WHERE start_ts > ? AND is_afk = 0 AND window_title != '' "
        "GROUP BY window_title ORDER BY s DESC LIMIT 30", (now - 30 * 86400,)).fetchall()
    title_lines = "; ".join(f'"{r["t"][:70]}" {_fmt_h(r["s"])}' for r in titles if r["s"] >= 600)

    projs = [dict(r) for r in c.execute("SELECT * FROM projects")]
    names = {p["id"]: p["name"] for p in projs}
    proj_list = ", ".join(
        p["name"] + f' ({p["kind"]}' + (f' under {names[p["parent_id"]]}' if p.get("parent_id") in names else "") + ")"
        for p in projs)

    return "\n\n".join(x for x in [
        f"USER USAGE DATA (from their local time tracker; auto-generated)\n"
        f"Today: {today} · tracking since {since}",
        _digest_range("LAST 7 DAYS", week),
        _digest_range("LAST 30 DAYS", month),
        f"WORK RHYTHM (last 4 weeks): most active hours {top_hours or '?'}; busiest days {top_days or '?'}",
        f"TOP WINDOW TITLES / PAGES (30 days, 10m+ each): {title_lines}" if title_lines else "",
        f"ALL PROJECTS: {proj_list}" if proj_list else "",
    ] if x)


class ChatIn(BaseModel):
    thread_id: Optional[int] = None
    message: str
    model: Optional[str] = None


@app.get("/api/chat/threads")
def chat_threads():
    c = conn()
    return [dict(r) for r in c.execute(
        "SELECT t.id, t.title, t.updated_ts, COUNT(m.id) AS messages "
        "FROM chat_threads t LEFT JOIN chat_messages m ON m.thread_id = t.id "
        "GROUP BY t.id ORDER BY t.updated_ts DESC")]


@app.get("/api/chat/threads/{tid}")
def chat_thread(tid: int):
    c = conn()
    t = c.execute("SELECT * FROM chat_threads WHERE id = ?", (tid,)).fetchone()
    if not t:
        raise HTTPException(404, "No such chat")
    msgs = [dict(r) for r in c.execute(
        "SELECT id, role, content, created_ts FROM chat_messages "
        "WHERE thread_id = ? ORDER BY id", (tid,))]
    return {**dict(t), "messages": msgs}


@app.delete("/api/chat/threads/{tid}")
def delete_chat_thread(tid: int):
    c = conn()
    c.execute("DELETE FROM chat_threads WHERE id = ?", (tid,))
    c.commit()
    return {"ok": True}


@app.post("/api/chat")
def chat(p: ChatIn):
    msg = p.message.strip()
    if not msg:
        raise HTTPException(400, "Empty message")
    c = conn()
    now = time.time()
    if p.thread_id:
        if not c.execute("SELECT 1 FROM chat_threads WHERE id = ?", (p.thread_id,)).fetchone():
            raise HTTPException(404, "No such chat")
        tid = p.thread_id
    else:
        title = msg if len(msg) <= 60 else msg[:59] + "…"
        tid = c.execute(
            "INSERT INTO chat_threads (title, created_ts, updated_ts) VALUES (?, ?, ?)",
            (title, now, now)).lastrowid
    history = [{"role": r["role"], "content": r["content"]} for r in c.execute(
        "SELECT role, content FROM chat_messages WHERE thread_id = ? ORDER BY id", (tid,))]
    system = CHAT_SYSTEM + "\n\n" + usage_digest(c)
    try:
        reply = ai_categorize.chat_llm(
            p.model, system, [*history[-20:], {"role": "user", "content": msg}])
    except Exception as e:
        # Nothing is committed on failure, so a freshly created thread rolls back.
        raise HTTPException(400, f"AI request failed: {e}")
    c.executemany(
        "INSERT INTO chat_messages (thread_id, role, content, created_ts) VALUES (?, ?, ?, ?)",
        [(tid, "user", msg, now), (tid, "assistant", reply, time.time())])
    c.execute("UPDATE chat_threads SET updated_ts = ? WHERE id = ?", (time.time(), tid))
    c.commit()
    return {"thread_id": tid, "reply": reply}


class AIProposeIn(BaseModel):
    start: float
    end: float
    model: Optional[str] = None


@app.post("/api/ai/propose")
def ai_propose(p: AIProposeIn):
    try:
        return ai_categorize.propose(p.start, p.end, p.model)
    except Exception as e:
        raise HTTPException(500, str(e))


@app.post("/api/ai/suggest_projects")
def ai_suggest_projects(p: AIProposeIn):
    try:
        return ai_categorize.suggest_projects(p.start, p.end, p.model)
    except Exception as e:
        raise HTTPException(500, str(e))


class AIFindIn(BaseModel):
    description: str
    start: float
    end: float
    model: Optional[str] = None


@app.post("/api/ai/find_project")
def ai_find_project(p: AIFindIn):
    """User describes a project in their own words; the model finds matching
    unattributed history and proposes a name + rules for it."""
    try:
        return ai_categorize.find_project(p.description, p.start, p.end, p.model)
    except Exception as e:
        raise HTTPException(500, str(e))


class AIApplyItem(BaseModel):
    project_id: int
    event_ids: list[int]


class AIApplyIn(BaseModel):
    items: list[AIApplyItem]


@app.post("/api/ai/apply")
def ai_apply(p: AIApplyIn):
    c = conn()
    created = 0
    for item in p.items:
        for eid in item.event_ids:
            row = c.execute("SELECT start_ts, end_ts FROM events WHERE id=?", (eid,)).fetchone()
            if row:
                c.execute("INSERT INTO assignments (start_ts, end_ts, project_id, note) VALUES (?,?,?,?)",
                          (row["start_ts"], row["end_ts"], item.project_id, "ai"))
                created += 1
    c.commit()
    return {"created": created}


@app.get("/api/ai/models")
def ai_models():
    return ai_categorize.model_availability()


class AISettingsIn(BaseModel):
    ai_model: Optional[str] = None
    anthropic_api_key: Optional[str] = None
    gemini_api_key: Optional[str] = None
    email_to: Optional[str] = None
    afk_idle_minutes: Optional[float] = None
    distraction_categories: Optional[list] = None
    unassigned_prompt_minutes: Optional[float] = None


@app.get("/api/settings")
def get_settings():
    """Non-secret settings the UI needs to pre-fill (never returns API keys)."""
    cfg = ai_categorize.load_config()
    return {"email_to": cfg.get("email_to", ""),
            "afk_idle_minutes": cfg.get("afk_idle_minutes", 5),
            "unassigned_prompt_minutes": cfg.get("unassigned_prompt_minutes", 1),
            "distraction_categories": sorted(distraction_categories())}


@app.post("/api/ai/settings")
def ai_settings(p: AISettingsIn):
    cfg = ai_categorize.load_config()
    for field in ("ai_model", "anthropic_api_key", "gemini_api_key", "email_to"):
        val = getattr(p, field)
        if val:
            cfg[field] = val.strip()
    if p.afk_idle_minutes is not None:
        # Clamp to a sane range; the collector re-reads this within a minute.
        cfg["afk_idle_minutes"] = max(0.5, min(60.0, p.afk_idle_minutes))
    if p.distraction_categories is not None:
        # Empty list falls back to the default at read time.
        cfg["distraction_categories"] = [
            str(c).strip() for c in p.distraction_categories if str(c).strip()]
    if p.unassigned_prompt_minutes is not None:
        cfg["unassigned_prompt_minutes"] = max(
            0.5, min(60.0, p.unassigned_prompt_minutes))
    ai_categorize.save_config(cfg)
    return {"ok": True}


class ReviewSkipIn(BaseModel):
    start_ts: float
    end_ts: float
    keys: list[str]


@app.post("/api/review/skip")
def review_skip(p: ReviewSkipIn):
    """Hide the Overview reminder for this block. Does not assign a project."""
    if p.end_ts <= p.start_ts:
        raise HTTPException(422, "end must be after start")
    keys = [k.strip() for k in p.keys if k and str(k).strip()]
    if not keys:
        raise HTTPException(422, "keys required")
    c = conn()
    now = time.time()
    for k in keys:
        c.execute(
            "INSERT INTO review_skips (start_ts, end_ts, visit_key, created_ts) "
            "VALUES (?,?,?,?)", (p.start_ts, p.end_ts, k, now))
    c.commit()
    return {"ok": True}


@app.get("/api/project_report")
def project_report():
    """Exact time per project: today / 7 days / 30 days / all time (children rolled up)."""
    c = conn()
    _, proj_rules = classify.load_rules(c)
    windows = report_windows()
    assignments = classify.load_assignments(c, 0, time.time())
    rows = c.execute("SELECT * FROM events WHERE is_afk = 0 ORDER BY start_ts").fetchall()

    projs = {r["id"]: dict(r) for r in c.execute("SELECT * FROM projects")}
    totals = {pid: {k: 0.0 for k in windows} for pid in projs}
    sweep = classify.AssignmentSweep(assignments)
    rcache = classify.shared_cache("proj", proj_rules)
    for row in rows:
        ovl = sweep.overlapping(row["start_ts"], row["end_ts"])
        spans = classify.project_spans(row, proj_rules, ovl, rule_cache=rcache)
        for k, w0 in windows.items():
            if row["start_ts"] >= w0:            # row fully inside the window
                win_spans = spans
            elif row["end_ts"] > w0:             # straddles the window start
                win_spans = classify.project_spans(row, proj_rules, ovl, start=w0,
                                                   rule_cache=rcache)
            else:
                continue
            for secs, pid, _ in win_spans:
                if pid in totals:
                    totals[pid][k] += secs

    for m in c.execute("SELECT * FROM manual_entries"):
        if m["project_id"] not in totals:
            continue
        for k, w0 in windows.items():
            lo = max(m["start_ts"], w0)
            if lo < m["end_ts"]:
                totals[m["project_id"]][k] += m["end_ts"] - lo

    kids = child_map(projs)

    def agg(pid):
        """Whole-subtree totals for each window (project + all descendants)."""
        sub = subtree_ids(kids, pid)
        return {w: sum(totals[n][w] for n in sub) for w in windows}

    out = []
    for pid, p in projs.items():
        if p.get("parent_id") in projs:
            continue
        a = agg(pid)
        if a["all"] < 1 and not p.get("pinned"):   # pinned projects show even with no time
            continue
        children = []
        for k in kids.get(pid, []):
            ka = agg(k)
            if ka["all"] >= 1 or projs[k].get("pinned"):
                children.append({"id": k, "name": projs[k]["name"],
                                 "archived": bool(projs[k].get("archived")),
                                 "pinned": bool(projs[k].get("pinned")),
                                 **{w: round(ka[w]) for w in windows}})
        out.append({"id": pid, "name": p["name"], "kind": p["kind"],
                    "archived": bool(p.get("archived")),
                    "pinned": bool(p.get("pinned")),
                    **{w: round(a[w]) for w in windows},
                    "children": sorted(children, key=lambda x: (not x["pinned"], -x["all"]))})
    out.sort(key=lambda x: (not x["pinned"], -x["all"]))
    return out


@app.get("/api/project_detail")
def project_detail(id: int, days: int = 30):
    """Everything about one project: totals, daily trend, and how its time
    splits across apps, sites, sub-projects, categories, and pages.
    `days` = 0 means all time (breakdowns cover the whole history)."""
    c = conn()
    proj = c.execute("SELECT * FROM projects WHERE id=?", (id,)).fetchone()
    if not proj:
        raise HTTPException(404, "no such project")
    projs = {r["id"]: dict(r) for r in c.execute("SELECT * FROM projects")}
    kmap = child_map(projs)
    # Whole subtree under this project: totals and raw activity roll up any depth.
    targets = set(subtree_ids(kmap, id))
    direct_children = kmap.get(id, [])
    # Map every node in the subtree to the direct child it rolls up into (the
    # project's own time maps to itself), so activity can be split per sub-project.
    bucket_of = {id: id}
    for dc in direct_children:
        for n in subtree_ids(kmap, dc):
            bucket_of[n] = dc
    bucket_secs = defaultdict(float)
    # Ancestor breadcrumb (root → … → parent), so the user can navigate back up.
    ancestors, cur = [], proj["parent_id"]
    while cur in projs:
        ancestors.append({"id": cur, "name": projs[cur]["name"]})
        cur = projs[cur].get("parent_id")
    ancestors.reverse()

    cat_rules, proj_rules = classify.load_rules(c)
    ai_rules = classify.load_ai_rules(c)
    windows = report_windows()
    today0 = windows["today"]
    all_time = days <= 0
    detail_start = 0 if all_time else today0 - (days - 1) * 86400

    assignments = classify.load_assignments(c, 0, time.time())
    rows = c.execute("SELECT * FROM events WHERE is_afk = 0 ORDER BY start_ts").fetchall()

    totals = {k: 0.0 for k in windows}
    daily = defaultdict(float)
    by_app, by_domain, by_title, by_cat = (defaultdict(float) for _ in range(4))
    ai_secs = detail_total = 0.0
    ai_by_tool = defaultdict(float)

    sweep = classify.AssignmentSweep(assignments)
    rcache, ccache, tcache = classify.shared_cache("proj", proj_rules), classify.shared_cache("cat", cat_rules), classify.shared_cache("ai", ai_rules)
    for row in rows:
        ovl = sweep.overlapping(row["start_ts"], row["end_ts"])
        spans = classify.project_spans(row, proj_rules, ovl, rule_cache=rcache)
        secs = sum(s for s, pid, _ in spans if pid in targets)
        if secs <= 0:
            continue
        for k, w0 in windows.items():
            if row["start_ts"] >= w0:            # row fully inside the window
                totals[k] += secs
            elif row["end_ts"] > w0:             # straddles the window start
                totals[k] += sum(s for s, pid, _ in
                                 classify.project_spans(row, proj_rules, ovl, start=w0,
                                                        rule_cache=rcache)
                                 if pid in targets)
        if row["end_ts"] < detail_start:
            continue
        day = datetime.datetime.fromtimestamp(row["start_ts"]).strftime("%Y-%m-%d")
        daily[day] += secs
        detail_total += secs
        tool, row_ai = classify.tool_and_ai(row, ai_rules, tcache)
        if row_ai:
            ai_secs += secs
            ai_by_tool[classify.ai_label(row, tool)] += secs
        by_app[row["app"] or "?"] += secs
        if row["domain"]:
            by_domain[row["domain"]] += secs
        if row["window_title"]:
            by_title[row["window_title"][:70]] += secs
        by_cat[classify.categorize(row, cat_rules, ccache)] += secs
        for s, pid, _ in spans:
            if pid in targets:
                bucket_secs[bucket_of[pid]] += s

    manual = [dict(r) for r in c.execute(
        "SELECT m.*, p.name AS project_name FROM manual_entries m "
        "JOIN projects p ON p.id = m.project_id "
        "WHERE m.project_id IN (%s) ORDER BY m.start_ts DESC" % ",".join("?" * len(targets)),
        tuple(targets))]
    for m in manual:
        dur = m["end_ts"] - m["start_ts"]
        for k, w0 in windows.items():
            lo = max(m["start_ts"], w0)
            if lo < m["end_ts"]:
                totals[k] += m["end_ts"] - lo
        if m["end_ts"] >= detail_start:
            day = datetime.datetime.fromtimestamp(m["start_ts"]).strftime("%Y-%m-%d")
            daily[day] += dur
            by_app["Logged manually"] += dur
            by_cat["Logged manually"] += dur
            bucket_secs[bucket_of.get(m["project_id"], id)] += dur

    if all_time:
        # Chart from the project's first tracked day through today.
        first = min(daily) if daily else None
        span = ((datetime.date.fromtimestamp(today0)
                 - datetime.date.fromisoformat(first)).days + 1) if first else 30
        chart_days = max(7, span)
    else:
        chart_days = days
    day_list = [{"date": key, "label": label, "seconds": round(daily.get(key, 0))}
                for key, label in day_series(chart_days, today0)]

    rules = _session_rules(c)
    sess = []
    for x in c.execute(
            "SELECT s.*, p.name AS project_name FROM sessions s "
            "LEFT JOIN projects p ON p.id = s.project_id "
            "WHERE s.project_id IN (%s) AND s.end_ts IS NOT NULL "
            "ORDER BY s.start_ts DESC LIMIT 10" % ",".join("?" * len(targets)),
            tuple(targets)).fetchall():
        sess.append({**dict(x), "metrics": _session_metrics(c, x, rules)})

    # Per-sub-project split (last `days`): each direct child carries its whole
    # subtree; the project's own directly-tracked time is a bucket too. Ids are
    # included so the UI can make each row a drill-in link.
    def bucket_name(bid):
        return projs[bid]["name"] if bid != id else proj["name"] + " (directly)"
    by_subproject = sorted(
        ({"id": bid, "name": bucket_name(bid), "seconds": round(v)}
         for bid, v in bucket_secs.items() if round(v) > 0),
        key=lambda x: -x["seconds"])[:10]

    children = [{"id": k, "name": projs[k]["name"], "kind": projs[k]["kind"],
                 "archived": bool(projs[k].get("archived"))}
                for k in direct_children]

    return {
        "project": dict(proj),
        "ancestors": ancestors,
        "children": children,
        "totals": {k: round(v) for k, v in totals.items()},
        "daily": day_list,
        "by_app": top(by_app, 10), "by_domain": top(by_domain, 10),
        "by_title": top(by_title, 12), "by_category": top(by_cat, 10),
        "by_subproject": by_subproject,
        "ai": {"seconds": round(ai_secs),
               "pct": round(100 * ai_secs / detail_total) if detail_total else None,
               "by_tool": top(ai_by_tool, 8), "days": days},
        "sessions": sess,
        "manual_entries": manual,
    }


class BriefIn(BaseModel):
    period: str = "today"


@app.post("/api/brief")
def send_brief(p: BriefIn):
    from . import report
    try:
        result = report.send_brief(p.period)
        return {"ok": True, "to": ai_categorize.load_config().get("email_to"),
                "id": result.get("id")}
    except Exception as e:
        raise HTTPException(500, str(e))


# --------------------------------------------------------------------------
@app.get("/api/heatmap")
def heatmap(weeks: int = 4):
    """Active seconds per (weekday, hour) over the last N weeks."""
    end = time.time()
    start = end - weeks * 7 * 86400
    rows = conn().execute(
        "SELECT start_ts, end_ts FROM events WHERE is_afk=0 AND end_ts > ? AND start_ts < ?",
        (start, end)).fetchall()
    grid = [[0.0] * 24 for _ in range(7)]
    for r in rows:
        s, e = max(r["start_ts"], start), min(r["end_ts"], end)
        while s < e:
            dt = datetime.datetime.fromtimestamp(s)
            hour_end = dt.replace(minute=0, second=0, microsecond=0).timestamp() + 3600
            grid[dt.weekday()][dt.hour] += min(e, hour_end) - s
            s = hour_end
    return {"grid": [[round(v) for v in row] for row in grid], "weeks": weeks}


# --------------------------------------------------------------------------
@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC, "index.html"))


# PWA: manifest at the root so its scope covers "/"; icons under /static.
@app.get("/manifest.json")
def manifest():
    return FileResponse(os.path.join(STATIC, "manifest.json"))


@app.get("/static/{name}")
def static_file(name: str):
    path = os.path.join(STATIC, os.path.basename(name))
    if not os.path.isfile(path):
        raise HTTPException(404, "not found")
    return FileResponse(path)


if __name__ == "__main__":
    import webbrowser

    import uvicorn
    port = int(os.environ.get("TT_PORT", "8321"))
    webbrowser.open(f"http://127.0.0.1:{port}")
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
