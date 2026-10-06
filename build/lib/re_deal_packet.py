#!/usr/bin/env python3
"""re-deal-packet: assemble a review-ready real-estate deal packet from two CSVs.

Inputs:
  --property  CSV with one row:
      address, city, state, zip, asking_price, beds, baths,
      sqft, lot_sqft, year_built, rehab_estimate, notes
  --comps     CSV with one row per comparable sale:
      address, sold_price, sold_date, beds, baths, sqft,
      distance_miles, notes

Outputs (into --out directory):
  deal_packet.md   human-readable packet
  underwriting.json  all computed numbers, machine-readable

Python standard library only.
"""

import argparse
import csv
import json
import os
import sys
from datetime import date

VERSION = "1.0.0"

# Columns that must exist in the header of each input file.
REQUIRED_PROPERTY_COLUMNS = [
    "address", "city", "state", "zip", "asking_price", "beds", "baths",
    "sqft", "lot_sqft", "year_built", "rehab_estimate", "notes",
]
REQUIRED_COMPS_COLUMNS = [
    "address", "sold_price", "sold_date", "beds", "baths", "sqft",
    "distance_miles", "notes",
]

# Property fields whose values must be non-empty; empties become a risk flag.
CRITICAL_PROPERTY_FIELDS = [
    "address", "city", "state", "zip", "asking_price", "beds", "baths", "sqft",
]

STALE_COMP_DAYS = 180
COMP_SPREAD_THRESHOLD = 0.25   # (max - min) / median
PSF_PREMIUM_THRESHOLD = 0.15   # subject $/sqft above comp median $/sqft
MAO_FACTOR = 0.70              # the 70% rule (rule of thumb, not advice)


class PacketError(Exception):
    """Fatal, user-fixable input problem (exit code 2)."""


def parse_number(raw):
    """Parse a money/count field. Returns float or None when blank.

    Raises PacketError on a non-blank value that is not numeric.
    """
    if raw is None:
        return None
    text = str(raw).strip()
    if text == "":
        return None
    cleaned = text.replace("$", "").replace(",", "").strip()
    try:
        return float(cleaned)
    except ValueError:
        raise PacketError("Not a number: %r" % text)


def parse_date(raw):
    """Parse an ISO or US date. Returns a datetime.date or None when blank."""
    if raw is None:
        return None
    text = str(raw).strip()
    if text == "":
        return None
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m-%d-%Y"):
        try:
            from datetime import datetime
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    raise PacketError("Unrecognized date (use YYYY-MM-DD): %r" % text)


def read_rows(path, required_columns):
    """Read a CSV, skipping '#' comment lines. Returns (header, list of dicts)."""
    if not os.path.exists(path):
        raise PacketError("Input file not found: %s" % path)
    with open(path, newline="", encoding="utf-8-sig") as fh:
        lines = [ln for ln in fh if not ln.lstrip().startswith("#")]
    if not lines:
        raise PacketError("Input file is empty: %s" % path)
    reader = csv.DictReader(lines)
    header = reader.fieldnames or []
    missing = [c for c in required_columns if c not in header]
    if missing:
        raise PacketError(
            "Missing required column(s) %s in %s" % (", ".join(missing), path)
        )
    return header, [dict(r) for r in reader]


def median(values):
    vals = sorted(values)
    n = len(vals)
    if n == 0:
        raise ValueError("median of empty sequence")
    mid = n // 2
    if n % 2 == 1:
        return vals[mid]
    return (vals[mid - 1] + vals[mid]) / 2.0


def mean(values):
    vals = list(values)
    if not vals:
        raise ValueError("mean of empty sequence")
    return sum(vals) / len(vals)


def fmt_money(value):
    if value is None:
        return "n/a"
    return "$%s" % f"{value:,.0f}"


def fmt_num(value, places=2):
    if value is None:
        return "n/a"
    return f"{value:,.{places}f}"


def fmt_date(value):
    if value is None:
        return "n/a"
    return value.isoformat()


