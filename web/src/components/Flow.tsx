// The medallion, drawn as the pipeline it is.
//
// Each arrow is a hop with a real status. An arrow whose contract nobody has confirmed
// is amber, not green: the checks on it are recorded but cannot run, and showing that
// as healthy would be the exact false comfort this tool exists to remove.

import type { Coverage, HopSummary } from "../api/client";
import { Pill } from "./ui";

const LAYERS = ["Source", "Bronze", "Silver", "Gold"] as const;
const HOPS = ["source_to_bronze", "bronze_to_silver", "silver_to_gold"] as const;
const HOP_LABEL = { source_to_bronze: "landing", bronze_to_silver: "cleaning", silver_to_gold: "modelling" };

export function Flow({ hops, coverage }: { hops: HopSummary[]; coverage: Coverage[] }) {
  const countIn = (layer: string) =>
    coverage.filter((c) => (c.layer ?? "").toLowerCase() === layer.toLowerCase()).length;

  return (
    <div className="flow">
      {LAYERS.map((layer, i) => {
        const hopName = HOPS[i];
        const atHop = hopName ? hops.filter((h) => h.hop === hopName) : [];
        const unconfirmed = atHop.filter((h) => !h.contract_confirmed).length;
        const tone = atHop.length === 0 ? "mute" : unconfirmed > 0 ? "warn" : "ok";
        const n = layer === "Source" ? null : countIn(layer);
        return (
          <div key={layer} style={{ display: "flex", flex: 1, minWidth: 0 }}>
            <div className="flow-node">
              <div className="flow-node-name">{layer.toUpperCase()}</div>
              <div className="flow-node-meta">
                {n === null ? "your database" : n === 0 ? "nothing watched" : `${n} table${n === 1 ? "" : "s"}`}
              </div>
            </div>
            {hopName && (
              <div className={`flow-edge is-${tone}`}>
                <span className="flow-edge-label">{HOP_LABEL[hopName]}</span>
                <span className="flow-edge-line" />
                <span className="xs">
                  {atHop.length === 0 ? (
                    <Pill tone="mute">not set up</Pill>
                  ) : unconfirmed > 0 ? (
                    <Pill tone="warn">{unconfirmed} unconfirmed</Pill>
                  ) : (
                    <Pill tone="ok">{atHop.length} checked</Pill>
                  )}
                </span>
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}
