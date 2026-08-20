import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { fromZonedTime, toZonedTime } from "date-fns-tz";
import { PaneStack, type PaneSpec } from "./components/PaneStack";
import { Sidebar, type ViewName } from "./components/Sidebar";
import { Reference } from "./views/Reference";
import { Overlay } from "./views/Overlay";
import {
  fetchFeeds,
  fetchSeries,
  fetchStrategyRuns,
  fetchStrategySeries,
  fetchZones,
} from "./api/series";
import { readUrl, writeUrl, type Resolution } from "./permalink";
import { useKeyboardScrub } from "./hooks/useKeyboardScrub";
import type {
  FeedMeta,
  FeedSeries,
  StrategyRunMeta,
  StrategySeriesResponse,
} from "./types";
import "./index.css";

// Synthetic feeds computed client-side after fetch — never appear in API
// requests. See `deriveSpread` and `deriveNetLoad` below.
const DERIVED_FEEDS = new Set<string>([
  "da_rt_spread_pos",
  "da_rt_spread_neg",
  "net_load_pos",
  "net_load_neg",
  // Strategy overlay synthetics (built from /api/strategy_series):
  "strategy_cleared_pos",
  "strategy_cleared_neg",
  "strategy_cum_revenue",
  "strategy_pf_ceiling",
]);

// Pane layout. Each pane shares the synced cursor; feeds within a pane
// must share a y-axis scale (we group LMPs and AS prices by magnitude).
// Heights are equal across panes — uniform stack reads better when scrubbing
// the bitemporal slider, and short panes (<150px) lose Y-axis labels to
// uPlot's tick density.
const PANE_HEIGHT = 260;
const PANES: PaneSpec[] = [
  {
    id: "prices",
    label: "Energy LMPs",
    feeds: ["da_lmp", "rt_lmp"],
    height: PANE_HEIGHT,
  },
  {
    id: "spread",
    label: "RT − DA Spread",
    feeds: ["da_rt_spread_pos", "da_rt_spread_neg"],
    height: PANE_HEIGHT,
  },
  {
    id: "as_prices",
    label: "AS Clearing Prices",
    feeds: ["reg_rmccp", "reg_rmpcp", "da_sr_mcp", "rt_sr_mcp"],
    height: PANE_HEIGHT,
  },
  {
    id: "load_renewables",
    label: "Load (zone) · Solar + Wind (RTO)",
    feeds: ["load_metered", "solar_gen", "wind_gen"],
    height: PANE_HEIGHT,
  },
  {
    id: "rto_load_forecast",
    label: "RTO Load Forecast",
    feeds: [
      "rto_load_actual",
      "rto_load_forecast_da",
      "rto_load_forecast_1h",
      "rto_load_forecast_asof",
    ],
    height: PANE_HEIGHT,
  },
  {
    id: "rto_solar_forecast",
    label: "RTO Solar Forecast",
    feeds: [
      "rto_solar_actual",
      "rto_solar_forecast_da",
      "rto_solar_forecast_1h",
      "rto_solar_forecast_asof",
    ],
    height: PANE_HEIGHT,
  },
  {
    id: "rto_wind_forecast",
    label: "RTO Wind Forecast",
    feeds: [
      "rto_wind_actual",
      "rto_wind_forecast_da",
      "rto_wind_forecast_1h",
      "rto_wind_forecast_asof",
    ],
    height: PANE_HEIGHT,
  },
  {
    id: "net_load",
    label: "Net Load (load − solar − wind)",
    feeds: ["net_load_pos", "net_load_neg"],
    height: PANE_HEIGHT,
  },
  {
    id: "outages",
    label: "Gen Outages (RTO, MW)",
    feeds: ["gen_outage_total", "gen_outage_forced", "gen_outage_planned", "gen_outage_maintenance"],
    height: PANE_HEIGHT,
    kind: "tiles",
  },
  {
    id: "reg_mw",
    label: "Reg Requirement (MW)",
    feeds: ["reg_requirement"],
    height: PANE_HEIGHT,
  },
  {
    id: "reg_score",
    label: "RTO Reg Performance Score",
    feeds: ["reg_perfscore"],
    height: PANE_HEIGHT,
  },
];

