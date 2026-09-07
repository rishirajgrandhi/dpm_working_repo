// Connect a data source.
//
// One question per screen. The previous version put fifteen fields and two nested
// optional sections on one page, which is why it felt like filling in a form rather than
// being guided.
//
// Every connection is tested BEFORE anything is written, and a failure returns a remedy,
// not just an error. Two rules the wizard will not bend: it never treats its own guess as
// confirmed, and it never creates a project that is already live.

import { useState } from "react";
import { api, type ConnectionTestResult, type DiscoveredTable } from "../api/client";
import { Card, Field, Notice, Pill, Spinner } from "../components/ui";

type Step = 0 | 1 | 2 | 3 | 4;
const TITLES = ["Warehouse", "Schemas", "Source", "Tables", "Confirm"];

export function Onboarding({
  onDone, firstRun,
}: { onDone: (project: string) => void; firstRun?: boolean }) {
  const [step, setStep] = useState<Step>(0);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const [sf, setSf] = useState({
    account: "", user: "", private_key_path: "~/.dphm/keys/dphm_dev_key.p8",
    warehouse: "DPHM_TEST_WH", database: "", role: "DPHM_DEV",
  });
  const [sfOk, setSfOk] = useState<ConnectionTestResult | null>(null);
  const [schemas, setSchemas] = useState<string[]>([]);
  const [name, setName] = useState("");

  const [wantSource, setWantSource] = useState<boolean | null>(null);
  const [pg, setPg] = useState({ host: "", port: 5432, database: "", user: "", password: "" });
  const [pgOk, setPgOk] = useState<ConnectionTestResult | null>(null);

  const [project, setProject] = useState<string | null>(null);
  const [tables, setTables] = useState<DiscoveredTable[]>([]);
  const [pick, setPick] = useState<Record<string, boolean>>({});
  const [who, setWho] = useState("");
  const [notes, setNotes] = useState<string[]>([]);

  async function go<T>(fn: () => Promise<T>) {
    setBusy(true); setErr(null);
    try { return await fn(); }
    catch (e) { setErr(e instanceof Error ? e.message : String(e)); return undefined; }
    finally { setBusy(false); }
  }

  return (
    <div className="wiz">
      <div className="brand" style={{ marginBottom: "var(--s5)" }}>
        <span className="brand-mark">D</span>
        <span>dphm<div className="brand-sub">connect a data source</div></span>
      </div>

      <div className="wiz-rail">
        {TITLES.map((t, i) => (
          <div key={t} style={{ display: "flex", alignItems: "center", gap: "var(--s2)", flex: i < TITLES.length - 1 ? 1 : 0 }}>
            <span className={`wiz-dot${step === i ? " on" : ""}${step > i ? " done" : ""}`}>
              <span className="wiz-dot-n">{step > i ? "✓" : i + 1}</span> {t}
            </span>
            {i < TITLES.length - 1 && <span className="wiz-bar" />}
          </div>
        ))}
      </div>

      {err && <Notice tone="bad" title="That did not work">{err}</Notice>}

      {/* ── 0. warehouse ─────────────────────────────────────────────── */}
      {step === 0 && (
        <>
          <div className="wiz-q">Where does your data live?</div>
          <p className="wiz-help">
            Snowflake details. We test them before saving anything, and we only ever read —
            except two small bookkeeping schemas of our own.
          </p>
          <Card>
            <div className="fields">
              <Field label="Account" placeholder="myorg-ab12345" value={sf.account}
                onChange={(v) => setSf({ ...sf, account: v })}
                hint="from select current_organization_name(), current_account_name()" />
              <Field label="User" value={sf.user} onChange={(v) => setSf({ ...sf, user: v })} />
              <Field label="Private key file" value={sf.private_key_path}
                onChange={(v) => setSf({ ...sf, private_key_path: v })}
                hint="key-pair auth — no password is ever stored" />
              <Field label="Warehouse" value={sf.warehouse} onChange={(v) => setSf({ ...sf, warehouse: v })} />
              <Field label="Database" value={sf.database} onChange={(v) => setSf({ ...sf, database: v })} />
              <Field label="Read as role" value={sf.role} onChange={(v) => setSf({ ...sf, role: v })}
                hint="checks run with this role's permissions" />
            </div>
          </Card>
          {sfOk && (
            <Notice tone={sfOk.ok ? "ok" : "bad"} title={sfOk.ok ? "Connected" : "Could not connect"}>
              {sfOk.detail}{sfOk.remedy ? ` — ${sfOk.remedy}` : ""}
            </Notice>
          )}
          <div className="wiz-foot">
            <button className="btn btn-ghost" onClick={() => onDone("")}>Cancel</button>
            <div className="btn-row">
              <button className="btn" disabled={busy || !sf.account || !sf.database || !sf.user}
                onClick={() => go(async () => {
                  const r = await api.testSnowflake(sf);
                  setSfOk(r);
                  if (r.ok) setSchemas(r.schemas.filter((s) => !s.startsWith("DPHM_")));
                })}>
                {busy ? <Spinner label="testing…" /> : "Test connection"}
              </button>
              <button className="btn btn-primary" disabled={!sfOk?.ok} onClick={() => setStep(1)}>
                Continue
              </button>
            </div>
          </div>
        </>
      )}

      {/* ── 1. schemas + name ────────────────────────────────────────── */}
      {step === 1 && (
        <>
          <div className="wiz-q">Which parts should we watch?</div>
          <p className="wiz-help">
            We found {sfOk?.schemas.length ?? 0} schemas. Our own bookkeeping schemas are
            excluded automatically — watching those would report our own activity as your
            pipeline changing.
          </p>
          <Card>
            <div className="checks-list">
              {(sfOk?.schemas ?? []).map((s) => (
                <label key={s} className="check-row">
                  <input type="checkbox" checked={schemas.includes(s)}
                    onChange={(e) => setSchemas(e.target.checked
                      ? [...schemas, s] : schemas.filter((x) => x !== s))} />
                  <span className="mono">{s}</span>
                  {s.startsWith("DPHM_") && <Pill tone="mute">ours</Pill>}
                </label>
              ))}
            </div>
          </Card>
          <Card title="Name this project">
            <div className="fields">
              <Field label="Project name" placeholder="orders" value={name}
                onChange={(v) => setName(v.toLowerCase().replace(/[^a-z0-9_-]/g, ""))}
                hint="lower-case letters, digits, - and _" />
            </div>
          </Card>
          <div className="wiz-foot">
            <button className="btn btn-ghost" onClick={() => setStep(0)}>Back</button>
            <button className="btn btn-primary" disabled={schemas.length === 0 || name.length < 2}
              onClick={() => setStep(2)}>Continue</button>
          </div>
        </>
      )}

      {/* ── 2. optional source ───────────────────────────────────────── */}
      {step === 2 && (
        <>
          <div className="wiz-q">Is there a source database too?</div>
          <p className="wiz-help">
            If your data is copied in from somewhere else — Postgres, RDS — we can compare
            the two. Without it, everything downstream still works; we just cannot check
            the very first copy.
          </p>
          <div className="btn-row" style={{ marginBottom: "var(--s4)" }}>
            <button className="btn" aria-pressed={wantSource === true} onClick={() => setWantSource(true)}>
              Yes, I have one
            </button>
            <button className="btn" aria-pressed={wantSource === false} onClick={() => setWantSource(false)}>
              Not yet — skip
            </button>
          </div>

          {wantSource && (
            <>
              <Card>
                <div className="fields">
                  <Field label="Host" value={pg.host} onChange={(v) => setPg({ ...pg, host: v })} />
                  <Field label="Port" value={String(pg.port)}
                    onChange={(v) => setPg({ ...pg, port: Number(v) || 5432 })} />
                  <Field label="Database" value={pg.database} onChange={(v) => setPg({ ...pg, database: v })} />
                  <Field label="User" value={pg.user} onChange={(v) => setPg({ ...pg, user: v })} />
                  <Field label="Password" type="password" value={pg.password}
                    onChange={(v) => setPg({ ...pg, password: v })} />
                </div>
              </Card>
              {pgOk && (
                <Notice tone={pgOk.ok ? "ok" : "bad"} title={pgOk.ok ? "Connected" : "Could not connect"}>
                  {pgOk.detail}{pgOk.remedy ? ` — ${pgOk.remedy}` : ""}
                </Notice>
              )}
              <button className="btn" disabled={busy || !pg.host}
                onClick={() => go(async () => setPgOk(await api.testPostgres(pg)))}>
                {busy ? <Spinner label="testing…" /> : "Test connection"}
              </button>
            </>
          )}

          {wantSource === false && (
            <Notice tone="info" title="Skipping the source">
              We will report the first-copy comparison as unavailable rather than pretend it
              passed. You can add it later.
            </Notice>
          )}

          <div className="wiz-foot">
            <button className="btn btn-ghost" onClick={() => setStep(1)}>Back</button>
            <button className="btn btn-primary"
              disabled={busy || wantSource === null || (wantSource && !pgOk?.ok)}
              onClick={() => go(async () => {
                const r = await api.createProject({
                  name, team: "@data-platform",
                  slack_channel: `#${name}-alerts`, shadow_channel: `#${name}-shadow`,
                  snowflake: sf, snowflake_schemas: schemas,
                  postgres: wantSource ? pg : null,
                  postgres_schemas: pgOk?.schemas?.slice(0, 5) ?? ["public"],
                  overwrite: true,
                });
                setProject(r.project);
                const d = await api.discover(r.project);
                if (d.error) { setErr(d.error); return; }
                setTables(d.tables);
                setPick(Object.fromEntries(d.tables.map((t) => [t.fqn, !isPlumbing(t.name)])));
                setStep(3);
              })}>
              {busy ? <Spinner label="setting up…" /> : "Create and look at my tables"}
            </button>
          </div>
        </>
      )}

      {/* ── 3. tables ────────────────────────────────────────────────── */}
      {step === 3 && (
        <>
          <div className="wiz-q">We found {tables.length} tables. Which matter?</div>
          <p className="wiz-help">
            The type beside each one is a <em>guess</em> from its shape, and we say why.
            Correct anything wrong at Setup afterwards — a wrong guess about what makes a
            row unique produces a check that passes while comparing nothing.
          </p>
          <Card tight>
            <ul className="rows">
              {tables.map((t) => (
                <li key={t.fqn} className="row clickable"
                  onClick={() => setPick({ ...pick, [t.fqn]: !pick[t.fqn] })}>
                  <input type="checkbox" checked={pick[t.fqn] ?? false} readOnly style={{ marginTop: 4 }} />
                  <div className="row-main">
                    <div className="row-title mono small">{t.fqn}</div>
                    <div className="row-sub">{t.reason}</div>
                  </div>
                  <div className="row-aside">
                    <Pill tone="info">{t.suggested_type}</Pill>
                    <div className="xs muted" style={{ marginTop: 2 }}>
                      {t.row_count ?? "?"} rows
                    </div>
                  </div>
                </li>
              ))}
            </ul>
          </Card>
          <div className="wiz-foot">
            <button className="btn btn-ghost" onClick={() => setStep(2)}>Back</button>
            <button className="btn btn-primary" onClick={() => setStep(4)}>
              Continue with {Object.values(pick).filter(Boolean).length} tables
            </button>
          </div>
        </>
      )}

      {/* ── 4. confirm ───────────────────────────────────────────────── */}
      {step === 4 && project && (
        <>
          <div className="wiz-q">One last thing, and it matters</div>
          <p className="wiz-help">
            We guessed what makes each row unique. If that guess is wrong the check still
            passes — it just compares nothing, which is worse than having no check. So it
            needs a person's name against it.
          </p>
          <Card>
            <div className="fields">
              <Field label="Your email" placeholder="you@company.com" value={who} onChange={setWho}
                hint="recorded against every table you confirm" />
            </div>
            <p className="muted small" style={{ marginTop: "var(--s3)" }}>
              Leave it blank to save everything unconfirmed. The checks are still recorded —
              they just cannot run until someone vouches for them.
            </p>
          </Card>

          {notes.length > 0 && (
            <Card title="Saved, with these caveats" accent>
              <ul className="rows" style={{ margin: "calc(var(--s5) * -1)" }}>
                {notes.map((n) => (
                  <li key={n} className="row"><div className="row-main small">{n}</div></li>
                ))}
              </ul>
            </Card>
          )}

          <div className="wiz-foot">
            <button className="btn btn-ghost" onClick={() => setStep(3)}>Back</button>
            {notes.length > 0 ? (
              <button className="btn btn-primary" onClick={() => onDone(project)}>
                Go to {project}
              </button>
            ) : (
              <button className="btn btn-primary" disabled={busy}
                onClick={() => go(async () => {
                  const chosen = tables.filter((t) => pick[t.fqn]);
                  const r = await api.setMonitored(project, {
                    tables: chosen.map((t) => ({
                      fqn: t.fqn, columns: t.columns, table_type: t.suggested_type,
                      grain: t.suggested_grain.length ? t.suggested_grain : ["ID"],
                      grain_confirmed_by: who || null,
                      ...(t.suggested_type === "scd2" ? scd2Guess(t) : {}),
                    })),
                    infer_hops: true,
                  });
                  setNotes(r.notes);
                  if (r.notes.length === 0) onDone(project);
                })}>
                {busy ? <Spinner label="saving…" /> : "Finish"}
              </button>
            )}
          </div>
        </>
      )}

      {firstRun && step === 0 && (
        <Notice tone="info" title="First time here">
          Nothing is monitored yet. This takes about two minutes, and nothing is written
          until a connection test passes.
        </Notice>
      )}
    </div>
  );
}

/** Bookkeeping tables are unticked by default — they are plumbing, not your data. */
function isPlumbing(name: string): boolean {
  return /(^ETL_|_LOG$|_REJECT$|_AUDIT$|^TMP_|^STG_)/i.test(name);
}

/** A first guess at the history columns, from conventional names. Always correctable. */
function scd2Guess(t: DiscoveredTable) {
  const has = (n: string) => t.columns.find((c) => c.toUpperCase() === n);
  const business = t.suggested_grain.filter((g) => !g.toUpperCase().includes("VALID"));
  return {
    scd2_business_key: business.length ? business : ["ID"],
    scd2_surrogate_key: t.columns.find((c) => /_SK$/i.test(c)) ?? null,
    scd2_valid_from: has("VALID_FROM") ?? null,
    scd2_valid_to: has("VALID_TO") ?? null,
    scd2_is_current: has("IS_CURRENT") ?? null,
    scd2_tracked_columns: t.columns.filter(
      (c) => !/(_ID$|_SK$|VALID_|IS_CURRENT|_AT$|^ETL_|^DW_)/i.test(c),
    ),
  };
}
