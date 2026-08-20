// Multi-series time chart powered by uPlot. Step-rendered (right-step:
// each point's value applies for the bucket starting at that timestamp).
//
// uPlot wants a wide-format aligned matrix [xs, ...ys] — feeds with mixed
// cadences (e.g. 1h DA vs 5min RT) get pivoted onto the union of timestamps
// here, with `null` filling buckets where a feed has no data.

import { forwardRef, useEffect, useImperativeHandle, useMemo, useRef } from "react";
import uPlot from "uplot";
import "uplot/dist/uPlot.min.css";
import { FEED_COLOR } from "../feedStyle";
import type { FeedSeries } from "../types";

export interface ChartHandle {
  resetZoom: () => void;
}

export interface CursorReadout {
  /** Unix seconds at the cursor, or null if cursor is off-canvas. */
  time: number | null;
  /** Values keyed by feed_id at the cursor index. null = no data at that bucket. */
  values: Record<string, number | null>;
}

const COLORS = {
  bg: "#0a0a0c",
  surface: "#14141a",
  border: "#26262e",
  text: "#e6e6e8",
  muted: "#6b6b7a",
  accent: "#fbbf24",
} as const;

// Feeds that fill the area between the line and y=0 — gives the classic
// P&L visual where above/below baseline are clearly distinguishable. Each
// pos/neg side has 0 at every "inactive" timestamp so the colored areas
// meet cleanly at the zero baseline. The series gets a stroke too, but a
// per-side clipping rectangle (set up in the drawClear hook below) hides
// the half of the stroke that would otherwise paint a colored line along
// y=0 during inactive stretches — so positives only get a green border
// above zero, negatives only a red border below.
const FEED_FILL: Record<string, string> = {
  da_rt_spread_pos: "rgba(52, 211, 153, 0.55)",
  da_rt_spread_neg: "rgba(251, 113, 133, 0.55)",
  net_load_pos: "rgba(52, 211, 153, 0.55)",
  net_load_neg: "rgba(251, 113, 133, 0.55)",
  // Strategy cleared MW: violet for discharge (sells), red for charge (buys).
  strategy_cleared_pos: "rgba(167, 139, 250, 0.55)",
  strategy_cleared_neg: "rgba(251, 113, 133, 0.55)",
};

// Optional per-feed dash pattern. Used to mark "ghost" lines that don't
// represent the same decision-time contract as the rest of the pane (e.g. the
// PF ceiling, which is solved with full foresight and ignores decision_time).
const FEED_DASH: Record<string, number[]> = {
  strategy_pf_ceiling: [6, 4],
};

interface Props {
  series: FeedSeries[];
  height?: number;
  // Group key for cross-pane cursor sync.
  syncKey?: string;
  /** Fired on every cursor move so a parent can render a custom shared
   *  legend / value inspector in its own DOM. */
  onCursor?: (readout: CursorReadout) => void;
  /** Unix seconds — vertical reference line drawn across the canvas.
   *  Null = no pin. */
  targetPin?: number | null;
  /** feed_id → color override. Used by views like Overlay where each
   *  "feed" is a date string and FEED_COLOR has no entry. */
  colorOverride?: Record<string, string>;
  /** Optional fixed x-domain, in Unix seconds. Keeps partially-visible
   *  partially-loaded feeds on the full selected operating-day axis. */
  xDomain?: { min: number; max: number };
  /** Per-feed scale assignment. When any feed maps to "y2" a secondary
   *  right-side y-axis is rendered. Lets a chart mix unrelated units
   *  (e.g. $/MWh on the left, MW on the right) without one swamping the
   *  other. Feeds not in the map default to "y" (left axis). */
  feedAxis?: Record<string, "y" | "y2">;
}

interface AlignedData {
  xs: number[];
  ys: (number | null)[][];
  feeds: string[];
}