// Strategy overlay panes. Inserted into the pane stack only when a strategy
// is selected — empty placeholder panes look broken.
const STRATEGY_PANES: PaneSpec[] = [
  {
    id: "strategy_cleared",
    label: "Cleared MW (strategy)",
    feeds: ["strategy_cleared_pos", "strategy_cleared_neg"],
    height: PANE_HEIGHT,
  },
  {
    id: "strategy_cum_revenue",
    label: "Cumulative Revenue · Strategy vs PF Ceiling",
    feeds: ["strategy_cum_revenue", "strategy_pf_ceiling"],
    height: PANE_HEIGHT + 60, // extra room for the revenue comparison
  },
];

/**
 * Build synthetic FeedSeries entries from /api/strategy_series JSON. The
 * existing PaneStack/ChartContainer pipeline already handles pos/neg fill
 * pairs and dashed series via the FEED_* maps in those files — we just need
 * to hand it points keyed by Unix seconds (not ISO).
 *
 * Cleared MW → split into pos/neg pair so the green/red fill pattern from
 * spread/net-load applies (energy products only — DA_Energy + RT_Energy).
 * AS products would need a separate capacity-lane pane.
 *
 * Cum revenue + PF ceiling → two stepped lines; PF dashed via FEED_DASH.
 */
function deriveStrategyFeeds(r: StrategySeriesResponse | null): FeedSeries[] {
  if (!r) return [];

  const isoToSec = (iso: string) => Math.floor(new Date(iso).getTime() / 1000);

  const cleared_pos: { time: number; value: number | null }[] = [];
  const cleared_neg: { time: number; value: number | null }[] = [];
  for (const a of r.cleared_mw) {
    if (!(a.product === "DA_Energy" || a.product === "RT_Energy")) continue;
    const t = isoToSec(a.t);
    cleared_pos.push({ time: t, value: a.mw >= 0 ? a.mw : 0 });
    cleared_neg.push({ time: t, value: a.mw < 0 ? a.mw : 0 });
  }

  const cum_revenue = r.cum_revenue.map((p) => ({
    time: isoToSec(p.t),
    value: p.value,
  }));

  const out: FeedSeries[] = [
    { feed: "strategy_cleared_pos", source_zone: "", points: cleared_pos },
    { feed: "strategy_cleared_neg", source_zone: "", points: cleared_neg },
    { feed: "strategy_cum_revenue", source_zone: "", points: cum_revenue },
  ];
  if (r.pf_ceiling) {
    out.push({
      feed: "strategy_pf_ceiling",
      source_zone: "",
      points: r.pf_ceiling.map((p) => ({
        time: isoToSec(p.t),
        value: p.value,
      })),
    });
  }
  return out;
}

// Real (non-derived) feed_ids that the backend can serve.
const ALL_FEEDS = Array.from(
  new Set(PANES.flatMap((p) => p.feeds).filter((f) => !DERIVED_FEEDS.has(f))),
);

/**
 * Compute RT − DA spread by forward-filling the hourly DA value across the
 * 5-min RT cadence. Two-pointer walk over time-sorted points, O(n+m).
 *
 * Returns TWO synthetic series so the spread pane can color-code positive
 * and negative regions: `da_rt_spread_pos` carries non-negative values (else
 * null), `da_rt_spread_neg` carries negative values (else null). Each series
 * fills to y=0 so the area-above and area-below the zero line render in
 * green / red respectively. Returns [] if either input feed is missing.
 */
function deriveSpread(feeds: FeedSeries[]): FeedSeries[] {
  const rt = feeds.find((f) => f.feed === "rt_lmp");
  const da = feeds.find((f) => f.feed === "da_lmp");
  if (!rt || !da || rt.points.length === 0 || da.points.length === 0) return [];

  let i = 0;
  let lastDa: number | null = null;
  const pos: { time: number; value: number | null }[] = [];
  const neg: { time: number; value: number | null }[] = [];

  // Both sides have a point at every timestamp (the inactive side gets 0,
  // not null). With stepped fills to y=0 this means the green and red
  // areas meet at the zero line at every sign change — no gaps.
  for (const rp of rt.points) {
    while (i < da.points.length && da.points[i]!.time <= rp.time) {
      const v = da.points[i]!.value;
      if (v != null) lastDa = v;
      i++;
    }
    if (rp.value != null && lastDa != null) {
      const diff = rp.value - lastDa;
      pos.push({ time: rp.time, value: diff >= 0 ? diff : 0 });
      neg.push({ time: rp.time, value: diff < 0 ? diff : 0 });
    }
  }

  return [
    { feed: "da_rt_spread_pos", source_zone: rt.source_zone, points: pos },
    { feed: "da_rt_spread_neg", source_zone: rt.source_zone, points: neg },
  ];
}

