"""First-run onboarding wizard:  tt setup   (or  tt setup --yes  for defaults).

Writes config.json (chmod 600), creates starter projects + auto-match rules,
and walks through the macOS permissions the collector needs. Safe to re-run:
existing values are offered as defaults and nothing is deleted.
"""
import argparse
import json
import os
import subprocess
import sys

from . import ai_categorize
from . import db

B, D, G, C, Y, X = "\033[1m", "\033[2m", "\033[32m", "\033[36m", "\033[33m", "\033[0m"
if not sys.stdout.isatty():
    B = D = G = C = Y = X = ""

# `curl ... | bash` leaves stdin attached to the pipe, so prompts read the terminal.
try:
    TTY = open("/dev/tty")
except OSError:
    TTY = None
ASSUME_YES = False


def ask(q, default=""):
    shown = f" {D}[{default}]{X}" if default else ""
    if ASSUME_YES or TTY is None:
        print(f"{C}?{X} {q}{shown} {D}→ {default or 'skip'}{X}")
        return default
    sys.stdout.write(f"{C}?{X} {q}{shown} ")
    sys.stdout.flush()
    ans = TTY.readline().strip()
    return ans or default


def confirm(q, default=True):
    if ASSUME_YES or TTY is None:
        print(f"{C}?{X} {q} {D}→ {'yes' if default else 'no'}{X}")
        return default
    ans = ask(q + (f" {D}(Y/n){X}" if default else f" {D}(y/N){X}"))
    return default if not ans else ans.lower().startswith("y")


def secret(q, current):
    hint = "keep current" if current else "skip"
    if ASSUME_YES or TTY is None:
        print(f"{C}?{X} {q} {D}→ {hint}{X}")
        return current
    import termios
    sys.stdout.write(f"{C}?{X} {q} {D}(input hidden, Enter to {hint}){X} ")
    sys.stdout.flush()
    fd = TTY.fileno()
    old = termios.tcgetattr(fd)
    new = termios.tcgetattr(fd)
    new[3] &= ~termios.ECHO
    try:
        termios.tcsetattr(fd, termios.TCSADRAIN, new)
        val = TTY.readline().strip()
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
    print("•" * min(len(val), 12) if val else "")
    return val or current


def step(n, title):
    print(f"\n{B}{n}{title}{X}")


def section_tracking(cfg, n=""):
    step(n, "Tracking")
    print(f"  {D}After this many minutes with no keyboard/mouse input you're marked away (AFK).")
    print(f"  Playing video counts as present. Raise it if you read long documents.{X}")
    try:
        cfg["afk_idle_minutes"] = max(1, int(ask("Idle minutes before AFK", str(cfg.get("afk_idle_minutes", 5)))))
    except ValueError:
        cfg["afk_idle_minutes"] = 5


def section_projects(conn, n=""):
    step(n, "Projects")
    have = [r[0] for r in conn.execute("SELECT name FROM projects WHERE archived=0 ORDER BY name")]
    if have:
        print(f"  {D}You already have: {', '.join(have)}{X}")
    print(f"  {D}Name what you work on. Add a keyword or folder and matching windows,")
    print(f"  tabs and terminals are filed there automatically (you can refine later).{X}")
    while True:
        name = ask("Project name (Enter to finish)")
        if not name:
            break
        hint = ask(f"  Keyword or folder for {B}{name}{X} (e.g. acme or ~/code/acme)")
        try:
            pid = conn.execute("INSERT INTO projects (name, kind) VALUES (?, 'project')", (name,)).lastrowid
        except Exception:
            print(f"  {Y}'{name}' already exists — skipped{X}")
            continue
        if hint:
            if "/" in hint:
                field, pattern = "path", os.path.expanduser(hint).rstrip("/")
            else:
                field, pattern = "title", hint
            conn.execute("INSERT INTO rules (kind, project_id, field, match_type, pattern, priority)"
                         " VALUES ('project', ?, ?, 'contains', ?, 50)", (pid, field, pattern))
        conn.commit()
        print(f"  {G}✓{X} {name}")


def section_ai(cfg, n=""):
    step(n, "AI (optional)")
    print(f"  {D}Used to auto-file unassigned time into projects, suggest new projects, write")
    print(f"  insights in email briefs and answer questions in the Ask tab. The model only")
    print(f"  sees summaries / visit titles you choose to send — never runs in the background.{X}")
    print("    1) Claude (Anthropic)   2) Gemini   3) Ollama — fully local   4) skip / keep current")
    cur = cfg.get("ai_model", "")
    default = "1" if cur.startswith("claude") and cfg.get("anthropic_api_key") else \
              "2" if cur.startswith("gemini") and cfg.get("gemini_api_key") else \
              "3" if cur.startswith("ollama/") else "4"
    choice = ask("Choose", default)
    if choice == "1":
        cfg["anthropic_api_key"] = secret("Anthropic API key (console.anthropic.com)", cfg.get("anthropic_api_key", ""))
        models = [m["id"] for m in ai_categorize.MODELS if m["provider"] == "anthropic"]
        pick = ask(f"Model ({' / '.join(models)})", cur if cur in models else ai_categorize.DEFAULT_MODEL)
        cfg["ai_model"] = pick if pick in models else ai_categorize.DEFAULT_MODEL
    elif choice == "2":
        cfg["gemini_api_key"] = secret("Gemini API key (aistudio.google.com)", cfg.get("gemini_api_key", ""))
        cfg["ai_model"] = cur if cur.startswith("gemini") else "gemini-2.5-flash"
    elif choice == "3":
        cfg["ai_model"] = "ollama/llama3.2:3b"
        print(f"  {D}Make sure Ollama is running:  ollama pull llama3.2:3b{X}")


