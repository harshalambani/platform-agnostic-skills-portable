#!/usr/bin/env python3
"""
Unit tests for transformation functions: date parsing, number parsing, Dr/Cr handling.
"""

import unittest
from datetime import datetime

# Import the module under test
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))

from agent import parse_date, parse_indian_number, cleanup_description


class TestDateParsing(unittest.TestCase):
    """Test date format detection and parsing."""

    def test_ddmmyyyy(self):
        self.assertEqual(parse_date("02/04/2025", "DD/MM/YYYY"), "2025-04-02")

    def test_ddmmyy(self):
        self.assertEqual(parse_date("02/04/25", "DD/MM/YY"), "2025-04-02")

    def test_mmddyyyy(self):
        self.assertEqual(parse_date("04/02/2025", "MM/DD/YYYY"), "2025-04-02")

    def test_iso8601(self):
        self.assertEqual(parse_date("2025-04-02", "YYYY-MM-DD"), "2025-04-02")

    def test_invalid_date(self):
        result = parse_date("99/99/9999", "DD/MM/YYYY")
        self.assertIsNone(result)

    def test_empty_date(self):
        self.assertIsNone(parse_date("", "DD/MM/YYYY"))
        self.assertIsNone(parse_date(None, "DD/MM/YYYY"))


class TestIndianNumberParsing(unittest.TestCase):
    """Test lakh/crore number format parsing."""

    def test_lakh_format(self):
        # 1,23,456.78 = 123456.78
        self.assertAlmostEqual(parse_indian_number("1,23,456.78"), 123456.78)

    def test_crore_format(self):
        # 1,00,00,000.00 = 10000000.00
        self.assertAlmostEqual(parse_indian_number("1,00,00,000.00"), 10000000.0)

    def test_simple_number(self):
        self.assertAlmostEqual(parse_indian_number("1234.56"), 1234.56)

    def test_with_commas(self):
        self.assertAlmostEqual(parse_indian_number("1,234.56"), 1234.56)

    def test_zero(self):
        self.assertAlmostEqual(parse_indian_number("0"), 0.0)
        self.assertAlmostEqual(parse_indian_number("0.00"), 0.0)

    def test_empty_or_none(self):
        self.assertEqual(parse_indian_number(""), 0.0)
        self.assertEqual(parse_indian_number(None), 0.0)

    def test_large_number(self):
        # 12,34,56,789.99 = 123456789.99
        self.assertAlmostEqual(parse_indian_number("12,34,56,789.99"), 123456789.99)


class TestDescriptionCleanup(unittest.TestCase):
    """Test description cleaning logic."""

    def test_upi_vpa_preserved(self):
        desc = "UPI-MERCHANT-ABC-7359777800-2@OKBIZAXIS-UTIB0000553"
        result = cleanup_description(desc)
        self.assertIn("@", result)

    def test_neft_preserved(self):
        desc = "NEFT CR-XYZB0001234-ACME CONSULTING LLP-JOHN DOE"
        result = cleanup_description(desc)
        self.assertTrue(result.startswith("NEFT"))

    def test_imps_preserved(self):
        desc = "IMPS-RECIPIENT-XXXX"
        result = cleanup_description(desc)
        self.assertTrue(result.startswith("IMPS"))

    def test_trimmed_length(self):
        desc = "X" * 150
        result = cleanup_description(desc)
        self.assertLessEqual(len(result), 150)

    def test_empty_description(self):
        self.assertEqual(cleanup_description(""), "")
        self.assertEqual(cleanup_description(None), "")

    def test_whitespace_trimmed(self):
        desc = "  MERCHANT NAME  "
        result = cleanup_description(desc)
        self.assertEqual(result, "MERCHANT NAME")


class TestIntegration(unittest.TestCase):
    """Integration tests: spec generation + transform pipeline."""

    def test_hdfc_statement_sample(self):
        """Test with actual HDFC statement sample rows."""
        from agent import ColumnSpec, transform_row

        # Sample HDFC row
        row = ["02/04/25", "CGST-MANAGED CUSTOMER BENEFIT", "NCB2609278553728", "02/04/25", "225", "", "47037.08"]

        # Expected spec from HDFC format
        spec = ColumnSpec({
            'date_column': 0,
            'date_format': 'DD/MM/YY',
            'description_column': 1,
            'withdrawal_column': 4,
            'deposit_column': 5,
            'balance_column': 6,
            'currency': 'INR',
            'txn_id_column': 2,
        })

        result = transform_row(row, spec, account_name="Checking")

        self.assertIsNotNone(result)
        self.assertEqual(result['date'], '2025-04-02')
        self.assertEqual(result['withdrawal'], '225.00')
        self.assertEqual(result['deposit'], '0.00')
        self.assertAlmostEqual(float(result['balance']), 47037.08)
        self.assertEqual(result['currency'], 'INR')
        self.assertEqual(result['account'], 'Checking')


if __name__ == '__main__':
    unittest.main()
