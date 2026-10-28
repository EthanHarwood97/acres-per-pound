/**
 * acres-per-pound API + static asset host.
 *
 * Routes:
 *   GET /api/search   filtered, sorted, paginated listings
 *   GET /api/map      slim point set for the current filters
 *   GET /api/listing/:uid
 *   GET /api/regions  region facets
 *   GET /api/stats    coverage + freshness
 *   everything else   the SPA (assets binding)
 */

export interface Env {
  DB: D1Database;
  BUCKET: R2Bucket;
  ASSETS: Fetcher;
}

const SORTS: Record<string, string> = {
  gbp_in: "gbp_in",
  gbp_ex: "gbp_ex",
  gbp_ac: "gbp_ac",
  price: "price",
  in_sqft: "in_sqft",
  ex_sqft: "ex_sqft",
  acres: "acres",
  beds: "beds",
  published: "published",
  last_seen: "last_seen",
};

function num(v: string | null): number | null {
  if (v === null || v === "") return null;
  const n = Number(v);
  return Number.isFinite(n) ? n : null;
}

interface Filters {
  where: string;
  params: (string | number)[];
}

// Portal subtype labels are inconsistent ("detached", "detached bungalow",
// "link detached house"), so a type filter matches the whole family.
const TYPE_GROUPS: Record<string, string[]> = {
  detached: ["detached", "detached bungalow", "detached villa",
             "link detached house", "link-detached house"],
  "semi-detached": ["semi-detached", "semi-detached bungalow", "semi detached",
                    "link detached house"],
  terraced: ["terraced", "end of terrace", "terraced bungalow",
             "end terrace", "mid terrace"],
  bungalow: ["bungalow", "detached bungalow", "semi-detached bungalow",
             "terraced bungalow"],
  house: ["house", "town house", "cottage", "villa", "character property",
          "mews", "link detached house", "end of terrace"],
};

function buildFilters(url: URL): Filters {
  const w: string[] = [];
  const p: (string | number)[] = [];
  const add = (clause: string, val: string | number) => {
    w.push(clause);
    p.push(val);
  };

  const q = url.searchParams.get("q")?.trim();
  if (q) {
    add("(address LIKE ? OR postcode LIKE ?)", `%${q}%`);
    p.push(`%${q}%`);
    // note: two placeholders, one value -> adjust below
  }

  const eq = (key: string, col: string) => {
    const v = url.searchParams.get(key);
    if (v !== null && v !== "") add(`${col} = ?`, v);
  };
  eq("source", "source");
  eq("country", "country");
  eq("region", "region");

  const rng = (key: string, col: string) => {
    const min = num(url.searchParams.get(`${key}_min`));
    const max = num(url.searchParams.get(`${key}_max`));
    if (min !== null) add(`${col} >= ?`, min);
    if (max !== null) add(`${col} <= ?`, max);
  };
  rng("price", "price");
  rng("beds", "beds");
  rng("baths", "baths");
  rng("in_sqft", "in_sqft");
  rng("ex_sqft", "ex_sqft");
  rng("acres", "acres");
  rng("gbp_in", "gbp_in");
  rng("gbp_ex", "gbp_ex");
  rng("gbp_ac", "gbp_ac");

  const types = url.searchParams.get("type");
  if (types) {
    const clauses: string[] = [];
    for (const t of types.split(",").map((x) => x.trim().toLowerCase()).filter(Boolean)) {
      if (t === "land") {
        clauses.push("land_only = 1");
      } else if (TYPE_GROUPS[t]) {
        const subs = TYPE_GROUPS[t];
        clauses.push(`type IN (${subs.map(() => "?").join(",")})`);
        p.push(...subs);
      } else {
        clauses.push("type = ?");
        p.push(t);
      }
    }
    if (clauses.length) w.push(`(${clauses.join(" OR ")})`);
  }

  const landOnly = url.searchParams.get("land_only");
  if (landOnly === "1") w.push("land_only = 1");
  if (landOnly === "0") w.push("land_only = 0");

  if (url.searchParams.get("has_in") === "1") w.push("in_sqft IS NOT NULL");
  if (url.searchParams.get("has_ex") === "1") {
    // shared titles are hidden from the land columns, so they must not
    // count as "has measured land" either
    w.push("ex_sqft IS NOT NULL");
  }
  if (url.searchParams.get("no_highlands") === "1") w.push("highlands = 0");

  const bbox = url.searchParams.get("bbox");
  if (bbox) {
    const [minLng, minLat, maxLng, maxLat] = bbox.split(",").map(Number);
    if ([minLng, minLat, maxLng, maxLat].every(Number.isFinite)) {
      w.push("lng BETWEEN ? AND ? AND lat BETWEEN ? AND ?");
      p.push(minLng, maxLng, minLat, maxLat);
    }
  }

  const lat = num(url.searchParams.get("lat"));
  const lng = num(url.searchParams.get("lng"));
  const radius = num(url.searchParams.get("radius_mi"));
  if (lat !== null && lng !== null && radius !== null) {
    const dLat = radius / 69.0;
    const dLng = radius / (69.0 * Math.max(0.2, Math.cos((lat * Math.PI) / 180)));
    w.push("lat BETWEEN ? AND ? AND lng BETWEEN ? AND ?");
    p.push(lat - dLat, lat + dLat, lng - dLng, lng + dLng);
    // precise distance filter applied in JS after fetch
  }

  return { where: w.length ? `WHERE ${w.join(" AND ")}` : "", params: p };
}

function json(data: unknown, init: ResponseInit = {}): Response {
  return new Response(JSON.stringify(data), {
    ...init,
    headers: {
      "content-type": "application/json; charset=utf-8",
      "cache-control": "public, max-age=60",
      "access-control-allow-origin": "*",
      ...(init.headers || {}),
    },
  });
}

