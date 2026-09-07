// Settings — the M0 screen (12 §2.10, 15 M0).
//
// M0's exit criterion: signing in and opening this screen shows every connection healthy
// or a precise error.

import { useQuery } from "@tanstack/react-query";
import { api } from "../api/client";
import { DiagnosticsPanel } from "../components/DiagnosticsPanel";
import { NotCheckedPanel } from "../components/NotCheckedPanel";
import { MedallionGraph } from "../components/MedallionGraph";

export function Settings({ project }: { project: string }) {
  const diagnostics = useQuery({
    queryKey: ["diagnostics", project],
    queryFn: () => api.getDiagnostics(project),
    // Reachability changes on the timescale of a deploy, not a second.
    refetchInterval: 30_000,
  });
  const detail = useQuery({
    queryKey: ["project", project],
    queryFn: () => api.getProject(project),
  });

  if (diagnostics.isLoading || detail.isLoading) return <p>Loading…</p>;
  if (diagnostics.error) return <ErrorBox error={diagnostics.error} />;
  if (detail.error) return <ErrorBox error={detail.error} />;
  if (!diagnostics.data || !detail.data) return null;

  return (
    <main className="page">
      <header className="page-header">
        <h1>{detail.data.project}</h1>
        {detail.data.shadow_mode && (
          <span className="badge-shadow">
            SHADOW MODE — reporting to the shadow channel, filing nothing
          </span>
        )}
        <span className="muted mono">config {detail.data.config_sha}</span>
      </header>

      <DiagnosticsPanel data={diagnostics.data} />
      <MedallionGraph hops={detail.data.hops} />
      <NotCheckedPanel items={detail.data.not_checked} />
    </main>
  );
}

function ErrorBox({ error }: { error: unknown }) {
  const message = error instanceof Error ? error.message : String(error);
  return (
    <div className="error-box">
      <h2>Could not load</h2>
      <p className="mono">{message}</p>
    </div>
  );
}
