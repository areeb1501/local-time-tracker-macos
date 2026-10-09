"""Feature close-ups + dark mode, from the demo dashboard on :8399."""
from pathlib import Path
from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8399"
OUT = Path(__file__).resolve().parents[2] / "docs" / "media"

def go(page, h, wait=1800):
    page.goto(f"{BASE}/#{h}"); page.wait_for_load_state("networkidle"); page.wait_for_timeout(wait)

def card(page, title, name):
    c = page.locator(".card", has=page.locator("h2", has_text=title)).first
    c.scroll_into_view_if_needed(); page.wait_for_timeout(400)
    c.screenshot(path=str(OUT / f"{name}.png")); print("saved", name)

with sync_playwright() as p:
    b = p.chromium.launch()
    ctx = b.new_context(viewport={"width": 1440, "height": 900}, device_scale_factor=2)
    page = ctx.new_page()
    go(page, "/overview?range=7d")
    card(page, "activity heatmap", "heatmap")
    card(page, "Focus & fragmentation", "focus")
    go(page, "/activity?range=7d")
    page.fill("#srchQ", "checkout"); page.evaluate("doSearch()"); page.wait_for_timeout(2500)
    c = page.locator(".card", has=page.locator("h2", has_text="Search history")).first
    page.evaluate("y => window.scrollTo(0, y)", c.evaluate("e => e.getBoundingClientRect().top + window.scrollY - 96"))
    page.wait_for_timeout(400)
    box = c.bounding_box()
    page.screenshot(path=str(OUT / "search.png"), clip={"x": box["x"] - 12, "y": box["y"] - 12,
                    "width": box["width"] + 24, "height": min(box["height"], 640) + 12}); print("saved search")
    go(page, "/settings")
    card(page, "Project rules", "rules")
    go(page, "/sessions/2?range=7d", 2500)
    page.screenshot(path=str(OUT / "session-detail.png")); print("saved session-detail")
    dark = b.new_context(viewport={"width": 1440, "height": 900}, device_scale_factor=2, color_scheme="dark")
    dp = dark.new_page()
    go(dp, "/overview?range=7d")
    dp.evaluate("""() => document.querySelectorAll('.card').forEach(c => {
        if (/needs a project/i.test(c.textContent.slice(0,60))) c.style.display='none'})""")
    dp.wait_for_timeout(300)
    dp.screenshot(path=str(OUT / "overview-dark.png")); print("saved overview-dark")
    go(dp, "/projects/1?range=30d")
    dp.screenshot(path=str(OUT / "project-detail-dark.png")); print("saved project-detail-dark")
    b.close()
