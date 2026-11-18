#!/usr/bin/env python3
"""Static site generator – renders SM-liiga Kokoonpanot to static HTML.

Usage:
    python build.py

Fetches live data from the Liiga.fi API and renders the Jinja2 template
to a self-contained static site in the ``output/`` directory.
"""

import asyncio
import shutil
from datetime import datetime
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

from app import liiga_client
from app.rounds import build_context, fi_date

OUTPUT_DIR = Path("output")
APP_DIR = Path("app")


async def build() -> None:
    """Fetch data from Liiga API and generate static HTML."""
    today = datetime.now(liiga_client.FI_TZ).date()
    now_fi = datetime.now(liiga_client.FI_TZ)

    print(f"Building for {fi_date(today)} …")

    ctx = await build_context(today, now_fi)

    # ── Render template ──
    env = Environment(loader=FileSystemLoader(str(APP_DIR / "templates")))
    template = env.get_template("index.html")

    build_time = now_fi.strftime("%d.%m.%Y %H:%M")

    html = template.render(**ctx, build_time=build_time)

    # ── Write output ──
    if OUTPUT_DIR.exists():
        shutil.rmtree(OUTPUT_DIR)
    OUTPUT_DIR.mkdir(parents=True)

    (OUTPUT_DIR / "index.html").write_text(html, encoding="utf-8")

    # Copy static assets
    shutil.copytree(APP_DIR / "static", OUTPUT_DIR / "static")

    # .nojekyll prevents GitHub Pages from running Jekyll
    (OUTPUT_DIR / ".nojekyll").touch()

    print(f"Static site written to {OUTPUT_DIR}/ (built at {build_time})")


if __name__ == "__main__":
    asyncio.run(build())
