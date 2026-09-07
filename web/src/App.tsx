// The app shell: project list, switcher, and the entry point to the wizard.
//
// A first-time visitor with no projects lands straight in the wizard rather than an
// empty dashboard, because an empty dashboard does not tell you what to do next.

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "./api/client";
import { Onboarding } from "./routes/Onboarding";
import { Overview } from "./routes/Overview";

export function App() {
  const [selected, setSelected] = useState<string | null>(null);
  const [wizard, setWizard] = useState(false);
  const queryClient = useQueryClient();

  const projects = useQuery({ queryKey: ["projects"], queryFn: api.listProjects });
  const unreachable = useQuery({
    queryKey: ["invalid"],
    queryFn: api.invalidProjects,
    retry: false,
  });

  if (projects.isLoading) return <p className="page">Loading…</p>;
  if (projects.error) {
    return (
      <div className="page error-box">
        <h2>Cannot reach the API</h2>
        <p className="mono">{String(projects.error)}</p>
        <p className="muted">Is the service running on port 8000?</p>
      </div>
    );
  }

  const list = projects.data ?? [];
  const showWizard = wizard || list.length === 0;

  if (showWizard) {
    return (
      <Onboarding
        onDone={(project) => {
          setWizard(false);
          queryClient.invalidateQueries();
          if (project) setSelected(project);
        }}
      />
    );
  }

  const active = selected ?? list[0]!.project;

  return (
    <>
      <div className="topbar">
        <span className="brand">dphm</span>
        <select
          className="project-select"
          value={active}
          onChange={(e) => setSelected(e.target.value)}
        >
          {list.map((p) => (
            <option key={p.project} value={p.project}>
              {p.project} · {p.checks_total} checks
            </option>
          ))}
        </select>
        <button className="tab" onClick={() => setWizard(true)}>
          + Connect a source
        </button>
        {Object.keys(unreachable.data ?? {}).length > 0 && (
          <span className="warn">
            {Object.keys(unreachable.data ?? {}).length} project(s) failed to load
          </span>
        )}
      </div>
      <Overview key={active} project={active} />
    </>
  );
}
