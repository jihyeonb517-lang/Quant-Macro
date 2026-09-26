import copy
import json
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

import fetch_data as f
import japan_sources as jp
import korea_sources as kr


def raw(**series):
    return {sid: {'points': points, 'retrieved': '2026-09-07T00:00:00+00:00', 'error': None}
            for sid, points in series.items()}


class FormulaTests(unittest.TestCase):
    def test_payroll_total_private_and_government_use_comparable_three_month_changes(self):
        data = raw(
            PAYEMS=[['2026-01-01', 1000], ['2026-02-01', 1010], ['2026-03-01', 1020], ['2026-04-01', 1030]],
            USPRIV=[['2026-01-01', 800], ['2026-02-01', 808], ['2026-03-01', 816], ['2026-04-01', 824]],
            USGOVT=[['2026-01-01', 200], ['2026-02-01', 202], ['2026-03-01', 204], ['2026-04-01', 206]],
        )
        calculated = f.calculate(data)
        self.assertEqual(calculated['jobs'][-1], ['2026-04-01', 10])
        self.assertEqual(calculated['jobs_private'][-1], ['2026-04-01', 8])
        self.assertEqual(calculated['jobs_government'][-1], ['2026-04-01', 2])

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

    def test_jobs_exact_three_month_difference(self):
        data = raw(PAYEMS=[['2026-01-01', 100], ['2026-04-01', 130]])
        self.assertEqual(f.calculate(data)['jobs'][-1][1], 10)

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
            CENSUS_MARTS_MPCSM=[['2026-06-01', 1.7], ['2026-07-01', -0.4]],
        )
        result = f.calculate(data)
        retail_yoy = dict(result['retail'])
        retail_mom = dict(result['retail_mom'])
        self.assertAlmostEqual(retail_yoy['2026-06-01'], 10)
        self.assertAlmostEqual(retail_mom['2026-06-01'], 1.7)
        self.assertAlmostEqual(retail_mom['2026-07-01'], -0.4)
        self.assertEqual(next(item for item in f.SPECS if item[0] == 'retail')[4], ['CENSUS_MARTS_SM'])
        self.assertEqual(next(item for item in f.SPECS if item[0] == 'retail_mom')[4], ['CENSUS_MARTS_MPCSM'])

    def test_real_gdp_and_pce_components_use_four_quarter_yoy(self):
        dates = ['2025-01-01', '2025-04-01', '2025-07-01', '2025-10-01', '2026-01-01']
        data = raw(
            GDPC1=[[d, value] for d, value in zip(dates, [100, 101, 102, 103, 110])],
            PCECC96=[[d, value] for d, value in zip(dates, [200, 202, 204, 206, 210])],
            PCNDGC96=[[d, value] for d, value in zip(dates, [100, 101, 102, 103, 105])],
            PCDGCC96=[[d, value] for d, value in zip(dates, [50, 51, 52, 53, 55])],
            PCESVC96=[[d, value] for d, value in zip(dates, [150, 151, 152, 153, 157.5])],
            JPNRGDPEXP=[[d, value] for d, value in zip(dates, [100, 101, 102, 103, 104])],
            ECOS_KR_REAL_GDP=[[d, value] for d, value in zip(dates, [100, 101, 102, 103, 106])],
        )
        result = f.calculate(data)
        for mid, expected in [('us_real_gdp', 10), ('real_pce', 5),
                              ('real_pce_nondurable', 5), ('real_pce_durable', 10),
                              ('real_pce_services', 5), ('jp_real_gdp', 4),
                              ('kr_real_gdp', 6)]:
            self.assertAlmostEqual(result[mid][-1][1], expected)

    def test_added_official_korea_series_are_registered(self):
        required = {
            'philly_fed', 'umich_sentiment', 'sticky_cpi', 'trimmed_pce',
            'kr_exports', 'kr_industrial_production', 'kr_retail',
            'kr_unemployment', 'kr_real_gdp', 'kr_semiconductor_exports',
        }
        self.assertTrue(required.issubset({spec[0] for spec in f.SPECS}))
        korea_deps = {
            sid for spec in f.SPECS if spec[1] == 'korea' for sid in spec[4]
        }
        self.assertTrue(all(sid.startswith(('ECOS_', 'KOSIS_')) or sid == 'KRW=X'
                            for sid in korea_deps))
        data = raw(
            GACDFSA066MSFRBPHI=[['2026-08-01', 12.5]],
            UMCSENT=[['2026-08-01', 58.2]],
            CORESTICKM159SFRBATL=[['2026-08-01', 2.8]],
            PCETRIM12M159SFRBDAL=[['2026-08-01', 2.6]],
            ECOS_KR_EXPORT_VALUE=[['2025-08-01', 100], ['2026-08-01', 104.1]],
            KOSIS_KR_INDUSTRIAL_PRODUCTION=[['2025-08-01', 100], ['2026-08-01', 101.7]],
            KOSIS_KR_RETAIL=[['2025-08-01', 100], ['2026-08-01', 102.2]],
            KOSIS_KR_UNEMPLOYMENT=[['2026-08-01', 2.7]],
        )
        result = f.calculate(data)
        self.assertEqual(result['philly_fed'][-1][1], 12.5)
        self.assertEqual(result['umich_sentiment'][-1][1], 58.2)
        self.assertAlmostEqual(result['kr_exports'][-1][1], 4.1)
        self.assertAlmostEqual(result['kr_industrial_production'][-1][1], 1.7)
        self.assertAlmostEqual(result['kr_retail'][-1][1], 2.2)
        self.assertEqual(result['kr_unemployment'][-1][1], 2.7)

    def test_korea_gdp_requires_same_quarter_and_filters_implausible_growth(self):
        data = raw(ECOS_KR_REAL_GDP=[
            ['2024-01-01', 100], ['2024-04-01', 101],
            ['2025-01-01', 102], ['2025-04-01', 10000],
            ['2026-04-01', 103],
        ])
        points = dict(f.calculate(data)['kr_real_gdp'])
        self.assertAlmostEqual(points['2025-01-01'], 2)
        self.assertIsNone(points['2025-04-01'])
        self.assertIsNone(points['2026-04-01'])

    def test_official_api_helper_parsing_and_item_selection(self):
        self.assertEqual(kr._points([
            {'TIME': '2026Q2', 'DATA_VALUE': '3.4'},
            {'TIME': '2026Q1', 'DATA_VALUE': '2.1'},
        ], 'TIME', 'DATA_VALUE', 'Q'),
            [['2026-01-01', 2.1], ['2026-04-01', 3.4]])
        rows = [
            {'GRP_CODE': 'Group1', 'CYCLE': 'M', 'ITEM_NAME': '전국', 'ITEM_CODE': 'A'},
            {'GRP_CODE': 'Group1', 'CYCLE': 'M', 'ITEM_NAME': '서울', 'ITEM_CODE': 'B'},
            {'GRP_CODE': 'Group2', 'CYCLE': 'M', 'ITEM_NAME': '총계', 'ITEM_CODE': 'C'},
        ]
        selected = kr._select_ecos_codes(rows, {'cycle': 'M', 'item': ('총계',)})
        self.assertEqual(selected, ['A', 'C'])

    def test_korean_api_sources_have_frequency_and_no_embedded_keys(self):
        self.assertEqual(f.FREQUENCIES['ECOS_KR_BASE_RATE'], 'daily')
        self.assertEqual(f.FREQUENCIES['ECOS_KR_REAL_GDP'], 'quarterly')
        self.assertEqual(f.FREQUENCIES['ECOS_KR_CPI'], 'monthly')
        self.assertNotIn('ECOS_API_KEY =', Path(f.__file__).read_text(encoding='utf-8'))

    def test_korean_api_errors_do_not_echo_credentials(self):
        with patch.object(kr, 'urlopen', side_effect=RuntimeError(
                'request failed for https://kosis.kr/?apiKey=secret-value')):
            with self.assertRaises(RuntimeError) as raised:
                kr._json('https://kosis.kr/?apiKey=secret-value')
        self.assertNotIn('secret-value', str(raised.exception))

    def test_kosis_requires_single_unambiguous_national_series(self):
        rows = [
            {'PRD_DE': '202608', 'C1': '00', 'C1_NM': '전국', 'DT': '104.2'},
            {'PRD_DE': '202608', 'C1': '01', 'C1_NM': '서울', 'DT': '110.7'},
        ]
        with patch.dict('os.environ', {'KOSIS_API_KEY': 'test'}), \
             patch.object(kr, '_kosis_search', return_value=[
                 {'TBL_NM': '소매판매액지수', 'TBL_ID': 'T', 'ORG_ID': '101'}]), \
             patch.object(kr, '_kosis_meta', return_value=[
                 {'OBJ_ID': 'ITEM', 'ITM_NM': '소매판매액지수', 'ITM_ID': 'I'},
                 {'OBJ_ID': 'C1', 'ITM_NM': '전국', 'ITM_ID': '00'},
                 {'OBJ_ID': 'C1', 'ITM_NM': '서울', 'ITM_ID': '01'}]), \
             patch.object(kr, '_kosis_data', return_value=rows):
            self.assertEqual(kr.fetch_kosis('KOSIS_KR_RETAIL'), [['2026-08-01', 104.2]])

    def test_kosis_rejects_conflicting_duplicate_periods(self):
        rows = [
            {'PRD_DE': '202608', 'C1': '00', 'C1_NM': '전국', 'DT': '104.2'},
            {'PRD_DE': '202608', 'C1': '00', 'C1_NM': '전국', 'DT': '110.7'},
        ]
        with patch.dict('os.environ', {'KOSIS_API_KEY': 'test'}), \
             patch.object(kr, '_kosis_search', return_value=[
                 {'TBL_NM': '소매판매액지수', 'TBL_ID': 'T', 'ORG_ID': '101'}]), \
             patch.object(kr, '_kosis_meta', return_value=[
                 {'OBJ_ID': 'ITEM', 'ITM_NM': '소매판매액지수', 'ITM_ID': 'I'},
                 {'OBJ_ID': 'C1', 'ITM_NM': '전국', 'ITM_ID': '00'}]), \
             patch.object(kr, '_kosis_data', return_value=rows):
            with self.assertRaisesRegex(ValueError, '하나로 좁혀지지 않았습니다'):
                kr.fetch_kosis('KOSIS_KR_RETAIL')

    def test_claims_require_four_consecutive_weeks(self):
        days = ['2026-08-01', '2026-08-08', '2026-08-15', '2026-08-22']
        data = raw(ICSA=[[d, v] for d, v in zip(days, [200000, 220000, 240000, 260000])])
        self.assertEqual(f.calculate(data)['claims'][-1][1], 230)
        data['ICSA']['points'].pop(1)
        self.assertIsNone(f.calculate(data)['claims'][-1][1])

    def test_netliq_no_fill_or_future_input(self):
        data = raw(WALCL=[['2026-08-26', 8000000], ['2026-09-02', 8100000]],
                   WTREGEN=[['2026-08-26', 500000], ['2026-09-03', 600000]],
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

    def test_korea_metric_unit_conversions(self):
        data = raw(
            ECOS_KR_RESERVES=[['2026-08-01', 421]],
            ECOS_KR_HOUSEHOLD_CREDIT=[['2026-04-01', 2_300_000]],
            ECOS_KR_HOUSE_PRICES=[['2026-04-01', 143.2]],
        )
        result = f.calculate(data)
        self.assertEqual(result['kr_reserves_bok'], [['2026-08-01', 421]])
        self.assertEqual(result['kr_household_credit_bok'], [['2026-04-01', 2300]])
        self.assertEqual(result['kr_house_prices_ecos'], [['2026-04-01', 143.2]])

    def test_korea_cli_source_is_registered(self):
        self.assertEqual(f.CLI_SOURCES['OECD_CLI_KR'], 'kr')
        self.assertEqual(f.FREQUENCIES['OECD_CLI_KR'], 'monthly')

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

