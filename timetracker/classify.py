"""Query-time classification: apply rules + manual assignments to raw events.

Nothing is stored on events themselves, so editing rules or assignments
retroactively reclassifies all history.
"""
import re
from functools import lru_cache
from urllib.parse import unquote, urlparse


@lru_cache(maxsize=512)
def _compiled(pattern: str):
    try:
        return re.compile(pattern, re.IGNORECASE)
    except re.error:
        return None


def _matches(rule, event) -> bool:
    """`event` is an event_fields() dict (values already lowercased)."""
    value = event.get(rule["field"])
    if not value:
        return False
    mt = rule["match_type"]
    if mt == "regex":
        rx = _compiled(rule["pattern"])
        return bool(rx and rx.search(value))
    pattern = rule.get("pattern_lower") or rule["pattern"].lower()
    if mt == "contains":
        return pattern in value
    if mt == "prefix":
        return value.startswith(pattern)
    if mt == "equals":
        return value == pattern
    return False


def _with_lower(rules):
    """Attach a pre-lowered pattern so _matches doesn't lower per event."""
    for r in rules:
        r["pattern_lower"] = r["pattern"].lower()
    return rules


def load_rules(conn):
    rows = conn.execute(
        "SELECT r.*, p.name AS project_name FROM rules r "
        "LEFT JOIN projects p ON p.id = r.project_id "
        "WHERE r.enabled = 1 ORDER BY r.priority, r.id"
    ).fetchall()
    cat_rules = _with_lower([dict(r) for r in rows if r["kind"] == "category"])
    proj_rules = _with_lower([dict(r) for r in rows if r["kind"] == "project"])
    return cat_rules, proj_rules + folder_name_rules(conn)


# Implicit path rules: a file inside a folder named exactly like a project
# (".../Orbit/notes.md" -> Orbit) is linked to it. They are sorted after
# every explicit rule, so a user rule always wins, and they match whole folder
# names only, so a short name like "Job" doesn't hit every path containing it.
FOLDER_RULE_PRIORITY = 10_000


def folder_name_rules(conn):
    rows = conn.execute(
        "SELECT id, name FROM projects ORDER BY length(name) DESC, id").fetchall()
    return [{
        "kind": "project", "implicit": True, "project_id": r["id"], "project_name": r["name"],
        "field": "path", "match_type": "regex", "priority": FOLDER_RULE_PRIORITY,
        "pattern": r"(^|/)" + re.escape(r["name"].strip()) + r"(/|$)",
    } for r in rows if len(r["name"].strip()) >= 3]


def load_assignments(conn, start_ts, end_ts):
    rows = conn.execute(
        "SELECT a.*, p.name AS project_name FROM assignments a "
        "JOIN projects p ON p.id = a.project_id "
        "WHERE a.end_ts > ? AND a.start_ts < ? ORDER BY a.start_ts",
        (start_ts, end_ts),
    ).fetchall()
    return [dict(r) for r in rows]


class AssignmentSweep:
    """Hands each event only the assignments that overlap it, for events
    visited in ascending start_ts order (how every aggregation loop reads
    them). Replaces an O(events × assignments) rescan of the full assignment
    list per event with one O(events + assignments) sweep."""

    def __init__(self, assignments):
        self._pending = assignments          # load_assignments order: by start_ts
        self._i = 0
        self._active = []

    def overlapping(self, start, end):
        while self._i < len(self._pending) and self._pending[self._i]["start_ts"] < end:
            self._active.append(self._pending[self._i])
            self._i += 1
        if any(a["end_ts"] <= start for a in self._active):
            self._active = [a for a in self._active if a["end_ts"] > start]
        return [a for a in self._active if a["start_ts"] < end]


def event_path(row) -> str:
    """File path of an event: the collector's `path` column (focused document
    or terminal cwd), else a file:// URL open in a browser. Rows from before
    the column existed simply have none."""
    try:
        path = row["path"]
    except (KeyError, IndexError):
        path = None
    if path:
        return path
    url = row["url"] or ""
    return unquote(urlparse(url).path) if url.startswith("file://") else ""


