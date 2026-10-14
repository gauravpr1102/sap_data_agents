# Planning definitions (what the tools mean)

**Scope.** Externally procured materials (MARC-BESKZ = F) with MRP, at the
client's plants, over a {horizon_days}-day horizon.

**Projected balance.** On-hand stock today, plus counted supply, minus
dependent requirements, day by day. Past-due requirements count today.

**Cover rule.** For this client, an open PO line counts as supply only when
the supplier has sent a shipping notification (ASN, EKES-EBTYP = LA). An order
acknowledgement (AB) is not a shipping notice. The result field `cover_rule`
says which rule applied.

**Shortage.** The projected balance goes below zero. `first_short_date` is the
first day it does; `shortfall_qty` is the deepest point; `recovery_date` is when
it comes back to zero or above (empty if not within the horizon).

**Risk score.** The value at risk, ranked within the plant (0-1), times
criticality (1 = feeds one assembly, 2 = feeds 2-5, 3 = feeds 6 or more), times
urgency (3 = short within 7 days, 2 = within 21 days, 1 = later).
`risk_rank` 1 is the most urgent at that plant.

**Value at risk.** Shortfall times standard price per unit (standard price
divided by the price unit).

**Missing shipping notice.** The PO line's confirmation control requires an
ASN, the ship-by date (delivery date minus transit time) has passed, and no ASN
exists. Meaning: "should have shipped and hasn't".

**Safety-stock dip.** The balance falls below safety stock but never below zero.
