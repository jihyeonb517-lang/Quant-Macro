import copy
import json
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

import fetch_data as f
import japan_sources as jp


def raw(**series):
    return {sid: {'points': points, 'retrieved': '2026-09-07T00:00:00+00:00', 'error': None}
            for sid, points in series.items()}




class FormulaTests(unittest.TestCase):
    def test_payroll_total_private_and_government_use_monthly_changes(self):
        data = raw(
            PAYEMS=[['2026-01-01', 1000], ['2026-02-01', 1010], ['2026-03-01', 1020], ['2026-04-01', 1037]],
            USPRIV=[['2026-01-01', 800], ['2026-02-01', 808], ['2026-03-01', 816], ['2026-04-01', 832]],
            USGOVT=[['2026-01-01', 200], ['2026-02-01', 202], ['2026-03-01', 204], ['2026-04-01', 205]],
        )
        calculated = f.calculate(data)
        self.assertEqual(calculated['jobs'][-1], ['2026-04-01', 17])
        self.assertEqual(calculated['jobs_private'][-1], ['2026-04-01', 16])
        self.assertEqual(calculated['jobs_government'][-1], ['2026-04-01', 1])

    def test_claims_weekly_and_four_week_average_are_both_exposed(self):
        data = raw(ICSA=[
            ['2026-01-07', 100000], ['2026-01-14', 200000],
            ['2026-01-21', 300000], ['2026-01-28', 400000],
        ])
        calculated = f.calculate(data)
        self.assertEqual(calculated['claims_weekly'][-1], ['2026-01-28', 400])
        self.assertEqual(calculated['claims'][-1], ['2026-01-28', 250])

    def test_boj_dates_and_cgpi_yoy(self):
        self.assertEqual(jp._boj_date(20260918, 'daily'), '2026-09-18')
        self.assertEqual(jp._boj_date(202608, 'monthly'), '2026-08-01')
        self.assertEqual(jp._boj_date(202603, 'quarterly'), '2026-09-01')
        data = raw(BOJ_CGPI=[['2025-01-01', 100], ['2026-01-01', 103]])
        self.assertAlmostEqual(f.calculate(data)['jp_cgpi'][-1][1], 3)

    def test_boj_response_validation(self):
        payload = {'STATUS': 200, 'RESULTSET': [{'SERIES_CODE': 'A'}]}
        self.assertEqual(jp._boj_rows(payload, ('A',))[0]['SERIES_CODE'], 'A')
        with self.assertRaisesRegex(ValueError, 'omitted series'):
            jp._boj_rows(payload, ('A', 'B'))

    def test_calendar_lags_do_not_skip_missing_months(self):
        data = raw(CPILFESL=[['2025-01-01', 100], ['2025-03-01', 110],
                             ['2026-01-01', 105], ['2026-02-01', 106]])
        points = dict(f.calculate(data)['cpi'])
        self.assertAlmostEqual(points['2026-01-01'], 5)
        self.assertIsNone(points['2026-02-01'])

    def test_jobs_do_not_compare_nonconsecutive_months(self):
        data = raw(PAYEMS=[['2026-01-01', 100], ['2026-04-01', 130]])
        self.assertIsNone(f.calculate(data)['jobs'][-1][1])

    def test_pce_yoy_and_annualization(self):
        data = raw(PCEPILFE=[['2025-01-01', 100], ['2025-10-01', 104], ['2026-01-01', 108]])
        result = f.calculate(data)
        self.assertAlmostEqual(result['pce'][-1][1], 8)
        self.assertAlmostEqual(result['pce_secondary'][-1][1], ((108/104)**4-1)*100)

    def test_census_error_detail_redacts_raw_and_url_encoded_keys(self):
        class Response:
            text = 'invalid request: key=ABC123 and key=%41BC123'

        detail = f.census_error_detail(Response(), 'ABC123')
        self.assertNotIn('ABC123', detail)
        self.assertIn('[REDACTED]', detail)

    def test_census_fetch_uses_documented_from_year_filter(self):
        with (patch('curl_cffi.requests.get') as mock_get,
              patch.dict('os.environ', {'CENSUS_API_KEY': 'test-key'})):
            mock_get.return_value.status_code = 200
            mock_get.return_value.json.return_value = [
                ['data_type_code', 'time_slot_id', 'seasonally_adj', 'category_code',
                 'cell_value', 'error_data', 'time_slot_date'],
                ['MPCSM', '757', 'yes', '44X72', '1.7', 'no', '2026-06'],
            ]
            points = f.fetch_census_marts('MPCSM', '44X72')
        self.assertEqual(points, [['2026-06-01', 1.7]])
        params = mock_get.call_args.kwargs['params']
        self.assertEqual(params['time'], 'from 1992')
        self.assertEqual(params['category_code'], '44X72')
        self.assertEqual(params['data_type_code'], 'MPCSM')
        self.assertEqual(params['seasonally_adj'], 'yes')

    def test_census_fetch_uses_requested_industry_category(self):
        with (patch('curl_cffi.requests.get') as mock_get,
              patch.dict('os.environ', {'CENSUS_API_KEY': 'test-key'})):
            mock_get.return_value.status_code = 200
            mock_get.return_value.json.return_value = [
                ['data_type_code', 'time_slot_id', 'seasonally_adj', 'category_code',
                 'cell_value', 'error_data', 'time_slot_date'],
                ['MPCSM', '757', 'yes', '722', '0.6', 'no', '2026-06'],
            ]
            points = f.fetch_census_marts('MPCSM', '722')
        self.assertEqual(points, [['2026-06-01', 0.6]])
        self.assertEqual(mock_get.call_args.kwargs['params']['category_code'], '722')

    def test_census_marts_parser_and_direct_month_over_month_series(self):
        payload = [
            ['data_type_code', 'time_slot_id', 'seasonally_adj', 'category_code',
             'cell_value', 'error_data', 'time_slot_date'],
            ['MPCSM', '757', 'yes', '44X72', '1.7', 'no', '2026-06'],
            ['MPCSM', '758', 'yes', '44X72', '-0.4', 'no', '2026-07'],
            ['SM', '759', 'yes', '44X72', '123', 'no', '2026-08'],
        ]
        points = f.parse_census_marts(payload, 'MPCSM')
        self.assertEqual(points, [['2026-06-01', 1.7], ['2026-07-01', -0.4]])
        data = raw(
            CENSUS_MARTS_SM=[['2025-06-01', 100], ['2026-06-01', 110]],
            CENSUS_MARTS_MPCSM=points,
        )
        result = f.calculate(data)
        self.assertAlmostEqual(dict(result['retail'])['2026-06-01'], 10)
        self.assertEqual(result['retail_mom'], points)
        self.assertEqual(next(item for item in f.SPECS if item[0] == 'retail_mom')[4],
                         ['CENSUS_MARTS_MPCSM'])

    def test_monthly_real_pce_components_use_actual_previous_month(self):
        data = raw(
            PCEC96=[['2026-05-01', 100], ['2026-06-01', 101], ['2026-07-01', 103.02]],
            PCENDC96=[['2026-05-01', 100], ['2026-06-01', 98], ['2026-07-01', 99.96]],
            PCEDGC96=[['2026-05-01', 100], ['2026-06-01', 105], ['2026-07-01', 104.475]],
            PCESC96=[['2026-05-01', 100], ['2026-06-01', 100.5], ['2026-07-01', 101.0025]],
        )
        result = f.calculate(data)
        expected = {'real_pce': (1, 2), 'real_pce_nondurable': (-2, 2),
                    'real_pce_durable': (5, -0.5), 'real_pce_services': (0.5, 0.5)}
        for mid, (june, july) in expected.items():
            points = dict(result[mid])
            self.assertIsNone(points['2026-05-01'])
            self.assertAlmostEqual(points['2026-06-01'], june)
            self.assertAlmostEqual(points['2026-07-01'], july)

        # A missing June observation must not turn July into a two-month comparison.
        missing = f.calculate(raw(PCEC96=[['2026-05-01', 100], ['2026-07-01', 103]]))
        self.assertIsNone(dict(missing['real_pce'])['2026-07-01'])
        for mid in expected:
            spec = next(item for item in f.SPECS if item[0] == mid)
            self.assertEqual(spec[5], 1)
            self.assertEqual(spec[6], '1개월 전 대비')

    def test_real_gdp_sources_use_four_quarter_yoy(self):
        dates = ['2025-01-01', '2025-04-01', '2025-07-01', '2025-10-01', '2026-01-01']
        data = raw(
            GDPC1=[[d, value] for d, value in zip(dates, [100, 101, 102, 103, 110])],
            JPNRGDPEXP=[[d, value] for d, value in zip(dates, [100, 101, 102, 103, 104])],
        )
        result = f.calculate(data)
        for mid, expected in [('us_real_gdp', 10), ('jp_real_gdp', 4)]:
            self.assertAlmostEqual(result[mid][-1][1], expected)

    def test_additional_us_series_are_registered(self):
        required = {
            'philly_fed', 'umich_sentiment', 'sticky_cpi', 'trimmed_pce',
        }
        self.assertTrue(required.issubset({spec[0] for spec in f.SPECS}))
        data = raw(
            GACDFSA066MSFRBPHI=[['2026-08-01', 12.5]],
            UMCSENT=[['2026-08-01', 58.2]],
            CORESTICKM159SFRBATL=[['2026-08-01', 2.8]],
            PCETRIM12M159SFRBDAL=[['2026-08-01', 2.6]],
        )
        result = f.calculate(data)
        self.assertEqual(result['philly_fed'][-1][1], 12.5)
        self.assertEqual(result['umich_sentiment'][-1][1], 58.2)

    def test_retired_korea_sources_are_not_requested_or_rebuilt(self):
        removed_sources = {'KOSIS_KR_INDUSTRIAL_PRODUCTION',
                           'KOSIS_KR_RETAIL', 'KOSIS_KR_UNEMPLOYMENT', 'ECOS_KR_REAL_GDP'}
        removed_metrics = {'kr_industrial_production', 'kr_retail', 'kr_unemployment', 'kr_real_gdp'}
        self.assertFalse(removed_sources & set(f.FREQUENCIES))
        historical = {sid: {'points': [], 'error': 'old failure'}
                      for sid in removed_sources}
        historical['^N225'] = {'points': [['2026-09-30', 40000]],
                               'error': 'Nikkei failure'}
        self.assertEqual(f.active_source_errors(historical), ['^N225'])
        previous = {'metrics': [{'id': mid, 'value': 1} for mid in removed_metrics]}
        rebuilt = f.build(historical, previous, '2026-10-02T00:00:00+00:00')
        self.assertFalse(removed_metrics & {m['id'] for m in rebuilt['metrics']})
        self.assertEqual(rebuilt['dcfInputs']['nikkei225']['indexPoints'],
                         historical['^N225']['points'])

    def test_claims_require_four_consecutive_weeks(self):
        days = ['2026-08-01', '2026-08-08', '2026-08-15', '2026-08-22']
        data = raw(ICSA=[[d, v] for d, v in zip(days, [200000, 220000, 240000, 260000])])
        self.assertEqual(f.calculate(data)['claims'][-1][1], 230)
        data['ICSA']['points'].pop(1)
        self.assertIsNone(f.calculate(data)['claims'][-1][1])

    def test_netliq_no_fill_or_future_input(self):
        data = raw(WALCL=[['2026-08-26', 8000000], ['2026-09-02', 8100000]],
                   TREASURY_TGA=[['2026-08-26', 500000], ['2026-09-03', 600000]],
                   RRPONTSYD=[['2026-08-26', 200], ['2026-09-01', 210]])
        self.assertEqual(f.calculate(data)['netliq'], [['2026-08-26', 7300], ['2026-09-02', None]])

    def test_units_and_same_date_spreads(self):
        d = '2026-09-01'
        data = raw(WRESBAL=[[d, 3000000]], SOFR=[[d, 5.4]], IORB=[[d, 5.3]],
                   BAMLH0A0HYM2=[[d, 3.2]], DGS10=[[d, 4.2]], DGS2=[[d, 3.8]],
                   VIXCLS=[[d, 18]], DFII10=[[d, 1.8]], UNRATE=[[d, 4.1]])
        result = f.calculate(data)
        for mid, expected in [('reserves', 3000), ('repo', 10), ('credit', 320),
                              ('curve', .4), ('vix', 18), ('real', 1.8), ('unemployment', 4.1)]:
            self.assertAlmostEqual(result[mid][-1][1], expected)
        data['DGS2']['points'] = [['2026-09-02', 3.8]]
        self.assertIsNone(f.calculate(data)['curve'][-1][1])

    def test_market_rolling_windows_and_warmup(self):
        days = [(date(2025, 1, 1)+timedelta(days=i)).isoformat() for i in range(201)]
        data = raw(RSP=[[d, 100] for d in days], SPY=[[d, 200] for d in days])
        data['^GSPC'] = {'points': [[d, 100 if i < 200 else 200] for i, d in enumerate(days)]}
        result = f.calculate(data)
        self.assertIsNone(result['trend'][198][1])
        self.assertEqual(result['trend'][199][1], 0)
        self.assertAlmostEqual(result['trend'][200][1], (200/100.5-1)*100)
        self.assertIsNone(result['participation'][123][1])
        self.assertEqual(result['participation'][124][1], 0)

    def test_m2_and_missing_values(self):
        data = raw(M2SL=[['2025-01-01', 100], ['2026-01-01', 110]])
        self.assertAlmostEqual(f.calculate(data)['m2'][-1][1], 10)
        self.assertEqual(f.clean([['1990-01-01', '1'], ['1990-01-02', '.'],
                                  ['1990-01-03', 'nan'], ['2999-01-01', 5]]),
                         [['1990-01-01', 1.0], ['1990-01-02', None], ['1990-01-03', None]])

    def test_buffett_proxy_converts_market_cap_millions_to_gdp_billions(self):
        data = raw(
            BOGZ1FL883164113Q=[['2026-04-01', 80_000_000]],
            GDP=[['2026-04-01', 32_000]],
        )
        self.assertAlmostEqual(f.calculate(data)['buffett'][-1][1], 250)

