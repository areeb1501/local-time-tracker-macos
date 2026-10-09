"""Record scripts/media/terminal.html (typed install + onboarding) → docs/media/raw/onboarding.webm."""
import shutil
from pathlib import Path
from playwright.sync_api import sync_playwright

HERE = Path(__file__).resolve().parent
OUT = HERE.parents[1] / "docs" / "media" / "raw"; OUT.mkdir(parents=True, exist_ok=True)
with sync_playwright() as p:
    b = p.chromium.launch()
    ctx = b.new_context(viewport={"width": 1140, "height": 720}, record_video_dir=str(OUT),
                        record_video_size={"width": 1140, "height": 720})
    pg = ctx.new_page()
    pg.goto((HERE / "terminal.html").as_uri())
    pg.wait_for_function("window.DONE === true", timeout=120000)
    v = pg.video.path(); ctx.close(); b.close()
    shutil.move(v, OUT / "onboarding.webm"); print("ok")
