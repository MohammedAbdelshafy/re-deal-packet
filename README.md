# re-deal-packet

A small Python CLI (standard library only) that assembles a review-ready
real-estate deal packet from two CSV inputs: a subject property and a set of
comparable sales.

It computes an underwriting worksheet — ARV from comp medians, a 70%-rule MAO,
offer guidance, and price-per-square-foot comparisons — plus a set of
rule-based risk flags, and writes them as a Markdown packet and a
machine-readable JSON file. Every number in the packet is traceable to the
input rows.

## What it does

- Reads `property.csv` (one row: address, city, state, zip, asking_price,
  beds, baths, sqft, lot_sqft, year_built, rehab_estimate, notes).
- Reads `comps.csv` (one row per comparable sale: address, sold_price,
  sold_date, beds, baths, sqft, distance_miles, notes).
- Computes:
  - **ARV** = median of comp sold prices (mean and comp count shown too;
    fewer than 3 comps is flagged).
  - **MAO (70% rule)** = 0.70 × ARV − rehab_estimate. The 70% rule is a
    screening rule of thumb, not investment advice.
  - **Offer guidance**: max offer (MAO), spread vs asking
    (asking_price − MAO), and projected profit at MAO
    (ARV − MAO − rehab_estimate).
  - **Price/sqft**: subject (at asking) vs comp median.
- Applies rule-based **risk flags**, each with the data behind it:
  - fewer than 3 comps
  - comp price spread (max − min) / median above 25%
  - any comp older than 180 days
  - rehab estimate missing or zero
  - subject price/sqft more than 15% above comp median
  - missing critical property fields
- Writes `packet/deal_packet.md` (property summary, comps table,
  underwriting worksheet with formulas shown, risk flags with evidence,
  assumptions) and `packet/underwriting.json` (all numbers, machine-readable).

## Install

No dependencies beyond Python 3.8+.

```sh
git clone https://github.com/MohammedAbdelshafy/re-deal-packet.git
cd re-deal-packet
```

## Usage

```sh
python3 re_deal_packet.py --property samples/property.csv \
    --comps samples/comps.csv --out ./packet/
```

Output:

```
Wrote ./packet/deal_packet.md
Wrote ./packet/underwriting.json
ARV: $253,000 | MAO: $132,100 | Risk flags: 0
```

The files in `samples/` are **synthetic samples, not real listings** —
made-up numbers for testing and demo purposes only.

## Formulas

- `ARV = median(comp sold_price)`
- `MAO = 0.70 × ARV − rehab_estimate` — the 70% rule, stated here as a
  common screening rule of thumb, not as financial advice.
- `spread_vs_asking = asking_price − MAO`
- `projected_profit_at_mao = ARV − MAO − rehab_estimate`
- `price_per_sqft = price / sqft`

## Inputs / outputs

Inputs: the two CSVs described above. Lines starting with `#` are treated as
comments and skipped. Dates are accepted as `YYYY-MM-DD` (or `MM/DD/YYYY`).

Outputs: `deal_packet.md` and `underwriting.json` in the directory given by
`--out`. Exit code 0 on success, 2 on a fatal input problem (missing file,
missing required column, non-numeric value) with a message on stderr.

## Limits

- **Not financial advice.** This tool performs arithmetic on numbers you
  supply; it does not evaluate deals for you.
- **Comps quality determines output quality.** The tool does not verify that
  your comps are actually comparable, arm's-length, or current — it only
  flags wide spreads and stale dates.
- **No MLS integration.** You enter comps by hand.
- Holding costs, closing costs, financing, and taxes are not modeled.

## License

MIT — see [LICENSE](LICENSE).
