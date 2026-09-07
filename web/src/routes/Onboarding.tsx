// The onboarding wizard (12 §2.9).
//
// Four steps: connect -> create -> discover -> choose. Each connection is TESTED before
// anything is saved, because a wizard that only reveals a bad password after writing
// everything is a wizard people abandon.
//
// Two rules this screen refuses to bend:
//   * a table's grain must be confirmed by a named person, or its checks cannot activate
//   * nothing the wizard guesses is treated as confirmed

import { useState } from "react";
import { api, type ConnectionTestResult, type DiscoveredTable } from "../api/client";

type Step = 1 | 2 | 3 | 4;

export function Onboarding({ onDone }: { onDone: (project: string) => void }) {
  const [step, setStep] = useState<Step>(1);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // step 1 — Snowflake
  const [sf, setSf] = useState({
    account: "",
    user: "",
    private_key_path: "~/.dphm/keys/dphm_dev_key.p8",
    warehouse: "DPHM_TEST_WH",
    database: "",
    role: "DPHM_DEV",
  });
  const [sfResult, setSfResult] = useState<ConnectionTestResult | null>(null);
  const [chosenSchemas, setChosenSchemas] = useState<string[]>([]);

  // step 1b — optional source database
  const [usePg, setUsePg] = useState(false);
  const [pg, setPg] = useState({ host: "", port: 5432, database: "", user: "", password: "" });
  const [pgResult, setPgResult] = useState<ConnectionTestResult | null>(null);

  // step 2 — project
  const [name, setName] = useState("");
  const [team, setTeam] = useState("@data-platform");
  const [created, setCreated] = useState<string | null>(null);
  const [warnings, setWarnings] = useState<string[]>([]);

  // step 3/4 — discovery
  const [tables, setTables] = useState<DiscoveredTable[]>([]);
  const [picked, setPicked] = useState<Record<string, boolean>>({});
  const [confirmer, setConfirmer] = useState("");
  const [notes, setNotes] = useState<string[]>([]);

  async function guard<T>(fn: () => Promise<T>): Promise<T | undefined> {
    setBusy(true);
    setError(null);
    try {
      return await fn();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      return undefined;
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="page">
      <header className="page-header">
        <h1>Connect a data source</h1>
        <span className="muted">step {step} of 4</span>
      </header>

      <div className="wizard-steps">
        {(["Connect", "Create project", "Discover tables", "Choose what to watch"] as const).map(
          (label, i) => (
            <div key={label} className={`wstep${step === i + 1 ? " wstep-on" : ""}${step > i + 1 ? " wstep-done" : ""}`}>
              <span className="wstep-num">{step > i + 1 ? "✓" : i + 1}</span> {label}
            </div>
          ),
        )}
      </div>

      {error && <div className="error-box">{error}</div>}

      {/* ── STEP 1 ─────────────────────────────────────────────────────── */}
      {step === 1 && (
        <section className="panel">
          <header className="panel-header">
            <h2>Snowflake</h2>
            <span className="muted">tested before anything is saved</span>
          </header>
          <div className="form">
            <Field label="Account identifier" hint="e.g. myorg-ab12345" value={sf.account}
              onChange={(v) => setSf({ ...sf, account: v })} />
            <Field label="User" value={sf.user} onChange={(v) => setSf({ ...sf, user: v })} />
            <Field label="Private key path" hint="key-pair auth; no passwords"
              value={sf.private_key_path} onChange={(v) => setSf({ ...sf, private_key_path: v })} />
            <Field label="Warehouse" value={sf.warehouse}
              onChange={(v) => setSf({ ...sf, warehouse: v })} />
            <Field label="Database" value={sf.database}
              onChange={(v) => setSf({ ...sf, database: v })} />
            <Field label="Role to read as" hint="checks run as this role"
              value={sf.role} onChange={(v) => setSf({ ...sf, role: v })} />
          </div>
          <button className="btn" disabled={busy || !sf.account || !sf.database}
            onClick={() => guard(async () => {
              const r = await api.testSnowflake(sf);
              setSfResult(r);
              if (r.ok) setChosenSchemas(r.schemas.filter((s) => !s.startsWith("DPHM_")));
            })}>
            {busy ? "Testing…" : "Test connection"}
          </button>

          {sfResult && (
            <div className={sfResult.ok ? "test-ok" : "test-bad"}>
              <strong>{sfResult.ok ? "Connected" : "Failed"}</strong> — {sfResult.detail}
              {sfResult.remedy && <div className="consequence">{sfResult.remedy}</div>}
            </div>
          )}

          {sfResult?.ok && sfResult.schemas.length > 0 && (
            <div className="pick-block">
              <h3>Which schemas should be monitored?</h3>
              <p className="muted small">
                The tool's own schemas are excluded automatically — monitoring them would
                report its own activity as pipeline change.
              </p>
              {sfResult.schemas.map((s) => (
                <label key={s} className="check-row">
                  <input type="checkbox" checked={chosenSchemas.includes(s)}
                    onChange={(e) => setChosenSchemas(e.target.checked
                      ? [...chosenSchemas, s] : chosenSchemas.filter((x) => x !== s))} />
                  <span className="mono">{s}</span>
                </label>
              ))}
            </div>
          )}

          <div className="pick-block">
            <label className="check-row">
              <input type="checkbox" checked={usePg} onChange={(e) => setUsePg(e.target.checked)} />
              <strong>I also have a source database (Postgres/RDS)</strong>
            </label>
            <p className="muted small">
              Optional. Without it, source-to-landing comparison is unavailable and reported
              as such; everything downstream still runs.
            </p>
            {usePg && (
              <>
                <div className="form">
                  <Field label="Host" value={pg.host} onChange={(v) => setPg({ ...pg, host: v })} />
                  <Field label="Port" value={String(pg.port)}
                    onChange={(v) => setPg({ ...pg, port: Number(v) || 5432 })} />
                  <Field label="Database" value={pg.database}
                    onChange={(v) => setPg({ ...pg, database: v })} />
                  <Field label="User" value={pg.user} onChange={(v) => setPg({ ...pg, user: v })} />
                  <Field label="Password" type="password" value={pg.password}
                    onChange={(v) => setPg({ ...pg, password: v })} />
                </div>
                <button className="btn" disabled={busy || !pg.host}
                  onClick={() => guard(async () => setPgResult(await api.testPostgres(pg)))}>
                  Test source connection
                </button>
                {pgResult && (
                  <div className={pgResult.ok ? "test-ok" : "test-bad"}>
                    <strong>{pgResult.ok ? "Connected" : "Failed"}</strong> — {pgResult.detail}
                    {pgResult.remedy && <div className="consequence">{pgResult.remedy}</div>}
                  </div>
                )}
              </>
            )}
          </div>

          <button className="btn btn-primary"
            disabled={!sfResult?.ok || chosenSchemas.length === 0 || (usePg && !pgResult?.ok)}
            onClick={() => setStep(2)}>
            Next
          </button>
        </section>
      )}

      {/* ── STEP 2 ─────────────────────────────────────────────────────── */}
      {step === 2 && (
        <section className="panel">
          <header className="panel-header"><h2>Name the project</h2></header>
          <div className="form">
            <Field label="Project name" hint="lower-case, digits, - and _ only"
              value={name} onChange={(v) => setName(v.toLowerCase().replace(/[^a-z0-9_-]/g, ""))} />
            <Field label="Owning team" value={team} onChange={setTeam} />
          </div>
          <p className="muted small">
            Created in <strong>shadow mode</strong>: checks run and report, but nothing is
            filed anywhere. Going live is a deliberate separate act.
          </p>
          <button className="btn btn-primary" disabled={busy || name.length < 2}
            onClick={() => guard(async () => {
              const r = await api.createProject({
                name, team, slack_channel: `#${name}-alerts`, shadow_channel: `#${name}-shadow`,
                snowflake: sf, snowflake_schemas: chosenSchemas,
                postgres: usePg ? pg : null,
                postgres_schemas: pgResult?.schemas?.slice(0, 5) ?? ["public"],
                overwrite: false,
              });
              setCreated(r.project);
              setWarnings(r.warnings);
              setStep(3);
            })}>
            {busy ? "Creating…" : "Create project"}
          </button>
        </section>
      )}

      {/* ── STEP 3 ─────────────────────────────────────────────────────── */}
      {step === 3 && created && (
        <section className="panel">
          <header className="panel-header">
            <h2>Tables found in {created}</h2>
            <span className="muted">{tables.length || "…"} tables</span>
          </header>
          {warnings.length > 0 && (
            <div className="pick-block">
              {warnings.map((w) => <div key={w} className="consequence">{w}</div>)}
            </div>
          )}
          {tables.length === 0 ? (
            <button className="btn btn-primary" disabled={busy}
              onClick={() => guard(async () => {
                const r = await api.discover(created);
                if (r.error) { setError(r.error); return; }
                setTables(r.tables);
                setPicked(Object.fromEntries(r.tables.map((t) => [t.fqn, true])));
              })}>
              {busy ? "Reading the warehouse…" : "Discover tables"}
            </button>
          ) : (
            <>
              <p className="muted small">
                The suggested type is a <em>guess</em> from the table's shape. Correct
                anything wrong — a wrong grain produces a check that passes while comparing
                nothing, which is the most dangerous kind of failure.
              </p>
              <table className="diag-table">
                <thead>
                  <tr><th>Watch</th><th>Table</th><th>Looks like</th><th>Rows</th></tr>
                </thead>
                <tbody>
                  {tables.map((t) => (
                    <tr key={t.fqn}>
                      <td>
                        <input type="checkbox" checked={picked[t.fqn] ?? false}
                          onChange={(e) => setPicked({ ...picked, [t.fqn]: e.target.checked })} />
                      </td>
                      <td>
                        <div className="mono small">{t.fqn}</div>
                        <div className="muted small">{t.reason}</div>
                      </td>
                      <td>
                        <span className="relation">{t.suggested_type}</span>
                        <div className="muted small mono">
                          grain: {t.suggested_grain.join(", ") || "—"}
                        </div>
                      </td>
                      <td className="mono small">{t.row_count ?? "—"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
              <button className="btn btn-primary" onClick={() => setStep(4)}>
                Next: confirm the grain
              </button>
            </>
          )}
        </section>
      )}

      {/* ── STEP 4 ─────────────────────────────────────────────────────── */}
      {step === 4 && created && (
        <section className="panel panel-emphasis">
          <header className="panel-header"><h2>Confirm the grain</h2></header>
          <p>
            A table's <strong>grain</strong> is what makes one row unique. Get it wrong and
            the check still passes — it just compares nothing. So it needs a person's name
            against it, and until it has one those checks are recorded but cannot activate.
          </p>
          <div className="form">
            <Field label="Your email" hint="recorded against every grain you confirm"
              value={confirmer} onChange={setConfirmer} />
          </div>
          <p className="muted small">
            Confirming {Object.values(picked).filter(Boolean).length} table(s). You can leave
            this blank to save them unconfirmed and decide later.
          </p>
          <button className="btn btn-primary" disabled={busy}
            onClick={() => guard(async () => {
              const chosen = tables.filter((t) => picked[t.fqn]);
              const r = await api.setMonitored(created, {
                tables: chosen.map((t) => ({
                  fqn: t.fqn,
                  // The real column list, so hop inference can find an ordering column
                  // for a dedup rule instead of giving up.
                  columns: t.columns,
                  table_type: t.suggested_type,
                  grain: t.suggested_grain.length ? t.suggested_grain : ["ID"],
                  grain_confirmed_by: confirmer || null,
                })),
                infer_hops: true,
              });
              setNotes(r.notes);
              if (r.notes.length === 0) onDone(created);
            })}>
            {busy ? "Saving…" : "Save and finish"}
          </button>
          {notes.length > 0 && (
            <div className="pick-block">
              <h3>Recorded, with these caveats</h3>
              {notes.map((n) => <div key={n} className="consequence">{n}</div>)}
              <button className="btn btn-primary" onClick={() => onDone(created)}>
                Go to the project
              </button>
            </div>
          )}
        </section>
      )}

      <div className="wizard-nav">
        {step > 1 && <button className="tab" onClick={() => setStep((step - 1) as Step)}>Back</button>}
        <button className="tab" onClick={() => onDone("")}>Cancel</button>
      </div>
    </main>
  );
}

function Field({
  label, value, onChange, hint, type = "text",
}: {
  label: string; value: string; onChange: (v: string) => void; hint?: string; type?: string;
}) {
  return (
    <label className="field">
      <span className="field-label">{label}</span>
      <input type={type} value={value} onChange={(e) => onChange(e.target.value)} />
      {hint && <span className="field-hint">{hint}</span>}
    </label>
  );
}
