// What is covered, and — the point of the page — what is not.

import { useQuery } from "@tanstack/react-query";
import { api } from "../api/client";
import { Card, Empty, Pill, Spinner } from "../components/ui";

const LAYERS = ["bronze", "silver", "gold"];

export function CoveragePage({ project }: { project: string }) {
  const latest = useQuery({
    queryKey: ["latest", project], queryFn: () => api.latestRun(project), retry: false,
  });
  if (latest.isLoading) return <Spinner label="loading coverage…" />;
  if (latest.error || !latest.data) {
    return <Empty title="No coverage recorded yet">Run the checks first.</Empty>;
  }
  const cov = latest.data.coverage;
  const covered = cov.filter((c) => !c.unvalidated).length;
  const pct = cov.length ? Math.round((covered / cov.length) * 100) : 0;

  return (
    <>
      <div className="page-head">
        <div className="page-head-text">
          <h1>Coverage</h1>
          <p className="page-sub">
            {covered} of {cov.length} tables have at least one working check.
            Coverage never reaches 100%, and this page says so rather than rounding up.
          </p>
        </div>
      </div>

      <Card title="How much is actually checked">
        <div className="bar" style={{ marginBottom: "var(--s2)" }}>
          <div className={`bar-fill${pct < 100 ? " is-warn" : ""}`} style={{ width: `${pct}%` }} />
        </div>
        <p className="muted small">
          {pct}% of watched tables have a check that ran and produced a verdict. A check that
          could not run does not count as coverage.
        </p>
      </Card>

      {LAYERS.filter((l) => cov.some((c) => c.layer === l)).map((layer) => (
        <Card key={layer} tight title={layer}>
          <ul className="rows">
            {cov.filter((c) => c.layer === layer).map((c) => (
              <li key={c.table_name} className="row">
                {c.unvalidated ? <Pill tone="warn">not covered</Pill> : <Pill tone="ok">covered</Pill>}
                <div className="row-main">
                  <div className="row-title mono small">{c.table_name}</div>
                  <div className="row-sub">
                    {c.checks_active} of {c.checks_expected} checks ran
                    {c.hop_relation && ` · ${c.hop_relation.replace(/_/g, " ")}`}
                  </div>
                  {!c.grain_confirmed && (
                    <div className="row-sub">
                      Nobody has confirmed what makes a row unique here, so its checks cannot run.
                    </div>
                  )}
                  {c.unverified_measures.length > 0 && (
                    <div className="row-sub">
                      Cannot be recomputed and so cannot be verified:{" "}
                      <span className="mono">{c.unverified_measures.join(", ")}</span>
                    </div>
                  )}
                </div>
              </li>
            ))}
          </ul>
        </Card>
      ))}
    </>
  );
}
