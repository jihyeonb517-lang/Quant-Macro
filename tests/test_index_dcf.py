import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

import index_dcf_source as dcf


class IndexDCFTests(unittest.TestCase):
    def test_two_stage_value(self):
        value = dcf.dcf_value(
            cf0=100,
            stage1_growth_pct=5,
            stage1_years=2,
            terminal_growth_pct=2,
            risk_free_rate_pct=3,
            erp_pct=5,
        )
        explicit = 105/1.08 + 110.25/1.08**2
        terminal = (110.25*1.02/(0.08-0.02))/1.08**2
        self.assertAlmostEqual(value, explicit + terminal)

    def test_discount_rate_must_exceed_terminal_growth(self):
        with self.assertRaisesRegex(ValueError, 'must exceed'):
            dcf.dcf_value(100, 5, 5, 8, 3, 5)

    def test_csv_generates_value_market_gap_and_assumptions(self):
        text = (
            'date,index_id,index_level,cf0,stage1_growth_pct,stage1_years,'
            'terminal_growth_pct,risk_free_rate_pct,erp_pct\n'
            '2026-07-01,sp500,5000,100,5,2,2,3,5\n'
            '2026-07-01,nasdaq100,18000,200,6,3,2,3,5\n'
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'index_dcf.csv'
            path.write_text(text, encoding='utf-8')
            with patch.object(dcf, 'CSV_PATH', path):
                rows = dcf._rows(date(2026, 9, 23))
        values = rows[('sp500', '2026-07-01')]
        self.assertEqual(values['MARKET'], 5000)
        self.assertEqual(values['DISCOUNT_RATE'], 8)
        self.assertAlmostEqual(values['GAP'],
                               (values['VALUE']/5000-1)*100)
        self.assertEqual(rows[('nasdaq100', '2026-07-01')]['MARKET'], 18000)

    def test_header_only_template_is_allowed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'index_dcf.csv'
            path.write_text(','.join(dcf._REQUIRED) + '\n', encoding='utf-8')
            with patch.object(dcf, 'CSV_PATH', path):
                self.assertEqual(
                    dcf.fetch_points(dcf.source_id('sp500', 'VALUE')), [])

    def test_invalid_row_reports_line_number(self):
        text = (','.join(dcf._REQUIRED) +
                '\n2026-07-01,topix,3000,100,5,2,9,3,5\n')
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'index_dcf.csv'
            path.write_text(text, encoding='utf-8')
            with patch.object(dcf, 'CSV_PATH', path):
                with self.assertRaisesRegex(ValueError, 'line 2'):
                    dcf.fetch_points(dcf.source_id('topix', 'VALUE'))


if __name__ == '__main__':
    unittest.main()
