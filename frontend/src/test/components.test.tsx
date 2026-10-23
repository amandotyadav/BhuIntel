import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError, api, errorMessage, tokenStore } from "../api/client";
import { boundsOf } from "../api/geojson";
import { StatusBadge } from "../components/ui";
import { LoginPage } from "../pages/LoginPage";
import { ReviewPage } from "../pages/ReviewPage";
import { DatasetsPage } from "../pages/DatasetsPage";
import { mockFetch, renderApp, USER } from "./utils";
import { render } from "@testing-library/react";

vi.mock("../components/map/MapView", () => ({
  MapView: ({ layers }: { layers: { id: string }[] }) => <div data-testid="map">{layers.map((l) => l.id).join(",")}</div>,
  geomFromString: (s: string | null) => (s ? JSON.parse(s) : null),
  LAYER_STYLES: {},
}));

beforeEach(() => sessionStorage.clear());
afterEach(() => vi.unstubAllGlobals());

describe("api client", () => {
  it("parses structured API errors", async () => {
    mockFetch(() => ({ status: 422, body: { error: { code: "crs_required", message: "CRS unknown", details: null, request_id: "r1" } } }));
    const err = (await api.get("/x").catch((e: unknown) => e)) as ApiError;
    expect(err).toBeInstanceOf(ApiError);
    expect(err.code).toBe("crs_required");
    expect(err.requestId).toBe("r1");
    expect(errorMessage(err)).toBe("CRS unknown");
  });

  it("formats validation details", () => {
    const e = new ApiError(422, "validation_error", "Request validation failed", [{ loc: ["body", "name"], msg: "too short" }]);
    expect(errorMessage(e)).toContain("name: too short");
  });

  it("sends bearer token", async () => {
    tokenStore.set("tok123");
    const calls = mockFetch(() => ({ body: [] }));
    await api.get("/api/v1/projects", { include_archived: false, skip: undefined });
    expect((calls[0].init?.headers as Record<string, string>).Authorization).toBe("Bearer tok123");
    expect(calls[0].url).toBe("/api/v1/projects?include_archived=false");
  });
});

describe("geojson utils", () => {
  it("computes bounds across geometry types", () => {
    const b = boundsOf([
      { type: "Point", coordinates: [73.8, 18.5] },
      { type: "Polygon", coordinates: [[[73.7, 18.4], [73.9, 18.4], [73.9, 18.6], [73.7, 18.4]]] },
      null,
    ]);
    expect(b).toEqual([[18.4, 73.7], [18.6, 73.9]]);
    expect(boundsOf([])).toBeNull();
  });
});

describe("StatusBadge", () => {
  it("renders human-readable status", () => {
    render(<StatusBadge value="needs_crs" />);
    expect(screen.getByText("needs crs")).toBeInTheDocument();
  });
});

describe("LoginPage", () => {
  it("validates input and shows server errors", async () => {
    mockFetch((url) => (url.endsWith("/auth/token")
      ? { status: 401, body: { error: { code: "invalid_credentials", message: "Incorrect email or password" } } } : undefined));
    renderApp(<LoginPage />);
    await userEvent.click(screen.getByRole("button", { name: "Sign in" }));
    expect(await screen.findByText("Enter a valid email address")).toBeInTheDocument();
    await userEvent.type(screen.getByLabelText("Email"), "a@b.org");
    await userEvent.type(screen.getByLabelText("Password"), "wrong");
    await userEvent.click(screen.getByRole("button", { name: "Sign in" }));
    expect(await screen.findByText(/Incorrect email or password/)).toBeInTheDocument();
    expect(screen.queryByText("Enter a valid email address")).not.toBeInTheDocument();
  });
});

const conflict = {
  id: 7, category: "parcel_overlap", severity: "high", priority: 92.5, confidence: 0.9, confidence_kind: "heuristic", status: "pending",
  legal_sensitive: true, explanation: "Parcels 'P-013' and 'P-014' overlap by 80.00 m².", proposed_action: "Do not auto-correct.",
  evidence: { overlap_area_m2: 80 }, run_id: null, geometry: { type: "Polygon", coordinates: [[[0, 0], [1, 0], [1, 1], [0, 0]]] },
  features: { type: "FeatureCollection", features: [1, 2].map((i) => ({ type: "Feature", id: i, geometry: null, properties: {
    id: i, source_id_raw: `P-01${i + 2}`, normalized_id: null, attributes: {}, normalized_attributes: { land_use: i === 1 ? "residential" : "commercial" },
    area_m2: 1200, is_valid_source: true, validity_reason: null, repair_status: "none", dataset: "Municipal", dataset_id: 1, kind: "parcels",
    provenance: "synthetic", reliability: 0.7, source_date: null, license: "", version_no: 1 } })) },
  corrections: [], decisions: [],
};

