"""
OPTIONAL / STANDALONE — not needed to run or deploy the live app.
Only used to rebuild pool.json from att_us_labeled.parquet, which lives outside this repo.

export_pool.py — turn the labeled cell parquet into the browser pool for the live app.

Reads att_us_labeled.parquet (184,920 AT&T cells) and writes a COLUMNAR JSON pool:
parallel arrays per field instead of array-of-objects, which roughly halves raw size
and parse time at this scale (~13 MB raw / ~3 MB gzipped vs ~35 MB / ~4.7 MB). The
live app fetches this once at startup; resample.js reads the { n, columns } shape
directly (see makePoolView there).

This is the LIVE-app analogue of build_map.py §3a — but it keeps the FULL pool (every
cell, so scope-to-state and per-region counts stay honest), not just faulted rows, and
it carries NO feature columns (the live app dropped them).

Usage:
    python export_pool.py --cells att_us_labeled.parquet --out pool.json
    python export_pool.py --cells att_us_labeled.parquet --out pool.json --gzip

Netlify serves .json gzipped on the wire automatically; --gzip additionally writes a
pool.json.gz you can inspect / serve directly. The --measure flag prints sizes and
exits without writing, so you can confirm the budget on your real data first.
"""

import argparse
import gzip
import json
import pathlib

import pandas as pd
from datasets import load_dataset

# Fields the browser needs. NO feature columns — the live app carries only the
# class label, which resample.js assigns; it does not come from the pool.
POOL_FIELDS = [
    "cell_global_id",
    "latitude",
    "longitude",
    "state",
    "state_name",
    "quadrant",
    "radio",
    "samples",
    "is_firstnet",
    "estimated_population_served",
    "nearest_city",
]

COORD_DP = 4  # lat/lon rounding; 4dp ≈ 11 m, ample for marker placement

# Non-contiguous / non-state codes excluded when --conus is set (the live app
# locks to a Contiguous-US frame, so these would never be visible anyway).
NON_CONUS = {"AK", "HI", "PR", "GU", "VI", "AS", "MP"}


def build_columnar(df: pd.DataFrame, conus: bool = False) -> dict:
    missing = [c for c in POOL_FIELDS if c not in df.columns]
    if missing:
        raise SystemExit(
            f"Parquet missing required columns: {missing}\n"
            f"Present: {list(df.columns)}"
        )

    sub = df[POOL_FIELDS].copy()

    if conus:
        before = len(sub)
        sub = sub[~sub["state"].isin(NON_CONUS)]
        # also drop rows with no state label (unplaceable on the CONUS frame)
        sub = sub[sub["state"].notna() & (sub["state"].astype("string") != "")]
        print(f"CONUS filter: {before:,} -> {len(sub):,} cells "
              f"(dropped {before - len(sub):,})")

    # Round coordinates to shrink the payload (the only high-entropy numeric fields).
    for c in ("latitude", "longitude"):
        sub[c] = sub[c].round(COORD_DP)

    # Normalize dtypes to clean JSON: bool for is_firstnet, int for samples,
    # plain str for categoricals (avoids pandas NA / numpy scalar leakage).
    sub["is_firstnet"] = sub["is_firstnet"].fillna(False).astype(bool)
    sub["samples"] = sub["samples"].fillna(0).astype(int)
    # estimated customers served — round to whole, NaN -> 0
    sub["estimated_population_served"] = (
        sub["estimated_population_served"].fillna(0).round().astype(int)
    )
    for c in ("cell_global_id", "state", "state_name", "quadrant", "radio", "nearest_city"):
        sub[c] = sub[c].astype("string").fillna("")

    columns = {}
    for c in POOL_FIELDS:
        # tolist() yields native Python scalars -> json-serializable
        columns[c] = sub[c].tolist()

    # Region counts fall out for free; the choropleth needs only these, but we ship
    # them in the same file so the app makes one fetch. Counts are over the FULL pool.
    state_agg = sub.groupby("state").size().to_dict()
    quad_agg = sub.groupby("quadrant").size().to_dict()

    return {
        "n": int(len(sub)),
        "fields": POOL_FIELDS,
        "columns": columns,
        "state_agg": {k: int(v) for k, v in state_agg.items()},
        "quad_agg": {k: int(v) for k, v in quad_agg.items()},
        "meta": {
            "coord_dp": COORD_DP,
            "source": "att_us_labeled (real AT&T cell locations); "
            "fault→location pairing is FABRICATED — not real outage data",
        },
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="Export columnar browser pool from cells parquet.")
    ap.add_argument("--cells", help="att_us_labeled.parquet")
    ap.add_argument("--out", default="pool.json", help="output JSON path")
    ap.add_argument("--gzip", action="store_true", help="also write <out>.gz")
    ap.add_argument("--conus", action="store_true",
                    help="keep only the 48 contiguous states (drop AK/HI/PR/territories)")
    ap.add_argument("--measure", action="store_true",
                    help="print raw/gzip sizes and exit without writing")
    args = ap.parse_args()

    if args.cells:
        df = pd.read_parquet(args.cells)
    else:
        df = load_dataset("LoneWolfgang/att-tower-rca", split="train").to_pandas()
    bundle = build_columnar(df, conus=args.conus)
    blob = json.dumps(bundle, separators=(",", ":")).encode("utf-8")
    gz = gzip.compress(blob, compresslevel=6)

    print(f"cells: {bundle['n']:,}")
    print(f"raw:   {len(blob) / 1e6:.2f} MB")
    print(f"gzip:  {len(gz) / 1e6:.2f} MB")

    if args.measure:
        print("(--measure: nothing written)")
        return

    out = pathlib.Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(blob)
    print(f"wrote {out}")
    if args.gzip:
        gzpath = out.with_suffix(out.suffix + ".gz")
        gzpath.write_bytes(gz)
        print(f"wrote {gzpath}")


if __name__ == "__main__":
    main()