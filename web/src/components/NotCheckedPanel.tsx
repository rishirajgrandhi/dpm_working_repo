// "What we are not checking" (12 §2.1).
//
// This panel is deliberately NOT collapsible and NOT below the fold. Coverage never
// reaches 100%, and a dashboard that shows only green is the failure this product exists
// to end. It renders on a green project too.

import type { NotCheckedItem } from "../api/client";

export function NotCheckedPanel({ items }: { items: NotCheckedItem[] }) {
  return (
    <section className="panel panel-emphasis">
      <header className="panel-header">
        <h2>What we are not checking</h2>
        <span className="muted">{items.length} items</span>
      </header>
      {items.length === 0 ? (
        <p className="muted">
          Nothing outstanding. Coverage is still not a guarantee — a green result means
          everything we checked, on the columns we compared, looks fine.
        </p>
      ) : (
        <ul className="not-checked">
          {items.map((item, index) => (
            <li key={`${item.subject}-${index}`}>
              <span className="mono">{item.subject}</span>
              <span className="reason"> — {item.reason}</span>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
