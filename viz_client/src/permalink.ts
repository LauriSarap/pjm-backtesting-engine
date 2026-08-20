// Pure ViewState ↔ URL codec. Lives outside React so it can be unit-tested
// without a DOM.
//
// URL format:
//   /?zone=PECO&res=auto&from=2025-10-15T04:00Z&to=2025-10-16T04:00Z&decision=2025-10-15T18:00Z
//
// Unknown keys are ignored on parse. Missing keys keep the caller's defaults.
// ~2KB URL budget — we're well under that with the current fields.

export type Resolution = "auto" | "5min" | "15min" | "1h" | "1d";

export interface ViewState {
  zone: string;
  resolution: Resolution;
  fromIso: string;
  toIso: string;
  decisionIso: string;
  // Strategy overlay state (optional — feature is additive, old permalinks
  // still parse). When run/strategy are both absent the strategy panes hide.
  run?: string;
  strategy?: string;
  asset?: string;
}

const KEYS = {
  zone: "zone",
  resolution: "res",
  fromIso: "from",
  toIso: "to",
  decisionIso: "decision",
  run: "run",
  strategy: "strat",
  asset: "asset",
} as const;

const VALID_RES: ReadonlySet<string> = new Set([
  "auto", "5min", "15min", "1h", "1d",
]);

/** Encode current state as a URLSearchParams-ready string. */
export function encodeViewState(state: ViewState): string {
  const p = new URLSearchParams();
  p.set(KEYS.zone, state.zone);
  p.set(KEYS.resolution, state.resolution);
  p.set(KEYS.fromIso, state.fromIso);
  p.set(KEYS.toIso, state.toIso);
  p.set(KEYS.decisionIso, state.decisionIso);
  if (state.run) p.set(KEYS.run, state.run);
  if (state.strategy) p.set(KEYS.strategy, state.strategy);
  if (state.asset) p.set(KEYS.asset, state.asset);
  return p.toString();
}

/** Parse a URLSearchParams-like input into a partial ViewState. Caller merges
 *  with defaults; unknown / malformed values are dropped silently. */
export function decodeViewState(search: string | URLSearchParams): Partial<ViewState> {
  const p = typeof search === "string" ? new URLSearchParams(search) : search;
  const out: Partial<ViewState> = {};
  const zone = p.get(KEYS.zone);
  if (zone) out.zone = zone;
  const res = p.get(KEYS.resolution);
  if (res && VALID_RES.has(res)) out.resolution = res as Resolution;
  for (const [field, key] of [
    ["fromIso", KEYS.fromIso],
    ["toIso", KEYS.toIso],
    ["decisionIso", KEYS.decisionIso],
  ] as const) {
    const v = p.get(key);
    if (v && isValidIso(v)) out[field] = v;
  }
  // Strategy fields are pure strings; backend validates on use.
  for (const [field, key] of [
    ["run", KEYS.run],
    ["strategy", KEYS.strategy],
    ["asset", KEYS.asset],
  ] as const) {
    const v = p.get(key);
    if (v) out[field] = v;
  }
  return out;
}

function isValidIso(s: string): boolean {
  // Strict UTC ISO ending in Z, minute or second precision.
  return /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2})?Z$/.test(s);
}

/** Replace the current browser URL without adding a history entry. Safe to
 *  call on every state change. */
export function writeUrl(state: ViewState): void {
  if (typeof window === "undefined") return;
  const next = `${window.location.pathname}?${encodeViewState(state)}`;
  window.history.replaceState(null, "", next);
}

/** Read the URL once at mount. */
export function readUrl(): Partial<ViewState> {
  if (typeof window === "undefined") return {};
  return decodeViewState(window.location.search.replace(/^\?/, ""));
}