def section_email(cfg, n="", force=False):
    step(n, "Email briefs via Resend (optional)")
    print(f"  {D}Daily/weekly/monthly summaries with charts and AI insights. Create a free key at")
    print(f"  resend.com/api-keys. On the free tier, without a verified domain, Resend only")
    print(f"  delivers to your own account's email and you send from onboarding@resend.dev.{X}")
    if not (force or confirm("Set up email briefs?", bool(cfg.get("resend_api_key")))):
        return
    cfg["resend_api_key"] = secret("Resend API key (re_…)", cfg.get("resend_api_key", ""))
    cfg["email_to"] = ask("Send briefs to", cfg.get("email_to", ""))
    cfg["email_from"] = ask("Send from (a verified Resend sender)",
                            cfg.get("email_from", "Time Tracker <onboarding@resend.dev>"))
    cfg["weekly_email"] = confirm("Email me a weekly brief every Monday 8am?", cfg.get("weekly_email", not ASSUME_YES))
    if cfg.get("resend_api_key") and cfg.get("email_to") and confirm("Send a test email now?", not ASSUME_YES):
        ai_categorize.save_config(cfg)
        test_email()


def test_email():
    from . import report
    try:
        report.send_email("⏱ Time Tracker — test email",
                          "<p style='font-family:sans-serif'>✅ Your Local Time Tracker can send email. "
                          "Briefs will arrive here.</p>")
        print(f"  {G}✓{X} test email sent to {ai_categorize.load_config().get('email_to')}")
        return True
    except Exception as e:
        print(f"  {Y}✗ test email failed: {e}{X}")
        print(f"  {D}Check the key and that the sender is verified in Resend (or use onboarding@resend.dev).{X}")
        return False


def section_permissions(n=""):
    step(n, "macOS permissions")
    py = os.path.realpath(sys.executable)
    print(f"""  {D}The collector reads the {X}frontmost app{D} (no permission needed), {X}window titles{D}
  (Accessibility) and {X}browser tab URLs{D} (Automation — macOS asks once per browser
  the first time you use it; click OK).

  Grant Accessibility to this Python binary:{X}
      {C}{py}{X}
  {D}System Settings → Privacy & Security → Accessibility → + → ⌘⇧G → paste the path.{X}""")
    if confirm("Open Accessibility settings now?", not ASSUME_YES):
        subprocess.run(["open", "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"])
        subprocess.run(["pbcopy"], input=py.encode())
        print(f"  {D}(path copied to the clipboard){X}")


SECTIONS = ("tracking", "projects", "ai", "email", "permissions")


def main():
    global ASSUME_YES
    ap = argparse.ArgumentParser(description="Local Time Tracker onboarding")
    ap.add_argument("--yes", "-y", action="store_true", help="accept defaults, no prompts")
    ap.add_argument("--no-start", action="store_true", help="don't start the agents afterwards")
    ap.add_argument("--only", choices=SECTIONS, help="run a single section (used by tt config)")
    ap.add_argument("--test-email", action="store_true", help="send a test email and exit")
    args = ap.parse_args()
    ASSUME_YES = args.yes

    if args.test_email:
        sys.exit(0 if test_email() else 1)

    conn = db.init_db()
    cfg = ai_categorize.load_config()

    if args.only:
        {"tracking": lambda: section_tracking(cfg),
         "projects": lambda: section_projects(conn),
         "ai": lambda: section_ai(cfg),
         "email": lambda: section_email(cfg, force=True),
         "permissions": section_permissions}[args.only]()
        ai_categorize.save_config(cfg)
        print(f"\n  {G}✓{X} saved {db.CONFIG_PATH} {D}— changes apply live, no restart needed{X}")
        return

    print(f"""
{B}  ⏱  Local Time Tracker{X} {D}— setup{X}
  Everything you track stays in {C}{db.DATA_DIR}{X} on this Mac.
  Every step is optional — press Enter to accept the default.""")
    section_tracking(cfg, "1. ")
    section_projects(conn, "2. ")
    section_ai(cfg, "3. ")
    section_email(cfg, "4. ")
    ai_categorize.save_config(cfg)
    print(f"\n  {G}✓{X} saved {db.CONFIG_PATH} {D}(chmod 600){X}")
    section_permissions("5. ")

    port = os.environ.get("TT_PORT", "8321")
    if args.no_start:
        print(f"\n{G}{B}  Configured.{X} Start tracking any time with {B}tt start{X}.\n")
        return
    print(f"""
{G}{B}  All set.{X} Tracking runs in the background and starts at login.
  Dashboard  {C}http://127.0.0.1:{port}{X}   ·   {B}tt open{X}
  Change settings any time with {B}tt config{X} (or in the dashboard → Rules & Settings).
""")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n  setup cancelled — run tt setup any time")
        sys.exit(130)
