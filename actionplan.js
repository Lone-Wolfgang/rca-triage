/**
 * actionplan.js — deterministic Hot Spot Action Plan builder.
 *
 * Structure is OWNED BY CODE (per action-plan-feature.md): the class→phase map,
 * dispatch type, per-phase site/customer rollups, and the recalibration flag are
 * all computed here so the numbers can never drift or hallucinate. The model
 * only writes the surrounding prose + labor/resource suggestions, given this
 * structured skeleton (see nl2sql.js "plan" mode).
 *
 * Phase model (fixed):
 *   Phase 1 Coverage & Geometry   — C1,C2 — FIELD dispatch (truck roll)
 *   Phase 2 Interference & Mobility— C4,C6,C5,C3 — REMOTE / NOC
 *   Phase 3 Throughput & Validation— C8,C7 — REMOTE / NOC
 * Recalibration: any Phase 2/3 item is flagged "recalibrate after coverage
 * fixes" IFF the hotspot also contains at least one Phase 1 fault.
 */

export const CLASS_PHASE = {
  C1: 1, C2: 1,
  C4: 2, C6: 2, C5: 2, C3: 2,
  C8: 3, C7: 3,
};

// Short human action verb per class (from the spec's label map).
export const CLASS_ACTION = {
  C1: "reduce downtilt",
  C2: "correct overshoot",
  C3: "tune neighbor list",
  C4: "resolve co-frequency overlap",
  C5: "damp handover churn",
  C6: "resolve PCI collision",
  C7: "re-validate (vehicle speed)",
  C8: "tune scheduler / RBs",
};

export const PHASE_META = {
  1: { name: "Coverage & Geometry",        dispatch: "field",  icon: "🚚", noc: false },
  2: { name: "Interference & Mobility",    dispatch: "remote", icon: "📡", noc: true  },
  3: { name: "Throughput Tuning & Validation", dispatch: "remote", icon: "📡", noc: true },
};

/**
 * Build the structured plan from a hotspot's member faults.
 * @param faults array of fault objects (need: answer/fault_class, cell_global_id,
 *               nearest_city, impacted-customer value via `impacted(f)`).
 * @param impacted (f)=>number — the app's impactedCustomers, passed in so the
 *               numbers match every other panel exactly.
 * @returns { phases:[{phase,name,dispatch,icon,sites,customers,recalibrate,items:[...]}],
 *            totals:{sites,customers,dispatchSites}, classesPresent:[...] }
 */
export function buildActionPlan(faults, impacted) {
  const items = faults.map((f) => {
    const cls = f.fault_class || f.answer;
    return {
      id: f.cell_global_id,
      cls,
      phase: CLASS_PHASE[cls] || 3,
      action: CLASS_ACTION[cls] || "investigate",
      city: f.nearest_city || "",
      customers: impacted(f),
    };
  });

  const hasPhase1 = items.some((i) => i.phase === 1);

  const byPhase = { 1: [], 2: [], 3: [] };
  for (const it of items) byPhase[it.phase].push(it);

  const phases = [1, 2, 3]
    .map((p) => {
      const list = byPhase[p].sort((a, b) => b.customers - a.customers);
      if (!list.length) return null;
      const meta = PHASE_META[p];
      const recalibrate = p !== 1 && hasPhase1; // spec: P2/P3 flagged iff a P1 exists
      return {
        phase: p,
        name: meta.name,
        dispatch: meta.dispatch,
        icon: meta.icon,
        recalibrate,
        sites: list.length,
        customers: list.reduce((s, i) => s + i.customers, 0),
        items: list.map((i) => ({
          id: i.id,
          cls: i.cls,
          action: i.action,
          city: i.city,
          customers: i.customers,
          recalibrate,
        })),
      };
    })
    .filter(Boolean);

  const totals = {
    sites: items.length,
    customers: items.reduce((s, i) => s + i.customers, 0),
    // "sites requiring dispatch" = Phase 1 only (the single number a planner cares about)
    dispatchSites: byPhase[1].length,
  };

  const classesPresent = [...new Set(items.map((i) => i.cls))].sort();

  return { phases, totals, classesPresent };
}

// Compact, model-friendly serialization of the plan skeleton. This is what we
// hand the LLM in "plan" mode; it narrates from this and must not change the
// numbers, phases, or ordering.
export function planForPrompt(plan, place) {
  const lines = [];
  lines.push(`HOTSPOT: ${place || "unnamed area"}`);
  lines.push(
    `TOTAL: ${plan.totals.sites} sites, ${plan.totals.customers.toLocaleString()} customers impacted, ${plan.totals.dispatchSites} sites need field dispatch.`
  );
  for (const ph of plan.phases) {
    lines.push(
      `\nPHASE ${ph.phase} — ${ph.name} [${ph.dispatch.toUpperCase()}] ` +
        `(${ph.sites} sites, ${ph.customers.toLocaleString()} customers)` +
        (ph.recalibrate ? " [recalibrate after Phase 1]" : "")
    );
    for (const it of ph.items) {
      lines.push(
        `  - ${it.cls} ${it.action}${it.city ? " @ " + it.city : ""} — ${it.customers.toLocaleString()} customers`
      );
    }
  }
  return lines.join("\n");
}
