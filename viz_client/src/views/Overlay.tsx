// Compare workspace. Stack of independent chart cards; each card picks
// its own feed set, zone, op-day, and N-days. With one feed selected and
// N≥2, the card behaves like the original day-overlay (each day a series
// re-anchored onto the anchor day, optional p10/50/90 bands). With
// multiple feeds, the card switches to a single-window layout — feeds
// share the time axis and split across two y-axes if their units differ.
//
// DST is handled by computing each day's EPT 00:00 in UTC explicitly.
// Spring-forward day is 23h, fall-back is 25h — re-anchoring uses the
// elapsed seconds from each day's local midnight, which preserves the
// time-of-day comparison correctly across the transition.

import { useEffect, useMemo, useState } from "react";
import { fromZonedTime } from "date-fns-tz";
import { ChartContainer } from "../components/ChartContainer";
import { fetchFeeds, fetchSeries, fetchZones } from "../api/series";
import type { FeedMeta, FeedSeries } from "../types";

const EPT = "America/New_York";

function eptMidnightSec(day: string): number {
  return fromZonedTime(`${day}T00:00:00`, EPT).getTime() / 1000;
}

function addUtcDays(day: string, n: number): string {
  const [y, m, d] = day.split("-").map(Number);
  const dt = new Date(Date.UTC(y!, m! - 1, d! + n));
  return dt.toISOString().slice(0, 10);
}

/** HSL hue rotation: most-recent day (i=0) = warm orange, oldest = cool blue.
 *  Keeps lightness high for visibility on the dark background. */
function dayColor(i: number, n: number): string {
  const t = n <= 1 ? 0 : i / (n - 1);
  const hue = 30 + t * 200;
  return `hsl(${hue.toFixed(0)}, 70%, 65%)`;
}

const FEED_PALETTE = [
  "#4f8cff", "#f97362", "#a78bfa", "#34d399",
  "#fbbf24", "#22d3ee", "#fb7185", "#94a3b8",
];

/** Map feed_id → unit class for axis splitting. */
const FEED_UNIT: Record<string, string> = {
  da_lmp: "$", rt_lmp: "$",
  reg_rmccp: "$", reg_rmpcp: "$",
  da_sr_mcp: "$", rt_sr_mcp: "$",
  reg_requirement: "MW",
  reg_perfscore: "score",
  load_metered: "MW",
  solar_gen: "MW", wind_gen: "MW",
  rto_load_actual: "MW",
  rto_load_forecast_da: "MW", rto_load_forecast_1h: "MW", rto_load_forecast_asof: "MW",
  rto_solar_actual: "MW",
  rto_solar_forecast_da: "MW", rto_solar_forecast_1h: "MW", rto_solar_forecast_asof: "MW",
  rto_wind_actual: "MW",
  rto_wind_forecast_da: "MW", rto_wind_forecast_1h: "MW", rto_wind_forecast_asof: "MW",
  rto_net_load_actual: "MW",
  rto_net_load_forecast_da: "MW", rto_net_load_forecast_1h: "MW", rto_net_load_forecast_asof: "MW",
  gen_outage_total: "MW", gen_outage_forced: "MW",
  gen_outage_planned: "MW", gen_outage_maintenance: "MW",
};

/** Assign each feed to "y" or "y2" so multi-unit charts get a split axis.
 *  Returns {} when all feeds share a unit. The first encountered unit
 *  takes the left axis; everything else goes right. */
function deriveFeedAxis(feeds: string[]): Record<string, "y" | "y2"> {
  if (feeds.length < 2) return {};
  const units = feeds.map((f) => FEED_UNIT[f] ?? "");
  const distinct = [...new Set(units)];
  if (distinct.length < 2) return {};
  const primary = distinct[0];
  const m: Record<string, "y" | "y2"> = {};
  for (let i = 0; i < feeds.length; i++) {
    m[feeds[i]!] = units[i] === primary ? "y" : "y2";
  }
  return m;
}

interface ChartCard {
  id: string;
  feeds: string[];
  zone: string;
  anchorDay: string;
  nDays: number;
  showBands: boolean;
}

