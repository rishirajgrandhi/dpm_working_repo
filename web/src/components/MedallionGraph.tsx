// The medallion hop chain (12 §2.2, §7).
//
// A small custom renderer rather than a graph library: it is a fixed four-node chain per
// table, not a general graph, so a library is not worth the weight.

import type { HopSummary } from "../api/client";

const LAYERS = ["RDS", "BRONZE", "SILVER", "GOLD"] as const;
const HOP_ORDER = ["source_to_bronze", "bronze_to_silver", "silver_to_gold"] as const;

export function MedallionGraph({ hops }: { hops: HopSummary[] }) {
  return (
    <section className="panel">
      <header className="panel-header">
        <h2>Medallion</h2>
        <span className="muted">L1 is the only cross-engine hop</span>
      </header>

      <div className="medallion">
        {LAYERS.map((layer, index) => {
          const hopName = HOP_ORDER[index];
          const atHop = hopName ? hops.filter((h) => h.hop === hopName) : [];
          const unconfirmed = atHop.filter((h) => !h.contract_confirmed).length;
          return (
            <div className="medallion-step" key={layer}>
              <div className="layer">{layer}</div>
              {hopName && (
                <div className="hop">
                  <div className="hop-label">
                    L{index + 1} · lane {atHop[0]?.lane ?? "?"}
                  </div>
                  <div className="hop-arrow">→</div>
                  <div className="hop-detail">
                    {atHop.length} hop{atHop.length === 1 ? "" : "s"}
                    {unconfirmed > 0 && (
                      <span className="warn"> · {unconfirmed} contract unconfirmed</span>
                    )}
                  </div>
                </div>
              )}
            </div>
          );
        })}
      </div>

      <ul className="hop-list">
        {hops.map((hop) => (
          <li key={hop.id}>
            <span className="mono">{hop.id}</span>
            <span className="relation">{hop.relation}</span>
            <span className="muted">
              {hop.source} → {hop.target}
            </span>
            {!hop.contract_confirmed && <span className="warn">contract unconfirmed</span>}
            {hop.unverified_measures.length > 0 && (
              <span className="warn">
                unverified measures: {hop.unverified_measures.join(", ")}
              </span>
            )}
          </li>
        ))}
      </ul>
    </section>
  );
}
