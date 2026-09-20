import unittest
from unittest.mock import patch
import estat_sources as es
import fetch_data as f


class EstatTests(unittest.TestCase):
    def fixture(self, values):
        return {'GET_STATS_DATA': {'RESULT': {'STATUS': 0}, 'STATISTICAL_DATA': {
            'CLASS_INF': {'CLASS_OBJ': [
                {'@id': 'cat01', 'CLASS': {'@code': 'A', '@name': 'Total'}},
                {'@id': 'time', 'CLASS': [
                    {'@code': 'x', '@name': '2025年1月'},
                    {'@code': 'a', '@name': '2025年'},
                ]}]}, 'DATA_INF': {'VALUE': values}}}}

    def test_monthly_only_and_missing_value(self):
        data = self.fixture([{'@cat01': 'A', '@time': 'x', '$': '***'},
                             {'@cat01': 'A', '@time': 'a', '$': '100'}])
        self.assertEqual(es.parse(data, {'cat01': 'A'}), [['2025-01-01', None]])

    def test_ambiguous_series_rejected(self):
        row = {'@cat01': 'A', '@time': 'x', '$': '1.2'}
        with self.assertRaisesRegex(ValueError, 'multiple observations'):
            es.parse(self.fixture([row, row]), {'cat01': 'A'})
        with self.assertRaisesRegex(ValueError, 'unrequested'):
            es.parse(self.fixture([dict(row, **{'@cat01': 'B'})]), {'cat01': 'A'})

    def test_calendar_yoy_preserves_missing_year(self):
        raw = {'ESTAT_jp_ip': {'points': [['2024-01-01', 100], ['2025-01-01', 110],
                                         ['2025-02-01', 120]]}}
        result = es.calculate(raw, f.calendar)['jp_ip']
        self.assertAlmostEqual(dict(result)['2025-01-01'], 10)
        self.assertIsNone(dict(result)['2025-02-01'])

    def test_credentials_not_in_request_error(self):
        with patch.dict('os.environ', {'ESTAT_APP_ID': 'secret-value'}):
            with patch('curl_cffi.requests.get', side_effect=Exception('url?appId=secret-value')):
                with self.assertRaisesRegex(ValueError, '^e-Stat request failed$'):
                    es.fetch_points('ESTAT_jp_cpi_all')

    def test_file_sources_do_not_require_key(self):
        with patch.dict('os.environ', {}, clear=True), patch.object(es, 'file_points', return_value=[['2025-01-01', 2]]):
            self.assertEqual(es.fetch_points('ESTAT_jp_retail'), [['2025-01-01', 2]])
