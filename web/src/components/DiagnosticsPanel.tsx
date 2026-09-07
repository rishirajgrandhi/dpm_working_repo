// The doctor panel (12 §2.10). M0's exit criterion: every connection healthy, or a
// precise error.
//
// The `consequence` line is why this is more than a status list: a red row tells you
// what it costs you, so the reader knows whether to act now or at leisure. An RDS
// failure blocks Lane B and leaves Lane A entirely unaffected (03 §8), and a panel that
// did not say so would read as "the tool is down".

import type { CheckState, Diagnostics } from "../api/client";

const STATE_LABEL: Record<CheckState, string> = {
  ok: "OK",
  degraded: "DEGRADED",
  error: "ERROR",
  skipped: "SKIPPED",
};

// Presentation only — the state itself is decided by the API, never re-derived here.
const STATE_STYLE: Record<CheckState, string> = {
  ok: "diag-ok",
  degraded: "diag-degraded",
  error: "diag-error",
  skipped: "diag-skipped",
};

export function DiagnosticsPanel({ data }: { data: Diagnostics }) {
  return (
    <section className="panel">
      <header className="panel-header">
        <h2>Connections</h2>
        <span className="muted">
          dphm {data.tool_version} · {data.healthy ? "all healthy" : "attention needed"}
        </span>
      </header>

      <table className="diag-table">
        <thead>
          <tr>
            <th>Check</th>
            <th>State</th>
            <th>Detail</th>
          </tr>
        </thead>
        <tbody>
          {data.results.map((result) => (
            <tr key={result.name}>
              <td className="mono">{result.name}</td>
              <td>
                <span className={STATE_STYLE[result.state]}>{STATE_LABEL[result.state]}</span>
              </td>
              <td>
                <div className="mono small">{result.detail}</div>
                {result.consequence && (
                  <div className="consequence">Consequence: {result.consequence}</div>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>

      {data.pending_confirmations.length > 0 && (
        <div className="pending">
          <h3>Waiting on a human</h3>
          {/* Not an error — the honest state of a project mid-onboarding. A wrong grain
              produces a check that passes while comparing nothing, so these block
              activation by design (B3/R4). */}
          <ul>
            {data.pending_confirmations.map((item) => (
              <li key={item}>{item}</li>
            ))}
          </ul>
        </div>
      )}
    </section>
  );
}
