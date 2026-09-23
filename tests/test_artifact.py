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
        self.assertEqual(set(data['dcfInputs']), {'sp500', 'nasdaq100', 'nikkei225', 'topix'})
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

        self.assertEqual([m['id'] for m in data['metrics']], [s[0] for s in f.SPECS])
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

    def test_korea_metrics_and_cli_have_seeded_history(self):
        data = json.loads((f.ROOT/'data.json').read_text(encoding='utf-8'))
        metrics = {item['id']: item for item in data['metrics']}
        for metric_id in ('kr_reserves', 'kr_household_credit', 'kr_house_prices'):
            self.assertTrue(metrics[metric_id]['points'])
            self.assertIsNotNone(metrics[metric_id]['value'])
        self.assertTrue(data['regimes']['kr'])

    def test_dcf_is_a_browser_calculator(self):
        html = (f.ROOT/'dcf.html').read_text(encoding='utf-8')
        self.assertIn("fetch('./data.json'", html)
        self.assertIn('localStorage', html)
        self.assertIn('implied', html)
        self.assertNotIn('index_dcf.csv', html)


if __name__ == '__main__':
    unittest.main()
