import { zodResolver } from "@hookform/resolvers/zod";
import { useState } from "react";
import { useForm } from "react-hook-form";
import { Link, useNavigate } from "react-router-dom";
import { z } from "zod";
import { api } from "../api/client";
import { useApiMutation, useProjects } from "../api/hooks";
import type { Project } from "../api/types";
import { useAuth } from "../auth/AuthContext";
import { Button, Card, EmptyState, ErrorBanner, Field, Input, LabeledInput, Loading, Textarea, fmtDate } from "../components/ui";

const schema = z.object({
  name: z.string().min(1, "Name is required").max(255),
  description: z.string().optional(),
  working_crs: z.string().regex(/^\d*$/, "EPSG code must be numeric").optional(),
  default_tolerance_m: z.coerce.number().positive().max(1000),
  study_area: z.string().optional(),
});
type Form = z.infer<typeof schema>;

function parseStudyArea(text: string | undefined): Record<string, unknown> | undefined {
  if (!text?.trim()) return undefined;
  const g = JSON.parse(text);
  if (g.type === "FeatureCollection") return g.features?.[0]?.geometry;
  if (g.type === "Feature") return g.geometry;
  return g;
}

export function ProjectsPage() {
  const { user, logout, hasRole } = useAuth();
  const projects = useProjects();
  const nav = useNavigate();
  const [formError, setFormError] = useState<unknown>(null);
  const { register, handleSubmit, formState, setValue } = useForm<Form>({
    resolver: zodResolver(schema),
    defaultValues: { default_tolerance_m: 5 },
  });
  const create = useApiMutation((body: Record<string, unknown>) => api.send<Project>("POST", "/api/v1/projects", body),
    [["projects"]]);

  const onSubmit = async (v: Form) => {
    setFormError(null);
    let area;
    try {
      area = parseStudyArea(v.study_area);
    } catch {
      setFormError(new Error("Study area is not valid GeoJSON"));
      return;
    }
    try {
      const p = await create.mutateAsync({
        name: v.name, description: v.description ?? "", default_tolerance_m: v.default_tolerance_m,
        working_crs: v.working_crs ? Number(v.working_crs) : undefined, study_area_geojson: area,
      });
      nav(`/projects/${p.id}`);
    } catch (e) {
      setFormError(e);
    }
  };

  return (
    <div className="min-h-full">
      <header className="flex items-center justify-between bg-brand-900 px-6 py-3 text-white">
        <span className="font-semibold">BhuIntel</span>
        <span className="flex items-center gap-3 text-sm">
          {user?.email} <span className="rounded bg-white/10 px-1.5 text-xs">{user?.role}</span>
          <Button variant="ghost" className="text-white hover:bg-white/10" onClick={logout}>Sign out</Button>
        </span>
      </header>
      <main className="mx-auto grid max-w-6xl gap-6 p-6 lg:grid-cols-[1fr_380px]">
        <Card title="Projects">
          {projects.isLoading && <Loading />}
          <ErrorBanner error={projects.error} />
          {projects.data?.length === 0 && <EmptyState title="No projects yet">Create one to start integrating datasets.</EmptyState>}
          <ul className="divide-y divide-slate-100">
            {projects.data?.map((p) => (
              <li key={p.id} className="py-3">
                <Link to={`/projects/${p.id}`} className="font-medium text-brand-700 hover:underline">{p.name}</Link>
                <p className="text-xs text-slate-500">
                  Working CRS EPSG:{p.working_crs} · tolerance {p.default_tolerance_m} m · created {fmtDate(p.created_at)}
                </p>
                {p.description && <p className="text-sm text-slate-600">{p.description}</p>}
              </li>
            ))}
          </ul>
        </Card>
        {hasRole("analyst") ? (
          <Card title="New project">
            <form onSubmit={handleSubmit(onSubmit)} className="space-y-3" noValidate>
              <LabeledInput label="Name" {...register("name")} error={formState.errors.name?.message} />
              <LabeledInput label="Description" {...register("description")} />
              <div className="grid grid-cols-2 gap-2">
                <LabeledInput label="Working CRS (EPSG)" placeholder="auto from study area" {...register("working_crs")}
                  error={formState.errors.working_crs?.message} hint="Projected, metres (e.g. 32643)" />
                <LabeledInput label="Tolerance (m)" type="number" step="0.1" {...register("default_tolerance_m")}
                  error={formState.errors.default_tolerance_m?.message} />
              </div>
              <Field label="Study area (GeoJSON Polygon in EPSG:4326)" hint="Paste GeoJSON or load a .geojson file">
                <Textarea rows={4} {...register("study_area")} placeholder='{"type":"Polygon","coordinates":[...]}' />
                <Input type="file" accept=".geojson,.json" aria-label="Load study area file" onChange={async (e) => {
                  const f = e.target.files?.[0];
                  if (f) setValue("study_area", await f.text());
                }} />
              </Field>
              <ErrorBanner error={formError} title="Could not create project" />
              <Button type="submit" loading={create.isPending}>Create project</Button>
            </form>
          </Card>
        ) : (
          <Card title="New project"><p className="text-sm text-slate-500">Your role ({user?.role}) cannot create projects.</p></Card>
        )}
      </main>
    </div>
  );
}
