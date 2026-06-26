#!/usr/bin/env python3
"""
Build the AT&T tower dataset for RCA-triage simulation, end to end.

This single pipeline turns raw OpenCellID exports into a labeled, customer-weighted
map of AT&T cell sites:

  STAGE 1  EXTRACT   read OpenCellID export(s), keep US AT&T cells (auditable code
                     table), dedup on MCC-MNC-AREA-CELL keeping the best-sampled obs.
  STAGE 2  GEO-LABEL point-in-polygon assign each tower a US STATE + Census QUADRANT
                     using real boundaries (not bounding boxes).
  STAGE 3  POPULATION apportion ACS block-group population to nearest tower(s) via a
                     KD-tree, scaled by AT&T market share -> estimated_population_served.
  STAGE 4  CITY-LABEL attach each tower its nearest populated place (in-state preferred)
                     -> nearest_city, nearest_city_state, nearest_city_km.
  STAGE 5  WRITE      save att_us_dataset.parquet (+ .csv).

Every stage emits a [stage] log line stating what it is doing and the counts it
produced, so a run is auditable from stdout alone.

ON "5G": OpenCellID documents radio classes GSM/UMTS/LTE/CDMA; 5G NR is not a
documented class and non-standalone 5G rides LTE. This is "where AT&T cells are,"
not a verified 5G map.

ON estimated_population_served: a ROUGH PROXY, not real subscriber counts. It uses
residents (not commuter load), a flat national market share, and nearest-tower
apportionment. Treat it as "relative customer weight," not a billed-subscriber figure.

See README.md for setup, keys, and the one-command quickstart.
"""

import argparse
import glob
import gzip
import io
import logging
import os
import sys
import urllib.request
from pathlib import Path

import numpy as np
import pandas as pd

log = logging.getLogger("build")


# ============================================================================
# Constants (single source of truth; auditable)
# ============================================================================

OCID_DOWNLOAD = "https://opencellid.org/ocid/downloads"
OPENCELLID_API_KEY = os.getenv("OPENCELLID_API_KEY")

# All seven US mobile country codes (the US exhausted MNC space under 310 alone).
US_MCCS = [310, 311, 312, 313, 314, 315, 316]

# --- AT&T code table: EXPLICIT and EDITABLE. --------------------------------
# (mcc, mnc): note.  Cross-check against a CURRENT PLMN list before trusting a
# national pull -- MNC<->carrier mappings drift. 310-410 & 310-280 are AT&T's
# principal commercial codes (some lists show 310-280 as T-Mobile post-churn).
# 313-100 is FirstNet (rides AT&T's RAN via MOCN) -> flagged is_firstnet.
ATT_CODES = {
    (310, 16): "AT&T", (310, 30): "AT&T", (310, 70): "AT&T", (310, 80): "AT&T",
    (310, 90): "AT&T", (310, 150): "AT&T", (310, 170): "AT&T",
    (310, 280): "AT&T (verify; some lists show T-Mobile)",
    (310, 380): "AT&T", (310, 410): "AT&T (primary)", (310, 560): "AT&T",
    (310, 670): "AT&T", (310, 680): "AT&T", (310, 950): "AT&T",
    (311, 70): "AT&T", (311, 90): "AT&T", (311, 180): "AT&T", (311, 190): "AT&T",
    (312, 90): "AT&T", (312, 680): "AT&T",
    (313, 100): "FirstNet (AT&T RAN)", (313, 210): "AT&T",
}
FIRSTNET_CODES = {(313, 100)}

# OpenCellID export schema (the country/world CSVs have no header).
OCID_COLS = [
    "radio", "mcc", "net", "area", "cell", "unit",
    "lon", "lat", "range", "samples", "changeable",
    "created", "updated", "averageSignal",
]

# Census 1:500k cartographic state boundary file (~1.4 MB), pinned for repro.
CENSUS_STATES_URL = (
    "https://www2.census.gov/geo/tiger/GENZ2024/shp/cb_2024_us_state_500k.zip"
)
CENSUS_STATES_CACHE = "cb_2024_us_state_500k.zip"

