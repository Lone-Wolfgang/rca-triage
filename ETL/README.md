# AT&T Tower Dataset for RCA-Triage Simulation

A national map of AT&T cell towers, where every tower is labeled with an estimate
of **how many customers it serves**.

Tower locations come from [OpenCellID](https://opencellid.org/) (broadcast MCC/MNC,
so a tower counts as AT&T by its radio identity, not by who owns the mast). Census
ACS block-group population is then apportioned to the nearest tower(s) and scaled by
AT&T's market share to produce a per-tower `estimated_population_served`. Each tower
is also tagged with its US state, Census quadrant, and nearest city.

The customer-weighting is what makes this dataset useful: it lets a fault simulation
tie **root cause analysis to customer impact** — when a tower (or a cluster of them)
goes down, you can immediately estimate how many people are affected and rank
hotspots by population, not just by tower count.

> `estimated_population_served` is a rough proxy, not real subscriber data. It counts
> residents (not commuter load), uses a flat national market-share assumption, and
> apportions by nearest tower. Treat it as relative customer weight, not a billed
> figure.

## Output columns

| column | meaning |
|---|---|
| `cell_global_id` | `MCC-MNC-AREA-CELL` composite cell identifier |
| `latitude`, `longitude` | decimal degrees |
| `state`, `state_name`, `quadrant` | point-in-polygon region labels (Census boundaries) |
| `estimated_population_served` | apportioned customer-weight estimate |
| `nearest_city`, `nearest_city_state`, `nearest_city_km` | nearest populated place |
| `radio` | GSM / UMTS / LTE (5G NR rides LTE; not a separate class) |
| `mcc`, `mnc`, `area`, `cell` | raw network identity |
| `samples`, `range` | OpenCellID measurement count and estimated range (m) |
| `position_exact` | `True` if GPS-precise, else averaged |
| `is_firstnet` | `True` for FirstNet (313-100) cells on AT&T's RAN |

---

# Quickstart

## API keys

| key | what it's for | get one |
|---|---|---|
| **OpenCellID token** | downloading the raw cell exports (skip if you pass local files with `--csv`) | https://opencellid.org/ — free, **2 downloads/token/day** |
| **Census API key** | pulling ACS block-group population live (skip if you pass a prebuilt file with `--acs-bg`) | https://api.census.gov/data/key_signup.html — free |

Set them as environment variables:

```bash
export OPENCELLID_API_KEY=...
export CENSUS_API_KEY=...
```

## Dependencies

```bash
pip install pandas pyarrow numpy requests geopandas shapely scipy pyproj
```

The Census **state** and **block-group** boundary files and the city reference list
auto-download on first run and are cached locally.

## Build the dataset

Because OpenCellID limits you to 2 downloads/token/day, the recommended path is to
download the per-MCC files once from opencellid.org, then point the script at them:

```bash
python build_att_dataset.py --csv 310.csv.gz 311.csv.gz 312.csv.gz \
                                  313.csv.gz 314.csv.gz 315.csv.gz 316.csv.gz
```

Or let it download everything for you (subject to the rate limit):

```bash
python build_att_dataset.py --token "$OPENCELLID_API_KEY"
```

Output: **`att_us_dataset.parquet`** (+ `.csv`). Every stage logs what it's doing and
the counts it produced, so a run is auditable from the console alone.

### Useful flags

```bash
--acs-bg acs_bg_pop.parquet    # use a prebuilt block-group pop file (no Census key needed)
--market-share 0.45            # AT&T share multiplier on apportioned population
--k 1                          # nearest towers a block group splits its population across
--no-population                # skip customer-weighting (geo + city labels only)
--no-city                      # skip nearest-city labeling
--out path.parquet             # output path
-v                             # verbose (DEBUG) logging
```

## Attribution

OpenCellID is **CC BY-SA 4.0**. Any product using this data must visibly credit
"OpenCelliD" with a link to https://opencellid.org/ and share derivatives under the
same license.
