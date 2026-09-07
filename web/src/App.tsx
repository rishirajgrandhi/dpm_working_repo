// The shell: a sidebar you can orient by, and one page at a time.
//
// The flow the previous version got wrong: it opened on six ambiguous tiles behind five
// unlabelled tabs, with the project switcher and the "add a source" button competing in
// the same bar. Now the sidebar answers "where am I and what needs attention", and the
// page answers one question.

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "./api/client";
import { Notice, Spinner } from "./components/ui";
import { CoveragePage } from "./routes/Coverage";
import { Health } from "./routes/Health";
import { Onboarding } from "./routes/Onboarding";
import { Results } from "./routes/Results";
import { Setup } from "./routes/Setup";

type View = "health" | "results" | "coverage" | "setup";

const NAV: { id: View; label: string }[] = [
  { id: "health", label: "Health" },
  { id: "results", label: "Results" },
  { id: "coverage", label: "Coverage" },
  { id: "setup", label: "Setup" },
];

export function App() {
  const [view, setView] = useState<View>("health");
  const [selected, setSelected] = useState<string | null>(null);
  const [wizard, setWizard] = useState(false);
  const qc = useQueryClient();

  const projects = useQuery({ queryKey: ["projects"], queryFn: api.listProjects });

  if (projects.isLoading) {
    return <div className="main"><Spinner label="starting up…" /></div>;
  }
  if (projects.error) {
    return (
      <div className="main">
        <Notice tone="bad" title="Cannot reach the service">
          {String(projects.error)} — is it running on port 8000?
        </Notice>
      </div>
    );
  }

  const list = projects.data ?? [];

  // A first-time visitor goes straight to the wizard. An empty dashboard tells you
  // nothing about what to do next.
  if (wizard || list.length === 0) {
    return (
      <Onboarding
        firstRun={list.length === 0}
        onDone={(p) => {
          setWizard(false);
          qc.invalidateQueries();
          if (p) { setSelected(p); setView("health"); }
        }}
      />
    );
  }

  const active = selected ?? list[0]!.project;
  return (
    <div className="shell">
      <aside className="sidebar">
        <div className="brand">
          <span className="brand-mark">D</span>
          <span>
            dphm
            <div className="brand-sub">pipeline health</div>
          </span>
        </div>

        <div className="proj-picker">
          <label htmlFor="proj">Project</label>
          <select id="proj" className="proj" value={active}
            onChange={(e) => setSelected(e.target.value)}>
            {list.map((p) => <option key={p.project} value={p.project}>{p.project}</option>)}
          </select>
        </div>

        <nav className="nav">
          {NAV.map((n) => (
            <button key={n.id} className="nav-item" aria-current={view === n.id ? "page" : undefined}
              onClick={() => setView(n.id)}>
              <span>{n.label}</span>
              <NavCount view={n.id} project={active} />
            </button>
          ))}
        </nav>

        <div className="sidebar-foot">
          <button className="btn btn-sm" onClick={() => setWizard(true)}>+ Connect a source</button>
          <span className="muted xs">
            Everything runs in shadow mode: it reports, and files nothing anywhere.
          </span>
        </div>
      </aside>

      <main className="main">
        {view === "health" && <Health key={active} project={active} onGo={(v) => setView(v as View)} />}
        {view === "results" && <Results key={active} project={active} />}
        {view === "coverage" && <CoveragePage key={active} project={active} />}
        {view === "setup" && <Setup key={active} project={active} />}
      </main>
    </div>
  );
}

/** A count in the sidebar, so what needs attention is visible without navigating. */
function NavCount({ view, project }: { view: View; project: string }) {
  const latest = useQuery({
    queryKey: ["latest", project], queryFn: () => api.latestRun(project), retry: false,
  });
  const detail = useQuery({ queryKey: ["project", project], queryFn: () => api.getProject(project) });
  const c = latest.data?.counts ?? {};

  if (view === "health") {
    const fail = c.FAIL ?? 0;
    return fail > 0 ? <span className="nav-count is-bad">{fail}</span> : null;
  }
  if (view === "results") {
    const n = latest.data?.results.length ?? 0;
    return n ? <span className="nav-count">{n}</span> : null;
  }
  if (view === "coverage") {
    const gaps = detail.data?.not_checked.length ?? 0;
    return gaps > 0 ? <span className="nav-count is-warn">{gaps}</span> : null;
  }
  return null;
}