# Census 4-region split (Northeast / Midwest / South / West), USPS -> quadrant.
STATE_TO_QUADRANT = {
    "CT": "Northeast", "ME": "Northeast", "MA": "Northeast", "NH": "Northeast",
    "RI": "Northeast", "VT": "Northeast", "NJ": "Northeast", "NY": "Northeast",
    "PA": "Northeast",
    "IL": "Midwest", "IN": "Midwest", "MI": "Midwest", "OH": "Midwest",
    "WI": "Midwest", "IA": "Midwest", "KS": "Midwest", "MN": "Midwest",
    "MO": "Midwest", "NE": "Midwest", "ND": "Midwest", "SD": "Midwest",
    "DE": "South", "FL": "South", "GA": "South", "MD": "South", "NC": "South",
    "SC": "South", "VA": "South", "DC": "South", "WV": "South", "AL": "South",
    "KY": "South", "MS": "South", "TN": "South", "AR": "South", "LA": "South",
    "OK": "South", "TX": "South",
    "AZ": "West", "CO": "West", "ID": "West", "MT": "West", "NV": "West",
    "NM": "West", "UT": "West", "WY": "West", "AK": "West", "CA": "West",
    "HI": "West", "OR": "West", "WA": "West",
}

# --- Population apportionment tunables --------------------------------------
ATT_MARKET_SHARE = 0.45   # final multiplier on apportioned population.
K_NEAREST = 1             # towers each block group splits its population across.
POP_FLOOR = 1.0           # towers that win no block group still get nonzero impact.
EQUAL_AREA_CRS = "EPSG:5070"   # CONUS Albers; "nearest" in meters, not degrees.

# ACS 5-year total population (block-group geography).
CENSUS_API_KEY = os.getenv("CENSUS_API_KEY")
ACS_YEAR = 2023
ACS_POP_VAR = "B01003_001E"
ACS_SENTINELS = {-666666666, -999999999, -888888888, -555555555, -333333333, -222222222}
ACS_BG_GEO_URL = "https://www2.census.gov/geo/tiger/GENZ2022/shp/cb_2022_us_bg_500k.zip"
ACS_BG_CACHE = "acs_bg_pop.parquet"

# --- City labeling (nearest populated place) --------------------------------
# A comprehensive ~30k-city list (full national coverage) enriched with population
# from a top-1k list used only as a TIEBREAKER, merged once and cached.
CITY_GEOMETRY_URL = (
    "https://raw.githubusercontent.com/kelvins/US-Cities-Database/main/csv/us_cities.csv"
)
CITY_POP_URL = (
    "https://raw.githubusercontent.com/plotly/datasets/master/us-cities-top-1k.csv"
)
CITY_CACHE = "us_cities_ref.parquet"
# Prefer an in-state city if it is within this margin (m) of the nearest city.
CITY_STATE_MARGIN_M = 8000.0

_STATE_NAME_TO_CODE = {
    "Alabama": "AL", "Alaska": "AK", "Arizona": "AZ", "Arkansas": "AR",
    "California": "CA", "Colorado": "CO", "Connecticut": "CT", "Delaware": "DE",
    "District of Columbia": "DC", "Florida": "FL", "Georgia": "GA", "Hawaii": "HI",
    "Idaho": "ID", "Illinois": "IL", "Indiana": "IN", "Iowa": "IA", "Kansas": "KS",
    "Kentucky": "KY", "Louisiana": "LA", "Maine": "ME", "Maryland": "MD",
    "Massachusetts": "MA", "Michigan": "MI", "Minnesota": "MN", "Mississippi": "MS",
    "Missouri": "MO", "Montana": "MT", "Nebraska": "NE", "Nevada": "NV",
    "New Hampshire": "NH", "New Jersey": "NJ", "New Mexico": "NM", "New York": "NY",
    "North Carolina": "NC", "North Dakota": "ND", "Ohio": "OH", "Oklahoma": "OK",
    "Oregon": "OR", "Pennsylvania": "PA", "Rhode Island": "RI",
    "South Carolina": "SC", "South Dakota": "SD", "Tennessee": "TN", "Texas": "TX",
    "Utah": "UT", "Vermont": "VT", "Virginia": "VA", "Washington": "WA",
    "West Virginia": "WV", "Wisconsin": "WI", "Wyoming": "WY",
}