/**
 * Pivot per-feed (time, value) lists into uPlot's [xs, y0, y1, ...] matrix.
 *
 * The union of timestamps is built from NON-NULL points only. Server returns
 * resampled output where coarse feeds (e.g. half-hourly reg_requirement at a
 * 5-min auto resolution) have lots of NaN buckets — including those in xs
 * makes each sparse feed's column mostly null, and uPlot's stepped paths
 * don't reliably span gaps for that shape. Dropping null timestamps yields
 * a dense column per feed and the steps render correctly.
 *
 * Cross-feed alignment for mixed cadences (DA hourly + RT 5-min in same
 * pane) still works: union covers every cadence's data points; each feed's
 * column has its known values at its own timestamps, null at the others;
 * `spanGaps:true` + step rendering connects them as right-steps.
 */
function alignFeeds(series: FeedSeries[], xDomain?: { min: number; max: number }): AlignedData {
  const allTs = new Set<number>();
  if (xDomain) {
    allTs.add(xDomain.min);
    allTs.add(xDomain.max);
  }
  for (const s of series) {
    for (const p of s.points) {
      if (p.value != null && !Number.isNaN(p.value)) allTs.add(p.time);
    }
  }
  const xs = Array.from(allTs).sort((a, b) => a - b);
  const tsIndex = new Map<number, number>();
  for (let i = 0; i < xs.length; i++) tsIndex.set(xs[i]!, i);

  const ys: (number | null)[][] = [];
  const feeds: string[] = [];
  for (const s of series) {
    const col: (number | null)[] = new Array(xs.length).fill(null);
    let nonNullCount = 0;
    let onlyValue: number | null = null;
    for (const p of s.points) {
      if (p.value == null || Number.isNaN(p.value)) continue;
      const idx = tsIndex.get(p.time);
      if (idx != null) {
        col[idx] = p.value;
        nonNullCount++;
        onlyValue = p.value;
      }
    }
    if (xDomain && s.feed.startsWith("gen_outage_") && nonNullCount === 1 && onlyValue != null) {
      const lo = tsIndex.get(xDomain.min);
      const hi = tsIndex.get(xDomain.max);
      if (lo != null) col[lo] = onlyValue;
      if (hi != null) col[hi] = onlyValue;
    }
    ys.push(col);
    feeds.push(s.feed);
  }
  return { xs, ys, feeds };
}