def load_property(path):
    _, rows = read_rows(path, REQUIRED_PROPERTY_COLUMNS)
    if not rows:
        raise PacketError("property.csv must contain exactly one data row; found none")
    row = rows[0]
    prop = {
        "address": (row.get("address") or "").strip(),
        "city": (row.get("city") or "").strip(),
        "state": (row.get("state") or "").strip(),
        "zip": (row.get("zip") or "").strip(),
        "asking_price": parse_number(row.get("asking_price")),
        "beds": parse_number(row.get("beds")),
        "baths": parse_number(row.get("baths")),
        "sqft": parse_number(row.get("sqft")),
        "lot_sqft": parse_number(row.get("lot_sqft")),
        "year_built": parse_number(row.get("year_built")),
        "rehab_estimate": parse_number(row.get("rehab_estimate")),
        "notes": (row.get("notes") or "").strip(),
    }
    return prop


def load_comps(path):
    _, rows = read_rows(path, REQUIRED_COMPS_COLUMNS)
    comps = []
    for i, row in enumerate(rows, start=1):
        comps.append({
            "index": i,
            "address": (row.get("address") or "").strip(),
            "sold_price": parse_number(row.get("sold_price")),
            "sold_date": parse_date(row.get("sold_date")),
            "beds": parse_number(row.get("beds")),
            "baths": parse_number(row.get("baths")),
            "sqft": parse_number(row.get("sqft")),
            "distance_miles": parse_number(row.get("distance_miles")),
            "notes": (row.get("notes") or "").strip(),
        })
    return comps


def build_worksheet(prop, comps, today=None):
    """Compute ARV/MAO worksheet and risk flags. Returns a plain dict."""
    today = today or date.today()
    flags = []

    priced = [c for c in comps if c["sold_price"] is not None]
    comp_prices = [c["sold_price"] for c in priced]
    count = len(priced)

    if count == 0:
        arv_median = arv_mean = None
    else:
        arv_median = median(comp_prices)
        arv_mean = mean(comp_prices)

    if count < 3:
        flags.append({
            "code": "FEW_COMPS",
            "message": "Fewer than 3 usable comps (%d). ARV is thin and unreliable." % count,
            "evidence": {"usable_comp_count": count},
        })

    if count >= 2 and arv_median:
        spread = (max(comp_prices) - min(comp_prices)) / arv_median
        if spread > COMP_SPREAD_THRESHOLD:
            flags.append({
                "code": "COMP_SPREAD_WIDE",
                "message": "Comp price spread (max-min)/median is %.1f%%, above the %.0f%% threshold." % (
                    spread * 100, COMP_SPREAD_THRESHOLD * 100),
                "evidence": {
                    "spread_ratio": round(spread, 4),
                    "min_sold_price": min(comp_prices),
                    "max_sold_price": max(comp_prices),
                    "median_sold_price": arv_median,
                },
            })

    stale = []
    for c in priced:
        if c["sold_date"] is not None:
            age_days = (today - c["sold_date"]).days
            if age_days > STALE_COMP_DAYS:
                stale.append({"address": c["address"], "sold_date": c["sold_date"].isoformat(),
                              "age_days": age_days})
    if stale:
        flags.append({
            "code": "STALE_COMPS",
            "message": "%d comp(s) sold more than %d days ago." % (len(stale), STALE_COMP_DAYS),
            "evidence": {"stale_comps": stale},
        })

    rehab = prop["rehab_estimate"]
    if rehab is None or rehab <= 0:
        flags.append({
            "code": "REHAB_MISSING",
            "message": "Rehab estimate is missing or zero; MAO cannot reflect repair costs.",
            "evidence": {"rehab_estimate": rehab},
        })

    asking = prop["asking_price"]
    sqft = prop["sqft"]
    subject_psf = None
    if asking is not None and sqft:
        subject_psf = asking / sqft

    comp_psf = []
    for c in priced:
        if c["sqft"]:
            comp_psf.append(c["sold_price"] / c["sqft"])
    comp_psf_median = median(comp_psf) if comp_psf else None

    if subject_psf is not None and comp_psf_median:
        premium = (subject_psf - comp_psf_median) / comp_psf_median
        if premium > PSF_PREMIUM_THRESHOLD:
            flags.append({
                "code": "SUBJECT_PSF_HIGH",
                "message": "Subject price/sqft (%s) is %.1f%% above comp median (%s), exceeding the %.0f%% threshold." % (
                    fmt_money(subject_psf), premium * 100,
                    fmt_money(comp_psf_median), PSF_PREMIUM_THRESHOLD * 100),
                "evidence": {
                    "subject_price_per_sqft": round(subject_psf, 2),
                    "comp_median_price_per_sqft": round(comp_psf_median, 2),
                    "premium_ratio": round(premium, 4),
                },
            })

    missing = [f for f in CRITICAL_PROPERTY_FIELDS
               if prop[f] is None or (isinstance(prop[f], str) and prop[f] == "")]
    if missing:
        flags.append({
            "code": "MISSING_FIELDS",
            "message": "Property is missing critical field(s): %s." % ", ".join(missing),
            "evidence": {"missing_fields": missing},
        })

    mao = None
    if arv_median is not None and rehab is not None:
        mao = MAO_FACTOR * arv_median - rehab

    spread_vs_asking = None
    if asking is not None and mao is not None:
        spread_vs_asking = asking - mao

    projected_profit = None
    if arv_median is not None and mao is not None and rehab is not None:
        projected_profit = arv_median - mao - rehab

    worksheet = {
        "generated": today.isoformat(),
        "property": prop,
        "comps": priced,
        "arv": {
            "median": arv_median,
            "mean": arv_mean,
            "count": count,
            "method": "median of comp sold_price",
        },
        "mao": {
            "factor": MAO_FACTOR,
            "value": mao,
            "formula": "0.70 x ARV - rehab_estimate (70% rule of thumb)",
            "rehab_estimate": rehab,
        },
        "asking_price": asking,
        "max_offer": mao,
        "spread_vs_asking": spread_vs_asking,
        "spread_vs_asking_formula": "asking_price - MAO",
        "projected_profit_at_mao": projected_profit,
        "projected_profit_formula": "ARV - MAO - rehab_estimate",
        "subject_price_per_sqft": subject_psf,
        "comp_median_price_per_sqft": comp_psf_median,
        "risk_flags": flags,
        "assumptions": [
            "ARV is the median of the sold prices of the %d usable comp(s) supplied." % count,
            "MAO uses the 70% rule of thumb: 0.70 x ARV - rehab_estimate. "
            "It is a screening shortcut, not investment advice.",
            "Comps are assumed arm's-length, comparable-condition sales unless notes say otherwise.",
            "Comp distance, condition, and age adjustments are NOT applied; verify manually.",
            "Projected profit at MAO ignores holding costs, closing costs, financing, and taxes.",
            "Days-old for each comp is measured from the packet generation date.",
        ],
    }
    return worksheet


