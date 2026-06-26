# RCA Triage — AT&T Network Fault Overlay (Live)

A static web dashboard that overlays simulated AT&T network fault events on a US map,
ranks where customer impact concentrates, and detects geographic hot spots. All
rendering, resampling, and analysis happen in the browser — there is no backend.

> **Simulated overlay.** Real AT&T cell locations (OpenCellID) and real TeleLogs fault
> classes, but the pairing of fault → location is **fabricated**. This is not real outage
> data; faults are resampled in-browser to illustrate where impact would concentrate.

## What it does

- **Resample** draws a fresh population of faulted cells on demand (new random seed each
  click), assigning each a root-cause class (C1–C8) per the configurable Fault Blend.
- **Display Filter** (saturation dial) controls how many faults are shown.
- **Severity** (per-class 1–10) drives **Impacted Customers** = served × severity × 0.1,
  shown live in aggregate and on per-fault hover tooltips.
- **Root Cause** legend doubles as a class filter — click to toggle a class, shift-click
  to solo, with a reset link.
- **Hot Spots** clusters the in-view faults into ~50-mile areas, ranks them by impacted
  customers, and names each by an impacted-weighted vote of its towers' nearest cities.
  Respects all active filters (display dial, class toggles, region drill).

## Running locally

Serve the repo root over HTTP (`file://` blocks `fetch` and ES module imports):

```
python -m http.server
```

Then open `http://localhost:8000`. The app fetches `pool.json` and `us_states.json`,
imports `resample.js`, and renders the dashboard.

## Deploying to GitHub Pages

The app is fully static, so GitHub Pages serves it with no build step or backend.

1. Put the four runtime files at the **repo root**: `index.html`, `resample.js`,
   `pool.json`, `us_states.json`.
2. Commit and push to GitHub (the repo must be **public** on a free account).
3. In the repo: **Settings → Pages → Build and deployment**. Set **Source** to
   *Deploy from a branch*, pick your branch (e.g. `main`) and the `/ (root)` folder.
4. Wait ~1 minute; the site publishes at `https://<username>.github.io/<repo>/`.

Notes:
- `pool.json` is ~18 MB on disk but GitHub Pages gzips it on the wire (~2–3 MB to the
  visitor). No pre-compression or `.gz` sidecar is needed — commit the plain file.
- A `.nojekyll` file at the root tells Pages to skip Jekyll processing (slightly faster
  publishes for a plain static site). Optional but recommended.
- Leaflet and the basemap tiles load from public CDNs, which work fine from any HTTPS
  origin including GitHub Pages.

## The dataset (`ETL/`)

The browser only needs `pool.json`. Everything that produces it lives in **`ETL/`** and
is **not** part of the runtime — you don't need it to run or deploy the site.

The enriched dataset behind the dashboard is published on Hugging Face:
**[`LoneWolfgang/att-tower-rca`](https://huggingface.co/datasets/LoneWolfgang/att-tower-rca)** —
a national map of AT&T towers, each labeled with its state, nearest city, and an estimate
of how many customers it serves. See `ETL/README.md` for the full data dictionary and a
build quickstart, and `ETL/att_tower_rca_eda.ipynb` for an exploration of tower
distribution, population served, and how customers are attributed to each tower.

Pipeline:

- **`ETL/build_att_dataset.py`** — builds the labeled, customer-weighted cell pool from
  OpenCellID: filters to AT&T, dedups, assigns state + quadrant via point-in-polygon,
  apportions ACS block-group population to nearest tower(s), and labels nearest cities.
  Outputs `att_us_dataset.parquet` (the file published to Hugging Face).
- **`export_pool.py`** (repo root) — bakes the enriched parquet into the columnar
  `pool.json` the browser loads.

```
python ETL/build_att_dataset.py --csv 310.csv.gz 311.csv.gz 312.csv.gz 313.csv.gz
python export_pool.py --cells att_us_dataset.parquet --out pool.json
python export_pool.py --cells att_us_dataset.parquet --measure   # dry run, prints sizes
```

Requires the deps in `requirements.txt` (`pandas>=2.2`, `pyarrow`, `geopandas`, `scipy`,
and others); the build step also needs either a cached city reference or network access.

## File layout

```
index.html          — live app (UI, map, hot spot detection, all in one file)
resample.js         — in-browser fault resampler (pure, no DOM)
pool.json           — baked cell pool (184,920 cells + city + population)
us_states.json      — states GeoJSON for the choropleth
export_pool.py      — regenerates pool.json from the enriched parquet
requirements.txt    — Python deps for the data pipeline
.nojekyll           — tells GitHub Pages to skip Jekyll
ETL/
  build_att_dataset.py     — OpenCellID → labeled, customer-weighted parquet
  att_tower_rca_eda.ipynb  — dataset exploration notebook
  README.md                — dataset dictionary + build quickstart
```

> Local-only (gitignored): `data/`, `hf_dataset/`, and `*.parquet` source/intermediate
> files. The committed site needs only the runtime files above; the dataset itself lives
> on Hugging Face.