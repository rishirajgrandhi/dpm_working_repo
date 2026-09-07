// All results, in plain language, filterable.
//
// Every row says what it MEANS, not just its status code. Expanding one shows what was
// compared, what was deliberately ignored, and exactly which rows were read — because
// exclusions travel with the result or the result is a half-truth.

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "../api/client";
import { Card, Empty, Notice, Pill, Spinner, STATUS_MEANS, STATUS_TONE } from "../components/ui";
import { describe } from "./Health";

const ORDER = ["FAIL", "INCONCLUSIVE", "UNVALIDATED", "PASS"];

export function Results({ project }: { project: string }) {
  const [filter, setFilter] = useState("all");
  const [open, setOpen] = useState<string | null>(null);
  const latest = useQuery({
    queryKey: ["latest", project],
    queryFn: () => api.latestRun(project),
    retry: false,
  });

  if (latest.isLoading) return <Spinner label="loading results…" />;
  if (latest.error || !latest.data) {
    return <Empty title="No checks have run yet">
      Go to Health and press <strong>Run checks</strong>.
    </Empty>;
  }
  const results = latest.data.results;
  const tally = results.reduce<Record<string, number>>((a, r) => {
    a[r.status] = (a[r.status] ?? 0) + 1; return a;
  }, {});
  const shown = results
    .filter((r) => filter === "all" || r.status === filter)
    .sort((a, b) => ORDER.indexOf(a.status) - ORDER.indexOf(b.status)
      || a.check_id.localeCompare(b.check_id));

  return (
    <>
      <div className="page-head">
        <div className="page-head-text">
          <h1>Results</h1>
          <p className="page-sub">{results.length} checks from the last run. Click any row for detail.</p>
        </div>
      </div>

      <Card tight title="Checks" action={
        <div className="chips">
          {["all", ...ORDER].map((f) => (
            <button key={f} className="chip" aria-pressed={filter === f} onClick={() => setFilter(f)}>
              {f === "all" ? `all ${results.length}` : `${f.toLowerCase()} ${tally[f] ?? 0}`}
            </button>
          ))}
        </div>
      }>
        {shown.length === 0 ? (
          <div className="card-body"><p className="muted">Nothing with that status.</p></div>
        ) : (
          <ul className="rows">
            {shown.map((x) => (
              <li key={x.result_id} className="row clickable"
                onClick={() => setOpen(open === x.result_id ? null : x.result_id)}>
                <Pill tone={STATUS_TONE[x.status] ?? "mute"}>
                  {x.status === "PASS" ? "fine" : x.status === "FAIL" ? "problem"
                    : x.status === "INCONCLUSIVE" ? "unknown" : "not run"}
                </Pill>
                <div className="row-main">
                  <div className="row-title">{describe(x.check_id)}</div>
                  <div className="row-sub">
                    {STATUS_MEANS[x.status]}
                    {x.status === "FAIL" && ` · ${x.failing_row_count} row(s) affected`}
                  </div>
                  {x.error_message && <div className="row-sub">{x.error_message}</div>}
                  {open === x.result_id && (
                    <div className="detail">
                      <div>
                        <div className="detail-k">columns compared</div>
                        <span className="mono small">{x.columns_compared.join(", ") || "—"}</span>
                      </div>
                      <div>
                        <div className="detail-k">deliberately ignored</div>
                        <span className="mono small">{x.columns_excluded.join(", ") || "none"}</span>
                      </div>
                      <div>
                        <div className="detail-k">rows it read</div>
                        <span className="mono small">{x.scope_predicate ?? "—"}</span>
                      </div>
                      <div>
                        <div className="detail-k">check id</div>
                        <span className="mono small">{x.check_id}</span>
                      </div>
                      {x.sample_rows.length > 0 && (
                        <div>
                          <div className="detail-k">examples of what is wrong</div>
                          <pre className="code">{JSON.stringify(x.sample_rows.slice(0, 3), null, 1)}</pre>
                        </div>
                      )}
                    </div>
                  )}
                </div>
                <div className="row-aside">{x.duration_ms != null ? `${(x.duration_ms / 1000).toFixed(1)}s` : "—"}</div>
              </li>
            ))}
          </ul>
        )}
      </Card>

      {(tally.UNVALIDATED ?? 0) > 0 && (
        <Notice tone="warn" title={`${tally.UNVALIDATED} checks are waiting on a person`}>
          These are recorded but cannot run. Usually it is a grain or a de-duplication rule
          nobody has confirmed — get that wrong and the check passes while comparing
          nothing, which is worse than not having it.
        </Notice>
      )}
    </>
  );
}
