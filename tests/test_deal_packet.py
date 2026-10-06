"""Tests for re_deal_packet. Run: python3 -m unittest discover -s tests -v"""
import csv
import json
import os
import sys
import tempfile
import unittest
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import re_deal_packet as rdp

PROP_HEADER = ["address", "city", "state", "zip", "asking_price", "beds", "baths",
               "sqft", "lot_sqft", "year_built", "rehab_estimate", "notes"]
COMPS_HEADER = ["address", "sold_price", "sold_date", "beds", "baths", "sqft",
                "distance_miles", "notes"]


def write_csv(path, header, rows):
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(header)
        w.writerows(rows)


def prop_row(**kw):
    base = {"address": "1 Test Way", "city": "Testville", "state": "TX", "zip": "75001",
            "asking_price": "200000", "beds": "3", "baths": "2", "sqft": "1600",
            "lot_sqft": "7000", "year_built": "1985", "rehab_estimate": "30000",
            "notes": "sample"}
    base.update(kw)
    return [base[c] for c in PROP_HEADER]


def comp_row(price, days_ago=30, sqft=1600, **kw):
    sold = (date.today() - timedelta(days=days_ago)).isoformat()
    base = {"address": "c", "sold_price": str(price), "sold_date": sold, "beds": "3",
            "baths": "2", "sqft": str(sqft), "distance_miles": "0.5", "notes": "sample"}
    base.update(kw)
    return [base[c] for c in COMPS_HEADER]


