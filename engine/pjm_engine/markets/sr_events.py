"""Synchronized Reserve event delivery + shortfall + clawback.

pjm-data.md §2.2, §7.2 + M28 §6.2.2/§6.3.3 + M11 §4.5.2: when PJM calls an SR
event, every resource holding cleared SR_MW must inject that MW within 10
minutes and hold until the event ends.

Three settlement consequences when an asset can't deliver in full:

1. **Shortfall charge** for the event itself (M28 §6.2.2):
       shortfall_$ = -Σ_{mtu in event} shortfall_MW × RT_SRMCP[mtu] × (1/12)
   Per-5-min-MTU at the **RT** SRMCP at that MTU — NOT a single DA SRMCP ×
   event_hours. PJM forfeits the SR credit on the un-delivered MW for each
   5-min interval the event runs.

2. **Retroactive clawback** of prior SR credits (M28 §6.3.3):
       clawback_$ = -Σ_{mtu in lookback}
                       RetroactivePenaltyMW[mtu] × RT_SRMCP[mtu] × (1/12)
   Where `RetroactivePenaltyMW[mtu]` is the asset's prior cleared SR_RTO MW
   at that MTU. NOT a refund of our own prior-credit revenue rows.
   Lookback is `SR_CLAWBACK_DAYS = 30` days, held fixed. Per pjm-data.md §7.2
   the true window is the rolling average inter-event interval (~18-30 days
   historically); the tariff quotes "approximately 30 days", and M28 specifies
   lesser-of(average interval, days since last failure) — neither refinement
   is modeled.

3. **No clawback for events < 10 min** (M11 §4.5.2): events shorter than
   10 minutes carry NO retroactive obligation. Per-event shortfall can
   still apply.

Failure-mode model:
- **SoC depletion** is the only modeled failure source. At event time, the
  asset must have `SR_MW × event_hours / discharge_eff` MWh available above
  `soc_min`; otherwise it can only deliver the proportion that fits.
- **Power headroom** is presumed enforced upstream — the SoC validator (and
  `validate_stack`) already rejects commitments that exceed the total power
  cap. If the validator passed, headroom existed at commitment time. Real-
  time energy dispatch can chew into headroom but the model treats the
  asset as instantaneously dispatchable up to nameplate.
- **Performance score** isn't applied to SR (no PFP score on capacity
  payments per pjm-data.md §2.2, only Reg has score-based settlement).
"""

from __future__ import annotations

from dataclasses import dataclass

SR_CLAWBACK_DAYS = 30
SR_SHORTFALL_TOLERANCE_MW = 0.0  # any shortfall > 0 triggers clawback; tunable
SR_MIN_EVENT_MIN_FOR_CLAWBACK = 10.0  # M11 §4.5.2: events <10 min skip clawback
ONE_TWELFTH_HOUR = 1.0 / 12.0  # 5-min MTU as a fraction of an hour


@dataclass(frozen=True)
class DeliveryResult:
    sr_committed_mw: float
    delivered_mw: float
    shortfall_mw: float
    available_energy_mwh: float
    required_energy_mwh: float


def simulate_sr_delivery(
    sr_committed_mw: float,
    soc_at_event_mwh: float,
    soc_min_mwh: float,
    event_duration_hours: float,
    discharge_efficiency: float,
) -> DeliveryResult:
    """Pure delivery check at event time.

    Returns how much the asset can actually inject given its SoC. Power
    headroom (asset.power_mw cap) is assumed available — see module docstring
    for the rationale. Discharge efficiency converts MWh of stored
    energy into MWh of delivered injection (e.g., 0.92 round-trip efficient
    → ~0.96 one-way).
    """
    if sr_committed_mw <= 0.0 or event_duration_hours <= 0.0:
        return DeliveryResult(
            sr_committed_mw=sr_committed_mw,
            delivered_mw=0.0,
            shortfall_mw=0.0,
            available_energy_mwh=0.0,
            required_energy_mwh=0.0,
        )
    available_mwh = max(0.0, soc_at_event_mwh - soc_min_mwh)
    required_mwh = sr_committed_mw * event_duration_hours / max(discharge_efficiency, 1e-9)
    if available_mwh >= required_mwh:
        return DeliveryResult(
            sr_committed_mw=sr_committed_mw,
            delivered_mw=sr_committed_mw,
            shortfall_mw=0.0,
            available_energy_mwh=available_mwh,
            required_energy_mwh=required_mwh,
        )
    delivered_mw = (available_mwh * discharge_efficiency) / event_duration_hours
    delivered_mw = min(delivered_mw, sr_committed_mw)
    shortfall_mw = sr_committed_mw - delivered_mw
    return DeliveryResult(
        sr_committed_mw=sr_committed_mw,
        delivered_mw=delivered_mw,
        shortfall_mw=shortfall_mw,
        available_energy_mwh=available_mwh,
        required_energy_mwh=required_mwh,
    )


def settle_sr_shortfall_per_mtu(
    shortfall_mw: float,
    rt_srmcps: list[float],
) -> float:
    """Per-5-min-MTU shortfall charge (M28 §6.2.2).

    `rt_srmcps` is the list of RT SRMCPs at each 5-min MTU within the event
    window. Returns the (negative) total dollar charge:

        -Σ shortfall_MW × rt_srmcp[mtu] × (1/12)

    Caller is responsible for assembling the MTU price list (e.g., iterating
    `[event.timestamp, event.event_end)` and looking up `tables.rt_sr_by_mtu`).
    Missing prices should be filtered upstream — if `rt_srmcps` is empty,
    the charge is 0 (no priced MTU intersects the event).
    """
    if shortfall_mw <= 0.0 or not rt_srmcps:
        return 0.0
    return -1.0 * shortfall_mw * sum(rt_srmcps) * ONE_TWELFTH_HOUR


def settle_sr_clawback_per_mtu(
    prior_mw_and_rt_srmcp: list[tuple[float, float]],
) -> float:
    """Per-5-min-MTU retroactive clawback (M28 §6.3.3).

    `prior_mw_and_rt_srmcp` is `[(prior_cleared_SR_MW, rt_srmcp), ...]` for each
    MTU in the lookback window where the asset held an SR commitment. Returns:

        -Σ prior_MW × rt_srmcp[mtu] × (1/12)

    A MTU with prior_MW=0 contributes nothing; the caller can elide such MTUs
    or include them — both yield identical totals.
    """
    if not prior_mw_and_rt_srmcp:
        return 0.0
    total = sum(mw * srmcp for mw, srmcp in prior_mw_and_rt_srmcp)
    if total <= 0.0:
        return 0.0
    return -1.0 * total * ONE_TWELFTH_HOUR