# ============================================================================
# STAGE 1 -- EXTRACT: download / read OpenCellID, filter to AT&T, dedup
# ============================================================================

def download_mcc(token: str, mcc: int, dest: Path) -> Path:
    import requests
    log.info("[extract] downloading MCC %d from OpenCellID", mcc)
    params = {"token": token, "type": "mcc", "file": f"{mcc}.csv.gz"}
    r = requests.get(OCID_DOWNLOAD, params=params, timeout=600,
                     headers={"User-Agent": "att-build/1.0"})
    if r.status_code == 429 or b"RATE_LIMIT" in r.content[:200]:
        sys.exit("OpenCellID rate limit (2 files/token/day). Download per-MCC files "
                 "manually from opencellid.org and pass them with --csv.")
    r.raise_for_status()
    dest.write_bytes(r.content)
    log.info("[extract]   wrote %s (%.1f MB)", dest, dest.stat().st_size / 1e6)
    return dest


def is_att(mcc, net) -> bool:
    try:
        return (int(mcc), int(net)) in ATT_CODES
    except (TypeError, ValueError):
        return False


def extract_att(path: Path) -> pd.DataFrame:
    """Stream one gzip/csv export; keep only US AT&T rows."""
    opener = gzip.open if path.suffix == ".gz" else open
    kept, total = [], 0
    log.info("[extract] scanning %s", path)
    with opener(path, "rt", encoding="utf-8", errors="replace") as fh:
        reader = pd.read_csv(
            fh, names=OCID_COLS, header=None, chunksize=500_000,
            dtype={"radio": "string", "mcc": "Int64", "net": "Int64",
                   "area": "Int64", "cell": "Int64", "unit": "Int64",
                   "lon": "float64", "lat": "float64", "range": "Int64",
                   "samples": "Int64", "changeable": "Int64"},
            on_bad_lines="skip",
        )
        for chunk in reader:
            total += len(chunk)
            chunk = chunk[chunk["mcc"].isin(US_MCCS)]
            if not chunk.empty:
                m = chunk.apply(lambda r: is_att(r["mcc"], r["net"]), axis=1)
                chunk = chunk[m]
                if not chunk.empty:
                    kept.append(chunk)
    n_kept = sum(len(k) for k in kept)
    log.info("[extract]   %s scanned, %s AT&T cells kept", f"{total:,}", f"{n_kept:,}")
    return pd.concat(kept, ignore_index=True) if kept else pd.DataFrame(columns=OCID_COLS)


def dedup(df: pd.DataFrame) -> pd.DataFrame:
    """Collapse duplicate cells (same MCC-MNC-AREA-CELL across or within files),
    keeping the observation with the most measurements (best averaged position)."""
    if df.empty:
        return df
    df = df.copy()
    df["cell_global_id"] = (
        df["mcc"].astype(str) + "-" + df["net"].astype(str) + "-"
        + df["area"].astype(str) + "-" + df["cell"].astype(str)
    )
    before = len(df)
    df = (df.sort_values("samples", ascending=False, na_position="last")
            .drop_duplicates(subset="cell_global_id", keep="first"))
    log.info("[extract] dedup %s -> %s unique cells (%s duplicates removed)",
             f"{before:,}", f"{len(df):,}", f"{before - len(df):,}")
    return df.reset_index(drop=True)