def event_fields(row) -> dict:
    """Lowercased match fields — lowered once per event, not once per rule."""
    return {
        "app": (row["app"] or "").lower(),
        "bundle": (row["bundle_id"] or "").lower(),
        "title": (row["window_title"] or "").lower(),
        "url": (row["url"] or "").lower(),
        "domain": (row["domain"] or "").lower(),
        "path": event_path(row).lower(),
    }


def _event_key(row):
    # Includes the path: the same title in two folders can be two projects.
    return (row["app"], row["bundle_id"], row["window_title"], row["url"], row["domain"],
            event_path(row))


# Verdict caches that outlive a request. Each is keyed on a fingerprint of
# the exact rule list it was built from: editing, adding, disabling or
# reordering a rule (or renaming a project) changes the fingerprint and swaps
# in an empty dict, so a stale verdict is never served. Verdicts depend only on
# the event's field tuple and the rules, never on time, so this is safe for
# history that is still growing.
_SHARED = {}
_SHARED_MAX = 250_000   # unique field tuples; ~20k today, cap just bounds memory


def shared_cache(kind, rules):
    """Persistent memo dict for categorize/rule_project/tool_and_ai `cache=`."""
    fp = tuple((r.get("id"), r.get("kind"), r.get("category"), r.get("project_id"),
                r.get("project_name"), r["field"], r["match_type"], r["pattern"],
                r.get("priority")) for r in rules)
    hit = _SHARED.get(kind)
    if hit is None or hit[0] != fp or len(hit[1]) > _SHARED_MAX:
        hit = (fp, {})
        _SHARED[kind] = hit
    return hit[1]


def categorize(row, cat_rules, cache=None) -> str:
    """`cache` (a per-request dict) memoizes the verdict per unique event
    field tuple — most events repeat the same app/title/url."""
    if row["is_afk"]:
        return "AFK"
    key = _event_key(row) if cache is not None else None
    if key is not None and key in cache:
        return cache[key]
    ev = event_fields(row)
    out = next((rule["category"] for rule in cat_rules if _matches(rule, ev)),
               "Browsing" if row["url"] else "Uncategorized")
    if key is not None:
        cache[key] = out
    return out


def rule_project(row, proj_rules, cache=None):
    """(project_id, project_name) from the first matching project rule.
    `cache` memoizes per unique event field tuple, like categorize()."""
    if row["is_afk"]:
        return (None, None)
    key = _event_key(row) if cache is not None else None
    if key is not None and key in cache:
        return cache[key]
    ev = event_fields(row)
    out = (None, None)
    for rule in proj_rules:
        if _matches(rule, ev):
            out = (rule["project_id"], rule["project_name"])
            break
    if key is not None:
        cache[key] = out
    return out


# ---------------------------------------------------------------------------
# Coding tools & agents (query-time, like categories/projects)
# ---------------------------------------------------------------------------
# Claude Code rewrites the terminal title to "<spinner> <session title>", where
# the spinner frame is a star variant (✳ ✢ ✻ ✽ ✶) or a braille dot (U+2800–U+28FF).
_CLAUDE_TITLE = re.compile(
    r"^[⠀-⣿✳✢✻✽✶✦◐◑◒◓]\s?|claude[ -]?code",
    re.IGNORECASE)

_TERMINAL_APPS = ("ghostty", "terminal", "iterm", "warp", "kitty",
                  "alacritty", "wezterm", "tabby", "hyper")

# (tool name, app-name needles). Short needles (<=4 chars) must match exactly
# so "code" doesn't swallow "Xcode" and "zed" doesn't match inside other names.
_TOOL_APPS = [
    ("Cursor", ("cursor",)),
    ("Windsurf", ("windsurf",)),
    ("Trae", ("trae",)),
    ("Antigravity", ("antigravity",)),
    ("Xcode", ("xcode",)),
    ("VS Code", ("visual studio code", "code")),
    ("Zed", ("zed",)),
    ("Devin", ("devin",)),
    ("DataGrip", ("datagrip",)),
]