def render_markdown(ws):
    prop = ws["property"]
    lines = []
    lines.append("# Deal Packet")
    lines.append("")
    lines.append("_Generated %s. Screening worksheet only -- not financial advice._" % ws["generated"])
    lines.append("")

    lines.append("## Property Summary")
    lines.append("")
    loc = ", ".join(x for x in [prop["address"], prop["city"], prop["state"], prop["zip"]] if x)
    lines.append("- **Location:** %s" % (loc or "n/a"))
    lines.append("- **Asking price:** %s" % fmt_money(ws["asking_price"]))
    beds = fmt_num(prop["beds"], 0) if prop["beds"] is not None else "n/a"
    baths = fmt_num(prop["baths"], 0) if prop["baths"] is not None else "n/a"
    lines.append("- **Beds / Baths:** %s / %s" % (beds, baths))
    lines.append("- **Living area:** %s sqft%s" % (
        fmt_num(prop["sqft"], 0),
        (" (lot %s sqft)" % fmt_num(prop["lot_sqft"], 0)) if prop["lot_sqft"] else ""))
    lines.append("- **Year built:** %s" % (
        ("%d" % prop["year_built"]) if prop["year_built"] else "n/a"))
    lines.append("- **Rehab estimate:** %s" % fmt_money(prop["rehab_estimate"]))
    if prop["notes"]:
        lines.append("- **Notes:** %s" % prop["notes"])
    lines.append("")

    lines.append("## Comparable Sales")
    lines.append("")
    if not ws["comps"]:
        lines.append("No usable comps supplied.")
    else:
        lines.append("| # | Address | Sold price | Sold date | Beds | Baths | Sqft | Dist (mi) | Notes |")
        lines.append("|---|---------|------------|-----------|------|-------|------|-----------|-------|")
        for c in ws["comps"]:
            lines.append("| %d | %s | %s | %s | %s | %s | %s | %s | %s |" % (
                c["index"], c["address"], fmt_money(c["sold_price"]), fmt_date(c["sold_date"]),
                fmt_num(c["beds"], 0) if c["beds"] is not None else "n/a",
                fmt_num(c["baths"], 0) if c["baths"] is not None else "n/a",
                fmt_num(c["sqft"], 0) if c["sqft"] else "n/a",
                fmt_num(c["distance_miles"]) if c["distance_miles"] is not None else "n/a",
                c["notes"]))
    lines.append("")

    lines.append("## Underwriting Worksheet")
    lines.append("")
    lines.append("Formula: **ARV = median(comp sold_price)**")
    lines.append("")
    lines.append("- ARV (median): %s" % fmt_money(ws["arv"]["median"]))
    lines.append("- Comp mean (for reference): %s" % fmt_money(ws["arv"]["mean"]))
    lines.append("- Comp count: %d" % ws["arv"]["count"])
    lines.append("")
    lines.append("Formula: **MAO = 0.70 x ARV - rehab_estimate** (the 70% rule, a rule of thumb)")
    lines.append("")
    lines.append("- MAO (max offer): %s" % fmt_money(ws["max_offer"]))
    lines.append("- Rehab estimate used: %s" % fmt_money(ws["mao"]["rehab_estimate"]))
    lines.append("")
    lines.append("Formula: **Spread vs asking = asking_price - MAO**")
    lines.append("")
    lines.append("- Spread vs asking: %s" % (
        fmt_money(ws["spread_vs_asking"]) if ws["spread_vs_asking"] is not None else "n/a"))
    lines.append("")
    lines.append("Formula: **Projected profit at MAO = ARV - MAO - rehab_estimate**")
    lines.append("")
    lines.append("- Projected profit at MAO: %s" % (
        fmt_money(ws["projected_profit_at_mao"]) if ws["projected_profit_at_mao"] is not None else "n/a"))
    lines.append("")
    lines.append("Formula: **price/sqft = price / sqft**")
    lines.append("")
    lines.append("- Subject (at asking): %s/sqft" % (
        fmt_money(ws["subject_price_per_sqft"]) if ws["subject_price_per_sqft"] else "n/a"))
    lines.append("- Comps median: %s/sqft" % (
        fmt_money(ws["comp_median_price_per_sqft"]) if ws["comp_median_price_per_sqft"] else "n/a"))
    lines.append("")

    lines.append("## Risk Flags")
    lines.append("")
    if not ws["risk_flags"]:
        lines.append("No risk flags fired.")
    else:
        for f in ws["risk_flags"]:
            lines.append("### %s" % f["code"])
            lines.append("")
            lines.append(f["message"])
            lines.append("")
            lines.append("Evidence:")
            lines.append("")
            lines.append("```json")
            lines.append(json.dumps(f["evidence"], indent=2, default=str))
            lines.append("```")
            lines.append("")
    lines.append("")
    lines.append("## Assumptions")
    lines.append("")
    for a in ws["assumptions"]:
        lines.append("- %s" % a)
    lines.append("")
    return "\n".join(lines)