const DEFAULT_ANCHOR = "2025-10-15";
const newId = () => `c${Math.random().toString(36).slice(2, 8)}`;

const DEFAULT_CARD = (): ChartCard => ({
  id: newId(),
  feeds: ["rt_lmp"],
  zone: "PECO",
  anchorDay: DEFAULT_ANCHOR,
  nDays: 7,
  showBands: true,
});

export function Overlay() {
  const [feeds, setFeeds] = useState<FeedMeta[]>([]);
  const [zones, setZones] = useState<string[]>([]);
  const [cards, setCards] = useState<ChartCard[]>(() => [DEFAULT_CARD()]);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    fetchFeeds().then(setFeeds).catch((e) => setError(String(e)));
    fetchZones().then(setZones).catch((e) => setError(String(e)));
  }, []);

  const update = (id: string, patch: Partial<ChartCard>) =>
    setCards((prev) => prev.map((c) => (c.id === id ? { ...c, ...patch } : c)));
  const remove = (id: string) =>
    setCards((prev) => (prev.length === 1 ? prev : prev.filter((c) => c.id !== id)));
  const add = () =>
    setCards((prev) => [...prev, { ...DEFAULT_CARD(), id: newId() }]);

  return (
    <div className="px-6 py-4 flex flex-col gap-4 h-full overflow-y-auto">
      <div>
        <h1 className="text-[16px] font-semibold mb-1">Compare</h1>
        <p className="text-[#6b6b7a] text-[12px]">
          Each card is independent. Pick one feed and N≥2 days to overlay
          a time-of-day pattern; pick multiple feeds to put them on a
          shared time axis (mixed units split across two y-axes).
        </p>
        {error && <p className="text-[#fb7185] text-[12px]">{error}</p>}
      </div>

      {cards.map((card, i) => (
        <OverlayCard
          key={card.id}
          card={card}
          feeds={feeds}
          zones={zones}
          onChange={(patch) => update(card.id, patch)}
          onRemove={cards.length > 1 ? () => remove(card.id) : null}
          index={i}
        />
      ))}

      <button
        onClick={add}
        className="self-start bg-[#0a0a0c] border border-[#26262e] hover:border-[#fbbf24] rounded-[4px] px-3 py-2 text-[12px] font-mono text-[#e6e6e8]"
      >
        + add chart
      </button>
    </div>
  );
}

interface CardProps {
  card: ChartCard;
  feeds: FeedMeta[];
  zones: string[];
  onChange: (patch: Partial<ChartCard>) => void;
  onRemove: (() => void) | null;
  index: number;
}