# CLI agents that put their name in the terminal title.
_TERMINAL_AGENTS = [
    ("Codex CLI", re.compile(r"\bcodex\b", re.IGNORECASE)),
    ("Gemini CLI", re.compile(r"\bgemini\b", re.IGNORECASE)),
    ("Aider", re.compile(r"\baider\b", re.IGNORECASE)),
    ("OpenCode", re.compile(r"\bopencode\b", re.IGNORECASE)),
]


def coding_tool(row):
    """Best-effort mapping of an event to a coding tool / agent, or None.

    Terminal events are split by window title (Claude Code, Codex, … set it);
    a terminal with no agent title counts as plain 'Terminal (<app>)'.
    """
    if row["is_afk"]:
        return None
    app = (row["app"] or "").lower()
    bundle = (row["bundle_id"] or "").lower()
    title = row["window_title"] or ""
    url = (row["url"] or "").lower()
    domain = (row["domain"] or "").lower()

    # The Codex desktop app was renamed to "ChatGPT" (July 2026 update) but kept
    # bundle id com.openai.codex; the chat app is com.openai.chat.
    if "codex" in bundle:
        return "Codex"

    if "devin" in domain:
        return "Devin (web)"
    if "chatgpt.com" in domain and "/codex" in url:
        return "Codex (web)"
    if "claude.ai" in domain and "/code" in url:
        return "Claude Code (web)"

    for name, needles in _TOOL_APPS:
        if any(n == app or (len(n) > 4 and n in app) for n in needles):
            # CLI agents running in an editor's integrated terminal (e.g.
            # Claude Code inside VS Code) surface in the window title while
            # the terminal is focused.
            if _CLAUDE_TITLE.search(title):
                return "Claude Code"
            for agent, rx in _TERMINAL_AGENTS:
                if rx.search(title):
                    return agent
            return name

    if any(t in app for t in _TERMINAL_APPS):
        if _CLAUDE_TITLE.search(title):
            return "Claude Code"
        for name, rx in _TERMINAL_AGENTS:
            if rx.search(title):
                return name
        return f"Terminal ({row['app']})" if row["app"] else "Terminal"
    return None


# ---------------------------------------------------------------------------
# AI usage detection (query-time, like categories/projects)
# ---------------------------------------------------------------------------
# coding_tool() results that are AI assistants/agents. Plain editors and bare
# terminals are not AI — but a terminal (Ghostty/iTerm/Terminal/…) or editor
# whose title shows an agent (Claude Code, Codex, …) maps to that agent above,
# so it lands in this set.
AI_TOOLS = frozenset({
    "Claude Code", "Claude Code (web)", "Codex", "Codex (web)", "Codex CLI",
    "Gemini CLI", "Aider", "OpenCode", "Devin", "Devin (web)",
    "Cursor", "Windsurf", "Trae", "Antigravity",
})

# Built-in fallbacks, expressed as rules so is_ai() has one matching path;
# users extend the same mechanism with rules of kind='ai'.
_BUILTIN_AI_RULES = [
    {"field": "domain", "match_type": "regex", "pattern":
        r"chatgpt\.com|chat\.openai\.com|claude\.(ai|com)|perplexity\.ai|"
        r"gemini\.google\.com|aistudio\.google\.com|notebooklm\.google\.com|"
        r"grok\.com|(^|\.)x\.ai|copilot\.microsoft\.com|poe\.com|"
        r"chat\.deepseek\.com|chat\.mistral\.ai|openrouter\.ai|"
        r"v0\.dev|bolt\.new|lovable\.dev"},
    {"field": "app", "match_type": "regex", "pattern":
        r"chatgpt|claude|perplexity|grok|copilot|gemini|ollama|lm studio|"
        r"macwhisper|openwork"},
]


