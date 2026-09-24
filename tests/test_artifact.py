import json
import math
import unittest
from datetime import datetime

import fetch_data as f


class ArtifactTests(unittest.TestCase):
    def test_data_contract(self):
        data = json.loads((f.ROOT/'data.json').read_text(encoding='utf-8'))
        self.assertEqual(
            set(data),
            {'generatedAt', 'method', 'metrics', 'regimes', 'dcfInputs'},
        )
        self.assertIsNotNone(datetime.fromisoformat(data['generatedAt']).tzinfo)

        self.assertEqual(set(data['regimes']), {'us', 'jp', 'kr'})
        self.assertTrue({'sp500', 'nikkei225'}.issubset(data['dcfInputs']))
        self.assertLessEqual(set(data['dcfInputs']), {'sp500', 'nasdaq100', 'nikkei225', 'topix'})
        for item in data['dcfInputs'].values():
            self.assertEqual(
                set(item),
                {'label', 'indexPoints', 'riskFreePoints', 'indexSource', 'riskFreeSource'},
            )
        valid_regimes = {
            'contraction',
            'recovery',
            'expansion',
            'slowdown',
        }
        for country in ('us', 'jp', 'kr'):
            intervals = data['regimes'][country]
            self.assertIsInstance(intervals, list)
            previous_end = None
            for interval in intervals:
                self.assertEqual(len(interval), 3)
                start, end, regime = interval
                datetime.fromisoformat(start)
                datetime.fromisoformat(end)
                self.assertLess(start, end)
                self.assertIn(regime, valid_regimes)
                if previous_end is not None:
                    self.assertLessEqual(previous_end, start)
                previous_end = end

        metric_ids = [m['id'] for m in data['metrics']]
        expected_ids = [s[0] for s in f.SPECS]
        # The checked-in JSON can be a previous successful snapshot when a
        # provider is temporarily unavailable. Refresh normally rebuilds the
        # full set before publishing, while this validation still permits the
        # preserved snapshot to deploy as a fallback.
        self.assertEqual(len(metric_ids), len(set(metric_ids)))
        retired = {'kr_reserves', 'kr_household_credit', 'kr_house_prices'}
        self.assertTrue(set(metric_ids).issubset(set(expected_ids) | retired))
        required = {'id', 'section', 'title', 'unit', 'points', 'date', 'value', 'delta',
                    'period', 'note', 'formula', 'status', 'sources', 'secondary'}
        source_keys = {'id', 'url', 'observed', 'retrieved', 'origin', 'frequency', 'age',
                       'maxAge', 'status', 'fallback'}
        for m in data['metrics']:
            self.assertEqual(set(m) - {'text'}, required)
            self.assertIn(m['status'], ['ok', 'stale', 'missing'])
            for source in m['sources']:
                self.assertEqual(set(source), source_keys)
            for points in [m['points']] + ([m['secondary']['points']] if m['secondary'] else []):
                dates = [d for d, _ in points]
                self.assertEqual(dates, sorted(set(dates)))
                for _, value in points:
                    self.assertTrue(value is None or math.isfinite(value))
            usable = [p for p in m['points'] if p[1] is not None]
            if usable:
                self.assertEqual([m['date'], m['value']], usable[-1])

    def test_async_loading_and_error_state(self):
        html = (f.ROOT/'index.html').read_text(encoding='utf-8')
        self.assertIn("fetch('./data.json'", html)
        self.assertNotIn('const DATA={', html)
        self.assertIn('if (!response.ok)', html)
        self.assertIn('})().catch(error =>', html)

    def test_korea_metrics_use_domestic_providers_and_cli_has_seeded_history(self):
        data = json.loads((f.ROOT/'data.json').read_text(encoding='utf-8'))
        korea_sources = {
            sid for _mid, section, _title, _unit, deps, *_ in f.SPECS
            if section == 'korea' for sid in deps
        }
        self.assertTrue(all(sid.startswith(('ECOS_', 'KOSIS_')) or sid == 'KRW=X'
                            for sid in korea_sources))
        self.assertFalse({'TRESEGKRM052N', 'CRDQKRAHABIS', 'QKRN628BIS',
                          'KORXTEXVA01GYSAM', 'KORPRMNTO01GYSAM',
                          'KORSLRTTO01GYSAM', 'LRUNTTTTKRM156S'} & korea_sources)
        self.assertTrue(data['regimes']['kr'])

    def test_fx_pairs_and_yen_quotation(self):
        raw = {
            'JPYKRW=X': {'points': [['2026-09-23', 8.7]]},
            'KRW=X': {'points': [['2026-09-23', 1400.0]]},
            'CNY=X': {'points': [['2026-09-23', 7.0]]},
        }
        calculated = f.calculate_base(raw)
        self.assertAlmostEqual(calculated['jpykrw_100'][0][1], 870.0)
        self.assertAlmostEqual(calculated['cnykrw'][0][1], 200.0)
        fx_ids = {spec[0] for spec in f.SPECS if spec[1] == 'fx'}
        self.assertTrue({'eurusd', 'gbpusd', 'eurgbp', 'usdjpy', 'usdkrw', 'usdcny',
                         'gbpjpy', 'eurjpy', 'gbpkrw', 'eurkrw', 'jpykrw_100', 'cnykrw', 'dxy'} <= fx_ids)

    def test_footer_has_deep_links_for_individual_metrics(self):
        html = (f.ROOT/'index.html').read_text(encoding='utf-8')
        self.assertIn('id="footer-nav"', html)
        self.assertIn('const [g,s,metricId]', html)
        self.assertIn('function renderFooterNav()', html)

    def test_dcf_is_a_browser_calculator(self):
        html = (f.ROOT/'dcf.html').read_text(encoding='utf-8')
        self.assertIn("fetch('./data.json'", html)
        self.assertIn('localStorage', html)
        self.assertIn('implied', html)
        self.assertNotIn('index_dcf.csv', html)
        self.assertEqual({row[0] for row in f.DCF_INDEXES}, {'sp500', 'nikkei225'})
        self.assertIn("const indexIds=['sp500','nikkei225'];", html)


if __name__ == '__main__':
    unittest.main()

