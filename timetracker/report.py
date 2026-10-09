#!/usr/bin/env python3
"""Email briefs via Resend: on-demand from the dashboard, weekly via launchd.

Formatted with email-safe inline-CSS bar charts, period-over-period trends,
and AI-generated insights (uses the configured model; falls back to computed
insights if no model is reachable).

Usage: tt report --period today|yesterday|week|month
"""
import argparse
import datetime
import json
import sys
import time
import urllib.request
from collections import defaultdict

from . import ai_categorize
from . import db

# light-mode palette, mirrors the dashboard's category colors
SLOTS = ["#2a78d6", "#1baf7a", "#eda100", "#008300", "#4a3aa7", "#e34948", "#e87ba4", "#eb6834"]
FIXED = {"Coding": 0, "AI Tools": 4, "Communication": 1, "Docs & Writing": 2,
         "Reading & Study": 3, "Entertainment": 5, "Browsing": 6, "Design": 7,
         "Databases": 4, "Spreadsheets": 2, "Finance & Admin": 7}


def color_of(name):
    if name in FIXED:
        return SLOTS[FIXED[name]]
    if not name or name in ("AFK", "Uncategorized"):
        return "#c3c2b7"
    return SLOTS[sum(ord(ch) for ch in name) % 8]


def fmt(s):
    s = round(s)
    h, m = s // 3600, (s % 3600) // 60
    return f"{h}h {m:02d}m" if h else (f"{m}m" if m else f"{s}s")


def day_start(offset=0):
    d = datetime.datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    return d.timestamp() + offset * 86400


def period_range(period):
    if period == "today":
        return day_start(), time.time(), "Today"
    if period == "yesterday":
        return day_start(-1), day_start(), "Yesterday"
    if period == "month":
        return day_start(-29), time.time(), "Last 30 days"
    return day_start(-6), time.time(), "Last 7 days"


# ---------------------------------------------------------------------------
# HTML building blocks (inline CSS only — email clients strip everything else)
# ---------------------------------------------------------------------------
def bar_rows(items, n=7):
    if not items:
        return '<tr><td style="color:#888;padding:4px 0;font-size:13px">nothing recorded</td></tr>'
    mx = max(it["seconds"] for it in items) or 1
    out = ""
    for it in items[:n]:
        pct = max(2, round(100 * it["seconds"] / mx))
        color = color_of(it["name"])
        sub = (f'<div style="font-size:11px;color:#8a8880;margin-top:1px;font-weight:400">{it["sub"]}</div>'
               if it.get("sub") else "")
        out += f"""<tr>
  <td style="padding:3px 10px 3px 0;font-size:13px;color:#111;max-width:170px;overflow:hidden">{it["name"]}{sub}</td>
  <td style="width:230px;padding:3px 0"><div style="background:#eeede8;border-radius:4px;height:12px"><div style="width:{pct}%;background:{color};height:12px;border-radius:4px"></div></div></td>
  <td style="padding:3px 0 3px 10px;font-size:13px;color:#444;text-align:right;white-space:nowrap">{fmt(it["seconds"])}{it.get("delta_html","")}</td>
</tr>"""
        for k in it.get("children", []):
            kpct = max(2, round(100 * k["seconds"] / mx))
            ksub = (f'<span style="color:#a9a79f"> · {k["sub"]}</span>' if k.get("sub") else "")
            out += f"""<tr>
  <td style="padding:2px 10px 2px 16px;font-size:12px;color:#666">{k["name"]}</td>
  <td style="width:230px;padding:2px 0"><div style="background:#f4f3ee;border-radius:3px;height:8px"><div style="width:{kpct}%;background:{color};opacity:.55;height:8px;border-radius:3px"></div></div></td>
  <td style="padding:2px 0 2px 10px;font-size:12px;color:#777;text-align:right;white-space:nowrap">{fmt(k["seconds"])}{ksub}</td>
</tr>"""
    return out


def section(title, inner):
    return (f'<h3 style="margin:22px 0 8px;font-size:13px;color:#52514e;'
            f'text-transform:uppercase;letter-spacing:.05em">{title}</h3>'
            f'<table style="border-collapse:collapse;width:100%">{inner}</table>')


def _arrow(d):
    """(arrow, color) for a positive/negative period-over-period delta."""
    return ("▲", "#0a7a0a") if d > 0 else ("▼", "#b34040")