def load_ai_rules(conn):
    """Rules that mark time as AI-assisted: user rules of kind='ai', plus
    whatever the user filed under the 'AI Tools' category."""
    return _with_lower([dict(r) for r in conn.execute(
        "SELECT * FROM rules WHERE enabled = 1 AND "
        "(kind = 'ai' OR (kind = 'category' AND category = 'AI Tools')) "
        "ORDER BY priority, id")])


def is_ai(row, ai_rules, tool) -> bool:
    """Whether an event is AI-assisted. `tool` is the caller's precomputed
    coding_tool(row); AI agents/IDEs count, plus any matching AI rule."""
    if row["is_afk"]:
        return False
    if tool in AI_TOOLS:
        return True
    ev = event_fields(row)
    return (any(_matches(rule, ev) for rule in ai_rules)
            or any(_matches(rule, ev) for rule in _BUILTIN_AI_RULES))


def tool_and_ai(row, ai_rules, cache=None):
    """(coding_tool, is_ai) for a non-AFK event, memoized per unique event
    field tuple via `cache` (a per-request dict). Callers must only pass
    non-AFK rows — the cache key ignores is_afk."""
    key = _event_key(row) if cache is not None else None
    if key is not None:
        hit = cache.get(key)
        if hit is not None:
            return hit
    tool = coding_tool(row)
    out = (tool, is_ai(row, ai_rules, tool))
    if key is not None:
        cache[key] = out
    return out


def ai_label(row, tool) -> str:
    """Display label for AI time: agent/IDE name, else site, else app."""
    return tool if tool in AI_TOOLS else row["domain"] or row["app"] or "?"


def group_events(rows, gap_tolerance=180):
    """Merge raw events into 'visits' (Rize-style): consecutive activity on the
    same site (domain) or app becomes one block, absorbing brief interruptions.
    A visit ends when the user has been away from that site/app for more than
    gap_tolerance seconds. Reports both span (wall clock) and active seconds,
    so "on YouTube 2:10-2:45 (28m active)" is honest about interruptions."""
    open_visits, done = {}, []
    for r in rows:
        if r["is_afk"]:
            continue
        key = r["domain"] or r["app"] or "?"
        v = open_visits.get(key)
        if v is not None and r["start_ts"] - v["end_ts"] <= gap_tolerance:
            v["end_ts"] = max(v["end_ts"], r["end_ts"])
            v["active_seconds"] += r["end_ts"] - r["start_ts"]
            v["event_ids"].append(r["id"])
            t = r["window_title"]
            if t and t not in v["titles"] and len(v["titles"]) < 5:
                v["titles"].append(t)
            if r["url"] and not v["url"]:
                v["url"] = r["url"]
        else:
            if v is not None:
                done.append(open_visits.pop(key))
            open_visits[key] = {
                "app": r["app"], "domain": r["domain"],
                "url": r["url"] or "", "titles": [r["window_title"]] if r["window_title"] else [],
                "start_ts": r["start_ts"], "end_ts": r["end_ts"],
                "active_seconds": r["end_ts"] - r["start_ts"],
                "event_ids": [r["id"]],
            }
        # close out visits whose gap window has lapsed
        for k in [k for k, ov in open_visits.items()
                  if k != key and r["start_ts"] - ov["end_ts"] > gap_tolerance]:
            done.append(open_visits.pop(k))
    done.extend(open_visits.values())
    done.sort(key=lambda v: v["start_ts"])
    return done


def project_spans(row, proj_rules, assignments, start=None, end=None, rule_cache=None):
    """Split an event's [start, end] into (seconds, project_id, project_name)
    spans. Manual assignments win over rule-based matches for the portion of
    the event they overlap. `start`/`end` clip the event to a window without
    the caller having to copy the row; `rule_cache` is passed to rule_project()."""
    start = row["start_ts"] if start is None else start
    end = row["end_ts"] if end is None else end
    matched = rule_project(row, proj_rules, rule_cache)

    spans = []
    cursor = start
    for a in assignments:
        lo, hi = max(a["start_ts"], cursor), min(a["end_ts"], end)
        if hi <= lo:
            continue
        if lo > cursor:
            spans.append((lo - cursor, *matched))
        spans.append((hi - lo, a["project_id"], a["project_name"]))
        cursor = hi
    if cursor < end:
        spans.append((end - cursor, *matched))
    return spans


