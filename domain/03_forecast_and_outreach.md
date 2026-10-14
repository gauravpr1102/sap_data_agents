# Forecast changes and supplier outreach

The forecast is captured as snapshots. The tools compare the two most recent.
Changes are split by whether the date falls within the material's lead time.

| Rank | Meaning |
|---|---|
| 1 | Within lead time, change of {hi_pct}% or more |
| 2 | Within lead time, {lo_pct}% to {hi_pct}% |
| 3 | Beyond lead time, {hi_pct}% or more |
| 4 | Beyond lead time, {lo_pct}% to {hi_pct}% |
| 5 | Within lead time, under {lo_pct}% (report only) |
| 6 | Beyond lead time, under {lo_pct}% (report only) |
| 7 | New demand, no previous quantity (for review) |

Ranks {draftable_ranks} qualify for a supplier e-mail. Each one then goes through
eligibility in a fixed order, and gets exactly one outcome:

- REPORT_ONLY: rank does not qualify
- EXCLUDED_MATERIAL_GROUP: an excluded group such as MRO
- EXCLUDED_VMI: vendor-managed inventory
- AMBIGUOUS_SUPPLIER: more than one possible supplier; a buyer chooses
- NO_SUPPLIER: no open PO and no valid source-list entry
- EXCLUDED_INTERPLANT: supplied by another of the client's own plants
- NO_BUYER_EMAIL: the purchasing group has no buyer mailbox
- ROUTE_TO_SOURCING: the supplier has no e-mail; routed to sourcing, never dropped
- ELIGIBLE: included in one combined draft per supplier and buyer

The supplier is the single open-PO vendor; otherwise the single fixed
source-list vendor; otherwise the single valid source-list vendor. The kit
never guesses between several.
