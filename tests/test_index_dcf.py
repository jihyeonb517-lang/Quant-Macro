import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

import index_dcf_source as dcf


class IndexDCFTests(unittest.TestCase):
    def test_guide_structure_value_and_terminal_payout(self):
        result = dcf.dcf_breakdown(
            eps=100, stage1_growth_pct=5, stage1_years=2,
            dividend_payout_pct=30, buyback_payout_pct=20,
            risk_free_rate_pct=3, erp_pct=5, terminal_growth_pct=2,
            long_term_risk_free_rate_pct=3, long_term_erp_pct=5,
            long_term_roe_pct=10)
        explicit = 52.5/1.08 + 55.125/1.08**2
        terminal_cf = 110.25*1.02*(1-.02/.10)
        terminal_pv = (terminal_cf/(.08-.02))/1.08**2
        self.assertAlmostEqual(result['VALUE'], explicit + terminal_pv)
        self.assertEqual(result['SHAREHOLDER_PAYOUT'], 50)
        self.assertAlmostEqual(result['TERMINAL_PAYOUT'], 80)
        self.assertAlmostEqual(result['CF0'], 50)

    def test_terminal_discount_and_roe_must_exceed_growth(self):
        args = dict(eps=100, stage1_growth_pct=5, stage1_years=2,
                    dividend_payout_pct=30, buyback_payout_pct=20,
                    risk_free_rate_pct=3, erp_pct=5, terminal_growth_pct=8,
                    long_term_risk_free_rate_pct=3, long_term_erp_pct=5,
                    long_term_roe_pct=10)
        with self.assertRaisesRegex(ValueError, 'must exceed terminal growth'):
            dcf.dcf_breakdown(**args)
        args.update(terminal_growth_pct=5, long_term_roe_pct=5)
        with self.assertRaisesRegex(ValueError, 'must exceed terminal_growth_pct'):
            dcf.dcf_breakdown(**args)

    def test_csv_generates_all_sections_and_overvaluation(self):
        text = (','.join(dcf._REQUIRED) + '\n' +
                '2026-07-01,sp500,5000,200,8,5,30,45,3,5,2.5,3,5,13\n' +
                '2026-07-01,nasdaq100,18000,500,9,5,20,50,3,5,2.5,3,5,13\n')
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'index_dcf.csv'
            path.write_text(text, encoding='utf-8')
            with patch.object(dcf, 'CSV_PATH', path):
                rows = dcf._rows(date(2026, 9, 23))
        values = rows[('sp500', '2026-07-01')]
        self.assertEqual(values['MARKET'], 5000)
        self.assertEqual(values['DISCOUNT_RATE'], 8)
        self.assertEqual(values['SHAREHOLDER_PAYOUT'], 75)
        self.assertAlmostEqual(values['GAP'], 5000/values['VALUE']*100-100)
        self.assertGreater(values['TERMINAL_PV'], 0)
        self.assertGreater(values['IMPLIED_PE'], 0)
        self.assertEqual(rows[('nasdaq100', '2026-07-01')]['MARKET'], 18000)

    def test_header_only_template_is_allowed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'index_dcf.csv'
            path.write_text(','.join(dcf._REQUIRED) + '\n', encoding='utf-8')
            with patch.object(dcf, 'CSV_PATH', path):
                self.assertEqual(dcf.fetch_points(dcf.source_id('sp500', 'VALUE')), [])

    def test_invalid_row_reports_line_number(self):
        text = (','.join(dcf._REQUIRED) +
                '\n2026-07-01,topix,3000,100,5,2,30,40,3,5,9,3,5,13\n')
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'index_dcf.csv'
            path.write_text(text, encoding='utf-8')
            with patch.object(dcf, 'CSV_PATH', path):
                with self.assertRaisesRegex(ValueError, 'line 2'):
                    dcf.fetch_points(dcf.source_id('topix', 'VALUE'))


if __name__ == '__main__':
    unittest.main()