# ---------------------------------------------------------------------------
# Unassigned long-block review (meetings, etc.)
# ---------------------------------------------------------------------------
# Google Meet URLs are opaque (`meet.google.com/xxx-yyyy-zzz`); the useful
# name is in the window title ("Meet - Dev ops help - Microphone recording…").
# These helpers never write a project onto events — they only decide which
# completed visits are worth a quiet "assign this?" prompt.

MEETING_DOMAINS = (
    "meet.google.com", "zoom.us", "teams.microsoft.com", "teams.live.com",
)
MEETING_APPS = (
    "zoom", "microsoft teams", "facetime", "webex", "cisco webex",
)
# Overlays on a call (recorders). Absorb into an adjacent Meet; never prompt alone.
COMPANION_APPS = ("wispr flow",)
MEETING_LABELS = {
    "meet.google.com": "Google Meet",
    "zoom.us": "Zoom",
    "teams.microsoft.com": "Teams",
    "teams.live.com": "Teams",
}
_MEET_PREFIX = re.compile(r"^meet\s*[-–—:]\s*", re.I)
_MEET_CODE = re.compile(r"^[a-z]{3}-[a-z]{4}-[a-z]{3}$")
_MEET_CUT = re.compile(
    r"\s*[-–—]\s*(?:"
    r"microphone recording|camera and microphone recording|camera recording|"
    r"high memory usage|google chrome|brave(?: browser)?|safari|microsoft edge"
    r")\b",
    re.I)
DEFAULT_PROMPT_SECONDS = 60    # 1 min — skip tab-switch noise, keep real chunks
OPEN_GRACE_SECONDS = 90        # don't prompt while the visit is still live
PROMPT_CAP = 8


def visit_key(v) -> str:
    return v.get("domain") or v.get("app") or "?"


def is_meeting(v) -> bool:
    domain = (v.get("domain") or "").lower()
    app = (v.get("app") or "").lower()
    if any(d in domain for d in MEETING_DOMAINS):
        return True
    if any(a == app or a in app for a in MEETING_APPS):
        return True
    return any(
        t and (_MEET_PREFIX.match(t) or "zoom meeting" in t.lower())
        for t in (v.get("titles") or []))


def is_companion(v) -> bool:
    return (v.get("app") or "").lower() in COMPANION_APPS


def meeting_label(titles, domain="", app="") -> str:
    """Human name for a Meet/Zoom window, stripping recording/browser chrome."""
    for t in titles or []:
        if not t:
            continue
        s = t.strip()
        if _MEET_PREFIX.match(s):
            rest = _MEET_CUT.split(_MEET_PREFIX.sub("", s), maxsplit=1)[0].strip(" -")
            if rest and not _MEET_CODE.fullmatch(rest.lower()):
                return rest
        low = s.lower()
        if "zoom meeting" in low:
            name = re.sub(r"\s*[-–—]?\s*zoom meeting.*$", "", s, flags=re.I).strip(" -")
            if name:
                return name
    for d, label in MEETING_LABELS.items():
        if d in (domain or "").lower():
            return label
    return domain or app or "Meeting"


def _skipped(v, skips) -> bool:
    key = visit_key(v)
    for s in skips:
        if s["visit_key"] != key:
            continue
        if s["end_ts"] > v["start_ts"] and s["start_ts"] < v["end_ts"]:
            return True
    return False


def _as_cluster(v):
    return {
        **v,
        "event_ids": list(v.get("event_ids") or []),
        "titles": list(v.get("titles") or []),
        "_keys": [visit_key(v)],
    }


