/**
 * Critical end-to-end workflow in a real browser against the running API, worker and PostGIS:
 * login -> create project -> upload two inconsistent parcel layers + property CSV -> inspect CRS ->
 * confirm CSV CRS -> preprocess -> match -> open conflict on the map -> approve correction (legal ack) ->
 * build integrated layer -> export GeoJSON -> download and validate the exported file.
 *
 * Requires a seeded admin (backend/scripts/seed.py) - credentials via E2E_EMAIL / E2E_PASSWORD.
 */
import { expect, test, type Page } from "@playwright/test";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const FIX = path.resolve(here, "../../fixtures/synthetic_pune");
const EMAIL = process.env.E2E_EMAIL ?? "admin@example.org";
const PASSWORD = process.env.E2E_PASSWORD ?? "demo-password-123";
const SHOTS = process.env.E2E_SCREENSHOT_DIR;

async function shot(page: Page, name: string) {
  if (SHOTS) await page.screenshot({ path: path.join(SHOTS, `${name}.png`), fullPage: false });
}

async function addDataset(page: Page, name: string, kind: string, file: string) {
  await page.getByLabel("Name", { exact: true }).fill(name);
  await page.getByLabel("Kind").selectOption(kind);
  await page.getByLabel("Provenance").selectOption("synthetic");
  await page.getByRole("button", { name: "Create dataset" }).click();
  await expect(page.getByRole("heading", { name })).toBeVisible();
  await page.getByLabel("Dataset file").setInputFiles(path.join(FIX, file));
  await page.getByRole("button", { name: "Upload new version" }).click();
  await expect(page.getByText(file).first()).toBeVisible();
}

async function processAndWait(page: Page) {
  await page.getByRole("button", { name: "Run preprocessing" }).click();
  await expect(page.getByText("features loaded")).toBeVisible({ timeout: 120_000 });
}