/**
 * Net load = load − solar − wind. Forward-fills solar and wind at each
 * load timestamp (load metered is hourly, gen feeds are 5-min, so we walk
 * a single pointer per renewable feed and carry the most-recent value).
 *
 * Splits into two series so the pane renders green above 0 (load >
 * renewables, the normal case) and red below 0 (renewables exceed load —
 * rare but possible at low load + high wind). Same pattern as deriveSpread.
 *
 * Returns [] if load is missing. Solar/wind missing is fine — treated as 0.
 */
function deriveNetLoad(feeds: FeedSeries[]): FeedSeries[] {
  const load = feeds.find((f) => f.feed === "load_metered");
  if (!load || load.points.length === 0) return [];
  const solar = feeds.find((f) => f.feed === "solar_gen");
  const wind = feeds.find((f) => f.feed === "wind_gen");

  let si = 0;
  let wi = 0;
  let lastSolar: number | null = null;
  let lastWind: number | null = null;
  const pos: { time: number; value: number | null }[] = [];
  const neg: { time: number; value: number | null }[] = [];

  for (const lp of load.points) {
    if (solar) {
      while (si < solar.points.length && solar.points[si]!.time <= lp.time) {
        const v = solar.points[si]!.value;
        if (v != null) lastSolar = v;
        si++;
      }
    }
    if (wind) {
      while (wi < wind.points.length && wind.points[wi]!.time <= lp.time) {
        const v = wind.points[wi]!.value;
        if (v != null) lastWind = v;
        wi++;
      }
    }
    if (lp.value == null) continue;
    const net = lp.value - (lastSolar ?? 0) - (lastWind ?? 0);
    // Both sides always carry a value (inactive side = 0). Lets the green
    // and red fills meet cleanly at the zero baseline — see deriveSpread.
    pos.push({ time: lp.time, value: net >= 0 ? net : 0 });
    neg.push({ time: lp.time, value: net < 0 ? net : 0 });
  }

  return [
    { feed: "net_load_pos", source_zone: load.source_zone, points: pos },
    { feed: "net_load_neg", source_zone: load.source_zone, points: neg },
  ];
}
const EPT = "America/New_York";
const SLIDER_DEBOUNCE_MS = 80;

/** Operating day "YYYY-MM-DD" (EPT) → [from, to) UTC bounds spanning 24 EPT
 *  hours. Handles DST: spring-forward day is 23h, fall-back is 25h. */
function eptDayWindow(yyyymmdd: string): { from: string; to: string } {
  const [y, m, d] = yyyymmdd.split("-").map(Number);
  const fromUtc = fromZonedTime(`${yyyymmdd}T00:00:00`, EPT);
  const next = new Date(Date.UTC(y!, m! - 1, d! + 1));
  const nextDayStr = next.toISOString().slice(0, 10);
  const toUtc = fromZonedTime(`${nextDayStr}T00:00:00`, EPT);
  return {
    from: fromUtc.toISOString().replace(/\.\d+Z$/, "Z"),
    to: toUtc.toISOString().replace(/\.\d+Z$/, "Z"),
  };
}

function isoToEptDate(iso: string): string {
  const ept = toZonedTime(new Date(iso), EPT);
  const y = ept.getFullYear();
  const m = String(ept.getMonth() + 1).padStart(2, "0");
  const d = String(ept.getDate()).padStart(2, "0");
  return `${y}-${m}-${d}`;
}

function isoToInput(iso: string): string {
  return iso.replace("Z", "").slice(0, 16);
}
function inputToIso(value: string): string {
  return `${value}:00Z`;
}

const DEFAULT_OP_DAY = "2025-10-15";
const DEFAULT_WINDOW = eptDayWindow(DEFAULT_OP_DAY);