describe("ReviewPage", () => {
  it("lists conflicts, compares sources and requires legal acknowledgement", async () => {
    tokenStore.set("t");
    const posted: unknown[] = [];
    mockFetch((url, init) => {
      if (url.endsWith("/auth/me")) return { body: { ...USER, role: "admin" } };
      if (url.includes("/conflicts?")) return { body: { type: "FeatureCollection", numberMatched: 1, summary: [], features: [
        { type: "Feature", geometry: null, properties: { ...conflict, feature_ids: [1, 2], dataset_version_ids: [1] } }] } };
      if (url.endsWith("/conflicts/7")) return { body: conflict };
      if (url.endsWith("/conflicts/7/decision") && init?.method === "POST") {
        posted.push(JSON.parse(String(init.body)));
        return { body: { status: "approved", correction_outcome: null } };
      }
      return undefined;
    });
    renderApp(<ReviewPage />, { path: "/projects/:projectId/review", route: "/projects/3/review?conflict=7" });
    expect(await screen.findByText(/overlap by 80.00/, { selector: "p" })).toBeInTheDocument();
    expect(screen.getAllByText("legally sensitive").length).toBeGreaterThan(0);
    const row = screen.getByText("land_use ⚠").closest("tr")!;
    expect(within(row).getByText("residential")).toBeInTheDocument();
    expect(within(row).getByText("commercial")).toBeInTheDocument();
    await userEvent.click(screen.getByLabelText(/backed by authoritative evidence/));
    await userEvent.type(screen.getByLabelText("Reviewer comment"), "verified");
    await userEvent.click(screen.getByRole("button", { name: "Approve proposal" }));
    await waitFor(() => expect(posted).toEqual([{ decision: "approve", comment: "verified", acknowledge_legal: true }]));
    expect(await screen.findByText(/Decision recorded/)).toBeInTheDocument();
  });

  it("hides decision controls from viewers", async () => {
    tokenStore.set("t");
    mockFetch((url) => {
      if (url.endsWith("/auth/me")) return { body: { ...USER, role: "viewer" } };
      if (url.includes("/conflicts?")) return { body: { type: "FeatureCollection", numberMatched: 0, summary: [], features: [] } };
      if (url.endsWith("/conflicts/7")) return { body: conflict };
      return undefined;
    });
    renderApp(<ReviewPage />, { path: "/projects/:projectId/review", route: "/projects/3/review?conflict=7" });
    expect(await screen.findByText(/can view but not decide/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Approve proposal" })).not.toBeInTheDocument();
  });
});

describe("DatasetsPage", () => {
  it("shows CRS confirmation for versions without CRS and confirms ambiguous mappings explicitly", async () => {
    tokenStore.set("t");
    const patches: unknown[] = [];
    mockFetch((url, init) => {
      if (url.endsWith("/auth/me")) return { body: { ...USER, role: "analyst" } };
      if (url.endsWith("/datasets")) return { body: [{ id: 5, project_id: 3, name: "Property register", kind: "property_records",
        provenance: "synthetic", provider: "", source_url: "", license: "", attribution: "", description: "", reliability: 0.5,
        positional_accuracy_m: null, source_date: null, status: "active", created_at: "2026-01-01T00:00:00Z" }] };
      if (url.endsWith("/datasets/5/versions")) return { body: [{ id: 9, dataset_id: 5, version_no: 1, file_sha256: "a".repeat(64),
        original_filename: "records.csv", format: "csv", size_bytes: 3000, layer: null, source_crs: null, crs_confirmed: false,
        geometry_type: "Point", feature_count: 31, schema: { plot_no: "string" }, field_profile: {}, csv_options: {}, status: "needs_crs",
        error: null, processing_log: [], stats: {}, created_at: "2026-01-01T00:00:00Z", processed_at: null }] };
      if (url.endsWith("/datasets/5/mappings")) return { body: [
        { id: 1, dataset_id: 5, source_field: "plot_area", target_field: "area_sqm", method: "similarity", score: 0.8, evidence: {},
          ambiguous: true, status: "suggested" }] };
      if (url.endsWith("/mappings/1") && init?.method === "PATCH") { patches.push(JSON.parse(String(init.body))); return { body: {} }; }
      return undefined;
    });
    renderApp(<DatasetsPage />, { path: "/projects/:projectId/datasets", route: "/projects/3/datasets" });
    await userEvent.click(await screen.findByRole("button", { name: /Property register/ }));
    expect(await screen.findByText(/Coordinate reference system required/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Run preprocessing" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Confirm CRS" })).toBeDisabled();
    expect(await screen.findByText("ambiguous")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Confirm" }));
    await waitFor(() => expect(patches).toEqual([{ status: "approved", confirm_ambiguous: true }]));
  });
});