test("upload -> match -> review -> integrate -> export", async ({ page }) => {
  const project = `E2E ${Date.now()}`;
  await page.goto("/");
  await page.getByLabel("Email").fill(EMAIL);
  await page.getByLabel("Password").fill(PASSWORD);
  await page.getByRole("button", { name: "Sign in" }).click();

  // 1. project with study area (working CRS derived -> UTM 43N)
  await page.getByLabel("Name").fill(project);
  await page.getByLabel(/Study area/).fill(fs.readFileSync(path.join(FIX, "study_area.geojson"), "utf8"));
  await page.getByRole("button", { name: "Create project" }).click();
  await expect(page.getByText("EPSG:32643").first()).toBeVisible();

  // 2-4. datasets, inspection, CRS confirmation, preprocessing
  await page.getByRole("link", { name: "Datasets" }).click();
  await addDataset(page, "E2E municipal parcels", "parcels", "municipal_parcels.gpkg");
  await expect(page.getByText("EPSG:32643").first()).toBeVisible(); // inspected source CRS
  await processAndWait(page);
  await expect(page.getByText("invalid geometries")).toBeVisible();

  await addDataset(page, "E2E revenue parcels", "parcels", "revenue_parcels.geojson");
  await expect(page.getByText("EPSG:4326").first()).toBeVisible();
  await processAndWait(page);

  await addDataset(page, "E2E property register", "property_records", "property_records.csv");
  await expect(page.getByText(/Coordinate reference system required/)).toBeVisible();
  await expect(page.getByRole("button", { name: "Run preprocessing" })).toBeDisabled();
  await page.getByLabel("CRS", { exact: true }).fill("EPSG:4326");
  await page.getByLabel("Evidence / justification").fill("Fixture README: WGS84 lat/lon columns");
  await page.getByRole("button", { name: "Confirm CRS" }).click();
  await processAndWait(page);
  await shot(page, "datasets");

  // 5-7. matching with explainable scores
  await page.getByRole("link", { name: "Matching" }).click();
  await page.getByLabel("Left version").selectOption({ label: "E2E municipal parcels v1" });
  await page.getByLabel("Right version").selectOption({ label: "E2E revenue parcels v1" });
  await page.getByRole("button", { name: "Run matching" }).click();
  const runRow = page.locator("tr", { hasText: "parcel_parcel" }).first();
  await expect(runRow.getByText("succeeded")).toBeVisible({ timeout: 120_000 });
  await runRow.getByRole("button").first().click();
  await page.locator("tbody tr").filter({ hasText: "P-001" }).first().getByRole("button", { name: "P-001" }).click();
  await expect(page.getByText(/Intersection-over-union/).first()).toBeVisible();
  await shot(page, "matching");

  // 8. conflicts on the map
  await page.getByRole("link", { name: "Map" }).click();
  await expect(page.getByText(/Pending conflicts/).first()).toBeVisible();
  await expect(page.locator(".leaflet-interactive").first()).toBeVisible();
  await shot(page, "map");

  // 9-10. review: approve a legally sensitive geometry repair with acknowledgement; audit preserved
  await page.getByRole("link", { name: "Review queue" }).click();
  await page.getByLabel("Category filter").selectOption("invalid_geometry");
  await page.getByRole("button", { name: /invalid geometry/ }).first().click();
  await expect(page.getByText("Correction #").first()).toBeVisible();
  await page.getByRole("button", { name: "Before (original)" }).click();
  await page.getByRole("button", { name: "After (proposed)" }).click();
  await page.getByLabel(/backed by authoritative evidence/).check();
  await page.getByLabel("Reviewer comment").fill("E2E: verified against synthetic survey sketch");
  await shot(page, "review");
  await page.getByRole("button", { name: "Approve proposal" }).click();
  await expect(page.getByText(/Decision recorded/)).toBeVisible();
  await expect(page.getByRole("emphasis").filter({ hasText: "E2E: verified against synthetic survey sketch" })).toBeVisible();
  await expect(page.getByLabel("Reviewer comment")).toHaveValue("");

  // integrated layer
  await page.getByRole("link", { name: "Integration & export" }).click();
  await page.getByLabel("Primary parcel version").selectOption({ label: "E2E municipal parcels v1" });
  await page.getByLabel("Parcel match run").selectOption({ index: 1 });
  await page.getByRole("button", { name: "Build integrated layer" }).click();
  await expect(page.locator("[aria-live=polite]").getByText("succeeded").first()).toBeVisible({ timeout: 120_000 });

  // 11-12. export, download and validate the file
  await page.getByLabel("Format").selectOption("geojson");
  await page.getByRole("button", { name: "Create export" }).click();
  const dl = page.getByRole("button", { name: "Download" }).first();
  await expect(dl).toBeVisible({ timeout: 120_000 });
  const [download] = await Promise.all([page.waitForEvent("download"), dl.click()]);
  const file = await download.path();
  const fc = JSON.parse(fs.readFileSync(file!, "utf8"));
  const parcels = fc.features.filter((f: { properties: { feature_class: string } }) => f.properties.feature_class === "parcel");
  expect(parcels.length).toBe(30);
  const p7 = parcels.find((f: { properties: { parcel_id: string } }) => f.properties.parcel_id === "P-007");
  expect(p7.properties.integrated_version).toBeGreaterThanOrEqual(1);
  expect(p7.geometry.type).toBe("MultiPolygon"); // approved repair applied (bow-tie -> two valid parts)
  expect(fc.metadata.disclaimer).toMatch(/not proof of ownership/);
  expect(parcels.every((f: { properties: Record<string, unknown> }) => !("owner_name" in f.properties))).toBe(true);

  // audit trail verifies
  await page.getByRole("link", { name: "Audit" }).click();
  await page.getByRole("button", { name: "Verify chain" }).click();
  await expect(page.getByText(/Hash chain intact/)).toBeVisible();
});
