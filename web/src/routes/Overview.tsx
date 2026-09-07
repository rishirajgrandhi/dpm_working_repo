// The landing page (12 §2.1).
//
// It opens on the honest view: open failures, the medallion, and a panel titled
// "what we are not checking" that is neither collapsible nor below the fold. Coverage
// never reaches 100%, and a dashboard that shows only green is the failure this product
// exists to end.

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "../api/client";
import { CoverageTable } from "../components/CoverageTable";
import { DiagnosticsPanel } from "../components/DiagnosticsPanel";
import { MedallionGraph } from "../components/MedallionGraph";
import { NotCheckedPanel } from "../components/NotCheckedPanel";
import { ResultTable } from "../components/ResultTable";
import { shortSha } from "../lib/format";

type Tab = "overview" | "results" | "coverage" | "checks" | "settings";

export function Overview({ project }: { project: string }) {
  const [tab, setTab] = useState<Tab>("overview");
  const queryClient = useQueryClient();

  const detail = useQuery({ queryKey: ["project", project], queryFn: () => api.getProject(project) });
  const latest = useQuery({
    queryKey: ["latest", project],
    queryFn: () => api.latestRun(project),
    retry: false,
  });
  const diagnostics = useQuery({
    queryKey: ["diagnostics", project],
    queryFn: () => api.getDiagnostics(project),
    refetchInterval: 60_000,
  });
  const checks = useQuery({ queryKey: ["checks", project], queryFn: () => api.listChecks(project) });

  const runNow = useMutation({
    mutationFn: () => api.triggerRun(project),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["latest", project] });
      queryClient.invalidateQueries({ queryKey: ["checks", project] });
    },
  });

  if (detail.isLoading) return <p className="page">Loading…</p>;
  if (detail.error) return <p className="page error-box">{String(detail.error)}</p>;
  const d = detail.data!;
  const run = latest.data;
  const counts = run?.counts ?? {};

  return (
    <main className="page">
      <header className="page-header">
        <h1>{d.project}</h1>
        {d.shadow_mode && (
          <span className="badge-shadow">
            SHADOW MODE — reporting only, filing nothing
          </span>
        )}
        <span className="muted mono">config {shortSha(d.config_sha, 8)}</span>
        <button
          className="btn"
          onClick={() => runNow.mutate()}
          disabled={runNow.isPending}
        >
          {runNow.isPending ? "Running…" : "Run checks now"}
        </button>
      </header>

      <nav className="tabs">
        {(["overview", "results", "coverage", "checks", "settings"] as Tab[]).map((t) => (
          <button key={t} className={tab === t ? "tab tab-on" : "tab"} onClick={() => setTab(t)}>
            {t}
          </button>
        ))}
      </nav>

      {runNow.error && <div className="error-box">{String(runNow.error)}</div>}

      {tab === "overview" && (
        <>
          <div className="tiles">
            <Tile
              label="failures"
              value={counts.FAIL ?? 0}
              tone={(counts.FAIL ?? 0) > 0 ? "bad" : "good"}
            />
            <Tile label="passing" value={counts.PASS ?? 0} />
            {/* Deliberately adjacent to the green number: an inconclusive check is not a
                pass, and showing only "passing" would imply coverage it does not have. */}
            <Tile
              label="inconclusive"
              value={counts.INCONCLUSIVE ?? 0}
              tone={(counts.INCONCLUSIVE ?? 0) > 0 ? "warn" : undefined}
            />
            <Tile
              label="unvalidated"
              value={counts.UNVALIDATED ?? 0}
              tone={(counts.UNVALIDATED ?? 0) > 0 ? "warn" : undefined}
            />
            <Tile label="checks known" value={checks.data?.length ?? 0} />
            <Tile
              label="activatable"
              value={checks.data?.filter((c) => c.activatable).length ?? 0}
              hint="all four gates passed"
            />
          </div>

          {run && (
            <p className="muted small mono">
              last run {run.run.run_id} · {run.run.status} · trigger {run.run.trigger_kind}
              {run.run.warehouse_seconds != null && ` · ${run.run.warehouse_seconds}s`}
            </p>
          )}
          {latest.error && (
            <p className="muted">No runs recorded yet. Press “Run checks now”.</p>
          )}

          <MedallionGraph hops={d.hops} />
          <NotCheckedPanel items={d.not_checked} />
        </>
      )}

      {tab === "results" &&
        (run ? <ResultTable results={run.results} /> : <p className="muted">No runs yet.</p>)}

      {tab === "coverage" &&
        (run ? (
          <CoverageTable coverage={run.coverage} />
        ) : (
          <p className="muted">No runs yet.</p>
        ))}

      {tab === "checks" && <ChecksTable checks={checks.data ?? []} />}

      {tab === "settings" &&
        (diagnostics.data ? (
          <DiagnosticsPanel data={diagnostics.data} />
        ) : (
          <p className="muted">Checking connections…</p>
        ))}
    </main>
  );
}

function Tile({
  label,
  value,
  tone,
  hint,
}: {
  label: string;
  value: number;
  tone?: "good" | "bad" | "warn";
  hint?: string;
}) {
  return (
    <div className={`tile${tone ? ` tile-${tone}` : ""}`}>
      <div className="tile-value">{value}</div>
      <div className="tile-label">{label}</div>
      {hint && <div className="tile-hint">{hint}</div>}
    </div>
  );
}

function ChecksTable({ checks }: { checks: import("../api/client").Check[] }) {
  return (
    <section className="panel">
      <header className="panel-header">
        <h2>Checks</h2>
        <span className="muted">
          Activate is disabled until all four gates pass
        </span>
      </header>
      <table className="diag-table">
        <thead>
          <tr>
            <th>Template</th>
            <th>Table</th>
            <th>Status</th>
            <th>Gates</th>
            <th />
          </tr>
        </thead>
        <tbody>
          {checks.map((c) => {
            const failed = Object.entries(c.gates)
              .filter(([, ok]) => !ok)
              .map(([g]) => g);
            return (
              <tr key={c.check_id}>
                <td className="mono small">{c.template}</td>
                <td className="mono small">{c.table_name}</td>
                <td className="mono small">{c.status}</td>
                <td className="mono small">
                  {Object.entries(c.gates).map(([g, ok]) => (
                    <span key={g} className={ok ? "diag-ok" : "diag-skipped"}>
                      {ok ? "✓" : "·"}
                      {g.slice(0, 4)}{" "}
                    </span>
                  ))}
                </td>
                <td>
                  {/* Disabled WITH the reason, before the click — not an error after it. */}
                  <button
                    className="btn btn-small"
                    disabled={!c.activatable}
                    title={
                      c.activatable
                        ? "All gates passed"
                        : `Cannot activate: failed gate(s) ${failed.join(", ")}. A check that has not been proven able to fail manufactures confidence.`
                    }
                  >
                    Activate
                  </button>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
      {checks.length === 0 && <p className="muted">No checks recorded yet. Run the checks first.</p>}
    </section>
  );
}