def write_packet(ws, out_dir):
    os.makedirs(out_dir, exist_ok=True)
    md_path = os.path.join(out_dir, "deal_packet.md")
    json_path = os.path.join(out_dir, "underwriting.json")
    with open(md_path, "w", encoding="utf-8") as fh:
        fh.write(render_markdown(ws))
    with open(json_path, "w", encoding="utf-8") as fh:
        json.dump(ws, fh, indent=2, default=str)
    return md_path, json_path


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        prog="re-deal-packet",
        description="Assemble a review-ready deal packet from property + comps CSVs.")
    p.add_argument("--property", required=True, help="Path to property.csv (one row)")
    p.add_argument("--comps", required=True, help="Path to comps.csv (one row per comp)")
    p.add_argument("--out", required=True, help="Output directory for the packet")
    p.add_argument("--version", action="version", version="re-deal-packet %s" % VERSION)
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    try:
        prop = load_property(args.property)
        comps = load_comps(args.comps)
    except PacketError as exc:
        print("Error: %s" % exc, file=sys.stderr)
        return 2
    ws = build_worksheet(prop, comps)
    md_path, json_path = write_packet(ws, args.out)
    print("Wrote %s" % md_path)
    print("Wrote %s" % json_path)
    print("ARV: %s | MAO: %s | Risk flags: %d" % (
        fmt_money(ws["arv"]["median"]), fmt_money(ws["max_offer"]), len(ws["risk_flags"])))
    return 0


if __name__ == "__main__":
    sys.exit(main())
