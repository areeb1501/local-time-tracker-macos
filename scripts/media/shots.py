"""Capture README screenshots from the demo dashboard (TT_HOME=/tmp/tt-demo on :8399)."""
import sys, time
from pathlib import Path
from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8399"
OUT = Path(__file__).resolve().parents[2] / "docs" / "media"
# Neutralise example strings in the UI that aren't demo-appropriate.
SUBS = []   # (old, new) text swaps applied to index.html before rendering, if ever needed

def patch(route):
    r = route.fetch(); body = r.text()
    for a, b in SUBS: body = body.replace(a, b)
    route.fulfill(response=r, body=body)

def go(page, hash_, wait=1800):
    page.goto(f"{BASE}/#{hash_}")
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(wait)

if __name__ == "__main__":
    with sync_playwright() as p:
        b = p.chromium.launch()
        ctx = b.new_context(viewport={"width": 1440, "height": 900}, device_scale_factor=2)
        ctx.route(f"{BASE}/", patch)
        page = ctx.new_page()
        shots = [("overview", "/overview?range=7d"), ("projects", "/projects?range=7d"),
                 ("project-detail", "/projects/1?range=30d"), ("sessions", "/sessions?range=7d"),
                 ("activity", "/activity?range=today"), ("settings", "/settings"), ("ask", "/ask/1")]
        for name, h in shots:
            go(page, h)
            if name == "overview":   # hero shot: hide the "needs a project" review card
                page.evaluate("""() => document.querySelectorAll('.card').forEach(c => {
                    if (/needs a project/i.test(c.querySelector('h2')?.textContent || c.textContent.slice(0,40))) c.style.display='none'})""")
                page.wait_for_timeout(300)
            page.screenshot(path=str(OUT / f"{name}.png"))
            print("saved", name)
        go(page, "/overview?range=7d")
        card = page.locator(".card", has=page.locator("h2", has_text="AI usage")).first
        if card.count():
            card.scroll_into_view_if_needed(); page.wait_for_timeout(500)
            card.screenshot(path=str(OUT / "ai-usage.png")); print("saved ai-usage")
        else:
            print("no AI usage card")
        # email brief
        ep = ctx.new_page(); ep.set_viewport_size({"width": 760, "height": 900})
        ep.goto("file:///tmp/tt-brief.html"); ep.wait_for_timeout(500)
        ep.screenshot(path=str(OUT / "email-brief.png"), full_page=True); print("saved email-brief")
        b.close()
