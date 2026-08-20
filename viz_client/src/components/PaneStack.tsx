// Vertical stack of charts with a shared cursor.
//
// Each pane is a ChartContainer rendering a subset of the fetched feeds.
// All panes share `syncKey` so uPlot's `cursor.sync` mirrors the crosshair
// across them — when you hover one pane, the others' cursors and readouts
// update in lockstep.
//
// Pane title strip doubles as the live legend: feed name + value at the
// cursor, color-marked, single horizontal row. uPlot's own multi-row legend
// is disabled (see ChartContainer) so panes can stack tightly.

import { useEffect, useMemo, useState } from "react";
import { ChartContainer, type CursorReadout } from "./ChartContainer";
import { FEED_COLOR, FEED_FORMAT } from "../feedStyle";
import type { FeedSeries } from "../types";

const TIME_FORMATTER = new Intl.DateTimeFormat("en-US", {
  timeZone: "America/New_York",
  weekday: "short",
  month: "short",
  day: "2-digit",
  year: "numeric",
  hour: "2-digit",
  minute: "2-digit",
  hour12: false,
});

function formatCursorTime(unixSeconds: number | null): string {
  if (unixSeconds == null) return "—";
  return TIME_FORMATTER.format(new Date(unixSeconds * 1000)) + " EPT";
}

export interface PaneSpec {
  id: string;
  label: string;
  feeds: string[];
  height: number;
  /** "chart" (default) renders a uPlot pane. "tiles" renders one stat box
   *  per feed — better for daily-cadence data where a step chart is just
   *  flat horizontal lines (e.g. gen outages). */
  kind?: "chart" | "tiles";
}

interface Props {
  panes: PaneSpec[];
  series: FeedSeries[];
  syncKey?: string;
  /** Unix seconds — vertical reference line drawn across every pane.
   *  Null = no pin. */
  targetPin?: number | null;
  /** Pushed up when the synced cursor moves. Lets the parent track
   *  the live cursor time for keyboard pin / display in a header. */
  onCursorTime?: (unixSeconds: number | null) => void;
  /** Fixed x-domain, in Unix seconds. */
  xDomain?: { min: number; max: number };
}

const FEED_LABEL: Record<string, string> = {
  rto_load_actual: "actual",
  rto_load_forecast_da: "DA forecast",
  rto_load_forecast_1h: "1h-before forecast",
  rto_load_forecast_asof: "latest at decision",
  rto_solar_actual: "actual",
  rto_solar_forecast_da: "DA forecast",
  rto_solar_forecast_1h: "1h-before forecast",
  rto_solar_forecast_asof: "latest at decision",
  rto_wind_actual: "actual",
  rto_wind_forecast_da: "DA forecast",
  rto_wind_forecast_1h: "1h-before forecast",
  rto_wind_forecast_asof: "latest at decision",
  gen_outage_total: "Total",
  gen_outage_forced: "Forced",
  gen_outage_planned: "Planned",
  gen_outage_maintenance: "Maintenance",
  strategy_cleared_pos: "discharge",
  strategy_cleared_neg: "charge",
  strategy_cum_revenue: "strategy",
  strategy_pf_ceiling: "ceiling (oracle, energy-only)",
};

function formatValue(feed: string, v: number | null): string {
  if (v == null) return "—";
  const fmt = FEED_FORMAT[feed];
  return fmt ? fmt(v) : v.toFixed(2);
}

/** Latest non-null value at or before `t`. Mirrors stepped right-step
 *  rendering — at any cursor / pin position you see "the value as of now."
 *  Series points are time-sorted, so a binary search is enough.
 */
function valueAt(points: { time: number; value: number | null }[], t: number): number | null {
  if (points.length === 0) return null;
  let lo = 0;
  let hi = points.length - 1;
  while (lo < hi) {
    const mid = (lo + hi + 1) >>> 1;
    if (points[mid]!.time <= t) lo = mid;
    else hi = mid - 1;
  }
  if (points[lo]!.time > t) return null;
  for (let i = lo; i >= 0; i--) {
    const v = points[i]!.value;
    if (v != null && !Number.isNaN(v)) return v;
  }
  return null;
}

