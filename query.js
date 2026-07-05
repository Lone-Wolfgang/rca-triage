/**
 * query.js — natural-language tower query for the RCA Triage dashboard.
 *
 * Design note (important): faults are NOT static. They are resampled in the
 * browser (resample.js) on every Resample, and impacted_customers shifts with
 * the per-class severity dials. So there is no static towers.sqlite to bake —
 * that would query the 184,920-cell POOL, not the live faults the user sees.
 *
 * Instead we build a tiny SQLite DB IN THE BROWSER from the current live fault
 * set (whatever faultsInView() returns) each time it changes, and run the
 * model-generated SELECT against that. The result is always consistent with the
 * markers, legend, hotspots, and impact panel — they all read the same set.
 *
 * Public API (wired from index.html):
 *   initQuery({ getFaults, impacted, onResult, endpoint })  -> sets up the panel
 *   refreshQueryDb()   -> call whenever the fault set changes (resample/filter/etc.)
 *
 * Pure-ish: it owns its panel DOM and the sql.js DB; it calls back into the app
 * via the callbacks passed to initQuery (no direct coupling to app internals).
 */

const SQLJS_CDN = "https://cdnjs.cloudflare.com/ajax/libs/sql.js/1.10.3";

// Guard: reject anything that isn't a single read-only SELECT. The DB is an
// in-memory throwaway rebuilt from live state, so the blast radius is nil — but
// a bad statement should fail loud and clean, not run.
const FORBIDDEN = /\b(DROP|DELETE|UPDATE|INSERT|ALTER|CREATE|ATTACH|DETACH|PRAGMA|REPLACE|VACUUM)\b/i;
function isSelectOnly(sql) {
  if (!sql || typeof sql !== "string") return false;
  const s = sql.trim();
  if (!/^select/i.test(s)) return false;
  if (FORBIDDEN.test(s)) return false;
  if (s.replace(/;\s*$/, "").includes(";")) return false; // one statement only
  return true;
}

let SQL = null;         // sql.js module (loaded once)
let db = null;          // current in-memory DB
let cfg = null;         // { getFaults, impacted, onResult, endpoint }
let lastSql = "";

// Load sql.js once. The wasm is fetched from the same CDN family the app already
// uses for Leaflet, so no new infra.
async function loadSqlJs() {
  if (SQL) return SQL;
  if (!window.initSqlJs) {
    await new Promise((res, rej) => {
      const s = document.createElement("script");
      s.src = `${SQLJS_CDN}/sql-wasm.min.js`;
      s.onload = res;
      s.onerror = () => rej(new Error("Could not load the query engine."));
      document.head.appendChild(s);
    });
  }
  SQL = await window.initSqlJs({ locateFile: (f) => `${SQLJS_CDN}/${f}` });
  return SQL;
}

// Rebuild the DB from the live fault set. Cheap: at most ~9,200 rows (5% of the
// pool), so a full teardown+rebuild on each change is well under a frame.
export function refreshQueryDb() {
  if (!SQL || !cfg) return; // engine not ready yet; will build on first run
  const faults = cfg.getFaults() || [];
  if (db) { db.close(); db = null; }
  db = new SQL.Database();

  db.run(`CREATE TABLE faults (
    cell_global_id TEXT, fault_class TEXT, latitude REAL, longitude REAL,
    state TEXT, state_name TEXT, region TEXT, nearest_city TEXT,
    population_served INTEGER, impacted_customers INTEGER
  );`);
  db.run(`CREATE TABLE cities (name TEXT, lat REAL, lon REAL);`);

  const ins = db.prepare(`INSERT INTO faults VALUES (?,?,?,?,?,?,?,?,?,?)`);
  const cityLat = new Map(), cityLon = new Map(), cityN = new Map();
  for (const f of faults) {
    const imp = cfg.impacted(f);
    ins.run([
      f.cell_global_id, f.answer, f.latitude, f.longitude,
      f.state, f.state_name, f.quadrant, f.nearest_city || "",
      Math.round(+f.estimated_population_served || 0), imp,
    ]);
    const c = f.nearest_city;
    if (c) {
      cityLat.set(c, (cityLat.get(c) || 0) + f.latitude);
      cityLon.set(c, (cityLon.get(c) || 0) + f.longitude);
      cityN.set(c, (cityN.get(c) || 0) + 1);
    }
  }
  ins.free();

  // cities = centroid of each city's live faults (keeps "near X" consistent
  // with what's actually on the map right now).
  const cins = db.prepare(`INSERT INTO cities VALUES (?,?,?)`);
  for (const [name, n] of cityN) {
    cins.run([name, cityLat.get(name) / n, cityLon.get(name) / n]);
  }
  cins.free();
}