def pct_str(part, whole):
    """Percent for display — never rounds a real slice down to a bare '0%'."""
    if not whole:
        return "0%"
    p = 100 * part / whole
    return f"{round(p)}%" if p >= 1 else "<1%"


def delta_html(cur, prev):
    d = cur - prev
    if abs(d) < 120:
        return ""
    arrow, color = _arrow(d)
    return (f'<br><span style="font-size:11px;color:{color}">{arrow} '
            f'{fmt(abs(d))} vs prev</span>')


def split_bar(parts):
    """One horizontal stacked bar: [(label, seconds, color), ...]"""
    total = sum(p[1] for p in parts) or 1
    cells = "".join(
        f'<td style="width:{max(1, round(100 * s / total))}%;background:{c};height:14px;'
        f'border-radius:0"></td>'
        for _, s, c in parts if s > 0)
    legend = " · ".join(f'<span style="color:{c}">●</span> {l} {fmt(s)}'
                        for l, s, c in parts if s > 0)
    return (f'<table style="border-collapse:collapse;width:100%;border-radius:5px;overflow:hidden">'
            f'<tr>{cells}</tr></table>'
            f'<div style="font-size:12px;color:#555;margin-top:5px">{legend}</div>')


def stat_cells(triples):
    """A row of (label, value, sub) stat tiles, email-safe."""
    tds = ""
    for label, value, sub in triples:
        sub_html = (f'<div style="font-size:11px;color:#777;margin-top:1px">{sub}</div>'
                    if sub else "")
        tds += (f'<td style="padding:6px 18px 6px 0;vertical-align:top">'
                f'<div style="font-size:11px;color:#898781;text-transform:uppercase;letter-spacing:.04em">{label}</div>'
                f'<div style="font-size:16px;font-weight:650;color:#111;margin-top:1px">{value}</div>{sub_html}</td>')
    return f'<table style="border-collapse:collapse"><tr>{tds}</tr></table>'


def daily_active(start, end):
    """Active (non-AFK) seconds per local 'YYYY-MM-DD' day in [start, end)."""
    c = db.connect(readonly=True)
    days = defaultdict(float)
    for row in c.execute(
            "SELECT start_ts, end_ts FROM events "
            "WHERE end_ts > ? AND start_ts < ? AND is_afk = 0", (start, end)):
        s, e = max(row["start_ts"], start), min(row["end_ts"], end)
        while s < e:
            d = datetime.datetime.fromtimestamp(s)
            day_end = (d.replace(hour=0, minute=0, second=0, microsecond=0)
                       + datetime.timedelta(days=1)).timestamp()
            days[d.strftime("%Y-%m-%d")] += min(e, day_end) - s
            s = day_end
    c.close()
    return days


def hourly_active(start, end):
    """Active (non-AFK) seconds per local hour-of-day (0-23) in [start, end)."""
    c = db.connect(readonly=True)
    hours = [0.0] * 24
    for row in c.execute(
            "SELECT start_ts, end_ts FROM events "
            "WHERE end_ts > ? AND start_ts < ? AND is_afk = 0", (start, end)):
        s, e = max(row["start_ts"], start), min(row["end_ts"], end)
        while s < e:
            dt = datetime.datetime.fromtimestamp(s)
            hour_end = dt.replace(minute=0, second=0, microsecond=0).timestamp() + 3600
            hours[dt.hour] += min(e, hour_end) - s
            s = hour_end
    c.close()
    return hours


def _hour_label(h):
    return f"{(h % 12) or 12}{'am' if h < 12 else 'pm'}"


def peak_window(hours, width=3):
    """Busiest `width`-hour stretch of the day → ('9am–12pm', seconds) or None."""
    if sum(hours) <= 0:
        return None
    best = max(range(24), key=lambda h: sum(hours[(h + i) % 24] for i in range(width)))
    secs = sum(hours[(best + i) % 24] for i in range(width))
    return f"{_hour_label(best)}–{_hour_label((best + width) % 24)}", secs


def all_time_projects():
    """{project name: lifetime seconds} — top-level projects and their children.

    Uses the same subtree rollup as the period summary, so a parent's lifetime
    total includes every descendant. Best-effort: never break the brief.
    """
    try:
        from .dashboard import summary
        allt = summary(0, time.time())
    except Exception:
        return {}, None
    totals = {}
    for p in allt["by_project"]:
        totals[p["name"]] = p["seconds"]
        for k in p.get("children", []):
            totals[k["name"]] = k["seconds"]
    since = None
    try:
        c = db.connect(readonly=True)
        row = c.execute("SELECT MIN(start_ts) AS t FROM events").fetchone()
        c.close()
        if row and row["t"]:
            since = datetime.datetime.fromtimestamp(row["t"])
    except Exception:
        pass
    return totals, since


