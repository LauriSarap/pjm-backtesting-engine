// Reference card. Source: docs/pjm-data.md, docs/example-assets.md.
// Goal: a glanceable card, not a textbook. Update when sources change.

import type { ReactNode } from "react";

const sectionH = "text-[#fbbf24] uppercase tracking-wider text-[11px] mt-6 mb-2 font-semibold";
const tableCls = "w-full text-[12px] font-mono border border-[#26262e] my-2";
const thCls = "text-left text-[#6b6b7a] font-medium px-2 py-1 border-b border-[#26262e] bg-[#14141a]";
const tdCls = "px-2 py-1 border-t border-[#26262e] align-top";
const codeCls = "font-mono text-[#a78bfa] bg-[#14141a] px-1 rounded-[2px]";

function H({ children }: { children: ReactNode }) {
  return <h2 className={sectionH}>{children}</h2>;
}
function C({ children }: { children: ReactNode }) {
  return <code className={codeCls}>{children}</code>;
}

export function Reference() {
  return (
    <div className="px-8 py-6 max-w-[960px] text-[13px] leading-relaxed text-[#e6e6e8]">
      <h1 className="text-[18px] font-semibold mb-1">PJM reference card</h1>
      <p className="text-[#6b6b7a] text-[12px]">
        Markets, products, formulas, and which feed in this app maps to which
        engine loader. Sources: <C>pjm-data.md</C>, <C>example-assets.md</C>,
        Manuals 11/12/13/15/21B/28.
      </p>

      <H>Products</H>
      <table className={tableCls}>
        <thead><tr>
          <th className={thCls}>Product</th>
          <th className={thCls}>Bid</th>
          <th className={thCls}>BESS</th>
        </tr></thead>
        <tbody>
          <tr><td className={tdCls}>Energy (DA + RT)</td><td className={tdCls}>charge / discharge MW; RT settles deviations from DA</td><td className={tdCls}>primary</td></tr>
          <tr><td className={tdCls}>Regulation</td><td className={tdCls}>bidirectional MW around 50% SoC, half-hour assignment</td><td className={tdCls}>best fit</td></tr>
          <tr><td className={tdCls}>Sync Reserve</td><td className={tdCls}>upward MW, 10-min response, 30-min sustain</td><td className={tdCls}>yes — shortfall has retroactive clawback</td></tr>
          <tr><td className={tdCls}>30-min (Secondary)</td><td className={tdCls}>slower upward, mostly clears at $0</td><td className={tdCls}>rarely worth it</td></tr>
          <tr><td className={tdCls}>NSR</td><td className={tdCls}>offline 10-min start</td><td className={tdCls}>excluded (ESR rule)</td></tr>
        </tbody>
      </table>

      <H>Day timeline (EPT)</H>
      <table className={tableCls}>
        <thead><tr>
          <th className={thCls}>When</th>
          <th className={thCls}>What</th>
        </tr></thead>
        <tbody>
          <tr><td className={tdCls}>D-1 11:00</td><td className={tdCls}>DA gate close (energy + DA AS)</td></tr>
          <tr><td className={tdCls}>D-1 13:30</td><td className={tdCls}>DA results post — LMPs, AS clearing prices, awards</td></tr>
          <tr><td className={tdCls}>D-1 14:15</td><td className={tdCls}>daily Reg offer locks</td></tr>
          <tr><td className={tdCls}>D, T-65 min</td><td className={tdCls}>RT offer-revision lock for upcoming hour</td></tr>
          <tr><td className={tdCls}>D, T-35 min</td><td className={tdCls}>Reg MW / availability locks for next interval</td></tr>
          <tr><td className={tdCls}>D, every 5 min</td><td className={tdCls}>RT SCED + LPC — 5-min basepoints, LMPs, RMCP, SRMCP</td></tr>
          <tr><td className={tdCls}>D, HH:00 / HH:30</td><td className={tdCls}>Reg assignment block (post Oct-2025)</td></tr>
          <tr><td className={tdCls}>Tue post-week</td><td className={tdCls}>preliminary settlement</td></tr>
          <tr><td className={tdCls}>~30-60 d</td><td className={tdCls}>verified settlement; perf scores reconciled</td></tr>
        </tbody>
      </table>

      <H>Revenue formulas (per interval)</H>
      <table className={tableCls}>
        <tbody>
          <tr><td className={tdCls}>DA energy</td><td className={tdCls}><C>cleared_MW × DA_LMP × hours</C></td></tr>
          <tr><td className={tdCls}>RT deviation</td><td className={tdCls}><C>(actual − DA_cleared) × RT_LMP × hours</C></td></tr>
          <tr><td className={tdCls}>Reg capability</td><td className={tdCls}><C>MW × score × RMCCP / 12</C></td></tr>
          <tr><td className={tdCls}>Reg mileage</td><td className={tdCls}><C>MW × score × mileage_ratio × RMPCP / 12</C></td></tr>
          <tr><td className={tdCls}>Sync Reserve</td><td className={tdCls}><C>MW × SRMCP × hours</C></td></tr>
          <tr><td className={tdCls}>LOC</td><td className={tdCls}><C>max(0, LMP − cycle_cost) × reserve_MW × hours</C></td></tr>
          <tr><td className={tdCls}>Cycle cost</td><td className={tdCls}><C>$5/MWh × |dispatch| × hours</C> (objective penalty)</td></tr>
        </tbody>
      </table>

      <H>Feeds in this app → engine</H>
      <table className={tableCls}>
        <thead><tr>
          <th className={thCls}>feed_id</th>
          <th className={thCls}>loader</th>
          <th className={thCls}>cadence</th>
          <th className={thCls}>published_at</th>
        </tr></thead>
        <tbody>
          <tr><td className={tdCls}><C>da_lmp</C></td><td className={tdCls}><C>load_da_hrl_lmps</C></td><td className={tdCls}>1h</td><td className={tdCls}>D-1 13:30 EPT</td></tr>
          <tr><td className={tdCls}><C>rt_lmp</C></td><td className={tdCls}><C>load_rt_fivemin_mnt_lmps</C></td><td className={tdCls}>5min</td><td className={tdCls}>MTU_end + 10 min*</td></tr>
          <tr><td className={tdCls}><C>reg_rmccp</C> · <C>reg_rmpcp</C></td><td className={tdCls}><C>load_reg_prices</C></td><td className={tdCls}>5min</td><td className={tdCls}>MTU_end + 10 min</td></tr>
          <tr><td className={tdCls}><C>da_sr_mcp</C></td><td className={tdCls}><C>load_da_sr_prices</C></td><td className={tdCls}>1h</td><td className={tdCls}>D-1 13:30 EPT</td></tr>
          <tr><td className={tdCls}><C>rt_sr_mcp</C></td><td className={tdCls}><C>load_rt_sr_prices</C></td><td className={tdCls}>5min</td><td className={tdCls}>MTU_end + 10 min</td></tr>
          <tr><td className={tdCls}><C>reg_requirement</C> · <C>reg_perfscore</C></td><td className={tdCls}><C>load_reg_market_results</C></td><td className={tdCls}>30min</td><td className={tdCls}><C>MTU_start + 40 min</C> (viz override**)</td></tr>
        </tbody>
      </table>
      <p className="text-[#6b6b7a] text-[11px]">
        * <C>rt_fivemin_mnt_lmps</C> is verified-settlement data used as a
        proxy for the prelim feed.
      </p>
      <p className="text-[#6b6b7a] text-[11px]">
        ** Engine's default for <C>reg_market_results</C> is{" "}
        <C>modified_datetime_utc</C> (settlement-time, ~3 weeks after operating).
        viz_server overrides to assignment-block-end + 10 min so the operational
        moment a trader knew the requirement / clearing MW is what the slider
        sees. <C>rto_perfscore</C> is genuinely settlement-time — the override
        surfaces the ex-post score as if it had been live, which is acceptable
        for replay but should not be confused with a real-time score.
      </p>

      <H>Bitemporal slider semantics</H>
      <ul className="list-disc list-inside text-[12px] space-y-1">
        <li>Slider scrubs <b>decision_time</b> within the displayed window.</li>
        <li>RT LMP visible when <C>MTU_start + 15 min ≤ decision_time</C> → 3-MTU blind window.</li>
        <li>DA LMP visible from <C>D-1 13:30 EPT</C> onward.</li>
      </ul>

      <H>Penalties (settlement)</H>
      <ul className="list-disc list-inside text-[12px] space-y-1">
        <li>Reg 5-min score &lt; 0.25 → forfeit interval credit + LOC.</li>
        <li>Reg rolling score &lt; 0.40 → disqualified until requalified.</li>
        <li>SR shortfall on event → SRMCP × MW <i>plus retroactive clawback</i> ~30 days of prior SR credits.</li>
        <li>30-min reserve shortfall → only that interval's credit lost. No clawback.</li>
      </ul>

      <H>Generator outages</H>
      <p className="text-[12px]">
        Daily forecast of generation MW unavailable on the operating day,
        broken out by why the unit is offline. Source: PJM Data Miner{" "}
        <C>gen_outages_by_type</C>; the <C>PJM RTO</C> region row is the
        system aggregate. Definitions follow Manuals 11/13/21B and the
        GADS reporting framework.
      </p>
      <table className={tableCls}>
        <thead><tr>
          <th className={thCls}>Type</th>
          <th className={thCls}>Meaning</th>
          <th className={thCls}>Lead time</th>
        </tr></thead>
        <tbody>
          <tr>
            <td className={tdCls}><C>forced</C></td>
            <td className={tdCls}>Unscheduled — equipment failure, trip, control or fuel issue. Rolling 3-yr forced-outage rate (FOR) is a primary input to PJM's day-ahead reserve adequacy calc (M-13). Bidders <i>must still submit</i> DA offers even when forced-out (M-11).</td>
            <td className={tdCls}>none / immediate</td>
          </tr>
          <tr>
            <td className={tdCls}><C>planned</C></td>
            <td className={tdCls}>Long-lead scheduled outage approved in advance (turbine overhaul, major inspection). Coordinated through PJM's outage-scheduling process (M-10, ref'd in M-13 §2.3.2).</td>
            <td className={tdCls}>weeks–months</td>
          </tr>
          <tr>
            <td className={tdCls}><C>maintenance</C></td>
            <td className={tdCls}>Short-duration scheduled work (M-11 calls out <i>"&lt; ten (10) days"</i> as the typical bound). PJM may cancel or recall maintenance outages during emergencies to recover capacity (M-13).</td>
            <td className={tdCls}>days</td>
          </tr>
          <tr>
            <td className={tdCls}><C>total</C></td>
            <td className={tdCls}>Sum of the three above. Headline number that flows into reserve-adequacy and emergency-procedure analysis.</td>
            <td className={tdCls}>—</td>
          </tr>
        </tbody>
      </table>
      <p className="text-[#6b6b7a] text-[11px]">
        Outage MW are reported through the eGADS system on a monthly cadence
        (M-21B); failure to submit data produces an <i>assumed forced outage</i>
        for the missing period. Why this matters for replay: the daily
        forecast publishes one row per region and was knowable at its{" "}
        <C>forecast_execution_date_ept</C> — i.e. it's a bitemporal feed,
        and scrubbing decision-time will reveal earlier vs later forecast
        revisions for the same operating day.
      </p>

      <H>Example BESS assets</H>
      <table className={tableCls}>
        <thead><tr>
          <th className={thCls}>Asset</th>
          <th className={thCls}>Zone</th>
          <th className={thCls}>MW / MWh</th>
        </tr></thead>
        <tbody>
          <tr><td className={tdCls}>example_a</td><td className={tdCls}>PJM-RTO</td><td className={tdCls}>250 / 1,000</td></tr>
          <tr><td className={tdCls}>example_b</td><td className={tdCls}>DAY</td><td className={tdCls}>20 / 80</td></tr>
        </tbody>
      </table>
      <p className="text-[#6b6b7a] text-[11px]">
        Fictional round-number configs (docs/example-assets.md). SoC 10-90% · RTE 0.85 · cycle cost $5/MWh · mileage ratio 1.0 (perfect tracking) · NSR excluded.
      </p>

      <p className="text-[#6b6b7a] text-[11px] mt-6">
        See <C>docs/pjm-data.md</C> for the full spec.
      </p>
    </div>
  );
}
