// Check results (12 §2.7).
//
// The status comes straight from the API. This component contains NO threshold logic and
// does not re-derive a verdict: every verdict is decided once, in SQL, and recorded in
// the state store, which is what makes the dashboard, Slack, Jira and the PR comment all
// say the same thing (12 §7).

import { useState } from "react";
import type { Result } from "../api/client";
import { duration } from "../lib/format";

const ORDER: Record<string, number> = { FAIL: 0, INCONCLUSIVE: 1, UNVALIDATED: 2, PASS: 3 };

const STYLE: Record<string, string> = {
  PASS: "diag-ok",
  FAIL: "diag-error",
  INCONCLUSIVE: "diag-degraded",
  UNVALIDATED: "diag-skipped",
};

// What each status MEANS. Shown because "we couldn't tell" and "everything's fine" must
// never read as the same thing.
const MEANING: Record<string, string> = {
  PASS: "checked, no violations",
  FAIL: "violations found",
  INCONCLUSIVE: "we tried and could not tell — not a pass",
  UNVALIDATED: "no way to check this yet — not a pass",
};

export function ResultTable({ results }: { results: Result[] }) {
  const [expanded, setExpanded] = useState<string | null>(null);
  const [filter, setFilter] = useState<string>("all");

  const shown = results
    .filter((r) => filter === "all" || r.status === filter)
    .slice()
    .sort((a, b) => (ORDER[a.status] ?? 9) - (ORDER[b.status] ?? 9)
      || a.check_id.localeCompare(b.check_id));

  const tally = results.reduce<Record<string, number>>((acc, r) => {
    acc[r.status] = (acc[r.status] ?? 0) + 1;
    return acc;
  }, {});

  return (
    <section className="panel">
      <header className="panel-header">
        <h2>Check results</h2>
        <span className="filters">
          {["all", "FAIL", "INCONCLUSIVE", "UNVALIDATED", "PASS"].map((f) => (
            <button
              key={f}
              className={filter === f ? "chip chip-on" : "chip"}
              onClick={() => setFilter(f)}
            >
              {f === "all" ? `all ${results.length}` : `${f} ${tally[f] ?? 0}`}
            </button>
          ))}
        </span>
      </header>

      <table className="diag-table">
        <thead>
          <tr>
            <th>Status</th>
            <th>Check</th>
            <th>Compared</th>
            <th>Time</th>
          </tr>
        </thead>
        <tbody>
          {shown.map((r) => (
            <tr key={r.result_id} onClick={() => setExpanded(expanded === r.result_id ? null : r.result_id)}>
              <td>
                <span className={STYLE[r.status]}>{r.status}</span>
              </td>
              <td>
                <div className="mono small">{r.check_id.split(".")[0]}</div>
                <div className="muted small">{MEANING[r.status]}</div>
                {r.status === "FAIL" && (
                  <div className="fail-detail">{r.failing_row_count} violating row(s)</div>
                )}
                {r.error_message && <div className="consequence">{r.error_message}</div>}
                {expanded === r.result_id && (
                  <div className="expanded">
                    {/* Exclusions travel with the result, every time. A green result
                        without them is a lie of omission (11 §8). */}
                    <div>
                      <strong>compared:</strong>{" "}
                      <span className="mono small">
                        {r.columns_compared.join(", ") || "—"}
                      </span>
                    </div>
                    <div>
                      <strong>excluded:</strong>{" "}
                      <span className="mono small">
                        {r.columns_excluded.join(", ") || "none"}
                      </span>
                    </div>
                    <div>
                      <strong>read:</strong>{" "}
                      <span className="mono small">{r.scope_predicate ?? "—"}</span>
                    </div>
                    {r.sample_rows.length > 0 && (
                      <pre className="sample">
                        {JSON.stringify(r.sample_rows.slice(0, 3), null, 1)}
                      </pre>
                    )}
                  </div>
                )}
              </td>
              <td className="mono small">{r.columns_compared.length}</td>
              <td className="mono small">{r.duration_ms != null ? duration(r.duration_ms) : "—"}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {shown.length === 0 && <p className="muted">No results with that status.</p>}
    </section>
  );
}
