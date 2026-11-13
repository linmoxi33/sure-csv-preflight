import json
import os
import subprocess
import sys
import unittest
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import sure_csv_preflight as sp  # noqa: E402

CLI = ROOT / "sure_csv_preflight.py"
EXAMPLES = ROOT / "examples"


def run(*args):
    return subprocess.run(
        [sys.executable, str(CLI), *map(str, args)],
        capture_output=True,
        text=True,
        cwd=str(ROOT),
    )


class NumberFormatPort(unittest.TestCase):
    """Behaviour of Import#sanitize_number, ported 1:1."""

    def test_us_thousands_and_decimals(self):
        self.assertEqual(sp.sanitize_number("1,234.56", "1,234.56"), Decimal("1234.56"))

    def test_eu_thousands_and_decimals(self):
        self.assertEqual(sp.sanitize_number("1.234,56", "1.234,56"), Decimal("1234.56"))

    def test_eu_reads_us_value_as_tiny_number(self):
        # "1,234.56" under the EU format: the dot is the thousands delimiter and
        # is deleted, the comma becomes the decimal point -> 1.23456, silently.
        self.assertEqual(sp.sanitize_number("1,234.56", "1.234,56"), Decimal("1.23456"))

    def test_us_reads_eu_value_as_tiny_number(self):
        self.assertEqual(sp.sanitize_number("1.234,56", "1,234.56"), Decimal("1.23456"))

    def test_space_format_rejects_us_style_value(self):
        # The space branch guards interior junk on purpose: rejected, not reinterpreted.
        self.assertIsNone(sp.sanitize_number("1,234.56", "1 234,56"))

    def test_space_format_accepts_nbsp(self):
        self.assertEqual(sp.sanitize_number("1\u00a0234,56", "1 234,56"), Decimal("1234.56"))

    def test_zero_decimal_format_truncates_structure(self):
        # separator is "" so the dot is treated as junk and removed: 123456.
        self.assertEqual(sp.sanitize_number("1,234.56", "1,234"), Decimal("123456"))

    def test_double_dot_is_rejected(self):
        self.assertIsNone(sp.sanitize_number("12.3.4", "1,234.56"))

    def test_currency_symbols_are_stripped(self):
        self.assertEqual(sp.sanitize_number("$1,234.56", "1,234.56"), Decimal("1234.56"))

    def test_garbage_is_rejected(self):
        self.assertIsNone(sp.sanitize_number("n/a", "1,234.56"))


class SignagePort(unittest.TestCase):
    """Import::Row#signed_amount: positive == outflow in Sure."""

    def base(self, **kw):
        cfg = dict(sp.DEFAULT_CONFIG)
        cfg.update(kw)
        return cfg

    def test_inflows_positive_inverts_raw_value(self):
        self.assertEqual(sp.signed_amount(Decimal("20.50"), self.base()), Decimal("-20.50"))

    def test_inflows_negative_keeps_raw_value(self):
        cfg = self.base(signage_convention="inflows_negative")
        self.assertEqual(sp.signed_amount(Decimal("20.50"), cfg), Decimal("20.50"))

    def test_custom_column_matches_identifier(self):
        cfg = self.base(
            amount_type_strategy="custom_column",
            amount_type_inflow_value="credit",
            signage_convention="inflows_positive",
        )
        self.assertEqual(sp.signed_amount(Decimal("10"), cfg, "credit"), Decimal("-10"))
        self.assertEqual(sp.signed_amount(Decimal("10"), cfg, "debit"), Decimal("10"))


class DatePort(unittest.TestCase):
    def test_valid_date(self):
        parsed, err = sp.parse_date("2026-01-04", "%Y-%m-%d", sp.date(2026, 11, 13))
        self.assertEqual(parsed, sp.date(2026, 1, 4))
        self.assertIsNone(err)

    def test_out_of_range_date(self):
        parsed, err = sp.parse_date("1969-12-31", "%Y-%m-%d", sp.date(2026, 11, 13))
        self.assertIsNone(parsed)
        self.assertIn("range", err)

    def test_future_date_rejected(self):
        parsed, _ = sp.parse_date("2027-01-01", "%Y-%m-%d", sp.date(2026, 11, 13))
        self.assertIsNone(parsed)

    def test_wrong_shape_date(self):
        parsed, err = sp.parse_date("04/01/2026", "%Y-%m-%d", sp.date(2026, 11, 13))
        self.assertIsNone(parsed)
        self.assertIn("%Y-%m-%d", err)


