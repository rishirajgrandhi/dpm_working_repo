// The landing page. It answers one question in one sentence: is my data OK?
//
// The previous version showed six equal tiles and left you to work it out. The number
// that matters is failures; everything else is context. And the caveat line is not
// optional — a headline of "nothing wrong" without "…but 6 checks could not run" is a
// half-truth, which is the failure mode this product exists to end.

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, type ProjectDetail } from "../api/client";
import { Flow } from "../components/Flow";
import { Card, Notice, Pill, Spinner, Stat } from "../components/ui";

export function Health({ project, onGo }: { project: string; onGo: (v: string) => void }) {
  const qc = useQueryClient();
  const detail = useQuery({ queryKey: ["project", project], queryFn: () => api.getProject(project) });
  const latest = useQuery({
    queryKey: ["latest", project],
    queryFn: () => api.latestRun(project),
    retry: false,
  });

  const run = useMutation({
    mutationFn: () => api.triggerRun(project),
    onSuccess: () => qc.invalidateQueries(),
  });

  if (detail.isLoading) return <Spinner label="loading project…" />;
  if (detail.error) return <Notice tone="bad" title="Could not load this project">{String(detail.error)}</Notice>;
  const d = detail.data as ProjectDetail;
  const r = latest.data;
  const c = r?.counts ?? {};
  const fail = c.FAIL ?? 0;
  const pass = c.PASS ?? 0;
  const cannot = (c.INCONCLUSIVE ?? 0) + (c.UNVALIDATED ?? 0);

  const tone = fail > 0 ? "bad" : cannot > 0 ? "warn" : pass > 0 ? "ok" : "mute";
  const headline =
    !r ? "No checks have run yet"
      : fail > 0 ? `${fail} problem${fail === 1 ? "" : "s"} found`
      : pass === 0 ? "Nothing could be checked"
      : "Nothing wrong in what was checked";

  const caveats: string[] = [];
  if (cannot > 0)
    caveats.push(`${cannot} check${cannot === 1 ? "" : "s"} could not run`);
  if (d.not_checked.length > 0) {
    const n = d.not_checked.length;
    caveats.push(`${n} thing${n === 1 ? " is" : "s are"} not covered at all`);
  }

  return (
    <>
      <div className="page-head">
        <div className="page-head-text">
          <h1>
            {d.project}
            {d.shadow_mode && <Pill tone="info" large>shadow mode</Pill>}
          </h1>
          <p className="page-sub">
            {d.tables} table{d.tables === 1 ? "" : "s"} watched · {d.hop_count} pipeline step
            {d.hop_count === 1 ? "" : "s"} · reporting only, nothing is filed anywhere
          </p>
        </div>
        <button className="btn btn-primary" disabled={run.isPending} onClick={() => run.mutate()}>
          {run.isPending ? <Spinner label="running…" /> : "Run checks"}
        </button>
      </div>

      {run.error && <Notice tone="bad" title="The run failed to start">{String(run.error)}</Notice>}

      <div className={`verdict is-${tone}`}>
        <div className="verdict-line">{headline}</div>
        {caveats.length > 0 && (
          <div className="verdict-caveat">
            …but {caveats.join(", and ")}. A green result only ever means “everything we
            looked at, on the columns we compared, is fine”.
          </div>
        )}
        {!r && (
          <div className="verdict-caveat">
            Press <strong>Run checks</strong> to look at your data for the first time.
          </div>
        )}
        {r && (
          <div className="verdict-meta">
            <span>last run {r.run.ended_at?.slice(0, 16) ?? "in progress"}</span>
            <span>took {r.run.warehouse_seconds ?? "—"}s</span>
            <span className="mono">{r.run.run_id.slice(0, 12)}</span>
          </div>
        )}
      </div>

      {r && (
        <div className="stats" style={{ marginBottom: "var(--s4)" }}>
          <Stat value={fail} label="problems" tone={fail > 0 ? "bad" : "ok"}
            hint={fail > 0 ? "look at these first" : "nothing found"} />
          <Stat value={pass} label="checked & fine" tone="ok" />
          <Stat value={c.INCONCLUSIVE ?? 0} label="couldn't tell"
            tone={(c.INCONCLUSIVE ?? 0) > 0 ? "warn" : undefined}
            hint="tried, but not a pass" />
          <Stat value={c.UNVALIDATED ?? 0} label="not checkable yet"
            tone={(c.UNVALIDATED ?? 0) > 0 ? "warn" : undefined}
            hint="waiting on a person" />
        </div>
      )}

      <Card title="Your pipeline">
        <Flow hops={d.hops} coverage={r?.coverage ?? []} />
      </Card>

      {fail > 0 && r && (
        <Card title="What is wrong"
          action={<button className="btn btn-sm" onClick={() => onGo("checks")}>See all results</button>}>
          <ul className="rows" style={{ margin: "calc(var(--s5) * -1)" }}>
            {r.results.filter((x) => x.status === "FAIL").map((x) => (
              <li key={x.result_id} className="row">
                <Pill tone="bad">problem</Pill>
                <div className="row-main">
                  <div className="row-title">{describe(x.check_id)}</div>
                  <div className="row-sub">
                    {x.failing_row_count} row{x.failing_row_count === 1 ? "" : "s"} affected ·{" "}
                    <span className="mono">{x.check_id.split(".")[0]}</span>
                  </div>
                </div>
              </li>
            ))}
          </ul>
        </Card>
      )}

      <Card title="What we are not checking" accent
        action={<span className="muted small">{d.not_checked.length} items</span>}>
        {d.not_checked.length === 0 ? (
          <p className="muted">
            Nothing outstanding. Coverage still is not a guarantee — it only ever means
            everything we looked at looked fine.
          </p>
        ) : (
          <ul className="rows" style={{ margin: "calc(var(--s5) * -1)" }}>
            {d.not_checked.map((n, i) => (
              <li key={`${n.subject}-${i}`} className="row">
                <div className="row-main">
                  <div className="row-title mono small">{n.subject}</div>
                  <div className="row-sub">{n.reason}</div>
                </div>
              </li>
            ))}
          </ul>
        )}
      </Card>
    </>
  );
}

