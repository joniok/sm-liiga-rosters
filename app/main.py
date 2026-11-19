"""SM-liiga Rosters – minimal web app (round-based view)."""

from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import liiga_client
from .rounds import build_context

app = FastAPI(title="SM-liiga Rosters")

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

# Mount static files
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


@app.get("/", response_class=HTMLResponse)
async def index(request: Request, stats: str | None = None):
    """Main page: show previous round results and next round rosters."""
    now_fi = datetime.now(liiga_client.FI_TZ)
    today = now_fi.date()
    ctx = await build_context(today, now_fi, stats_mode=stats)
    return templates.TemplateResponse("index.html", {"request": request, **ctx})