function OverlayCard({ card, feeds, zones, onChange, onRemove, index }: CardProps) {
  const { feeds: selectedFeeds, zone, anchorDay, nDays, showBands } = card;
  const [raw, setRaw] = useState<FeedSeries[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Multi-feed mode collapses N to 1 — overlaying many feeds across many
  // days produces a hairball. The slider stays editable but is ignored.
  const isMultiFeed = selectedFeeds.length > 1;
  const effectiveN = isMultiFeed ? 1 : nDays;
  const isStackingDays = !isMultiFeed && effectiveN >= 2;

  const anyZonal = useMemo(() => {
    if (selectedFeeds.length === 0) return false;
    return selectedFeeds.some((id) => feeds.find((f) => f.feed_id === id)?.zonal);
  }, [selectedFeeds, feeds]);

  const { days, fromUtcSec, toUtcSec, anchorSec } = useMemo(() => {
    const list: string[] = [];
    for (let i = 0; i < effectiveN; i++) list.push(addUtcDays(anchorDay, -i));
    const earliest = list[list.length - 1]!;
    const latest = list[0]!;
    return {
      days: list,
      fromUtcSec: eptMidnightSec(earliest),
      toUtcSec: eptMidnightSec(addUtcDays(latest, 1)),
      anchorSec: eptMidnightSec(latest),
    };
  }, [anchorDay, effectiveN]);

  useEffect(() => {
    if (selectedFeeds.length === 0) {
      setRaw([]);
      return;
    }
    const ctrl = new AbortController();
    setLoading(true);
    setError(null);
    fetchSeries({
      feeds: selectedFeeds,
      zone: anyZonal ? zone : undefined,
      targetFrom: new Date(fromUtcSec * 1000),
      targetTo: new Date(toUtcSec * 1000),
      decisionTime: new Date(),
      resolution: "auto",
      signal: ctrl.signal,
    })
      .then((r) => {
        setRaw(r.feeds);
        if (r.partial) setError(`partial: ${r.errors.map((e) => e.code).join(",")}`);
      })
      .catch((e) => {
        if (e.name !== "AbortError") setError(String(e));
      })
      .finally(() => setLoading(false));
    return () => ctrl.abort();
  }, [selectedFeeds.join("|"), zone, fromUtcSec, toUtcSec, anyZonal]);

  // ---------- chart data assembly ----------
  // Stacking days, single feed: re-anchor each day onto anchor's midnight.
  // Multi-feed: pass raw through (no re-anchoring; feeds share the wall-clock).
  const chartSeries = useMemo<FeedSeries[]>(() => {
    if (raw.length === 0) return [];
    if (isStackingDays) {
      const r = raw[0]!;
      return days.map((day) => {
        const dayStart = eptMidnightSec(day);
        const dayEnd = eptMidnightSec(addUtcDays(day, 1));
        const points = r.points
          .filter((p) => p.time >= dayStart && p.time < dayEnd && p.value != null)
          .map((p) => ({ time: anchorSec + (p.time - dayStart), value: p.value }));
        return { feed: day, source_zone: r.source_zone, points };
      });
    }
    return raw;
  }, [raw, isStackingDays, days, anchorSec]);

  // p10/50/90 bands: only meaningful when stacking ≥2 days of one feed.
  const bands = useMemo<FeedSeries[]>(() => {
    if (!showBands || !isStackingDays || chartSeries.length < 2) return [];
    const tsSet = new Set<number>();
    for (const day of chartSeries) for (const p of day.points) tsSet.add(p.time);
    const xs = Array.from(tsSet).sort((a, b) => a - b);
    const ptr = chartSeries.map(() => 0);
    const last: (number | null)[] = chartSeries.map(() => null);
    const p10: { time: number; value: number | null }[] = [];
    const p50: { time: number; value: number | null }[] = [];
    const p90: { time: number; value: number | null }[] = [];
    const quantile = (sorted: number[], p: number) => {
      const idx = (sorted.length - 1) * p;
      const lo = Math.floor(idx);
      const hi = Math.ceil(idx);
      const f = idx - lo;
      return sorted[lo]! * (1 - f) + sorted[hi]! * f;
    };
    for (const ts of xs) {
      const vals: number[] = [];
      for (let di = 0; di < chartSeries.length; di++) {
        const day = chartSeries[di]!;
        while (ptr[di]! < day.points.length && day.points[ptr[di]!]!.time <= ts) {
          const v = day.points[ptr[di]!]!.value;
          if (v != null) last[di] = v;
          ptr[di] = ptr[di]! + 1;
        }
        const v = last[di];
        if (v != null) vals.push(v);
      }
      if (vals.length >= 2) {
        vals.sort((a, b) => a - b);
        p10.push({ time: ts, value: quantile(vals, 0.1) });
        p50.push({ time: ts, value: quantile(vals, 0.5) });
        p90.push({ time: ts, value: quantile(vals, 0.9) });
      }
    }
    return [
      { feed: "p10", source_zone: zone, points: p10 },
      { feed: "p50", source_zone: zone, points: p50 },
      { feed: "p90", source_zone: zone, points: p90 },
    ];
  }, [showBands, isStackingDays, chartSeries, zone]);

  const finalSeries = useMemo(() => [...chartSeries, ...bands], [chartSeries, bands]);

  // ---------- color overrides ----------
  const colorOverride = useMemo(() => {
    const m: Record<string, string> = {};
    if (isStackingDays) {
      days.forEach((day, i) => (m[day] = dayColor(i, days.length)));
    } else {
      selectedFeeds.forEach((feed, i) => (m[feed] = FEED_PALETTE[i % FEED_PALETTE.length]!));
    }
    if (showBands && isStackingDays) {
      m.p10 = "#94a3b8";
      m.p50 = "#e6e6e8";
      m.p90 = "#94a3b8";
    }
    return m;
  }, [isStackingDays, days, selectedFeeds, showBands]);

  const feedAxis = useMemo(
    () => (isStackingDays ? {} : deriveFeedAxis(selectedFeeds)),
    [isStackingDays, selectedFeeds],
  );

  // ---------- UI ----------
  return (
    <div className="border border-[#26262e] rounded-[4px] bg-[#14141a]">
      <div className="flex items-start gap-3 px-3 py-2 border-b border-[#26262e]">
        <span className="text-[#6b6b7a] text-[11px] font-mono uppercase tracking-wider mt-1.5 shrink-0">
          chart {index + 1}
        </span>
        <div className="flex flex-wrap items-center gap-2 text-[12px] font-mono flex-1">
          <FeedMultiSelect
            allFeeds={feeds}
            selected={selectedFeeds}
            onChange={(s) => onChange({ feeds: s })}
          />
          {anyZonal && (
            <label className="flex items-center gap-1.5">
              <span className="text-[#6b6b7a]">zone</span>
              <select
                value={zone}
                onChange={(e) => onChange({ zone: e.target.value })}
                className="bg-[#0a0a0c] border border-[#26262e] rounded-[3px] px-1.5 py-0.5"
              >
                {zones.map((z) => <option key={z} value={z}>{z}</option>)}
              </select>
            </label>
          )}
          <label className="flex items-center gap-1.5">
            <span className="text-[#6b6b7a]">anchor</span>
            <input
              type="date"
              value={anchorDay}
              onChange={(e) => onChange({ anchorDay: e.target.value })}
              className="bg-[#0a0a0c] border border-[#26262e] rounded-[3px] px-1.5 py-0.5"
            />
          </label>
          <label
            className={[
              "flex items-center gap-1.5",
              isMultiFeed ? "opacity-40" : "",
            ].join(" ")}
            title={isMultiFeed ? "Multi-feed mode pins N=1" : undefined}
          >
            <span className="text-[#6b6b7a]">N days</span>
            <input
              type="range"
              min={1}
              max={14}
              step={1}
              value={nDays}
              disabled={isMultiFeed}
              onChange={(e) => onChange({ nDays: Number(e.target.value) })}
              className="accent-[#fbbf24]"
            />
            <span className="text-[#e6e6e8] w-6 text-right">{effectiveN}</span>
          </label>
          {isStackingDays && (
            <label className="flex items-center gap-1.5 cursor-pointer select-none">
              <input
                type="checkbox"
                checked={showBands}
                onChange={(e) => onChange({ showBands: e.target.checked })}
                className="accent-[#fbbf24]"
              />
              <span className="text-[#6b6b7a]">p10/50/90</span>
            </label>
          )}
          <span className="ml-auto text-[#6b6b7a] text-[11px]">
            {loading ? "loading…" : ""}
            {error ? <span className="text-[#fb7185]">{error}</span> : null}
          </span>
        </div>
        {onRemove && (
          <button
            onClick={onRemove}
            title="Remove this chart"
            className="text-[#6b6b7a] hover:text-[#fb7185] text-[14px] leading-none mt-1.5 shrink-0"
          >
            ×
          </button>
        )}
      </div>

      {isStackingDays ? (
        <div className="flex flex-wrap gap-3 text-[10px] font-mono px-3 py-1 border-b border-[#26262e]">
          {days.map((day, i) => (
            <span key={day} className="flex items-center gap-1.5">
              <span
                className="inline-block rounded-[2px]"
                style={{ width: 8, height: 8, background: dayColor(i, days.length) }}
              />
              <span className="text-[#e6e6e8]">{day}</span>
              <span className="text-[#6b6b7a]">
                {new Date(`${day}T12:00:00`).toLocaleString("en-US", {
                  weekday: "short",
                  timeZone: EPT,
                })}
              </span>
            </span>
          ))}
          {showBands && (
            <>
              <span className="flex items-center gap-1.5">
                <span className="inline-block rounded-[2px]" style={{ width: 8, height: 8, background: "#94a3b8" }} />
                <span className="text-[#e6e6e8]">p10/p90</span>
              </span>
              <span className="flex items-center gap-1.5">
                <span className="inline-block rounded-[2px]" style={{ width: 8, height: 8, background: "#e6e6e8" }} />
                <span className="text-[#e6e6e8]">p50</span>
              </span>
            </>
          )}
        </div>
      ) : (
        // Multi-feed legend — feed pills with their assigned colors. Side
        // marker (L/R) shows which y-axis the feed maps to.
        selectedFeeds.length > 0 && (
          <div className="flex flex-wrap gap-3 text-[10px] font-mono px-3 py-1 border-b border-[#26262e]">
            {selectedFeeds.map((feed) => {
              const side = feedAxis[feed] === "y2" ? "R" : "L";
              return (
                <span key={feed} className="flex items-center gap-1.5">
                  <span
                    className="inline-block rounded-[2px]"
                    style={{ width: 8, height: 8, background: colorOverride[feed] ?? "#e6e6e8" }}
                  />
                  <span className="text-[#e6e6e8]">{feed}</span>
                  {Object.keys(feedAxis).length > 0 && (
                    <span className="text-[#6b6b7a]">
                      [{side}: {FEED_UNIT[feed] ?? ""}]
                    </span>
                  )}
                </span>
              );
            })}
          </div>
        )
      )}

      <ChartContainer
        series={finalSeries}
        height={420}
        // Shared sync key across all cards anchored to the same op-day.
        // Both modes resolve to a wall-clock or anchor-relative range
        // covering [anchor 00:00, anchor+1 00:00] in EPT, so cards
        // with matching anchor days will synchronize their cursors.
        // Different anchors get different keys → no cross-day sync.
        syncKey={`overlay-${anchorDay}`}
        colorOverride={colorOverride}
        feedAxis={feedAxis}
      />
    </div>
  );
}

interface FeedMultiSelectProps {
  allFeeds: FeedMeta[];
  selected: string[];
  onChange: (next: string[]) => void;
}

function FeedMultiSelect({ allFeeds, selected, onChange }: FeedMultiSelectProps) {
  const [open, setOpen] = useState(false);
  const toggle = (id: string) => {
    if (selected.includes(id)) onChange(selected.filter((x) => x !== id));
    else onChange([...selected, id]);
  };
  return (
    <div className="relative">
      <button
        onClick={() => setOpen((o) => !o)}
        className="bg-[#0a0a0c] border border-[#26262e] hover:border-[#fbbf24] rounded-[3px] px-2 py-0.5 text-[12px] font-mono text-left min-w-[200px]"
        title="Choose feeds"
      >
        <span className="text-[#6b6b7a]">feeds: </span>
        <span className="text-[#e6e6e8]">
          {selected.length === 0
            ? "(none)"
            : selected.length === 1
              ? selected[0]
              : `${selected.length} selected`}
        </span>
      </button>
      {open && (
        <div
          className="absolute top-full left-0 mt-1 z-30 bg-[#0c0c12] border border-[#26262e] rounded-[3px] py-1 max-h-[400px] overflow-y-auto min-w-[260px] shadow-xl"
          onMouseLeave={() => setOpen(false)}
        >
          {allFeeds.map((f) => {
            const checked = selected.includes(f.feed_id);
            return (
              <label
                key={f.feed_id}
                className={[
                  "flex items-center gap-2 px-2 py-1 text-[12px] font-mono cursor-pointer hover:bg-[#14141a]",
                  checked ? "text-[#fbbf24]" : "text-[#e6e6e8]",
                ].join(" ")}
              >
                <input
                  type="checkbox"
                  checked={checked}
                  onChange={() => toggle(f.feed_id)}
                  className="accent-[#fbbf24]"
                />
                <span>{f.feed_id}</span>
                <span className="text-[#6b6b7a] text-[10px] ml-auto">
                  {FEED_UNIT[f.feed_id] ?? ""}
                </span>
              </label>
            );
          })}
        </div>
      )}
    </div>
  );
}
