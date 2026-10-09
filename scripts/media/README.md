# README media

Scripts that regenerate the screenshots and GIFs in `docs/media/` from the
fictional demo dataset (never from real data).

```bash
TT_HOME=/tmp/tt-demo .venv/bin/python -m timetracker.demo --fresh
TT_HOME=/tmp/tt-demo .venv/bin/python -m uvicorn timetracker.dashboard:app --port 8399 &
python3 scripts/media/shots.py           # page screenshots
python3 scripts/media/feature_shots.py   # close-ups + dark mode
python3 scripts/media/tour.py            # docs/media/raw/tour.webm → convert with ffmpeg
python3 scripts/media/record_terminal.py # onboarding terminal animation
```

Needs Playwright (`pip install playwright && playwright install chromium`) and ffmpeg.
