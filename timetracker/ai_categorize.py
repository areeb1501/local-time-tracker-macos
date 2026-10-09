"""AI categorizer: propose project assignments for unclassified time.

Reads grouped visits for a period, asks an LLM (Claude / Gemini / local
Ollama, per config) which project each visit belongs to, and returns
proposals for the user to review. Nothing is written until the user approves.
"""
import json
import os
import urllib.request

from . import classify
from . import db

CONFIG_PATH = db.CONFIG_PATH
DEFAULT_MODEL = "claude-haiku-4-5"

MODELS = [
    {"id": DEFAULT_MODEL, "label": "Claude Haiku 4.5 — fast & cheap", "provider": "anthropic"},
    {"id": "claude-sonnet-5", "label": "Claude Sonnet 5 — in-depth", "provider": "anthropic"},
    {"id": "gemini-2.5-flash", "label": "Gemini 2.5 Flash — fast & cheap", "provider": "gemini"},
    {"id": "gemini-2.5-pro", "label": "Gemini 2.5 Pro — in-depth", "provider": "gemini"},
    {"id": "ollama/llama3.2:3b", "label": "Llama 3.2 3B — local & free", "provider": "ollama"},
]

SCHEMA = {
    "type": "object",
    "properties": {
        "assignments": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "visit_index": {"type": "integer"},
                    "project_id": {"type": "integer"},
                    "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
                    "reason": {"type": "string"},
                },
                "required": ["visit_index", "project_id", "confidence", "reason"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["assignments"],
    "additionalProperties": False,
}


def load_config():
    try:
        return json.load(open(CONFIG_PATH))
    except Exception:
        return {}


def save_config(cfg):
    json.dump(cfg, open(CONFIG_PATH, "w"), indent=2)
    os.chmod(CONFIG_PATH, 0o600)


def model_availability():
    cfg = load_config()
    ollama_up = False
    try:
        with urllib.request.urlopen("http://127.0.0.1:11434/api/tags", timeout=2) as r:
            ollama_up = any(m["name"].startswith("llama3.2")
                            for m in json.load(r).get("models", []))
    except Exception:
        pass
    out = []
    for m in MODELS:
        available = (
            (m["provider"] == "anthropic" and bool(cfg.get("anthropic_api_key")))
            or (m["provider"] == "gemini" and bool(cfg.get("gemini_api_key")))
            or (m["provider"] == "ollama" and ollama_up)
        )
        out.append({**m, "available": available})
    return {"models": out, "selected": cfg.get("ai_model", DEFAULT_MODEL)}


def _resolve_model(model_id):
    return model_id or load_config().get("ai_model", DEFAULT_MODEL)


# ---------------------------------------------------------------------------
# LLM backends
# ---------------------------------------------------------------------------
def _call_anthropic(model, system, user, cfg, schema):
    import anthropic
    client = anthropic.Anthropic(api_key=cfg["anthropic_api_key"])
    resp = client.messages.create(
        model=model,
        max_tokens=8000,
        system=system,
        messages=[{"role": "user", "content": user}],
        output_config={"format": {"type": "json_schema", "schema": schema}},
    )
    if resp.stop_reason == "refusal":
        raise RuntimeError("The model declined this request")
    text = next(b.text for b in resp.content if b.type == "text")
    return json.loads(text)


def _call_gemini(model, system, user, cfg, schema):
    body = {
        "system_instruction": {"parts": [{"text": system}]},
        "contents": [{"parts": [{"text": user}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseSchema": schema,
            "temperature": 0,
        },
    }
    req = urllib.request.Request(
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json",
                 "x-goog-api-key": cfg["gemini_api_key"]},
    )
    with urllib.request.urlopen(req, timeout=120) as r:
        data = json.load(r)
    return json.loads(data["candidates"][0]["content"]["parts"][0]["text"])


def _call_ollama(model, system, user, cfg, schema):
    body = {
        "model": model,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
        "stream": False,
        "format": schema,
        "options": {"temperature": 0},
    }
    req = urllib.request.Request(
        "http://127.0.0.1:11434/api/chat",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=300) as r:
        data = json.load(r)
    return json.loads(data["message"]["content"])


def _chat_anthropic(model, system, messages, cfg):
    import anthropic
    client = anthropic.Anthropic(api_key=cfg["anthropic_api_key"])
    resp = client.messages.create(
        model=model, max_tokens=2000, system=system, messages=messages)
    if resp.stop_reason == "refusal":
        raise RuntimeError("The model declined this request")
    return "".join(b.text for b in resp.content if b.type == "text")


def _chat_gemini(model, system, messages, cfg):
    contents = [{"role": "model" if m["role"] == "assistant" else "user",
                 "parts": [{"text": m["content"]}]} for m in messages]
    body = {"system_instruction": {"parts": [{"text": system}]},
            "contents": contents}
    req = urllib.request.Request(
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json",
                 "x-goog-api-key": cfg["gemini_api_key"]},
    )
    with urllib.request.urlopen(req, timeout=120) as r:
        data = json.load(r)
    return data["candidates"][0]["content"]["parts"][0]["text"]


def _chat_ollama(model, system, messages, cfg):
    body = {"model": model, "stream": False,
            "messages": [{"role": "system", "content": system}, *messages]}
    req = urllib.request.Request(
        "http://127.0.0.1:11434/api/chat",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=300) as r:
        data = json.load(r)
    return data["message"]["content"]


def chat_llm(model_id, system, messages):
    """Free-text multi-turn chat (no JSON schema). `messages` is a list of
    {'role': 'user'|'assistant', 'content': str}; returns the reply text."""
    model_id = _resolve_model(model_id)
    cfg = load_config()
    provider = next((m["provider"] for m in MODELS if m["id"] == model_id), None)
    if provider == "anthropic":
        if not cfg.get("anthropic_api_key"):
            raise RuntimeError("No Anthropic API key saved - add one in Settings")
        return _chat_anthropic(model_id, system, messages, cfg)
    if provider == "gemini":
        if not cfg.get("gemini_api_key"):
            raise RuntimeError("No Gemini API key saved - add one in Settings")
        return _chat_gemini(model_id, system, messages, cfg)
    if provider == "ollama":
        return _chat_ollama(model_id.split("/", 1)[1], system, messages, cfg)
    raise RuntimeError(f"Unknown model: {model_id}")


def call_llm(model_id, system, user, schema=None):
    schema = schema or SCHEMA
    cfg = load_config()
    provider = next((m["provider"] for m in MODELS if m["id"] == model_id), None)
    if provider == "anthropic":
        if not cfg.get("anthropic_api_key"):
            raise RuntimeError("No Anthropic API key saved - add one in Settings")
        return _call_anthropic(model_id, system, user, cfg, schema)
    if provider == "gemini":
        if not cfg.get("gemini_api_key"):
            raise RuntimeError("No Gemini API key saved - add one in Settings")
        return _call_gemini(model_id, system, user, cfg, schema)
    if provider == "ollama":
        return _call_ollama(model_id.split("/", 1)[1], system, user, cfg, schema)
    raise RuntimeError(f"Unknown model: {model_id}")


# ---------------------------------------------------------------------------
# Propose
# ---------------------------------------------------------------------------
SYSTEM_PROMPT = """You classify a person's computer activity into their projects.
You are given (1) their projects, with existing matching-rule patterns as hints
about what belongs to each, and (2) visits: blocks of time on one app or website,
with sample window titles.

Assign a visit to a project ONLY when the evidence (app, domain, titles) clearly
relates to that project. Generic activity (email, chat, search engines, social
media, OS utilities) stays unassigned unless titles clearly tie it to a project.
It is much better to leave a visit out than to guess wrong. Return an assignments
array; omit visits that belong to no project."""


def propose(start: float, end: float, model_id: str | None = None):
    model_id = _resolve_model(model_id)
    c = db.init_db()
    _, proj_rules = classify.load_rules(c)
    projects = [dict(r) for r in c.execute("SELECT * FROM projects ORDER BY id")]
    if not projects:
        raise RuntimeError("Create some projects first - the AI assigns time to them")

    visits = _unattributed_visits(c, start, end, min_seconds=60, cap=80)
    visits.sort(key=lambda v: v["start_ts"])
    if not visits:
        return {"model": model_id, "proposals": [], "visits_considered": 0}

    hints = {}
    for r in proj_rules:
        if r.get("implicit"):
            continue
        hints.setdefault(r["project_id"], []).append(f'{r["field"]} ~ {r["pattern"]}')
    proj_lines = [
        {"id": p["id"], "name": p["name"], "kind": p["kind"],
         "parent": next((q["name"] for q in projects if q["id"] == p.get("parent_id")), None),
         "known_patterns": hints.get(p["id"], [])}
        for p in projects
    ]
    user_msg = (f"PROJECTS:\n{json.dumps(proj_lines, indent=1)}\n\n"
                f"VISITS:\n{json.dumps(_visit_lines(visits), indent=1)}")

    result = call_llm(model_id, SYSTEM_PROMPT, user_msg)

    by_project = {}
    valid_ids = {p["id"] for p in projects}
    for a in result.get("assignments", []):
        i, pid = a.get("visit_index"), a.get("project_id")
        if not isinstance(i, int) or not (0 <= i < len(visits)) or pid not in valid_ids:
            continue
        v = visits[i]
        entry = by_project.setdefault(pid, {
            "project_id": pid,
            "project_name": next(p["name"] for p in projects if p["id"] == pid),
            "total_seconds": 0, "items": []})
        entry["total_seconds"] += round(v["active_seconds"])
        entry["items"].append({
            "start": v["start_ts"], "end": v["end_ts"],
            "app": v["app"], "domain": v["domain"],
            "title": v["titles"][0] if v["titles"] else "",
            "seconds": round(v["active_seconds"]),
            "confidence": a.get("confidence", "medium"),
            "reason": a.get("reason", ""),
            "event_ids": v["event_ids"],
        })
    proposals = sorted(by_project.values(), key=lambda p: -p["total_seconds"])
    return {"model": model_id, "proposals": proposals, "visits_considered": len(visits)}


# ---------------------------------------------------------------------------
# Suggest new projects
# ---------------------------------------------------------------------------
SUGGEST_SCHEMA = {
    "type": "object",
    "properties": {
        "suggestions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "reason": {"type": "string"},
                    "visit_indices": {"type": "array", "items": {"type": "integer"}},
                    "rules": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "field": {"type": "string",
                                          "enum": ["app", "title", "url", "domain"]},
                                "match_type": {"type": "string",
                                               "enum": ["contains", "prefix", "equals", "regex"]},
                                "pattern": {"type": "string"},
                            },
                            "required": ["field", "match_type", "pattern"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": ["name", "reason", "visit_indices", "rules"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["suggestions"],
    "additionalProperties": False,
}

SUGGEST_PROMPT = """You review a person's recent computer activity and suggest NEW projects
they seem to be working on but haven't set up yet.

You get (1) their existing projects — never suggest a duplicate or near-duplicate
of these — and (2) visits: blocks of time on one app or site with sample window
titles, none of which is credited to any existing project.

Suggest a project only when several visits form a coherent recurring theme with
meaningful time (roughly 30+ minutes total). Give it a short, human name (e.g. a
repo, organisation, or course name found in the titles), cite the visit indices that
support it, and propose 1-3 matching rules (prefer 'title' or 'domain' contains
patterns that would reliably catch this work in future). Suggest at most 5
projects; an empty list is a fine answer."""


def _unattributed_visits(c, start, end, min_seconds, cap):
    """Grouped visits in [start, end] that no rule or assignment credits to
    any project, largest first, capped."""
    _, proj_rules = classify.load_rules(c)
    assignments = classify.load_assignments(c, start, end)
    rows = c.execute(
        "SELECT * FROM events WHERE end_ts > ? AND start_ts < ? AND is_afk = 0 ORDER BY start_ts",
        (start, end)).fetchall()
    unattributed = [r for r in rows
                    if all(pid is None for _, pid, _ in
                           classify.project_spans(dict(r), proj_rules, assignments))]
    visits = [v for v in classify.group_events(unattributed)
              if v["active_seconds"] >= min_seconds]
    visits.sort(key=lambda v: -v["active_seconds"])
    return visits[:cap]


def _visit_lines(visits):
    return [{"index": i, "app": v["app"], "domain": v["domain"],
             "minutes": round(v["active_seconds"] / 60),
             "titles": v["titles"][:4]}
            for i, v in enumerate(visits)]


def _pack_suggestions(result, visits, skip_names=()):
    """Validate the model's suggestions and attach visit details/event ids."""
    skip = {n.strip().lower() for n in skip_names}
    suggestions = []
    for s in result.get("suggestions", []):
        name = (s.get("name") or "").strip()
        idxs = [i for i in s.get("visit_indices", [])
                if isinstance(i, int) and 0 <= i < len(visits)]
        if not name or name.lower() in skip or not idxs:
            continue
        items, event_ids, total = [], [], 0
        for i in idxs:
            v = visits[i]
            total += round(v["active_seconds"])
            event_ids.extend(v["event_ids"])
            items.append({
                "start": v["start_ts"], "end": v["end_ts"],
                "app": v["app"], "domain": v["domain"],
                "title": v["titles"][0] if v["titles"] else "",
                "titles": v["titles"],
                "seconds": round(v["active_seconds"]),
            })
        items.sort(key=lambda x: -x["seconds"])
        suggestions.append({
            "name": name,
            "reason": s.get("reason", ""),
            "rules": s.get("rules", [])[:3],
            "total_seconds": total,
            "items": items,
            "event_ids": event_ids,
        })
    suggestions.sort(key=lambda s: -s["total_seconds"])
    return suggestions


def _suggest(prompt, start, end, model_id, *, preamble="", min_seconds, cap,
             skip_existing=False, limit=None):
    """Shared core of suggest_projects / find_project: gather unattributed
    visits, ask the model, validate and pack its suggestions."""
    model_id = _resolve_model(model_id)
    c = db.init_db()
    projects = [dict(r) for r in c.execute("SELECT * FROM projects ORDER BY id")]
    visits = _unattributed_visits(c, start, end, min_seconds, cap)
    if not visits:
        return {"model": model_id, "suggestions": [], "visits_considered": 0}

    proj_lines = [{"name": p["name"], "kind": p["kind"]} for p in projects]
    user_msg = (preamble
                + f"EXISTING PROJECTS:\n{json.dumps(proj_lines, indent=1)}\n\n"
                + f"UNATTRIBUTED VISITS:\n{json.dumps(_visit_lines(visits), indent=1)}")

    result = call_llm(model_id, prompt, user_msg, SUGGEST_SCHEMA)
    skip = [p["name"] for p in projects] if skip_existing else ()
    suggestions = _pack_suggestions(result, visits, skip)[:limit]
    return {"model": model_id, "suggestions": suggestions, "visits_considered": len(visits)}


def suggest_projects(start: float, end: float, model_id: str | None = None):
    return _suggest(SUGGEST_PROMPT, start, end, model_id,
                    min_seconds=120, cap=120, skip_existing=True)


# ---------------------------------------------------------------------------
# Find a described project
# ---------------------------------------------------------------------------
FIND_PROMPT = """The user describes a project they want to set up in their time tracker.
You get (1) their description, (2) their existing projects (context only — pick
a name that doesn't collide with them), and (3) recent visits: blocks of time
on one app or website with sample window titles, none of which is credited to
any existing project.

Select every visit that clearly belongs to the described project — err on the
side of precision, leaving a visit out is better than guessing wrong. Propose a
short human project name based on the user's own wording, cite the matching
visit indices, and 1-3 matching rules (prefer 'title', 'domain' or 'url'
contains-patterns that would reliably catch this work in the future).
Return exactly one suggestion, or an empty list if nothing matches."""


def find_project(description: str, start: float, end: float,
                 model_id: str | None = None):
    if not description.strip():
        raise RuntimeError("Describe the project first")
    return _suggest(
        FIND_PROMPT, start, end, model_id,
        preamble=f"PROJECT DESCRIPTION (from the user):\n{description.strip()}\n\n",
        min_seconds=60, cap=150, limit=1)