/** Stages we render in the revision sparkline, in chronological order.
 *  Each pane's set of feeds is matched against this template via the
 *  trailing suffix; non-matching panes skip the sparkline. */
const REVISION_STAGES: { suffix: string; label: string; color: string }[] = [
  { suffix: "_forecast_da", label: "DA", color: "#4f8cff" },
  { suffix: "_forecast_1h", label: "1h", color: "#fbbf24" },
  { suffix: "_forecast_asof", label: "asof", color: "#34d399" },
  { suffix: "_actual", label: "actual", color: "#94a3b8" },
];

/** Small inline SVG: one vertical bar per revision stage at the pin.
 *  Bar heights are proportional to value relative to the max across
 *  stages; baseline is at 0. */
function RevisionSparkline({
  values,
  format,
}: {
  values: { label: string; color: string; value: number | null }[];
  format: (v: number) => string;
}) {
  const present = values.filter((v) => v.value != null);
  if (present.length < 2) return null;
  const max = Math.max(...present.map((v) => v.value!));
  const min = Math.min(0, ...present.map((v) => v.value!));
  const span = Math.max(1, max - min);
  const W = 12;
  const GAP = 2;
  const H = 22;
  return (
    <div className="flex items-center gap-2 ml-2">
      <svg
        width={values.length * (W + GAP) - GAP}
        height={H}
        className="shrink-0"
      >
        {values.map((v, i) => {
          if (v.value == null) {
            return (
              <rect key={v.label} x={i * (W + GAP)} y={H - 1} width={W} height={1}
                fill="#26262e" />
            );
          }
          const h = ((v.value - min) / span) * (H - 1);
          return (
            <rect
              key={v.label}
              x={i * (W + GAP)}
              y={H - h}
              width={W}
              height={h}
              fill={v.color}
              opacity={0.85}
            >
              <title>{v.label}: {format(v.value)}</title>
            </rect>
          );
        })}
      </svg>
      <span className="text-[10px] text-[#6b6b7a] tabular-nums whitespace-nowrap">
        {present.map((v, i) => {
          const prev = i > 0 ? present[i - 1]!.value! : null;
          const delta = prev != null ? v.value! - prev : null;
          return (
            <span key={v.label} style={{ color: v.color }}>
              {i > 0 ? " → " : ""}
              {v.label}
              {delta != null && (
                <span className="text-[#6b6b7a]">
                  {" "}({delta >= 0 ? "+" : ""}{format(delta)})
                </span>
              )}
            </span>
          );
        })}
      </span>
    </div>
  );
}

const EMPTY_SERIES: FeedSeries[] = [];
const EMPTY_SET: Set<string> = new Set();

