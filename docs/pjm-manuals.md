# PJM manuals

The engine's market mechanics and settlement formulas were built against
the PJM manuals below. Section references throughout this repo (such as
"M28 §6.2.2") point into these documents.

The manuals are public documents on pjm.com and are not redistributed in
this repository. Download them from the URLs below. The full manual index
is at <https://www.pjm.com/library/manuals>. The revision numbers listed
are the versions the engine was developed against (mid-2026). pjm.com
always serves the current revision, and section numbers can shift between
revisions.

| Manual | Official title | Developed against | What the engine uses it for | URL |
|---|---|---|---|---|
| M-11 | Energy & Ancillary Services Market Operations | Rev. 136 (eff. 2025-10-01) | DA/RT energy and AS market clearing rules: gate closures (DA 11:00, T-65, T-35), offer rules, Reg redesign mechanics, SR demand curve, price posting cadence | <https://www.pjm.com/-/media/DotCom/documents/manuals/m11.ashx> |
| M-12 | Balancing Operations | Rev. 57 (eff. 2026-04-22) | Regulation deployment and performance scoring, disqualification thresholds, reserve deployment/event operations | <https://www.pjm.com/-/media/DotCom/documents/manuals/m12.ashx> |
| M-13 | Emergency Operations | Rev. 97 (eff. 2025-11-20) | Emergency procedures context for reserve events and outage recalls | <https://www.pjm.com/-/media/DotCom/documents/manuals/m13.ashx> |
| M-15 | Cost Development Guidelines | Rev. 47 (eff. 2025-10-01) | Cost-based offer components (context for the cycle-cost treatment in bids) | <https://www.pjm.com/-/media/DotCom/documents/manuals/m15.ashx> |
| M-21B | PJM Rules and Procedures for Determination of Generating Capability | Rev. 05 (eff. 2026-01-22) | ESR/storage capacity ratings (ELCC class, UCAP), eGADS outage reporting cadence | <https://www.pjm.com/-/media/DotCom/documents/manuals/m21b.ashx> |
| M-27 | Open Access Transmission Tariff Accounting | Rev. 103 (eff. 2025-07-23) | Tariff-side accounting context for transmission-related charges | <https://www.pjm.com/-/media/DotCom/documents/manuals/m27.ashx> |
| M-28 | Operating Agreement Accounting | Rev. 104 (eff. 2026-03-01) | Settlement formulas: energy two-settlement, Reg PFP credit and mileage ratio (§4.2.1), SR/Sec two-settlement (§6.2.2, §19.2.2), LOC, clawbacks, reconciliation windows (§19.4) | <https://www.pjm.com/-/media/DotCom/documents/manuals/m28.ashx> |

The shorter form `https://www.pjm.com/-/media/documents/manuals/mXX.ashx`
also works. pjm.com permanently redirects it to the `DotCom` path above.

Two manuals are deliberately not used (noted in the `design.md` calibration
caveats). M-18 (Capacity Market) would be needed to simulate Performance
Assessment Hours, which are not modeled. M-29 (Billing) is cited in
`pjm-data.md` §6 for the weekly statement cadence, but the engine encodes
only the cadence, not M-29 formulas.