def stage_extract(csv_args, token, mccs) -> pd.DataFrame:
    log.info("=== STAGE 1: EXTRACT (read + filter to AT&T + dedup) ===")
    frames = []
    if csv_args:
        paths = []
        for pat in csv_args:
            hits = [Path(p) for p in glob.glob(pat)] or [Path(pat)]
            paths.extend(hits)
        for p in paths:
            if not p.exists():
                log.warning("[extract] missing input, skipping: %s", p)
                continue
            frames.append(extract_att(p))
    else:
        tmp = Path("ocid_downloads"); tmp.mkdir(exist_ok=True)
        for mcc in mccs:
            frames.append(extract_att(download_mcc(token, mcc, tmp / f"{mcc}.csv.gz")))

    df = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    if df.empty:
        sys.exit("[extract] No AT&T US cells found -- check inputs / ATT_CODES table.")
    df = dedup(df)
    # Canonical coordinate names used by every later stage and the output schema.
    df = df.rename(columns={"lat": "latitude", "lon": "longitude"})
    return df


# ============================================================================
# STAGE 2 -- GEO-LABEL: point-in-polygon state + quadrant
# ============================================================================

def get_states_gdf(states_shp):
    import geopandas as gpd
    if states_shp is None:
        local = Path(CENSUS_STATES_CACHE)
        if not local.exists():
            import requests
            log.info("[geo] downloading Census state boundaries")
            r = requests.get(CENSUS_STATES_URL, timeout=300,
                             headers={"User-Agent": "att-build/1.0"})
            r.raise_for_status()
            local.write_bytes(r.content)
            log.info("[geo]   cached %s (%.1f MB)", local, local.stat().st_size / 1e6)
        src = local
    else:
        src = states_shp
    gdf = gpd.read_file(src)
    gdf = gdf[["STUSPS", "NAME", "geometry"]].rename(
        columns={"STUSPS": "state", "NAME": "state_name"})
    return gdf.to_crs("EPSG:4326")


def stage_geo_label(df: pd.DataFrame, states_shp) -> pd.DataFrame:
    import geopandas as gpd
    log.info("=== STAGE 2: GEO-LABEL (point-in-polygon state + quadrant) ===")
    states = get_states_gdf(states_shp)
    pts = gpd.GeoDataFrame(
        df.copy(),
        geometry=gpd.points_from_xy(df["longitude"], df["latitude"]),
        crs="EPSG:4326",
    )
    log.info("[geo] point-in-polygon labeling %s cells", f"{len(pts):,}")
    joined = gpd.sjoin(pts, states, how="left", predicate="within")
    joined = joined[~joined.index.duplicated(keep="first")]  # border overlaps -> first
    out = pd.DataFrame(joined.drop(columns=["geometry", "index_right"]))
    out["quadrant"] = out["state"].map(STATE_TO_QUADRANT).astype("string")
    unlabeled = int(out["state"].isna().sum())
    if unlabeled:
        log.info("[geo]   %s cells fell outside all US state polygons "
                 "(offshore / position noise) -> state=NaN", f"{unlabeled:,}")
    by_quad = out["quadrant"].value_counts(dropna=False).to_dict()
    log.info("[geo] by quadrant: %s", by_quad)
    return out


# ============================================================================
# STAGE 3 -- POPULATION: ACS block-group apportionment
# ============================================================================