/** Turn a template id into something a person can read. */
export function describe(checkId: string): string {
  const t = checkId.split(".")[0] ?? checkId;
  const map: Record<string, string> = {
    "scd/one_current_row_per_key": "Each customer should have exactly one current record",
    "scd/no_overlapping_ranges": "History periods should never overlap",
    "scd/no_gaps_in_history": "History should have no missing periods",
    "scd/well_formed_ranges": "Every history period should start before it ends",
    "scd/current_flag_agrees_with_range": "The “current” flag should match the dates",
    "scd/no_spurious_versions": "A new version should only appear when something changed",
    "scd/tracked_change_created_version": "A real change should create new history, not overwrite it",
    "scd/type1_consistent_across_versions": "Overwrite-only fields should match across versions",
    "scd/surrogate_key_unique": "Internal row ids should be unique",
    "scd/closed_versions_immutable": "Finished history should never be edited afterwards",
    "layer/dedup_key_unique": "Duplicates should not survive de-duplication",
    "layer/dedup_pick_rule_fidelity": "The *right* duplicate should survive",
    "layer/dedup_pick_rule_total": "Duplicate picking should never be a coin flip",
    "layer/no_invented_keys": "No records should appear out of nowhere",
    "layer/row_conservation": "Every row should be accounted for",
    "layer/reject_reasons_closed": "Every rejected row should have a known reason",
    "layer/dim_key_coverage": "Every customer should reach the final table",
    "layer/measure_conservation": "Totals should survive the pipeline",
    "layer/fk_resolves_as_of": "Orders should point at who the customer was at the time",
    "layer/fanout_guard": "A join should not multiply rows",
    "layer/aggregate_of": "Summary tables should add up from their source",
    "keys/grain_unique": "Each row should be unique",
    "keys/key_not_null": "Key columns should never be empty",
  };
  return map[t] ?? t;
}
