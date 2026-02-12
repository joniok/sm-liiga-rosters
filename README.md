# SM-liiga Kokoonpanot

Minimal web app showing daily rosters for Finnish SM-liiga ice hockey matches, including season statistics, golden helmet (kultakypärä) markers, and Red Bull U20 indicators.

## Run locally

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8080
```

Open http://localhost:8080

## Deploy to Google Cloud Run

```bash
# Set your GCP project
gcloud config set project YOUR_PROJECT_ID

# Build and deploy
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
