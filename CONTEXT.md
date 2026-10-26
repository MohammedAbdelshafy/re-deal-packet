---
type: repository
status: active
owner_repo: MohammedAbdelshafy/re-deal-packet
product_name: RE Deal Packet
source_of_truth: https://github.com/MohammedAbdelshafy/re-deal-packet
---

# RE Deal Packet CONTEXT

One job: turn property and comparable-sale CSVs into a traceable underwriting worksheet with visible assumptions and risk flags.

## Inputs
- Formulas, formats and limits: [README](README.md)
- Implementation: [re_deal_packet.py](re_deal_packet.py)
- Samples: [samples](samples/)
- Tests: [tests](tests/)

## Process
1. Read the subject property and comparable-sale files.
2. Calculate the supported comp median, max-offer screening value and price-per-square-foot comparisons.
3. Add flags for missing fields, stale comps, small sample sizes and wide spreads.
4. Write a Markdown packet and a machine-readable JSON file.
5. Keep every calculated number traceable to the input rows.

## Output
A deal brief with formulas, risk flags, sources supplied by the user and assumptions.

## Check
Run the documented tests with synthetic samples. Clearly mark the 70% rule as a screening rule of thumb, not financial advice. Never describe synthetic sample records as real property data.
