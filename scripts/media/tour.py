"""Record a short dashboard walkthrough from the demo server (:8399) → docs/media/raw/tour.webm."""
import shutil
from pathlib import Path
from playwright.sync_api import sync_playwright
import shots  # reuse SUBS/patch/BASE

OUT = Path(__file__).resolve().parents[2] / "docs" / "media" / "raw"
OUT.mkdir(exist_ok=True)
W, H = 1440, 900

with sync_playwright() as p:
    b = p.chromium.launch()
    ctx = b.new_context(viewport={"width": W, "height": H}, record_video_dir=str(OUT), record_video_size={"width": W, "height": H})
    ctx.route(f"{shots.BASE}/", shots.patch)
    pg = ctx.new_page()
    pg.goto(f"{shots.BASE}/#/overview?range=today"); pg.wait_for_load_state("networkidle")
    pg.evaluate("localStorage.clear()")
    pg.wait_for_timeout(1500)
    for label in ("7 days", "30 days"):
        pg.get_by_text(label, exact=True).first.click(); pg.wait_for_timeout(1600)
    pg.mouse.wheel(0, 900); pg.wait_for_timeout(1500)
    pg.mouse.wheel(0, 900); pg.wait_for_timeout(1300)
    pg.get_by_text("Projects", exact=True).first.click(); pg.wait_for_timeout(2000)
    pg.locator("text=Acme Storefront >> visible=true").first.click(); pg.wait_for_timeout(2400)
    pg.mouse.wheel(0, 700); pg.wait_for_timeout(1400)
    pg.get_by_text("Sessions", exact=True).first.click(); pg.wait_for_timeout(1800)
    pg.locator("text=Ask").first.click(); pg.wait_for_timeout(1000)
    pg.get_by_text("Where did my week go?", exact=True).first.click(); pg.wait_for_timeout(2600)
    video = pg.video.path()
    ctx.close(); b.close()
    shutil.move(video, OUT / "tour.webm")
    print(OUT / "tour.webm")