class CliBehaviour(unittest.TestCase):
    """Real process runs: exit codes and machine-readable output."""

    def test_clean_file_exits_zero(self):
        proc = run("--csv", EXAMPLES / "in" / "clean-us.csv", "--today", "2026-11-13", "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["summary"]["rows_importable"], 3)
        self.assertEqual(
            [f for f in payload["findings"] if f["severity"] in ("error", "warning")], []
        )

    def test_ambiguous_config_exits_one(self):
        proc = run(
            "--csv", EXAMPLES / "in" / "us-bank.csv",
            "--config", EXAMPLES / "in" / "config-eu.json",
            "--today", "2026-11-13",
        )
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertIn("AMOUNT_FORMAT_AMBIGUOUS", proc.stdout)

    def test_fail_on_error_ignores_warnings(self):
        proc = run(
            "--csv", EXAMPLES / "in" / "us-bank.csv",
            "--config", EXAMPLES / "in" / "config-eu.json",
            "--today", "2026-11-13", "--fail-on", "error",
        )
        # us-bank.csv has one real error (2026-13-40), so 1 here.
        self.assertEqual(proc.returncode, 1)
        proc = run(
            "--csv", EXAMPLES / "in" / "clean-us.csv",
            "--today", "2026-11-13", "--fail-on", "never",
        )
        self.assertEqual(proc.returncode, 0)

    def test_bad_config_exits_two(self):
        bad = Path(ROOT / "tests" / "_bad-config.json")
        bad.write_text('{"number_format": "1.234.567,89"}', encoding="utf-8")
        try:
            proc = run("--csv", EXAMPLES / "in" / "clean-us.csv", "--config", bad)
            self.assertEqual(proc.returncode, 2)
            self.assertIn("number_format", proc.stderr)
        finally:
            bad.unlink()

    def test_missing_file_exits_two(self):
        proc = run("--csv", "does-not-exist.csv")
        self.assertEqual(proc.returncode, 2)

    def test_json_is_parseable_and_matches_example(self):
        proc = run(
            "--csv", EXAMPLES / "in" / "us-bank.csv",
            "--config", EXAMPLES / "in" / "config-eu.json",
            "--today", "2026-11-13", "--json",
        )
        self.assertEqual(proc.returncode, 1)
        payload = json.loads(proc.stdout)
        expected = json.loads((EXAMPLES / "out" / "report.json").read_text(encoding="utf-8"))
        # source.path is the only thing that may differ between invocations
        payload["source"].pop("path")
        expected["source"].pop("path")
        self.assertEqual(payload, expected)

    def test_two_runs_are_byte_identical(self):
        args = (
            "--csv", EXAMPLES / "in" / "us-bank.csv",
            "--config", EXAMPLES / "in" / "config-eu.json",
            "--today", "2026-11-13", "--json",
        )
        first = run(*args).stdout
        second = run(*args).stdout
        self.assertEqual(first, second)

    def test_missing_header_is_reported(self):
        proc = run("--csv", EXAMPLES / "in" / "clean-us.csv", "--json")
        self.assertNotIn("HEADER_MISSING", proc.stdout)
        cfg = Path(ROOT / "tests" / "_rename.json")
        cfg.write_text('{"amount_col_label": "value"}', encoding="utf-8")
        try:
            proc = run("--csv", EXAMPLES / "in" / "clean-us.csv", "--config", cfg, "--json")
            self.assertEqual(proc.returncode, 1)
            self.assertIn("HEADER_MISSING", proc.stdout)
        finally:
            cfg.unlink()


if __name__ == "__main__":
    unittest.main(verbosity=2)
