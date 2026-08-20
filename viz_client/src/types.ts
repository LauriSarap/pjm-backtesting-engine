// Types matching the viz_server JSON contract. Arrow IPC responses are
// decoded into the same shapes by `api/series.ts`.

export interface FeedMeta {
  feed_id: string;
  display_name: string;
  pane: "prices" | "load" | "gen" | "as" | "outages" | "forecast";
  unit: string;
  zonal: boolean;
  has_revisions: boolean;
}

export interface SeriesPoint {
  // Unix epoch seconds (UTC) — uPlot's x values are seconds, not ms.
  time: number;
  value: number | null;
}

export interface FeedSeries {
  feed: string;
  source_zone: string;
  points: SeriesPoint[];
}

export interface SeriesResponse {
  resolution: string;
  decision_time: string;
  target_from: string;
  target_to: string;
  partial: boolean;
  errors: { feed: string; code: string; message: string }[];
  feeds: FeedSeries[];
}

// Strategy overlay: types for /api/strategy_runs and /api/strategy_series.
// The viz tool reads parquet emitted by pjm_engine.runner via pjm_eval.io.
// Schema is contracted in docs/design.md §"Output schema".

export interface StrategyRunMeta {
  run: string;          // e.g. "demo"
  strategy: string;     // e.g. "perfect_foresight"
  asset: string;        // "example_a"
  window: { from: string; to: string };
  is_pf_ceiling: boolean;
}

export interface ClearedAward {
  t: string;            // period_start_utc ISO
  t_end: string;        // period_end_utc ISO
  mw: number;           // signed: + discharge, − charge
  product: string;      // "DA_Energy", "RT_Energy", "Reg_v2", ...
  revenue: number;      // $
}

export interface CumRevenuePoint {
  t: string;            // period_start_utc ISO
  value: number;        // running cumulative $
}

export interface StrategySeriesResponse {
  strategy: StrategyRunMeta;
  decision_time: string;
  target_from: string;
  target_to: string;
  cleared_mw: ClearedAward[];
  cum_revenue: CumRevenuePoint[];
  pf_ceiling: CumRevenuePoint[] | null;
  partial: boolean;
}
