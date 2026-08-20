// Keyboard shortcuts for scrubbing the decision-time cursor.
//
//   ,    step decision_time back  5 min  (1 MTU)
//   .    step decision_time fwd   5 min
//   [    step decision_time back  1 hour
//   ]    step decision_time fwd   1 hour
//   R    reset decision_time to target_to ("now")
//
// We bypass when the user is typing in an input/select/textarea — otherwise
// editing date inputs would scrub the cursor.

import { useEffect } from "react";

export interface KeyboardScrubOptions {
  decisionIso: string;
  /** Where R sends the cursor — typically "now" or target_to. */
  resetIso: string;
  setDecisionIso: (iso: string) => void;
  /** Called when T is pressed. Receives the current cursor time (Unix
   *  seconds) so the parent can pin it. Pressing T again with a pin set
   *  should clear it (the parent decides — null cursorTime + a pin → clear). */
  onTogglePin?: () => void;
}

const STEP_5MIN_MS = 5 * 60 * 1000;
const STEP_1H_MS = 60 * 60 * 1000;

function shouldIgnore(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false;
  const tag = target.tagName;
  if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return true;
  if (target.isContentEditable) return true;
  return false;
}

function shift(iso: string, deltaMs: number): string {
  const next = new Date(new Date(iso).getTime() + deltaMs);
  return next.toISOString().replace(/\.\d+Z$/, "Z");
}

export function useKeyboardScrub({
  decisionIso, resetIso, setDecisionIso, onTogglePin,
}: KeyboardScrubOptions): void {
  useEffect(() => {
    function onKeyDown(e: KeyboardEvent) {
      if (e.metaKey || e.ctrlKey || e.altKey) return;
      if (shouldIgnore(e.target)) return;

      switch (e.key) {
        case ",":
          setDecisionIso(shift(decisionIso, -STEP_5MIN_MS));
          e.preventDefault();
          break;
        case ".":
          setDecisionIso(shift(decisionIso, +STEP_5MIN_MS));
          e.preventDefault();
          break;
        case "[":
          setDecisionIso(shift(decisionIso, -STEP_1H_MS));
          e.preventDefault();
          break;
        case "]":
          setDecisionIso(shift(decisionIso, +STEP_1H_MS));
          e.preventDefault();
          break;
        case "r":
        case "R":
          setDecisionIso(resetIso);
          e.preventDefault();
          break;
        case "t":
        case "T":
          if (onTogglePin) {
            onTogglePin();
            e.preventDefault();
          }
          break;
      }
    }
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [decisionIso, resetIso, setDecisionIso, onTogglePin]);
}