def fetch_acs_bg(api_key, cache: Path):
    import geopandas as gpd
    import requests
    if cache.exists():
        log.info("[pop] loading cached block-group population %s", cache)
        return gpd.read_parquet(cache)
    if not api_key:
        sys.exit("No --acs-bg file and no CENSUS_API_KEY. Get a free key: "
                 "https://api.census.gov/data/key_signup.html")

    # Block group's only allowed parent wildcard is county:* nested in a specific
    # state, so we query per state with `in=state:FP county:*`.
    base = f"https://api.census.gov/data/{ACS_YEAR}/acs/acs5"
    frames, errors = [], []
    log.info("[pop] pulling ACS block-group population per state")
    for fp in (f"{i:02d}" for i in range(1, 57)):
        params = {"get": f"NAME,{ACS_POP_VAR}", "for": "block group:*",
                  "in": f"state:{fp} county:*", "key": api_key}
        try:
            r = requests.get(base, params=params, timeout=180)
        except Exception as e:
            errors.append((fp, f"request failed: {e}")); continue
        if r.status_code == 204:
            continue  # FIPS gap
        if r.status_code != 200:
            errors.append((fp, f"HTTP {r.status_code}: {r.text[:200].strip()}")); continue
        try:
            rows = r.json()
        except Exception as e:
            errors.append((fp, f"bad JSON: {e}")); continue
        hdr, *data = rows
        d = pd.DataFrame(data, columns=hdr)
        d["GEOID"] = d["state"] + d["county"] + d["tract"] + d["block group"]
        d["population"] = pd.to_numeric(d[ACS_POP_VAR], errors="coerce")
        frames.append(d[["GEOID", "population"]])

    if not frames:
        msg = "\n".join(f"  state {fp}: {err}" for fp, err in errors[:8])
        sys.exit("[pop] ERROR: every block-group request failed. First errors:\n"
                 f"{msg}\nCheck CENSUS_API_KEY, network egress to api.census.gov, "
                 "and the variable/vintage.")
    if errors:
        log.info("[pop] %d state requests returned no data (FIPS gaps / transient)",
                 len(errors))
    pop = pd.concat(frames, ignore_index=True)
    pop.loc[pop["population"].isin(ACS_SENTINELS), "population"] = 0
    pop["population"] = pop["population"].fillna(0).clip(lower=0)
    log.info("[pop]   %s block groups with population", f"{len(pop):,}")

    log.info("[pop] loading block-group geometry")
    bg = gpd.read_file(ACS_BG_GEO_URL)[["GEOID", "geometry"]]
    bg = bg.merge(pop, on="GEOID", how="left")
    bg["population"] = bg["population"].fillna(0)
    bg = bg.to_crs("EPSG:4326")
    try:
        bg.to_parquet(cache)
        log.info("[pop]   cached %s (%s block groups)", cache, f"{len(bg):,}")
    except Exception as e:
        log.warning("[pop]   cache skipped (%s)", e)
    return bg


def load_acs_bg(acs_path, api_key):
    import geopandas as gpd
    if acs_path:
        p = Path(acs_path)
        gdf = gpd.read_parquet(p) if p.suffix in (".parquet", ".pq") else gpd.read_file(p)
        cols = {c.lower(): c for c in gdf.columns}
        if "geoid" not in cols or "population" not in cols:
            if ACS_POP_VAR in gdf.columns:
                gdf = gdf.rename(columns={ACS_POP_VAR: "population"})
        gdf = gdf.rename(columns={cols.get("geoid", "GEOID"): "GEOID"})
        if "population" not in gdf.columns:
            sys.exit("--acs-bg must have a 'population' column (or B01003_001E).")
        if gdf.crs is None:
            gdf = gdf.set_crs("EPSG:4326")
        return gdf.to_crs("EPSG:4326")
    return fetch_acs_bg(api_key, Path(ACS_BG_CACHE))


