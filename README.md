# SM-liiga Kokoonpanot

Minimal web app showing daily rosters for Finnish SM-liiga ice hockey matches, including season statistics, golden helmet (kultakypärä) markers, and Red Bull U20 indicators.

## GitHub Pages deployment (static site)

The site is automatically built and deployed to GitHub Pages **daily at 13:00 Finnish time** via GitHub Actions. You can also trigger a build manually from the Actions tab.

### How it works

1. `build.py` fetches live data from the Liiga.fi API and renders the Jinja2 template to static HTML in the `output/` directory.
2. The GitHub Actions workflow (`.github/workflows/deploy.yml`) runs `build.py` and deploys the output to GitHub Pages.

### Initial setup

1. Go to your repository **Settings → Pages**.
2. Under **Source**, select **GitHub Actions**.
3. Push to the `main` branch (or trigger the workflow manually).

### Custom domain

1. In **Settings → Pages → Custom domain**, enter your subdomain (e.g. `liiga.example.com`).
2. Add a **CNAME** DNS record pointing the subdomain to `<username>.github.io`.
3. GitHub will automatically provision an SSL certificate.

### Manual build

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python build.py
# Static site is written to output/
```

## Run locally (development server)

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8080
```

Open http://localhost:8080

## Deploy to Google Cloud Run (alternative)

```bash
gcloud config set project YOUR_PROJECT_ID

gcloud run deploy sm-liiga-rosters \
  --source . \
  --region europe-north1 \
  --allow-unauthenticated \
  --memory 256Mi \
  --min-instances 0 \
  --max-instances 2
```

## Data source

All data is fetched from the [Liiga.fi](https://liiga.fi) public API.
