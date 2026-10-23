export type Position = number[];

export type Geometry =
  | { type: "Point"; coordinates: Position }
  | { type: "MultiPoint"; coordinates: Position[] }
  | { type: "LineString"; coordinates: Position[] }
  | { type: "MultiLineString"; coordinates: Position[][] }
  | { type: "Polygon"; coordinates: Position[][] }
  | { type: "MultiPolygon"; coordinates: Position[][][] }
  | { type: "GeometryCollection"; geometries: Geometry[] };

export interface Feature<P = Record<string, unknown>> {
  type: "Feature";
  id?: number | string;
  geometry: Geometry | null;
  properties: P;
}

export interface FeatureCollection<P = Record<string, unknown>> {
  type: "FeatureCollection";
  features: Feature<P>[];
  numberMatched?: number;
  numberReturned?: number;
}

/** [[south, west], [north, east]] bounds of any geometry list, or null. */
export function boundsOf(geoms: (Geometry | null | undefined)[]): [[number, number], [number, number]] | null {
  let minx = Infinity,
    miny = Infinity,
    maxx = -Infinity,
    maxy = -Infinity;
  const visit = (c: unknown): void => {
    if (Array.isArray(c) && typeof c[0] === "number") {
      const [x, y] = c as number[];
      minx = Math.min(minx, x);
      maxx = Math.max(maxx, x);
      miny = Math.min(miny, y);
      maxy = Math.max(maxy, y);
    } else if (Array.isArray(c)) c.forEach(visit);
  };
  const walk = (g: Geometry | null | undefined) => {
    if (!g) return;
    if (g.type === "GeometryCollection") g.geometries.forEach(walk);
    else visit(g.coordinates);
  };
  geoms.forEach(walk);
  if (!Number.isFinite(minx)) return null;
  return [
    [miny, minx],
    [maxy, maxx],
  ];
}