async function search(url: URL, env: Env): Promise<Response> {
  const { where, params } = buildFilters(url);
  const sortKey = SORTS[url.searchParams.get("sort") || "gbp_in"] || "gbp_in";
  const dir = (url.searchParams.get("dir") || "asc").toLowerCase() === "desc" ? "DESC" : "ASC";
  const limit = Math.min(Number(url.searchParams.get("limit") || 100), 500);
  const offset = Math.max(Number(url.searchParams.get("offset") || 0), 0);

  // NULLs always sort last regardless of direction
  const order = `ORDER BY (${sortKey} IS NULL) ASC, ${sortKey} ${dir}, price ASC`;
  const sql =
    `SELECT * FROM listings ${where} ${order} LIMIT ? OFFSET ?`;
  const stmt = env.DB.prepare(sql).bind(...params, limit, offset);
  const res = await stmt.all();
  const rows = (res.results || []) as Record<string, unknown>[];

  // precise radius filter (haversine) when requested
  let filtered = rows;
  const lat = num(url.searchParams.get("lat"));
  const lng = num(url.searchParams.get("lng"));
  const radius = num(url.searchParams.get("radius_mi"));
  if (lat !== null && lng !== null && radius !== null) {
    filtered = rows.filter((r) => {
      const rlat = r.lat as number, rlng = r.lng as number;
      if (typeof rlat !== "number" || typeof rlng !== "number") return false;
      const dLat = ((rlat - lat) * Math.PI) / 180;
      const dLng = ((rlng - lng) * Math.PI) / 180;
      const a =
        Math.sin(dLat / 2) ** 2 +
        Math.cos((lat * Math.PI) / 180) *
          Math.cos((rlat * Math.PI) / 180) *
          Math.sin(dLng / 2) ** 2;
      return 3958.8 * 2 * Math.atan2(Math.sqrt(a), Math.sqrt(1 - a)) <= radius;
    });
  }

  let total: number | null = null;
  if (url.searchParams.get("count") === "1") {
    const c = await env.DB.prepare(`SELECT COUNT(*) AS n FROM listings ${where}`)
      .bind(...params)
      .first<{ n: number }>();
    total = c?.n ?? 0;
  }
  return json({ rows: filtered, total, limit, offset });
}

async function mapPoints(url: URL, env: Env): Promise<Response> {
  const { where, params } = buildFilters(url);
  const limit = Math.min(Number(url.searchParams.get("limit") || 20000), 50000);
  const clause = where ? `${where} AND` : "WHERE";
  const res = await env.DB.prepare(
    `SELECT uid, source, lat, lng, price, gbp_in, gbp_ex, gbp_ac, in_sqft, ex_sqft, acres, land_only, type
     FROM listings ${clause} lat IS NOT NULL AND lng IS NOT NULL LIMIT ?`
  )
    .bind(...params, limit)
    .all();
  return json({ points: res.results || [], limit });
}

async function listing(uid: string, env: Env): Promise<Response> {
  const row = await env.DB.prepare("SELECT * FROM listings WHERE uid = ?")
    .bind(uid)
    .first<Record<string, unknown>>();
  if (!row) return json({ error: "not found" }, { status: 404 });
  let plot = null;
  if (row.plot_id) {
    try {
      const g = await env.DB.prepare("SELECT geojson FROM plot_geom WHERE plot_id = ?")
        .bind(row.plot_id)
        .first<{ geojson: string }>();
      if (g?.geojson) plot = JSON.parse(g.geojson);
    } catch {
      plot = null; // geometry table not synced yet
    }
  }
  return json({ listing: { ...row, plot_geojson: plot } });
}

async function regions(env: Env): Promise<Response> {
  const res = await env.DB.prepare(
    "SELECT * FROM regions ORDER BY med_gbp_ac ASC NULLS LAST"
  ).all();
  return json({ regions: res.results || [] });
}

async function stats(env: Env): Promise<Response> {
  const r = await env.DB.prepare(
    `SELECT
       COUNT(*) AS n,
       SUM(CASE WHEN in_sqft IS NOT NULL THEN 1 ELSE 0 END) AS with_in,
       SUM(CASE WHEN ex_sqft IS NOT NULL THEN 1 ELSE 0 END) AS with_ex,
       SUM(CASE WHEN acres IS NOT NULL THEN 1 ELSE 0 END) AS with_acres,
       SUM(CASE WHEN source='rightmove' THEN 1 ELSE 0 END) AS rightmove,
       SUM(CASE WHEN source='onthemarket' THEN 1 ELSE 0 END) AS onthemarket,
       SUM(CASE WHEN source='zoopla' THEN 1 ELSE 0 END) AS zoopla,
       MIN(price) AS min_price, MAX(price) AS max_price,
       MAX(last_seen) AS last_seen
     FROM listings`
  ).first();
  return json({ stats: r });
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const url = new URL(request.url);
    try {
      if (url.pathname === "/api/search") return await search(url, env);
      if (url.pathname === "/api/map") return await mapPoints(url, env);
      if (url.pathname === "/api/regions") return await regions(env);
      if (url.pathname === "/api/stats") return await stats(env);
      if (url.pathname.startsWith("/api/listing/")) {
        return await listing(decodeURIComponent(url.pathname.slice("/api/listing/".length)), env);
      }
      if (url.pathname.startsWith("/api/")) {
        return json({ error: "unknown endpoint" }, { status: 404 });
      }
      return env.ASSETS.fetch(request);
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      return json({ error: msg }, { status: 500 });
    }
  },
};