def stage_population(cells: pd.DataFrame, acs_path, api_key,
                     k: int, market_share: float, pop_floor: float) -> pd.DataFrame:
    import geopandas as gpd
    from scipy.spatial import cKDTree
    log.info("=== STAGE 3: POPULATION (ACS apportionment -> customers served) ===")

    bg = load_acs_bg(acs_path, api_key)
    log.info("[pop] %s block groups loaded", f"{len(bg):,}")

    valid = cells["latitude"].notna() & cells["longitude"].notna()
    tw = gpd.GeoDataFrame(
        cells.loc[valid, ["cell_global_id"]].copy(),
        geometry=gpd.points_from_xy(cells.loc[valid, "longitude"],
                                    cells.loc[valid, "latitude"]),
        crs="EPSG:4326",
    ).to_crs(EQUAL_AREA_CRS)
    tower_xy = np.column_stack([tw.geometry.x.values, tw.geometry.y.values])
    if len(tower_xy) == 0:
        sys.exit("[pop] No towers with valid coordinates to apportion to.")

    bg_p = bg.to_crs(EQUAL_AREA_CRS)
    cent = bg_p.geometry.representative_point()
    bg_xy = np.column_stack([cent.x.values, cent.y.values])
    bg_pop = pd.to_numeric(bg_p["population"], errors="coerce").fillna(0).clip(lower=0).values

    kk = max(1, k)
    log.info("[pop] KD-tree over %s towers; querying %s block groups (k=%d)",
             f"{len(tower_xy):,}", f"{len(bg_xy):,}", kk)
    tree = cKDTree(tower_xy)
    dist, idx = tree.query(bg_xy, k=kk)
    if kk == 1:
        idx = idx[:, None]

    served = np.zeros(len(tower_xy), dtype="float64")
    share = bg_pop / kk
    for j in range(kk):
        np.add.at(served, idx[:, j], share)
    served *= market_share
    served = np.maximum(served, pop_floor)

    out = cells.copy()
    out["estimated_population_served"] = pop_floor
    out.loc[valid, "estimated_population_served"] = served

    s = out["estimated_population_served"]
    at_floor = int((s <= pop_floor).sum())
    log.info("[pop] BG population total: %s", f"{float(bg_pop.sum()):,.0f}")
    log.info("[pop] served per tower  min=%.1f  median=%.1f  max=%.1f",
             s.min(), s.median(), s.max())
    log.info("[pop] towers at floor (%.0f): %s / %s (%.1f%%)",
             pop_floor, f"{at_floor:,}", f"{len(out):,}", 100 * at_floor / len(out))
    return out


# ============================================================================
# STAGE 4 -- CITY-LABEL: nearest populated place per tower
# ============================================================================

def load_city_ref(city_ref):
    if city_ref:
        p = Path(city_ref)
        df = pd.read_parquet(p) if p.suffix in (".parquet", ".pq") else pd.read_csv(p)
        cols = {c.lower(): c for c in df.columns}
        df = df.rename(columns={cols.get("city", "city"): "city",
                                cols.get("state", "state"): "state",
                                cols.get("lat", "lat"): "lat",
                                cols.get("lon", "lon"): "lon"})
        if "population" not in df.columns:
            df["population"] = 0
        return df[["city", "state", "lat", "lon", "population"]].dropna(subset=["lat", "lon"])

    cache = Path(CITY_CACHE)
    if cache.exists():
        log.info("[city] loading cached city reference %s", cache)
        return pd.read_parquet(cache)

    log.info("[city] building city reference from bundled sources")
    geo = pd.read_csv(io.BytesIO(urllib.request.urlopen(CITY_GEOMETRY_URL, timeout=60).read()))
    geo.columns = [c.strip() for c in geo.columns]
    geo = geo.rename(columns={"CITY": "city", "STATE_CODE": "state",
                              "LATITUDE": "lat", "LONGITUDE": "lon"})
    geo = geo[["city", "state", "lat", "lon"]].dropna(subset=["lat", "lon"])

    pop = pd.read_csv(io.BytesIO(urllib.request.urlopen(CITY_POP_URL, timeout=60).read()))
    pop["state"] = pop["State"].map(_STATE_NAME_TO_CODE)
    pop = pop.rename(columns={"City": "city", "Population": "population"})
    pop = pop[["city", "state", "population"]].dropna(subset=["state"])

    ref = geo.merge(pop, on=["city", "state"], how="left")
    ref["population"] = ref["population"].fillna(0).astype(int)
    try:
        ref.to_parquet(cache)
        log.info("[city]   cached %s (%s cities, %s with population)", cache,
                 f"{len(ref):,}", f"{int((ref['population'] > 0).sum()):,}")
    except Exception as e:
        log.warning("[city]   cache skipped (%s)", e)
    return ref