class Case(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.prop = os.path.join(self.tmp.name, "property.csv")
        self.comps = os.path.join(self.tmp.name, "comps.csv")
        self.out = os.path.join(self.tmp.name, "packet")

    def tearDown(self):
        self.tmp.cleanup()

    def run_cli(self, prop_rows, comp_rows):
        write_csv(self.prop, PROP_HEADER, prop_rows)
        write_csv(self.comps, COMPS_HEADER, comp_rows)
        rc = rdp.main(["--property", self.prop, "--comps", self.comps, "--out", self.out])
        self.assertEqual(rc, 0)
        with open(os.path.join(self.out, "underwriting.json"), encoding="utf-8") as fh:
            ws = json.load(fh)
        with open(os.path.join(self.out, "deal_packet.md"), encoding="utf-8") as fh:
            md = fh.read()
        return ws, md

    def flag_codes(self, ws):
        return {f["code"] for f in ws["risk_flags"]}


class TestMath(Case):
    def test_arv_median_odd(self):
        ws, _ = self.run_cli([prop_row()],
                             [comp_row(100000), comp_row(200000), comp_row(300000)])
        self.assertEqual(ws["arv"]["median"], 200000)
        self.assertAlmostEqual(ws["arv"]["mean"], 200000)

    def test_arv_median_even(self):
        ws, _ = self.run_cli([prop_row()],
                             [comp_row(100000), comp_row(200000), comp_row(300000), comp_row(400000)])
        self.assertEqual(ws["arv"]["median"], 250000)

    def test_mao_70pct_rule(self):
        # ARV median = 200000, rehab = 30000 -> MAO = 0.70*200000 - 30000 = 110000
        ws, _ = self.run_cli([prop_row(rehab_estimate="30000")],
                             [comp_row(100000), comp_row(200000), comp_row(300000)])
        self.assertEqual(ws["max_offer"], 110000)
        # spread vs asking = 200000 - 110000 = 90000
        self.assertEqual(ws["spread_vs_asking"], 90000)
        # projected profit = 200000 - 110000 - 30000 = 60000
        self.assertEqual(ws["projected_profit_at_mao"], 60000)

    def test_median_helper_even_odd(self):
        self.assertEqual(rdp.median([5]), 5)
        self.assertEqual(rdp.median([1, 3]), 2.0)
        self.assertEqual(rdp.median([3, 1, 2]), 2)


class TestFlags(Case):
    def test_few_comps_fires_at_two(self):
        ws, _ = self.run_cli([prop_row()], [comp_row(200000), comp_row(210000)])
        self.assertIn("FEW_COMPS", self.flag_codes(ws))

    def test_few_comps_not_at_three(self):
        ws, _ = self.run_cli([prop_row()],
                             [comp_row(200000), comp_row(210000), comp_row(220000)])
        self.assertNotIn("FEW_COMPS", self.flag_codes(ws))

    def test_spread_flag_fires_above_25(self):
        # prices 100k..150k, median 125k -> spread 50/125 = 40%
        ws, _ = self.run_cli([prop_row()],
                             [comp_row(100000), comp_row(125000), comp_row(150000)])
        self.assertIn("COMP_SPREAD_WIDE", self.flag_codes(ws))

    def test_spread_flag_boundary_exact_25(self):
        # Exact 25%: median 120k, range 30k -> (135000-105000)/120000 = 0.25 -> no flag.
        # Comp sqft chosen so $/sqft matches the subject (125) and PSF flag stays off.
        ws, _ = self.run_cli([prop_row()],
                             [comp_row(105000, sqft=840),
                              comp_row(120000, sqft=960),
                              comp_row(135000, sqft=1080)])
        self.assertNotIn("COMP_SPREAD_WIDE", self.flag_codes(ws))

    def test_stale_comp_fires(self):
        ws, _ = self.run_cli([prop_row()],
                             [comp_row(200000, days_ago=200),
                              comp_row(210000), comp_row(220000)])
        self.assertIn("STALE_COMPS", self.flag_codes(ws))
        ev = [f["evidence"] for f in ws["risk_flags"] if f["code"] == "STALE_COMPS"][0]
        self.assertEqual(ev["stale_comps"][0]["age_days"], 200)

    def test_stale_comp_boundary_180_days(self):
        ws, _ = self.run_cli([prop_row()],
                             [comp_row(200000, days_ago=180),
                              comp_row(210000), comp_row(220000)])
        self.assertNotIn("STALE_COMPS", self.flag_codes(ws))

    def test_rehab_missing_fires(self):
        ws, _ = self.run_cli([prop_row(rehab_estimate="")],
                             [comp_row(200000), comp_row(210000), comp_row(220000)])
        self.assertIn("REHAB_MISSING", self.flag_codes(ws))
        self.assertIsNone(ws["max_offer"])

    def test_rehab_zero_fires(self):
        ws, _ = self.run_cli([prop_row(rehab_estimate="0")],
                             [comp_row(200000), comp_row(210000), comp_row(220000)])
        self.assertIn("REHAB_MISSING", self.flag_codes(ws))

    def test_rehab_positive_no_flag(self):
        ws, _ = self.run_cli([prop_row(rehab_estimate="1")],
                             [comp_row(200000), comp_row(210000), comp_row(220000)])
        self.assertNotIn("REHAB_MISSING", self.flag_codes(ws))

    def test_subject_psf_high_fires(self):
        # asking 200k / 1000 sqft = $200; comps $100/sqft -> 100% premium
        ws, _ = self.run_cli([prop_row(asking_price="200000", sqft="1000")],
                             [comp_row(160000, sqft=1600),
                              comp_row(160000, sqft=1600),
                              comp_row(160000, sqft=1600)])
        self.assertIn("SUBJECT_PSF_HIGH", self.flag_codes(ws))

    def test_subject_psf_boundary_exact_15(self):
        # comp psf = 100; subject psf exactly 115 -> premium 15% -> no flag
        ws, _ = self.run_cli([prop_row(asking_price="115000", sqft="1000")],
                             [comp_row(160000, sqft=1600),
                              comp_row(160000, sqft=1600),
                              comp_row(160000, sqft=1600)])
        self.assertAlmostEqual(ws["subject_price_per_sqft"], 115.0)
        self.assertAlmostEqual(ws["comp_median_price_per_sqft"], 100.0)
        self.assertNotIn("SUBJECT_PSF_HIGH", self.flag_codes(ws))

    def test_missing_critical_fields_flag(self):
        ws, _ = self.run_cli([prop_row(zip="", asking_price="")],
                             [comp_row(200000), comp_row(210000), comp_row(220000)])
        self.assertIn("MISSING_FIELDS", self.flag_codes(ws))
        ev = [f["evidence"] for f in ws["risk_flags"] if f["code"] == "MISSING_FIELDS"][0]
        self.assertIn("zip", ev["missing_fields"])
        self.assertIn("asking_price", ev["missing_fields"])

    def test_clean_packet_no_flags(self):
        ws, md = self.run_cli(
            [prop_row(asking_price="160000", sqft="1600")],  # $100/sqft, comps $100/sqft
            [comp_row(160000, days_ago=30), comp_row(160000, days_ago=40),
             comp_row(160000, days_ago=50)])
        self.assertEqual(ws["risk_flags"], [])
        self.assertIn("No risk flags fired.", md)


class TestInputs(Case):
    def test_missing_property_column_fatal(self):
        header = [c for c in PROP_HEADER if c != "asking_price"]
        write_csv(self.prop, header, [[r for r, c in zip(prop_row(), PROP_HEADER) if c != "asking_price"]])
        write_csv(self.comps, COMPS_HEADER, [comp_row(200000)])
        rc = rdp.main(["--property", self.prop, "--comps", self.comps, "--out", self.out])
        self.assertEqual(rc, 2)

    def test_missing_file_fatal(self):
        write_csv(self.comps, COMPS_HEADER, [comp_row(200000)])
        rc = rdp.main(["--property", os.path.join(self.tmp.name, "nope.csv"),
                       "--comps", self.comps, "--out", self.out])
        self.assertEqual(rc, 2)

    def test_bad_number_fatal(self):
        ws_rows = [prop_row(asking_price="lots")]
        write_csv(self.prop, PROP_HEADER, ws_rows)
        write_csv(self.comps, COMPS_HEADER, [comp_row(200000)])
        rc = rdp.main(["--property", self.prop, "--comps", self.comps, "--out", self.out])
        self.assertEqual(rc, 2)

    def test_comment_lines_skipped(self):
        with open(self.prop, "w", encoding="utf-8") as fh:
            fh.write("# a comment line\n")
            w = csv.writer(fh)
            w.writerow(PROP_HEADER)
            w.writerow(prop_row())
        with open(self.comps, "w", encoding="utf-8") as fh:
            fh.write("# another comment\n")
            w = csv.writer(fh)
            w.writerow(COMPS_HEADER)
            w.writerow(comp_row(200000))
            w.writerow(comp_row(210000))
            w.writerow(comp_row(220000))
        rc = rdp.main(["--property", self.prop, "--comps", self.comps, "--out", self.out])
        self.assertEqual(rc, 0)
        with open(os.path.join(self.out, "underwriting.json"), encoding="utf-8") as fh:
            ws = json.load(fh)
        self.assertEqual(ws["arv"]["count"], 3)

    def test_no_comps_graceful(self):
        write_csv(self.prop, PROP_HEADER, [prop_row()])
        write_csv(self.comps, COMPS_HEADER, [])
        rc = rdp.main(["--property", self.prop, "--comps", self.comps, "--out", self.out])
        self.assertEqual(rc, 0)
        with open(os.path.join(self.out, "underwriting.json"), encoding="utf-8") as fh:
            ws = json.load(fh)
        self.assertIsNone(ws["arv"]["median"])
        self.assertIn("FEW_COMPS", {f["code"] for f in ws["risk_flags"]})


class TestPacketFiles(Case):
    def test_expected_sections_present(self):
        ws, md = self.run_cli([prop_row()],
                             [comp_row(200000), comp_row(210000), comp_row(220000)])
        for section in ["## Property Summary", "## Comparable Sales",
                        "## Underwriting Worksheet", "## Risk Flags", "## Assumptions"]:
            self.assertIn(section, md)
        self.assertIn("0.70 x ARV - rehab_estimate", md)
        # JSON keys
        for key in ["arv", "mao", "max_offer", "spread_vs_asking",
                    "projected_profit_at_mao", "subject_price_per_sqft",
                    "comp_median_price_per_sqft", "risk_flags", "assumptions"]:
            self.assertIn(key, ws)

    def test_sample_inputs_consistent(self):
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        prop = os.path.join(base, "samples", "property.csv")
        comps = os.path.join(base, "samples", "comps.csv")
        self.assertTrue(os.path.exists(prop))
        self.assertTrue(os.path.exists(comps))
        rc = rdp.main(["--property", prop, "--comps", comps, "--out", self.out])
        self.assertEqual(rc, 0)
        with open(os.path.join(self.out, "underwriting.json"), encoding="utf-8") as fh:
            ws = json.load(fh)
        # Recompute independently from the sample comps CSV
        prices = []
        with open(comps, encoding="utf-8") as fh:
            for row in csv.DictReader([ln for ln in fh if not ln.lstrip().startswith("#")]):
                prices.append(float(row["sold_price"]))
        self.assertEqual(ws["arv"]["median"], rdp.median(prices))
        self.assertEqual(ws["arv"]["mean"], rdp.mean(prices))
        rehab = ws["property"]["rehab_estimate"]
        self.assertAlmostEqual(ws["max_offer"], 0.70 * ws["arv"]["median"] - rehab)
        self.assertAlmostEqual(ws["spread_vs_asking"], ws["asking_price"] - ws["max_offer"])
        self.assertAlmostEqual(
            ws["projected_profit_at_mao"],
            ws["arv"]["median"] - ws["max_offer"] - rehab)
        # Every risk flag carries evidence
        for f in ws["risk_flags"]:
            self.assertIn("code", f)
            self.assertIn("message", f)
            self.assertIn("evidence", f)


if __name__ == "__main__":
    unittest.main()


class TestValidation(Case):
    def test_property_two_rows_fatal(self):
        write_csv(self.prop, PROP_HEADER, [prop_row(), prop_row()])
        write_csv(self.comps, COMPS_HEADER, [comp_row(200000)])
        rc = rdp.main(["--property", self.prop, "--comps", self.comps, "--out", self.out])
        self.assertEqual(rc, 2)

    def test_property_no_rows_fatal(self):
        write_csv(self.prop, PROP_HEADER, [])
        write_csv(self.comps, COMPS_HEADER, [comp_row(200000)])
        rc = rdp.main(["--property", self.prop, "--comps", self.comps, "--out", self.out])
        self.assertEqual(rc, 2)

    def test_blank_trailing_rows_ignored(self):
        with open(self.prop, "w", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(PROP_HEADER)
            w.writerow(prop_row())
            fh.write("\n\n")  # trailing blank lines must not count as a second row
        with open(self.comps, "w", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(COMPS_HEADER)
            w.writerow(comp_row(200000))
            w.writerow(comp_row(210000))
            w.writerow(comp_row(220000))
            fh.write("\n")
        ws, _ = self.run_cli_from_files()
        self.assertEqual(ws["arv"]["count"], 3)

    def run_cli_from_files(self):
        rc = rdp.main(["--property", self.prop, "--comps", self.comps, "--out", self.out])
        self.assertEqual(rc, 0)
        with open(os.path.join(self.out, "underwriting.json"), encoding="utf-8") as fh:
            ws = json.load(fh)
        with open(os.path.join(self.out, "deal_packet.md"), encoding="utf-8") as fh:
            md = fh.read()
        return ws, md

    def test_negative_asking_price_fatal(self):
        write_csv(self.prop, PROP_HEADER, [prop_row(asking_price="-100")])
        write_csv(self.comps, COMPS_HEADER, [comp_row(200000)])
        rc = rdp.main(["--property", self.prop, "--comps", self.comps, "--out", self.out])
        self.assertEqual(rc, 2)

    def test_negative_comp_sold_price_fatal(self):
        write_csv(self.prop, PROP_HEADER, [prop_row()])
        write_csv(self.comps, COMPS_HEADER, [comp_row(-50000)])
        rc = rdp.main(["--property", self.prop, "--comps", self.comps, "--out", self.out])
        self.assertEqual(rc, 2)

    def test_negative_distance_fatal(self):
        write_csv(self.prop, PROP_HEADER, [prop_row()])
        rows = [comp_row(200000, **{"distance_miles": "-1"})]
        write_csv(self.comps, COMPS_HEADER, rows)
        rc = rdp.main(["--property", self.prop, "--comps", self.comps, "--out", self.out])
        self.assertEqual(rc, 2)

    def test_zero_beds_allowed(self):
        # A studio (0 beds) is legitimate input, not an error.
        ws, _ = self.run_cli([prop_row(beds="0")],
                             [comp_row(200000), comp_row(210000), comp_row(220000)])
        self.assertEqual(ws["property"]["beds"], 0)

    def test_out_path_is_existing_file_fatal(self):
        write_csv(self.prop, PROP_HEADER, [prop_row()])
        write_csv(self.comps, COMPS_HEADER, [comp_row(200000)])
        blocker = os.path.join(self.tmp.name, "blocker")
        with open(blocker, "w", encoding="utf-8") as fh:
            fh.write("not a directory")
        rc = rdp.main(["--property", self.prop, "--comps", self.comps, "--out", blocker])
        self.assertEqual(rc, 2)
        # The pre-existing file must be untouched.
        with open(blocker, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "not a directory")

    def test_us_date_accepted(self):
        row = comp_row(200000)
        row[COMPS_HEADER.index("sold_date")] = "08/12/2026"
        ws, _ = self.run_cli([prop_row()], [row, comp_row(210000), comp_row(220000)])
        self.assertEqual(ws["comps"][0]["sold_date"], "2026-08-12")

    def test_bad_date_error_mentions_accepted_formats(self):
        import io
        from contextlib import redirect_stderr
        write_csv(self.prop, PROP_HEADER, [prop_row()])
        row = comp_row(200000)
        row[COMPS_HEADER.index("sold_date")] = "12 Aug 2026"
        write_csv(self.comps, COMPS_HEADER, [row])
        err = io.StringIO()
        with redirect_stderr(err):
            rc = rdp.main(["--property", self.prop, "--comps", self.comps, "--out", self.out])
        self.assertEqual(rc, 2)
        self.assertIn("YYYY-MM-DD", err.getvalue())

    def test_pipe_in_comp_text_escaped_in_markdown(self):
        row = comp_row(200000, **{"address": "1 Main | Suite 2", "notes": "a|b"})
        _, md = self.run_cli([prop_row()], [row, comp_row(210000), comp_row(220000)])
        self.assertIn("1 Main \\| Suite 2", md)
        self.assertIn("a\\|b", md)

    def test_version_matches_pyproject(self):
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(base, "pyproject.toml"), encoding="utf-8") as fh:
            for line in fh:
                if line.strip().startswith("version"):
                    pkg_version = line.split("=")[1].strip().strip('"')
                    break
        self.assertEqual(rdp.VERSION, pkg_version)
