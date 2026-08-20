// Left rail: view nav, zone, strategy overlay selectors, pane visibility
// toggles, defaults reset. Width fixed so the chart canvas size stays
// predictable across reloads.

import type { PaneSpec } from "./PaneStack";
import type { StrategyRunMeta } from "../types";

export type ViewName = "replay" | "overlay" | "reference";

interface Props {
  view: ViewName;
  onViewChange: (v: ViewName) => void;

  zones: string[];
  zone: string;
  onZoneChange: (z: string) => void;

  // Strategy overlay. All optional; when strategy is unset the pane stack
  // hides the strategy panes.
  strategyRuns: StrategyRunMeta[];
  selectedRun: string | null;
  selectedStrategy: string | null;
  selectedAsset: string | null;
  onStrategySelect: (run: string | null, strategy: string | null, asset: string | null) => void;

  panes: PaneSpec[];
  hiddenPanes: Set<string>;
  onTogglePane: (id: string) => void;

  onResetDefaults: () => void;
}

export function Sidebar({
  view, onViewChange,
  zones, zone, onZoneChange,
  strategyRuns, selectedRun, selectedStrategy, selectedAsset, onStrategySelect,
  panes, hiddenPanes, onTogglePane,
  onResetDefaults,
}: Props) {
  // Group runs by run_name so the run dropdown is short. For each chosen
  // run, derive the available strategies + assets.
  const runNames = Array.from(new Set(strategyRuns.map((r) => r.run))).sort();
  const strategiesForRun = selectedRun
    ? strategyRuns
        .filter((r) => r.run === selectedRun)
        .reduce<Map<string, StrategyRunMeta>>((acc, r) => {
          // Prefer the meta of the selected asset if available, else first.
          if (!acc.has(r.strategy) || r.asset === selectedAsset) acc.set(r.strategy, r);
          return acc;
        }, new Map())
    : new Map<string, StrategyRunMeta>();
  const assetsForRunStrat = selectedRun && selectedStrategy
    ? Array.from(new Set(
        strategyRuns
          .filter((r) => r.run === selectedRun && r.strategy === selectedStrategy)
          .map((r) => r.asset),
      )).sort()
    : [];
  return (
    <aside className="w-[220px] shrink-0 bg-[#0c0c12] border-r border-[#26262e] flex flex-col h-screen sticky top-0 overflow-y-auto">
      <div className="px-3 py-3 border-b border-[#26262e]">
        <div className="text-[#e6e6e8] font-semibold tracking-wide text-[13px]">
          PJM viz
        </div>
        <div className="text-[#6b6b7a] text-[11px] font-mono mt-0.5">
          market replay
        </div>
      </div>

      <nav className="px-2 py-2 border-b border-[#26262e] flex flex-col gap-1">
        <NavButton active={view === "replay"} onClick={() => onViewChange("replay")}>
          Replay
        </NavButton>
        <NavButton active={view === "overlay"} onClick={() => onViewChange("overlay")}>
          Overlay
        </NavButton>
        <NavButton active={view === "reference"} onClick={() => onViewChange("reference")}>
          Reference
        </NavButton>
      </nav>

      {view === "replay" ? (
        <>
          <div className="px-3 py-3 border-b border-[#26262e]">
            <div className="text-[#6b6b7a] text-[10px] uppercase tracking-wider mb-2">Zone</div>
            <select
              value={zone}
              onChange={(e) => onZoneChange(e.target.value)}
              className="w-full bg-[#0a0a0c] border border-[#26262e] rounded-[4px] px-2 py-1 font-mono text-[13px]"
            >
              {zones.map((z) => (
                <option key={z} value={z}>{z}</option>
              ))}
            </select>
          </div>

          <div className="px-3 py-3 border-b border-[#26262e]">
            <div className="text-[#6b6b7a] text-[10px] uppercase tracking-wider mb-2">
              Strategy <span className="text-[#fbbf24]">(overlay)</span>
            </div>
            <div className="flex flex-col gap-1.5">
              <select
                value={selectedRun ?? ""}
                onChange={(e) => {
                  const run = e.target.value || null;
                  onStrategySelect(run, null, null);
                }}
                className="w-full bg-[#0a0a0c] border border-[#26262e] rounded-[4px] px-2 py-1 font-mono text-[12px]"
                title="Backtest run (eval pipeline output set)"
              >
                <option value="">— no overlay —</option>
                {runNames.map((r) => (
                  <option key={r} value={r}>run: {r}</option>
                ))}
              </select>
              {selectedRun && (
                <select
                  value={selectedStrategy ?? ""}
                  onChange={(e) => {
                    const strat = e.target.value || null;
                    onStrategySelect(selectedRun, strat, null);
                  }}
                  className="w-full bg-[#0a0a0c] border border-[#26262e] rounded-[4px] px-2 py-1 font-mono text-[12px]"
                  title="Strategy run to overlay."
                >
                  <option value="">— pick strategy —</option>
                  {Array.from(strategiesForRun.values())
                    .sort((a, b) => Number(b.is_pf_ceiling) - Number(a.is_pf_ceiling)
                      || a.strategy.localeCompare(b.strategy))
                    .map((meta) => (
                      <option key={meta.strategy} value={meta.strategy}>
                        {meta.strategy}
                      </option>
                    ))}
                </select>
              )}
              {selectedRun && selectedStrategy && (
                <select
                  value={selectedAsset ?? ""}
                  onChange={(e) => {
                    const asset = e.target.value || null;
                    onStrategySelect(selectedRun, selectedStrategy, asset);
                  }}
                  className="w-full bg-[#0a0a0c] border border-[#26262e] rounded-[4px] px-2 py-1 font-mono text-[12px]"
                >
                  <option value="">— pick asset —</option>
                  {assetsForRunStrat.map((a) => (
                    <option key={a} value={a}>{a}</option>
                  ))}
                </select>
              )}
              {selectedStrategy && (
                <div className="text-[#6b6b7a] text-[10px] font-mono leading-tight">
                  Simulated revenue — see the calibration caveats in docs/design.md.
                </div>
              )}
            </div>
          </div>

          <div className="px-3 py-3 border-b border-[#26262e] flex-1">
            <div className="text-[#6b6b7a] text-[10px] uppercase tracking-wider mb-2">Panes</div>
            <ul className="space-y-1">
              {panes.map((p) => {
                const visible = !hiddenPanes.has(p.id);
                return (
                  <li key={p.id}>
                    <label className="flex items-start gap-2 text-[13px] cursor-pointer hover:bg-[#14141a] px-1 py-1 rounded-[3px]">
                      <input
                        type="checkbox"
                        checked={visible}
                        onChange={() => onTogglePane(p.id)}
                        className="mt-1 accent-[#fbbf24]"
                      />
                      <span>
                        <div className={visible ? "text-[#e6e6e8]" : "text-[#6b6b7a] line-through"}>
                          {p.label}
                        </div>
                        <div className="text-[#6b6b7a] text-[11px] font-mono">
                          {p.feeds.join(" · ")}
                        </div>
                      </span>
                    </label>
                  </li>
                );
              })}
            </ul>
          </div>

          <div className="px-3 py-3 border-t border-[#26262e]">
            <button
              onClick={onResetDefaults}
              className="w-full bg-[#0a0a0c] border border-[#26262e] hover:border-[#fbbf24] rounded-[4px] px-2 py-1.5 text-[12px] font-mono"
              title="Reset zone, panes, and decision time to defaults"
            >
              Reset to defaults
            </button>
          </div>
        </>
      ) : view === "overlay" ? (
        <div className="px-3 py-3 text-[#6b6b7a] text-[12px] flex-1">
          Stack N days of one feed on a time-of-day axis. Surface daily
          patterns and isolate anomalies. Pickers for feed / zone / anchor
          day / N days are in the main panel.
        </div>
      ) : (
        <div className="px-3 py-3 text-[#6b6b7a] text-[12px] flex-1">
          Glossary of PJM markets, products, and how this app maps to the
          backtesting engine. Switch to <b className="text-[#e6e6e8]">Replay</b> for the chart.
        </div>
      )}
    </aside>
  );
}

function NavButton({
  active, onClick, children,
}: {
  active: boolean;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      onClick={onClick}
      className={[
        "text-left px-3 py-1.5 rounded-[4px] text-[13px] font-mono",
        active
          ? "bg-[#14141a] text-[#fbbf24] border border-[#26262e]"
          : "text-[#e6e6e8] hover:bg-[#14141a] border border-transparent",
      ].join(" ")}
    >
      {children}
    </button>
  );
}
