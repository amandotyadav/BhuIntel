import { Link } from "react-router-dom";
import { useMetrics, useQueue } from "../api/hooks";
import { Card, EmptyState, ErrorBanner, Loading, Notice, Stat, StatusBadge, Table, Td } from "../components/ui";
import { useProjectId } from "./ProjectLayout";

const sum = (rows: { n: number }[] | undefined, pred: (r: never) => boolean = () => true) =>
  (rows ?? []).filter(pred as (r: unknown) => boolean).reduce((a, r) => a + r.n, 0);

export function OverviewPage() {
  const pid = useProjectId();
  const m = useMetrics(pid);
  const q = useQueue(pid);
  if (m.isLoading) return <Loading />;
  if (m.error) return <ErrorBanner error={m.error} />;
  const d = m.data!;
  const features = d.datasets.reduce((a, r) => a + Number(r.features), 0);
  const pending = sum(d.conflicts as never, (r: { status: string }) => r.status === "pending");
  const synthetic = d.datasets.some((r) => r.provenance === "synthetic");
  return (
    <div className="space-y-4">
      <h1 className="text-lg font-semibold">Overview</h1>
      {synthetic && (
        <Notice tone="amber">This project contains <strong>synthetic</strong> datasets. Results derived from them are
          test results and say nothing about real-world survey accuracy.</Notice>
      )}
      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        <Stat label="Datasets" value={d.datasets.reduce((a, r) => a + Number(r.datasets), 0)}
          hint={`${d.datasets.reduce((a, r) => a + Number(r.versions), 0)} versions`} />
        <Stat label="Source features" value={features.toLocaleString()} />
        <Stat label="Pending conflicts" value={pending} hint={`${sum(d.conflicts)} total detected`} />
        <Stat label="Integrated features" value={sum(d.integrated as never, (r: { status: string }) => r.status === "active")} />
      </div>
      <div className="grid gap-4 lg:grid-cols-2">
        <Card title="Conflicts by category" actions={<Link className="text-sm text-brand-700 hover:underline" to="review">Open review queue →</Link>}>
          {d.conflicts.length === 0 ? <EmptyState title="No conflicts detected yet">Run matching or topology checks.</EmptyState> : (
            <Table head={["Category", "Status", "Count"]}>
              {d.conflicts.map((r) => (
                <tr key={`${r.category}-${r.status}`}><Td>{r.category.replace(/_/g, " ")}</Td><Td><StatusBadge value={r.status} /></Td><Td>{r.n}</Td></tr>
              ))}
            </Table>
          )}
        </Card>
        <Card title="Match runs">
          {d.match_runs.length === 0 ? <EmptyState title="No match runs yet" /> : (
            <Table head={["Type", "Runs", "Candidates", "Matches"]}>
              {d.match_runs.map((r) => <tr key={r.match_type}><Td>{r.match_type}</Td><Td>{r.runs}</Td><Td>{r.candidates}</Td><Td>{r.matches}</Td></tr>)}
            </Table>
          )}
        </Card>
        <Card title="Datasets by kind">
          {d.datasets.length === 0 ? <EmptyState title="No datasets"><Link className="text-brand-700 underline" to="datasets">Add a dataset</Link></EmptyState> : (
            <Table head={["Kind", "Provenance", "Datasets", "Features"]}>
              {d.datasets.map((r) => <tr key={`${r.kind}-${r.provenance}`}><Td>{r.kind}</Td><Td><StatusBadge value={r.provenance} /></Td><Td>{r.datasets}</Td><Td>{Number(r.features).toLocaleString()}</Td></tr>)}
            </Table>
          )}
        </Card>
        <Card title="Review activity">
          {q.data && (
            <ul className="space-y-1 text-sm">
              {q.data.corrections_by_status.map((r) => <li key={`${r.status}${r.safety}`}>Corrections {r.status} ({r.safety.replace(/_/g, " ")}): <strong>{r.n}</strong></li>)}
              {q.data.predictions_by_status.map((r) => <li key={r.status}>Predicted buildings {r.status}: <strong>{r.n}</strong></li>)}
              {d.review_decisions.map((r) => <li key={`${r.subject_type}${r.decision}`}>Decisions on {r.subject_type}: {r.decision} <strong>{r.n}</strong></li>)}
            </ul>
          )}
          {d.review_decisions.length === 0 && <p className="text-sm text-slate-500">No review decisions yet.</p>}
        </Card>
      </div>
      <p className="text-xs text-slate-500">All figures are computed from live database queries.</p>
    </div>
  );
}
