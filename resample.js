/**
 * resample.js — browser-side fault resampler for the live RCA triage dashboard.
 *
 * Runtime model (differs deliberately from the offline fault_engine/bind.py):
 *   - Saturation is FIXED at SATURATION_CEILING (0.05). The severity dial filters
 *     this set down by `threshold` on the frontend; it never resamples.
 *   - Each selected cell gets a `threshold` ~ Uniform[0, SATURATION_CEILING], so the
 *     dial is a pure comparison: a cell shows at dial value s iff threshold <= s.
 *   - No feature payload. A faulted cell carries only its CLASS LABEL (`answer`),
 *     drawn from the blend weights. (Synthetic per-class features, if ever wanted,
 *     are a separate additive module — not this one.)
 *   - Determinism is STATISTICAL, not byte-identical to the NumPy-based bind.py.
 *     Contract: same (seed, blend, pool) -> same output within this module, plus the
 *     distributional/structural invariants in resample.test.mjs.
 *
 * Borrows the cell-selection (uniform, without replacement) and blend (weighted class
 * choice) semantics of bind.py without mutating it; the offline pipeline still uses
 * bind.py as-is.
 *
 * Pure: no DOM, no fetch, no Leaflet. Data in, data out.
 */

export const SATURATION_CEILING = 0.05;
export const VALID_CLASSES = ["C1", "C2", "C3", "C4", "C5", "C6", "C7", "C8"];

// --------------------------------------------------------------------------
// RNG — splitmix32-dispersed seed feeding a mulberry32 stream. Integer seed
// maps deterministically to a float-in-[0,1) generator. No Math.random anywhere.
// --------------------------------------------------------------------------

export function makeRng(seed) {
  let s = seed >>> 0;
  return function next() {
    s = (s + 0x9e3779b9) | 0;
    let t = Math.imul(s ^ (s >>> 16), 0x21f0aaad);
    t = Math.imul(t ^ (t >>> 15), 0x735a2d97);
    t = t ^ (t >>> 15);
    return (t >>> 0) / 4294967296;
  };
}

function randInt(rng, n) {
  return Math.floor(rng() * n);
}

function randFloat(rng, lo, hi) {
  return lo + (hi - lo) * rng();
}

// --------------------------------------------------------------------------
// Blend (Knob 3) — normalize an eight-class weight object to a probability
// array aligned with VALID_CLASSES. `"uniform"` / null => equal 1/8 each.
// All eight classes always span the result; weights are adjustable per class.
// --------------------------------------------------------------------------

function normalizeBlend(blend) {
  if (blend == null || blend === "uniform") {
    return VALID_CLASSES.map(() => 1 / VALID_CLASSES.length);
  }
  if (typeof blend !== "object") {
    throw new Error(`blend must be null, "uniform", or an object; got ${blend}`);
  }
  for (const k of Object.keys(blend)) {
    if (!VALID_CLASSES.includes(k)) {
      throw new Error(`blend references unknown class ${k}`);
    }
  }
  const weights = VALID_CLASSES.map((c) => {
    const w = c in blend ? blend[c] : 0;
    if (typeof w !== "number" || Number.isNaN(w)) {
      throw new Error(`blend weight for ${c} must be a number; got ${w}`);
    }
    if (w < 0) throw new Error(`blend weights must be non-negative; got ${w} for ${c}`);
    return w;
  });
  const total = weights.reduce((a, b) => a + b, 0);
  if (total <= 0) throw new Error(`blend weights must sum to > 0; got ${total}`);
  return weights.map((w) => w / total);
}

// Pick a class index by cumulative probability. `u` is a draw in [0,1).
function pickClass(probs, u) {
  let acc = 0;
  for (let i = 0; i < probs.length; i++) {
    acc += probs[i];
    if (u < acc) return i;
  }
  return probs.length - 1; // float-rounding guard
}

// --------------------------------------------------------------------------
// Cell selection — choose nFaults distinct indices from [0, poolLen) uniformly
// WITHOUT replacement, via a partial Fisher-Yates so cost is O(nFaults).
// --------------------------------------------------------------------------