export const ChartContainer = forwardRef<ChartHandle, Props>(function ChartContainer(
  { series, height = 400, syncKey = "panes", onCursor, targetPin, colorOverride, xDomain, feedAxis },
  ref,
) {
  const hasSecondary = useMemo(
    () => feedAxis ? Object.values(feedAxis).some((s) => s === "y2") : false,
    [feedAxis],
  );
  const containerRef = useRef<HTMLDivElement | null>(null);
  const plotRef = useRef<uPlot | null>(null);
  // uPlot's draw hook closures are bound at chart construction; updating
  // targetPin via prop must reach the hook through a ref + redraw().
  const targetPinRef = useRef<number | null>(targetPin ?? null);

  const aligned = useMemo(() => alignFeeds(series, xDomain), [series, xDomain]);
  const domainKey = xDomain ? `${xDomain.min}:${xDomain.max}` : "";

  useEffect(() => {
    targetPinRef.current = targetPin ?? null;
    plotRef.current?.redraw(false);
  }, [targetPin]);

  useImperativeHandle(ref, () => ({
    resetZoom: () => {
      const u = plotRef.current;
      if (!u || aligned.xs.length === 0) return;
      u.setScale("x", {
        min: xDomain?.min ?? aligned.xs[0]!,
        max: xDomain?.max ?? aligned.xs[aligned.xs.length - 1]!,
      });
    },
  }), [aligned, xDomain]);

  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;

    const stepped = (uPlot.paths as any).stepped({ align: 1 });

    // The uPlot legend is disabled (see `legend` below), so no per-series
    // value formatters here — PaneStack renders the live readout via onCursor.
    const seriesOpts: uPlot.Series[] = [
      { label: "Time (EPT)" },
      ...aligned.feeds.map((feed) => {
        const fill = FEED_FILL[feed];
        const isFillPair = feed in FEED_FILL;
        const scale = feedAxis?.[feed] === "y2" ? "y2" : "y";
        const dash = FEED_DASH[feed];
        return {
          label: feed,
          stroke: colorOverride?.[feed] ?? FEED_COLOR[feed] ?? COLORS.text,
          width: 2,
          paths: stepped,
          points: { show: false },
          spanGaps: !isFillPair,
          scale,
          ...(fill ? { fill, fillTo: 0 } : {}),
          ...(dash ? { dash } : {}),
        } satisfies uPlot.Series;
      }),
    ];

    const opts: uPlot.Options = {
      width: el.clientWidth,
      height,
      // Time axis renders in EPT.
      tzDate: (ts: number) => uPlot.tzDate(new Date(ts * 1e3), "America/New_York"),
      series: seriesOpts,
      // Always include y=0 in the visible range so charts have a consistent
      // baseline reference (e.g. reg_requirement at 500-800 MW would otherwise
      // hide 0 entirely). Pads the data extreme by 5%.
      scales: (() => {
        const yRange: uPlot.Scale.Range = (_u, dataMin, dataMax) => {
          if (dataMin == null || dataMax == null) return [0, 1];
          if (dataMin >= 0) {
            const top = Math.max(dataMax, 0);
            return [0, top + (top || 1) * 0.05];
          }
          if (dataMax <= 0) {
            const bot = Math.min(dataMin, 0);
            return [bot - Math.abs(bot || 1) * 0.05, 0];
          }
          const span = dataMax - dataMin;
          return [dataMin - span * 0.05, dataMax + span * 0.05];
        };
        const s: uPlot.Options["scales"] = {
          x: xDomain ? { range: () => [xDomain.min, xDomain.max] } : {},
          y: { range: yRange },
        };
        if (hasSecondary) s!.y2 = { range: yRange };
        return s;
      })(),
      cursor: {
        sync: { key: syncKey, scales: ["x", null], match: [() => true, () => true] },
        focus: { prox: 16 },
      },
      // uPlot's default multi-row legend takes 80-120px per pane in a stacked
      // layout. We hide it here and let the parent render a compact inline
      // readout in its title strip via the `onCursor` prop.
      legend: { show: false },
      hooks: {
        // Recompute per-side clip rectangles before each draw and assign
        // them to the fill-pair series. This hides the half of each side's
        // stroke that would otherwise paint a colored line along y=0
        // during inactive stretches (where the series carries 0). Pos
        // strokes/fills only appear above y=0; neg only below.
        drawClear: [
          (u) => {
            const y0 = u.valToPos(0, "y", true);
            if (y0 == null || !isFinite(y0)) return;
            const left = u.bbox.left;
            const top = u.bbox.top;
            const w = u.bbox.width;
            const h = u.bbox.height;
            const above = new Path2D();
            above.rect(left, top, w, Math.max(0, y0 - top));
            const below = new Path2D();
            below.rect(left, y0, w, Math.max(0, top + h - y0));
            for (let i = 1; i < u.series.length; i++) {
              const s = u.series[i] as any;
              const label: string | undefined = s.label;
              if (!label) continue;
              if (label.endsWith("_pos")) s.clip = above;
              else if (label.endsWith("_neg")) s.clip = below;
            }
          },
        ],
        draw: [
          (u) => {
            const ctx = u.ctx;

            // Zero reference line for panes whose data crosses zero
            // (any fill-to-zero feed). Drawn beneath the target pin.
            const hasFill = aligned.feeds.some((f) => f in FEED_FILL);
            if (hasFill) {
              const y0 = u.valToPos(0, "y", true);
              if (y0 != null && isFinite(y0)) {
                ctx.save();
                ctx.strokeStyle = "rgba(230, 230, 232, 0.35)";
                ctx.lineWidth = 1;
                ctx.setLineDash([4, 3]);
                ctx.beginPath();
                ctx.moveTo(u.bbox.left, y0);
                ctx.lineTo(u.bbox.left + u.bbox.width, y0);
                ctx.stroke();
                ctx.restore();
              }
            }

            // Render the target-pin vertical reference line. Drawn after
            // the canvas paints so it sits over the gridlines and series.
            const t = targetPinRef.current;
            if (t == null) return;
            const x = u.valToPos(t, "x", true);
            if (x == null || !isFinite(x)) return;
            ctx.save();
            ctx.strokeStyle = "#fbbf24";
            ctx.lineWidth = 2;
            ctx.beginPath();
            ctx.moveTo(x, u.bbox.top);
            ctx.lineTo(x, u.bbox.top + u.bbox.height);
            ctx.stroke();
            // Small triangle marker at the top so the pin is identifiable
            // even when the cursor is exactly on top of it.
            ctx.fillStyle = "#fbbf24";
            ctx.beginPath();
            ctx.moveTo(x - 5, u.bbox.top);
            ctx.lineTo(x + 5, u.bbox.top);
            ctx.lineTo(x, u.bbox.top + 6);
            ctx.closePath();
            ctx.fill();
            ctx.restore();
          },
        ],
        setCursor: [
          (u) => {
            if (!onCursor) return;
            const idx = u.cursor.idx;
            if (idx == null || idx < 0) {
              onCursor({ time: null, values: {} });
              return;
            }
            const t = u.data[0]?.[idx] ?? null;
            const values: Record<string, number | null> = {};
            for (let i = 0; i < aligned.feeds.length; i++) {
              // Step-rendered series: report the last-known value at or
              // before the cursor (mirrors what's drawn). Without this, an
              // hourly DA shows `—` at every non-HH:00 cursor position even
              // though the chart visibly shows DA flat through the hour.
              const col = u.data[i + 1];
              let v: number | null = null;
              if (col) {
                for (let j = idx; j >= 0; j--) {
                  const w = col[j];
                  if (w != null && !Number.isNaN(w)) {
                    v = Number(w);
                    break;
                  }
                }
              }
              values[aligned.feeds[i]!] = v;
            }
            onCursor({ time: typeof t === "number" ? t : null, values });
          },
        ],
      },
      axes: [
        // Time axis: rely on uPlot's built-in multi-tier formatter, which
        // adapts to zoom level (HH:MM at sub-day, MM/DD when zoomed out,
        // \nM/D suffix on day rollover). Driven by `tzDate` above so labels
        // render in America/New_York.
        {
          stroke: COLORS.muted,
          grid: { stroke: COLORS.border, width: 1 },
          ticks: { stroke: COLORS.border, width: 1 },
          font: "11px JetBrains Mono, ui-monospace, monospace",
        },
        // Y axis: explicit `size` + `values`. Without an explicit formatter
        // uPlot's auto-tick can collapse to zero labels in short panes (the
        // 140-px spread pane was rendering with no Y-axis labels). The unit
        // prefix is picked from the first feed so prices show $ and MW
        // feeds show plain numbers.
        {
          stroke: COLORS.muted,
          grid: { stroke: COLORS.border, width: 1 },
          ticks: { stroke: COLORS.border, width: 1 },
          font: "11px JetBrains Mono, ui-monospace, monospace",
          size: 55,
          // Tighten min spacing between ticks (default 50px) so short panes
          // (~110px plot area) fit 3+ labels instead of just 0.
          space: 30,
          values: (_u, splits) =>
            splits.map((v) => {
              if (v == null) return "";
              const f = aligned.feeds.find((feed) => feedAxis?.[feed] !== "y2")
                ?? aligned.feeds[0] ?? "";
              if (/lmp|mcp|spread/.test(f)) {
                const sign = v < 0 ? "−" : "";
                const abs = Math.abs(v);
                return `${sign}$${abs >= 100 ? abs.toFixed(0) : abs.toFixed(abs < 10 ? 1 : 0)}`;
              }
              if (/perfscore/.test(f)) return v.toFixed(2);
              return v.toFixed(0);
            }),
        },
        // Optional secondary y-axis (right side). Mirrors the primary
        // styling but pulls its formatter from the first feed assigned
        // to "y2".
        ...(hasSecondary ? [{
          scale: "y2",
          side: 1,
          stroke: COLORS.muted,
          grid: { show: false },
          ticks: { stroke: COLORS.border, width: 1 },
          font: "11px JetBrains Mono, ui-monospace, monospace",
          size: 55,
          space: 30,
          values: (_u: uPlot, splits: number[]) =>
            splits.map((v) => {
              if (v == null) return "";
              const f = aligned.feeds.find((feed) => feedAxis?.[feed] === "y2") ?? "";
              if (/lmp|mcp|spread/.test(f)) {
                const sign = v < 0 ? "−" : "";
                const abs = Math.abs(v);
                return `${sign}$${abs >= 100 ? abs.toFixed(0) : abs.toFixed(abs < 10 ? 1 : 0)}`;
              }
              if (/perfscore/.test(f)) return v.toFixed(2);
              return v.toFixed(0);
            }),
        } satisfies uPlot.Axis] : []),
      ],
    };

    const data: uPlot.AlignedData = [aligned.xs, ...aligned.ys] as any;
    const u = new uPlot(opts, data, el);
    plotRef.current = u;

    const ro = new ResizeObserver(() => {
      if (!el || !plotRef.current) return;
      plotRef.current.setSize({ width: el.clientWidth, height });
    });
    ro.observe(el);

    // Double-click to reset zoom — uPlot doesn't bind this by default in our
    // config since drag-to-zoom is on.
    const onDblClick = () => {
      if (aligned.xs.length === 0) return;
      u.setScale("x", {
        min: xDomain?.min ?? aligned.xs[0]!,
        max: xDomain?.max ?? aligned.xs[aligned.xs.length - 1]!,
      });
    };
    el.addEventListener("dblclick", onDblClick);

    return () => {
      ro.disconnect();
      el.removeEventListener("dblclick", onDblClick);
      u.destroy();
      plotRef.current = null;
    };
    // Recreate the plot when feed list changes (column count changes uPlot
    // schema). For data-only updates (same feeds, new points), the second
    // effect below uses setData on the existing instance.
  }, [aligned.feeds.join("|"), height, syncKey, domainKey, hasSecondary,
      // Recreate the plot when axis assignment changes so series are
      // bound to the right scale.
      aligned.feeds.map((f) => `${f}:${feedAxis?.[f] ?? "y"}`).join("|")]);

  // Fast-path: same feed list, new data → setData in place.
  useEffect(() => {
    const u = plotRef.current;
    if (!u) return;
    const data: uPlot.AlignedData = [aligned.xs, ...aligned.ys] as any;
    u.setData(data);
  }, [aligned]);

  const handleReset = () => {
    const u = plotRef.current;
    if (!u || aligned.xs.length === 0) return;
    u.setScale("x", {
      min: xDomain?.min ?? aligned.xs[0]!,
      max: xDomain?.max ?? aligned.xs[aligned.xs.length - 1]!,
    });
  };

  return (
    <div
      className="relative w-full"
      style={{ background: COLORS.surface }}
    >
      {/* No fixed height on the mount node: uPlot's `height` option sizes the
          canvas, and the legend is rendered as a sibling INSIDE `.uplot`
          below the canvas. If we constrain this div, the legend gets clipped
          and the next pane visually overlaps it. */}
      <div
        ref={containerRef}
        className="w-full"
        style={{ background: COLORS.surface }}
      />
      <button
        onClick={handleReset}
        title="Reset zoom (or double-click chart)"
        className="absolute top-2 right-2 z-10 bg-[#0a0a0c]/80 backdrop-blur border border-[#26262e] hover:border-[#fbbf24] text-[#e6e6e8] rounded-[4px] px-2 py-1 text-[11px] font-mono"
      >
        Reset zoom
      </button>
    </div>
  );
});