ACTIVE_DAY_MIN_SECS = 600   # ≥10m of activity counts a day as "active"


def glance_html(start, end, title):
    """Stat tiles for any multi-day period: rhythm, consistency, best day."""
    days = daily_active(start, end)
    if not days:
        return ""
    n_days = max(1, round((end - start) / 86400))
    d0 = datetime.datetime.fromtimestamp(start).replace(
        hour=0, minute=0, second=0, microsecond=0)
    day_keys = [(d0 + datetime.timedelta(days=i)).strftime("%Y-%m-%d")
                for i in range(n_days)]
    active_keys = [k for k in day_keys if days.get(k, 0) >= ACTIVE_DAY_MIN_SECS]
    longest = run = 0
    active_set = set(active_keys)
    for k in day_keys:
        run = run + 1 if k in active_set else 0
        longest = max(longest, run)
    busiest_key = max(days, key=days.get)
    busiest_dt = datetime.datetime.strptime(busiest_key, "%Y-%m-%d")
    tiles = [
        ("Daily average", fmt(sum(days.values()) / max(1, len(active_keys))), "per active day"),
        ("Active days", f"{len(active_keys)} of {n_days}", "days with ≥ 10m"),
        ("Busiest day", fmt(days[busiest_key]), busiest_dt.strftime("%a %b %-d")),
        ("Best streak", f"{longest} day{'s' if longest != 1 else ''}", "consecutive active days"),
    ]
    pk = peak_window(hourly_active(start, end))
    if pk:
        tiles.append(("Peak hours", pk[0], f"{fmt(pk[1])} of your active time"))
    return section(title, f'<tr><td style="padding:4px 0">{stat_cells(tiles)}</td></tr>')


def month_extras_html(start, end):
    """Monthly-only section: the week-by-week chart."""
    days = daily_active(start, end)
    if not days:
        return ""
    n_days = max(1, round((end - start) / 86400))
    d0 = datetime.datetime.fromtimestamp(start).replace(
        hour=0, minute=0, second=0, microsecond=0)
    day_keys = [(d0 + datetime.timedelta(days=i)).strftime("%Y-%m-%d")
                for i in range(n_days)]

    rows = ""
    weekly = []
    for i in range(0, n_days, 7):
        chunk = day_keys[i:i + 7]
        weekly.append((chunk[0], chunk[-1], sum(days.get(k, 0) for k in chunk)))
    mx = max((w[2] for w in weekly), default=0) or 1
    for k0, k1, secs in weekly:
        lbl = (datetime.datetime.strptime(k0, "%Y-%m-%d").strftime("%b %-d") + " – "
               + datetime.datetime.strptime(k1, "%Y-%m-%d").strftime("%b %-d"))
        pct = max(2, round(100 * secs / mx))
        rows += (f'<tr><td style="padding:3px 10px 3px 0;font-size:13px;color:#111;white-space:nowrap">{lbl}</td>'
                 f'<td style="width:230px;padding:3px 0"><div style="background:#eeede8;border-radius:4px;height:12px">'
                 f'<div style="width:{pct}%;background:#2a78d6;height:12px;border-radius:4px"></div></div></td>'
                 f'<td style="padding:3px 0 3px 10px;font-size:13px;color:#444;text-align:right;white-space:nowrap">{fmt(secs)}</td></tr>')

    return section("Week by week", rows)


