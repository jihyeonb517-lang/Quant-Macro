import io
import json
import unittest
from datetime import date
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

import fetch_data as f
import treasury_sources as t


def row(day, label, opening='null', closing='null'):
    return dict(record_date=day, account_type=label,
                open_today_bal=opening, close_today_bal=closing)


def response(rows, pages=1):
    return io.BytesIO(json.dumps({
        'data': rows, 'meta': {
            'total-pages': pages,
            'dataFormats': {'close_today_bal': '$1,000,000',
                            'open_today_bal': '$1,000,000'},
        },
    }).encode())


class TreasuryTests(unittest.TestCase):
    def test_account_schema_changes_and_exclusion_of_opening_and_other_accounts(self):
        # First and latest samples checked against Fiscal Data on 2026-10-07;
        # transition values below are synthetic to distinguish the fields.
        points = t.parse_points([
            row('2005-10-03', 'Federal Reserve Account', '4381', '5448'),
            row('2021-10-01', 'Treasury General Account (TGA)', '900', '1000'),
            row('2026-10-05', 'Treasury General Account (TGA) Opening Balance', '871181'),
            row('2026-10-05', 'Total TGA Deposits (Table II)', '29117'),
            row('2026-10-05', 'Treasury General Account (TGA) Closing Balance', '883335'),
            row('2005-10-03', 'Tax and Loan Note Accounts', '5555', '7777'),
        ])
        self.assertEqual(points, [['2005-10-03', 5448], ['2021-10-01', 1000],
                                  ['2026-10-05', 883335]])

    def test_nulls_retained_and_bad_or_conflicting_balances_rejected(self):
        closing = 'Treasury General Account (TGA) Closing Balance'
        self.assertEqual(t.parse_points([row('2026-10-01', closing, '893699'),
                                         row('2026-10-02', closing)]),
                         [['2026-10-01', 893699], ['2026-10-02', None]])
        for value in ['nan', 'inf', '-1', 'unknown']:
            with self.subTest(value=value), self.assertRaises(ValueError):
                t.parse_points([row('2026-10-01', closing, value)])
        with self.assertRaisesRegex(ValueError, 'conflicting'):
            t.parse_points([row('2026-10-01', closing, '1'), row('2026-10-01', closing, '2')])
        with self.assertRaisesRegex(ValueError, 'no usable'):
            t.parse_points([row('2026-10-01', 'Unknown Account', '100')])

    def test_download_pagination_and_keyless_requests(self):
        with patch.object(t, 'urlopen', side_effect=[
            response([row('2026-10-02', 'Treasury General Account (TGA) Closing Balance', '871181')], 2),
            response([row('2026-10-05', 'Treasury General Account (TGA) Closing Balance', '883335')], 2),
        ]) as request:
            self.assertEqual(t.fetch_points(), [['2026-10-02', 871181], ['2026-10-05', 883335]])
        self.assertEqual(request.call_count, 2)
        for number, call in enumerate(request.call_args_list, 1):
            query = parse_qs(urlsplit(call.args[0].full_url).query)
            self.assertEqual(query['page[number]'], [str(number)])
            self.assertEqual(set(query), {'fields', 'filter', 'sort', 'page[size]', 'page[number]'})

    def test_invalid_units_and_non_json_rejected(self):
        with patch.object(t, 'urlopen', return_value=io.BytesIO(b'<html>outage</html>')):
            with self.assertRaises(ValueError):
                t.fetch_points()
        bad_units = {'data': [{}], 'meta': {'total-pages': 1, 'dataFormats': {}}}
        with patch.object(t, 'urlopen', return_value=io.BytesIO(json.dumps(bad_units).encode())):
            with self.assertRaisesRegex(ValueError, 'units'):
                t.fetch_points()

    def test_daily_units_delta_and_weekly_exact_date_liquidity(self):
        raw = {
            'TREASURY_TGA': {'points': [['2026-09-30', 984046], ['2026-10-01', 893699],
                                      ['2026-10-02', 871181], ['2026-10-05', 883335]]},
            'WALCL': {'points': [['2026-09-30', 8000000], ['2026-10-07', 8000000]]},
            'RRPONTSYD': {'points': [['2026-09-30', 20], ['2026-10-05', 21]]},
        }
        data = f.build(raw, {}, None, date(2026, 10, 7))
        metrics = {m['id']: m for m in data['metrics']}
        self.assertEqual(metrics['tga']['date'], '2026-10-05')
        self.assertAlmostEqual(metrics['tga']['value'], 883.335)
        self.assertAlmostEqual(metrics['tga']['delta'], 12.154)
        self.assertEqual(len(metrics['tga']['points']), 4)  # No weekend fill.
        self.assertEqual(metrics['tga']['sources'][0]['frequency'], 'daily')
        self.assertEqual(metrics['tga']['sources'][0]['origin'], 'U.S. Treasury Fiscal Data')
        self.assertEqual(metrics['netliq']['points'][-1], ['2026-10-07', None])
        self.assertAlmostEqual(metrics['netliq']['value'], 6995.954)
        self.assertNotIn('WTREGEN', f.FREQUENCIES)

    def test_old_weekly_snapshot_is_not_reused_as_a_daily_fallback(self):
        previous = {'metrics': [{'id': 'tga', 'formula': 'old weekly definition',
                                 'period': '4주 전 대비', 'value': 948.674,
                                 'date': '2026-09-30', 'note': 'old note'}]}
        data = f.build({}, previous, None, date(2026, 10, 7))
        metric = next(m for m in data['metrics'] if m['id'] == 'tga')
        self.assertEqual(metric['status'], 'missing')
        self.assertIsNone(metric['value'])
        self.assertEqual(metric['note'], t.NOTE)
        # A later outage preserves an actual daily Treasury snapshot.
        raw = {'TREASURY_TGA': {'points': [['2026-10-05', 883335]]}}
        previous = f.build(raw, {}, None, date(2026, 10, 7))
        raw['TREASURY_TGA'].update(points=[], error='timeout')
        metric = next(m for m in f.build(raw, previous, None)['metrics'] if m['id'] == 'tga')
        self.assertEqual(metric['value'], 883.335)
        self.assertEqual(metric['sources'][0]['id'], 'TREASURY_TGA')
        self.assertTrue(metric['sources'][0]['fallback'])


if __name__ == '__main__':
    unittest.main()