class FallbackTests(unittest.TestCase):
    def test_retired_cache_error_does_not_fail_active_sources(self):
        cache = {
            'WILL5000IND': {'error': 'HTTP 404'},
            'GDP': {'error': None},
            'UNRATE': {'error': 'timeout'},
        }
        self.assertEqual(f.active_source_errors(cache, {'GDP'}), [])
        self.assertEqual(f.active_source_errors(cache, {'GDP', 'UNRATE'}), ['UNRATE'])

    def test_provider_timeout_returns_fallback(self):
        with patch.object(
            f.subprocess,
            'run',
            side_effect=f.subprocess.TimeoutExpired('worker', 60),
        ):
            sid, entry, error = f.fetch_bounded('UNRATE')

        self.assertEqual(sid, 'UNRATE')
        self.assertIsNone(entry)
        self.assertIn('60s deadline', error)

    def test_last_value_retained_without_filling_points(self):
        data = raw(UNRATE=[['2026-07-01', 4.2], ['2026-08-01', None]])
        metric = f.build(data, {}, None, date(2026, 9, 7))['metrics'][1]
        self.assertEqual((metric['date'], metric['value'], metric['status']), ('2026-07-01', 4.2, 'stale'))
        self.assertIsNone(metric['points'][-1][1])
        self.assertTrue(metric['sources'][0]['fallback'])

    def test_failed_dependency_preserves_metric_and_provenance(self):
        data = raw(UNRATE=[['2026-08-01', 4.2]])
        previous = f.build(data, {}, '2026-09-06T00:00:00+00:00', date(2026, 9, 7))
        data['UNRATE']['error'] = 'timeout'
        data['UNRATE']['points'] = []
        result = f.build(data, previous, previous['generatedAt'], date(2026, 9, 8))
        m = result['metrics'][1]
        self.assertEqual(m['value'], 4.2)
        self.assertEqual(m['status'], 'stale')
        self.assertTrue(m['sources'][0]['fallback'])
        self.assertEqual(m['sources'][0]['retrieved'], previous['metrics'][1]['sources'][0]['retrieved'])

    def test_total_failure_keeps_generation_time(self):
        with tempfile.TemporaryDirectory() as directory:
            output, cache = Path(directory)/'data.json', Path(directory)/'raw.json'
            data = raw(UNRATE=[['2026-08-01', 4.2]])
            timestamp = '2026-09-06T00:00:00+00:00'
            f.atomic_json(cache, data)
            f.atomic_json(output, f.build(data, {}, timestamp))
            with patch.object(f, 'fetch_bounded', side_effect=lambda sid: (sid, None, 'timeout')):
                self.assertEqual(f.refresh(output, cache), 0)
            result = json.loads(output.read_text(encoding='utf-8'))
            self.assertEqual(result['generatedAt'], timestamp)
            self.assertEqual(result['metrics'][1]['value'], 4.2)

    def test_refresh_accepts_empty_optional_manual_source(self):
        with tempfile.TemporaryDirectory() as directory:
            output, cache = Path(directory)/'data.json', Path(directory)/'raw.json'
            entry = {'points': [], 'retrieved': None, 'error': None}
            with patch.object(f, 'download_all', return_value=[
                ('INDEX_DCF_SP500_VALUE', entry, None),
            ]):
                self.assertEqual(f.refresh(output, cache), 1)
            stored = json.loads(cache.read_text(encoding='utf-8'))
            self.assertEqual(stored['INDEX_DCF_SP500_VALUE']['points'], [])
            self.assertIsNone(stored['INDEX_DCF_SP500_VALUE']['error'])

    def test_stale_daily_source_and_empty_bootstrap(self):
        data = raw(VIXCLS=[['2026-01-01', 18]])
        result = f.build(data, {}, None, date(2026, 9, 7))
        self.assertEqual(result['metrics'][12]['status'], 'stale')
        self.assertTrue(result['metrics'][12]['sources'][0]['fallback'])
        self.assertEqual(result['metrics'][0]['status'], 'missing')


if __name__ == '__main__':
    unittest.main()

