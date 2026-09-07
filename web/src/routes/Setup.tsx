// Connection health and the check registry.
//
// Each failing row says what it COSTS you, not just that it failed — an unreachable
// source blocks one comparison and leaves everything else working, and a panel that did
// not say so would read as "the tool is down".

import { useQuery } from "@tanstack/react-query";
import { api } from "../api/client";
import { Card, Notice, Pill, Spinner, type Tone } from "../components/ui";
import { describe } from "./Health";

const STATE_TONE: Record<string, Tone> = { ok: "ok", error: "bad", degraded: "warn", skipped: "mute" };
const STATE_WORD: Record<string, string> = {
  ok: "working", error: "broken", degraded: "limited", skipped: "not used",
};

export function Setup({ project }: { project: string }) {
  const diag = useQuery({
    queryKey: ["diagnostics", project],
    queryFn: () => api.getDiagnostics(project),
    refetchInterval: 60_000,
  });
  const checks = useQuery({ queryKey: ["checks", project], queryFn: () => api.listChecks(project) });

  const list = checks.data ?? [];
  const activatable = list.filter((c) => c.activatable).length;

  return (
    <>
      <div className="page-head">
        <div className="page-head-text">
          <h1>Setup</h1>
          <p className="page-sub">Connections, and the checks that exist for this project.</p>
        </div>
      </div>

      <Card title="Connections" action={
        diag.isFetching ? <Spinner /> : <span className="muted small">rechecks every minute</span>
      }>
        {diag.isLoading && <Spinner label="testing connections…" />}
        {diag.data && (
          <ul className="rows" style={{ margin: "calc(var(--s5) * -1)" }}>
            {diag.data.results.map((r) => (
              <li key={r.name} className="row">
                <Pill tone={STATE_TONE[r.state] ?? "mute"}>{STATE_WORD[r.state] ?? r.state}</Pill>
                <div className="row-main">
                  <div className="row-title">{friendly(r.name)}</div>
                  <div className="row-sub mono small">{r.detail}</div>
                  {r.consequence && (
                    <div className="row-sub"><strong>What this costs you:</strong> {r.consequence}</div>
                  )}
                </div>
                <div className="row-aside">{r.duration_ms ? `${(r.duration_ms / 1000).toFixed(1)}s` : ""}</div>
              </li>
            ))}
          </ul>
        )}
      </Card>

      {activatable === 0 && list.length > 0 && (
        <Notice tone="warn" title="No check can be switched on yet — and that is deliberate">
          A check has to be <em>proven able to fail</em> before it is trusted: clone the table,
          introduce the exact bug it exists to catch, and confirm it goes red. That proving
          step is not built yet, so every check here runs and reports but none can be
          activated. A check that can never fail is worse than no check — it manufactures
          confidence.
        </Notice>
      )}

      <Card tight title={`Checks (${list.length})`}>
        {list.length === 0 ? (
          <div className="card-body"><p className="muted">No checks recorded. Run them once first.</p></div>
        ) : (
          <ul className="rows">
            {list.map((c) => {
              const failed = Object.entries(c.gates).filter(([, ok]) => !ok).map(([g]) => g);
              return (
                <li key={c.check_id} className="row">
                  <div className="row-main">
                    <div className="row-title">{describe(c.check_id)}</div>
                    <div className="row-sub mono small">{c.table_name}</div>
                  </div>
                  <div className="row-aside">
                    <div className="btn-row" style={{ justifyContent: "flex-end" }}>
                      {Object.entries(c.gates).map(([g, ok]) => (
                        <span key={g} className={`pill pill-${ok ? "ok" : "mute"}`} title={gateHelp(g)}>
                          {ok ? "✓" : "·"} {g.replace(/_/g, " ")}
                        </span>
                      ))}
                      <button className="btn btn-sm" disabled={!c.activatable}
                        title={c.activatable ? "All four proofs passed"
                          : `Cannot switch on: ${failed.map((f) => f.replace(/_/g, " ")).join(", ")} not proven`}>
                        Switch on
                      </button>
                    </div>
                  </div>
                </li>
              );
            })}
          </ul>
        )}
      </Card>
    </>
  );
}

function friendly(name: string): string {
  const map: Record<string, string> = {
    "snowflake.connection": "Snowflake — can we connect?",
    "snowflake.grants": "Snowflake — do we have the right permissions, and only those?",
    "state.migrations": "Our own record-keeping tables",
    "rds.connection": "Source database — can we connect?",
    "rds.grants": "Source database — are we genuinely read-only?",
    "repo.git": "Your pipeline code repository",
    "llm.reachable": "AI service (optional)",
  };
  return map[name] ?? name;
}

function gateHelp(gate: string): string {
  const map: Record<string, string> = {
    budget: "Proven to run within its time and cost limit",
    passes_on_good: "Proven to pass on data we believe is correct — and to have actually looked at rows",
    mutation: "Proven to FAIL when the exact bug it hunts is introduced. The important one.",
    deterministic: "Proven to give the same answer twice on unchanging data",
  };
  return map[gate] ?? gate;
}