def stage_city_label(out: pd.DataFrame, city_ref_path) -> pd.DataFrame:
    import geopandas as gpd
    from scipy.spatial import cKDTree
    log.info("=== STAGE 4: CITY-LABEL (nearest populated place per tower) ===")

    city_ref = load_city_ref(city_ref_path)
    log.info("[city] %s cities in reference", f"{len(city_ref):,}")

    valid = out["latitude"].notna() & out["longitude"].notna()
    cpt = gpd.GeoDataFrame(
        city_ref.copy(),
        geometry=gpd.points_from_xy(city_ref["lon"], city_ref["lat"]),
        crs="EPSG:4326",
    ).to_crs(EQUAL_AREA_CRS)
    city_xy = np.column_stack([cpt.geometry.x.values, cpt.geometry.y.values])

    tpt = gpd.GeoDataFrame(
        out.loc[valid, ["cell_global_id", "state"]].copy(),
        geometry=gpd.points_from_xy(out.loc[valid, "longitude"], out.loc[valid, "latitude"]),
        crs="EPSG:4326",
    ).to_crs(EQUAL_AREA_CRS)
    tower_xy = np.column_stack([tpt.geometry.x.values, tpt.geometry.y.values])

    K = 5
    tree = cKDTree(city_xy)
    dist, idx = tree.query(tower_xy, k=K)
    if K == 1:
        dist = dist[:, None]; idx = idx[:, None]

    cities = city_ref.reset_index(drop=True)
    c_name = cities["city"].values
    c_state = cities["state"].values

    tower_state = tpt["state"].values
    n = len(tower_xy)
    pick_name = np.empty(n, dtype=object)
    pick_state = np.empty(n, dtype=object)
    pick_km = np.empty(n, dtype="float64")

    for i in range(n):
        nearest_j = idx[i, 0]
        nearest_d = dist[i, 0]
        chosen, chosen_d = nearest_j, nearest_d
        ts = tower_state[i]
        if ts is not None and c_state[nearest_j] != ts:
            for r in range(K):
                j = idx[i, r]
                if c_state[j] == ts and dist[i, r] - nearest_d <= CITY_STATE_MARGIN_M:
                    chosen, chosen_d = j, dist[i, r]
                    break
        pick_name[i] = c_name[chosen]
        pick_state[i] = c_state[chosen]
        pick_km[i] = chosen_d / 1000.0

    out["nearest_city"] = pd.NA
    out["nearest_city_state"] = pd.NA
    out["nearest_city_km"] = np.nan
    out.loc[valid, "nearest_city"] = pick_name
    out.loc[valid, "nearest_city_state"] = pick_state
    out.loc[valid, "nearest_city_km"] = np.round(pick_km, 2)

    matched = int(valid.sum())
    in_state = int((out.loc[valid, "nearest_city_state"] == out.loc[valid, "state"]).sum())
    log.info("[city] labeled %s towers; %s (%.1f%%) in-state; median dist %.1f km",
             f"{matched:,}", f"{in_state:,}", 100 * in_state / max(matched, 1),
             float(np.nanmedian(pick_km)))
    return out


# ============================================================================
# STAGE 5 -- WRITE: shape + save
# ============================================================================

def stage_write(df: pd.DataFrame, out_path: Path) -> pd.DataFrame:
    log.info("=== STAGE 5: WRITE ===")
    out = df.copy()
    out["carrier_note"] = out.apply(
        lambda r: ATT_CODES.get((int(r["mcc"]), int(r["net"])), ""), axis=1)
    out["is_firstnet"] = out.apply(
        lambda r: (int(r["mcc"]), int(r["net"])) in FIRSTNET_CODES, axis=1)
    out["position_exact"] = out["changeable"] == 0

    cols = ["cell_global_id", "latitude", "longitude", "state", "state_name",
            "quadrant", "estimated_population_served", "nearest_city",
            "nearest_city_state", "nearest_city_km", "radio", "mcc", "mnc",
            "area", "cell", "samples", "range", "position_exact", "is_firstnet",
            "carrier_note"]
    out = out.rename(columns={"net": "mnc"})
    cols = [c for c in cols if c in out.columns]
    out = out[cols].sort_values(["state", "cell_global_id"],
                                na_position="last").reset_index(drop=True)

    if out_path.suffix in (".parquet", ".pq"):
        out.to_parquet(out_path, index=False)
        csv_path = out_path.with_suffix(".csv")
        out.to_csv(csv_path, index=False)
    else:
        out.to_csv(out_path, index=False)
        csv_path = None
        try:
            out.to_parquet(out_path.with_suffix(".parquet"), index=False)
        except Exception as e:
            log.warning("[write] parquet skipped (%s)", e)

    log.info("[write] %s (%s rows, %d columns)", out_path, f"{len(out):,}", len(out.columns))
    if csv_path:
        log.info("[write] %s", csv_path)
    return out