export function PaneStack({ panes, series, syncKey = "panes", targetPin, onCursorTime, xDomain }: Props) {
  const [readouts, setReadouts] = useState<Record<string, CursorReadout>>({});
  // Per-pane disabled feeds. Click a legend pill in a pane's title strip to
  // toggle that feed's visibility within that pane only.
  const [disabled, setDisabled] = useState<Record<string, Set<string>>>({});

  // Memoize per-pane series slices so their identity is stable across
  // re-renders triggered by cursor moves or App slider drags. Without this,
  // every keystroke/pixel rebuilds 6 uPlot instances.
  const paneSeriesMap = useMemo(() => {
    const m = new Map<string, FeedSeries[]>();
    for (const p of panes) {
      const off = disabled[p.id] ?? EMPTY_SET;
      m.set(
        p.id,
        series.filter((s) => p.feeds.includes(s.feed) && !off.has(s.feed)),
      );
    }
    return m;
  }, [panes, series, disabled]);

  const toggleFeed = (paneId: string, feed: string) => {
    setDisabled((prev) => {
      const next = { ...prev };
      const set = new Set(next[paneId] ?? []);
      if (set.has(feed)) set.delete(feed);
      else set.add(feed);
      next[paneId] = set;
      return next;
    });
  };

  // All panes share a synced cursor — pick the most recent non-null time
  // from any pane to show in the top readout. (Hovering one pane updates
  // the others' readouts via cursor.sync, so any of them is current.)
  const cursorTime = useMemo<number | null>(() => {
    for (const r of Object.values(readouts)) {
      if (r?.time != null) return r.time;
    }
    return null;
  }, [readouts]);

  useEffect(() => {
    onCursorTime?.(cursorTime);
  }, [cursorTime, onCursorTime]);

  // When a target is pinned, the legend strip should show values AT THE PIN
  // instead of at the cursor. This is the heart of the bitemporal demo:
  // pin a target hour, then scrub decision_time and watch the per-feed
  // values revise (DA forecast → 1h forecast → actual). Without this, the
  // forecast revisions are visible only as line redraws, not numbers.
  const pinnedValues = useMemo<Record<string, number | null> | null>(() => {
    if (targetPin == null) return null;
    const m: Record<string, number | null> = {};
    for (const s of series) m[s.feed] = valueAt(s.points, targetPin);
    return m;
  }, [series, targetPin]);

  const isPinned = targetPin != null && pinnedValues != null;

  return (
    <div className="flex flex-col">
      <div className="flex items-center gap-4 px-3 py-2 bg-[#0c0c12] border-b border-[#26262e] text-[13px] font-mono">
        <span className="text-[#6b6b7a] text-[11px] uppercase tracking-wider shrink-0">cursor</span>
        <span className="text-[#fbbf24] tabular-nums">{formatCursorTime(cursorTime)}</span>
        {isPinned && (
          <>
            <span className="text-[#6b6b7a] text-[11px] uppercase tracking-wider shrink-0 ml-2">pin</span>
            <span className="text-[#fbbf24] tabular-nums">{formatCursorTime(targetPin!)}</span>
            <span className="text-[#6b6b7a] text-[10px] uppercase tracking-wider ml-2">
              legend shows values at pin · scrub decision to see revisions
            </span>
          </>
        )}
      </div>
      {panes.map((p) => {
        const paneSeries = paneSeriesMap.get(p.id) ?? EMPTY_SERIES;
        const off = disabled[p.id] ?? EMPTY_SET;
        const readout = readouts[p.id];
        // Build a chronological revision strip if this pane is a forecast
        // family: pick the feed in p.feeds that matches each stage suffix.
        // E.g. for the load forecast pane → rto_load_forecast_da → _1h →
        // _forecast_asof → rto_load_actual.
        let sparklineValues: { label: string; color: string; value: number | null }[] | null = null;
        let sparklineFormat: ((v: number) => string) | null = null;
        if (isPinned) {
          const matched = REVISION_STAGES.map((stage) => {
            const feed = p.feeds.find((f) => f.endsWith(stage.suffix));
            return feed ? { ...stage, feed } : null;
          });
          if (matched.every((m) => m != null)) {
            sparklineValues = matched.map((m) => ({
              label: m!.label,
              color: m!.color,
              value: pinnedValues?.[m!.feed] ?? null,
            }));
            const sampleFeed = matched[0]!.feed;
            sparklineFormat = FEED_FORMAT[sampleFeed] ?? ((v: number) => v.toFixed(0));
          }
        }
        if (p.kind === "tiles") {
          // Tiles mode: one stat box per feed. Used for daily-cadence
          // feeds (gen outages) where a step chart is just flat lines.
          // Value source: pin > cursor > latest in series. Stays in sync
          // with the shared cursor when you hover any other pane.
          const tileTime: number | null =
            (isPinned ? targetPin : null) ?? cursorTime;
          const valueFor = (feed: string): number | null => {
            const s = paneSeries.find((x) => x.feed === feed);
            if (!s) return null;
            if (tileTime != null) return valueAt(s.points, tileTime);
            // Fall back to latest non-null in the series.
            for (let i = s.points.length - 1; i >= 0; i--) {
              const v = s.points[i]!.value;
              if (v != null && !Number.isNaN(v)) return v;
            }
            return null;
          };
          return (
            <div key={p.id} className="border-t border-[#26262e] first:border-t-0">
              <div className="flex items-center gap-3 px-3 py-2 bg-[#14141a] border-b border-[#26262e] text-[12px] font-mono">
                <span className="text-[#e6e6e8] uppercase tracking-wide shrink-0">{p.label}</span>
                <span className="text-[#6b6b7a] text-[10px]">
                  {tileTime != null ? formatCursorTime(tileTime) : "no cursor"}
                </span>
              </div>
              <div className="grid grid-cols-2 md:grid-cols-4 gap-2 p-3 bg-[#0c0c12]">
                {p.feeds.map((feed) => {
                  const v = valueFor(feed);
                  const color = FEED_COLOR[feed] ?? "#e6e6e8";
                  return (
                    <div
                      key={feed}
                      className="flex flex-col gap-1 px-3 py-2 rounded-[4px] border border-[#26262e] bg-[#14141a]"
                    >
                      <div className="flex items-center gap-1.5 text-[10px] text-[#6b6b7a] uppercase tracking-wider">
                        <span
                          className="inline-block rounded-[2px]"
                          style={{ width: 8, height: 8, background: color }}
                        />
                        {FEED_LABEL[feed] ?? feed.replace(/^gen_outage_/, "")}
                      </div>
                      <div
                        className="text-[20px] font-mono tabular-nums leading-none"
                        style={{ color }}
                      >
                        {formatValue(feed, v)}
                      </div>
                    </div>
                  );
                })}
              </div>
            </div>
          );
        }

        return (
          <div key={p.id} className="border-t border-[#26262e] first:border-t-0">
            <div className="flex items-center gap-3 px-3 py-1 bg-[#14141a] border-b border-[#26262e] text-[12px] font-mono">
              <span className="text-[#e6e6e8] uppercase tracking-wide shrink-0">{p.label}</span>
              {sparklineValues && sparklineFormat && (
                <RevisionSparkline values={sparklineValues} format={sparklineFormat} />
              )}
              <div className="flex items-center gap-4 overflow-hidden">
                {p.feeds.map((feed) => {
                  const isOff = off.has(feed);
                  return (
                    <button
                      key={feed}
                      onClick={() => toggleFeed(p.id, feed)}
                      title={isOff ? `Show ${feed}` : `Hide ${feed}`}
                      className={[
                        "flex items-center gap-1.5 whitespace-nowrap px-1.5 py-0.5 rounded-[3px]",
                        "hover:bg-[#0a0a0c] cursor-pointer select-none",
                        isOff ? "opacity-40" : "",
                      ].join(" ")}
                    >
                      <span
                        className="inline-block rounded-[2px]"
                        style={{
                          width: 10,
                          height: 10,
                          background: FEED_COLOR[feed] ?? "#e6e6e8",
                          // Hollow swatch when off — colored border, transparent fill.
                          ...(isOff
                            ? {
                                background: "transparent",
                                border: `2px solid ${FEED_COLOR[feed] ?? "#e6e6e8"}`,
                              }
                            : null),
                        }}
                      />
                      <span className={isOff ? "text-[#6b6b7a] line-through" : "text-[#6b6b7a]"}>
                        {FEED_LABEL[feed] ?? feed}
                      </span>
                      <span
                        className={[
                          "tabular-nums",
                          isPinned ? "text-[#fbbf24]" : "text-[#e6e6e8]",
                        ].join(" ")}
                        title={isPinned ? "value at pinned target" : "value at cursor"}
                      >
                        {isOff
                          ? "—"
                          : formatValue(
                              feed,
                              isPinned
                                ? pinnedValues?.[feed] ?? null
                                : readout?.values[feed] ?? null,
                            )}
                      </span>
                    </button>
                  );
                })}
              </div>
            </div>
            <ChartContainer
              series={paneSeries}
              height={p.height}
              syncKey={syncKey}
              targetPin={targetPin}
              xDomain={xDomain}
              onCursor={(r) => setReadouts((prev) => ({ ...prev, [p.id]: r }))}
            />
          </div>
        );
      })}
    </div>
  );
}