function selectCellIndices(rng, poolLen, nFaults) {
  // Sparse swap map: avoids allocating a full poolLen array.
  const swapped = new Map();
  const get = (i) => (swapped.has(i) ? swapped.get(i) : i);
  const chosen = new Array(nFaults);
  for (let i = 0; i < nFaults; i++) {
    const j = i + randInt(rng, poolLen - i); // j in [i, poolLen)
    chosen[i] = get(j);
    swapped.set(j, get(i));
  }
  return chosen;
}

// --------------------------------------------------------------------------
// Pool accessor — accept either shape, present a uniform (length, getCell) view.
//
//   array-of-objects:  [ {cell_global_id, latitude, ...}, ... ]
//   columnar (export_pool.py output):
//       { n: <int>, columns: { cell_global_id: [...], latitude: [...], ... } }
//
// Columnar is what the export script ships (~3 MB gz at 184,920 cells); the
// array shape is kept for tests and small inline use. getCell(i) reconstructs a
// plain cell object on demand — only nFaults (~5%) cells are ever materialized,
// so the 95% nominal cells never become objects.
// --------------------------------------------------------------------------

function makePoolView(pool) {
  if (Array.isArray(pool)) {
    return { length: pool.length, getCell: (i) => pool[i] };
  }
  if (pool && typeof pool === "object" && pool.columns) {
    const cols = pool.columns;
    const names = Object.keys(cols);
    const n = typeof pool.n === "number" ? pool.n : (cols[names[0]]?.length ?? 0);
    return {
      length: n,
      getCell: (i) => {
        const cell = {};
        for (const name of names) cell[name] = cols[name][i];
        return cell;
      },
    };
  }
  throw new Error(
    "pool must be an array of cells or a columnar { n, columns } object"
  );
}

// --------------------------------------------------------------------------
// resample — the one call the frontend makes on a blend change.
//
//   pool  : array-of-objects OR columnar { n, columns } (see makePoolView).
//           Must carry at least cell_global_id, latitude, longitude, state,
//           state_name, quadrant, radio, samples, is_firstnet,
//           estimated_population_served. Not mutated.
//   blend : "uniform"/null, or { C1: w1, ..., C8: w8 } (missing keys => weight 0).
//   seed  : integer.
//
// Returns { faults: [...], meta: {...} }. Only the faulted set is returned; the
// nominal cells already live in `pool` on the frontend. Each faulted object is a
// shallow copy of its pool cell plus: is_faulted, answer, threshold, fault_uid.
//
// RNG draw order (part of the determinism contract — do not reorder):
//   1) class label per fault slot   2) cell indices   3) thresholds
// --------------------------------------------------------------------------

export function resample({ pool, blend = "uniform", seed = 0 } = {}) {
  const view = makePoolView(pool);
  const probs = normalizeBlend(blend);
  const rng = makeRng(seed);

  let nFaults = Math.round(SATURATION_CEILING * view.length);
  if (nFaults > view.length) {
    console.warn(`nFaults ${nFaults} exceeds pool ${view.length}; clamping.`);
    nFaults = view.length;
  }

  const meta = {
    nFaults,
    saturationCeiling: SATURATION_CEILING,
    seed,
    blend: blend == null ? "uniform" : blend,
  };
  if (nFaults === 0) return { faults: [], meta };

  // 1) class labels (Knob 3)
  const labels = new Array(nFaults);
  for (let i = 0; i < nFaults; i++) {
    labels[i] = VALID_CLASSES[pickClass(probs, rng())];
  }

  // 2) cell selection (uniform, without replacement)
  const idx = selectCellIndices(rng, view.length, nFaults);

  // 3) thresholds
  const faults = new Array(nFaults);
  for (let i = 0; i < nFaults; i++) {
    const cell = view.getCell(idx[i]);
    faults[i] = {
      ...cell,
      is_faulted: true,
      answer: labels[i],
      threshold: randFloat(rng, 0, SATURATION_CEILING),
      fault_uid: cell.cell_global_id,
    };
  }

  return { faults, meta };
}