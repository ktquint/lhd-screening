# lhd-screening

Public-facing low-head dam screening tool. Combines a Leaflet-based map of
~20k known LHDs with an offline ARC-driven hydraulic screening pipeline that
estimates each dam's crest length, height, and dangerous-flow range.

- **Frontend** (`frontend/`): static Leaflet map (`index.html` +
  `mapping-logic.js`) that reads `data/full_lhd_website.csv`.
  Color-codes dams by hazard class and exposes filters by state, owner,
  hazard, and HUC.
- **Backend** (`backend/`): HUC6-batched ARC pipeline that stages NHD
  flowlines + 3DEP DEMs, runs the Automated Rating Curve generator, then
  derives geometry (weir length, dam height) and dangerous-flow range
  (Qmin/Qmax) per dam.

## Setup

The env is a **micromamba** env (not conda). Bootstrap from scratch with
`environment.yml` (conda-forge for the GDAL/rasterio/geopandas binaries,
pip for the rest):

```bash
micromamba create -f environment.yml
micromamba activate lhd-environment
```

The `backend/lhd_processor/` package is the in-repo successor to the old
external `lhd-processor` package — no separate install needed.

The pipeline also imports `arc` (Automated Rating Curve), installed as an
editable sibling checkout — not covered by `environment.yml`:

```bash
git clone https://github.com/MikeFHS/automated-rating-curve.git ../automated-rating-curve
cd ../automated-rating-curve && pip install -e .
```

## Backend pipeline

### One-time prep

Spatial-joins every dam in `data/full_lhd_website.csv` against the
USGS WBD national GPKG (auto-downloaded into `cache/wbd/`, ~2.5 GB) and
overwrites the `HUC2/HUC4/HUC6/HUC8` columns with authoritative codes:

```bash
python backend/assign_huc8.py
```

### Main pipeline (`rolling_pipeline.py`)

Groups dams by HUC6 (default; `--huc-level` accepts 2/4/6/8), writes a
per-HUC bundle at `<staging-root>/huc6_<KEY>/`, and chains 6 steps per
batch:

1. `stage_nhd_dem.py` — NHD flowlines + 3DEP raw tile manifest.
2. `build_trimmed_dems.py` — mosaic + clip per-dam DEMs.
3. `build_stream_rasters.py` — STRM rasters for ARC.
4. `build_streamflow_csv.py` — NWM zarr retrospective flow series.
5. `run_arc_batch.py` — ARC (Automated Rating Curve).
6. `run_analysis_batch.py` — weir length, dam height, Qmin/Qmax per XS.

```bash
python backend/rolling_pipeline.py \
    --local-staging-root /path/to/staging \
    --existing-data-dir  /old/lhd_staging \   # optional, may repeat
    --min-free-gb 600
```

`--existing-data-dir` symlinks already-staged per-dam folders
(DEM/STRM/LAND/FLOW/RESULTS) from a prior scattered run into the new HUC
bundle, so reruns don't refetch DEMs or rerun ARC.

A ledger at `<staging-root>/huc8_ledger.json` tracks each batch's status:
`staging → arc_running → ready_to_archive | partial | errored | archived`.
Reruns only skip `ready_to_archive` and `archived`; `partial` and `errored`
batches retry automatically (per-dam caches make retries cheap).

### Disk gate and archiving

When free disk drops below `--min-free-gb` (default 600), the loop halts
cleanly with the archive recipe printed. To clear the queue in one shot:

```bash
python backend/rolling_pipeline.py \
    --local-staging-root /path/to/staging \
    --archive-to /Volumes/ExternalDrive
```

`--archive-to` walks the ledger, consolidates symlinks into real files,
`mv`s every `ready_to_archive` bundle to the destination, and flips its
ledger status to `archived`.

### Master CSV refresh

The pipeline auto-runs the aggregate step at the end of every run (and on
disk-gate halt), which rolls per-dam `RESULTS/<id>/analysis_summary.json`
into four columns on `data/full_lhd_website.csv`:

- `Qmin_env` / `Qmax_env` — envelope across the 4 downstream XS.
- `Qmin_stable` / `Qmax_stable` — XS nearest the slope-stabilization cell
  used for the dam-height estimate.

To trigger manually:

```bash
python backend/rolling_pipeline.py \
    --local-staging-root /path/to/staging \
    --existing-data-dir  /old/lhd_staging \
    --aggregate
```

### Other commands

```bash
# inspect ledger
python backend/rolling_pipeline.py --local-staging-root /path --status

# find a specific dam's batch
python backend/rolling_pipeline.py --local-staging-root /path --locate 1234

# per-key manual archive (rare; --archive-to handles the queue)
python backend/rolling_pipeline.py --local-staging-root /path --consolidate 140600
python backend/rolling_pipeline.py --local-staging-root /path --mark-archived 140600
```

### Failure logging

`run_arc_batch.py` and `run_analysis_batch.py` write
`<huc-bundle>/failures_arc.json` and `failures_analysis.json` when any dam
fails — `{dam_id: status_string}` with the specific reason
(`missing-input`, `arc-error:…`, `localxs-error:…`, etc.). Files are
cleaned up automatically on a rerun that resolves all failures.

## Frontend

Static site, no build step. Serve `frontend/` over any HTTP server and open
`index.html`:

```bash
cd frontend && python -m http.server 8000
```

Reads `data/full_lhd_website.csv` via the relative URL `data/...`. The CSV
is canonically at the repo-root `data/` dir; a symlink at
`frontend/data/full_lhd_website.csv` makes the relative URL resolve in
local dev, and `.github/workflows/pages.yml` stages `data/` alongside
`frontend/` so the same URL works on GitHub Pages.

## Repo layout

```
backend/
  assign_huc8.py            # one-time WBD HUC assignment
  rolling_pipeline.py       # HUC6-batched orchestrator
  stage_nhd_dem.py          # step 1
  build_trimmed_dems.py     # step 2
  build_stream_rasters.py   # step 3
  build_streamflow_csv.py   # step 4
  run_arc_batch.py          # step 5
  run_analysis_batch.py     # step 6
  lhd_processor/            # in-repo hydraulic library
  screening/                # shared helpers (reach, width, height)
data/
  full_lhd_website.csv      # master dam CSV (read by site + written by pipeline)
frontend/
  index.html                # Leaflet map
  mapping-logic.js
  data/                     # local-dev symlink to ../data (gitignored)
cache/wbd/                  # WBD national GPKG (auto-downloaded)
scripts/                    # analysis + paper scripts (compare_*, joint_error_propagation, …)
```

## Known gotchas

- **TNM outages.** `stage_nhd_dem` hits `tnmaccess.nationalmap.gov`. When
  it's down it returns HTTP 200 with a non-JSON `BadRequest …
  RemoteDisconnected …` body, breaking `r.json()`. Dams without
  prior-cached DEMs then get 0 tiles and fail downstream. Don't start a
  full sweep during an outage; reruns retry only the failed dams.
- **Windows file locks.** Antivirus / Search Indexer holding open
  `RESULTS/<id>/Bathymetry/*.tif` can produce `Permission denied` on the
  ARC cleanup step. Exclude the staging root from real-time scanning.
