import { NavLink, Outlet, useParams } from "react-router-dom";
import { useProject } from "../api/hooks";
import { useAuth } from "../auth/AuthContext";
import { Button, ErrorBanner, Loading } from "../components/ui";

const NAV = [
  ["", "Overview"], ["map", "Map"], ["datasets", "Datasets"], ["matching", "Matching"], ["review", "Review queue"],
  ["changes", "Changes & imagery"], ["models", "ML models"], ["integration", "Integration & export"],
  ["acquisition", "Data acquisition"], ["jobs", "Jobs"], ["audit", "Audit"],
] as const;

export function useProjectId(): number {
  return Number(useParams().projectId);
}

export function ProjectLayout() {
  const pid = useProjectId();
  const project = useProject(pid);
  const { user, logout } = useAuth();
  return (
    <div className="flex h-full min-h-0 flex-col md:flex-row">
      <aside className="flex shrink-0 flex-col bg-brand-900 text-white md:w-56">
        <div className="px-4 py-3">
          <NavLink to="/" className="text-xs text-brand-100 hover:underline">← All projects</NavLink>
          <p className="mt-1 truncate font-semibold" title={project.data?.name}>{project.data?.name ?? "…"}</p>
          {project.data && <p className="text-xs text-brand-100">EPSG:{project.data.working_crs}</p>}
        </div>
        <nav aria-label="Project sections" className="flex gap-1 overflow-x-auto px-2 pb-2 md:flex-col md:overflow-visible">
          {NAV.map(([to, label]) => (
            <NavLink key={to} to={to} end={to === ""}
              className={({ isActive }) => `whitespace-nowrap rounded px-3 py-1.5 text-sm ${isActive ? "bg-white/15 font-medium" : "text-brand-100 hover:bg-white/10"}`}>
              {label}
            </NavLink>
          ))}
        </nav>
        <div className="mt-auto hidden border-t border-white/10 p-3 text-xs md:block">
          <p className="truncate">{user?.email}</p>
          <p className="text-brand-100">role: {user?.role}</p>
          <Button variant="ghost" className="mt-1 px-0 text-white hover:bg-transparent hover:underline" onClick={logout}>Sign out</Button>
        </div>
      </aside>
      <main className="min-h-0 min-w-0 flex-1 overflow-auto p-4">
        {project.isLoading && <Loading />}
        <ErrorBanner error={project.error} />
        {project.data && <Outlet />}
      </main>
    </div>
  );
}
