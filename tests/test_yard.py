"""The yard book is the contract. Assignments are checked against a hand trace."""

from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path

from standard_iron.yard import build_weeks, load_yard, run_yard, standard_offers

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "fixtures" / "yard.json"
RESULT = (
    "standard-iron ships 6/7 orders (bespoke-reman 4/7, new-build 0/7). "
    "The 20-week SI-50 order ships from the shelf in 8 weeks; "
    "bespoke's fastest exact nameplate is 36 weeks."
)


class YardTest(unittest.TestCase):
    def setUp(self) -> None:
        self.yard = load_yard(FIXTURE)
        self.report = run_yard(self.yard)

    def test_half_lead_time_and_learning_floor(self) -> None:
        for sku in self.yard.skus:
            self.assertEqual(sku.first_build_weeks * 2, sku.new_weeks)
        si50 = self.yard.sku("SI-50")
        offers = {offer.unit_id: offer for offer in standard_offers(self.yard, si50)}
        self.assertEqual(offers["u3"].weeks, 8)
        self.assertEqual(offers["u3"].kind, "shelf")
        self.assertEqual(offers["u1"].weeks, 52)
        self.assertEqual(offers["u10"].weeks, 46)
        self.assertEqual(offers["u11"].weeks, 40)
        self.assertEqual(offers["u2"].weeks, 34)
        self.assertEqual(offers["u9"].weeks, 34)
        self.assertEqual(build_weeks(si50, 5), 34)
        self.assertNotIn("u4", offers)
        self.assertNotIn("u5", offers)
        self.assertNotIn("u8", offers)
        si30 = {offer.unit_id: offer for offer in standard_offers(self.yard, self.yard.sku("SI-30"))}
        self.assertEqual(si30["u6"].weeks, 46)
        self.assertNotEqual(si30["u6"].weeks, 40)

    def test_ship_counts_and_assignments(self) -> None:
        bespoke = self.report.policy("bespoke-reman")
        standard = self.report.policy("standard-iron")
        fresh = self.report.policy("new-build")
        self.assertEqual(bespoke.shipped_count, 4)
        self.assertEqual(standard.shipped_count, 6)
        self.assertEqual(fresh.shipped_count, 0)
        self.assertEqual([miss.order_id for miss in bespoke.missed], ["o6", "o1", "o5"])
        self.assertEqual([miss.order_id for miss in standard.missed], ["o7"])
        self.assertEqual(standard.missed[0].soonest_week, 46)
        shipped = {(item.order_id, item.unit_id, item.weeks, item.kind) for item in standard.shipped}
        self.assertEqual(
            shipped,
            {
                ("o1", "u3", 8, "shelf"),
                ("o2", "u2", 34, "build"),
                ("o3", "u9", 34, "build"),
                ("o4", "u11", 40, "build"),
                ("o5", "u10", 46, "build"),
                ("o6", "u7", 8, "shelf"),
            },
        )
        bespoke_shipped = {(item.order_id, item.unit_id, item.weeks) for item in bespoke.shipped}
        self.assertEqual(
            bespoke_shipped,
            {("o2", "u11", 36), ("o3", "u2", 44), ("o4", "u1", 56), ("o7", "u6", 40)},
        )

    def test_failure_quotes(self) -> None:
        quotes = {quote.failure: quote for quote in self.report.quotes}
        self.assertEqual((quotes["gasket-leak"].seen, quotes["gasket-leak"].learned_usd), (5, 160000))
        self.assertEqual((quotes["bushing"].seen, quotes["bushing"].learned_usd), (3, 234000))
        self.assertEqual((quotes["insulation-age"].seen, quotes["insulation-age"].learned_usd), (3, 306000))
        self.assertEqual(quotes["core-melt"].seen, 1)
        self.assertFalse(quotes["core-melt"].quotable)
        self.assertIsNone(quotes["core-melt"].learned_usd)

    def test_result_line(self) -> None:
        self.assertEqual(self.report.result_line, RESULT)

    def test_cli(self) -> None:
        proc = subprocess.run(
            [sys.executable, "-m", "standard_iron", str(FIXTURE)],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn(RESULT, proc.stdout)
        self.assertIn("o7", proc.stdout)

    def test_cli_json(self) -> None:
        proc = subprocess.run(
            [sys.executable, "-m", "standard_iron", "--json", str(FIXTURE)],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["result"], RESULT)
        by_name = {row["policy"]: row for row in payload["policies"]}
        self.assertEqual(by_name["standard-iron"]["shipped"], 6)
        self.assertEqual(by_name["bespoke-reman"]["shipped"], 4)
        self.assertEqual(by_name["new-build"]["missed"], 7)

    def test_missing_file(self) -> None:
        proc = subprocess.run(
            [sys.executable, "-m", "standard_iron", "fixtures/missing.json"],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 2)
        self.assertTrue(proc.stderr.strip())
