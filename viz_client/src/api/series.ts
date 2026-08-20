// Typed client for /api/series. Decodes Arrow IPC into per-feed point arrays
// indexed by feed_id.

import { tableFromIPC } from "apache-arrow";
import type {
  FeedMeta,
  FeedSeries,
  SeriesPoint,
  SeriesResponse,
  StrategyRunMeta,
  StrategySeriesResponse,
} from "../types";

export interface SeriesQuery {
  feeds: string[];
  zone?: string;
  targetFrom: Date;
  targetTo: Date;
  decisionTime: Date;
  resolution?: "auto" | "5min" | "15min" | "1h" | "1d";
  signal?: AbortSignal;
}

const ISO = (d: Date) => d.toISOString();

function buildSearch(q: SeriesQuery): URLSearchParams {
  const p = new URLSearchParams({
    feeds: q.feeds.join(","),
    target_from: ISO(q.targetFrom),
    target_to: ISO(q.targetTo),
    decision_time: ISO(q.decisionTime),
    resolution: q.resolution ?? "auto",
  });
  if (q.zone) p.set("zone", q.zone);
  return p;
}

export async function fetchSeries(q: SeriesQuery): Promise<SeriesResponse> {
  const url = `/api/series?${buildSearch(q)}`;
  const r = await fetch(url, { signal: q.signal });
  if (!r.ok) {
    let body = "";
    try { body = await r.text(); } catch {}
    throw new Error(`/api/series ${r.status}: ${body}`);
  }

  const buf = new Uint8Array(await r.arrayBuffer());
  const table = tableFromIPC(buf);

  const meta = table.schema.metadata;
  const get = (k: string): string => meta.get(k) ?? "";

  // Columnar extraction.
  const tCol = table.getChild("t");
  const feedCol = table.getChild("feed");
  const valCol = table.getChild("value");
  const zoneCol = table.getChild("source_zone");

  const grouped = new Map<string, FeedSeries>();
  const n = table.numRows;
  for (let i = 0; i < n; i++) {
    const feed = String(feedCol?.get(i));
    let series = grouped.get(feed);
    if (!series) {
      series = { feed, source_zone: String(zoneCol?.get(i) ?? ""), points: [] };
      grouped.set(feed, series);
    }
    const tns = tCol?.get(i);
    const time = ns_to_unix_seconds(tns);
    const raw = valCol?.get(i);
    const value: number | null = raw == null || Number.isNaN(raw) ? null : Number(raw);
    series.points.push({ time, value } satisfies SeriesPoint);
  }

  const errors_json = get("errors_json");
  return {
    resolution: get("resolution"),
    decision_time: get("decision_time"),
    target_from: get("target_from"),
    target_to: get("target_to"),
    partial: get("partial") === "true",
    errors: errors_json ? JSON.parse(errors_json) : [],
    feeds: Array.from(grouped.values()),
  };
}

// Arrow timestamp[ns,UTC] decodes to BigInt nanoseconds.
function ns_to_unix_seconds(v: unknown): number {
  if (typeof v === "bigint") return Number(v / 1_000_000_000n);
  if (v instanceof Date) return Math.floor(v.getTime() / 1000);
  if (typeof v === "number") return Math.floor(v / 1000); // ms fallback
  throw new Error(`unexpected timestamp type: ${typeof v}`);
}

export async function fetchFeeds(): Promise<FeedMeta[]> {
  const r = await fetch("/api/feeds");
  if (!r.ok) throw new Error(`/api/feeds ${r.status}`);
  const j = await r.json();
  return j.feeds;
}

export async function fetchZones(): Promise<string[]> {
  const r = await fetch("/api/zones");
  if (!r.ok) throw new Error(`/api/zones ${r.status}`);
  const j = await r.json();
  return j.zones;
}

// ─── Strategy overlay endpoints ───────────────────────────────────────────

export async function fetchStrategyRuns(): Promise<StrategyRunMeta[]> {
  const r = await fetch("/api/strategy_runs");
  if (!r.ok) throw new Error(`/api/strategy_runs ${r.status}`);
  const j = await r.json();
  return j.runs;
}

export interface StrategySeriesQuery {
  run: string;
  strategy: string;
  asset: string;
  targetFrom: Date;
  targetTo: Date;
  decisionTime: Date;
  includePf?: boolean;
  signal?: AbortSignal;
}

export async function fetchStrategySeries(
  q: StrategySeriesQuery,
): Promise<StrategySeriesResponse> {
  const p = new URLSearchParams({
    run: q.run,
    strategy: q.strategy,
    asset: q.asset,
    target_from: ISO(q.targetFrom),
    target_to: ISO(q.targetTo),
    decision_time: ISO(q.decisionTime),
    include_pf: String(q.includePf ?? true),
  });
  const r = await fetch(`/api/strategy_series?${p}`, { signal: q.signal });
  if (!r.ok) {
    let body = "";
    try { body = await r.text(); } catch {}
    throw new Error(`/api/strategy_series ${r.status}: ${body}`);
  }
  return await r.json();
}
