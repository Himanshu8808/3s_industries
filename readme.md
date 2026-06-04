# VAHAN report automation

## What it does

- Downloads an Excel report from the VAHAN dashboard for e‑rickshaw and three‑wheeler filters.
- Report page: `https://vahan.parivahan.gov.in/vahan4dashboard/vahan/view/reportview.xhtml`
- **Local:** file goes under `download_reports/` (path is in each script).
- **Cloud:** file is produced inside the container, then should be **uploaded to Google Cloud Storage (GCS)** so it is not lost when the run finishes.

## Can you deploy on Google Cloud Run if you are new to GCP?

- **Yes.** You do not need to be a GCP expert. You need:
  - A **Google account** and a **GCP project** (create one in the Google Cloud console).
  - **Billing enabled** on the project (Cloud Run and related services are paid, though a small daily job is usually very cheap).
  - **Docker Desktop** (or another way to build the image) and either the **web console** or the **`gcloud` CLI** to push the image and create the job.
- This repo uses a **Cloud Run Job** (run a container once, then exit), not a public website. That matches “run once per day and download a file.”
- **Caveat:** Some government sites block or behave differently from **cloud datacenter IPs**. If the job fails only in GCP but works on your PC, you may need networking help later (not required to start).

## Explain the cloud workflow to someone else (big picture)

1. **Code** — Python + Playwright opens the site and downloads an `.xlsx` (same logic as locally, but **headless** in the container).
2. **Docker image** — The code + browser dependencies are **packaged** so Google can run the same environment every time (`Dockerfile`).
3. **Artifact Registry** — The image is **stored** in your Google project (like a private “Docker hub”).
4. **Cloud Run Job** — Google **starts a container** from that image, runs **`python cloud_run_main.py`**, then **stops**. You pay only for that short run, not 24/7 servers.
5. **Cloud Storage (GCS)** — The script can **upload** the Excel to a bucket so you have a permanent “datasource” path (`gs://your-bucket/...`).
6. **Cloud Scheduler** — A **daily timer** tells Google to **execute the job** (e.g. every morning). No one has to log in to trigger it.

**One sentence:** *We package the automation in Docker, store it in Artifact Registry, run it on demand as a Cloud Run Job, save the file to GCS, and Cloud Scheduler runs that job once per day.*

## Terms in plain English

- **Project** — Your folder in GCP that owns billing, APIs, and resources.
- **Artifact Registry** — Where your **built Docker image** lives after `docker push`.
- **Cloud Run Job** — Runs your container **to completion** (good for scripts). A **Cloud Run service** would stay up for HTTP traffic; you do **not** need that here.
- **GCS bucket** — A folder in the cloud for files; good place for daily `.xlsx` files.
- **Service account** — A robot identity the job uses; you grant it **permission to write** to the bucket.
- **Cloud Scheduler** — Cron + trigger; “run this job every day at 8:00.”

## Automation steps (what the script does on the website)

- Open the report URL and wait for the page.
- Y axis → **Maker**; X axis → **Month Wise**; year → **current year**.
- First **Refresh**, then wait for data to settle.
- **Expand** the panel.
- Select vehicle types: E‑RICKSHAW WITH CART (G), E‑RICKSHAW(P), THREE WHEELER (PASSENGER), THREE WHEELER (GOODS).
- Wait again; second **Refresh**; longer wait; **Download** Excel.

## Scripts (repo)

- **`state/*.py`** — one Playwright script per state/UT. Default: **state-wide combined** data via **All Vahan4 Running Office** (3 Excel files). Optional: every RTO separately.
- **`controller.py`** — run all scripts in `state/` in parallel (36 states + India combined; calls `run_downloads` on each).
- **`state/download_india_combined.py`** — all-India combined (State = **All Vahan4 Running States**, RTO = **All Vahan4 Running Office**), 3 Excel files.
- **`scripts/sync_state_files.py`** — after editing `state/uttar_pradesh.py`, re-copy logic to the other 35 state files (keeps each file’s state name constants).

## Local setup

- `pip install -r requirements.txt`
- `playwright install chromium`

## Local run

```powershell
cd R:\erikshaw-automation

# One state — combined sales for whole state (default)
python state/uttar_pradesh.py

# Default: combined folder + every RTO folder
python state/maharashtra.py

# Only state-wide combined (3 files) — same as controller default
$env:DOWNLOAD_MODE = "combined"
python state/chandigarh.py

# Only per-RTO folders (no all_vahan4_running_office)
$env:DOWNLOAD_MODE = "individual"
python state/chandigarh.py

# All scripts in state/ (37 jobs: 36 states + India), parallel
python controller.py

# India only (also runs when included in controller)
python state/download_india_combined.py
```

### Environment variables

| Variable | Purpose |
|----------|---------|
| `DOWNLOAD_PATH` | Output root (default `download_reports/`) |
| `DOWNLOAD_MODE` | `both` (default): `all_vahan4_running_office/` + each RTO folder. `combined`: state-wide only (3 files). `individual`: RTO folders only. |
| `HEADLESS` | `1` = headless browser |
| `RTO_LIMIT` | Test first N RTOs (individual/both only) |
| `STATE_FILTER` | Controller: comma-separated modules, e.g. `uttar_pradesh,chandigarh` |
| `MAX_PARALLEL_STATES` | Controller parallelism (default ~3 for 8 GB RAM) |

### Output folders

- **State combined:** `download_reports/<state>/all_vahan4_running_office/<e_rickshaw|three_wheeler|all_vehicles>/`
- **Per RTO:** `download_reports/<state>/<rto folder>/<filter>/`
- **India combined:** `download_reports/india/all_vahan4_running_office/<filter>/`

Each run saves a **new** Excel file with that run’s date and time, e.g.  
`e_rickshaw_2025-05-24_14-30-45.xlsx` — older files in the same folder are kept.

## Cloud deploy — step order (same numbering as the team plan)

| Step | What | Where |
|------|------|--------|
| 1 | Application code | This repo |
| 2 | `Dockerfile`, `.dockerignore`, `requirements.txt` | This repo |
| 3 | `docker build -t vahan-report .` | Your PC (Docker running) |
| 4 | Create Artifact Registry repo; `gcloud auth configure-docker`; tag + `docker push` to `REGION-docker.pkg.dev/...` | Your PC → GCP |
| 5 | Create **Cloud Run Job** with that image; set **task timeout** (e.g. 30m), **memory/CPU**, env vars | GCP console or `gcloud` |
| 6 | Create **GCS bucket**; grant the job’s **service account** `storage.objectCreator` (or tighter custom role) if using `GCS_BUCKET` | GCP |
| 7 | **Cloud Scheduler** → target **Cloud Run job** (e.g. daily cron) | GCP |

**Example job create (replace placeholders):**

- `gcloud run jobs create vahan-report --image=REGION-docker.pkg.dev/PROJECT/REPO/vahan-report:latest --region=REGION --tasks=1 --max-retries=0 --task-timeout=30m --memory=2Gi --cpu=2 --set-env-vars=GCS_BUCKET=your-bucket,GCS_OBJECT_PREFIX=reports,HEADLESS=1`

**Container env vars**

- `HEADLESS` — use `1` in cloud.
- `DOWNLOAD_DIR` — default `/tmp/download_reports` inside the container.
- `GCS_BUCKET` — set to enable upload to your bucket.
- `GCS_OBJECT_PREFIX` — optional folder prefix inside the bucket (default `reports` in code).

## Windows (console encoding)

- If local prints fail on encoding, before `python`:  
  `$env:PYTHONIOENCODING="utf-8"`  
  `$env:PYTHONUTF8="1"`
