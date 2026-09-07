// Per-layer coverage (12 §2.5, 07B §9).
//
// Coverage will never reach 100%, and this table reports that rather than rounding it up.
// An UNVALIDATED or INCONCLUSIVE check is NOT coverage.

import type { Coverage } from "../api/client";

export function CoverageTable({ coverage }: { coverage: Coverage[] }) {
  const byLayer = coverage.reduce<Record<string, Coverage[]>>((acc, c) => {
    const key = c.layer ?? "other";
    (acc[key] ??= []).push(c);
    return acc;
  }, {});
  const order = ["bronze", "silver", "gold", "other"];

  return (
    <section className="panel">
      <header className="panel-header">
        <h2>Coverage</h2>
        <span className="muted">what we check, and what we cannot</span>
      </header>
      {order
        .filter((l) => byLayer[l]?.length)
        .map((layer) => (
          <div key={layer} className="cov-layer">
            <h3>{layer}</h3>
            <table className="diag-table">
              <thead>
                <tr>
                  <th>Table</th>
                  <th>Relation</th>
                  <th>Checks</th>
                  <th>Contract</th>
                  <th>Grain</th>
                </tr>
              </thead>
              <tbody>
                {byLayer[layer]!.map((c) => (
                  <tr key={c.table_name}>
                    <td className="mono small">
                      {c.table_name}
                      {c.unvalidated && <span className="warn">unvalidated</span>}
                      {c.unverified_measures.length > 0 && (
                        <div className="consequence">
                          measures that cannot be recomputed:{" "}
                          {c.unverified_measures.join(", ")}
                        </div>
                      )}
                    </td>
                    <td className="mono small">{c.hop_relation ?? "—"}</td>
                    <td className="mono small">
                      {c.checks_active}/{c.checks_expected}
                    </td>
                    <td>{c.contract_confirmed ? "✓" : <span className="warn">unconfirmed</span>}</td>
                    <td>{c.grain_confirmed ? "✓" : <span className="warn">unconfirmed</span>}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ))}
    </section>
  );
}