// Run a natural-language question: translate -> guard -> execute -> callback.
async function runQuestion(question) {
  // Flush any prior query's overlay (dimming, ring, chart, answer) before this
  // one runs, so a new query always starts from a clean, unfiltered state — and
  // a failed/empty query never leaves the previous highlight stuck.
  if (cfg.onFlush) cfg.onFlush();

  setStatus("loading", "Translating…");
  hideSqlReveal();

  let sql;
  try {
    const r = await fetch(cfg.endpoint, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ question }),
    });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(data.error || `Translator error (${r.status}).`);
    sql = (data.sql || "").trim();
    cfg._lastAnswer = (data.answer || "").trim();
  } catch (e) {
    setStatus("error", e.message || "Couldn't reach the translator.");
    return;
  }

  if (!isSelectOnly(sql)) {
    setStatus("error", "Couldn't form a safe query — try rephrasing.");
    showSqlReveal(sql);
    return;
  }
  lastSql = sql;

  // Rebuild the DB fresh from the current live fault set on EVERY query, so no
  // state from a prior query carries over. (Previously only built when absent,
  // which meant the 2nd+ query ran against a stale DB.)
  refreshQueryDb();

  let rows;
  try {
    const res = db.exec(sql);
    rows = res.length ? res[0].values.map((v) => rowObj(res[0].columns, v)) : [];
  } catch (e) {
    setStatus("error", "That query didn't run — try rephrasing.");
    showSqlReveal(sql);
    return;
  }

  showSqlReveal(sql);
  const ids = new Set(rows.map((r) => r.cell_global_id));
  setStatus(
    rows.length ? "ok" : "empty",
    rows.length
      ? `${rows.length.toLocaleString()} tower${rows.length === 1 ? "" : "s"} matched`
      : "No matching towers — try a broader query."
  );

  // Hand the matched rows back to the app: it dims non-matches, fits bounds,
  // and draws the stacked bar. Passing rows (not just ids) lets the app chart
  // the fault-class blend without re-querying.
  cfg.onResult({ rows, ids, sql, answer: cfg._lastAnswer || "" });
}

function rowObj(cols, vals) {
  const o = {};
  for (let i = 0; i < cols.length; i++) o[cols[i]] = vals[i];
  return o;
}

// ---- panel DOM ----
function setStatus(kind, msg) {
  const el = document.getElementById("q-status");
  if (!el) return;
  el.textContent = msg || "";
  el.dataset.kind = kind || "";
}
function showSqlReveal(sql) {
  const wrap = document.getElementById("q-sql-wrap");
  const code = document.getElementById("q-sql");
  if (!wrap || !code) return;
  code.textContent = sql || "";
  wrap.hidden = !sql;
}
function hideSqlReveal() {
  const wrap = document.getElementById("q-sql-wrap");
  if (wrap) wrap.hidden = true;
}

export async function initQuery(options) {
  cfg = Object.assign(
    { endpoint: "/.netlify/functions/nl2sql" },
    options
  );

  const form = document.getElementById("q-form");
  const input = document.getElementById("q-input");
  const clear = document.getElementById("q-clear");
  if (!form || !input) return;

  form.addEventListener("submit", (e) => {
    e.preventDefault();
    const q = input.value.trim();
    if (q) runQuestion(q);
  });
  if (clear) {
    clear.addEventListener("click", () => {
      input.value = "";
      setStatus("", "");
      hideSqlReveal();
      cfg.onClear && cfg.onClear();
    });
  }

  // Warm up the engine in the background so the first query is snappy.
  try {
    await loadSqlJs();
    refreshQueryDb();
  } catch (e) {
    setStatus("error", e.message || "Query engine unavailable.");
  }
}
