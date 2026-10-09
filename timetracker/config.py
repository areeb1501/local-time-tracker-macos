"""`tt config` — view and change settings from the terminal.

    tt config                       show every setting (secrets masked)
    tt config get KEY
    tt config set KEY VALUE         e.g. tt config set afk_idle_minutes 8
    tt config unset KEY
    tt config email | ai | tracking | projects | permissions   guided setup for one area
    tt config test-email            send a test email through Resend
    tt config edit                  open config.json in $EDITOR

The dashboard and collector re-read config.json live, so changes apply without a restart.
"""
import json
import re
import os
import subprocess
import sys

from . import ai_categorize
from . import db

B, D, G, Y, X = "\033[1m", "\033[2m", "\033[32m", "\033[33m", "\033[0m"
if not sys.stdout.isatty():
    B = D = G = Y = X = ""

# key: (type, default, group, description)
SETTINGS = {
    "afk_idle_minutes":          (float, 5,     "Tracking", "idle minutes before you're marked away"),
    "unassigned_prompt_minutes": (float, 1,     "Tracking", "min. block length for the 'assign this?' reminder"),
    "distraction_categories":    (list,  ["Entertainment"], "Tracking", "categories that count as distractions (comma-separated)"),
    "ai_model":                  (str,   ai_categorize.DEFAULT_MODEL, "AI", "which model (tt config ai to choose)"),
    "anthropic_api_key":         ("secret", "", "AI", "Claude — console.anthropic.com"),
    "gemini_api_key":            ("secret", "", "AI", "Gemini — aistudio.google.com"),
    "resend_api_key":            ("secret", "", "Email", "Resend — resend.com/api-keys"),
    "email_to":                  (str,   "",    "Email", "where briefs are sent"),
    "email_from":                (str,   "Time Tracker <onboarding@resend.dev>", "Email", "verified Resend sender"),
    "weekly_email":              (bool,  False, "Email", "weekly brief every Monday 8am"),
}


def mask(v):
    v = str(v or "")
    return "" if not v else (v[:3] + "…" + v[-4:] if len(v) > 10 else "••••")


def coerce(key, raw):
    typ = SETTINGS.get(key, (str,))[0]
    if typ is bool:
        if raw.lower() in ("1", "true", "yes", "on", "y"):
            return True
        if raw.lower() in ("0", "false", "no", "off", "n"):
            return False
        raise ValueError("expected true/false")
    if typ is float:
        v = float(raw)
        return int(v) if v.is_integer() else v
    if typ is list:
        return [x.strip() for x in raw.split(",") if x.strip()]
    if key == "ai_model" and raw not in {m["id"] for m in ai_categorize.MODELS}:
        raise ValueError("unknown model — one of: " + " | ".join(m["id"] for m in ai_categorize.MODELS))
    return raw


def show(cfg):
    print(f"{B}Settings{X}  {D}{db.CONFIG_PATH}{X}")
    group = None
    for key, (typ, default, grp, desc) in SETTINGS.items():
        if grp != group:
            group = grp
            print(f"\n  {B}{grp}{X}")
        set_ = key in cfg
        v = cfg.get(key, default)
        if typ == "secret":
            shown = mask(v) or f"{D}not set{X}"
        elif isinstance(v, list):
            shown = ", ".join(v)
        else:
            shown = json.dumps(v) if isinstance(v, bool) else str(v)
        if not shown:
            shown = f"{D}not set{X}"
        elif not set_ and typ != "secret":
            shown = f"{shown} {D}(default){X}"
        vis = len(re.sub(r"\x1b\[[0-9;]*m", "", shown))
        print(f"  {key:<27} {shown}{' ' * max(1, 38 - vis)}{D}{desc}{X}")
    extra = [k for k in cfg if k not in SETTINGS]
    if extra:
        print(f"\n  {B}Other{X}")
        for k in extra:
            print(f"  {k:<27} {cfg[k]}")
    print(f"\n  {D}tt config set KEY VALUE · tt config email | ai | tracking · tt config test-email{X}")


def main(argv):
    cfg = ai_categorize.load_config()
    cmd = argv[0] if argv else "list"
    if cmd in ("list", "show", "ls"):
        show(cfg)
    elif cmd == "get" and len(argv) == 2:
        v = cfg.get(argv[1], SETTINGS.get(argv[1], (None, None))[1])
        print(mask(v) if SETTINGS.get(argv[1], ("",))[0] == "secret" else
              (", ".join(v) if isinstance(v, list) else json.dumps(v) if isinstance(v, bool) else v))
    elif cmd == "set" and len(argv) >= 3:
        key, raw = argv[1], " ".join(argv[2:])
        if key not in SETTINGS:
            print(f"{Y}note:{X} '{key}' isn't a known setting — saving it anyway")
        try:
            cfg[key] = coerce(key, raw)
        except ValueError as e:
            sys.exit(f"invalid value for {key}: {e}")
        ai_categorize.save_config(cfg)
        v = cfg[key]
        shown = mask(v) if SETTINGS.get(key, ("",))[0] == "secret" else \
            ", ".join(v) if isinstance(v, list) else json.dumps(v) if isinstance(v, bool) else v
        print(f"{G}✓{X} {key} = {shown}")
    elif cmd in ("unset", "rm", "delete") and len(argv) == 2:
        cfg.pop(argv[1], None)
        ai_categorize.save_config(cfg)
        print(f"{G}✓{X} {argv[1]} cleared")
    elif cmd == "edit":
        if not os.path.exists(db.CONFIG_PATH):
            ai_categorize.save_config(cfg)
        subprocess.call([os.environ.get("EDITOR", "nano"), db.CONFIG_PATH])
    elif cmd == "path":
        print(db.CONFIG_PATH)
    else:
        print(__doc__.strip())
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
