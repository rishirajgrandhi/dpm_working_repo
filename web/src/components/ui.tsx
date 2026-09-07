// Small shared pieces. Presentation only — no verdict logic lives here.

import type { ReactNode } from "react";

export type Tone = "ok" | "bad" | "warn" | "info" | "mute";

/** Maps a result status to a tone. The ONE place that mapping exists.
 *
 * INCONCLUSIVE and UNVALIDATED are warn, never ok. "We could not tell" and
 * "everything is fine" must not look the same — painting them green is the
 * single most common way a dashboard lies. */
export const STATUS_TONE: Record<string, Tone> = {
  PASS: "ok",
  FAIL: "bad",
  INCONCLUSIVE: "warn",
  UNVALIDATED: "warn",
};

export const STATUS_MEANS: Record<string, string> = {
  PASS: "checked, nothing wrong",
  FAIL: "something is wrong",
  INCONCLUSIVE: "tried, could not tell — not a pass",
  UNVALIDATED: "no way to check this yet — not a pass",
};

export function Pill({ tone, children, large }: { tone: Tone; children: ReactNode; large?: boolean }) {
  return <span className={`pill pill-${tone}${large ? " pill-lg" : ""}`}>{children}</span>;
}

export function Card({
  title, action, children, tight, accent,
}: { title?: string; action?: ReactNode; children: ReactNode; tight?: boolean; accent?: boolean }) {
  return (
    <section className={`card${accent ? " accent" : ""}`}>
      {title && (
        <header className="card-head">
          <h2>{title}</h2>
          {action}
        </header>
      )}
      <div className={`card-body${tight ? " tight" : ""}`}>{children}</div>
    </section>
  );
}

export function Stat({
  value, label, hint, tone,
}: { value: ReactNode; label: string; hint?: string; tone?: Tone }) {
  return (
    <div className={`stat${tone ? ` is-${tone}` : ""}`}>
      <div className="stat-v">{value}</div>
      <div className="stat-l">{label}</div>
      {hint && <div className="stat-h">{hint}</div>}
    </div>
  );
}

export function Notice({
  tone, title, children,
}: { tone: Tone; title: string; children?: ReactNode }) {
  return (
    <div className={`notice notice-${tone}`}>
      <div className="notice-t">{title}</div>
      {children && <div className="notice-b">{children}</div>}
    </div>
  );
}

export function Field({
  label, value, onChange, hint, type = "text", placeholder,
}: {
  label: string; value: string; onChange: (v: string) => void;
  hint?: string; type?: string; placeholder?: string;
}) {
  return (
    <label className="field">
      <span className="field-l">{label}</span>
      <input type={type} value={value} placeholder={placeholder}
        onChange={(e) => onChange(e.target.value)} />
      {hint && <span className="field-h">{hint}</span>}
    </label>
  );
}

export function Empty({ title, children, action }: { title: string; children?: ReactNode; action?: ReactNode }) {
  return (
    <div className="empty">
      <h2>{title}</h2>
      {children && <p>{children}</p>}
      {action}
    </div>
  );
}

export function Spinner({ label }: { label?: string }) {
  return (
    <span className="btn-row">
      <span className="spinner" /> {label && <span className="muted small">{label}</span>}
    </span>
  );
}