def _merge_into(prev, cur):
    prev["start_ts"] = min(prev["start_ts"], cur["start_ts"])
    prev["end_ts"] = max(prev["end_ts"], cur["end_ts"])
    prev["active_seconds"] = prev.get("active_seconds", 0) + cur.get("active_seconds", 0)
    prev["unassigned_seconds"] = (
        prev.get("unassigned_seconds", 0) + cur.get("unassigned_seconds", 0))
    prev["event_ids"].extend(cur.get("event_ids") or [])
    key = visit_key(cur)
    if key not in prev["_keys"]:
        prev["_keys"].append(key)
    for t in cur.get("titles") or []:
        if t and t not in prev["titles"] and len(prev["titles"]) < 6:
            prev["titles"].append(t)
    vd = cur.get("domain") or ""
    if any(d in vd for d in MEETING_DOMAINS):
        prev["domain"] = cur.get("domain")
        prev["app"] = cur.get("app") or prev.get("app")


def _coalesce_meetings(meetings, companions=(), gap=180):
    """Stitch Meet fragments, then fold recorder overlays into the same call."""
    clusters = []
    for v in sorted(meetings, key=lambda x: x["start_ts"]):
        cur = _as_cluster(v)
        if clusters and cur["start_ts"] - clusters[-1]["end_ts"] <= gap:
            _merge_into(clusters[-1], cur)
        else:
            clusters.append(cur)
    for c in companions:
        for m in clusters:
            if c["start_ts"] - m["end_ts"] <= gap and m["start_ts"] - c["end_ts"] <= gap:
                _merge_into(m, _as_cluster(c))
                break
    return clusters


def unassigned_prompt_blocks(visits, unassigned_by_eid, skips, now,
                             min_seconds=DEFAULT_PROMPT_SECONDS,
                             open_grace=OPEN_GRACE_SECONDS, cap=PROMPT_CAP):
    """Completed visits with enough unassigned time to be worth a reminder.

    `unassigned_by_eid` maps event id → seconds of that event with no project.
    Skips (user said 'not a project') hide overlapping same-key visits.
    Still-open visits are omitted so a live meeting isn't interrupted.
    Recorder apps (Wispr Flow) attach to an adjacent Meet and are never
    prompted on their own.
    """
    min_seconds = max(30, min_seconds)   # never nag about <30s noise
    raw = []
    for v in visits:
        if v["end_ts"] > now - open_grace:
            continue
        if _skipped(v, skips):
            continue
        u = sum(unassigned_by_eid.get(eid, 0) for eid in v.get("event_ids") or [])
        if u <= 0:
            continue
        raw.append({**v, "unassigned_seconds": u, "meeting": is_meeting(v),
                    "companion": is_companion(v)})
    meetings = _coalesce_meetings(
        [c for c in raw if c["meeting"]],
        [c for c in raw if c["companion"] and not c["meeting"]])
    others = [c for c in raw if not c["meeting"] and not c["companion"]]
    blocks = [b for b in meetings + others if b["unassigned_seconds"] >= min_seconds]
    blocks.sort(key=lambda x: (not x.get("meeting"), -x["unassigned_seconds"]))
    out = []
    for b in blocks[:cap]:
        keys = b.get("_keys") or [visit_key(b)]
        label = (meeting_label(b.get("titles"), b.get("domain") or "", b.get("app") or "")
                 if b["meeting"]
                 else ((b.get("titles") or [None])[0] or visit_key(b)))
        source = MEETING_LABELS.get(b.get("domain") or "") or visit_key(b)
        out.append({
            "start_ts": b["start_ts"], "end_ts": b["end_ts"],
            "seconds": round(b["unassigned_seconds"]),
            "meeting": bool(b["meeting"]),
            "source": source, "label": label,
            "titles": (b.get("titles") or [])[:3],
            "keys": keys, "event_ids": b.get("event_ids") or [],
        })
    return out
