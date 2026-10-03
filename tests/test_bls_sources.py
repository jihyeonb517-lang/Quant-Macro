import json
import unittest
from datetime import date
from unittest.mock import patch, MagicMock

import bls_sources as bls
import fetch_data as f


def payload(rows, sid='CES0000000001'):
    return {'status': 'REQUEST_SUCCEEDED', 'Results': {'series': [
        {'seriesID': sid, 'data': rows}]}}


class BLSTests(unittest.TestCase):
    def test_reversed_months_and_annual_average(self):
        result = bls.parse_points(payload([
            {'year': '2026', 'period': 'M09', 'value': '159,044'},
            {'year': '2026', 'period': 'M13', 'value': '159000'},
            {'year': '2026', 'period': 'M08', 'value': '159015'},
        ]), 'CES0000000001')
        self.assertEqual(result, [['2026-08-01', 159015], ['2026-09-01', 159044]])

    def test_missing_or_failed_series_rejected_even_with_http_success(self):
        for response in [payload([], 'OTHER'), payload([]), {'status': 'REQUEST_FAILED'}, {}]:
            with self.assertRaises(ValueError):
                bls.parse_points(response, 'CES0000000001')

    def test_unavailable_month_remains_a_gap(self):
        result = bls.parse_points(payload([
            {'year': '2025', 'period': 'M11', 'value': '4.6'},
            {'year': '2025', 'period': 'M10', 'value': '-'},
            {'year': '2025', 'period': 'M09', 'value': '4.4'},
        ], 'LNS14000000'), 'LNS14000000')
        self.assertEqual(result, [['2025-09-01', 4.4], ['2025-10-01', None],
                                  ['2025-11-01', 4.6]])

    def test_registered_key_in_body_only_and_twenty_year_window(self):
        response = MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps(payload([
            {'year': '2026', 'period': 'M09', 'value': '159044'},
        ])).encode()
        with patch.dict('os.environ', {'BLS_API_KEY': 'test-secret'}), \
                patch.object(bls, 'urlopen', return_value=response) as request:
            bls.fetch_points('PAYEMS')
        req = request.call_args.args[0]
        body = json.loads(req.data)
        self.assertEqual(body['registrationkey'], 'test-secret')
        self.assertEqual(int(body['endyear'])-int(body['startyear']), 19)
        self.assertNotIn('test-secret', req.full_url)

    def test_transport_exception_never_discloses_key(self):
        with patch.dict('os.environ', {'BLS_API_KEY': 'test-secret'}), \
                patch.object(bls, 'urlopen', side_effect=ValueError('test-secret')):
            with self.assertRaises(RuntimeError) as failure:
                bls.fetch_points('PAYEMS')
        self.assertNotIn('test-secret', str(failure.exception))

    def test_headline_matches_bls_release_and_total_equals_components(self):
        rows = {
            'PAYEMS': [158892, 158882, 159015, 159044],
            'USPRIV': [135554, 135582, 135671, 135717],
            'USGOVT': [23338, 23300, 23344, 23327],
            'UNRATE': [4.1, 4.2, 4.1, 4.2],
        }
        raw = {sid: {'points': [[f'2026-{6+i:02d}-01', v] for i, v in enumerate(values)],
                     'origin': 'BLS', 'retrieved': '2026-10-02T13:00:00+00:00', 'error': None}
               for sid, values in rows.items()}
        built = f.build(raw, {}, '2026-10-02T13:00:00+00:00', date(2026, 10, 3))
        metrics = {m['id']: m for m in built['metrics']}
        self.assertEqual(metrics['jobs']['value'], 29)
        self.assertEqual(metrics['jobs_private']['value'], 46)
        self.assertEqual(metrics['jobs_government']['value'], -17)
        self.assertEqual(metrics['jobs']['delta'], -104)
        self.assertAlmostEqual(metrics['unemployment']['delta'], .1)
        self.assertEqual(metrics['jobs']['sources'][0]['origin'], 'BLS')
        self.assertIn('CES0000000001', metrics['jobs']['sources'][0]['url'])
        self.assertEqual(metrics['jobs']['value'], metrics['jobs_private']['value'] + metrics['jobs_government']['value'])

    def test_provider_outage_does_not_restore_old_three_month_formula(self):
        raw = {'PAYEMS': {'points': [['2026-06-01', 158892], ['2026-07-01', 158882],
                                   ['2026-08-01', 159015], ['2026-09-01', 159044]],
                          'retrieved': '2026-10-02T13:00:00+00:00', 'error': 'timeout'}}
        previous = f.build(raw, {}, None, date(2026, 10, 3))
        previous['metrics'][0].update(value=50.6667, formula='old 3 month average',
                                      period='직전 3개월 평균 대비', note='old smoothed note')
        metric = f.build(raw, previous, None, date(2026, 10, 3))['metrics'][0]
        self.assertEqual(metric['value'], 29)
        self.assertEqual(metric['note'], bls.NOTE)
        self.assertEqual(metric['status'], 'stale')
        self.assertEqual(metric['sources'][0]['origin'], 'FRED')


if __name__ == '__main__':
    unittest.main()