const TIME_FORMATTER = new Intl.DateTimeFormat("en-US", {
  timeZone: EPT,
  weekday: "short",
  month: "short",
  day: "2-digit",
  year: "numeric",
  hour: "2-digit",
  minute: "2-digit",
  hour12: false,
});

function formatEpt(iso: string): string {
  return TIME_FORMATTER.format(new Date(iso)) + " EPT";
}

/** Decision-time slider scrubs strictly within the displayed window —
 *  [target_from, target_to]. To examine pre-window publishes (D-1 DA gate)
 *  or settlement-time-published feeds (reg_market_results), widen the
 *  target window via the Op-day / From / To controls. */
function decisionRange(fromIso: string, toIso: string) {
  return {
    min: new Date(fromIso).getTime(),
    max: new Date(toIso).getTime(),
  };
}

/** Clamp an ISO timestamp into [fromIso, toIso]. */
function clampToWindow(iso: string, fromIso: string, toIso: string): string {
  const t = new Date(iso).getTime();
  const lo = new Date(fromIso).getTime();
  const hi = new Date(toIso).getTime();
  if (t < lo) return fromIso;
  if (t > hi) return toIso;
  return iso;
}

export function App() {
  const urlState = readUrl();

  const [zones, setZones] = useState<string[]>([]);
  const [zone, setZone] = useState<string>(urlState.zone ?? "PECO");
  const [feeds, setFeeds] = useState<FeedMeta[]>([]);
  const [series, setSeries] = useState<FeedSeries[]>([]);
  const [resolution, setResolution] = useState<Resolution>(urlState.resolution ?? "auto");
  const [fromIso, setFromIso] = useState<string>(urlState.fromIso ?? DEFAULT_WINDOW.from);
  const [toIso, setToIso] = useState<string>(urlState.toIso ?? DEFAULT_WINDOW.to);
  // decisionTime defaults to target_to ("everything published during the
  // displayed window is visible"). Slider is bounded to [from, to] so the
  // bitemporal cursor only scrubs through what the chart is showing.
  const [decisionIso, setDecisionIsoState] = useState<string>(
    clampToWindow(
      urlState.decisionIso ?? (urlState.toIso ?? DEFAULT_WINDOW.to),
      urlState.fromIso ?? DEFAULT_WINDOW.from,
      urlState.toIso ?? DEFAULT_WINDOW.to,
    ),
  );
  // Slider sets this immediately for snappy feedback; the debounced version
  // below is what actually drives fetchSeries.
  const [pendingDecisionIso, setPendingDecisionIso] = useState<string>(decisionIso);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [copied, setCopied] = useState(false);
  const [hiddenPanes, setHiddenPanes] = useState<Set<string>>(() => new Set());
  const [targetPin, setTargetPin] = useState<number | null>(null);

  // Strategy overlay state. Discovery list comes from /api/strategy_runs at
  // mount; selection drives /api/strategy_series.
  const [strategyRuns, setStrategyRuns] = useState<StrategyRunMeta[]>([]);
  const [selectedRun, setSelectedRun] = useState<string | null>(urlState.run ?? null);
  const [selectedStrategy, setSelectedStrategy] = useState<string | null>(
    urlState.strategy ?? null,
  );
  const [selectedAsset, setSelectedAsset] = useState<string | null>(urlState.asset ?? null);
  const [strategyData, setStrategyData] = useState<StrategySeriesResponse | null>(null);
  const [strategyToast, setStrategyToast] = useState<string | null>(null);
  // Ref, not state: cursor moves update this without re-rendering App.
  const cursorTimeRef = useRef<number | null>(null);
  const handleCursorTime = useCallback((t: number | null) => {
    cursorTimeRef.current = t;
  }, []);
  const [view, setView] = useState<ViewName>(() => {
    const h = window.location.hash;
    if (h === "#reference") return "reference";
    if (h === "#overlay") return "overlay";
    return "replay";
  });

  useEffect(() => {
    const next = view === "reference" ? "#reference" : view === "overlay" ? "#overlay" : "";
    if (window.location.hash !== next) {
      window.history.replaceState(null, "", `${window.location.pathname}${window.location.search}${next}`);
    }
  }, [view]);

  // Wrapper so the slider, keyboard, and Now button share one path.
  const setDecisionIso = (iso: string) => {
    setDecisionIsoState(iso);
    setPendingDecisionIso(iso);
  };

  // Sync state → URL on any change.
  useEffect(() => {
    writeUrl({
      zone, resolution, fromIso, toIso, decisionIso,
      run: selectedRun ?? undefined,
      strategy: selectedStrategy ?? undefined,
      asset: selectedAsset ?? undefined,
    });
  }, [zone, resolution, fromIso, toIso, decisionIso, selectedRun, selectedStrategy, selectedAsset]);

  // Debounce slider drag → committed decisionIso.
  useEffect(() => {
    if (pendingDecisionIso === decisionIso) return;
    const t = setTimeout(() => setDecisionIsoState(pendingDecisionIso), SLIDER_DEBOUNCE_MS);
    return () => clearTimeout(t);
  }, [pendingDecisionIso, decisionIso]);

  useKeyboardScrub({
    decisionIso,
    resetIso: toIso,
    setDecisionIso: (iso) => setDecisionIso(clampToWindow(iso, fromIso, toIso)),
    onTogglePin: () => {
      // Pin at the current cursor; if already pinned, unpin.
      if (targetPin != null) setTargetPin(null);
      else if (cursorTimeRef.current != null) setTargetPin(cursorTimeRef.current);
    },
  });

  // When the displayed window changes, keep decision inside the new range.
  useEffect(() => {
    const clamped = clampToWindow(decisionIso, fromIso, toIso);
    if (clamped !== decisionIso) setDecisionIso(clamped);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [fromIso, toIso]);

  useEffect(() => {
    fetchFeeds().then(setFeeds).catch((e) => setError(String(e)));
    fetchZones().then((z) => {
      setZones(z);
      if (z.length && !z.includes(zone)) setZone(z[0]!);
    }).catch((e) => setError(String(e)));
    fetchStrategyRuns().then((runs) => {
      setStrategyRuns(runs);
      // Soft-fail toast if a permalink referenced something not on disk.
      if (selectedRun && selectedStrategy) {
        const present = runs.some(
          (r) => r.run === selectedRun
            && r.strategy === selectedStrategy
            && (!selectedAsset || r.asset === selectedAsset),
        );
        if (!present) {
          setStrategyToast(
            `Strategy '${selectedStrategy}' / run '${selectedRun}' not on disk — clearing overlay`,
          );
          setSelectedRun(null);
          setSelectedStrategy(null);
          setSelectedAsset(null);
        }
      }
    }).catch((e) => setError(String(e)));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Auto-clear the strategy toast after a few seconds.
  useEffect(() => {
    if (!strategyToast) return;
    const t = setTimeout(() => setStrategyToast(null), 5000);
    return () => clearTimeout(t);
  }, [strategyToast]);

  // Fetch strategy series whenever selection or window/decision changes.
  useEffect(() => {
    if (!selectedRun || !selectedStrategy || !selectedAsset) {
      setStrategyData(null);
      return;
    }
    const ctrl = new AbortController();
    fetchStrategySeries({
      run: selectedRun,
      strategy: selectedStrategy,
      asset: selectedAsset,
      targetFrom: new Date(fromIso),
      targetTo: new Date(toIso),
      decisionTime: new Date(decisionIso),
      includePf: true,
      signal: ctrl.signal,
    })
      .then(setStrategyData)
      .catch((e) => {
        if (e.name !== "AbortError") {
          setStrategyToast(`strategy fetch failed: ${e}`);
          setStrategyData(null);
        }
      });
    return () => ctrl.abort();
  }, [selectedRun, selectedStrategy, selectedAsset, fromIso, toIso, decisionIso]);

  useEffect(() => {
    if (!zone) return;
    const from = new Date(fromIso);
    const to = new Date(toIso);
    if (!(to > from)) {
      setError("target_to must be after target_from");
      return;
    }
    const decision = new Date(decisionIso);

    const ctrl = new AbortController();
    setLoading(true);
    setError(null);
    fetchSeries({
      feeds: ALL_FEEDS,
      zone,
      targetFrom: from,
      targetTo: to,
      decisionTime: decision,
      resolution,
      signal: ctrl.signal,
    })
      .then((r) => {
        const derived = [...deriveSpread(r.feeds), ...deriveNetLoad(r.feeds)];
        setSeries([...r.feeds, ...derived]);
        if (r.partial) setError(`partial: ${r.errors.map(e => e.code).join(",")}`);
      })
      .catch((e) => {
        if (e.name !== "AbortError") setError(String(e));
      })
      .finally(() => setLoading(false));
    return () => ctrl.abort();
  }, [zone, resolution, fromIso, toIso, decisionIso]);

  const shiftWindow = (deltaDays: number) => {
    const fromMs = new Date(fromIso).getTime() + deltaDays * 86_400_000;
    const toMs = new Date(toIso).getTime() + deltaDays * 86_400_000;
    const newFrom = new Date(fromMs).toISOString().replace(/\.\d+Z$/, "Z");
    const newTo = new Date(toMs).toISOString().replace(/\.\d+Z$/, "Z");
    setFromIso(newFrom);
    setToIso(newTo);
    setDecisionIso(newTo);
  };

  const copyLink = async () => {
    try {
      await navigator.clipboard.writeText(window.location.href);
      setCopied(true);
      setTimeout(() => setCopied(false), 1200);
    } catch {
      setError("clipboard write failed");
    }
  };

  const togglePane = (id: string) => {
    setHiddenPanes((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  const resetDefaults = () => {
    setZone("PECO");
    setResolution("auto");
    setFromIso(DEFAULT_WINDOW.from);
    setToIso(DEFAULT_WINDOW.to);
    setDecisionIso(DEFAULT_WINDOW.to);
    setHiddenPanes(new Set());
    setSelectedRun(null);
    setSelectedStrategy(null);
    setSelectedAsset(null);
  };

  const strategySelected = !!(selectedRun && selectedStrategy && selectedAsset);
  const allPanes = useMemo(
    () => (strategySelected ? [...PANES, ...STRATEGY_PANES] : PANES),
    [strategySelected],
  );
  const visiblePanes = useMemo(
    () => allPanes.filter((p) => !hiddenPanes.has(p.id)),
    [allPanes, hiddenPanes],
  );

  const seriesWithStrategy = useMemo(() => {
    if (!strategySelected) return series;
    return [...series, ...deriveStrategyFeeds(strategyData)];
  }, [series, strategyData, strategySelected]);

  const range = decisionRange(fromIso, toIso);
  const sliderValue = new Date(pendingDecisionIso).getTime();
  const chartDomain = useMemo(() => ({
    min: Math.floor(new Date(fromIso).getTime() / 1000),
    max: Math.floor(new Date(toIso).getTime() / 1000),
  }), [fromIso, toIso]);

  return (
    <div className="h-screen flex bg-[#0a0a0c] text-[#e6e6e8] font-sans overflow-hidden">
      <Sidebar
        view={view}
        onViewChange={setView}
        zones={zones}
        zone={zone}
        onZoneChange={setZone}
        strategyRuns={strategyRuns}
        selectedRun={selectedRun}
        selectedStrategy={selectedStrategy}
        selectedAsset={selectedAsset}
        onStrategySelect={(run, strat, asset) => {
          setSelectedRun(run);
          setSelectedStrategy(strat);
          // If only run picked, clear strat+asset; if strat picked but no
          // asset, default to the only available asset.
          if (run && strat && !asset) {
            const assets = strategyRuns
              .filter((r) => r.run === run && r.strategy === strat)
              .map((r) => r.asset);
            setSelectedAsset(assets.length === 1 ? assets[0]! : null);
          } else {
            setSelectedAsset(asset);
          }
        }}
        panes={allPanes}
        hiddenPanes={hiddenPanes}
        onTogglePane={togglePane}
        onResetDefaults={resetDefaults}
      />

      {view === "reference" ? (
        <main className="flex-1 min-w-0 h-screen overflow-y-auto">
          <Reference />
        </main>
      ) : view === "overlay" ? (
        <main className="flex-1 min-w-0 h-screen overflow-y-auto">
          <Overlay />
        </main>
      ) : (
      <main className="flex-1 min-w-0 h-screen overflow-y-auto flex flex-col">
      {/* Header row 1 — resolution + permalink + status */}
      <div className="flex items-center gap-3 px-4 py-2 border-b border-[#26262e] bg-[#14141a] text-[13px]">
        <label className="flex items-center gap-2">
          <span className="text-[#6b6b7a]">Resolution</span>
          <select
            value={resolution}
            onChange={(e) => setResolution(e.target.value as Resolution)}
            className="bg-[#0a0a0c] border border-[#26262e] rounded-[4px] px-2 py-1 font-mono"
          >
            <option value="auto">auto</option>
            <option value="5min">5min</option>
            <option value="15min">15min</option>
            <option value="1h">1h</option>
            <option value="1d">1d</option>
          </select>
        </label>
        <div className="flex items-center gap-2 text-[#6b6b7a] text-[12px] font-mono">
          {visiblePanes.length}/{PANES.length} panes ·{" "}
          {visiblePanes.flatMap(p => p.feeds).length} feeds
        </div>
        <div className="ml-auto flex items-center gap-3">
          <span className="text-[#6b6b7a] font-mono text-[11px]">
            {loading ? "loading…" : ""}
            {error ? <span className="text-[#fb7185] ml-2">{error}</span> : null}
          </span>
          <button
            onClick={copyLink}
            className="bg-[#0a0a0c] border border-[#26262e] hover:border-[#fbbf24] rounded-[4px] px-2 py-1 text-[12px] font-mono"
            title="Copy permalink to clipboard"
          >
            {copied ? "copied" : "Copy link"}
          </button>
        </div>
      </div>

      {/* Header row 2 — decision-time slider (the bitemporal cursor) */}
      <div
        className="flex items-center gap-3 px-4 py-3 border-b border-[#26262e] bg-[#14141a] text-[13px] font-mono sticky top-0 z-20"
      >
        <span className="text-[#6b6b7a] shrink-0">decision</span>
        <span className="text-[#fbbf24] shrink-0 w-[230px]">{formatEpt(pendingDecisionIso)}</span>
        <div className="flex-1 relative">
          <input
            type="range"
            min={range.min}
            max={range.max}
            step={5 * 60_000}
            value={Math.min(Math.max(sliderValue, range.min), range.max)}
            onChange={(e) => setPendingDecisionIso(
              new Date(Number(e.target.value)).toISOString().replace(/\.\d+Z$/, "Z"),
            )}
            className="block w-full accent-[#fbbf24]"
            title="Scrub decision_time within the displayed window. Keys: , . 5min · [ ] 1h · R end-of-window"
          />
          {targetPin != null && (() => {
            // Pin position as a percentage of the slider's range. Clamped
            // to [0, 100]; if the pin is outside the visible window the
            // marker sits at the edge so it stays click-targetable.
            const pinMs = targetPin * 1000;
            const pct = Math.max(0, Math.min(100,
              ((pinMs - range.min) / Math.max(1, range.max - range.min)) * 100,
            ));
            const pinIso = new Date(pinMs).toISOString().replace(/\.\d+Z$/, "Z");
            const snap = () => setDecisionIso(clampToWindow(pinIso, fromIso, toIso));
            return (
              <button
                onClick={snap}
                style={{ left: `${pct}%` }}
                title={`Snap decision-time to pin (${formatEpt(pinIso)})`}
                className="absolute -top-1 -translate-x-1/2 leading-none text-[#fbbf24] hover:text-[#fde68a] cursor-pointer text-[14px] select-none"
              >
                ▼
              </button>
            );
          })()}
        </div>
        {targetPin != null && (() => {
          // Decision-time presets relative to the pin. Snap to the moment
          // a specific revision would have been knowable; lets you flip
          // between "DA result", "T-1h forecast", etc. without dragging.
          const pinMs = targetPin * 1000;
          const setDecMs = (ms: number) => {
            const iso = new Date(ms).toISOString().replace(/\.\d+Z$/, "Z");
            setDecisionIso(clampToWindow(iso, fromIso, toIso));
          };
          // DA market results post around 13:30 EPT on the day before
          // the operating hour. Compute via EPT-aware arithmetic.
          const pinEpt = toZonedTime(new Date(pinMs), EPT);
          const dayBefore = new Date(Date.UTC(
            pinEpt.getFullYear(), pinEpt.getMonth(), pinEpt.getDate() - 1,
          )).toISOString().slice(0, 10);
          const daResultMs = fromZonedTime(`${dayBefore}T13:30:00`, EPT).getTime();
          const presetBtn = (label: string, ms: number, title: string) => (
            <button
              key={label}
              onClick={() => setDecMs(ms)}
              className="bg-[#0a0a0c] border border-[#26262e] hover:border-[#fbbf24] rounded-[3px] px-1.5 py-0.5 text-[10px] shrink-0"
              title={title}
            >
              {label}
            </button>
          );
          return (
            <div className="flex items-center gap-1 shrink-0">
              {presetBtn("DA", daResultMs, "Snap decision to DA-result post (D-1 13:30 EPT)")}
              {presetBtn("T−1h", pinMs - 3_600_000, "1 hour before pinned target")}
              {presetBtn("T−15m", pinMs - 900_000, "15 minutes before pinned target")}
              {presetBtn("post-MTU", pinMs + 3_600_000, "1h after pin — actual lands")}
              <button
                onClick={() => setTargetPin(null)}
                className="bg-[#0a0a0c] border border-[#fbbf24] text-[#fbbf24] hover:bg-[#fbbf24]/10 rounded-[3px] px-1.5 py-0.5 text-[10px] shrink-0 ml-1"
                title="Unpin (T)"
              >
                unpin
              </button>
            </div>
          );
        })()}
        <span className="text-[#6b6b7a] text-[11px] shrink-0">
          , . 5min · [ ] 1h · R reset · T pin
        </span>
      </div>

      {/* Header row 3 — target window controls */}
      <div className="flex items-center gap-3 px-4 py-2 border-b border-[#26262e] bg-[#14141a] text-[13px] font-mono">
        <label className="flex items-center gap-2">
          <span className="text-[#6b6b7a]">Op day (EPT)</span>
          <input
            type="date"
            value={isoToEptDate(fromIso)}
            onChange={(e) => {
              if (!e.target.value) return;
              const w = eptDayWindow(e.target.value);
              setFromIso(w.from);
              setToIso(w.to);
              setDecisionIso(w.to);
            }}
            className="bg-[#0a0a0c] border border-[#26262e] rounded-[4px] px-2 py-1"
          />
        </label>
        <span className="text-[#6b6b7a]">From (UTC)</span>
        <input
          type="datetime-local"
          value={isoToInput(fromIso)}
          onChange={(e) => setFromIso(inputToIso(e.target.value))}
          step={300}
          className="bg-[#0a0a0c] border border-[#26262e] rounded-[4px] px-2 py-1"
        />
        <span className="text-[#6b6b7a]">To (UTC)</span>
        <input
          type="datetime-local"
          value={isoToInput(toIso)}
          onChange={(e) => setToIso(inputToIso(e.target.value))}
          step={300}
          className="bg-[#0a0a0c] border border-[#26262e] rounded-[4px] px-2 py-1"
        />
        <button
          onClick={() => shiftWindow(-1)}
          className="bg-[#0a0a0c] border border-[#26262e] rounded-[4px] px-2 py-1 hover:border-[#fbbf24]"
          title="Shift window back 1 day"
        >−1d</button>
        <button
          onClick={() => shiftWindow(1)}
          className="bg-[#0a0a0c] border border-[#26262e] rounded-[4px] px-2 py-1 hover:border-[#fbbf24]"
          title="Shift window forward 1 day"
        >+1d</button>
        <span className="text-[#6b6b7a] ml-auto">drag chart to zoom · double-click or Reset to reset</span>
      </div>

      {/* Pane stack — synced cursor */}
      <div className="p-1 flex-1">
        <div className="border border-[#26262e]">
          <PaneStack
            panes={visiblePanes}
            series={seriesWithStrategy}
            targetPin={targetPin}
            xDomain={chartDomain}
            onCursorTime={handleCursorTime}
          />
        </div>
      </div>
      {strategyToast && (
        <div className="fixed bottom-4 right-4 bg-[#14141a] border border-[#fbbf24] text-[#fbbf24] px-3 py-2 rounded-[4px] font-mono text-[12px] z-50">
          {strategyToast}
        </div>
      )}
      </main>
      )}
    </div>
  );
}

export default App;