# ---------------------------------------------------------------------------
# Insights
# ---------------------------------------------------------------------------
INSIGHT_SCHEMA = {
    "type": "object",
    "properties": {
        "insights": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"emoji": {"type": "string"}, "text": {"type": "string"}},
                "required": ["emoji", "text"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["insights"],
    "additionalProperties": False,
}

INSIGHT_SYSTEM = """You are a friendly, sharp productivity coach reviewing someone's
computer time data. Their projects (and any study subjects) are listed in the data.
Write 3-5 short insights (one sentence each, max ~25 words), each with a fitting emoji:
- where most of their time actually went, and whether that matches their priorities
- the single biggest time sink worth cutting, with a concrete suggestion
- whether projects/study are on track vs the previous period (use the deltas)
- how this period's project time compares to that project's lifetime total
  (`all_time_by_project`) — e.g. a push on something long-running, or a project
  that has quietly stalled — and anything notable about focus, rhythm or idle time
Only discuss projects that appear in this period's `projects` list: the reader
wants to know what they did in THIS window, not what they neglected.
Be specific — name the apps/sites and amounts. No fluff, no generic advice.
Quote durations using the `time` field ("7h 52m"), never the raw `seconds`
number, and never invent a figure that is not in the data."""


def computed_insights(cur, prev, lifetime=None):
    out = []
    cats = {c["name"]: c["seconds"] for c in cur["by_category"]}
    if cur["by_category"]:
        top = cur["by_category"][0]
        out.append({"emoji": "⏱", "text": f'Most time went to {top["name"]} ({fmt(top["seconds"])}).'})
    ent = cats.get("Entertainment", 0)
    if ent > 1800:
        out.append({"emoji": "✂️", "text": f"Entertainment took {fmt(ent)} — the easiest place to claw time back."})
    for p in cur["by_project"][:2]:
        pv = next((q["seconds"] for q in prev["by_project"] if q["name"] == p["name"]), 0)
        if p["seconds"] > pv + 120:
            out.append({"emoji": "📈", "text": f'{p["name"]} is up {fmt(p["seconds"] - pv)} vs the previous period — on track.'})
        elif pv > p["seconds"] + 120:
            out.append({"emoji": "📉", "text": f'{p["name"]} is down {fmt(pv - p["seconds"])} vs the previous period.'})
    if lifetime and cur["by_project"]:
        p = cur["by_project"][0]
        total = lifetime.get(p["name"])
        if total and p["seconds"] >= 600:
            out.append({"emoji": "🏗", "text": f'{p["name"]} is now {fmt(total)} all time, {pct_str(p["seconds"], total)} of it this period.'})
    return out[:5]


def _plain(items):
    """Model-facing rows: drop presentation keys (delta_html, sub) and spell the
    duration out — given bare seconds the model reads 28362 as "28h"."""
    return [{"name": it["name"], "seconds": it["seconds"], "time": fmt(it["seconds"]),
             **({"children": _plain(it["children"])} if it.get("children") else {})}
            for it in items]


def build_insights(cur, prev, label, lifetime=None, extras=None):
    cfg = ai_categorize.load_config()
    payload = {
        "period": label,
        "current": {
            "active": fmt(cur["active_seconds"]), "idle": fmt(cur["afk_seconds"]),
            "categories": _plain(cur["by_category"][:8]),
            "projects": _plain(cur["by_project"][:6]),
            "top_apps": _plain(cur["by_app"][:6]),
            "top_sites": _plain(cur["by_domain"][:6]),
        },
        "previous_period": {
            "active": fmt(prev["active_seconds"]),
            "categories": _plain(prev["by_category"][:8]),
            "projects": _plain(prev["by_project"][:6]),
        },
        "all_time_by_project": {k: fmt(v) for k, v in (lifetime or {}).items()},
        **(extras or {}),
    }
    try:
        result = ai_categorize.call_llm(
            cfg.get("ai_model", ai_categorize.DEFAULT_MODEL), INSIGHT_SYSTEM,
            json.dumps(payload), schema=INSIGHT_SCHEMA)
        insights = result.get("insights", [])[:5]
        if insights:
            return insights
    except Exception:
        pass
    return computed_insights(cur, prev, lifetime)


# ---------------------------------------------------------------------------
def build_html(start, end, label):
    from .dashboard import ai_usage, coding_tools, focus, list_sessions, summary
    cur = summary(start, end)
    ct = coding_tools(start, end)
    span = end - start
    prev = summary(start - span, start)
    sessions = [x for x in list_sessions(since=start) if x["end_ts"]]
    # Monthly briefs get extra stats and deeper splits.
    monthly = span >= 20 * 86400
    glance = glance_html(start, end, "📅 Month at a glance" if monthly
                         else "📅 At a glance") if span >= 3 * 86400 else ""
    month_html = glance + (month_extras_html(start, end) if monthly else "")
    n_proj, n_cat, n_top = (8, 10, 8) if monthly else (6, 7, 5)
    lifetime, tracking_since = all_time_projects()

    # AI usage + focus sections; never let them break the brief
    ai_html = focus_html = ""
    try:
        au = ai_usage(start, end, trend_days=0)
        if au["ai_seconds"]:
            pau = ai_usage(start - span, start, trend_days=0)
            note = f'{au["pct"]}% of active time was AI-assisted'
            if au["coding_seconds"]:
                note += f' · {au["coding_ai_pct"]}% of coding time'
            d = au["ai_seconds"] - pau["ai_seconds"]
            if abs(d) >= 120:
                arrow, color = _arrow(d)
                note += f' · <span style="color:{color}">{arrow} {fmt(abs(d))} vs prev</span>'
            bar = split_bar([("AI", au["ai_seconds"], "#4a3aa7"),
                             ("not AI", max(0, au["active_seconds"] - au["ai_seconds"]), "#c3c2b7")])
            inner = (f'<tr><td colspan="3" style="padding:2px 0 8px">{bar}'
                     f'<div style="font-size:12px;color:#555;margin-top:5px">{note}</div></td></tr>'
                     + bar_rows(au["by_tool"], 6))
            ai_html = section(f"🤖 AI usage — {fmt(au['ai_seconds'])}", inner)
    except Exception:
        pass
    try:
        fo = focus(start, end)
        if fo["active_seconds"]:
            plural = "s" if fo["deep_blocks"] != 1 else ""
            bits = [f'deep work {fmt(fo["deep_seconds"])} ({fo["deep_blocks"]} block{plural} ≥ {fo["deep_threshold_minutes"]}m)',
                    f'longest streak {fmt(fo["longest_streak_seconds"])}']
            if fo["switches_per_hour"] is not None:
                bits.append(f'{fo["switches_per_hour"]} context switches/hr')
            if fo["focus_score"] is not None:
                bits.append(f'<b>focus score {fo["focus_score"]}/100</b>')
            focus_html = section("🎯 Focus & fragmentation",
                                 f'<tr><td style="font-size:13px;color:#333;padding:4px 0;line-height:1.6">{" · ".join(bits)}</td></tr>')
    except Exception:
        pass

    # Attach period-over-period deltas + lifetime totals. Only projects with
    # time in THIS period are listed at all (summary() already drops the rest),
    # so the brief stays a record of what actually happened in the window.
    has_prev = prev["active_seconds"] > 0   # nothing to compare against otherwise
    for p in cur["by_project"]:
        pv = next((q["seconds"] for q in prev["by_project"] if q["name"] == p["name"]), 0)
        p["delta_html"] = delta_html(p["seconds"], pv) if has_prev else ""
        # Lifetime context, but only when it says something the row doesn't:
        # in a brief that already covers all of history they are the same number.
        total = lifetime.get(p["name"])
        if total and total > p["seconds"] + 60:
            p["sub"] = f"{fmt(total)} all time · {pct_str(p['seconds'], total)} of it this period"
        for k in p.get("children", []):
            kt = lifetime.get(k["name"])
            if kt and kt > k["seconds"] + 60:
                k["sub"] = f"{fmt(kt)} all time"

    proj_title = "Projects — are you on track?"
    if cur["by_project"]:
        proj_total = sum(p["seconds"] for p in cur["by_project"])
        proj_title = f"Projects — {fmt(proj_total)} on {len(cur['by_project'])} project{'s' if len(cur['by_project']) != 1 else ''}"
    proj_note = ""
    if cur["by_project"] and lifetime:
        since_str = (tracking_since.strftime("%b %-d, %Y") if tracking_since else "the start")
        proj_note = (f'<tr><td colspan="3" style="padding:6px 0 0;font-size:11px;color:#8a8880">'
                     f'Only projects you touched in this period are listed · '
                     f'“all time” totals run since {since_str}</td></tr>')

    total = cur["active_seconds"] + cur["afk_seconds"]
    pct_active = round(100 * cur["active_seconds"] / total) if total else 0

    sess_html = ""
    if sessions:
        rows = ""
        for x in sessions[:8]:
            m = x["metrics"]
            pct = m["focus_pct"]
            bar = ""
            if pct is not None:
                bcol = "#0a7a0a" if pct >= 70 else ("#eda100" if pct >= 40 else "#b34040")
                bar = (f'<div style="background:#eeede8;border-radius:4px;height:10px;width:120px">'
                       f'<div style="width:{max(2,pct)}%;background:{bcol};height:10px;border-radius:4px"></div></div>')
            rows += f"""<tr>
  <td style="padding:4px 10px 4px 0;font-size:13px">{x["label"] or x["project_name"] or "session"}</td>
  <td style="padding:4px 10px 4px 0;font-size:13px;color:#555">{fmt(m["duration_seconds"])}</td>
  <td style="padding:4px 10px 4px 0">{bar}</td>
  <td style="padding:4px 0;font-size:13px;text-align:right">{str(pct) + "% focused" if pct is not None else "—"}</td>
</tr>"""
        sess_html = section("Focus sessions", rows)

    rhythm = peak_window(hourly_active(start, end))
    insights = build_insights(cur, prev, label, lifetime,
                              {"peak_hours": rhythm[0] if rhythm else None})
    insights_html = "".join(
        f'<tr><td style="padding:5px 8px 5px 0;font-size:15px;vertical-align:top">{i["emoji"]}</td>'
        f'<td style="padding:5px 0;font-size:13px;color:#222;line-height:1.5">{i["text"]}</td></tr>'
        for i in insights)

    date_str = datetime.datetime.fromtimestamp(start).strftime("%a %b %-d")
    if span > 86400 * 1.5:
        date_str += datetime.datetime.fromtimestamp(end).strftime(" – %a %b %-d")

    return f"""
<div style="font-family:-apple-system,'Segoe UI',sans-serif;max-width:600px;margin:0 auto;color:#111;background:#fcfcfb;border:1px solid #e6e5df;border-radius:12px;padding:26px 28px">
  <div style="font-size:12px;color:#898781;text-transform:uppercase;letter-spacing:.06em">Time Tracker · {date_str}</div>
  <h2 style="font-size:21px;margin:4px 0 2px">{label}: {fmt(cur["active_seconds"])} active</h2>
  <div style="font-size:13px;color:#666;margin-bottom:10px">{pct_active}% of tracked time · {fmt(cur["afk_seconds"])} away/idle
    {(' · <b style="color:' + ('#0a7a0a' if cur["active_seconds"] >= prev["active_seconds"] else '#b34040') + '">' + ('▲' if cur["active_seconds"] >= prev["active_seconds"] else '▼') + ' ' + fmt(abs(cur["active_seconds"] - prev["active_seconds"])) + ' vs previous period</b>') if prev["active_seconds"] else ''}</div>
  {split_bar([(c["name"], c["seconds"], color_of(c["name"])) for c in cur["by_category"][:6]])}

  {month_html}
  {section("💡 Insights & suggestions", insights_html)}
  {section(proj_title, bar_rows(cur["by_project"], n_proj) + proj_note) if cur["by_project"] else ""}
  {section("Where the time went", bar_rows(cur["by_category"], n_cat))}
  {ai_html}
  {focus_html}
  {section(f"Coding tools &amp; agents — {fmt(ct['total_seconds'])} total", bar_rows(ct["tools"], 8)) if ct["tools"] else ""}
  {section("Top apps", bar_rows(cur["by_app"], n_top))}
  {section("Top sites", bar_rows(cur["by_domain"], n_top))}
  {sess_html}

  <p style="margin-top:24px;padding-top:12px;border-top:1px solid #e6e5df;color:#999;font-size:11px">
    Sent by your local time tracker · dashboard at 127.0.0.1:8321 · data stays on your Mac except this email</p>
</div>"""


def send_email(subject, html):
    cfg = ai_categorize.load_config()
    key = cfg.get("resend_api_key")
    to = cfg.get("email_to")
    if not key or not to:
        raise RuntimeError(f"resend_api_key / email_to missing in {db.CONFIG_PATH}")
    body = {"from": cfg.get("email_from", "Time Tracker <onboarding@resend.dev>"),
            "to": [to], "subject": subject, "html": html}
    req = urllib.request.Request(
        "https://api.resend.com/emails",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}",
                 "User-Agent": "timetracker/1.0"},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def send_brief(period):
    """Build and email the brief for a period; used by the CLI and /api/brief."""
    start, end, label = period_range(period)
    return send_email(f"⏱ Time brief — {label.lower()}", build_html(start, end, label))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--period", default="week",
                    choices=["today", "yesterday", "week", "month"])
    args = ap.parse_args()
    result = send_brief(args.period)
    print(f"sent: {result.get('id')}")


if __name__ == "__main__":
    sys.exit(main())
