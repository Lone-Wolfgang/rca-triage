// config.example.js — template for the runtime config the dashboard reads.
//
// The dashboard is a static page (served with `python -m http.server`), so it
// can't read environment variables itself. Instead, `gen-config.py` reads your
// environment (see env.example) and writes `config.js` from this same shape.
//
// Two ways to get a real config.js:
//   1) cp config.example.js config.js   and edit the values by hand, OR
//   2) set the env vars (see env.example) and run:  python gen-config.py
//
// config.js is gitignored — it may hold a real key, so it must never be
// committed. This template carries NO secrets and is safe to commit.

window.RCA_CONFIG = {
  // ---- Model connection (LiteLLM gateway, OpenAI-compatible) --------------
  // Same shape as telelogs_litellm_eval.py: a model NAME addressed through a
  // gateway BASE_URL with a KEY. If any of the three is blank, the dashboard
  // runs a connection probe on boot; if the probe fails, every model-dependent
  // UI element is simply not rendered — the map/hotspots/dials still work.
  model:    "",   // e.g. "databricks-claude-sonnet-4"
  baseUrl:  "",   // e.g. "https://<gateway-host>/v1"  (the OpenAI-compatible root)
  key:      "",   // gateway key — leave blank in the committed template

  // ---- Dashboard defaults ------------------------------------------------
  // Per-class defaults for the two 0..10 dials. Override any subset; classes
  // you omit fall back to `severityDefault` / `blendDefault`.
  severityDefault: 5,
  blendDefault:    5,
  severity: { C1:5, C2:5, C3:5, C4:5, C5:5, C6:5, C7:5, C8:5 },
  blend:    { C1:5, C2:5, C3:5, C4:5, C5:5, C6:5, C7:5, C8:5 },
};