def report(df: pd.DataFrame):
    if df.empty:
        return
    log.info("=== SUMMARY: %s AT&T towers ===", f"{len(df):,}")
    log.info("by quadrant: %s", df["quadrant"].value_counts(dropna=False).to_dict())
    log.info("radio types: %s", df["radio"].value_counts(dropna=False).to_dict())
    if "estimated_population_served" in df.columns:
        tot = float(df["estimated_population_served"].sum())
        log.info("total estimated customers served (sum): %s", f"{tot:,.0f}")
    if "is_firstnet" in df.columns:
        log.info("FirstNet cells: %s", f"{int(df['is_firstnet'].sum()):,}")


# ============================================================================
# main
# ============================================================================

def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--csv", nargs="+", help="local OpenCellID file(s)/glob(s)")
    src.add_argument("--token", default=OPENCELLID_API_KEY,
                     help="OpenCellID token (downloads all US MCCs; "
                          "or set OPENCELLID_API_KEY)")
    ap.add_argument("--mccs", nargs="+", type=int, default=US_MCCS,
                    help=f"MCCs to download with --token (default {US_MCCS})")
    ap.add_argument("--states-shp", help="local Census state boundary zip/shp "
                    "(else auto-downloads cb_2024_us_state_500k.zip)")
    ap.add_argument("--acs-bg", help="prebuilt block-group population+geometry "
                    "(parquet/geojson: GEOID, population, geometry). "
                    "If omitted, pulled live via CENSUS_API_KEY.")
    ap.add_argument("--census-api-key", default=CENSUS_API_KEY,
                    help="Census API key (or set CENSUS_API_KEY env)")
    ap.add_argument("--k", type=int, default=K_NEAREST,
                    help=f"nearest towers a block group splits across (default {K_NEAREST})")
    ap.add_argument("--market-share", type=float, default=ATT_MARKET_SHARE,
                    help=f"AT&T share multiplier (default {ATT_MARKET_SHARE})")
    ap.add_argument("--pop-floor", type=float, default=POP_FLOOR,
                    help=f"floor served value for towers winning no BG (default {POP_FLOOR})")
    ap.add_argument("--city-ref", default=None,
                    help="city reference [city,state,lat,lon,population]; "
                         "if omitted, built from bundled sources and cached.")
    ap.add_argument("--no-population", action="store_true",
                    help="skip ACS population apportionment")
    ap.add_argument("--no-city", action="store_true",
                    help="skip nearest-city labeling")
    ap.add_argument("--out", default="att_us_dataset.parquet")
    ap.add_argument("-v", "--verbose", action="store_true", help="DEBUG logging")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stderr,
    )

    log.info("AT&T dataset build starting")

    # STAGE 1 -- extract
    df = stage_extract(args.csv, args.token, args.mccs)

    # STAGE 2 -- geo-label
    df = stage_geo_label(df, args.states_shp)

    # STAGE 3 -- population
    if args.no_population:
        log.info("=== STAGE 3: POPULATION skipped (--no-population) ===")
    else:
        df = stage_population(df, args.acs_bg, args.census_api_key,
                              args.k, args.market_share, args.pop_floor)

    # STAGE 4 -- city
    if args.no_city:
        log.info("=== STAGE 4: CITY-LABEL skipped (--no-city) ===")
    else:
        df = stage_city_label(df, args.city_ref)

    # STAGE 5 -- write
    out = stage_write(df, Path(args.out))
    report(out)
    log.info("Done.")


if __name__ == "__main__":
    main()
